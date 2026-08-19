"""Host-side mirror clone of the work repo (spec D24).

Why a mirror at all
-------------------
The platform needs run state — phases, open questions, ledgers — for every run on
the host. That state lives in the work repo, and the spec originally assumed the
daemon could read it off local disk. It cannot: ``LMER_WORK_REPO`` is a git *URL*
and each **container** clones it to ``/work`` (``docs/LMER-CLI.md``). There is no
host-side checkout, so the daemon keeps its own.

The rejected alternatives are worth recording, because both look cheaper than
they are. Having sessions push their state to the control API is immediate, but
requires cooperation from every session and duplicates run state into platform
state. Reading it with ``podman exec`` is also immediate, but fails exactly where
the platform matters most: a run that stopped to ask a question has *exited*, so
there is no container left to exec into — and that is the row the operator most
needs to see.

What this costs, stated plainly because the UI must not paper over it:
**liveness is instant, progress is eventual.** Whether a session is running comes
from the local registry. Its phase and open question come from git, and lag by
the session's commit cadence plus the pull interval. The lag lands in a forgiving
place — the run-state contract makes a session ``work commit`` *before* stopping
to ask — but a phase transition mid-work can be a minute stale.

Design notes
------------
- **Read-only and disposable.** The mirror never holds local work, so a fetch +
  ``reset --hard`` is the right update: it converges on the remote even after a
  force-push, which a ``pull`` can fail to do. Losing the mirror entirely costs
  one re-clone.
- **The token is never persisted.** Cloning with a tokenized URL would bake the
  credential into ``.git/config`` on a long-lived host directory. So the remote is
  immediately rewritten to the clean URL and each fetch passes the tokenized URL
  as an argument instead. The mirror directory is created 0700.
- **Git failures are recorded, never raised.** A daemon that will not boot
  because a fetch failed is worse than one that boots and reports staleness — and
  a silently stale fleet view is worse than both (R20). Every outcome lands in
  ``mirror.json`` for the API to expose.
- **Credential scrubbing is mandatory.** Git echoes the URL it was given into
  its error output, so every captured stream goes through ``_scrub_credentials``
  before being stored or logged. This is the leak that was fixed in
  ``clone_and_exec.py`` (MR !104); the same discipline applies here.
- **One update at a time.** Callers overlap by design (see :func:`_mirror_lock`),
  and two concurrent shallow fetches make git abort one of them — as do two
  first-boot clones, where the loser finds the destination already populated.
  Nothing is damaged either way, but both were reported as mirror failures — so
  the clone and the fetch/reset pair each run under an advisory lock instead.
- **A run's slug is its identity; its directory name is only an address.** The
  two agree until a run is named, at which point the container takes its one
  name-bearing rename to ``runs/<slug>--<name>/`` (``work_repo.run_state.
  freeze_run_dir``, issue #87 D2) and the address stops being the identity —
  which is why the work tooling resolves a run by recorded ``state.slug`` and
  never by directory name (#87 D1). This module used to compose the address from
  the slug and stop there, so **every named run the platform spawned went dark**:
  see :func:`resolve_run_dir`.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import logging
import os
import shutil
import subprocess
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

import yaml
from work_repo import run_state

# Private helpers reused deliberately rather than reimplemented: credential
# lookup and credential scrubbing must have exactly one definition each.
# `clone_cache.py` imports `_scrub_credentials` the same way.
from lmer_cli.container.clone_and_exec import _scrub_credentials
from lmer_cli.tokens import _inject_gitlab_token_if_available

from .config import PlatformConfig
from .store import (
    StoreError, mutating, read_json, snapshot_path, utc_now_iso, write_json,
)

logger = logging.getLogger("lmer_platform.workrepo")

__all__ = [
    "MIRROR_STATE_FILE", "RunDirRef", "MirrorStatus", "mirror_status",
    "ensure_clone", "pull", "iter_run_dirs", "run_dirs",
]

MIRROR_STATE_FILE = "mirror.json"
GIT_TIMEOUT_SECONDS = 300
#: Suffix of the mirror's advisory update lock, a sibling of the mirror directory.
MIRROR_LOCK_SUFFIX = ".lock"
#: ``flock`` says "held by someone else" with these. Any *other* ``OSError``
#: means locking itself is unavailable here (a filesystem without ``flock``,
#: ``ENOLCK``), which is a different situation with a different answer.
_LOCK_HELD_ERRNOS = (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK)
#: Directory names that never contain run dirs and are expensive to walk.
_PRUNED_DIRS = {".git", "node_modules", "__pycache__"}
#: Run-state file names, in the order ``work_repo.run_state`` looks for them.
_STATE_FILENAMES = ("state.yaml", "state.yml")
#: Facts about run directories this process has already said out loud, so a read
#: path the UI polls says each of them once — see :func:`_announce_once`.
_ANNOUNCED: set = set()
# One scan per mirrored revision and project for carried reslug identities. The
# platform polls tracked rows individually; without this index every missing
# direct slug re-read every state file in the project.
_RESLUGGED_INDEX: dict[tuple[str, str, str], tuple[str, dict[str, str]]] = {}
_RESLUGGED_INDEX_LOCK = threading.Lock()
#: Characters that stop a slug from being usable as half of a directory-name glob
#: pattern — path separators and the wildcards — see :func:`_renamed_run_dir`.
_NOT_IN_A_DIR_NAME = ("/", "\\", "*", "?", "[")


@dataclass(frozen=True)
class RunDirRef:
    """One run directory found in the mirror.

    :attr:`slug` is the run's *identity* and :attr:`dir_name` its *address*, and
    they are separate fields because for a named run they stop being the same
    string (see the module header). ``dir_name`` is ``None`` whenever the two
    agree, which is every ref this module could build before a named run's dir
    could be found at all — so an unnamed run's ref is what it always was.
    """

    host: str
    project: str
    slug: str
    path: Path
    #: The directory this run actually occupies, when that is not its slug — i.e.
    #: after the container's one name-bearing rename. Set by
    #: :func:`resolve_run_dir` when it is asked for a run, and by
    #: :func:`iter_run_dirs` when it reads one off the disk; ``None`` means the run
    #: sits at ``runs/<slug>``.
    dir_name: Optional[str] = None

    @property
    def rel_path(self) -> str:
        """Work-repo-relative path, e.g. ``gitlab.example.com/group/project/runs/foo``.

        The *address*, so a caller that renders a forge link or quotes a path in
        an error names the directory a human can actually open — which for a named
        run is ``runs/<slug>--<name>``, not ``runs/<slug>``.
        """
        return f"{self.host}/{self.project}/runs/{self.dir_name or self.slug}"


@dataclass(frozen=True)
class MirrorStatus:
    """What the daemon knows about its mirror, including how stale it is."""

    present: bool
    url: Optional[str] = None
    last_pull_at: Optional[str] = None
    last_pull_ok: Optional[bool] = None
    last_error: Optional[str] = None
    head_sha: Optional[str] = None

    @property
    def healthy(self) -> bool:
        return self.present and self.last_pull_ok is True

    def to_dict(self) -> dict:
        return {
            "present": self.present,
            "url": self.url,
            "last_pull_at": self.last_pull_at,
            "last_pull_ok": self.last_pull_ok,
            "last_error": self.last_error,
            "head_sha": self.head_sha,
            "healthy": self.healthy,
        }


def _now() -> float:
    """Monotonic-enough wall clock, isolated so tests can control it."""
    return time.time()


def _state_path() -> Path:
    return snapshot_path(MIRROR_STATE_FILE)


def _load_state() -> dict:
    try:
        return read_json(_state_path()) or {}
    except StoreError as exc:
        logger.warning("platform_mirror_state_unreadable error=%s", exc)
        return {}


def _save_state(**fields) -> None:
    # Merges into what is on disk, so the read and the write are one operation:
    # the mirror lock covers the success paths but not the pre-lock failure
    # reporting, and two of those overlapping would each write the whole file
    # from its own read.
    with mutating(_state_path()):
        state = _load_state()
        state.update(fields)
        state.pop("schema", None)
        state.pop("updated", None)
        try:
            write_json(_state_path(), state)
        except StoreError as exc:
            # Losing the bookkeeping must not fail the pull it describes; the
            # next successful write repairs it.
            logger.warning("platform_mirror_state_unwritable error=%s", exc)


def _record_failure(message: str) -> None:
    scrubbed = _scrub_credentials(message)
    logger.warning("platform_mirror_failure error=%s", scrubbed)
    _save_state(
        last_pull_at=utc_now_iso(), last_pull_ok=False, last_error=scrubbed
    )


def _git(args: list[str], *, cwd: Optional[Path] = None) -> tuple[bool, str]:
    """Run git, returning ``(ok, output)`` with credentials already scrubbed.

    Never raises for a git-level failure: the caller records the message. A
    missing git binary or a timeout is reported the same way, since from the
    operator's perspective they are all "the mirror could not be updated".
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return False, "git is not installed or not on PATH"
    except subprocess.TimeoutExpired:
        return False, f"git {args[0]} timed out after {GIT_TIMEOUT_SECONDS}s"
    except OSError as exc:
        return False, _scrub_credentials(f"git {args[0]} failed to start ({exc})")

    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0, _scrub_credentials(output.strip())


def _clean_url(url: str) -> str:
    """The URL with any embedded credentials removed."""
    return _scrub_credentials(url or "")


def _authenticated_url(url: str) -> str:
    """The clone/fetch URL with a work-repo token injected when one is available.

    ``LMER_WORK_REPO_TOKEN`` is the documented dedicated variable;
    ``GITLAB_TOKEN_worklog`` is honored as the deprecated fallback that existing
    setups may still use (``lmer_cli.tokens`` keeps that pair behind its
    ``for_work_repo`` flag, which the public injection helper does not expose).
    """
    dedicated = "LMER_WORK_REPO_TOKEN"
    if not os.environ.get(dedicated) and os.environ.get("GITLAB_TOKEN_worklog"):
        dedicated = "GITLAB_TOKEN_worklog"
    return _inject_gitlab_token_if_available(url, dedicated_env=dedicated)


def _head_sha(mirror: Path) -> Optional[str]:
    ok, output = _git(["rev-parse", "HEAD"], cwd=mirror)
    return output.strip() if ok and output.strip() else None


def mirror_status(config: PlatformConfig) -> MirrorStatus:
    """Everything the daemon knows about the mirror, without touching the network."""
    mirror = config.mirror_path
    state = _load_state()
    return MirrorStatus(
        present=(mirror / ".git").is_dir(),
        url=state.get("url") or _clean_url(config.work_repo_url or "") or None,
        last_pull_at=state.get("last_pull_at"),
        last_pull_ok=state.get("last_pull_ok"),
        last_error=state.get("last_error"),
        head_sha=state.get("head_sha"),
    )


def ensure_clone(config: PlatformConfig) -> MirrorStatus:
    """Clone the work repo if the mirror is absent. Never raises.

    Refuses to touch a mirror that was cloned from a *different* URL rather than
    wiping it: an automatic ``rm -rf`` of an operator's directory is not a call
    this code should make, and silently serving the wrong repo's runs is exactly
    the confusion R20 is about. The error text says what to remove.

    The clone runs under :func:`_mirror_lock` for the reason the fetch does, and
    **finding that lock held is the same success**: a peer is cloning this very
    mirror, so the honest answer is the current status. Two first-boot callers
    used to race here — the loser handed git a destination the winner had already
    created and got "already exists and is not an empty directory", recorded as
    ``clone failed`` against a mirror that was fine. The window is only open
    until the mirror exists, but while it is open it produces the same misleading
    "unhealthy mirror" as the fetch race did.
    """
    url = (config.work_repo_url or "").strip()
    mirror = config.mirror_path

    if not url:
        _record_failure(
            "no work repo configured — set work_repo_url in config.json "
            "or export LMER_WORK_REPO"
        )
        return mirror_status(config)

    if (mirror / ".git").is_dir():
        return _status_of_existing_mirror(config, url)

    try:
        # Before taking the lock, not after: the lock file is a sibling of the
        # mirror, so its directory is what has to exist for the lock to be
        # takeable at all — and on first boot, the only time this race is open,
        # nothing has created it yet. Creating it under the lock would mean
        # every first-boot caller found the lock unusable and cloned anyway.
        mirror.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _record_failure(f"cannot create mirror parent {mirror.parent} ({exc})")
        return mirror_status(config)

    with _mirror_lock(config) as acquired:
        if not acquired:
            logger.debug("platform_mirror_clone_in_progress dest=%s", mirror)
            return mirror_status(config)

        # Look again now the lock is ours. The check above is the check-then-act
        # half of the race: a peer can finish its clone and release between our
        # look and our acquire, and handing git a destination that is now a
        # populated directory is exactly the failure this lock exists to stop.
        if (mirror / ".git").is_dir():
            return _status_of_existing_mirror(config, url)

        return _clone(config, url)


def _status_of_existing_mirror(config: PlatformConfig, url: str) -> MirrorStatus:
    """Status of a mirror already on disk, refusing one cloned from elsewhere."""
    recorded = _load_state().get("url")
    if recorded and recorded != _clean_url(url):
        _record_failure(
            f"mirror at {config.mirror_path} was cloned from {recorded}, but the "
            f"configured work repo is {_clean_url(url)} — remove that "
            "directory to re-clone"
        )
    return mirror_status(config)


def _clone(config: PlatformConfig, url: str) -> MirrorStatus:
    """The clone and its remote rewrite. Call only under :func:`_mirror_lock`."""
    mirror = config.mirror_path
    logger.info("platform_mirror_cloning url=%s dest=%s", _clean_url(url), mirror)

    ok, output = _git([
        "clone", "--depth", "1", "--no-tags",
        _authenticated_url(url), str(mirror),
    ])
    if not ok:
        # A clone that really failed is still a failure worth recording: the
        # lock only excuses losing to a peer, never a remote that is gone.
        _record_failure(f"clone failed: {output}")
        # A partial clone would be mistaken for a usable mirror.
        if mirror.exists() and not (mirror / ".git").is_dir():
            shutil.rmtree(mirror, ignore_errors=True)
        return mirror_status(config)

    # Never leave the token in .git/config on a long-lived host directory.
    set_ok, set_output = _git(
        ["remote", "set-url", "origin", _clean_url(url)], cwd=mirror
    )
    if not set_ok:
        logger.warning("platform_mirror_remote_rewrite_failed error=%s", set_output)
    try:
        mirror.chmod(0o700)
    except OSError:
        pass

    _save_state(
        url=_clean_url(url),
        last_pull_at=utc_now_iso(),
        last_pull_ok=True,
        last_error=None,
        head_sha=_head_sha(mirror),
    )
    logger.info("platform_mirror_cloned dest=%s", mirror)
    return mirror_status(config)


def _lock_path(config: PlatformConfig) -> Path:
    """Path of the mirror's update lock.

    A sibling of the mirror directory rather than a file in the platform state
    dir, because the lock protects *the mirror* and not the daemon holding it:
    two configs pointing at one mirror must contend, and a mirror moved
    elsewhere must not keep contending on the old lock. Outside the working tree
    on purpose — a lock file inside it would show up as untracked content in the
    repo the platform is mirroring.
    """
    mirror = config.mirror_path
    return mirror.with_name(mirror.name + MIRROR_LOCK_SUFFIX)


@contextlib.contextmanager
def _mirror_lock(config: PlatformConfig) -> Iterator[bool]:
    """Hold the mirror's update lock for the body, yielding whether we got it.

    Why any lock: callers overlap routinely. ``build_state`` pulls while serving
    a request and Starlette runs those handlers in a threadpool (:mod:`.api` says
    why), so two polls of the fleet view can sit in :func:`pull` at once — and
    ``lmer platform status`` adds a *second process* against the same mirror.
    Two concurrent ``git fetch --depth 1`` runs both rewrite ``.git/shallow``, so
    git re-reads it before committing and aborts the loser with "shallow file has
    changed since we read it". That guard is doing its job — the losing fetch
    stops before touching anything, and it lost to one that succeeded — but the
    abort used to be recorded as ``last_error``, which told the operator their
    perfectly current mirror was unhealthy. The same pair of callers racing on
    first boot instead lands in :func:`ensure_clone`, where the loser hands git a
    destination directory the winner has already created — a narrower window,
    since it closes once the mirror exists, but the same false report.

    ``flock`` and not a lockfile holding a PID: the kernel releases it when the
    fd closes or the holder dies, so a daemon killed mid-fetch cannot wedge every
    later pull, and there is no stale-lock reaper to get wrong. A fresh ``open``
    per call is required, not incidental — ``flock`` is owned by the open file
    description, so two *threads* of one process contend only while each holds
    its own fd. Caching one module-level fd would silently disable the lock in
    exactly the case that motivated it.

    Yields ``False`` when another update is in flight; see :func:`pull` and
    :func:`ensure_clone` for why that is a success. Yields ``True`` if locking is
    unavailable altogether: an unserialised update is the old, racy behaviour,
    and it beats a mirror that would never update again.

    Non-blocking on purpose. Waiting would pin the caller — a request handler —
    for up to ``GIT_TIMEOUT_SECONDS`` behind someone else's git, to then do work
    that peer has already done.
    """
    path = _lock_path(config)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        logger.warning(
            "platform_mirror_lock_unusable path=%s error=%s — pulling unserialised",
            path, exc,
        )
        yield True
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in _LOCK_HELD_ERRNOS:
                yield False
                return
            logger.warning(
                "platform_mirror_lock_unsupported path=%s error=%s — "
                "pulling unserialised", path, exc,
            )
            yield True
            return
        yield True
    finally:
        # Closing releases the lock; the file itself stays, since unlinking it
        # would let the next caller lock a fresh inode and exclude nobody.
        os.close(fd)


def pull(config: PlatformConfig, *, force: bool = False) -> MirrorStatus:
    """Bring the mirror up to date, throttled by ``work_repo_pull_interval``.

    Fetches shallowly and ``reset --hard``s onto ``FETCH_HEAD``: the mirror holds
    no local work, so converging on the remote is always correct and survives a
    force-push that would leave a ``pull`` stuck.

    ``force`` skips the throttle (the ``rescan`` path). Never raises — every
    failure is recorded for the API to surface.

    The update runs under :func:`_mirror_lock`, and **finding that lock held is a
    success**: it means another caller is fetching this same mirror right now, so
    the honest answer is the current status and let them finish. Recording an
    error there would only move the spurious ``last_error`` from git's race guard
    to ours, and ``last_error`` has to keep meaning "the mirror could not be
    updated" — bad credentials, a remote that is gone, no network.
    """
    # Sequential and not nested: ``ensure_clone`` takes this same lock and has
    # released it by the time it returns. Calling it from inside the block below
    # would deadlock in the honest sense — a second ``flock`` on a fresh fd is
    # indistinguishable from a peer's, so the fetch would skip itself forever.
    status = ensure_clone(config)
    if not status.present:
        return status

    if not force and not _throttle_expired(config):
        return status

    with _mirror_lock(config) as acquired:
        if not acquired:
            logger.debug(
                "platform_mirror_pull_in_progress mirror=%s", config.mirror_path
            )
            return mirror_status(config)

        # Read the throttle again now the lock is ours. The pre-lock check is an
        # optimisation that can only have been made stale in the harmless
        # direction: whoever we queued behind may have just satisfied it, and a
        # caller that had to wait usually has nothing left to fetch. The
        # timestamp is written under this lock too, which is what closes the
        # check-then-act window that let two callers both decide to fetch.
        if not force and not _throttle_expired(config):
            return mirror_status(config)

        return _fetch_and_converge(config)


def _fetch_and_converge(config: PlatformConfig) -> MirrorStatus:
    """The fetch + ``reset --hard`` pair. Call only under :func:`_mirror_lock`."""
    url = _authenticated_url((config.work_repo_url or "").strip())
    mirror = config.mirror_path

    ok, output = _git(
        ["fetch", "--depth", "1", "--no-tags", url, "HEAD"], cwd=mirror
    )
    if not ok:
        _record_failure(f"fetch failed: {output}")
        return mirror_status(config)

    ok, output = _git(["reset", "--hard", "FETCH_HEAD"], cwd=mirror)
    if not ok:
        _record_failure(f"reset failed: {output}")
        return mirror_status(config)

    _save_state(
        url=_clean_url((config.work_repo_url or "").strip()),
        last_pull_at=utc_now_iso(),
        last_pull_ok=True,
        last_error=None,
        head_sha=_head_sha(mirror),
        last_pull_monotonic=_now(),
    )
    return mirror_status(config)


def _throttle_expired(config: PlatformConfig) -> bool:
    """Whether enough time has passed since the last pull attempt.

    Uses a wall-clock stamp recorded alongside the mirror state. A clock that
    jumps backwards makes this return ``True`` (pull sooner than needed), which
    is the harmless direction — the alternative is a mirror that refuses to
    update until the clock catches up.
    """
    last = _load_state().get("last_pull_monotonic")
    if not isinstance(last, (int, float)):
        return True
    elapsed = _now() - last
    if elapsed < 0:
        return True
    return elapsed >= config.work_repo_pull_interval


def iter_run_dirs(config: PlatformConfig) -> Iterator[RunDirRef]:
    """Yield every run directory in the mirror, across all projects.

    Walks for directories named ``runs`` rather than globbing a fixed depth,
    because a project path is not a fixed number of segments — the work repo holds
    both ``gitlab.example.com/group/project/runs/…`` and
    ``gitlab.example.com/group/subgroup/project/runs/…``. This is also why
    ``run_state.runs_base()`` cannot serve the platform: it resolves exactly one
    project from ``LMER_REPO_HOST``/``LMER_REPO_PROJECT``, and the platform needs
    the whole fleet.

    A directory only counts as a run if it holds a run-state file, which keeps
    stray directories under a ``runs/`` parent out of the inventory.

    Each run is keyed by the slug its state file records, not by the directory it
    sits in — see :func:`_listed_refs` for why the difference matters to the one
    thing this feeds, the adoption picker.
    """
    mirror = config.mirror_path
    if not mirror.is_dir():
        return

    for dirpath, dirnames, _filenames in os.walk(mirror):
        dirnames[:] = [d for d in dirnames if d not in _PRUNED_DIRS]
        current = Path(dirpath)
        if current.name != "runs":
            continue

        # Do not descend into run dirs hunting for a nested `runs`.
        dirnames[:] = []
        try:
            rel = current.parent.relative_to(mirror)
        except ValueError:  # pragma: no cover - os.walk stays under mirror
            continue
        parts = rel.parts
        if len(parts) < 2:
            # Needs at least <host>/<project>; a bare `runs` at the top of the
            # repo belongs to no project.
            continue
        host, project = parts[0], "/".join(parts[1:])

        yield from _listed_refs(host, project, [
            entry
            for entry in sorted(current.iterdir())
            if entry.is_dir()
            and any((entry / name).is_file() for name in _STATE_FILENAMES)
        ])


def run_dirs(config: PlatformConfig) -> list[RunDirRef]:
    """:func:`iter_run_dirs` as a list, ordered by host, project, then slug.

    Enumerates the **whole shared work repo**, so this is not the fleet view — it
    is the candidate list for adoption (spec D25). The inventory is scoped by the
    local run index instead; use :func:`resolve_run_dir` for tracked runs.

    Ordered by :attr:`RunDirRef.slug`, which for a named run is what its state file
    records rather than the directory it sits in (:func:`_claimed_slug`) — so a run
    already tracked appears in this list under the key it is tracked by.
    """
    return sorted(
        iter_run_dirs(config), key=lambda r: (r.host, r.project, r.slug)
    )


def _ref_at(
    root: Path, host: str, project: str, slug: str, dir_name: str
) -> Optional[RunDirRef]:
    """A ref for ``runs/<dir_name>`` under *root*, or ``None`` if that is not a run.

    The containment re-check is the one :func:`lmer_platform.api._mirror_project_dir`
    makes (T73), and the shared ``None`` is deliberate: a caller able to tell a
    refused walk from an absent run has been told whether the path it aimed at
    exists on this host. The segments are index-fed rather than request-fed, which
    is why the check belongs at the composition and not only at the routes — an
    entry hand-edited or corrupted into carrying ``..`` reaches this line without
    passing :func:`lmer_platform.answer._reject_traversal`. Resolving first also
    means a run dir that is a *symlink* out of the mirror is refused, as in
    :func:`lmer_platform.api._safe_asset`.
    """
    try:
        candidate = (root / host / project / "runs" / dir_name).resolve()
        candidate.relative_to(root)
    # RuntimeError as well as OSError: on 3.12 a symlink loop makes resolve()
    # raise it, and letting that through would turn a refusal into a traceback.
    except (OSError, RuntimeError, ValueError):
        return None
    if not candidate.is_dir():
        return None
    if not any((candidate / name).is_file() for name in _STATE_FILENAMES):
        return None
    return RunDirRef(
        host=host,
        project=project,
        slug=slug,
        path=candidate,
        dir_name=None if dir_name == slug else dir_name,
    )


def _read_run_state(path: Path) -> Optional[dict]:
    """Read one mirror state file without ever mutating the checkout."""
    for name in _STATE_FILENAMES:
        state_file = path / name
        if not state_file.is_file():
            continue
        try:
            state = yaml.safe_load(state_file.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return None
        return state if isinstance(state, dict) else None
    return None


def _recorded_slug(path: Path) -> Optional[str]:
    """The slug a run dir *says* it is, or ``None`` when it does not say.

    Read here rather than through ``work_repo.run_state.load_state`` on purpose:
    that function moves an unparseable state file aside for post-mortem, and the
    mirror is a read-only checkout this daemon must not write into — a backup file
    created here would be a local modification for the next ``reset --hard`` to
    trip over. Everything unreadable, unparseable or slugless is ``None``, which
    the one caller treats as "this dir does not claim to be the run I am looking
    for".
    """
    state = _read_run_state(path)
    if state is None:
        return None
    recorded = state.get("slug")
    if not isinstance(recorded, str) or not recorded.strip():
        return None
    return recorded.strip()


def _announce_once(key: str, level: int, message: str, *args) -> None:
    """Log *message* the first time this process sees *key*, then stay quiet.

    Resolution happens on a read path the UI polls every few seconds, so a line
    per resolution would say the same thing several hundred times an hour and bury
    the log that has to stay readable. Said once per daemon per run, which is
    exactly as often as the fact is news.
    """
    if key in _ANNOUNCED:
        return
    _ANNOUNCED.add(key)
    logger.log(level, message, *args)


def _claimed_slug(path: Path) -> str:
    """The slug this run directory claims, falling back to its own name.

    A run's identity is the slug in its state file and its directory is only an
    address (module header), so a listing that keyed on the address would file a
    *named* run under ``<slug>--<name>`` — a name nothing else in the platform
    uses, since the index, the sessions and :func:`resolve_run_dir` all key on the
    slug. In the adoption picker that reads as a second, untracked run.

    The claim is honoured only when the directory name follows the container's own
    grammar for it — ``runs/<slug>`` before the rename, ``runs/<slug>--<name>``
    after (``work_repo.run_state.freeze_run_dir``). Anything else is reported as it
    is found, under its directory name: a dir named neither way is a run whose
    address nobody can derive from its slug, so :func:`resolve_run_dir` would not
    find it under the claimed slug either, and offering an operator a key that
    resolves to nothing is worse than naming the directory they can see. That is
    also why the state file is read only for a name-bearing directory: without a
    ``--`` the grammar cannot fire, so the answer is the directory name whatever
    the file says, and on a shared work repo that is a good third of the reads
    this route would otherwise make.

    The state file is read through :func:`_recorded_slug` and for the reason given
    there — the mirror is a read-only checkout this daemon must not write into,
    which rules ``run_state.load_state`` out on a listing as much as on a resolve.
    """
    if "--" not in path.name:
        return path.name
    recorded = _recorded_slug(path)
    if recorded is None:
        return path.name
    if recorded == path.name or path.name.startswith(f"{recorded}--"):
        return recorded
    return path.name


def _listed_refs(host: str, project: str, entries: list[Path]) -> Iterator[RunDirRef]:
    """One ref per run directory in a single ``runs/``, keyed by recorded slug.

    Healing an address back to an identity (:func:`_claimed_slug`) is only safe
    while the identity picks out one directory, and in this repo it sometimes does
    not: a taskdef run with no target is slugged after the taskdef alone
    (``run_state.derive_slug``), so two ``masterplan`` runs can each record
    ``slug: masterplan``. :func:`resolve_run_dir` refuses that ambiguity and says
    to track such a run by its directory name — so that is exactly what this
    offers, listing every directory that shares a claim under its own name. The
    same goes for a claim that is some *other* directory's name: that directory is
    where :func:`resolve_run_dir` would land.

    The result is that no run is listed twice and every key listed is one
    :func:`resolve_run_dir` can find again — directory names are unique within a
    ``runs/``, and a claim is only honoured when it collides with neither a name
    nor another claim.
    """
    claims = {entry: _claimed_slug(entry) for entry in entries}
    names = {entry.name for entry in entries}
    counts = Counter(claims.values())
    for entry, claim in claims.items():
        if claim == entry.name:
            yield RunDirRef(host=host, project=project, slug=claim, path=entry)
            continue
        if counts[claim] > 1 or claim in names:
            _announce_once(
                f"listed-by-name:{host}/{project}/{entry.name}",
                logging.INFO,
                "platform_run_dir_listed_by_name run=%s/%s/runs/%s slug=%s — more "
                "than one directory here answers to that slug, so the candidate "
                "list offers this one by its directory name; that is also the name "
                "to track it under, since resolving the slug refuses to guess "
                "between them",
                host, project, entry.name, claim,
            )
            yield RunDirRef(host=host, project=project, slug=entry.name, path=entry)
            continue
        yield RunDirRef(
            host=host, project=project, slug=claim, path=entry, dir_name=entry.name
        )


def _renamed_run_dir(
    root: Path, host: str, project: str, slug: str
) -> Optional[RunDirRef]:
    """The name-bearing directory this run was renamed into, if exactly one says so.

    The container takes a single rename to ``runs/<slug>--<name>/`` when a run is
    named before execution (``work_repo.run_state.freeze_run_dir``), so the
    address the platform derived from the slug stops existing while the run itself
    is perfectly healthy. This finds it again by the container's own grammar — and
    then *confirms by content*, which is the part that makes it safe: a candidate
    is adopted only if its state file records this exact slug, so a differently
    named run that merely shares a prefix is never mistaken for this one.

    Two confirmed candidates are a refusal, not a coin toss, and this is not a
    theoretical branch: a slug is only as unique as the target it was derived from,
    so a taskdef run with **no** target gets the bare taskdef as its slug
    (``run_state.derive_slug``) — the shared work repo holds two ``masterplan``
    runs whose state files both say ``slug: masterplan``. Content cannot separate
    them because both claims are true, and resolving a run's whole record to the
    wrong one of two is worse than reporting it as unpushed. So it refuses, says so
    once, and names the way through: such a run is addressed by its directory name,
    which needs no fallback (adopt it as ``<slug>--<name>``).

    Only a plain directory name is looked up this way, because the slug becomes
    half of a **glob pattern** here rather than a path segment. The bare address
    above is containment-checked and can afford a strange slug; a pattern is a
    different hazard — a hand-edited or corrupted index entry carrying ``**``
    makes pathlib refuse the pattern outright (``ValueError``, from a read path
    that must not raise), and one carrying a separator or a wildcard turns a
    single directory lookup into a search. No run's recorded slug contains any of
    them: ``run_state.derive_slug`` sanitizes the target it is built from.
    """
    if slug in (".", "..") or any(char in slug for char in _NOT_IN_A_DIR_NAME):
        return None
    base = root / host / project / "runs"
    try:
        names = sorted(
            entry.name for entry in base.glob(f"{slug}--*") if entry.is_dir()
        )
    except OSError:
        return None
    found = [
        ref
        for ref in (_ref_at(root, host, project, slug, name) for name in names)
        if ref is not None and _recorded_slug(ref.path) == slug
    ]
    if not found:
        return None
    if len(found) > 1:
        _announce_once(
            f"ambiguous:{host}/{project}/{slug}",
            logging.WARNING,
            "platform_run_dir_ambiguous run=%s/%s/runs/%s candidates=%s — this "
            "slug names more than one run (a taskdef run with no target is slugged "
            "after the taskdef alone), and a run's whole record is not worth "
            "guessing at; track the one you mean by its directory name instead",
            host, project, slug, ", ".join(ref.dir_name or "" for ref in found),
        )
        return None
    ref = found[0]
    _announce_once(
        f"settled:{host}/{project}/{slug}->{ref.dir_name}",
        logging.INFO,
        "platform_run_dir_settled run=%s/%s/runs/%s dir=%s — the run was named, "
        "so the container renamed its directory (runs/%s -> runs/%s); the "
        "platform reads it there and keeps %s as the run's identity",
        host, project, slug, ref.dir_name, slug, ref.dir_name, slug,
    )
    return ref


def _build_reslugged_index(root: Path, host: str,
                           project: str) -> dict[str, str]:
    """Map each vacated slug to its newest live successor directory."""
    try:
        base = (root / host / project / "runs").resolve()
        base.relative_to(root)
        children = sorted(base.iterdir())
    except (OSError, RuntimeError, ValueError):
        return {}

    matches: dict[str, tuple[str, str]] = {}
    for child in children:
        if (not child.is_dir() or child.name.startswith(".")
                or child.name == run_state.ARCHIVE_DIR):
            continue
        state = _read_run_state(child)
        if state is None or state.get("status") != "in-progress":
            continue
        rank = (str(state.get("created") or ""), child.name)
        for vacated in run_state.vacated_slugs(state):
            if rank > matches.get(vacated, ("", "")):
                matches[vacated] = rank
    return {slug: rank[1] for slug, rank in matches.items()}


def _reslugged_run_dir(
    config: PlatformConfig, root: Path, host: str, project: str, slug: str
) -> Optional[RunDirRef]:
    """The current live run that records *slug* as a vacated identity.

    Release runs change from their target-derived seed slug to a version-bearing
    slug after discovering the version. The container records that transition in
    ``reslugged_from``; following that explicit history is safer than re-deriving
    aliases, which can collide with legacy or truncated identities.

    Only in-progress successors qualify. Terminal releases deliberately release
    the seed address for a later run. The project is scanned once per mirrored
    HEAD and indexed by every vacated slug, so fleet polling does not repeat the
    YAML walk for each unresolved tracked row. A tie should be prevented by
    release single-flight, but resolves exactly like the container: newest
    ``created``, then directory name.
    """
    revision = mirror_status(config).head_sha
    cache_key = (str(root), host, project)
    with _RESLUGGED_INDEX_LOCK:
        cached = _RESLUGGED_INDEX.get(cache_key)
        if revision is not None and cached is not None and cached[0] == revision:
            index = cached[1]
        else:
            index = _build_reslugged_index(root, host, project)
            if revision is not None:
                _RESLUGGED_INDEX[cache_key] = (revision, index)

    dir_name = index.get(slug)
    if dir_name is None:
        return None
    ref = _ref_at(root, host, project, slug, dir_name)
    if ref is None:
        return None
    _announce_once(
        f"reslugged:{host}/{project}/{slug}->{ref.dir_name}",
        logging.INFO,
        "platform_run_dir_reslugged run=%s/%s/runs/%s dir=%s — the live run "
        "records the requested slug in reslugged_from, so the platform follows "
        "that carried identity",
        host, project, slug, ref.dir_name,
    )
    return ref


def resolve_run_dir(
    config: PlatformConfig, host: str, project: str, slug: str
) -> Optional[RunDirRef]:
    """Locate one tracked run's directory in the mirror, or ``None``.

    Direct path lookup handles the ordinary case. Once scope comes from the local
    index (spec D25) the platform knows exactly which paths it wants; the one
    carried-identity fallback that must inspect sibling state builds one index per
    mirror revision rather than scaling a YAML walk by the operator's tracked-row
    count.

    ``runs/<slug>`` first, then the name-bearing directory the container renamed
    the run into (:func:`_renamed_run_dir`), and finally the live successor that
    explicitly records the requested identity in ``reslugged_from``. The second
    lookup is the fix
    for a bug found in the field (T90): a run tracked as ``review-mr-172`` whose
    directory was ``review-mr-172--review-mr-172`` read back as *no run state at
    all* — null phase and status, no files, a fleet row that was raw session
    liveness wearing a run's clothes, and a row that a taskdef marking itself
    complete could never have flipped. It looked like a doubled slug and was not:
    the run was simply *named*, and the platform was addressing runs by a
    directory name that a named run does not have.

    Nothing here consults a version, and a run with no renamed directory resolves
    exactly as it did before — including to ``None``, which means the run's dir is
    not in the mirror at all: either it has not been pushed yet (a freshly spawned
    run) or the mirror is stale. Both are states the inventory renders rather than
    errors.

    What this deliberately does *not* do is re-key the run. The slug stays the
    identity — it is what the container records in ``state.slug``, what
    ``run_state.derive_slug`` reproduces from the taskdef and target, and
    therefore what the next session for this run registers under
    (:func:`lmer_platform.spawn.derive_run_identity`) — so everything keyed on it
    keeps working untouched: the tracked index, this orchestrator's titles
    (:mod:`lmer_platform.meta`), related runs (:mod:`lmer_platform.relations`),
    ``answer``, ``resume`` and ``forget``. Only the *address* was ever wrong.
    """
    if not (host and project and slug):
        return None
    root = config.mirror_path.resolve()
    ref = _ref_at(root, host, project, slug, slug)
    if ref is not None:
        return ref
    renamed = _renamed_run_dir(root, host, project, slug)
    if renamed is not None:
        return renamed
    return _reslugged_run_dir(config, root, host, project, slug)
