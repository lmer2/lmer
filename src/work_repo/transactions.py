"""Checkout-wide serialization and durable, precisely scoped pending commits.

Lock order is checkout → memory retirement. The lock spans Git sync, index,
HEAD, push and rollback, and is reentrant in one thread. A Git hook inherits
the owner token: its durability command parks work instead of waiting for the
Git process that is itself waiting for the hook.
"""

from contextlib import contextmanager
import fcntl
from functools import wraps
import json
import os
from pathlib import Path
import sys
import threading
import uuid

from .utils import MANIFEST_NAME

OWNER_ENV = "LMER_WORK_REPO_TRANSACTION_OWNERS"
_guard = threading.RLock()
_locks = {}
_local = threading.local()


def checkout_metadata_dir(root: Path) -> Path:
    """Locate checkout-local bookkeeping outside all commit path sets."""
    root = Path(root).resolve()
    marker = root / ".git"
    if marker.is_dir():
        return marker
    if marker.is_file():
        text = marker.read_text().strip()
        if text.startswith("gitdir: "):
            return (root / text[8:]).resolve()
    # Non-Git fixtures and not-yet-initialized checkouts: never put machinery
    # in a path an ordinary work commit could stage.
    return root.parent / f".{root.name}.lmer-transactions"


def _owners():
    try:
        value = json.loads(os.environ.get(OWNER_ENV, "{}"))
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def inherited_transaction(root: Path) -> bool:
    """Whether an ancestor currently owns this checkout's transaction lock."""
    path = checkout_metadata_dir(root) / "lmer-checkout.lock"
    owner = _owners().get(str(path))
    if not isinstance(owner, int) or owner == os.getpid() or not path.exists():
        return False
    # An inherited token may outlive its transaction. Consult the actual lock.
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
    return False


@contextmanager
def checkout_lock(root: Path):
    """Serialize one checkout, with thread reentrancy and hook refusal."""
    path = checkout_metadata_dir(root) / "lmer-checkout.lock"
    key = (os.getpid(), str(path))
    if inherited_transaction(root):
        raise OSError("work-repo transaction is held by the invoking process; retry after its Git hook finishes")
    with _guard:
        lock = _locks.setdefault(key, threading.RLock())
    with lock:
        held = getattr(_local, "held", set())
        if key in held:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            _local.held = held | {key}
            with _guard:
                owners = _owners()
                owners[str(path)] = os.getpid()
                os.environ[OWNER_ENV] = json.dumps(owners)
            try:
                yield
            finally:
                with _guard:
                    owners = _owners()
                    owners.pop(str(path), None)
                    if owners:
                        os.environ[OWNER_ENV] = json.dumps(owners)
                    else:
                        os.environ.pop(OWNER_ENV, None)
                _local.held = held
                fcntl.flock(handle, fcntl.LOCK_UN)


def configured_checkout() -> Path:
    """The work-repo checkout this process was launched against."""
    return Path(os.environ.get("LMER_WORK_REPO_PATH", "/work"))


def checkout_of(tree: Path, root: Path | None = None) -> Path | None:
    """The checkout whose lock a write to ``tree`` must take, if any.

    A path inside the configured checkout takes that checkout's lock, the one
    every other writer of it shares. A path elsewhere is a different tree: it
    is serialized on its own checkout when one encloses it (a manifest or a
    ``.git`` marks one), and on nothing when none does. Taking the configured
    checkout's lock for a write that never touches it would block unrelated
    work there without protecting anything.
    """
    root = (root or configured_checkout()).resolve()
    tree = Path(tree).resolve()
    if tree == root or root in tree.parents:
        return root
    for candidate in (tree, *tree.parents):
        if (candidate / MANIFEST_NAME).is_file() or (candidate / ".git").exists():
            return candidate
    return None


@contextmanager
def work_mutation(tree: Path | None = None):
    """Protect an entire supported mutation, before lookup or the first write.

    This boundary also covers direct library callers. It raises on inherited
    hook ownership: a hook must never mutate the live checkout while its
    invoking Git process can still clean or reset that checkout. ``tree`` is
    the path being written when the caller names one; the lock is keyed on
    the checkout that path belongs to (:func:`checkout_of`), not on the
    process environment alone.
    """
    checkout = configured_checkout() if tree is None else checkout_of(tree)
    if checkout is None or not checkout.exists():
        yield
        return
    with checkout_lock(checkout):
        yield


def mutation_transaction(function):
    """Serialize a library mutation without changing its return/error types.

    A writer whose first positional argument is a path (``write_state(rdir,
    …)``, ``record_version(rdir, …)``) is locked on that path's checkout.
    """
    @wraps(function)
    def locked(*args, **kwargs):
        tree = args[0] if args and isinstance(args[0], Path) else None
        with work_mutation(tree):
            return function(*args, **kwargs)
    return locked


def work_transaction(function=None, *, failure_result=1):
    """Guard a command and report refusal; hooks can retain their exit policy."""
    if function is None:
        return lambda function: work_transaction(function, failure_result=failure_result)
    protected = mutation_transaction(function)

    @wraps(function)
    def locked(*args, **kwargs):
        try:
            return protected(*args, **kwargs)
        except OSError as exc:
            print(f"❌ Work-repo transaction refused/failed ({function.__name__}): {exc}", file=sys.stderr)
            return failure_result
    return locked


def queue_paths(root: Path, paths: list[str]) -> Path | None:
    """Park the complete requested path set before staging; one atomic file each.

    Separate immutable entries allow a hook to defer while its parent holds
    the checkout lock. Attribution survives a failed commit or event flush.
    """
    paths = list(dict.fromkeys(paths))
    if not paths:
        return None
    for value in paths:
        path = Path(value)
        if not value or path.is_absolute() or ".." in path.parts:
            raise ValueError(f"pending commit path must be checkout-relative: {value!r}")
    directory = checkout_metadata_dir(root) / "lmer-pending-commits"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{uuid.uuid4().hex}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "version": 1, "session": os.environ.get("LMER_SESSION_ID"), "paths": paths,
    }))
    temporary.replace(path)
    return path


def pending_paths(root: Path) -> tuple[list[Path], list[str]]:
    """Snapshot all pending requests and their complete validated path union."""
    entries, paths = [], []
    directory = checkout_metadata_dir(root) / "lmer-pending-commits"
    for entry in sorted(directory.glob("*.json")):
        record = json.loads(entry.read_text())
        if not isinstance(record, dict) or record.get("version") != 1:
            raise ValueError(f"unsupported pending commit record: {entry}")
        requested = record.get("paths")
        if not isinstance(requested, list) or any(
            not isinstance(value, str) or not value or Path(value).is_absolute()
            or ".." in Path(value).parts for value in requested
        ):
            raise ValueError(f"invalid pending commit paths: {entry}")
        entries.append(entry)
        paths.extend(value for value in requested if value not in paths)
    return entries, paths


def published_paths(entries: list[Path], result: int) -> int:
    """Clear only the entries this verified publication included."""
    if result == 0:
        for entry in entries:
            entry.unlink(missing_ok=True)
    return result
