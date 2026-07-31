"""The tracked-run index — what this orchestrator considers *its* fleet.

Why this exists
---------------
The work repo is shared by every dev using lmer. An early build of the fleet view
enumerated the mirror and proudly reported 152 runs, 12 of them "needing your
input" — several belonging to other people. Presenting a colleague's blocked
question as *your* input inverts the product's headline promise, so scope cannot
come from the work repo (spec D25).

It comes from here instead: a local index of the runs this orchestrator knows
about. **A fresh orchestrator tracks nothing and therefore shows nothing**, no
matter how full the shared repo is. That is the intended behavior.

Two ways a run enters the index:

- **spawned** — the platform started it. The normal path, recorded at spawn time.
- **adopted** — an operator named an existing run explicitly. The escape hatch for
  work started by hand, which spec D8 already excludes from *control*; including
  it in the *view* is therefore a deliberate act, not an inference.

The index deliberately outlives sessions. A run that stopped to ask a question
has no container left, and that is exactly the row that must not vanish — so
entries are removed only by an explicit ``forget``. That one removal path also
takes the run's operator-facing metadata with it (:mod:`lmer_platform.meta`),
which is scoped to a tracked run and would otherwise be state no view lists.

What was rejected
-----------------
Deriving ownership from git authorship of each run dir. The mirror's commits do
carry each dev's email, so it would work — but it requires abandoning the shallow
clone to get history, and it *infers* an answer the operator can simply state. A
wrong inference here means either hiding your own work or showing someone else's,
and both are worse than asking.

Storage is one JSON snapshot (``runs.json``) rather than a file per run: the
daemon is its only writer, entries are tiny, and the whole set is read on every
state build — the conditions under which a single snapshot is simpler than a
directory. This is the opposite trade from :mod:`lmer_platform.registry`, where
each session writes its *own* file precisely because the writers are different
processes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional

from .store import (
    StoreError, mutating, read_json, snapshot_path, utc_now_iso, write_json,
)

logger = logging.getLogger("lmer_platform.runs")

__all__ = [
    "RUNS_FILE", "SOURCES", "TrackedRun", "RunIndexError", "run_key",
    "load_index", "list_tracked", "get_tracked", "track", "forget",
    "note_session",
]

RUNS_FILE = "runs.json"

#: How a run came to be tracked.
SOURCES = ("spawned", "adopted")


class RunIndexError(RuntimeError):
    """Raised on a malformed tracking request — a caller bug."""


@dataclass(frozen=True)
class TrackedRun:
    """One run this orchestrator tracks."""

    host: str
    project: str
    slug: str
    source: str = "spawned"
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    last_session_id: Optional[str] = None
    taskdef: Optional[str] = None
    target: Optional[str] = None
    repo: Optional[str] = None
    note: Optional[str] = None

    @property
    def key(self) -> str:
        """The run's identity, ``host/project/slug``. Deliberately not a path.

        There used to be a ``rel_path`` beside this that composed
        ``<host>/<project>/runs/<slug>``, and for a run with a name that is a
        directory which does not exist: the container renames a named run's dir to
        ``runs/<slug>--<name>`` while this index records only the slug it was keyed
        under. So every reader of it — the CLI's tracked listing, the adopt
        message, the dormant fleet row — quoted a path the operator could not open,
        for exactly the runs that are hardest to find by hand. Same correction and
        same reasoning as :attr:`lmer_platform.answer.AnswerRequest.key`.

        An entry therefore carries identity and no address at all. A real
        directory is *found* — by content, via
        :func:`lmer_platform.workrepo.resolve_run_dir`, which is also the only
        thing that can build the honest path
        (:attr:`lmer_platform.workrepo.RunDirRef.rel_path`) — and never composed
        from what is stored here.
        """
        return run_key(self.host, self.project, self.slug)

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "project": self.project,
            "slug": self.slug,
            "source": self.source,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "last_session_id": self.last_session_id,
            "taskdef": self.taskdef,
            "target": self.target,
            "repo": self.repo,
            "note": self.note,
            "key": self.key,
        }

    @classmethod
    def from_dict(cls, key: str, payload: dict) -> Optional["TrackedRun"]:
        """Rebuild an entry, or ``None`` when it is unusable.

        Tolerant on purpose: one malformed entry (hand-edited, or written by a
        version that changed shape) must not empty the whole fleet view.
        """
        if not isinstance(payload, dict):
            return None
        host = payload.get("host")
        project = payload.get("project")
        slug = payload.get("slug")
        if not (isinstance(host, str) and isinstance(project, str)
                and isinstance(slug, str) and host and project and slug):
            logger.warning("platform_tracked_run_malformed key=%s", key)
            return None
        source = payload.get("source")
        return cls(
            host=host,
            project=project,
            slug=slug,
            source=source if source in SOURCES else "adopted",
            first_seen=payload.get("first_seen"),
            last_seen=payload.get("last_seen"),
            last_session_id=payload.get("last_session_id"),
            taskdef=payload.get("taskdef"),
            target=payload.get("target"),
            repo=payload.get("repo"),
            note=payload.get("note"),
        )


def run_key(host: str, project: str, slug: str) -> str:
    """Stable identity for a run across the platform's state files."""
    for name, value in (("host", host), ("project", project), ("slug", slug)):
        if not isinstance(value, str) or not value.strip():
            raise RunIndexError(f"{name} must be a non-empty string, got {value!r}")
    return f"{host.strip()}/{project.strip()}/{slug.strip()}"


def _index_path():
    return snapshot_path(RUNS_FILE)


def _meta():
    """The :mod:`lmer_platform.meta` module, imported on demand.

    Inside a function rather than at module scope because that module imports
    *this* one — it keys metadata off :func:`run_key` and refuses to describe a
    run this index does not carry — so a top-level import here would close the
    cycle and try to read those names before they exist. Same lazy-import shape,
    and same one named seam, as ``lmer_platform.spawn._transcripts``.
    """
    from . import meta

    return meta


def load_index() -> dict:
    """Raw ``{key: entry}`` mapping. Empty when nothing is tracked yet."""
    try:
        stored = read_json(_index_path())
    except StoreError as exc:
        # A corrupt index would otherwise make the whole fleet vanish; the bad
        # bytes have already been moved aside for post-mortem.
        logger.error("platform_run_index_unreadable error=%s — treating as empty", exc)
        return {}
    if not stored:
        return {}
    runs = stored.get("runs")
    return runs if isinstance(runs, dict) else {}


def _save_index(runs: dict) -> None:
    write_json(_index_path(), {"runs": runs})


def list_tracked() -> list:
    """Every tracked run, most recently seen first.

    Ordering favours recency because that is what an operator scanning the list
    cares about; the inventory re-sorts by attention anyway.
    """
    entries = []
    for key, payload in load_index().items():
        entry = TrackedRun.from_dict(key, payload)
        if entry is not None:
            entries.append(entry)
    entries.sort(
        key=lambda e: (e.last_seen or e.first_seen or "", e.slug), reverse=True
    )
    return entries


def get_tracked(host: str, project: str, slug: str) -> Optional[TrackedRun]:
    key = run_key(host, project, slug)
    payload = load_index().get(key)
    if not isinstance(payload, dict):
        return None
    return TrackedRun.from_dict(key, payload)


def track(
    host: str,
    project: str,
    slug: str,
    *,
    source: str = "spawned",
    taskdef: Optional[str] = None,
    target: Optional[str] = None,
    repo: Optional[str] = None,
    session_id: Optional[str] = None,
    note: Optional[str] = None,
) -> TrackedRun:
    """Start tracking a run, or refresh what is known about one already tracked.

    Re-tracking preserves ``first_seen`` and the original ``source``: a run the
    platform spawned does not become "adopted" because someone later named it,
    and the metadata of the first sighting is the interesting one. Everything
    else is refreshed when a newer value is supplied.
    """
    if source not in SOURCES:
        raise RunIndexError(
            f"invalid source {source!r}: expected one of {', '.join(SOURCES)}"
        )
    key = run_key(host, project, slug)
    # The whole index is rewritten to change one key, so an unserialised
    # concurrent writer would drop this one: an adopt and a spawn overlapping
    # here is a run that never appears in the fleet view while its container is
    # running, and that `resume` and `answer` then refuse as `run_not_tracked`.
    # Every route that gets here is a sync def in Starlette's threadpool.
    with mutating(_index_path()):
        runs = load_index()
        existing = runs.get(key) if isinstance(runs.get(key), dict) else {}
        now = utc_now_iso()

        entry = {
            "host": host,
            "project": project,
            "slug": slug,
            "source": (
                existing.get("source")
                if existing.get("source") in SOURCES
                else source
            ),
            "first_seen": existing.get("first_seen") or now,
            "last_seen": now,
            "last_session_id": session_id or existing.get("last_session_id"),
            "taskdef": taskdef or existing.get("taskdef"),
            "target": target if target is not None else existing.get("target"),
            "repo": repo or existing.get("repo"),
            "note": note if note is not None else existing.get("note"),
        }
        runs[key] = entry
        _save_index(runs)
    logger.info("platform_run_tracked key=%s source=%s", key, entry["source"])
    return TrackedRun.from_dict(key, entry)


def note_session(
    host: str, project: str, slug: str, session_id: Optional[str]
) -> Optional[TrackedRun]:
    """Record the latest session for an already-tracked run.

    Returns ``None`` when the run is not tracked — deliberately *not* an implicit
    track, because that would let any passing session widen the view's scope
    behind the operator's back.
    """
    if get_tracked(host, project, slug) is None:
        return None
    return track(host, project, slug, session_id=session_id)


def forget(host: str, project: str, slug: str) -> bool:
    """Stop tracking a run. Returns whether it had been tracked.

    The only removal path. Sessions ending never remove a run: a run that exited
    to ask a question is precisely the one that must stay visible.

    The run's title and description go with it. They describe a run in *this*
    orchestrator's fleet (:mod:`lmer_platform.meta`), so leaving them behind
    would leave state that no view lists and no verb can reach — and an operator
    who forgets a run has said they are done with what this orchestrator knew
    about it.
    """
    key = run_key(host, project, slug)
    with mutating(_index_path()):
        runs = load_index()
        if key not in runs:
            return False
        runs.pop(key)
        _save_index(runs)
    # Best effort, and deliberately after the index write — and outside the lock,
    # because ``meta.drop`` takes its own file's: the run *is* forgotten
    # by the time this runs, so raising here would report a failure of something
    # that succeeded. An orphaned description is worth a log line, not an error.
    try:
        _meta().drop(host, project, slug)
    except StoreError as exc:
        logger.warning("platform_run_meta_orphaned key=%s error=%s", key, exc)
    logger.info("platform_run_forgotten key=%s", key)
    return True


def keys(entries: Iterable[TrackedRun]) -> set:
    """The ``(host, project, slug)`` tuples for *entries*."""
    return {(e.host, e.project, e.slug) for e in entries}
