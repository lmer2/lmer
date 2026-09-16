"""Exclusive connector ownership and child handoff. See docs/CONNECTOR-RECOVERY.md."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import select
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


class OwnershipError(RuntimeError):
    pass


class OwnershipBusy(OwnershipError):
    pass


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path: Path, value: dict) -> None:
    """Atomically replace a file and persist its directory entry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise OwnershipError(f"invalid connector state: {path}")
    return value


def process_identity(pid: int) -> str | None:
    """Linux boot and process start identity, including zombie detection."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return f"{boot}:{fields[19]}"
    except FileNotFoundError:
        return None
    except (OSError, IndexError) as exc:
        raise OwnershipError(f"cannot validate connector process {pid}") from exc


def same_process(pid: int, identity: str | None) -> bool:
    return bool(identity and process_identity(pid) == identity)


@dataclass(frozen=True)
class ThreadKey:
    transport: str
    conversation: str
    root: str

    def __post_init__(self):
        if self.transport not in ("slack", "matrix") or not self.conversation or not self.root:
            raise OwnershipError("a resolved Slack or Matrix thread is required")

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.parts).encode()).hexdigest()

    @property
    def parts(self) -> list[str]:
        return [self.transport, self.conversation, self.root]

    @property
    def directory(self) -> Path:
        from slack_chat.registry import _registry_dir

        inherited = os.environ.get(LEASE_DIRECTORY_ENV)
        if inherited and os.environ.get(LEASE_FD_ENV):
            return Path(inherited)
        return _registry_dir(self.transport) / "connectors" / self.digest

    @property
    def lock_path(self) -> Path:
        return self.directory / "lease.lock"

    @property
    def owner_path(self) -> Path:
        return self.directory / "owner.json"

    @contextlib.contextmanager
    def metadata_lock(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.directory / "metadata.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def busy(self) -> bool:
        self.directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
        finally:
            os.close(fd)

    def holder(self) -> dict | None:
        if not self.directory.exists():
            return None
        with self.metadata_lock():
            if not self.busy():
                return None
            return read_json(self.owner_path) or {"owner": "unknown", "thread": self.parts}


LEASE_FD_ENV = "LMER_CONNECTOR_LEASE_FD"
LEASE_TOKEN_ENV = "LMER_CONNECTOR_LEASE_TOKEN"
READY_FD_ENV = "LMER_CONNECTOR_READY_FD"
LEASE_DIRECTORY_ENV = "LMER_CONNECTOR_LEASE_DIRECTORY"


class Lease:
    def __init__(self, key: ThreadKey, fd: int, token: str):
        self.key, self.fd, self.token = key, fd, token
        self.closed = False

    @classmethod
    def acquire(cls, key: ThreadKey, *, owner="standalone", session=None, permalink=None):
        key.directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(key.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise OwnershipBusy("another connector owns this thread") from exc
            with key.metadata_lock():
                from slack_chat import registry

                legacy = read_json(registry._entry_path(key.conversation, key.root, key.transport))
                if legacy and registry._pid_alive(int(legacy.get("pid") or 0)):
                    raise OwnershipBusy("a legacy connector still owns this thread")
                token = uuid.uuid4().hex
                write_json(key.owner_path, {
                    "version": 1, "thread": key.parts, "token": token,
                    "owner": owner, "session": session, "permalink": permalink,
                    "pid": os.getpid(), "parent_pid": os.getpid(),
                    "parent_start": process_identity(os.getpid()),
                    "child_pid": None, "child_start": None,
                })
            return cls(key, fd, token)
        except BaseException:
            os.close(fd)
            raise

    @classmethod
    def enter(cls, key: ThreadKey, *, environ=None):
        env = os.environ if environ is None else environ
        raw = env.get(LEASE_FD_ENV)
        if raw is None:
            if env.get(LEASE_TOKEN_ENV) or env.get(READY_FD_ENV):
                raise OwnershipError("incomplete connector handoff")
            return cls.acquire(key)
        fd = int(raw)
        ready = env.pop(READY_FD_ENV, None)
        if ready is not None:
            ready_fd = int(ready)
            try:
                readable, _, _ = select.select([ready_fd], [], [], 10.0)
                if not readable or os.read(ready_fd, 1) != b"1":
                    raise OwnershipError("connector handoff was not completed")
            finally:
                os.close(ready_fd)
        lease = cls(key, fd, env.get(LEASE_TOKEN_ENV, ""))
        lease.validate(child=True)
        return lease

    def validate(self, *, child=False) -> dict:
        if self.closed:
            raise OwnershipError("connector lease is closed")
        held, expected = os.fstat(self.fd), self.key.lock_path.stat()
        if (held.st_dev, held.st_ino) != (expected.st_dev, expected.st_ino):
            raise OwnershipError("connector inherited the wrong lease")
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise OwnershipError("connector descriptor does not own the lease") from exc
        with self.key.metadata_lock():
            entry = read_json(self.key.owner_path) or {}
            if entry.get("token") != self.token or entry.get("thread") != self.key.parts:
                raise OwnershipError("connector owner identity changed")
            if child and (entry.get("child_pid") != os.getpid() or not same_process(
                os.getpid(), entry.get("child_start"),
            )):
                raise OwnershipError("connector child identity does not match handoff")
            return entry

    def attach_child(self, pid: int) -> None:
        identity = process_identity(pid)
        if not identity:
            raise OwnershipError("connector child exited before handoff")
        with self.key.metadata_lock():
            entry = read_json(self.key.owner_path) or {}
            if entry.get("token") != self.token:
                raise OwnershipError("connector reservation changed before handoff")
            entry.update(pid=pid, child_pid=pid, child_start=identity)
            write_json(self.key.owner_path, entry)

    def bind_session(self, session: str) -> None:
        with self.key.metadata_lock():
            entry = read_json(self.key.owner_path) or {}
            if entry.get("token") != self.token:
                raise OwnershipError("connector owner changed before supervisor validation")
            if entry.get("session") not in (None, session):
                raise OwnershipError("supervisor identity differs from the owner's session")
            if entry.get("session") is None:
                entry["session"] = session
                write_json(self.key.owner_path, entry)

    def mark_activity(self) -> None:
        """Publish new bus activity for the listener that owns this process."""
        with self.key.metadata_lock():
            entry = read_json(self.key.owner_path) or {}
            if entry.get("token") != self.token:
                raise OwnershipError("connector owner changed before activity update")
            entry["activity_at"] = time.monotonic()
            write_json(self.key.owner_path, entry)

    def environment(self, ready_fd: int | None = None) -> dict[str, str]:
        result = {LEASE_FD_ENV: str(self.fd), LEASE_TOKEN_ENV: self.token,
                  LEASE_DIRECTORY_ENV: str(self.key.directory)}
        if ready_fd is not None:
            result[READY_FD_ENV] = str(ready_fd)
        return result

    def close(self, *, release=True) -> None:
        if self.closed:
            return
        try:
            if release:
                with self.key.metadata_lock():
                    entry = read_json(self.key.owner_path) or {}
                    child = entry.get("child_pid")
                    live_child = child and child != os.getpid() and same_process(
                        child, entry.get("child_start"),
                    )
                    if entry.get("token") == self.token and not live_child:
                        self.key.owner_path.unlink()
                        sync_directory(self.key.directory)
        finally:
            # Explicit LOCK_UN would release the inherited child's lock too.
            os.close(self.fd)
            self.closed = True
