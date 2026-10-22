"""Shared lifecycle for the two connector processes."""
from __future__ import annotations

import contextlib
import copy
import logging
import os
import sys
import threading
import time

from ask_channel import bus
from .connector_delivery import DeliveryState, IdentityUnavailable, RecoveryRequired, verified_rejection
from .connector_ownership import Lease, same_process
from .connector_receipts import ReceiptReactions

logger = logging.getLogger(__name__)
MODULE_POLL_SECONDS = 1.0
DRAIN_SECONDS = 45.0
FAILURE_LIMIT = 8
#: What the bus answers about the record itself (its shape, size or sender),
#: never about the moment: posting it again gets the same answer.
REFUSED_RECORD_STATUSES = frozenset({400, 403, 413, 422})
#: How much of a refused post its notice quotes back, enough to say which one.
REFUSED_HEAD_CHARS = 60
EXIT_RETRY = 1
EXIT_DRAIN = 3
EXIT_RECOVERY = 4


class ConnectorLifecycle:
    def _init_lifecycle(self):
        module = sys.modules[self.__class__.__module__]
        self._entrypoint = getattr(getattr(module, "__spec__", None), "name", module.__name__)
        self._lease = None
        self._drain_fd = None
        self._signal_stop = False
        self._state = None
        self._seeded = False
        self._connected = False
        self._questions = {}
        self._pending_inbound = {}
        self._replayed = False
        self._direction_failures = {}
        self._delivery_health = {}
        self._activity_seq = None
        self._receipt_pending = {}
        self._receipts = ReceiptReactions(self)
        self.exit_code = 0
        self.status = "starting"

    def _acquire(self, key):
        if self._lease is None:
            self._lease = Lease.enter(key)
            self._state = DeliveryState(key, self._lease, snapshot=self._snapshot, restore=self._restore)
            self._posters.lock = self._state.lock

    def _snapshot(self):
        fields = ["_bus_cursor", "_answered_freely", "_announced", "_questions",
                  "_pending_inbound", "_replayed", "_receipt_pending", "_delivery_health", *self._transport_fields]
        return {
            **{field: sorted(getattr(self, field)) if isinstance(getattr(self, field), set)
               else copy.deepcopy(getattr(self, field)) for field in fields},
            "posters": self._posters.snapshot(),
        }

    def _restore(self, saved):
        for field, value in saved.items():
            if field == "posters":
                self._posters.restore(value)
            elif field in {"_answered_freely", "_seen", "_accepted_inbound"}:
                setattr(self, field, set(value))
            elif field in self._snapshot():
                setattr(self, field, copy.deepcopy(value))

    def _seed_cursor(self):
        self._prepare()
        health = self.bus.health()
        restored = self._state.bind(health)
        if self._seeded:
            return
        with self._state.lock:
            self._activity_seq = int(health.get("seq") or 0)
            self._posters._session = self._state.session
            launch = health.get("launch")
            self._launch_settings = launch if isinstance(launch, dict) else None
            if not restored:
                acked = (health.get("subscribers") or {}).get(self.subscriber)
                if isinstance(acked, int) and not isinstance(acked, bool):
                    self._bus_cursor = acked
                elif self.config.attach_since is not None:
                    self._bus_cursor = self.config.attach_since
                elif self.config.attach_from_now:
                    self._bus_cursor = int(health.get("seq") or 0)
            self._state.save()
            self._seeded = True

    def _ensure_ready(self):
        self._prepare()
        if not self._seeded:
            self._seed_cursor()

    def start(self):
        with self._threads_lock:
            if self._threads:
                return
            worker = threading.Thread(target=self._run_when_ready,
                                      name="lmer-connector-connect", daemon=True)
            self._threads.append(worker)
            worker.start()

    def _run_when_ready(self):
        try:
            deadline = time.monotonic() + self.config.connect_budget
            self._prepare()
            if not self.bus.wait_ready(budget=self.config.connect_budget, stop=self._stop):
                if not self._stop.is_set():
                    raise RuntimeError("supervisor message bus never came up; this thread will not hear the session")
                return
            while not self._stop.is_set():
                try:
                    self._seed_cursor()
                    break
                except (IdentityUnavailable, bus.BusError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("supervisor session identity remained unavailable")
                    self._stop.wait(0.25)
            if self._stop.is_set():
                return
            pending = self._state.data.get("pending")
            if pending and pending["source"].get("notice"):
                self._notice(pending["source"]["text"], pending["source"]["notice"])
            self._announce_attached()
            with self._threads_lock:
                if self._stop.is_set():
                    return
                for direction in ("outbound", "inbound"):
                    worker = threading.Thread(target=getattr(self, f"_{direction}_loop"),
                                              name=f"lmer-connector-{direction}", daemon=True)
                    self._threads.append(worker)
                    worker.start()
                worker = threading.Thread(target=self._receipts.run,
                                          name="lmer-connector-receipts", daemon=True)
                self._threads.append(worker)
                worker.start()
                self._connected = True
                self.status = "running"
        except Exception as exc:
            self._terminal(exc)

    def _terminal(self, exc):
        recovery = isinstance(exc, RecoveryRequired) or (self._state and self._state.blocked)
        self.exit_code = EXIT_RECOVERY if recovery else EXIT_RETRY
        self.status = "recovery required" if recovery else "retrying"
        logger.exception("connector_stopped status=%s", self.status)
        with contextlib.suppress(Exception):
            self._warn(str(exc))
        self._stop.set()

    def _transition_note(self, text):
        with contextlib.suppress(Exception):
            self.bus.post("note", text, source={"channel": self._lease.key.transport},
                          meta={"notification_class": "connector_health"})
        if self._notify is not None:
            with contextlib.suppress(Exception):
                self._notify(text)

    def _loop(self, direction, step, *, interval=0):
        while not self._stop.is_set():
            try:
                self._ensure_ready()
                step()
            except bus.NoChannel:
                self._stop.set()
                return
            except RecoveryRequired as exc:
                self._terminal(exc)
                return
            except Exception as exc:
                logger.exception("connector_iteration_failed direction=%s", direction)
                count = self._direction_failures.get(direction, 0) + 1
                self._direction_failures[direction] = count
                self.failures = max(self._direction_failures.values())
                self._record_delivery_health(direction, exc)
                if count == 1:
                    self._transition_note(f"{direction} connector delivery is retrying after an error")
                if count >= FAILURE_LIMIT:
                    self._terminal(exc)
                    return
                self._backoff()
                continue
            if self._direction_failures.get(direction) or self._delivery_health.get(direction, {}).get("failures"):
                self._transition_note(f"{direction} connector delivery recovered")
                self._record_delivery_health(direction)
            self._direction_failures[direction] = 0
            self.failures = max(self._direction_failures.values(), default=0)
            self._stop.wait(interval)

    def _record_delivery_health(self, direction, error=None):
        if self._state is None or self._state.session is None:
            return
        try:
            with self._state.lock:
                previous = self._delivery_health.get(direction, {})
                self._delivery_health[direction] = {
                    "failures": previous.get("failures", 0) + 1 if error else 0,
                    "error": type(error).__name__ if error else None,
                    "at": time.time(),
                }
                self._state.save()
        except (OSError, RecoveryRequired, IdentityUnavailable):
            logger.exception("connector_health_checkpoint_failed direction=%s", direction)

    def _backoff(self):
        self._stop.wait(min(30.0, 0.5 * 2 ** min(self.failures, 6)))

    def _deliver_part(self, source, part, payload, send, bookkeeping, *, rejected=verified_rejection):
        self._ensure_ready()
        with self._state.outbound:
            receipt = self._state.send(source, part, payload, send, rejected=rejected)
            with self._state.lock:
                if not self._state.part_done(part):
                    bookkeeping(receipt)
                    self._state.finish_part(part)
            return receipt

    def _outbound_loop(self):
        self._loop("outbound", self._outbound_once)

    def _record_activity(self, record):
        with self._state.lock:
            seq = int(record.get("seq") or 0)
            if self._activity_seq is not None and seq > self._activity_seq:
                self._lease.mark_activity()
                self._activity_seq = seq

    def _outbound_once(self):
        records, cursor = self.bus.messages(self._bus_cursor, timeout=self._bus_poll_seconds)
        pending = self._state.data.get("pending")
        if pending and pending["source"].get("notice"):
            self._notice(pending["source"]["text"], pending["source"]["notice"])
            pending = None
        if pending and not any(record == pending["source"] for record in records):
            raise RecoveryRequired("pending source record is unavailable or changed")
        for record in records:
            if self._stop.is_set():
                return
            if ((record.get("source") or {}).get("channel") in (None, "agent")
                    or (record.get("meta") or {}).get("session_origin") is True):
                self._record_activity(record)
            with self._state.lock:
                if record.get("kind") == "question" and record.get("id"):
                    self._questions[record["id"]] = record
            self._send_record(record)
            self._state.finish(lambda: setattr(self, "_bus_cursor", max(
                self._bus_cursor, int(record.get("seq") or 0),
            )))
        self._state.finish(lambda: setattr(self, "_bus_cursor", max(self._bus_cursor, cursor)))
        with contextlib.suppress(bus.BusError):
            self.bus.ack(self.subscriber, self._bus_cursor)

    def _post_prepared(self, ref, prepare, accepted):
        self._ensure_ready()
        with self._state.lock:
            pending = self._pending_inbound.get(ref)
            if pending is None:
                pending = prepare()
                if pending is None:
                    return None
                self._pending_inbound[ref] = pending
                self._state.save()
        try:
            result = self.bus.post(**pending["request"])
        except bus.NoChannel:
            raise
        except bus.BusError as exc:
            if exc.status not in REFUSED_RECORD_STATUSES:
                raise
            self._drop_refused_inbound(ref, accepted, exc, pending)
            return None
        message = result.get("message") if isinstance(result, dict) else None
        if not isinstance(message, dict) or not message.get("id"):
            raise bus.BusError("supervisor returned no accepted message receipt")
        self._record_activity(message)
        with self._state.lock:
            if pending.get("question"):
                self._answered_freely.add(pending["question"])
            if pending.get("human_receipt"):
                self._receipts.accepted(ref, message)
            accepted()
            self._posters.ack_ref(ref)
            self._pending_inbound.pop(ref, None)
            self._state.save()
        return result

    def _drop_refused_inbound(self, ref, accepted, exc, pending):
        """Retire one record the bus refuses, so it never holds up the next."""
        logger.warning("connector_inbound_refused ref=%s status=%s reason=%s", ref, exc.status, exc)
        with self._state.lock:
            accepted()
            self._posters.ack_ref(ref)
            self._pending_inbound.pop(ref, None)
            self._state.save()
        self._transition_note(f"{self._lease.key.transport} connector dropped an inbound message "
                              f"the bus refused ({exc.status}): {exc}")
        # The note above reaches no thread and no digest; without this the sender
        # sees a post that silently never arrived. Best effort, after the drop is
        # saved: a crash here loses the notice, never the next message.
        words = " ".join(str((pending.get("request") or {}).get("text") or "").split())
        head = words if len(words) <= REFUSED_HEAD_CHARS else words[:REFUSED_HEAD_CHARS - 1] + "…"
        try:
            receipt = self._send_notice(
                f"This message didn't reach the session: {self._inert(repr(head))} ({self._inert(str(exc))})."
            )
            with self._state.lock:
                self._remember_notice(receipt)
                self._state.save()
        except Exception as error:
            logger.warning("connector_refusal_notice_failed ref=%s error=%s", ref, error)

    def _inert(self, text):
        """*text*, quoted from someone else's post, as the transport shows it
        without acting on it: a quoted mention must not ping anyone again."""
        return text

    def _resume_pending_inbound(self):
        with self._state.lock:
            pending = list(self._pending_inbound)
        for ref in pending:
            self._post_prepared(ref, lambda: None, lambda: self._accept_inbound(ref))
        return len(pending)

    def _notice(self, text, kind):
        source = {"notice": kind, "text": text}
        self._deliver_part(source, "notice", {"text": text, "notice": kind},
                           lambda: self._send_notice(text), self._remember_notice)
        self._state.finish(lambda: setattr(self, "_announced", kind == "attached")
                           if kind in ("attached", "detached") else None)

    def _announce_attached(self):
        if self.config.attach_ack and not self._announced and not self._state.data.get("pending"):
            self._notice(self._attachment_notice(), "attached")

    def _announce_detached(self):
        if self._announced and self._state and not self._state.blocked and not self._state.data.get("pending"):
            self._notice(self._detach_note, "detached")

    def stop(self, timeout=DRAIN_SECONDS, *, release=True, announce=True):
        self._stop.set()
        deadline = time.monotonic() + timeout
        with self._threads_lock:
            workers = list(self._threads)
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(timeout=max(0, deadline - time.monotonic()))
        if (time.monotonic() >= deadline or any(
            worker.is_alive() for worker in workers if worker is not threading.current_thread()
        )):
            self.exit_code = EXIT_RECOVERY if self._state and self._state.blocked else EXIT_DRAIN
            return False
        if self._lease and not self._lease.closed and self._drain_fd is None:
            self._drain_fd = os.dup(self._lease.fd)
        errors = []

        def finalize():
            try:
                if announce:
                    self._announce_detached()
                if time.monotonic() >= deadline:
                    return
                if self._state:
                    self._state.close()
                if time.monotonic() >= deadline:
                    return
                if self._bus is not None and self._seeded:
                    with contextlib.suppress(Exception):
                        self._bus.ack(self.subscriber, self._bus_cursor, final=True)
                if time.monotonic() >= deadline:
                    return
                if self._lease and not self._lease.closed:
                    if release:
                        self._lease.close()
                    else:
                        self._lease.attach_child(os.getpid())
                        os.set_inheritable(self._lease.fd, True)
                        os.environ.update(self._lease.environment())
            except Exception as exc:
                errors.append(exc)
                self._terminal(exc)

        worker = threading.Thread(target=finalize, name="lmer-connector-finalize", daemon=True)
        with self._threads_lock:
            self._threads.append(worker)
            worker.start()
        worker.join(timeout=max(0, deadline - time.monotonic()))
        if worker.is_alive() or time.monotonic() >= deadline or errors:
            self.exit_code = EXIT_RECOVERY if self._state and self._state.blocked else EXIT_DRAIN
            return False
        if self._drain_fd is not None:
            os.close(self._drain_fd)
            self._drain_fd = None
        self.status = "recovery required" if self.exit_code == EXIT_RECOVERY else "stopped"
        return True

    def _module_versions(self):
        names = [self.__class__.__module__, __name__, "lmer_cli.connector_delivery",
                 "lmer_cli.connector_ownership", "lmer_cli.thread_posts", "lmer_cli.connector_receipts"]
        versions = []
        for name in names:
            path = getattr(sys.modules.get(name), "__file__", None)
            if path:
                stat = os.stat(path)
                versions.append((path, stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size))
        return versions

    def run_forever(self):
        versions = self._module_versions()
        changed = False
        try:
            self.start()
            while not self._stop.wait(MODULE_POLL_SECONDS):
                if self._signal_stop:
                    break
                if self._lease is not None:
                    owner = self._lease.validate()
                    if not same_process(owner.get("parent_pid", 0), owner.get("parent_start")):
                        self.status = "parent owner exited; draining connector"
                        break
                if self._module_versions() != versions:
                    changed = True
                    break
                with self._threads_lock:
                    dead = self._connected and any(
                        not worker.is_alive() for worker in self._threads
                        if worker.name in ("lmer-connector-inbound", "lmer-connector-outbound")
                    )
                if dead:
                    raise RuntimeError("connector worker disappeared")
        except Exception as exc:
            self._terminal(exc)
        drained = self.stop(timeout=DRAIN_SECONDS, release=not changed, announce=not changed)
        if changed and drained and not self.exit_code and not self._signal_stop and self._lease is not None:
            os.execv(sys.executable, [sys.executable, "-m", self._entrypoint, *sys.argv[1:]])
        return self.exit_code
