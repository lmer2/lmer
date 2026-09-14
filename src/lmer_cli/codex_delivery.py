"""Codex's private app-server lifecycle and structured input delivery.

See docs/CODEX-DELIVERY.md for receipts, recovery, and protocol limitations.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
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
RPC_TIMEOUT = 3.0
RUNTIME_INTERVAL = 5.0
MAX_RECEIPTS = 10000


class CodexDeliveryError(OSError):
    pass


class CodexDelivery:
    def __init__(self, command, *, notify=None):
        self.command = list(command)
        self.notify = notify or logger.warning
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

    def _warn(self, text):
        if text not in self._warnings:
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
            open_timeout=RPC_TIMEOUT, close_timeout=1, max_size=32 * 1024 * 1024,
        )
        self._request('initialize', {
            'clientInfo': {'name': 'lmer_delivery', 'version': '1'},
            'capabilities': {'experimentalApi': True},
        })
        self._ws.send(json.dumps({'method': 'initialized'}))

    def _request(self, method, params):
        self._sequence += 1
        ident = self._sequence
        self._ws.send(json.dumps({'id': ident, 'method': method, 'params': params}))
        deadline = time.monotonic() + RPC_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f'Codex {method} response deadline')
            reply = json.loads(self._ws.recv(timeout=remaining))
            if reply.get('id') != ident or not ('result' in reply or 'error' in reply):
                continue  # Approval requests belong to the TUI, never this adapter.
            if 'error' in reply:
                raise CodexDeliveryError(f'Codex rejected {method}: {reply["error"].get("message", "unknown error")}')
            return reply['result']

    def _thread(self, *, include_turns=False):
        deadline = time.monotonic() + RPC_TIMEOUT
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
        if include_turns:
            thread = self._request('thread/read', {
                'threadId': thread['id'], 'includeTurns': True,
            })['thread']
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

    def deliver(self, text, *, client_id=None):
        """Submit once; a missing response never authorizes retransmission."""
        ident = client_id or str(uuid.uuid4())
        if not isinstance(ident, str) or re.fullmatch(r'[A-Za-z0-9_./:-]{1,200}', ident) is None:
            raise CodexDeliveryError('Invalid Codex delivery ID')
        digest = hashlib.sha256(text.encode()).hexdigest()
        with self._lock:
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
            submitted = False
            try:
                self._connect()
                thread = self._thread()
                receipt.update(thread_id=thread['id'], model=thread.get('model'),
                               effort=thread.get('reasoningEffort'))
                submitted = True
                response = self._request('turn/start', {
                    'threadId': thread['id'], 'clientUserMessageId': ident,
                    'input': [{'type': 'text', 'text': text, 'text_elements': []}],
                })
                receipt.update(state='accepted', turn_id=response['turn']['id'],
                               note='Codex accepted this input; model-context confirmation is pending.')
            except Exception as exc:
                state = 'rejected' if isinstance(exc, CodexDeliveryError) or not submitted else 'uncertain'
                receipt.update(state=state, error=str(exc), retryable=not submitted,
                               note=('Native submission was not attempted; the same ID and text may be retried.'
                                     if not submitted else 'Original input retained; no automatic replay or terminal fallback.'))
                self._disconnect()
                self._warn(f'Codex delivery {ident} {state}: {exc}. Original input retained at {self.outbox}.')
            self._persist()
            return self._public(receipt)

    def refresh(self):
        with self._lock:
            pending = [r for r in self._receipts.values() if r['state'] in ('accepted', 'uncertain')]
            if self._closed or (not pending and time.monotonic() < self._next_runtime_read):
                return
            try:
                self._connect()
                thread = self._thread(include_turns=bool(pending))
                self._usage.refresh(thread, self._runtime, self._request)
                if not pending:
                    return
                items = {item.get('clientId'): item for turn in thread['turns'] for item in turn['items']
                         if item.get('type') == 'userMessage' and item.get('clientId')}
                turns = {turn['id']: turn for turn in thread['turns']}
                for receipt in pending:
                    item = items.get(receipt['delivery_id'])
                    if item is not None:
                        expected = [{'type': 'text', 'text': receipt['text'], 'text_elements': []}]
                        if item.get('content') == expected:
                            receipt.update(state='confirmed', submit_confirmed=True,
                                           note='Original input confirmed in the native thread context.')
                            receipt.pop('text', None)
                        else:
                            receipt.update(state='uncertain', error='Native input content differs from original')
                            self._warn(f'Codex delivery {receipt["delivery_id"]} content mismatch; original retained at {self.outbox}.')
                    elif turns.get(receipt.get('turn_id'), {}).get('status') in ('completed', 'interrupted', 'failed'):
                        receipt.update(state='uncertain', note='Turn ended before input confirmation; original retained, not replayed.')
                        self._warn(f'Codex delivery {receipt["delivery_id"]} remains unconfirmed after turn end; original retained at {self.outbox}.')
                self._persist()
            except Exception as exc:
                self._disconnect()
                if self._runtime is not None:
                    self._runtime['observation_error'] = str(exc)
                self._warn(f'Codex runtime or delivery observation unavailable: {exc}. Pending input is retained without replay.')
            finally:
                self._next_runtime_read = time.monotonic() + RUNTIME_INTERVAL

    def snapshot(self):
        with self._lock:
            return {'runtime': self._runtime, 'receipts': [self._public(r) for r in list(self._receipts.values())[-100:]]}

    def start_monitor(self):
        def run():
            while not self._stop.wait(.5):
                self.refresh()
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
                self._warn('Codex startup could not identify its native thread; automated input remains held.')
                return
            for name, text in (('startup', options.get('start_command')), ('startup-prompt', options.get('start_prompt'))):
                if cancel.is_set() or self._stop.is_set():
                    return
                if text and self.deliver(text, client_id=name)['state'] != 'accepted':
                    return
            on_started()
        worker = threading.Thread(target=run, name='lmer-codex-startup', daemon=True)
        worker.start()
        return worker

    def close(self):
        self._stop.set()
        if self._monitor is not None:
            self._monitor.join(timeout=RPC_TIMEOUT + 1)
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
