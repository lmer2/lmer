"""Connector receipts and durable recovery barriers; no automatic uncertain replay."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import tempfile
import threading
import uuid

from .connector_ownership import Lease, OwnershipError, ThreadKey, read_json, sync_directory, write_json


class IdentityUnavailable(RuntimeError):
    pass


class RecoveryRequired(RuntimeError):
    pass


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def checkpoint_path(key: ThreadKey, session: str):
    return key.directory / ("session-" + digest(session) + ".json")


def marker_path(key: ThreadKey):
    return key.directory / "pending-send.json"


def receipt_path(key: ThreadKey, receipt: str):
    return key.directory / "receipts" / (digest(receipt) + ".json")


def claim_receipt(key: ThreadKey, marker: dict, receipt: str) -> None:
    """Retain operation ownership independently of the bounded echo cache."""
    owner = {**marker, "receipt": receipt}
    path = receipt_path(key, receipt)
    previous = read_json(path)
    if previous is not None and previous != owner:
        raise RecoveryRequired("that receipt already belongs to another delivery operation")
    write_json(path, owner)
    sync_directory(key.directory)


def verified_rejection(exc: Exception) -> bool:
    """Only structured, documented rejections establish non-acceptance."""
    from matrix_chat.client import MatrixError
    from slack_chat.client import SlackError

    if isinstance(exc, MatrixError):
        return getattr(exc, "status", None) in (400, 401, 403, 404, 405, 413, 429)
    if isinstance(exc, SlackError):
        return getattr(exc, "code", None) in {
            "not_authed", "invalid_auth", "account_inactive", "token_revoked",
            "missing_scope", "channel_not_found", "not_in_channel", "is_archived",
            "restricted_action", "restricted_action_read_only_channel",
            "no_text", "msg_too_long", "invalid_arguments", "invalid_arg_name",
            "invalid_blocks", "invalid_blocks_format", "invalid_attachments",
            "ratelimited", "rate_limited", "thread_not_found",
        }
    return False


class DeliveryState:
    def __init__(self, key: ThreadKey, lease: Lease, *, snapshot, restore):
        self.key, self.lease = key, lease
        self.snapshot, self.restore = snapshot, restore
        self.lock = threading.RLock()
        self.outbound = threading.RLock()
        self.session = None
        self.data = None
        self.frozen = False

    @property
    def blocked(self) -> bool:
        return self.frozen or marker_path(self.key).exists()

    def bind(self, health: dict) -> bool:
        """The same validation handles immediate and deferred supervisor identity."""
        session = health.get("session")
        if not isinstance(session, str) or not session:
            raise IdentityUnavailable("waiting for the supervisor's session identity")
        with self.lock:
            self.lease.validate()
            self.lease.bind_session(session)
            if self.session is not None:
                if session != self.session:
                    raise RecoveryRequired("the supervisor session changed while connected")
                return False
            if marker_path(self.key).exists():
                raise RecoveryRequired("pending send requires lmer_cli.connector_recovery")
            saved = read_json(checkpoint_path(self.key, session))
            if saved is not None:
                if (saved.get("version") != 1 or saved.get("session") != session
                        or saved.get("thread") != self.key.parts
                        or not isinstance(saved.get("transport"), dict)
                        or not (set(self.snapshot()) - {"_receipt_pending"}).issubset(saved["transport"])
                        or type(saved["transport"].get("_bus_cursor")) is not int
                        or saved["transport"]["_bus_cursor"] < 0):
                    raise RecoveryRequired("incompatible connector checkpoint; state preserved")
                pending = saved.get("pending")
                if pending is not None:
                    if (not isinstance(pending, dict) or not isinstance(pending.get("source"), dict)
                            or digest(pending["source"]) != pending.get("source_digest")
                            or not isinstance(pending.get("parts"), dict)):
                        raise RecoveryRequired("invalid pending source; checkpoint preserved")
                    for part in pending["parts"].values():
                        if (not isinstance(part, dict) or not isinstance(part.get("id"), str)
                                or not isinstance(part.get("payload"), dict)
                                or digest(part["payload"]) != part.get("identity")
                                or ("receipt" in part and not (
                                    isinstance(part["receipt"], str) and part["receipt"]
                                ))):
                            raise RecoveryRequired("invalid delivery receipt; checkpoint preserved")
                self.restore(copy.deepcopy(saved["transport"]))
            self.session = session
            self.data = saved or {
                "version": 1, "session": session, "thread": self.key.parts,
                "generation": 0, "pending": None, "transport": self.snapshot(),
            }
            return saved is not None

    def save(self) -> None:
        with self.lock:
            if self.frozen:
                raise RecoveryRequired("delivery requires recovery; checkpoint writes are blocked")
            if self.session is None or self.data is None:
                raise IdentityUnavailable("supervisor identity is not validated")
            self.lease.validate()
            self.data["transport"] = copy.deepcopy(self.snapshot())
            self.data["generation"] += 1
            write_json(checkpoint_path(self.key, self.session), self.data)

    def begin(self, source: dict) -> dict:
        with self.lock:
            if self.blocked:
                raise RecoveryRequired("a previous send requires explicit recovery")
            if self.data is None:
                raise IdentityUnavailable("supervisor identity is not validated")
            pending = self.data.get("pending")
            if pending is not None:
                if pending.get("source_digest") != digest(source):
                    raise RecoveryRequired("pending source changed; original context preserved")
                return pending
            pending = {"source": copy.deepcopy(source), "source_digest": digest(source), "parts": {}}
            self.data["pending"] = pending
            self.save()
            return pending

    def send(self, source: dict, part: str, payload: dict, call) -> str:
        """Persist intent before calling, receipt before returning to bookkeeping."""
        with self.outbound:
            with self.lock:
                pending = self.begin(source)
                operation = pending["parts"].get(part)
                identity = digest(payload)
                if operation is not None and operation.get("identity") != identity:
                    raise RecoveryRequired("pending attachment or rendered content changed")
                if operation and operation.get("receipt"):
                    return operation["receipt"]
                if operation and operation.get("status") in ("rejected", "not-sent"):
                    operation.update(id=uuid.uuid4().hex, status="prepared")
                if operation is None:
                    operation = {
                        "id": uuid.uuid4().hex, "identity": identity,
                        "payload": copy.deepcopy(payload), "status": "prepared",
                    }
                    pending["parts"][part] = operation
                # Consume a retry authorization before its new send boundary.
                operation["status"] = "prepared"
                self.save()
                marker = {
                    "version": 1, "thread": self.key.parts, "session": self.session,
                    "operation": operation["id"], "part": part,
                    "source_digest": pending["source_digest"], "identity": identity,
                }
                path = marker_path(self.key)
                published = False
                temporary = None
                try:
                    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
                    with os.fdopen(fd, "w") as stream:
                        json.dump(marker, stream, sort_keys=True)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.link(temporary, path)
                    published = True
                    sync_directory(path.parent)
                except Exception:
                    if published or not path.exists():
                        try:
                            operation["status"] = "not-sent"
                            self.save()
                            if published:
                                self.clear_marker(operation["id"])
                        except Exception:
                            self.frozen = True
                    raise
                finally:
                    if temporary is not None:
                        with contextlib.suppress(OSError):
                            os.unlink(temporary)
            try:
                receipt = call()
                if not isinstance(receipt, str) or not receipt:
                    raise ValueError("transport returned no receipt")
            except Exception as exc:
                with self.lock:
                    if verified_rejection(exc):
                        try:
                            operation["status"] = "rejected"
                            self.save()
                            self.clear_marker(operation["id"])
                        except Exception as persistence:
                            self.frozen = True
                            raise RecoveryRequired("could not persist the verified rejection") from persistence
                        raise
                    self.frozen = True
                raise RecoveryRequired("remote outcome is uncertain; inspect and resolve the pending operation") from exc
            with self.lock:
                operation.update(receipt=receipt, status="confirmed")
                try:
                    claim_receipt(self.key, marker, receipt)
                    self.save()
                    self.clear_marker(operation["id"])
                except Exception as exc:
                    self.frozen = True
                    raise RecoveryRequired("confirmed send receipt could not be persisted; recovery required") from exc
            return receipt

    def clear_marker(self, operation_id: str) -> None:
        marker = read_json(marker_path(self.key))
        if marker is None or marker.get("operation") != operation_id:
            raise RecoveryRequired("pending operation changed during recovery")
        marker_path(self.key).unlink()
        sync_directory(self.key.directory)

    def part_done(self, part: str) -> bool:
        with self.lock:
            pending = self.data.get("pending") if self.data else None
            return bool(pending and pending["parts"].get(part, {}).get("bookkept"))

    def finish_part(self, part: str) -> None:
        with self.lock:
            self.data["pending"]["parts"][part]["bookkept"] = True
            self.save()

    def finish(self, advance) -> None:
        """Commit cursor advancement and pending removal in the same snapshot."""
        with self.lock:
            if self.blocked:
                raise RecoveryRequired("pending send prevents cursor advancement")
            advance()
            self.data["pending"] = None
            self.save()

    def close(self) -> None:
        if self.data is not None and not self.frozen:
            with contextlib.suppress(OSError, OwnershipError, RecoveryRequired):
                self.save()
