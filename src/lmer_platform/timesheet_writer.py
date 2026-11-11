"""The timesheet writer: the only platform code that writes the work repo (D14).

Finished entries wait in :mod:`lmer_platform.timesheet_store`'s outbox. The
writer delivers them, oldest first, through its **own clone** at
``platform_dir()/timesheet-clone`` (never the read-only mirror): one file per
entry, one commit per file, pushed with rebase-and-retry. A row leaves the
outbox only once its push has landed, and the first row that cannot be
delivered stops the drain, so a later entry never lands before an earlier one.

- **The clone holds no work of its own.** Every drain starts by converging on
  the upstream branch with ``reset --hard``, dropping any commit an earlier
  failed push left behind; the outbox, not the clone, is what is undelivered.
- **The change set is guarded, not the intended path.** After writing a file
  the writer reads the clone's actual changes (``git status``) and refuses,
  resets and cleans the clone if any path lies outside ``timesheet.d/``.
- **The credential is passed per command.** The clone's ``origin`` is the plain
  URL; fetches and pushes take the tokenized URL as an argument, as the mirror
  does (:mod:`lmer_platform.workrepo`), and every git message is scrubbed
  before it is logged or stored. A drain that finds a credential in the
  clone's ``origin`` (a crash between the clone and the ``set-url``) drops it.
- **A file already upstream at the entry's path means delivered.** The path
  carries the entry's unique id, so replaying a row after a crash between push
  and dequeue is harmless, and a hand correction made since is kept.

See docs/TIME-TRACKING.md.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Optional

from lmer_timesheet import TIMESHEET_DIR, Entry, entry_path, format_timestamp, is_person_key, parse_entry

# The same deliberate private reuse as lmer_platform.workrepo: credential
# lookup and credential scrubbing must have exactly one definition each.
from lmer_cli.container.clone_and_exec import _scrub_credentials
from lmer_cli.tokens import _inject_gitlab_token_if_available

from . import timesheet_store, workrepo
from .config import ENV_TIME_WRITER_CREDENTIAL, PlatformConfig, time_writer_token
from .store import ensure_state_dir, platform_dir

logger = logging.getLogger("lmer_platform.timesheet_writer")

__all__ = [
    "CLONE_DIRNAME", "TimesheetWriter", "DrainResult", "WrittenEntry",
    "clone_path", "written_entries", "register_writer", "get_writer",
]

CLONE_DIRNAME = "timesheet-clone"
GIT_TIMEOUT_SECONDS = 300
#: Push attempts per row, each after a fetch and rebase onto the new upstream.
PUSH_ATTEMPTS = 3
#: How long a started writer sleeps with an empty or healthy queue before it
#: looks again; :meth:`TimesheetWriter.notify` cuts the wait short.
IDLE_SECONDS = 60.0
#: The retry delay after a failed drain, doubled per consecutive failure.
BACKOFF_SECONDS = 5.0
MAX_BACKOFF_SECONDS = 300.0
#: One drain at a time per process, whichever writer runs it. The clone and the
#: outbox are shared, and a writer stopped while a push hangs (its thread outlives
#: :meth:`TimesheetWriter.stop`'s join) must not drain beside its successor.
_DRAIN_LOCK = threading.Lock()


@dataclass(frozen=True)
class DrainResult:
    """What one drain did. ``error`` is set when it stopped at a row."""

    delivered: int = 0
    remaining: int = 0
    error: Optional[str] = None


@dataclass(frozen=True)
class WrittenEntry:
    """An entry file on the clone's upstream branch, as pushed."""

    path: str
    id: str
    raw: bytes
    entry: Optional[Entry]
    problems: list = field(default_factory=list)


class _Refused(Exception):
    """A row cannot be delivered this drain; the message says why."""


def clone_path() -> Path:
    return platform_dir() / CLONE_DIRNAME


def _git(args: list, *, cwd: Optional[Path] = None, binary: bool = False,
         stdin: Optional[bytes] = None) -> tuple:
    """Run git and return ``(ok, output)``, never raising for a git failure.

    Text output is stdout and stderr together, credentials scrubbed; *binary*
    returns stdout's raw bytes. No prompt can block the daemon: there is no
    terminal and stdin is never the daemon's.
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_REFLOG_ACTION": "lmer-timesheet"}
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(cwd) if cwd else None, env=env,
            capture_output=True, timeout=GIT_TIMEOUT_SECONDS,
            **({"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}),
        )
    except FileNotFoundError:
        return False, "git is not installed or not on PATH"
    except subprocess.TimeoutExpired:
        return False, f"git {args[0]} timed out after {GIT_TIMEOUT_SECONDS}s"
    except OSError as exc:
        return False, _scrub_credentials(f"git {args[0]} failed to start ({exc})")
    if binary:
        return result.returncode == 0, result.stdout
    output = (result.stdout + result.stderr).decode("utf-8", "replace").strip()
    return result.returncode == 0, _scrub_credentials(output)


def _remote_url(config: PlatformConfig) -> str:
    """The work-repo URL with the writer's credential injected.

    :data:`~lmer_platform.config.ENV_TIME_WRITER_CREDENTIAL` when it is set,
    because the mirror's credential may only read; otherwise the mirror's.
    """
    url = (config.work_repo_url or "").strip()
    if time_writer_token():
        return _inject_gitlab_token_if_available(url, dedicated_env=ENV_TIME_WRITER_CREDENTIAL)
    return workrepo._authenticated_url(url)


def _branch(clone: Path) -> Optional[str]:
    """The upstream default branch the clone was made from."""
    ok, output = _git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=clone)
    if ok and output.startswith("origin/"):
        return output[len("origin/"):]
    ok, output = _git(["symbolic-ref", "--short", "HEAD"], cwd=clone)
    return output if ok and output else None


def _changed_paths(clone: Path) -> tuple:
    """Every path in the clone's change set: staged, unstaged and untracked.

    ``-z`` output is unquoted; a rename or copy names its source in the next
    field, and that path is part of the change set too.
    """
    ok, output = _git(
        ["status", "--porcelain", "-z", "--untracked-files=all"], cwd=clone, binary=True
    )
    if not ok:
        raise _Refused("git status failed in the timesheet clone")
    fields = output.split(b"\0")
    paths = []
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if len(record) < 4:
            continue
        paths.append(os.fsdecode(record[3:]))
        if record[:1] in (b"R", b"C") or record[1:2] in (b"R", b"C"):
            if index < len(fields):
                paths.append(os.fsdecode(fields[index]))
            index += 1
    return tuple(paths)


def _outside_timesheet_dir(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return len(parts) < 2 or parts[0] != TIMESHEET_DIR or ".." in parts


class TimesheetWriter:
    """Delivers the outbox to the work repo, from a daemon thread or a test."""

    def __init__(self, config: PlatformConfig, *, idle_seconds: float = IDLE_SECONDS,
                 backoff_seconds: float = BACKOFF_SECONDS,
                 max_backoff_seconds: float = MAX_BACKOFF_SECONDS) -> None:
        self.config = config
        self._idle_seconds = idle_seconds
        self._backoff_seconds = backoff_seconds
        self._max_backoff_seconds = max_backoff_seconds
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._drain_lock = _DRAIN_LOCK
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._drain_forever, name="lmer-timesheet-writer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=10.0)
            self._thread = None

    def notify(self) -> None:
        """Drain now rather than at the next tick: an entry was queued."""
        self._wake.set()

    def _drain_forever(self) -> None:
        failures = 0
        while not self._stop.is_set():
            self._wake.clear()
            try:
                failed = self.drain_once().error is not None
            except Exception as exc:  # the writer must outlive one bad drain
                logger.warning("platform_timesheet_drain_crashed error=%s", _scrub_credentials(str(exc)))
                failed = True
            failures = failures + 1 if failed else 0
            if failures:
                delay = min(self._backoff_seconds * 2 ** (failures - 1), self._max_backoff_seconds)
            else:
                delay = self._idle_seconds
            self._wake.wait(delay)

    # -- delivery --------------------------------------------------------------

    def drain_once(self) -> DrainResult:
        """Deliver queued rows in order until the queue is empty or one fails."""
        with self._drain_lock:
            rows = timesheet_store.pending()
            if not rows:
                return DrainResult()
            delivered = 0
            try:
                clone, branch, url = self._prepare()
                for row in rows:
                    self._deliver(clone, branch, url, row)
                    delivered += 1
            except _Refused as exc:
                reason = _scrub_credentials(str(exc))
                head = rows[delivered]
                timesheet_store.mark_failed(head.seq, reason)
                logger.warning(
                    "platform_timesheet_delivery_failed seq=%s path=%s error=%s",
                    head.seq, head.path, reason,
                )
                return DrainResult(delivered, len(rows) - delivered, reason)
            return DrainResult(delivered, 0)

    def _prepare(self) -> tuple:
        """Clone if needed, set the local identity, converge on upstream."""
        plain = workrepo._clean_url((self.config.work_repo_url or "").strip())
        if not plain:
            raise _Refused(
                "no work repo configured — set work_repo_url in config.json "
                "or export LMER_WORK_REPO"
            )
        url = _remote_url(self.config)
        clone = clone_path()
        self._ensure_clone(clone, plain, url)
        for key, value in (
            ("user.name", self.config.time_writer_name),
            ("user.email", self.config.time_writer_email),
            # The operator's global hooks and signing setup are not the
            # writer's: neither may block or alter a timesheet commit.
            ("core.hooksPath", os.devnull),
            ("commit.gpgsign", "false"),
        ):
            ok, output = _git(["config", key, value], cwd=clone)
            if not ok:
                raise _Refused(f"git config {key} failed: {output}")
        branch = _branch(clone)
        if not branch:
            raise _Refused("cannot tell the work repo's default branch from the timesheet clone")
        self._fetch(clone, branch, url)
        ok, output = _git(["reset", "-q", "--hard", f"refs/remotes/origin/{branch}"], cwd=clone)
        if not ok:
            raise _Refused(f"reset failed: {output}")
        return clone, branch, url

    def _ensure_clone(self, clone: Path, plain: str, url: str) -> None:
        if (clone / ".git").is_dir():
            # Raw, because _git scrubs: a credential a crash left between the
            # clone and the set-url below would read back as the plain URL.
            # Never log *current*.
            ok, raw = _git(["config", "--local", "--get", "remote.origin.url"], cwd=clone, binary=True)
            current = raw.decode("utf-8", "replace").strip() if ok else ""
            if current == plain:
                return
            if current and _scrub_credentials(current) == plain:
                ok, output = _git(["remote", "set-url", "origin", plain], cwd=clone)
                if ok:
                    logger.warning("platform_timesheet_clone_credential_dropped url=%s", plain)
                    return
                logger.warning("platform_timesheet_clone_credential_drop_failed error=%s", output)
            # Disposable by design: the outbox holds everything undelivered.
            logger.info("platform_timesheet_reclone url=%s", plain)
        if clone.exists():
            shutil.rmtree(clone)
        ensure_state_dir(clone.parent)
        logger.info("platform_timesheet_cloning url=%s dest=%s", plain, clone)
        ok, output = _git(["clone", "-q", "--no-tags", url, str(clone)])
        if not ok:
            shutil.rmtree(clone, ignore_errors=True)
            raise _Refused(f"clone failed: {output}")
        ok, output = _git(["remote", "set-url", "origin", plain], cwd=clone)
        if not ok:
            shutil.rmtree(clone, ignore_errors=True)
            raise _Refused(f"cannot drop the credential from the clone's remote: {output}")
        clone.chmod(0o700)

    def _fetch(self, clone: Path, branch: str, url: str) -> None:
        ok, output = _git(
            ["fetch", "-q", "--no-tags", url, f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
            cwd=clone,
        )
        if not ok:
            raise _Refused(f"fetch failed: {output}")

    def _deliver(self, clone: Path, branch: str, url: str, row) -> None:
        entry, _problems = parse_entry(row.entry_yaml)
        if entry is None or str(entry_path(entry)) != row.path or _outside_timesheet_dir(row.path):
            raise _Refused(f"outbox row {row.seq} does not match its entry's path {row.path}")
        target = clone / row.path
        root = Path(os.path.realpath(clone / TIMESHEET_DIR))
        if (clone / TIMESHEET_DIR).is_symlink() or not Path(os.path.realpath(target)).is_relative_to(root):
            raise _Refused(f"refused: {row.path} resolves outside {TIMESHEET_DIR}/")
        data = row.entry_yaml.encode("utf-8")

        ok, blob = _git(["rev-parse", "--verify", "--quiet", f"HEAD:{row.path}"], cwd=clone)
        if ok:
            ok, existing = _git(["cat-file", "blob", blob], cwd=clone, binary=True)
            if not ok:
                raise _Refused(f"cannot read {row.path} from the timesheet clone")
            # The path carries the entry's unique id, so a file there is this
            # entry: pushed before a crash, and possibly edited by hand since.
            # The file wins; re-sending would undo the person's correction.
            timesheet_store.mark_delivered(row.seq)
            logger.info("platform_timesheet_already_present seq=%s path=%s edited=%s",
                        row.seq, row.path, existing != data)
            return

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

        outside = [path for path in _changed_paths(clone) if _outside_timesheet_dir(path)]
        if outside:
            self._reset_clean(clone, branch)
            shown = ", ".join(outside[:5]) + (" …" if len(outside) > 5 else "")
            raise _Refused(f"refused: the timesheet clone had changes outside {TIMESHEET_DIR}/: {shown}")

        ok, output = _git(["add", "--", row.path], cwd=clone)
        if not ok:
            raise _Refused(f"git add failed: {output}")
        message = f"timesheet: {entry.person} {entry.project} {format_timestamp(entry.start)}"
        ok, output = _git(["commit", "-q", "--no-verify", "-m", message, "--", row.path], cwd=clone)
        if not ok:
            raise _Refused(f"commit failed: {output}")
        self._push(clone, branch, url)
        timesheet_store.mark_delivered(row.seq)
        logger.info("platform_timesheet_delivered seq=%s path=%s", row.seq, row.path)

    def _push(self, clone: Path, branch: str, url: str) -> None:
        """Push HEAD, rebasing onto a moved upstream between bounded attempts."""
        output = ""
        for attempt in range(PUSH_ATTEMPTS):
            if attempt:
                self._fetch(clone, branch, url)
                ok, rebase_output = _git(["rebase", "-q", f"refs/remotes/origin/{branch}"], cwd=clone)
                if not ok:
                    _git(["rebase", "--abort"], cwd=clone)
                    raise _Refused(f"rebase onto the work repo failed: {rebase_output}")
            self._guard_outgoing(clone, branch)
            ok, output = _git(["push", "-q", "--no-verify", url, f"HEAD:refs/heads/{branch}"], cwd=clone)
            if ok:
                _git(["update-ref", f"refs/remotes/origin/{branch}", "HEAD"], cwd=clone)
                return
        raise _Refused(f"push refused after {PUSH_ATTEMPTS} attempts: {output}")

    def _guard_outgoing(self, clone: Path, branch: str) -> None:
        """Refuse a push whose commits touch anything outside ``timesheet.d/``."""
        ok, output = _git(
            ["diff", "--name-only", "-z", "--no-renames", f"refs/remotes/origin/{branch}", "HEAD"],
            cwd=clone, binary=True,
        )
        if not ok:
            raise _Refused("cannot list the paths the timesheet push would change")
        outside = [os.fsdecode(path) for path in output.split(b"\0")
                   if path and _outside_timesheet_dir(os.fsdecode(path))]
        if outside:
            self._reset_clean(clone, branch)
            shown = ", ".join(outside[:5]) + (" …" if len(outside) > 5 else "")
            raise _Refused(f"refused: the timesheet push would change paths outside {TIMESHEET_DIR}/: {shown}")

    def _reset_clean(self, clone: Path, branch: str) -> None:
        _git(["reset", "-q", "--hard", f"refs/remotes/origin/{branch}"], cwd=clone)
        _git(["clean", "-q", "-fdx"], cwd=clone)


def upstream_ahead_of(repo: Path) -> bool:
    """Whether the clone last saw upstream commits that *repo*'s ``HEAD`` lacks.

    Answered in the clone, which has full history, because the mirror is
    shallow: *repo*'s ``HEAD`` is a strict ancestor of the clone's ``origin``
    ref. When the clone does not know that ``HEAD``, *repo* pulled after the
    clone last fetched or pushed, so it already holds every entry the clone
    could add, or their later deletion. ``True`` when *repo* has no readable
    ``HEAD``: nothing shows it holds what the clone has.
    """
    clone = clone_path()
    if not (clone / ".git").is_dir():
        return False
    branch = _branch(clone)
    if not branch:
        return False
    ok, seen = _git(["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}^{{commit}}"],
                    cwd=clone)
    if not ok or not seen:
        return False
    if not (Path(repo) / ".git").exists():
        return True
    ok, head = _git(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], cwd=Path(repo))
    if not ok or not head:
        return True
    if head == seen:
        return False
    ok, _output = _git(["merge-base", "--is-ancestor", head, seen], cwd=clone)
    return ok


def written_entries(person: str) -> list:
    """The entry files of *person* on the upstream branch the clone last saw.

    Read from the clone's ``origin`` ref rather than its working tree, so an
    entry committed but not yet pushed is not listed: it is still in the
    outbox. Empty when there is no clone yet.
    """
    clone = clone_path()
    if not is_person_key(person) or not (clone / ".git").is_dir():
        return []
    branch = _branch(clone)
    if not branch:
        return []
    ok, listing = _git(
        ["ls-tree", "-r", "-z", f"refs/remotes/origin/{branch}", "--", f"{TIMESHEET_DIR}/{person}/"],
        cwd=clone, binary=True,
    )
    if not ok:
        return []
    blobs = []
    for record in listing.split(b"\0"):
        if not record:
            continue
        meta, _, raw_path = record.partition(b"\t")
        _mode, kind, sha = meta.split(b" ")
        path = os.fsdecode(raw_path)
        if kind == b"blob" and path.endswith(".yaml"):
            blobs.append((path, sha))
    if not blobs:
        return []
    ok, batch = _git(
        ["cat-file", "--batch"], cwd=clone, binary=True,
        stdin=b"".join(sha + b"\n" for _path, sha in blobs),
    )
    if not ok:
        return []
    entries = []
    offset = 0
    for path, _sha in blobs:
        header_end = batch.index(b"\n", offset)
        size = int(batch[offset:header_end].split(b" ")[2])
        raw = batch[header_end + 1:header_end + 1 + size]
        offset = header_end + 1 + size + 1
        entry, problems = parse_entry(raw, path=path)
        entry_id = entry.id if entry else PurePosixPath(path).stem.rpartition("--")[2]
        entries.append(WrittenEntry(path=path, id=entry_id, raw=raw, entry=entry, problems=problems))
    return entries


# ---------------------------------------------------------------------------
# Process-wide writer, the way the comms hub is published
# ---------------------------------------------------------------------------

_WRITER: Optional[TimesheetWriter] = None
_WRITER_LOCK = threading.Lock()


def register_writer(writer: Optional[TimesheetWriter]) -> None:
    global _WRITER
    with _WRITER_LOCK:
        _WRITER = writer


def get_writer() -> Optional[TimesheetWriter]:
    with _WRITER_LOCK:
        return _WRITER
