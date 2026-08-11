"""Platform state persistence: atomic JSON snapshots and append-only history.

Why this module exists
---------------------
The daemon keeps facts that have no home in the work repo — PIDs, container ids,
published port mappings, slot occupancy, and the queue (spec §6.1). The spec
chose plain files over a database (D2), and the license for that choice is that
platform state is *reconstructible*: everything except the queue can be
re-derived from ``podman ps``, the session registry, and the work repo, so a
database's durability and transaction guarantees would buy nothing a
temp-file-plus-rename does not. What a database would cost is real, though:
schema migrations for a shape that churns weekly at first, and an opaque file
nobody can repair with an editor while the orchestrator is wedged.

That leaves this module three narrow responsibilities:

- **Each write lands whole or not at all.** Temp file + ``rename`` (the
  :mod:`slack_chat.registry` pattern), so a reader never sees a half-written
  snapshot and a crash mid-write leaves the previous snapshot intact.
- **A read never explodes on a bad file, and never destroys the evidence.**
  Unparseable content is moved aside and reported rather than overwritten in
  place — the bad bytes are usually the only record of what went wrong. This is
  the :mod:`work_repo.run_state` discipline, deliberately mirrored so the two
  state layers fail the same way.
- **History is append-only.** Rewriting a blob per event is the one access
  pattern a snapshot file genuinely cannot serve, so events go to JSONL and are
  read back tolerantly: a torn final line from a crash mid-append is skipped,
  not fatal.
- **Every snapshot is owner-only** (:data:`SNAPSHOT_FILE_MODE`), rather than
  whatever the daemon's umask happened to choose. Blanket, not per file: the
  contents range from status (the mirror state) through operator and agent prose
  (``runs.json``, ``run_meta.json``, ``relations.json``) to a file that holds
  three pieces of agent-authored text at once (``assistant.json``'s handoff,
  digest spool and standing orders), and a per-file rule would mean the next
  snapshot added here defaults to world-readable and its author has to notice.
  Nothing out of process reads any of them: every caller of :func:`read_json` is
  in this package, the UI reads through the daemon's API, no session mount
  reaches this directory, and the registry entries that *are* handed around
  carry a ``token_ref`` path rather than a token
  (``registry._reject_inline_token``, with the token file itself already 0600).
  So there is nothing for the wider bits to serve.
- **Every directory in the tree is owner-only too** (:data:`STATE_DIR_MODE`).
  Owner-only *contents* in a world-traversable directory still publish the
  metadata: which sessions exist and when each was last written, that the
  assistant was notified four times in the last minute, that a ``.bad-`` backup
  happened at 03:12. The names and the mtimes are the shape of the fleet, and a
  ``mkdir`` that takes the daemon's umask hands them to every account on the
  host. Same treatment as the transcript directory and the ask channel
  (:data:`lmer_platform.transcripts.SESSION_DIR_MODE`,
  :data:`ask_channel.protocol.DIR_MODE`), which is to say the PTY log's
  sensitivity rather than a cache's.

- **Concurrent writers of one snapshot are serialized** (:func:`mutating`,
  and :func:`write_json` takes the same lock). One process is not one writer:
  every route that changes a snapshot is a sync ``def`` handler, so Starlette
  runs them in its threadpool *concurrently* — ``POST /api/runs/adopt``,
  ``POST /api/sessions`` (via :mod:`lmer_platform.spawn`), ``POST
  /api/runs/forget`` and :mod:`lmer_platform.resume` all read ``runs.json``,
  change one key and write the whole file back. Without a lock the loser's key
  is dropped: the spawned run vanishes from the fleet view, and ``resume`` and
  ``answer`` then refuse it with ``run_not_tracked`` while its container is
  running. Per-write atomicity never addressed that — it makes each write whole,
  not each read-modify-write consistent — so the gap was open for as long as the
  handlers were sync, which is all of them.

  The lock is per resolved path and lives here rather than at the call sites,
  because the invariant belongs to the file: patching the two routes that
  collide today would leave the next writer of the same snapshot unprotected. An
  in-process :class:`threading.RLock` is enough while the daemon is the only
  writer, and it is the one place to put an ``flock`` if the ctl CLI ever writes
  a snapshot directly rather than through the API. Session registry entries need
  none of it — each is written only by the session it describes, which is exactly
  why they are separate files rather than keys in one shared file — but they pay
  nothing for having it.

Writes are loud and reads are quiet, on purpose. A failed write means state the
operator believes was recorded was not (the queue is the one thing in here that
cannot be reconstructed), so it raises. A failed read means one file is
unusable, which must not take the daemon down with it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from lmer_cli.runtime import lmer_state_dir

from . import SCHEMA_VERSION

logger = logging.getLogger("lmer_platform.store")

__all__ = [
    "SCHEMA_VERSION", "StoreError", "PLATFORM_DIR", "SNAPSHOT_FILE_MODE",
    "STATE_DIR_MODE", "utc_now_iso", "TS_FORMAT", "age_seconds", "clamp_text",
    "platform_dir", "sessions_dir", "logs_dir", "snapshot_path", "events_path",
    "ensure_state_dir",
    "read_json", "write_json", "mutating", "append_event", "read_events",
]

PLATFORM_DIRNAME = "platform"
SESSIONS_DIRNAME = "sessions"
LOGS_DIRNAME = "logs"
EVENTS_FILE = "events.jsonl"

#: Overridable root for platform state. When ``None`` the directory is derived
#: from the lmer state dir at call time, so tests can point it at a temp dir via
#: ``monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path))``. Mirrors the
#: patchable ``slack_chat.registry.REGISTRY_DIR`` convention.
PLATFORM_DIR: Optional[str] = None

#: Mode every snapshot is published with — see the module docstring for why it is
#: one rule for all of them. The same mode a transcript, a session log and the
#: shared secret already get (``transcripts.TRANSCRIPT_FILE_MODE``,
#: ``supervisor.SESSION_LOG_MODE``, :func:`lmer_platform.config.ensure_secret`):
#: the daemon's state is owner-only throughout, so no snapshot is the odd one out.
SNAPSHOT_FILE_MODE = 0o600

#: Mode every directory in the state tree is left with — see the module docstring
#: for why the file mode alone is not enough. The same 0700 the transcript
#: directory and the ask channel already get
#: (:data:`lmer_platform.transcripts.SESSION_DIR_MODE`,
#: :data:`ask_channel.protocol.DIR_MODE`).
STATE_DIR_MODE = 0o700


class StoreError(RuntimeError):
    """Raised when platform state cannot be read or written safely."""


#: One lock per snapshot, keyed on the resolved path, created on first use.
#: Never pruned: a lock is a few dozen bytes, the keys are file paths rather than
#: request-shaped input, and dropping one while a thread is between its ``read``
#: and its ``write`` is precisely the race this exists to close. The unbounded
#: key here is the sessions directory, which gains one path per session started —
#: a daemon would have to outlive hundreds of thousands of sessions for that to
#: be worth a sweep, and the sweep is what would need the proof.
_LOCKS: dict = {}

#: Guards :data:`_LOCKS` itself. Held only long enough to hand back a lock, never
#: across a read or a write, so it can never be what a caller waits behind.
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    """The lock serializing changes to *path*.

    Keyed on the resolved path rather than the object, so two callers that spell
    one snapshot differently (``PLATFORM_DIR`` repointed under a symlinked temp
    dir, say) still meet on the same lock. Non-strict resolution, because the
    common case is a snapshot that does not exist yet.

    Re-entrant: :func:`mutating` holds it across a read-modify-write and the
    :func:`write_json` inside that block takes it again on the same thread.
    """
    try:
        key = str(path.resolve())
    except (OSError, RuntimeError):
        # A symlink loop or an unreadable parent — the write is going to fail
        # anyway, and a lock keyed on the unresolved spelling still serializes
        # the callers that spell it the same way.
        key = str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


@contextlib.contextmanager
def mutating(path: Path):
    """Serialize a read-modify-write of the snapshot at *path*.

    The supported way to change a snapshot in place::

        with store.mutating(path):
            blob = read_json(path) or {}
            blob["key"] = value
            write_json(path, blob)

    Callers keep their own read, because each one is tolerant in its own way — a
    corrupt ``runs.json`` reads as an empty fleet, a corrupt session entry as an
    absent session — and that judgement belongs to the module that knows what the
    file means. What belongs here is the lock: see the module docstring for why
    "the daemon is a single process" was never the same thing as "one writer".

    A body that raises leaves the file as it was and releases the lock, which is
    the same outcome as never having entered — nothing is written until the
    caller's :func:`write_json` runs.
    """
    with _lock_for(path):
        yield


#: The one timestamp format across both state layers (``work_repo.run_state``).
#: Named because what :func:`utc_now_iso` writes is what :func:`age_seconds` has
#: to be able to read.
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utc_now_iso() -> str:
    """Timestamp in the format both state layers use (``work_repo.run_state``)."""
    return datetime.now(timezone.utc).strftime(TS_FORMAT)


def age_seconds(stamp: object, *, now: Optional[datetime] = None):
    """Seconds since an ISO-8601 Z timestamp; ``None`` when it will not parse.

    :func:`utc_now_iso`'s inverse, and here rather than in each caller because
    three copies of it is three chances to disagree about what a stamp means.

    Unparseable is missing data rather than a crash — every caller is on a read
    path where "not known" is the honest answer. ``TypeError`` shares the catch
    because that is what ``strptime`` raises for ``None``.

    A future timestamp reads **negative**, unclamped: clamping would report a
    plausible age for a fact that is wrong. Callers that must not act on one say
    so themselves (:func:`lmer_platform.checkin._latest_stamp`).
    """
    try:
        when = datetime.strptime(stamp, TS_FORMAT).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return ((now or datetime.now(timezone.utc)) - when).total_seconds()


def clamp_text(text: str, limit: int) -> str:
    """*text* cut to *limit* characters, with the cut said out loud.

    One definition for the package: there were two of this name in it, with
    different edge behaviour — same name, same package, quietly different
    answers. The ellipsis is contract, not decoration: a truncation a reader
    cannot see is worse than a long string.
    """
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def platform_dir() -> Path:
    """Root of the platform's state tree (``<state-dir>/platform`` by default)."""
    if PLATFORM_DIR is not None:
        return Path(PLATFORM_DIR)
    return lmer_state_dir() / PLATFORM_DIRNAME


def sessions_dir() -> Path:
    """Directory holding one registry file per live session (spec §6.1)."""
    return platform_dir() / SESSIONS_DIRNAME


def logs_dir() -> Path:
    """Directory holding per-session PTY logs — the scrollback source (D16)."""
    return platform_dir() / LOGS_DIRNAME


def snapshot_path(name: str) -> Path:
    """Path of a daemon-owned snapshot file, e.g. ``queue.json``."""
    return platform_dir() / name


def events_path() -> Path:
    """Path of the append-only platform history."""
    return platform_dir() / EVENTS_FILE


def _owned_levels(directory: Path) -> list[Path]:
    """*directory* and the ancestors this module owns, outermost first.

    The chain stops at :func:`platform_dir` inclusive. Above that is the lmer
    state dir, which belongs to :mod:`lmer_cli.runtime` and holds things this
    module neither writes nor knows the requirements of (the harness's own
    caches, directories a spawn mounts into a container), so tightening it here
    would be this module choosing a mode for somebody else's directory.

    A *directory* outside the platform root — a caller passing an explicit path,
    as ``append_event(path=...)`` does — is a single level for the same reason:
    it gets the tight mode itself, and whoever chose where it lives keeps its
    parents.
    """
    root = platform_dir()
    levels = [directory]
    if directory != root and root in directory.parents:
        while levels[-1].parent != root:
            levels.append(levels[-1].parent)
        levels.append(root)
    return list(reversed(levels))


def ensure_state_dir(directory: Path) -> None:
    """Create *directory* owner-only, and tighten the levels already there.

    Every level is created *with* the mode rather than chmod'ed into it
    afterwards, because a directory that is 0755 for an instant is a directory a
    listing can be taken from — the window :func:`_create_owner_only` closes for
    the file, closed for the names around it. ``mkdir(parents=True)`` cannot do
    this on its own: pathlib passes its *mode* to the leaf and creates every
    intermediate level with the default, so ``sessions/`` would land 0700 inside a
    0755 ``platform/``. Hence the walk, and the ``chmod`` behind each ``mkdir``,
    which makes the mode exact under a umask that would otherwise have narrowed
    it further (:func:`lmer_platform.ask.prepare_ask_dir` does the same two steps
    for the same two reasons).

    **A pre-existing directory is tightened, not left alone.** ``mkdir`` does
    nothing whatsoever to a directory that exists, so without this the fix would
    apply only to hosts that do not need it: every host that ever ran an earlier
    build would keep the 0755 tree that build created, since the platform root
    outlives any single release. It is the same correction
    :func:`_create_owner_only` makes to a leftover temp's bits, and it is
    conditional on the ``stat`` so the ordinary write does no syscall it does not
    need. What it will not do is *widen* anything: an operator who chose 0500 for
    an archived tree keeps it, because only bits outside :data:`STATE_DIR_MODE`
    trigger the chmod.

    Raises :class:`OSError`, for the caller to report in its own register —
    :func:`write_json` raises, :func:`append_event` logs.
    """
    for level in _owned_levels(directory):
        if level.is_dir():
            current = stat.S_IMODE(level.stat().st_mode)
            if current & ~STATE_DIR_MODE:
                level.chmod(STATE_DIR_MODE)
            continue
        # ``parents=True`` only ever reaches *above* the outermost level: the
        # walk is top-down, so every inner level's parent exists by the time it
        # is created here.
        level.mkdir(mode=STATE_DIR_MODE, parents=True, exist_ok=True)
        level.chmod(STATE_DIR_MODE)


def _backup_bad_file(path: Path, reason: str) -> StoreError:
    """Move an unusable file aside and return the error to raise.

    Never overwrite what cannot be parsed: the bad bytes are preserved as
    ``<name>.bad-<utc-compact>`` for post-mortem. Mirrors
    ``work_repo.run_state._backup_bad_state``. A failure to even rename is
    folded into the same error — the caller's problem is "this file is
    unusable" either way, and losing that message behind an OSError traceback
    would hide it.
    """
    stamp = utc_now_iso().replace(":", "").replace("-", "")
    backup = path.with_name(f"{path.name}.bad-{stamp}")
    try:
        path.rename(backup)
    except OSError as exc:
        return StoreError(f"{reason} — and it could not be moved aside ({exc})")
    logger.warning("platform_state_backed_up path=%s reason=%s", backup, reason)
    return StoreError(f"{reason} — backed up to {backup}")


def read_json(
    path: Path,
    *,
    supported_version: int = SCHEMA_VERSION,
) -> Optional[dict]:
    """Read a JSON snapshot. ``None`` when the file does not exist.

    Raises :class:`StoreError` when the file exists but is unusable. Corrupt
    content (unparseable, not a mapping, non-integer ``schema``) is backed up
    first. A ``schema`` *newer* than this code supports is a read-only refusal
    that leaves the file untouched — a future version wrote it, and truncating
    or "fixing" it here would destroy state that version still needs.

    A file with no ``schema`` key reads as version 0 rather than as corrupt,
    matching ``work_repo.run_state``: hand-written files are a legitimate way to
    seed config, and the writer stamps the version on the next write anyway.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StoreError(f"cannot read {path.name} ({exc})")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _backup_bad_file(path, f"unparseable {path.name} ({exc})")
    if not isinstance(data, dict):
        raise _backup_bad_file(path, f"{path.name} is not a JSON object")

    schema = data.get("schema", 0)
    if not isinstance(schema, int) or isinstance(schema, bool):
        raise _backup_bad_file(
            path, f"{path.name} schema field is not an integer ({schema!r})"
        )
    if schema > supported_version:
        raise StoreError(
            f"{path.name} schema {schema} is newer than supported "
            f"{supported_version} — read-only refusal"
        )
    return data


def _create_owner_only(tmp: Path) -> None:
    """Create *tmp* empty with :data:`SNAPSHOT_FILE_MODE`, before it holds anything.

    The mode has to be on the inode the ``rename`` publishes, and it has to be
    there before the first byte: a ``chmod`` after the write leaves a window in
    which the snapshot is readable by anyone on the host, and that window is the
    whole point — ``assistant.json`` carries agent-authored prose that can quote
    a credential, so "world-readable for a millisecond" is the same failure the
    secret file and the control token are opened restrictively to avoid
    (:func:`lmer_platform.config.ensure_secret`,
    ``spawn._mint_control_token``).

    ``O_TRUNC`` rather than ``O_EXCL``, and ``fchmod`` behind the ``open``: a
    temp left by a crashed write must not make its target unwritable forever, and
    ``os.open`` ignores its mode argument for a file that already exists — so a
    stale temp's looser bits are corrected while the file is still empty. Same
    shape as :func:`lmer_platform.transcripts.scrub_transcript`, which writes its
    own temp-plus-rename this way.

    The write itself then goes through :meth:`pathlib.Path.write_text`, which
    truncates the file it finds rather than creating one, so the mode set here is
    the mode the published snapshot has.

    No ``O_EXCL``, and reopening by name is deliberate rather than overlooked. The
    attack it would answer is a pre-planted temp name — a symlink under the temp
    path, which this ``open`` follows and the write then lands through — and what
    closes that is the directory, not the flag: the tree is 0700
    (:data:`STATE_DIR_MODE`, :func:`ensure_state_dir`), so the only accounts that
    can plant a name in it are the daemon's own and root, and neither needs a
    symlink to reach a file the daemon writes. Against that, ``O_EXCL`` costs the
    two properties above: a temp left by a crashed write would make its target
    unwritable until somebody deleted it by hand — recoverable only by an
    unlink-and-retry dance that races the same attacker — and the payload would
    have to go through this fd rather than through ``write_text``, which is the
    boundary the temp-name contract is observed at. A flag that trades a
    self-healing write for a loud failure on a threat the directory mode already
    refuses is churn; if this tree ever has to live somewhere group-writable,
    ``O_EXCL`` plus a write through the fd is the change to make, and this is the
    site.
    """
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, SNAPSHOT_FILE_MODE)
    try:
        os.fchmod(fd, SNAPSHOT_FILE_MODE)
    finally:
        os.close(fd)


def write_json(path: Path, payload: dict) -> None:
    """Atomically replace a snapshot file with *payload*.

    Stamps ``schema`` and ``updated`` (the caller's mapping is never mutated),
    writes a temp file in the destination directory, and ``rename``s it over the
    target — atomic on the same filesystem, so a reader sees either the old
    snapshot or the new one and never a partial write. The temp file stays in
    that directory for exactly that reason: a rename that crosses filesystems is
    a copy, and a copy is not atomic.

    The temp name carries the writer's identity — process *and* thread — for the
    reason :mod:`slack_chat.registry` carries the pid: two writers must never
    share a temp path. Sharing one is worse than losing a write. Both truncate
    it, the winner renames it onto the target, and the loser is then holding an
    open fd on the *destination* inode: its payload lands on top of the snapshot
    the winner just published, and its own ``rename`` fails ENOENT on a file that
    no longer exists. That is a torn snapshot produced by the very mechanism
    meant to prevent one. A pid alone was enough while the only concurrent
    writers were separate processes, and stopped being enough once the daemon
    began serving sync handlers from a threadpool.

    No caller in this tree is known to have collided yet: the mirror state is the
    one snapshot with overlapping writers, and ``workrepo`` writes it under the
    mirror lock on every success path — leaving only its pre-lock failure
    reporting unserialised. The thread id costs a handful of characters and takes
    the question off the table.

    The leading dot keeps the temp out of directory listings that glob
    ``*.json``, so a crashed write can never be mistaken for state.

    The temp is created owner-only *before* it is written
    (:func:`_create_owner_only`), inside a directory this call has already made
    owner-only (:func:`ensure_state_dir`), which is what makes both modes a
    property of the published snapshot rather than of a chmod that runs after the
    bytes are already on disk.
    """
    if not isinstance(payload, dict):
        raise StoreError(
            f"cannot write {path.name}: payload must be a mapping, "
            f"got {type(payload).__name__}"
        )
    record = {**payload, "schema": SCHEMA_VERSION, "updated": utc_now_iso()}
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    # Under the same lock :func:`mutating` holds, and re-entrantly so: a lone
    # write must not land in the middle of another thread's read-modify-write,
    # which is the whole failure a per-write rename never addressed. Inside a
    # ``mutating`` block on this thread it is already held and this is free.
    with _lock_for(path):
        try:
            ensure_state_dir(path.parent)
            _create_owner_only(tmp)
            tmp.write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            tmp.replace(path)
        except OSError as exc:
            # Leave no half-written temp behind to confuse the next post-mortem.
            try:
                tmp.unlink()
            except OSError:
                pass
            raise StoreError(f"cannot write {path.name} ({exc})")


def append_event(
    event_type: str,
    note: Optional[str] = None,
    data: Optional[dict] = None,
    *,
    path: Optional[Path] = None,
) -> None:
    """Append one JSON line to the platform's append-only history.

    Shape matches the work repo's ``events.jsonl`` (``ts`` / ``type`` / optional
    ``note`` / optional ``data``) so anyone reading both logs reads one format.

    Unlike :func:`write_json` this is best-effort: history is an audit trail over
    state that is itself recorded elsewhere, and losing the ability to *note* an
    event must not break the operation being noted.
    """
    target = path if path is not None else events_path()
    event: dict = {"ts": utc_now_iso(), "type": event_type}
    if note is not None:
        event["note"] = note
    if data is not None:
        event["data"] = data
    try:
        ensure_state_dir(target.parent)
        # Owner-only like every snapshot beside it: the history carries the
        # same agent-quoted text the snapshots do (assistant digests among it),
        # and a plain open("a") would take the umask — the one file in this
        # tree whose mode nobody had chosen (T93 finding). The mode binds only
        # on create; an existing file keeps whatever an operator set.
        fd = os.open(
            str(target), os.O_WRONLY | os.O_CREAT | os.O_APPEND, SNAPSHOT_FILE_MODE
        )
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning(
            "platform_event_append_failed type=%s path=%s error=%s",
            event_type, target, exc,
        )


def read_events(
    last_n: int = 0,
    *,
    path: Optional[Path] = None,
) -> list[dict]:
    """Read history, newest last. ``last_n=0`` returns everything.

    Tolerates a torn or truncated line: a crash mid-append leaves a partial
    final line, and that must not make the whole log unreadable. Non-object
    lines are skipped for the same reason.
    """
    target = path if path is not None else events_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("platform_event_read_failed path=%s error=%s", target, exc)
        return []

    events: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events[-last_n:] if last_n else events
