"""Codex's private app-server lifecycle and structured input delivery.

See docs/CODEX-DELIVERY.md for receipts, recovery, and protocol limitations.
"""
from __future__ import annotations

import collections
import contextlib
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid

from .codex_usage import CodexUsage

logger = logging.getLogger(__name__)
START_TIMEOUT = 120.0
RPC_TIMEOUT_ENV = 'LMER_CODEX_RPC_TIMEOUT'
DEFAULT_RPC_TIMEOUT = 10.0
MAX_RPC_TIMEOUT = 600.0
RUNTIME_INTERVAL = 5.0
PENDING_INTERVAL = .5
# Escalation waits this long with the thread not running a turn and the input
# still absent: a steer sent during a long tool call is only taken when the
# call returns, so wall-clock time since submission says nothing.
CONFIRM_DEADLINE = 120.0
MAX_CONFIRM_WAIT = 1800.0
RETRY_INTERVAL = 2.0
TURN_PAGE = 20
MAX_TURN_PAGES = 10
# Codex stamps turns in whole seconds; this also absorbs a steer landing in a
# turn that started just before the input was submitted.
TURN_START_SLACK = 60
LATENCY_WINDOW = 200
MAX_RECEIPTS = 10000
ENDED_TURN = ('completed', 'interrupted', 'failed')
# JSON-RPC "method not found" / "invalid request": what an older Codex answers a
# method it does not have. Anything else from thread/turns/list is not a reason to
# give up paging for the run.
UNSUPPORTED_METHOD = (-32601, -32600)
OUTAGE_FAILURES = 2
# One submission's RPC deadlines: open + initialize, discovery (its last read may
# start just inside its own deadline), turn/start.
SUBMIT_RPCS = 5
# How long a caller-owned deliver() waits behind an observation before saying
# "nothing sent" instead of outliving the daemon's request.
LOCK_WAIT_RPCS = 2


def rpc_timeout(environ=None):
    """The per-RPC deadline in seconds, from ``LMER_CODEX_RPC_TIMEOUT``."""
    raw = ((os.environ if environ is None else environ).get(RPC_TIMEOUT_ENV) or '').strip()
    if not raw:
        return DEFAULT_RPC_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not (0 < value <= MAX_RPC_TIMEOUT):
        logger.warning('%s=%r is not a number of seconds in (0, %s]; using %s',
                       RPC_TIMEOUT_ENV, raw, MAX_RPC_TIMEOUT, DEFAULT_RPC_TIMEOUT)
        return DEFAULT_RPC_TIMEOUT
    return value


def input_budget(timeout=None):
    """Worst case for one caller-owned :meth:`CodexDelivery.deliver`, in seconds.

    The daemon's ``/input`` read timeout is sized from this, so that a timeout
    there means a wedged session rather than a slow submission it was told failed.
    """
    return (LOCK_WAIT_RPCS + SUBMIT_RPCS) * (rpc_timeout() if timeout is None else timeout)


class CodexDeliveryError(OSError):
    def __init__(self, *args, rpc_code=None):
        super().__init__(*args)
        self.rpc_code = rpc_code


class CodexDelivery:
    def __init__(self, command, *, notify=None, escalate=None, deescalate=None):
        self.command = list(command)
        self.notify = notify or logger.warning
        # escalate(receipt, reason, text) -> handle; deescalate(handle, receipt).
        self.escalate = escalate
        self.deescalate = deescalate
        self.rpc_timeout = rpc_timeout()
        self._latency = collections.deque(maxlen=LATENCY_WINDOW)
        self._since = {}
        self._idle_since = {}
        self._retry_at = {}
        self._escalate_at = {}
        self._turns_list = None
        self._observation_down = None
        self._observation_failures = 0
        self.directory = None
        self.backend = None
        self._log = None
        self._ws = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._monitor = None
        self._sequence = 0
        self._receipts = {}
        self._thread_id = None
        self._runtime = None
        self._next_runtime_read = 0.0
        self._warnings = set()
        self._closed = False
        self._startup_failed = False
        self._usage = CodexUsage()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @property
    def closed(self):
        return self._closed

    def _warn(self, text, *, once=True):
        if once and text in self._warnings:
            return
        self._warnings.add(text)
        try:
            self.notify(text)
        except Exception:
            logger.exception('Could not publish Codex delivery notice')

    def start(self):
        """Start after supervisor credentials are set, before its PTY fork."""
        if any(arg == '--remote' or arg.startswith('--remote=') for arg in self.command):
            raise CodexDeliveryError('Codex delivery requires its own private backend; --remote is unsupported')
        if any(arg in ('--ephemeral', '-p', '--profile') or arg.startswith('--profile=') for arg in self.command):
            raise CodexDeliveryError('Managed Codex delivery requires persistent history and the shared config; ephemeral/profile overrides are unsupported')
        self.directory = Path(tempfile.mkdtemp(prefix='lmer-codex-'))
        self.socket = self.directory / 'server.sock'
        self.outbox = self.directory / 'delivery.json'
        self._log = (self.directory / 'backend.log').open('wb')
        overrides = ['-c', 'check_for_update_on_startup=false']
        args = iter(self.command[1:])
        for arg in args:
            if arg in ('-c', '--config'):
                overrides.extend(['-c', next(args)])
            elif arg.startswith('--config='):
                overrides.extend(['-c', arg.split('=', 1)[1]])
            elif arg in ('-m', '--model'):
                overrides.extend(['-c', 'model=' + json.dumps(next(args))])
            elif arg.startswith('--model='):
                overrides.extend(['-c', 'model=' + json.dumps(arg.split('=', 1)[1])])
        self.backend = subprocess.Popen(
            [self.command[0], *overrides, 'app-server', '--listen', 'unix://' + str(self.socket)],
            stdin=subprocess.DEVNULL, stdout=self._log, stderr=self._log,
            start_new_session=True,
        )
        deadline = time.monotonic() + START_TIMEOUT
        while not self.socket.exists():
            if self.backend.poll() is not None:
                self._startup_failed = True
                raise CodexDeliveryError(f'Codex backend exited with status {self.backend.returncode}; log: {self.directory}')
            if time.monotonic() >= deadline:
                self._startup_failed = True
                raise CodexDeliveryError(f'Codex backend startup timed out; log: {self.directory}')
            time.sleep(.05)
        os.chmod(self.socket, 0o600)
        return [self.command[0], '--remote', 'unix://' + str(self.socket),
                '-c', 'check_for_update_on_startup=false', *self.command[1:]]

    def _disconnect(self):
        if self._ws is not None:
            with contextlib.suppress(Exception):
                self._ws.close()
            self._ws = None

    def _connect(self):
        if self._ws is not None:
            return
        from websockets.sync.client import unix_connect

        self._ws = unix_connect(
            str(self.socket), uri='ws://localhost/rpc', compression=None,
            open_timeout=self.rpc_timeout, close_timeout=1, max_size=32 * 1024 * 1024,
        )
        self._request('initialize', {
            'clientInfo': {'name': 'lmer_delivery', 'version': '1'},
            'capabilities': {'experimentalApi': True},
        })
        self._ws.send(json.dumps({'method': 'initialized'}))

    def _request(self, method, params):
        self._sequence += 1
        ident = self._sequence
        started = time.monotonic()
        self._ws.send(json.dumps({'id': ident, 'method': method, 'params': params}))
        deadline = started + self.rpc_timeout
        timed_out = False
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    raise TimeoutError(f'Codex {method} response deadline ({self.rpc_timeout:g}s)')
                try:
                    reply = json.loads(self._ws.recv(timeout=remaining))
                except TimeoutError:
                    timed_out = True
                    raise
                if reply.get('id') != ident or not ('result' in reply or 'error' in reply):
                    continue  # Approval requests belong to the TUI, never this adapter.
                if 'error' in reply:
                    error = reply['error'] if isinstance(reply['error'], dict) else {}
                    raise CodexDeliveryError(f'Codex rejected {method}: {error.get("message", "unknown error")}',
                                             rpc_code=error.get('code'))
                return reply['result']
        finally:
            self._latency.append((time.monotonic() - started, timed_out))

    def _latency_stats(self):
        window = list(self._latency)
        samples = sorted(seconds for seconds, _ in window)
        timeouts = sum(1 for _, timed_out in window if timed_out)
        if not samples:
            return {'count': 0, 'max': None, 'p99': None, 'timeouts': 0}
        p99 = samples[min(len(samples) - 1, math.ceil(.99 * len(samples)) - 1)]
        return {'count': len(samples), 'max': round(samples[-1], 3), 'p99': round(p99, 3), 'timeouts': timeouts}

    def _thread(self):
        deadline = time.monotonic() + self.rpc_timeout
        loaded = self._request('thread/loaded/list', {})['data']
        if len(loaded) > 32:
            raise CodexDeliveryError('Too many loaded Codex threads to establish delivery ownership')
        primary = []
        for ident in loaded:
            if self._stop.is_set() or time.monotonic() >= deadline:
                raise CodexDeliveryError('Codex thread discovery interrupted or timed out')
            thread = self._request('thread/read', {'threadId': ident, 'includeTurns': False})['thread']
            if (not thread.get('ephemeral') and thread.get('parentThreadId') is None
                    and thread.get('threadSource') == 'user'):
                primary.append(thread)
        if len(primary) != 1:
            raise CodexDeliveryError(f'Codex delivery needs one loaded primary thread; found {len(primary)}')
        thread = primary[0]
        if self._thread_id is not None and thread['id'] != self._thread_id:
            raise CodexDeliveryError('Codex primary thread changed; input retained for review')
        self._thread_id = thread['id']
        self._runtime = {'thread_id': thread['id'], 'model': thread.get('model'),
                         'effort': thread.get('reasoningEffort'), 'observed_at': time.time()}
        model, effort = os.environ.get('LMER_LLM_NAME'), os.environ.get('LMER_REASONING_EFFORT')
        effort = 'xhigh' if effort == 'max' else effort
        mismatch = (bool(model) and model != self._runtime['model']) or (
            effort not in (None, '', 'auto') and effort != self._runtime['effort'])
        self._runtime['preset_mismatch'] = mismatch
        if mismatch:
            self._warn(f'Codex runtime differs from launch preset: {self._runtime["model"]}/{self._runtime["effort"]}; launch {model}/{effort}.')
        return thread

    def _persist(self):
        temporary = self.outbox.with_suffix('.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(self._receipts, stream, ensure_ascii=False)
        temporary.replace(self.outbox)

    @staticmethod
    def _public(receipt):
        return {key: value for key, value in receipt.items() if key != 'text'}

    def deliver(self, text, *, client_id=None, adapter_converges=False):
        """Submit once; a missing response never authorizes retransmission.

        With *adapter_converges* (bus records, startup: nobody else retries or
        reports them) the adapter resubmits a provably unsent input itself and
        escalates one that cannot converge. See docs/CODEX-DELIVERY.md.
        """
        ident = client_id or str(uuid.uuid4())
        if not isinstance(ident, str) or re.fullmatch(r'[A-Za-z0-9_./:-]{1,200}', ident) is None:
            raise CodexDeliveryError('Invalid Codex delivery ID')
        digest = hashlib.sha256(text.encode()).hexdigest()
        if adapter_converges:
            self._lock.acquire()
        elif not self._lock.acquire(timeout=LOCK_WAIT_RPCS * self.rpc_timeout):
            raise CodexDeliveryError(f'Codex delivery {ident} was not sent: the adapter is busy observing; '
                                     'retry with the same ID')
        try:
            previous = self._receipts.get(ident)
            if previous is not None:
                if previous['payload_sha256'] != digest:
                    raise CodexDeliveryError('Codex delivery ID was reused with different text')
                if not previous.get('retryable'):
                    return self._public(previous)
            if self._closed or (previous is None and len(self._receipts) >= MAX_RECEIPTS):
                raise CodexDeliveryError('Codex delivery is closed or its receipt capacity is exhausted')
            receipt = {
                'delivery_id': ident, 'payload_sha256': digest, 'payload_length': len(text.encode()),
                'bytes_written': 0, 'transport': 'codex-app-server', 'state': 'pending',
                'submit_confirmed': False, 'retryable': False, 'text': text,
                'adapter_converges': bool(adapter_converges or (previous or {}).get('adapter_converges')),
                **{key: previous[key] for key in ('escalated', 'escalation') if key in (previous or {})},
            }
            self._receipts[ident] = receipt
            try:
                self._persist()
            except OSError as exc:
                if previous is None:
                    self._receipts.pop(ident)
                else:
                    self._receipts[ident] = previous
                self._warn(f'Codex input {ident} was not sent: cannot persist its original text ({exc}).')
                raise CodexDeliveryError('Cannot persist original Codex input; native submission refused') from exc
            self._since[ident] = time.monotonic()
            self._idle_since.pop(ident, None)
            self._submit(receipt)
            return self._public(receipt)
        finally:
            self._lock.release()

    def _submit(self, receipt):
        """One native submission attempt; the caller holds the lock."""
        ident, text = receipt['delivery_id'], receipt['text']
        submitted = False
        try:
            self._connect()
            thread = self._thread()
            receipt.update(thread_id=thread['id'], model=thread.get('model'),
                           effort=thread.get('reasoningEffort'))
            submitted = True
            receipt['submitted_at'] = time.time()
            response = self._request('turn/start', {
                'threadId': thread['id'], 'clientUserMessageId': ident,
                'input': [{'type': 'text', 'text': text, 'text_elements': []}],
            })
            receipt.update(state='accepted', turn_id=response['turn']['id'], retryable=False,
                           note='Codex accepted this input; model-context confirmation is pending.')
            receipt.pop('error', None)
        except Exception as exc:
            if not submitted:
                retrying = receipt.get('adapter_converges')
                receipt.update(state='retrying' if retrying else 'rejected', error=str(exc), retryable=True,
                               note=('Native submission was not attempted; the adapter retries it with the same ID and text.'
                                     if retrying else 'Native submission was not attempted; the same ID and text may be retried.'))
            else:
                receipt.update(state='rejected' if isinstance(exc, CodexDeliveryError) else 'uncertain',
                               error=str(exc), retryable=False,
                               note='Original input retained; no automatic replay or terminal fallback.')
            state = receipt['state']
            self._disconnect()
            self._retry_at[ident] = time.monotonic() + RETRY_INTERVAL
            self._warn(f'Codex delivery {ident} {state}: {exc}. Original input retained at {self.outbox}.')
        try:
            self._persist()
        except OSError as exc:
            # The attempt already happened; raising would report it refused. The
            # outbox keeps the earlier copy until the next write.
            self._warn(f'Codex delivery {ident} {receipt["state"]}, but its outbox record was not updated ({exc}).')

    def _retry_unsent(self):
        """Resubmit inputs ``turn/start`` provably never carried (``retryable``)."""
        now = time.monotonic()
        for receipt in list(self._receipts.values()):
            ident = receipt['delivery_id']
            if (receipt['state'] == 'retrying' and not receipt.get('escalated')
                    and now >= self._retry_at.get(ident, 0)
                    and now < self._since.get(ident, now) + CONFIRM_DEADLINE):
                self._submit(receipt)
                if receipt.get('retryable'):
                    break  # The backend is still unreachable; the rest wait for the next tick.

    def _recent_turns(self, thread_id, watched):
        """Turns new enough to hold every *watched* input, newest first.

        With nothing watched, one page: an escalated input Codex takes late
        lands in a recent turn.
        """
        if self._turns_list is False:
            return self._request('thread/read', {'threadId': thread_id, 'includeTurns': True})['thread']['turns']
        submitted = [r['submitted_at'] for r in watched if r.get('submitted_at')]
        oldest = min(submitted, default=time.time()) - TURN_START_SLACK
        missing = {r['delivery_id'] for r in watched}
        turns, cursor = [], None
        for _ in range(MAX_TURN_PAGES if watched else 1):
            params = {'threadId': thread_id, 'limit': TURN_PAGE, 'sortDirection': 'desc', 'itemsView': 'full'}
            if cursor:
                params['cursor'] = cursor
            try:
                page = self._request('thread/turns/list', params)
            except CodexDeliveryError as exc:
                if self._turns_list or exc.rpc_code not in UNSUPPORTED_METHOD:
                    raise
                self._turns_list = False
                return self._recent_turns(thread_id, watched)
            self._turns_list = True
            data = page.get('data') or []
            turns.extend(data)
            missing -= {item.get('clientId') for turn in data for item in turn.get('items') or []}
            cursor = page.get('nextCursor')
            if (not cursor or not missing or self._stop.is_set()
                    or any((turn.get('startedAt') or oldest) < oldest for turn in data)):
                break
        return turns

    def refresh(self):
        with self._lock:
            if self._closed:
                return
            self._retry_unsent()
            pending = [r for r in self._receipts.values() if r['state'] in ('accepted', 'uncertain')]
            watched = [r for r in pending if not r.get('escalated')]
            if watched or time.monotonic() >= self._next_runtime_read:
                self._observe(pending)
            self._escalate_due()

    def _observe(self, pending):
        try:
            self._connect()
            thread = self._thread()
            self._usage.refresh(thread, self._runtime, self._request)
            if pending:
                watched = [r for r in pending if not r.get('escalated')]
                running = (thread.get('status') or {}).get('type') == 'active'
                now = time.monotonic()
                for receipt in watched:
                    if running:
                        self._idle_since.pop(receipt['delivery_id'], None)
                    else:
                        self._idle_since.setdefault(receipt['delivery_id'], now)
                self._match(pending, self._recent_turns(thread['id'], watched))
            self._observation_failures = 0
            if self._observation_down is not None:
                self._warn(f'Codex delivery observation recovered after '
                           f'{time.monotonic() - self._observation_down:.0f}s.', once=False)
                self._observation_down = None
        except Exception as exc:
            self._disconnect()
            if self._runtime is not None:
                self._runtime['observation_error'] = str(exc)
            self._observation_failures += 1
            if self._observation_down is None and self._observation_failures >= OUTAGE_FAILURES:
                self._observation_down = time.monotonic()
                waiting = ', '.join(r['delivery_id'] for r in pending) or 'none'
                self._warn(f'Codex runtime or delivery observation unavailable: {exc}. '
                           f'Pending input ({waiting}) is retained without replay.', once=False)
        finally:
            self._next_runtime_read = time.monotonic() + RUNTIME_INTERVAL

    def _match(self, pending, turns):
        items = {item.get('clientId'): item for turn in turns for item in turn.get('items') or []
                 if item.get('type') == 'userMessage' and item.get('clientId')}
        by_id = {turn['id']: turn for turn in turns}
        for receipt in pending:
            item = items.get(receipt['delivery_id'])
            if item is not None:
                expected = [{'type': 'text', 'text': receipt['text'], 'text_elements': []}]
                if item.get('content') == expected:
                    receipt.update(state='confirmed', submit_confirmed=True,
                                   note='Original input confirmed in the native thread context.')
                    receipt.pop('text', None)
                    self._resolved(receipt)
                else:
                    receipt.update(state='uncertain', error='Native input content differs from original',
                                   undecidable=True)
                    self._warn(f'Codex delivery {receipt["delivery_id"]} content mismatch; original retained at {self.outbox}.')
            elif receipt['state'] == 'accepted' and by_id.get(receipt.get('turn_id'), {}).get('status') in ENDED_TURN:
                # Not a verdict: Codex carries a steer that raced its turn's end
                # into a follow-up turn.
                receipt.update(state='uncertain', note='Turn ended before input confirmation; still observed, never replayed.')
        self._persist()

    def _escalate_due(self):
        """Hand each input that cannot converge to a human, once, never resending it."""
        now = time.monotonic()
        for receipt in list(self._receipts.values()):
            state, ident = receipt['state'], receipt['delivery_id']
            if state == 'confirmed' or receipt.get('escalated') or (
                    state == 'rejected' and not receipt.get('adapter_converges')):
                continue  # Its caller was handed the rejection.
            since = self._since.get(ident, now)
            if state == 'rejected':
                reason = 'Codex rejected it'
            elif receipt.get('undecidable'):
                reason = 'Codex holds different content under its ID'
            elif state == 'retrying' and now >= since + CONFIRM_DEADLINE:
                reason = f'Codex could not be reached to submit it for {CONFIRM_DEADLINE:.0f}s'
            elif now >= self._idle_since.get(ident, math.inf) + CONFIRM_DEADLINE:
                reason = f'Codex has been idle for {CONFIRM_DEADLINE:.0f}s without taking it'
            elif state != 'retrying' and now >= since + MAX_CONFIRM_WAIT:
                reason = f'Codex has not confirmed it after {MAX_CONFIRM_WAIT / 60:.0f} minutes'
            else:
                continue
            if now < self._escalate_at.get(ident, 0):
                continue
            if self.escalate is None or not receipt.get('adapter_converges'):
                handle = None
            else:
                try:
                    handle = self.escalate(self._public(receipt), reason, receipt.get('text') or '')
                except Exception as exc:
                    if ident not in self._escalate_at:
                        logger.exception('Could not escalate Codex delivery %s; retrying', ident)
                        self._warn(f'Codex delivery {ident} could not be escalated ({exc}); retrying.')
                    self._escalate_at[ident] = now + RETRY_INTERVAL
                    continue
            receipt['escalated'] = True
            if handle is None:
                self._warn(f'Codex delivery {ident} needs review: {reason}; original retained at {self.outbox}, not replayed.')
            else:
                receipt['escalation'] = handle
            self._persist()

    def _resolved(self, receipt):
        handle = receipt.get('escalation')
        if handle is None or self.deescalate is None:
            return
        try:
            self.deescalate(handle, self._public(receipt))
        except Exception:
            logger.exception('Could not close the escalation for Codex delivery %s', receipt['delivery_id'])

    def snapshot(self):
        # No lock: /healthz must answer while a tick holds it for several RPC
        # deadlines. Each copy is a single C-level call, atomic under the GIL.
        runtime = self._runtime
        receipts = [dict(r) for r in list(self._receipts.values())[-100:]]
        return {'runtime': None if runtime is None else dict(runtime),
                'rpc_timeout': self.rpc_timeout, 'rpc_latency': self._latency_stats(),
                'receipts': [self._public(r) for r in receipts]}

    def start_monitor(self):
        def run():
            while not self._stop.wait(PENDING_INTERVAL):
                try:
                    self.refresh()
                except Exception as exc:
                    logger.exception('Codex receipt monitor tick failed')
                    self._warn(f'Codex receipt monitor tick failed: {exc}.')
        self._monitor = threading.Thread(target=run, name='lmer-codex-receipts', daemon=True)
        self._monitor.start()

    def auto_start(self, options, cancel, on_started):
        def run():
            deadline = time.monotonic() + START_TIMEOUT
            while not cancel.is_set() and time.monotonic() < deadline:
                try:
                    with self._lock:
                        self._connect()
                        self._thread()
                    break
                except Exception:
                    with self._lock:
                        self._disconnect()
                    if cancel.wait(.25):
                        return
            else:
                if cancel.is_set():
                    return
                self._warn(f'Codex startup could not identify its native thread in {START_TIMEOUT:.0f}s; '
                           'startup input is retried and escalated like any other.')
            for name, text in (('startup', options.get('start_command')), ('startup-prompt', options.get('start_prompt'))):
                if cancel.is_set() or self._stop.is_set():
                    return
                if text and not self._startup_accepted(self.deliver(text, client_id=name, adapter_converges=True), cancel):
                    if cancel.is_set() or self._stop.is_set():
                        return
                    self._warn(f'Codex {name} input did not converge; inbound messages are released anyway.')
                    break
            on_started()
        worker = threading.Thread(target=run, name='lmer-codex-startup', daemon=True)
        worker.start()
        return worker

    def _startup_accepted(self, receipt, cancel):
        """Wait out the monitor's retries of a startup input that was never sent."""
        ident = receipt['delivery_id']
        while receipt['state'] == 'retrying' and not receipt.get('escalated'):
            if cancel.wait(PENDING_INTERVAL) or self._stop.is_set():
                return False
            with self._lock:
                receipt = self._public(self._receipts[ident])
        return receipt['state'] in ('accepted', 'confirmed')

    def close(self):
        self._stop.set()
        if self._monitor is not None:
            self._monitor.join(timeout=self.rpc_timeout + 1)
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._disconnect()
            if self.backend is not None and self.backend.poll() is None:
                os.killpg(self.backend.pid, signal.SIGTERM)
                try:
                    self.backend.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(self.backend.pid, signal.SIGKILL)
                    self.backend.wait(timeout=5)
            if self._log is not None:
                self._log.close()
            retained = self._startup_failed or any(r['state'] != 'confirmed' for r in self._receipts.values())
            if retained:
                self._warn(f'Unconfirmed Codex input retained for review at {self.outbox}; it will not be replayed.')
            elif self.directory is not None:
                shutil.rmtree(self.directory)
