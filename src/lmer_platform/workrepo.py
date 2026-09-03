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

Two tree shapes, one address model (issue #353)
-----------------------------------------------
The work repo holds two shapes at once, deliberately and indefinitely (D16/D20):

- **legacy** — the shared ``{host}/{project}/runs/<slug>/`` tree, read in place
  and never restructured;
- **schema form** — ``<project>/_runs/<slug>/<session-id>[--<name>]/``, where the
  SESSION is the unit of record (D22) and ``_runs/<slug>/`` is a grouping level
  nothing is written at. ``<project>`` is the registered SHORT NAME, a flat
  top-level directory with **no host segment** (D7), or — for a repo nobody has
  named yet — the ``{host}/{project}/`` host bucket, whose project begins at the
  first project-shaped directory at whatever depth it sits (D9; there is no
  depth rule anywhere, the marker test decides). See ``docs/SCHEMA.md``.

Those two shapes cannot both be composed from ``(host, project, slug)``, so
:attr:`RunDirRef.rel_path` stops being a composition and becomes **the address
the discovering code recorded**: option (a) of the design note, an explicit
mirror-relative :attr:`RunDirRef.address` field, chosen over a shape enum plus a
grouping-level field because every other module in this subsystem consumes
``rel_path`` as the one honest address — a shape enum would make each of them
re-derive the string, and the derivation is exactly what keeps being wrong. The
walk sets it to the path it actually found, so the address cannot drift from the
directory. A ref built with no address falls back to the legacy composition,
which is precisely the legacy shape and nothing else: any discoverer of any
other shape MUST set the address.

**Identity is never the path in schema form.** ``widgets/`` says nothing about
which repo it holds, so the run key of a flat project dir comes from the
registry entry's ``repo:`` (``lmer_schema.registry``, normalized by the ONE
normalizer per D21), and falls back to the identity the run's own record carries.
A host bucket keeps path-derived identity because the bucket path *is* derived
from the remote (D9), which is what keeps ``run_key`` stable across both shapes.
A flat project whose identity cannot be recovered is skipped and said once, for
the reason :func:`_listed_refs` gives: a candidate offered under a key nothing
can resolve is worse than one the operator is told about.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import logging
import os
import re
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

# The schema side of the tree, consumed rather than re-derived: `load_registry`
# owns what a registry entry means and `urls.normalize` is the one remote
# normalizer (D21).
from lmer_schema.manifest import ManifestError
from lmer_schema.registry import load_registry
from lmer_schema.urls import normalize

from .config import PlatformConfig
from .store import (
    StoreError, mutating, read_json, snapshot_path, utc_now_iso, write_json,
)

logger = logging.getLogger("lmer_platform.workrepo")

__all__ = [
    "MIRROR_STATE_FILE", "RunDirRef", "MirrorStatus", "mirror_status",
    "ensure_clone", "pull", "iter_run_dirs", "run_dirs", "record_session_id",
    "folded_identity", "fold_registry",
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
#: The legacy shared run tree and the schema-form record area (D22). A directory
#: with either name is where the walk stops descending and starts listing.
_LEGACY_RUNS_DIR = "runs"
_SCHEMA_RUNS_DIR = "_runs"
#: Where a retired record goes (D13). The legacy equivalent, ``runs/archive/``,
#: is already excluded by never descending past a ``runs/`` — archived sessions
#: are not runs the fleet offers, in either shape.
_ARCHIVE_DIR = "_archive"
#: Facts about run directories this process has already said out loud, so a read
#: path the UI polls says each of them once — see :func:`_announce_once`.
_ANNOUNCED: set = set()
# One scan per mirrored revision and project for carried reslug identities. The
# platform polls tracked rows individually; without this index every missing
# direct slug re-read every state file in the project.
_RESLUGGED_INDEX: dict[tuple[str, str, str], tuple[str, dict[str, str]]] = {}
_RESLUGGED_INDEX_LOCK = threading.Lock()
#: A session DIRECTORY name: the ``LMER_SESSION_ID`` form (a UTC
#: ``%Y%m%d-%H%M%S`` timestamp, a pid and a random suffix) plus the optional
#: ``--<name>`` the freeze rename appends once (``docs/SCHEMA.md``, "Sessions
#: and runs"). Restated here rather than imported: the pattern's owner is
#: ``lmer_schema.checks.record.SESSION_DIR_RE``, but that module is the
#: VALIDATOR — this one depends on the schema's model side only (``manifest``,
#: ``registry``, ``urls``), and a fleet read path has no business importing the
#: check registry to sort two directory names. Both quote ``docs/SCHEMA.md``;
#: keep them in step there.
_SESSION_DIR_RE = re.compile(r"\d{8}-\d{6}-\d+-\d+(?:--[^/]+)?")
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
    #: The mirror-relative path this ref was found at, recorded by whatever found
    #: it. ``None`` only for a ref built without one, whose address is then the
    #: legacy composition — see :attr:`rel_path` and the module header.
    address: Optional[str] = None

    @property
    def rel_path(self) -> str:
        """Work-repo-relative path, e.g. ``gitlab.example.com/group/project/runs/foo``.

        The *address*, so a caller that renders a forge link or quotes a path in
        an error names the directory a human can actually open — which for a named
        run is ``runs/<slug>--<name>``, not ``runs/<slug>``, and in schema form is
        the session dir ``<project>/_runs/<slug>/<session-id>`` (D22).

        :attr:`address` is that answer when the discovering code recorded one,
        and it always does (the module header says why the address is carried
        rather than composed). The fallback composes the LEGACY shape and only
        that shape: it is what a ref built from an identity alone can honestly
        say, and it is exactly where a legacy run lives.
        """
        if self.address:
            return self.address
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

    Both tree shapes are walked (module header): a directory named ``runs`` holds
    legacy run dirs directly, and one named ``_runs`` holds ``<slug>/`` GROUPING
    levels whose session children are the records (D22). Nothing is written at the
    grouping level, so a directory under ``_runs/`` is a run only through its
    sessions — and a session dir is a complete record, never descended into
    hunting for a nested run root.

    A directory only counts as a run if it holds a run-state file, which keeps
    stray directories under a ``runs/`` or ``_runs/<slug>/`` parent out of the
    inventory. Seed directories (``runs/.new-<session-id>-*`` and its schema-form
    twin at the ``_runs/`` level) and every other dot-directory are skipped as the
    transient tooling state they are — a dir mid-rename is nobody's candidate —
    and the archive is skipped in both shapes: ``_archive/`` is not walked at all
    and legacy ``runs/archive/`` is not listed, because an archived session is a
    retired record (D13), not a run the fleet offers.

    Each run is keyed by the slug its state file records, not by the directory it
    sits in — see :func:`_listed_refs` for why the difference matters to the one
    thing this feeds, the adoption picker, and :func:`_grouped_refs` for what
    that means when one slug groups several independent sessions.
    """
    mirror = config.mirror_path
    if not mirror.is_dir():
        return
    identities = _RegistryIdentities(mirror)

    for dirpath, dirnames, _filenames in os.walk(mirror):
        dirnames[:] = [
            d for d in dirnames if d not in _PRUNED_DIRS and d != _ARCHIVE_DIR
        ]
        current = Path(dirpath)
        if current.name not in (_LEGACY_RUNS_DIR, _SCHEMA_RUNS_DIR):
            continue

        # Do not descend into run dirs (or session dirs) hunting for a nested
        # `runs`/`_runs`: this level lists its own children and stops.
        dirnames[:] = []
        try:
            parts = current.parent.relative_to(mirror).parts
        except ValueError:  # pragma: no cover - os.walk stays under mirror
            continue

        if current.name == _SCHEMA_RUNS_DIR:
            groups = _session_groups(current)
            if not groups:
                continue
            identity = _project_identity(
                parts, identities, lambda: _read_run_state(groups[0][1])
            )
            if identity is None:
                continue
            yield from _grouped_refs(mirror, *identity, groups)
            continue

        entries = [
            entry
            for entry in sorted(current.iterdir())
            if entry.is_dir()
            and not entry.name.startswith(".")
            and entry.name != run_state.ARCHIVE_DIR
            and _holds_run_state(entry)
        ]
        if not entries:
            continue
        identity = _project_identity(
            parts, identities, lambda: _read_run_state(entries[0])
        )
        if identity is None:
            continue
        yield from _listed_refs(mirror, *identity, entries)


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


def record_session_id(ref: RunDirRef) -> Optional[str]:
    """The session id the record at *ref* is addressed by, or ``None``.

    In schema form the SESSION is the unit of record (D22): a launch owns
    ``<project>/_runs/<slug>/<session-id>[--<name>]/`` and ``LMER_SESSION_ID`` is
    what decides which of them a container writes in — minted at container start
    only when the host injected none (``clone_and_exec.main`` ->
    ``mint_session_id``). So a verb that continues a *particular* record
    (:mod:`lmer_platform.resume`, :mod:`lmer_platform.answer`) has to be able to
    name the launch it resolved, and hand that name to the spawn.

    It lives here because both rules the answer needs already do, and a second
    opinion about either is how a resume would come to inject an id that
    addresses a different directory than the one it read:

    - **is this a session record at all**: the structural test the layout
      supplies, that the record's grandparent is the record area
      (:data:`_SCHEMA_RUNS_DIR`) — the same shape :func:`_session_groups` walks.
      A legacy ``{host}/{project}/runs/<slug>/`` dir is one shared record per
      target with no launches under it, and a tree adopted before its runs were
      migrated holds its record at ``_runs/<slug>/`` itself
      (``work_repo.run_state._creation_address``). Both are addressed by their
      slug rather than by a launch, and both answer ``None`` — which is what
      keeps a *slug* from travelling as a session id.
    - **which half of the directory name is the id**: ``run_state.session_id_of``,
      reused rather than restated, because ``<sid>`` and ``<sid>--<name>`` are
      the same session everywhere in that module and the id is ``<sid>``. The
      name-bearing dir would work today by accident and would compose
      ``<sid>--<name>--<name>`` the moment anything built the record's address
      back out of what it was handed.

    A directory whose name is not in the ``LMER_SESSION_ID`` grammar is answered
    with anyway: it is still the name that addresses that record, and an
    out-of-form name is a shape this module MEETS rather than assumes away —
    :func:`_newest_session` deliberately keeps such a group addressable rather
    than dropping the run from the fleet view, so refusing to name it here would
    leave the operator unable to continue the record they are being shown.
    """
    if ref.path.parent.parent.name != _SCHEMA_RUNS_DIR:
        return None
    return run_state.session_id_of(ref.path.name)


def _holds_run_state(path: Path) -> bool:
    """The one rule for "this directory is a run's record": it has a state file."""
    return any((path / name).is_file() for name in _STATE_FILENAMES)


def _address(root: Path, path: Path) -> Optional[str]:
    """*path* as a mirror-relative address, or ``None`` if it is not under *root*.

    The honest address is the path that was found, which is the whole reason
    :attr:`RunDirRef.address` exists rather than a composition (module header).
    """
    try:
        return path.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover - callers walk from root
        return None


def _checked_dir(root: Path, path: Path) -> Optional[Path]:
    """*path* resolved and confirmed to be a directory inside *root*, else ``None``.

    The one containment rule of the resolve path, in one place: resolve first,
    then re-check that what came back is still under the mirror. Resolving first
    is what refuses a directory *symlinked* out of the mirror (as in
    :func:`lmer_platform.api._safe_asset`), and re-checking is what refuses a
    ``..`` that reached this composition through an index entry rather than a
    request (:func:`lmer_platform.answer._reject_traversal` never saw it).

    ``RuntimeError`` as well as ``OSError``: on 3.12 a symlink loop makes
    ``resolve()`` raise it, and letting that through would turn a refusal into a
    traceback on a read path the UI polls.
    """
    try:
        candidate = path.resolve()
        candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate if candidate.is_dir() else None


def _sessions_in(group: Path) -> list[Path]:
    """The record-holding session dirs of one ``_runs/<slug>/`` group, oldest first.

    THE definition of "which sessions are records, and in what order", used by
    both halves of this module on purpose. ``_runs/<slug>/`` is a grouping folder
    that holds nothing itself (D22), so its records are the children carrying a
    state file; dot-directories are the transient seed of a session mid-rename
    (``_runs/.new-<session-id>-*`` (the run area, not the group)) and never a record.

    Session ids are timestamp-first, so ``ls`` order is start order
    (``docs/SCHEMA.md``) and the last entry is the newest launch — which is the
    one both :func:`_session_groups` and :func:`_session_run_dir` address. If the
    picker and the resolver each had their own rule here, the candidate list and
    the fleet row could quote different directories of one run.
    """
    try:
        entries = sorted(group.iterdir())
    except OSError:
        return []
    return [
        session
        for session in entries
        if session.is_dir()
        and not session.name.startswith(".")
        and _holds_run_state(session)
    ]


def _newest_session(group: Path) -> Path | None:
    """THE launch a ``_runs/<slug>/`` group is addressed at, or ``None``.

    The picker (:func:`_session_groups`) and the resolver (:func:`_session_run_dir`,
    :func:`_record_dirs`) both come here, so a run's candidate row and its fleet
    row can never quote different directories of one group.

    The choice is the newest session **among the directories named in the
    session-id form**, by name: those names are timestamp-first, so ``ls`` order
    is start order (``docs/SCHEMA.md``, "Sessions and runs").

    Restricting the comparison to that form is the whole point. Name order is
    start order only while every state-bearing child is in it, and nothing here
    enforces that — the schema registers the non-conforming case as a WARNING
    (``record.session-id-malformed``), so it is a shape this walk MEETS rather
    than one it may assume away. A hand-made ``backup-before-i-broke-it/`` sorts
    after ``2026…`` and used to win its group, so the ref addressed the copy and
    the fleet row read ``complete`` for a run that was still going: a live run
    gone from the operator's view with no error and nothing said.

    **A group with no in-form child at all is still addressed**, at the newest
    directory MTIME (ties broken by name, so the answer is stable), with a
    warning that the address is a guess. Yielding nothing would drop the run
    from the fleet view and the adoption picker entirely — strictly worse than
    the defect above, since an unlisted run cannot even be looked at. Mtime and
    not the state file's own ``updated`` because this walk runs per poll over
    every group in the mirror: ``st_mtime`` is a stat, where ``updated`` is a
    YAML read per session — the cost :func:`_recorded_slug` makes once, and only
    for the session that won.
    """
    sessions = _sessions_in(group)
    if not sessions:
        return None
    in_form = [
        session for session in sessions if _SESSION_DIR_RE.fullmatch(session.name)
    ]
    if in_form:
        if len(in_form) != len(sessions):
            _announce_once(
                f"session-not-in-form:{group}",
                logging.INFO,
                "platform_session_dir_not_in_form group=%s ignored=%s chosen=%s — "
                "a session directory is named <session-id> and those sort into "
                "start order; a directory that is not is no part of that order, so "
                "it is passed over rather than taken as the newest launch",
                group.name,
                ", ".join(
                    session.name for session in sessions if session not in in_form
                ),
                in_form[-1].name,
            )
        return in_form[-1]
    newest = max(sessions, key=lambda session: (_mtime(session), session.name))
    _announce_once(
        f"session-undated:{group}",
        logging.WARNING,
        "platform_session_dir_undated group=%s candidates=%s chosen=%s — no "
        "directory here is named <session-id>, so their start order is not "
        "recorded anywhere; the run is listed at the most recently modified one "
        "rather than dropped from the fleet view, but that address is a guess",
        group.name,
        ", ".join(session.name for session in sessions),
        newest.name,
    )
    return newest


def _session_groups(runs_dir: Path) -> list:
    """``[(grouping dir, session dir)]`` for one ``_runs/``, newest session each.

    ``_runs/<slug>/`` groups every launch against one (taskdef, target) and holds
    nothing itself (D22), so the records are its session children — and the ones
    that are records are exactly the ones with a state file.

    One pair per grouping dir, not per session, because the listing this feeds is
    the adoption picker and adoption keys on ``host/project/slug``: several
    sessions of one slug are several records of ONE run identity, and offering
    that key once per session would offer the same adoption several times — the
    defect :func:`_listed_refs` already refuses in the legacy tree.

    The pair kept is the NEWEST session **among the directories named in the
    session-id form**, by name: those names are timestamp-first, so ``ls`` order
    is start order (``docs/SCHEMA.md``, "Sessions and runs"), which makes the
    address the launch an operator is most likely to be looking at and makes the
    choice deterministic without reading a state file per session.

    Restricting the comparison to that form is the whole point. Name order is
    start order only while every state-bearing child is in it, and nothing here
    enforces that: the schema registers the non-conforming case as a WARNING
    (``record.session-id-malformed``), so it is a shape this walk MEETS rather
    than one it may assume away. A hand-made ``backup-before-i-broke-it/`` — a
    ``cp -r`` before an edit, a partly restored dir, a rename that did not follow
    the freeze convention — sorts after ``2026…`` and used to win its group, so
    the one ref yielded addressed the copy and the fleet row read ``complete``
    for a run that was still going: a live run gone from the operator's view with
    no error and nothing said. Out-of-form children are therefore passed over,
    and passing one over is said once.

    **A group with no in-form child at all still yields a pair**, chosen by
    newest directory MTIME (ties broken by name, so the answer is stable). That
    is a stated fallback and not an ordering the tree promised — hence the
    warning — but the alternative is yielding nothing, which drops the run from
    the fleet view and the adoption picker entirely: strictly worse than the
    defect above, since an unlisted run cannot even be looked at. Mtime and not
    the state file's own ``updated`` stamp because this walk runs per poll over
    every group in the mirror: ``st_mtime`` is the stat the listing already
    needs to make, where ``updated`` is a YAML read per session — the cost
    :func:`_recorded_slug` is careful to make once, and only for the session
    that won.
    """
    groups = []
    for group in sorted(runs_dir.iterdir()):
        if not group.is_dir() or group.name.startswith("."):
            continue
        chosen = _newest_session(group)
        if chosen is not None:
            groups.append((group, chosen))
    return groups


def _mtime(path: Path) -> float:
    """Modification time for ordering, or ``0.0`` when it cannot be read.

    A stat and never a read: :func:`_session_groups` says why the state file's
    ``updated`` stamp is not what orders sessions. A path that vanished under the
    walk sorts oldest, which is the harmless direction — the caller is choosing
    between siblings, not deciding whether one exists.
    """
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


class _RegistryIdentities:
    """``short name -> (host, project)`` from the plexus registry, read once.

    The registry is the identity of a flat project dir (D7/D8): the directory
    name is an address and says nothing about which repo it holds. Loaded lazily,
    because a mirror with no schema-form project never needs it, and at most once
    per walk, because the walk meets one project dir per project.

    Fail-soft, like every other read on this path: a tree with no manifest is
    legacy (``load_registry`` returns ``None``) and a broken one is the
    validator's problem, not the fleet view's — either way this answers "no
    identities" and the runs whose identity that file held are skipped and said
    once, rather than the whole listing raising.

    Two maps out of one registry read, and they are NOT each other's inverse:
    the forward direction (:meth:`get`, what the walk keys a listed run on) is
    ``repo:`` alone, the backward direction (:meth:`names_for`, how the resolver
    finds a directory) is the registry's UNION of ``repo:`` + ``remotes:``. That
    asymmetry is deliberate and it is not the whole answer; :meth:`names_for`
    states what it fixes, what it leaves standing, and where the whole answer
    would land.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._identities: Optional[dict] = None
        self._names: Optional[dict] = None

    def get(self, name: str) -> Optional[tuple]:
        self._read()
        return self._identities.get(name)

    def names_for(self, identity: tuple) -> list:
        """The short names whose project answers to *identity*, by ANY remote.

        The walk meets a directory and asks what it is; the resolver is handed an
        identity and has to find the directory (:func:`_project_dirs`). This is
        that second question, and it is answered across the registry's UNION —
        ``repo:`` and every ``remotes:`` entry — because **that is what the
        writers do**: ``work_repo.utils._schema_layout`` (through
        ``resolve_project_dir``) and :func:`lmer_platform.api._plexus_project_dir`
        (through ``Registry.lookup``) both match a launch URL against the union,
        so a project reached through a secondary remote writes its record under
        the same short name. A resolver inverting ``repo:`` alone reported that
        record ABSENT — the fleet row with no state at all that T90 fixed once in
        the legacy tree, in its schema-form form. ``remotes:`` is exactly what
        multi-remote projects exist for (D8).

        **The walk's keying is unchanged, and it is no longer half an answer.**
        The LISTED identity is still the project's ``repo:``
        (:func:`_project_identity`); what moved is the other end, where a launch
        URL is folded to that same primary before the run is ever tracked
        (:func:`primary_remote_identity`, which carries the argument). One
        directory therefore answers to one identity key by construction, with no
        union lookup on the write path either.

        This direction stays for what the fold does not reach: a row tracked by a
        build that predates it, a run adopted by hand under a secondary's
        spelling, and any caller deriving an identity with no mirror to read —
        all of which arrive here as an identity that is a real remote of the
        project and must still find its directory.

        A list, and no tiebreak: ``build_registry`` refuses one remote claimed by
        two entries (it names both projects), so a lookup URL matches at most one
        entry and the ordinary answer here is one name or none. Two names can
        only meet in one identity when their remotes differ solely by PORT, which
        the registry keeps as distinct keys while a host DIRECTORY cannot express
        it (:func:`_identity_from_repo` drops the port). Both are then offered,
        in name order so the attempt order is deterministic, and the first that
        actually holds the record answers — guessing between two projects nothing
        can separate is not an improvement on trying both.
        """
        self._read()
        return self._names.get(identity, [])

    def _read(self) -> None:
        """Both maps, from one registry read, at most once per instance."""
        if self._identities is None:
            self._identities, self._names = self._load()

    def _load(self) -> tuple:
        try:
            registry = load_registry(self._root)
        except ManifestError as exc:
            _announce_once(
                f"manifest-unusable:{self._root}",
                logging.WARNING,
                "platform_mirror_manifest_unusable path=%s error=%s — the "
                "registry cannot say which repo a schema-form project dir "
                "belongs to, so those runs are not listed until it parses",
                self._root, exc,
            )
            return {}, {}
        except OSError as exc:  # pragma: no cover - load_manifest wraps these
            logger.warning("platform_mirror_manifest_unreadable error=%s", exc)
            return {}, {}
        if registry is None:
            return {}, {}
        identities: dict = {}
        names: dict = {}
        for name, project in registry.projects.items():
            identity = _identity_from_repo(project.repo)
            if identity is not None:
                identities[name] = identity
            # The union, primary first, exactly as `entry_keys` builds the
            # registry's own index — a repoless entry (D19) declares no remote
            # and so answers to no identity here, only to its name.
            for remote in (project.repo, *project.remotes):
                bound = _identity_from_repo(remote)
                if bound is None:
                    continue
                found = names.setdefault(bound, [])
                if name not in found:
                    found.append(name)
        return identities, {
            bound: sorted(found) for bound, found in names.items()
        }


def _identity_from_repo(repo: Optional[str]) -> Optional[tuple]:
    """``(host, project)`` for a remote URL, or ``None`` when it has no host.

    ``normalize`` is the ONE remote normalizer (D21, via ``lmer_schema.urls``);
    the port is then dropped from the host because a host DIRECTORY never carries
    one — the port participates in registry keys only, and every
    ``{host}/{project}`` path lmer has ever written came from
    ``urlparse(...).hostname`` (``lmer_schema.registry.bucket_path`` states the
    same rule for bucket paths). That rule is not taken from ``bucket_path``
    itself because this is an IDENTITY, not a bucket: a dotless host is a real
    run key (``localhost`` is a real ``LMER_REPO_HOST``) where it is not a bucket
    the schema supports.
    """
    spelling = normalize(repo)
    host, _, project = spelling.partition("/")
    host = host.partition(":")[0]
    if not host or not project:
        return None
    return host, project


#: "no registry was handed in", distinct from ``None`` which is the real answer
#: "there is no registry here" (a legacy tree, an uncloned mirror).
_UNREAD = object()


def fold_registry(config: Optional[PlatformConfig]):
    """The registry the fold reads, resolved ONCE — or ``None`` when there is
    none to read.

    For a caller that folds MANY identities in one pass. Without it each fold
    re-opens and re-parses ``_plexus.yaml``: the candidates listing folds both
    the tracked rows and the walked directories, so ten of each meant twenty-odd
    manifest reads to answer one question about a file that did not change
    between them. The same shape :class:`_RegistryIdentities` already uses on the
    walk — read once, ask many times — and for the same reason.

    Fail-soft like everything else on this path: an unreadable or unusable
    manifest answers ``None``, which folds nothing and changes no identity.
    """
    if config is None:
        return None
    try:
        mirror = config.mirror_path
        if not mirror.is_dir():
            return None
        return load_registry(mirror)
    except ManifestError as exc:
        _announce_once(
            f"fold-manifest-unusable:{config.mirror_path}",
            logging.WARNING,
            "platform_identity_fold_manifest_unusable path=%s error=%s — the "
            "registry cannot say which project a launch URL belongs to, so runs "
            "keep the identity their launch URL parses to",
            config.mirror_path, exc,
        )
        return None
    except Exception as exc:  # best-effort: a cold mirror costs the fold, not the run
        logger.warning("platform_identity_fold_failed error=%s", exc)
        return None


def folded_identity(
    config: Optional[PlatformConfig],
    host: Optional[str],
    project: Optional[str],
    registry=_UNREAD,
) -> tuple:
    """The identity key *(host, project)* belongs to, folded to the project's
    PRIMARY remote — or the pair unchanged when the registry does not claim it.

    :func:`primary_remote_identity` folds a URL, which is what the spawn has.
    This folds an identity PAIR, which is what an already-tracked row has, and
    the two exist together because a comparison between them has to fold BOTH
    sides or it can never agree.

    Folding only the URL was right for every row keyed on the primary and a dead
    end for one keyed on a secondary: the tracked side stayed at the secondary
    while the derived side moved to the primary, so
    :func:`lmer_platform.resume._repo_url_for` refused the resume — and refused
    it by telling the operator their own repo URL named a different project,
    advice no URL could satisfy, since every spelling of that project folds to
    the same primary. The adoption picker read the same disagreement as "this
    directory is untracked" and invited a second row for it, which is the defect
    the fold exists to close, reappearing one level up.

    Such a row needs no old build to exist: ``POST /api/runs/adopt`` takes host
    and project verbatim with no registry validation, and the secondary is the
    URL an operator actually cloned from — so it is the spelling they would type.
    A spawn whose fold no-opped because the mirror was momentarily unreadable
    leaves one too; the fold is best-effort by design, and this is the cost of
    that being true.

    The pair is spelled as a URL for the lookup because the registry is keyed on
    URLs and one normalizer owns that (D21). Anything the registry does not claim
    comes back exactly as it went in, so a legacy tree, a cold mirror and an
    unregistered project are all "no change" rather than "no answer" — this
    returns a pair, never ``None``, because its callers are comparing and a
    ``None`` there would read as a third identity.
    """
    if not host or not project:
        return host, project
    folded = primary_remote_identity(
        config, f"https://{host}/{project}", registry=registry
    )
    return folded if folded is not None else (host, project)


def primary_remote_identity(
    config: Optional[PlatformConfig], repo_url: Optional[str], registry=_UNREAD
) -> Optional[tuple]:
    """``(host, project)`` of the registered project *repo_url* reaches, keyed on
    that project's PRIMARY remote — or ``None`` when nothing here can say.

    The fold that keeps one run directory answering to ONE identity key, and the
    other half of :meth:`_RegistryIdentities.names_for`. A project with
    ``remotes:`` is one project at several URLs (D8) and every WRITER places its
    record across the union — ``Registry.lookup``, which
    :func:`lmer_platform.api._plexus_project_dir` and the work CLI's
    ``resolve_project_dir`` both go through — so a launch through a secondary
    remote writes under the same project as one through the primary. The walk
    that LISTS that directory keys it on the project's ``repo:``
    (:class:`_RegistryIdentities`), so an identity taken from the launch URL
    verbatim gave the one directory two keys: the adoption picker offered a run
    this orchestrator had spawned itself, adopting it wrote a SECOND index entry
    for that directory, and :func:`lmer_platform.spawn._refuse_if_run_is_live`
    did not see two launches through the two remotes as one run.
    :func:`lmer_platform.spawn.derive_run_identity` folds through here, so both
    halves agree by construction rather than by a union lookup on every read.

    ``repo:`` goes through :func:`_identity_from_repo` and never through
    ``cli._parse_repo_url``: the registry stores the canonical scheme-less
    spelling (D21) and that parser reads ``(None, None)`` out of it, which would
    not narrow this defect but replace it with a worse one — a spawn with no
    identity is not tracked at all (``spawn._untracked_run_warning``), so its row
    leaves the fleet view the moment the session exits.

    **Best-effort, and never a refusal.** No mirror configured, a mirror not
    cloned yet, a legacy tree with no manifest, a manifest this build cannot
    read, and a URL no entry claims all answer ``None``, and the caller keeps the
    identity it would have derived without this. Deriving an identity is on the
    spawn path, and a spawn that failed because the mirror was cold would be a
    worse failure than the two keys this closes: the run would not start at all,
    where today it starts and is merely keyed twice. The manifest is read from
    the mirror as it stands, with no pull — the fleet poll keeps it current, and
    a stale one costs a fold, not a spawn.
    """
    if config is None or not repo_url:
        return None
    try:
        if registry is _UNREAD:
            mirror = config.mirror_path
            # `is_dir` rather than `has_manifest`: an uncloned mirror is the
            # ordinary state of a fresh daemon, and it is not news.
            if not mirror.is_dir():
                return None
            registry = load_registry(mirror)
        if registry is None:
            return None
        # The union of `repo:` + `remotes:`, which is the same lookup the
        # container's placement makes — a second matching rule here would fold
        # runs into a project the record never lands in.
        project = registry.lookup(repo_url)
        return None if project is None else _identity_from_repo(project.repo)
    except ManifestError as exc:
        _announce_once(
            f"fold-manifest-unusable:{config.mirror_path}",
            logging.WARNING,
            "platform_identity_fold_manifest_unusable path=%s error=%s — the "
            "registry cannot say which project a launch URL belongs to, so runs "
            "are identified by the URL they were launched with until it parses",
            config.mirror_path, exc,
        )
        return None
    # Deliberately everything else, and the lookup is inside it: this is a
    # best-effort read on the SPAWN path, so a mirror that is a broken symlink or
    # a URL the normalizer trips over must cost the fold and never the session.
    except Exception as exc:
        logger.warning("platform_identity_fold_unreadable error=%r", exc)
        return None


def _identity_from_state(state: Optional[dict]) -> Optional[tuple]:
    """``(host, project)`` a run's own record carries, or ``None``.

    The fallback for a project dir with no registry entry: the record may name
    the repo it belongs to even when the manifest does not. Read tolerantly, in
    both spellings a record can use — a ``repo:`` remote (identity per D8) or the
    ``host``/``project`` the container routed by — and never from the path, which
    in schema form is a short name that means nothing outside this tree.
    """
    if not isinstance(state, dict):
        return None
    identity = _identity_from_repo(state.get("repo"))
    if identity is not None:
        return identity
    host, project = state.get("host"), state.get("project")
    if isinstance(host, str) and isinstance(project, str) and host and project:
        return host, project
    return None


def _project_identity(parts, identities: _RegistryIdentities, state_hint):
    """``(host, project)`` of the project dir a ``runs``/``_runs`` sits in.

    *parts* is that dir's mirror-relative path. Three answers, in the order the
    schema gives them:

    - **no parts** — a bare ``runs``/``_runs`` at the top of the repo belongs to
      no project;
    - **two or more** — the ``{host}/{project}`` tree: the legacy shared layout
      and the host bucket of an unnamed repo are the same path shape, and it is
      derived FROM the remote (D9), so reading identity back off it is honest and
      keeps ``run_key`` byte-identical to what it has always been. The project is
      every segment below the host, at any depth — a subgroup tree is deeper and
      no less a project (there is no depth rule, D9/§1);
    - **exactly one** — a flat short-name project dir (D7), whose name is an
      address and never an identity: the registry answers, then the run's own
      record (*state_hint*, called only if the registry cannot). Neither means
      the run is skipped and said once — nothing is derived from a directory
      name (D9).
    """
    if not parts:
        return None
    if len(parts) >= 2:
        return parts[0], "/".join(parts[1:])
    name = parts[0]
    identity = identities.get(name) or _identity_from_state(state_hint())
    if identity is None:
        _announce_once(
            f"unidentified:{name}",
            logging.INFO,
            "platform_run_dir_unidentified project=%s — no registry entry names "
            "this project dir and its runs record no repository of their own, so "
            "the platform cannot say which repo they belong to; adopt them by "
            "adding the project to the plexus manifest",
            name,
        )
    return identity


def _project_dirs(
    root: Path, host: str, project: str, identities: _RegistryIdentities
) -> Iterator[Path]:
    """Every directory in the mirror that could hold this identity's record.

    The inverse of :func:`_project_identity`, and the reason the resolve path
    needs the registry at all: the walk reads a directory and asks which repo it
    holds, while a *tracked* run arrives as an identity with no directory
    attached. Two shapes answer to one identity, in this order:

    - ``{host}/{project}/`` — the legacy shared tree AND the host bucket of an
      unnamed repo, which are the same path and are both *derived from the
      remote* (D9), so this needs nothing but the identity itself. Yielded first
      because it costs one ``stat`` and is where today's runs live;
    - ``<short-name>/`` — a registered flat project (D7), whose directory name
      says nothing about which repo it holds, so the registry is the only thing
      that can name it, and it names it by ANY of the entry's declared remotes
      (:meth:`_RegistryIdentities.names_for`, which says what that fixes and what
      it leaves standing). Lazily, because a generator stops here for every run
      the first shape already answered, and reading the manifest is then never
      on the common path.

    Nothing is derived from a directory name and no directory is searched for by
    content: an identity with no registry entry and no path-shaped tree simply
    has no candidate here, which is D9's "names are never derived" from the
    resolving end. Every candidate is containment-checked at use
    (:func:`_checked_dir`), so a short name that should never have passed the
    registry's charset rule cannot aim a lookup out of the mirror.
    """
    yield root / host / project
    for name in identities.names_for((host, project)):
        yield root / name


def _ref_at(
    root: Path, host: str, project: str, slug: str, dir_name: str
) -> Optional[RunDirRef]:
    """A ref for ``runs/<dir_name>`` under *root*, or ``None`` if that is not a run.

    The LEGACY composition, and the only ref built without an address: what it
    composes is exactly what :attr:`RunDirRef.rel_path` falls back to (module
    header). Every other shape is discovered rather than composed and carries the
    address it was found at — see :func:`_ref_for`.

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
    candidate = _checked_dir(
        root, root / host / project / _LEGACY_RUNS_DIR / dir_name
    )
    if candidate is None or not _holds_run_state(candidate):
        return None
    return RunDirRef(
        host=host,
        project=project,
        slug=slug,
        path=candidate,
        dir_name=None if dir_name == slug else dir_name,
    )


def _ref_for(
    root: Path, host: str, project: str, slug: str, record: Path
) -> Optional[RunDirRef]:
    """A ref for a record directory that was *found*, or ``None`` if it is not one.

    :func:`_ref_at`'s sibling for every shape the legacy composition cannot
    express — a session dir in schema form, and the carried-identity successor of
    either shape. The address is the path that was found rather than a
    composition, which is the whole point of :attr:`RunDirRef.address` (module
    header): a ref built here can be quoted, linked and opened, and cannot drift
    from the directory it names.

    The address and :attr:`RunDirRef.dir_name` are both read off the *resolved*
    path, so neither can disagree with :attr:`RunDirRef.path`. The
    containment re-check and the state-file test are :func:`_ref_at`'s, for
    :func:`_checked_dir`'s reasons — the record may be reached through an index
    entry or a symlink, and neither is trusted here.
    """
    candidate = _checked_dir(root, record)
    if candidate is None or not _holds_run_state(candidate):
        return None
    return RunDirRef(
        host=host,
        project=project,
        slug=slug,
        path=candidate,
        dir_name=None if candidate.name == slug else candidate.name,
        address=_address(root, candidate),
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


def _listed_refs(
    root: Path, host: str, project: str, entries: list[Path]
) -> Iterator[RunDirRef]:
    """One ref per run directory in a single legacy ``runs/``, keyed by recorded slug.

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
        address = _address(root, entry)
        if claim == entry.name:
            yield RunDirRef(
                host=host, project=project, slug=claim, path=entry, address=address
            )
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
            yield RunDirRef(
                host=host, project=project, slug=entry.name, path=entry,
                address=address,
            )
            continue
        yield RunDirRef(
            host=host, project=project, slug=claim, path=entry,
            dir_name=entry.name, address=address,
        )


def _grouped_refs(
    root: Path, host: str, project: str, groups: list
) -> Iterator[RunDirRef]:
    """One ref per ``_runs/<slug>/`` grouping dir, keyed by the recorded slug.

    The schema-form half of :func:`_listed_refs`, and the same de-dup rule for the
    same reason: a claim is honoured only while it picks out one grouping dir, and
    a claim that names two of them (or another one's directory name) is listed as
    found instead of guessed at.

    What differs is where each half comes from. The IDENTITY is the slug the
    addressed session records (issue #87 D1), falling back to the grouping dir's
    own name — which the schema says is the slug, so the two normally agree and
    the fallback only covers a record that does not say. The ADDRESS is the
    session dir, which is where the record actually is: the container's one
    name-bearing rename now lands on ``<session-id>--<name>`` (D22), so the
    session dir name is never the slug and ``dir_name`` always carries it.
    """
    claims = {}
    for group, session in groups:
        claims[group] = (_recorded_slug(session) or group.name, session)
    names = {group.name for group in claims}
    counts = Counter(claim for claim, _session in claims.values())
    for group, (claim, session) in claims.items():
        address = _address(root, session)
        if claim != group.name and (counts[claim] > 1 or claim in names):
            _announce_once(
                f"listed-by-name:{host}/{project}/_runs/{group.name}",
                logging.INFO,
                "platform_run_dir_listed_by_name run=%s/%s/_runs/%s slug=%s — more "
                "than one grouping directory here answers to that slug, so the "
                "candidate list offers this one by its directory name; that is "
                "also the name to track it under, since resolving the slug "
                "refuses to guess between them",
                host, project, group.name, claim,
            )
            claim = group.name
        yield RunDirRef(
            host=host, project=project, slug=claim, path=session,
            dir_name=session.name, address=address,
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

    Legacy-only, and the ambiguity above is why. In schema form the container's
    one name-bearing rename lands on the SESSION dir (``<session-id>--<name>``,
    D22) and the grouping dir keeps the slug, so there is no name to glob for and
    :func:`_session_run_dir` addresses ``_runs/<slug>/`` directly. What that
    function meets instead is several SESSIONS under one grouping dir, and it
    resolves rather than refuses — deliberately, because it is not this
    ambiguity. Two ``<slug>--<name>`` dirs are two RUNS whose identity nothing
    can separate, so picking one lands a whole record on the wrong run. Several
    sessions under one ``_runs/<slug>/`` are two launches of ONE identity (D22):
    the run is not in doubt, only which launch is being read, and both are its
    records. It takes the newest, which is what :func:`_session_groups` addresses
    for the picker (:func:`_sessions_in` holds the rule for both), and says so
    once; a specific launch is addressed by its session id.
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


def _session_run_dir(
    root: Path, host: str, project: str, slug: str,
    identities: _RegistryIdentities,
) -> Optional[RunDirRef]:
    """The session dir holding this run's record in schema form, or ``None``.

    The schema-form counterpart of :func:`_ref_at` plus :func:`_renamed_run_dir`
    in one lookup, because in schema form those two are the same lookup. The
    grouping dir IS the slug (``<project>/_runs/<slug>/``, D22 and
    ``docs/SCHEMA.md``), so the address is composed as before and needs no glob;
    what moved one level deeper is the RECORD, which lives in a session dir whose
    name is never the slug — and the container's one name-bearing rename now
    lands there (``<session-id>--<name>``), which is why no name is searched for
    up here.

    The slug is joined as a path SEGMENT and never as half of a pattern, so the
    hazards :func:`_renamed_run_dir` refuses outright do not arise: a hand-edited
    ``**`` or ``*`` is a directory name nothing is called, and a ``..`` is caught
    by the containment re-check every candidate goes through
    (:func:`_checked_dir`) exactly as in the legacy composition.

    Several sessions in one grouping dir resolve to the newest, which is the
    session :func:`_session_groups` addresses for the picker;
    :func:`_renamed_run_dir` documents why that is a choice rather than the
    refusal the legacy ambiguity gets. It is said once — which launch is being
    read is news, where the ordinary one-launch run is not: a line per tracked
    run per daemon would bury the log this read path has to keep readable.
    """
    for project_dir in _project_dirs(root, host, project, identities):
        group = _checked_dir(root, project_dir / _SCHEMA_RUNS_DIR / slug)
        if group is None:
            continue
        sessions = _sessions_in(group)
        chosen = _newest_session(group)
        if chosen is None:
            continue
        ref = _ref_for(root, host, project, slug, chosen)
        if ref is None:
            continue
        if len(sessions) > 1:
            _announce_once(
                f"sessions:{host}/{project}/{slug}",
                logging.INFO,
                "platform_run_dir_sessions run=%s/%s/%s sessions=%d dir=%s — this "
                "slug groups more than one launch, which in schema form is more "
                "than one complete record of the SAME run (the session is the "
                "unit of record); the platform reads the newest and keeps %s as "
                "the run's identity, and a specific launch is addressed by its "
                "session id",
                host, project, slug, len(sessions), ref.rel_path, slug,
            )
        return ref
    return None


def _record_dirs(root: Path, project_dir: Path) -> Iterator[Path]:
    """Every directory under one project that holds a run's record, either shape.

    Legacy: the children of ``runs/``, minus the transient seed dirs and the
    archive a retired record is moved to. Schema: the newest session of each
    ``_runs/<slug>/`` group (:func:`_sessions_in`) — the same session the picker
    addresses and :func:`_session_run_dir` resolves to, so a carried identity
    followed from here lands on the directory the fleet row quotes. The
    schema-form archive needs no exclusion of its own: it is ``_archive/_runs/``
    (D13), a sibling of the area walked here rather than a child of it.
    """
    legacy = _checked_dir(root, project_dir / _LEGACY_RUNS_DIR)
    if legacy is not None:
        try:
            children = sorted(legacy.iterdir())
        except OSError:
            children = []
        for child in children:
            if (not child.is_dir() or child.name.startswith(".")
                    or child.name == run_state.ARCHIVE_DIR):
                continue
            yield child

    schema = _checked_dir(root, project_dir / _SCHEMA_RUNS_DIR)
    if schema is not None:
        try:
            groups = sorted(schema.iterdir())
        except OSError:
            groups = []
        for group in groups:
            if not group.is_dir() or group.name.startswith("."):
                continue
            chosen = _newest_session(group)
            if chosen is not None:
                yield chosen


def _build_reslugged_index(root: Path, host: str, project: str,
                           identities: _RegistryIdentities) -> dict[str, str]:
    """Map each vacated slug to its newest live successor's mirror-relative address.

    Both shapes of the project are scanned (:func:`_record_dirs`), and the value
    is an ADDRESS rather than a directory name because a session dir cannot be
    composed from ``(host, project, slug)`` — the successor of a reslugged run
    may sit under a grouping dir named for the identity it took, in a project dir
    named nothing like its remote.
    """
    matches: dict[str, tuple[tuple[str, str], str]] = {}
    for project_dir in _project_dirs(root, host, project, identities):
        for record in _record_dirs(root, project_dir):
            state = _read_run_state(record)
            if state is None or state.get("status") != "in-progress":
                continue
            address = _address(root, record)
            if address is None:  # pragma: no cover - _record_dirs walks from root
                continue
            rank = (str(state.get("created") or ""), record.name)
            for vacated in run_state.vacated_slugs(state):
                if rank > matches.get(vacated, (("", ""), ""))[0]:
                    matches[vacated] = (rank, address)
    return {slug: found[1] for slug, found in matches.items()}


def _reslugged_run_dir(
    config: PlatformConfig, root: Path, host: str, project: str, slug: str,
    identities: _RegistryIdentities,
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
            index = _build_reslugged_index(root, host, project, identities)
            if revision is not None:
                _RESLUGGED_INDEX[cache_key] = (revision, index)

    address = index.get(slug)
    if address is None:
        return None
    ref = _ref_for(root, host, project, slug, root.joinpath(*address.split("/")))
    if ref is None:
        return None
    _announce_once(
        f"reslugged:{host}/{project}/{slug}->{address}",
        logging.INFO,
        "platform_run_dir_reslugged run=%s/%s/%s dir=%s — the live run "
        "records the requested slug in reslugged_from, so the platform follows "
        "that carried identity",
        host, project, slug, ref.rel_path,
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

    Four lookups, cheapest and most exact first: the legacy ``runs/<slug>``
    directory; the schema-form session dir under ``_runs/<slug>/``
    (:func:`_session_run_dir`); the name-bearing directory the container renamed
    a legacy run into (:func:`_renamed_run_dir`); and finally the live successor
    that explicitly records the requested identity in ``reslugged_from``. The two
    composed addresses come before the two searches: a tree is in one shape or
    the other per project, and where both somehow exist the run is read where it
    literally sits rather than where a pattern or a carried identity leads.

    **The identity is never the path in schema form**, which is what the second
    lookup has to work around: a tracked run arrives here as
    ``(host, project, slug)`` and its project may be a flat ``widgets/`` whose
    name says nothing about which repo it holds (D7). :func:`_project_dirs` maps
    the identity back to the directories that can hold it — the
    ``{host}/{project}/`` path, which serves the legacy tree and the host bucket
    of an unnamed repo because that path is derived from the remote (D9), and
    then any registered short name bound to the same remote. Nothing is derived
    from a directory name and nothing is searched for by content, so a run whose
    project is neither path-shaped nor registered is reported absent rather than
    guessed at.

    The third lookup is the fix
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
    # Lazy, and read at most once per resolve: a legacy run and a host bucket's
    # run both compose their own path, so the manifest is only touched for a run
    # neither of them found.
    identities = _RegistryIdentities(root)
    ref = _ref_at(root, host, project, slug, slug)
    if ref is not None:
        return ref
    session = _session_run_dir(root, host, project, slug, identities)
    if session is not None:
        return session
    renamed = _renamed_run_dir(root, host, project, slug)
    if renamed is not None:
        return renamed
    return _reslugged_run_dir(config, root, host, project, slug, identities)
