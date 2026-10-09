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

The key segment (issue #353, spec §6.1, D1/D7)
---------------------------------------------
In schema form a launch's record lives in its own session directory,
``<project>/_runs/<slug>/<uid>[--<name>]/``, and one slug groups every launch
against one (taskdef, target) pair (D22). The identity of a schema-2 launch is
its execution uid, not its slug, so this index keys it ``host/project/<uid>``
and stores the slug only as the group label. A schema-1 launch has no uid and
keys by slug as it always did.

**The key segment is the third part of every ``(host, project, x)`` triple.**
For a schema-2 row it is the uid; otherwise the slug. :func:`key_parts` is the
one helper that spells it — over a fleet row, a request body or a registry
``run`` block; strict, so a malformed uid is refused rather than read as the
slug, with :func:`key_parts_lenient` for the read-only paths that must degrade
instead — and every keyed store (:mod:`lmer_platform.meta`,
:mod:`lmer_platform.relations`, the check-in marks, references, projects) takes
that segment as its third argument and never re-spells ``uid or slug``.
:func:`run_key` itself is unchanged: a 32-hex uid is already a valid last
segment.

- **No session list is stored.** An entry already carries
  ``last_session_id`` — the newest session this orchestrator ran for the run,
  which in schema form is also the name of that session's directory, so it is
  exactly the hint an operator needs to reach one of several sessions grouped
  under a slug. A list of the others was considered and rejected: the durable
  answer to "what launches exist for this run" is ``ls _runs/<slug>/`` in the
  work repo, which stays right when a colleague launches a session this
  orchestrator never saw, and a copy here would go stale the moment they did.
  This index scopes the fleet (D25); it is not a second record.
- **An address is never stored** — see :attr:`TrackedRun.key`. A session dir
  makes that more true, not less: which of a slug's session dirs is the current
  one is something only the mirror can answer, so a directory is *found*
  (:func:`lmer_platform.workrepo.resolve_run_dir`) and only what found it names
  one.

:meth:`TrackedRun.from_dict` reads the keys it knows and ignores the rest, so an
index written by a build that stores more than this one does still loads.

Storage is one JSON snapshot (``runs.json``) rather than a file per run: the
daemon is its only writer, entries are tiny, and the whole set is read on every
state build — the conditions under which a single snapshot is simpler than a
directory. This is the opposite trade from :mod:`lmer_platform.registry`, where
each session writes its *own* file precisely because the writers are different
processes.
"""

from __future__ import annotations

import contextlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Mapping, Optional

from lmer_cli import runref

from . import registry
from .store import (
    StoreError, mutating, read_json, snapshot_path, utc_now_iso, write_json,
)
from . import projects, project_context

logger = logging.getLogger("lmer_platform.runs")

__all__ = [
    "RUNS_FILE", "SOURCES", "TrackedRun", "RunIndexError", "run_key",
    "load_index", "list_tracked", "get_tracked", "track", "forget",
    "forgotten_keys", "filter_forgotten",
    "note_session", "run_for_session", "run_key_of_entry", "keys",
    "key_parts", "key_parts_lenient", "merge_values", "BOOKKEEPING_STAMPS",
    "ORIGIN_STAMPS",
    "is_conflict_pair", "holds_conflict", "Transition", "UID_RE",
    "INDEX_CARRIED_FIELDS", "run_of_session", "carried_session_ids",
    "conflict_stores", "settle_conflict", "index_conflicted",
    "TRANSITION_STATES", "TRANSITION_STORES", "READOPT_BY_UID",
    "transition_lock", "begin_transition", "mark_transition", "finish_transition",
    "pending_transitions", "mark_unresolved", "readopt_marks",
]

RUNS_FILE = "runs.json"

#: How a run came to be tracked.
SOURCES = ("spawned", "adopted")

#: A schema-2 execution uid: 32 lowercase hex. The same grammar
#: ``work_repo.session_record.UID_RE`` enforces; repeated here so the platform
#: index does not import the work CLI to read its own key. ``\Z`` rather than
#: ``$``, which also matches before a trailing newline: every caller uses
#: ``.match``, and a key segment ending in ``\n`` is not a uid.
UID_RE = re.compile(r"^[0-9a-f]{32}\Z")

#: States of a key transition record (:class:`Transition`).
TRANSITION_STATES = ("pending", "done")

#: The platform-local stores a transition carries, in the order it moves them.
#:
#: ``projects`` and ``threads`` are moved by one call
#: (:func:`lmer_platform.projects.rekey` carries the home, its sessions and the
#: durable thread owners in one database transaction) and are marked done
#: together; they stay two names so the record still says that thread ownership
#: moved. ``registry`` is the live registry entry's ``run`` block gaining the
#: uid (:mod:`lmer_platform.run_transition`), last because it is the one step a
#: client can observe joining two rows into one.
TRANSITION_STORES = (
    "meta", "relations", "checkin", "references", "projects", "threads", "registry",
)

#: Fleet-row text when a collision kept both values (spec §6.1).
READOPT_BY_UID = "re-adopt by uid"

#: The index entry's own fields the lossless policy covers when a slug entry is
#: bound onto an entry already under the uid key (:func:`begin_transition`).
INDEX_CARRIED_FIELDS = ("note", "last_session_id", "taskdef", "target", "repo")


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
    uid: Optional[str] = None

    @property
    def key(self) -> str:
        """The run's identity, ``host/project/<key segment>``.

        The key segment is the execution uid for a schema-2 run and the slug
        otherwise (:func:`key_parts`). Deliberately not a path.

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
        return run_key(self.host, self.project, _segment(self.slug, self.uid))

    def to_dict(self) -> dict:
        """The entry as the adopt reply and the CLI show it.

        ``uid`` appears only on a schema-2 entry, so a schema-1 entry keeps
        exactly the shape it had before uids existed.
        """
        payload = {
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
        if self.uid is not None:
            payload["uid"] = self.uid
        return payload

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
        uid = payload.get("uid")
        if uid is not None and not (isinstance(uid, str) and UID_RE.match(uid)):
            # A malformed uid must not make the whole entry unusable — the
            # entry still has its slug, so it degrades to slug-keyed.
            logger.warning("platform_tracked_run_malformed key=%s uid=%r", key, uid)
            uid = None
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
            uid=uid,
        )


def run_key(host: str, project: str, slug: str) -> str:
    """Stable identity for a run across the platform's state files.

    The validation is this index's — an empty part is a refusal rather than a row
    nothing can address; the spelling is :mod:`lmer_cli.runref`'s.
    """
    for name, value in (("host", host), ("project", project), ("slug", slug)):
        if not isinstance(value, str) or not value.strip():
            raise RunIndexError(f"{name} must be a non-empty string, got {value!r}")
    return runref.compose(host.strip(), project.strip(), slug.strip())


def _segment(slug: Optional[str], uid: Optional[str]) -> Optional[str]:
    """The key segment of a run known by its two values: the uid when there is
    one, otherwise the slug — :func:`key_parts`'s rule, for a caller inside this
    module that holds the values rather than a mapping."""
    return uid if uid is not None else slug


def _key_source(mapping: Mapping) -> Mapping:
    """Where *mapping* names its run: its own flat fields, or a nested ``run``.

    The flat fields win when the mapping has a flat ``host`` or ``project`` —
    a fleet row or request body says which run it is at the top level, and a
    ``run`` block riding along is somebody else's description of it. A registry
    entry has no flat identity, so its ``run`` block is read.
    """
    nested = mapping.get("run")
    if isinstance(nested, Mapping) and mapping.get("host") is None \
            and mapping.get("project") is None:
        return nested
    return mapping


def key_parts(mapping: Mapping) -> tuple:
    """``(host, project, key_segment)`` for anything that names a run.

    The one spelling of the key-segment rule (module docstring): a 32-hex
    ``uid`` wins, otherwise the ``slug``. Accepts a fleet row or request body
    (flat) and a registry entry (its ``run`` block nested under ``run``); flat
    fields win when both are present (:func:`_key_source`).

    Strict, because routes call it on request bodies. Raises
    :class:`RunIndexError` when host, project or the segment is missing — the
    same refusal :func:`run_key` gives, so a caller can compose the key with
    ``run_key(*key_parts(row))`` — and when a ``uid`` is present but not 32
    lowercase hex: falling back to the slug would act on the group's slug-keyed
    run, a different run than the caller named. An absent uid (``None`` or
    ``""``, the spelling an empty query parameter arrives in) is a schema-1
    reference and keys by slug.

    A display or fold path that must degrade rather than raise reads
    :func:`key_parts_lenient` instead.
    """
    if not isinstance(mapping, Mapping):
        raise RunIndexError(f"expected a mapping, got {type(mapping).__name__}")
    source = _key_source(mapping)
    uid = source.get("uid")
    if uid is not None and uid != "" and not (isinstance(uid, str) and UID_RE.match(uid)):
        raise RunIndexError(f"uid must be 32 lowercase hex, got {uid!r}")
    return _parts_of(source, uid or None)


def key_parts_lenient(mapping: object) -> Optional[tuple]:
    """:func:`key_parts` for a value that is read, never acted on, or ``None``.

    For the registry, fleet-row folds and inventory joins, where one malformed
    value must cost its own row and not the build: a malformed uid degrades to
    the slug (as :meth:`TrackedRun.from_dict` degrades it), and a missing part
    is ``None`` rather than a raise. Never call this on a request body — that
    fallback is exactly what :func:`key_parts` refuses.
    """
    if not isinstance(mapping, Mapping):
        return None
    source = _key_source(mapping)
    uid = source.get("uid")
    try:
        return _parts_of(source, uid if isinstance(uid, str) and UID_RE.match(uid) else None)
    except RunIndexError:
        return None


def _parts_of(source: Mapping, uid: Optional[str]) -> tuple:
    host = source.get("host")
    project = source.get("project")
    slug = source.get("slug")
    segment = _segment(slug, uid)
    if not (isinstance(host, str) and host.strip()):
        raise RunIndexError(f"host must be a non-empty string, got {host!r}")
    if not (isinstance(project, str) and project.strip()):
        raise RunIndexError(f"project must be a non-empty string, got {project!r}")
    if not (isinstance(segment, str) and segment.strip()):
        raise RunIndexError(
            f"a run needs a uid or a slug, got uid={source.get('uid')!r} slug={slug!r}"
        )
    return (host.strip(), project.strip(), segment.strip())


#: Bookkeeping stamps — when something happened to a stored value, not what the
#: operator or the launch said. Two keys stamping one run differently is not two
#: answers to keep, so :func:`merge_values` keeps one stamp — the earlier for an
#: :data:`ORIGIN_STAMPS` key, the later otherwise — and never reports it as a
#: conflict; otherwise every collision would be one, whatever the operator's
#: titles said. Covers meta's ``updated_at``, the index's and check-in
#: marks' ``first_seen``/``last_seen``/``checked_at``/``announced_at``, a
#: reference's ``added_at``, a relation's ``created_at`` and a project home's
#: ``created``/``revision``.
BOOKKEEPING_STAMPS = frozenset({
    "updated_at", "first_seen", "last_seen", "added_at", "checked_at",
    "announced_at", "created", "created_at", "revision",
})

#: The stamps among :data:`BOOKKEEPING_STAMPS` that record when something
#: BEGAN: for these the earlier of two is the true answer, so a merge keeps it.
ORIGIN_STAMPS = frozenset({"first_seen", "created", "created_at"})


def _later(old, new):
    """The later of two stamps; *new* when they cannot be ordered.

    ISO-8601 text is compared as instants (``Z`` and ``+00:00`` are one zone), a
    number (a home's ``revision``) by value.
    """
    if isinstance(old, (int, float)) and isinstance(new, (int, float)) \
            and not isinstance(old, bool) and not isinstance(new, bool):
        return max(old, new)
    if isinstance(old, str) and isinstance(new, str):
        instants = []
        for stamp in (old, new):
            try:
                parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                return new
            instants.append(parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc))
        return old if instants[0] > instants[1] else new
    return new


def _earlier(old, new):
    """The earlier of two stamps; *old* when they cannot be ordered."""
    if old == new:
        return old
    return new if _later(old, new) is old else old


def merge_values(old, new) -> tuple:
    """The lossless collision policy for carrying state from an old key to a new.

    Returns ``(merged, conflict)``. A field the new key lacks is carried over; a
    field both hold with equal value is a no-op; a field both hold with different
    values is **never** dropped — both values are retained as an
    ``{"old": .., "new": ..}`` pair and ``conflict`` is ``True``. Nothing is
    written to a log in lieu of being kept.

    Two mappings merge **one level**: their keys are compared, and a key whose
    values differ becomes a pair even when both values are mappings themselves.
    A key in :data:`BOOKKEEPING_STAMPS` is not a conflict: the earlier stamp is
    kept for an :data:`ORIGIN_STAMPS` key and the later one otherwise.
    :func:`holds_conflict` finds the pairs this leaves.
    """
    if old is None:
        return (new, False)
    if new is None:
        return (old, False)
    if old == new:
        return (old, False)
    if isinstance(old, Mapping) and isinstance(new, Mapping):
        merged = dict(new)
        conflict = False
        for key, value in old.items():
            if key not in merged or merged[key] is None:
                merged[key] = value
            elif merged[key] == value or value is None:
                continue
            elif key in ORIGIN_STAMPS:
                merged[key] = _earlier(value, merged[key])
            elif key in BOOKKEEPING_STAMPS:
                merged[key] = _later(value, merged[key])
            else:
                merged[key] = {"old": value, "new": merged[key]}
                conflict = True
        return (merged, conflict)
    return ({"old": old, "new": new}, True)


def is_conflict_pair(value: object) -> bool:
    """Whether *value* is the ``{"old", "new"}`` pair :func:`merge_values` keeps."""
    return isinstance(value, Mapping) and set(value) == {"old", "new"}


def holds_conflict(value: object) -> bool:
    """Whether *value*, or a field of it, is a pair :func:`merge_values` kept.

    How a store's ``rekey`` answers ``conflict`` from what is on disk rather
    than from what this one call merged: a re-run after a crash between the
    store's write and :func:`mark_transition` finds the old key gone and the
    pair under the new one, and must still report it (spec §6.1). Also how a
    re-adopt by uid decides whether a conflict mark may be cleared.
    """
    if is_conflict_pair(value):
        return True
    if isinstance(value, Mapping):
        return any(_pairs(item) for item in value.values())
    return False


@dataclass
class Transition:
    """A durable, resumable move of platform-local state from one key to another.

    Kept in ``runs.json`` beside the entry it describes. It is written
    ``pending`` with every store pending **before** the first store moves, and
    cleared only after every store reports done — so a crash leaves a ``pending``
    record the next scan resumes. ``get_tracked`` follows the old key as an alias
    while it lives.
    """

    old_key: str
    new_key: str
    state: str = "pending"
    stores: dict = field(default_factory=lambda: {s: "pending" for s in TRANSITION_STORES})

    def __post_init__(self):
        if self.state not in TRANSITION_STATES:
            raise RunIndexError(f"invalid transition state {self.state!r}")
        for store, status in self.stores.items():
            if store not in TRANSITION_STORES:
                raise RunIndexError(f"unknown transition store {store!r}")
            if status not in TRANSITION_STATES:
                raise RunIndexError(f"invalid store state {status!r} for {store!r}")

    @property
    def done(self) -> bool:
        return self.state == "done"

    def pending_stores(self) -> list:
        return [s for s in TRANSITION_STORES if self.stores.get(s) != "done"]

    def to_dict(self) -> dict:
        return {
            "old_key": self.old_key,
            "new_key": self.new_key,
            "state": self.state,
            "stores": {s: self.stores.get(s, "pending") for s in TRANSITION_STORES},
        }

    @classmethod
    def from_dict(cls, payload: Mapping) -> "Transition":
        stores = payload.get("stores")
        return cls(
            old_key=payload.get("old_key"),
            new_key=payload.get("new_key"),
            state=payload.get("state", "pending"),
            stores=(
                {s: stores.get(s, "pending") for s in TRANSITION_STORES}
                if isinstance(stores, Mapping) else
                {s: "pending" for s in TRANSITION_STORES}
            ),
        )


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


def _references():
    """The :mod:`lmer_platform.references` module, imported on demand — the same
    cycle and the same seam as :func:`_meta`."""
    from . import references

    return references


def _snapshot() -> dict:
    """The whole index file: ``{"runs": {...}, "forgotten": [[...], ...]}``.

    Run entries and forgotten-run tombstones share one file and one writer, so
    a forget cannot half-apply. ``forgotten`` holds ``[host, project, slug]``
    parts — the composed key is derived, never stored, so a spelling change in
    :func:`run_key` cannot strand a tombstone.
    """
    try:
        stored = read_json(_index_path())
    except StoreError as exc:
        # A corrupt index would otherwise make the whole fleet vanish; the bad
        # bytes have already been moved aside for post-mortem.
        logger.error("platform_run_index_unreadable error=%s — treating as empty", exc)
        return {"runs": {}, "forgotten": []}
    if not isinstance(stored, dict):
        return {"runs": {}, "forgotten": []}
    runs = stored.get("runs")
    forgotten = stored.get("forgotten")
    return {
        "runs": runs if isinstance(runs, dict) else {},
        "forgotten": [list(parts) for parts in forgotten
                      if isinstance(parts, (list, tuple)) and len(parts) == 3
                      and all(isinstance(part, str) and part for part in parts)]
        if isinstance(forgotten, list) else [],
    }


def load_index() -> dict:
    """Raw ``{key: entry}`` mapping. Empty when nothing is tracked yet."""
    return _snapshot()["runs"]


def _save_index(runs: dict, forgotten: Iterable = ()) -> None:
    payload = {"runs": runs}
    tombstones = [list(parts) for parts in sorted({tuple(part) for part in forgotten})]
    if tombstones:
        payload["forgotten"] = tombstones
    write_json(_index_path(), payload)


def forgotten_keys() -> set:
    """Composed run keys the operator has forgotten.

    ``forget`` drops the index entry, but a run's retained registry sessions are
    evidence and stay on disk. This set lets the fleet view leave those sessions
    out without deleting them (#527).
    """
    return {run_key(*parts) for parts in _snapshot()["forgotten"]}


def filter_forgotten(sessions: Iterable[dict]) -> list:
    """*sessions* minus a forgotten run's retained (dead) entries.

    A live session is kept: forgetting says the operator is done tracking the
    run, not that they want a running container to vanish from the fleet view.
    Once that session dies its retained entry is dropped by the same rule.
    """
    forgotten = forgotten_keys()
    if not forgotten:
        return list(sessions)
    kept = []
    for entry in sessions:
        parts = run_key_of_entry(entry)
        live = entry.get("live")
        if live is None:
            live = registry.is_live(entry)
        if parts and not live and run_key(*parts) in forgotten:
            continue
        kept.append(entry)
    return kept


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


def get_tracked(
    host: str, project: str, slug: Optional[str] = None, *, uid: Optional[str] = None
) -> Optional[TrackedRun]:
    """The tracked entry for a run, addressed by uid or by slug.

    ``uid`` wins when given (spec §6.1). A composed key that is no longer an
    entry's own key is still followed when a key transition recorded it as an
    alias, so a stale client reference resolves.
    """
    segment = _segment(slug, uid)
    key = run_key(host, project, segment)
    index = load_index()
    payload = index.get(key)
    if not isinstance(payload, dict):
        for candidate_key, candidate in index.items():
            if not isinstance(candidate, dict):
                continue
            aliases = candidate.get("aliases")
            if isinstance(aliases, list) and key in aliases:
                payload, key = candidate, candidate_key
                break
        else:
            return None
    return TrackedRun.from_dict(key, payload)


def track(
    host: str,
    project: str,
    slug: str,
    *,
    uid: Optional[str] = None,
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

    ``uid`` keys a schema-2 launch by its execution uid; the slug is still
    stored as the group label.
    """
    if source not in SOURCES:
        raise RunIndexError(
            f"invalid source {source!r}: expected one of {', '.join(SOURCES)}"
        )
    if uid is not None and not UID_RE.match(uid or ""):
        raise RunIndexError(f"uid must be 32 lowercase hex, got {uid!r}")
    segment = _segment(slug, uid)
    key = run_key(host, project, segment)
    # The whole index is rewritten to change one key, so an unserialised
    # concurrent writer would drop this one: an adopt and a spawn overlapping
    # here is a run that never appears in the fleet view while its container is
    # running, and that `resume` and `answer` then refuse as `run_not_tracked`.
    # Every route that gets here is a sync def in Starlette's threadpool.
    with projects.operation(), mutating(_index_path()):
        if projects.enabled():
            home = projects.run_home(key)
            projects.bind_run(key, project_context.current() or (home or {}).get("project_id"))
        snapshot = _snapshot()
        runs = snapshot["runs"]
        forgotten = {tuple(parts) for parts in snapshot["forgotten"]}
        existing = runs.get(key) if isinstance(runs.get(key), dict) else {}
        now = utc_now_iso()

        # Fields this writer does not own (a key transition's ``transition``,
        # ``aliases``, ``carried`` and ``conflict``) ride along untouched — but
        # an explicit note is the operator writing the field a carry kept two
        # values of, so it replaces that pair (:func:`begin_transition`).
        entry = dict(existing)
        if note is not None:
            _drop_carried(entry, "note")
        # ``uid`` is written only for a schema-2 entry: a schema-1 entry stays
        # byte-identical to the shape it had before uids existed.
        if uid is None:
            entry.pop("uid", None)
        entry.update({
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
        })
        if uid is not None:
            entry["uid"] = uid
        runs[key] = entry
        # Re-tracking clears a forget: the operator asked for the run again.
        forgotten.discard((host.strip(), project.strip(), segment.strip()))
        _save_index(runs, forgotten)
    logger.info("platform_run_tracked key=%s source=%s", key, entry["source"])
    return TrackedRun.from_dict(key, entry)


def note_session(
    host: str,
    project: str,
    slug: Optional[str] = None,
    session_id: Optional[str] = None,
    *,
    uid: Optional[str] = None,
) -> Optional[TrackedRun]:
    """Record the latest session for an already-tracked run.

    Returns ``None`` when the run is not tracked — deliberately *not* an implicit
    track, because that would let any passing session widen the view's scope
    behind the operator's back.
    """
    tracked = get_tracked(host, project, slug, uid=uid)
    if tracked is None:
        return None
    # The entry's own uid, not the caller's: a slug reference that reached a
    # bound entry through its alias would otherwise re-create the slug key the
    # transition retired (:mod:`lmer_platform.run_transition`).
    return track(host, project, tracked.slug, uid=tracked.uid, session_id=session_id)


def run_for_session(session_id: str) -> Optional[tuple]:
    """``(host, project, slug)`` of the run *session_id* belongs to, or ``None``.

    The second source is the whole point: a clean exit **removes** the registry
    entry (``spawn._watch`` on ``code == 0``), so the entry answers for a live or
    crashed session and says nothing for the ordinary finished one. The tracked
    index remembers the other side and outlives every entry.

    Both callers need both halves. The session routes keep serving a session that
    is only a PTY log, so with the registry read alone an assistant reading a
    *dormant* run got a 200 and stamped nothing — the shape issue #244 was filed
    about, announced again every window forever.

    ``None`` means no run this host knows, and neither caller infers from it.
    Tolerant on both reads: a missing key costs a stamp or a narrowed transcript,
    never the response.

    The third part is the key segment (the uid for a schema-2 run); a caller
    that has to *say* which run it is reads :func:`run_of_session`, which keeps
    the slug and the uid apart.
    """
    run = run_of_session(session_id)
    return key_parts_lenient(run) if run is not None else None


def run_of_session(session_id: str) -> Optional[dict]:
    """``{host, project, slug[, uid]}`` of the run *session_id* belongs to, or ``None``.

    The same two sources as :func:`run_for_session`, in the same order, as the
    run object a reply names: ``slug`` is the group label and ``uid`` appears
    only for a schema-2 run, so a schema-1 reply keeps the shape it had.
    """
    try:
        entry = registry.read_session(session_id)
    except registry.RegistryError as exc:
        logger.debug(
            "platform_run_for_session_unreadable id=%s error=%s — falling back "
            "to the tracked index", session_id, exc,
        )
        entry = None
    parts = run_key_of_entry(entry)
    if parts is not None:
        run = entry["run"]
        host, project, segment = parts
        if run.get("uid") == segment:
            slug = run.get("slug")
            return {"host": host, "project": project,
                    "slug": slug if isinstance(slug, str) and slug else None, "uid": segment}
        return {"host": host, "project": project, "slug": segment}
    for candidate in list_tracked():
        if candidate.last_session_id == session_id:
            found = {"host": candidate.host, "project": candidate.project,
                     "slug": candidate.slug}
            if candidate.uid is not None:
                found["uid"] = candidate.uid
            return found
    return None


def run_key_of_entry(entry: Optional[dict]) -> Optional[tuple]:
    """``(host, project, key segment)`` off a registry entry's ``run`` block.

    The segment is the execution uid when the block carries one, otherwise the
    slug — the same rule :func:`key_parts` spells. ``None`` when the block is
    absent or carries empty parts, so a caller can compose the key with
    ``run_key(*parts)`` and never re-spell ``uid or slug``. Lenient
    (:func:`key_parts_lenient`): a registry entry is a value read, and one
    malformed entry must cost its own row, not the fleet view.
    """
    run = (entry or {}).get("run")
    if not isinstance(run, dict):
        return None
    return key_parts_lenient(run)


def forget(
    host: str, project: str, slug: Optional[str] = None, *, uid: Optional[str] = None
) -> bool:
    """Stop tracking a run. Returns whether it had been tracked.

    Addressed by uid or by slug (spec §6.1). The only removal path. Sessions
    ending never remove a run: a run that exited to ask a question is precisely
    the one that must stay visible.

    The run's title and description go with it. They describe a run in *this*
    orchestrator's fleet (:mod:`lmer_platform.meta`), so leaving them behind
    would leave state that no view lists and no verb can reach — and an operator
    who forgets a run has said they are done with what this orchestrator knew
    about it.
    """
    segment = _segment(slug, uid)
    key = run_key(host, project, segment)
    with mutating(_index_path()):
        snapshot = _snapshot()
        runs = snapshot["runs"]
        if key not in runs:
            return False
        runs.pop(key)
        forgotten = {tuple(parts) for parts in snapshot["forgotten"]}
        forgotten.add((host.strip(), project.strip(), segment.strip()))
        _save_index(runs, forgotten)
    # Best effort, and deliberately after the index write — and outside the lock,
    # because ``meta.drop`` takes its own file's: the run *is* forgotten
    # by the time this runs, so raising here would report a failure of something
    # that succeeded. An orphaned description is worth a log line, not an error.
    try:
        _meta().drop(host, project, segment)
    except StoreError as exc:
        logger.warning("platform_run_meta_orphaned key=%s error=%s", key, exc)
    # And the threads it was pointed at, for the same reason and on the same
    # terms (:mod:`lmer_platform.references`).
    try:
        _references().drop(host, project, segment)
    except StoreError as exc:
        logger.warning("platform_run_references_orphaned key=%s error=%s", key, exc)
    logger.info("platform_run_forgotten key=%s", key)
    return True


def keys(entries: Iterable[TrackedRun]) -> set:
    """The ``(host, project, key segment)`` tuples for *entries*."""
    return {(e.host, e.project, _segment(e.slug, e.uid)) for e in entries}


# --- the key transition's storage (spec §6.1, astra R3) ----------------------
#
# What :mod:`lmer_platform.run_transition` keeps beside an entry, and nothing
# about *deciding* a transition: that module reads the mirror and the registry,
# this file only owns ``runs.json``. Fields an entry may carry, none of which
# :func:`track` writes (it carries them along untouched, except that an explicit
# note replaces a kept ``note`` pair):
#
# - ``transition`` — a :class:`Transition` record while one is in flight;
# - ``aliases`` — the keys the entry was known by before, which
#   :func:`get_tracked` follows so a stale client reference still resolves;
# - ``unresolved`` — ``{"candidates": [{uid, started_at, name}, ...]}`` on a
#   slug-keyed entry whose group holds schema-2 records none of which is
#   provably the one it meant; ``conflict`` — ``{"stores": [...]}`` when a carry
#   kept two differing values (:func:`merge_values`), cleared as
#   :func:`settle_conflict` says;
# - ``carried`` — ``{field: {"old", "new"}}`` for an index field two merged
#   entries held differently (:func:`begin_transition`).


@contextlib.contextmanager
def transition_lock():
    """The serialization a key transition runs under.

    The same pair :func:`track` takes, in the same order — the operation lock,
    then this index's — so a transition and an adopt or spawn cannot interleave
    their rewrites of the index, and no new lock order is introduced. Both are
    re-entrant, so the helpers below may take the index lock again inside it.
    """
    with projects.operation(), mutating(_index_path()):
        yield


def begin_transition(host: str, project: str, slug: str, uid: str) -> Optional[Transition]:
    """Re-key the slug-keyed entry to its uid and record a pending transition.

    One index write does both, so there is no state in which the entry has
    moved and no record says what is left to carry. Returns ``None`` when no
    slug-keyed entry exists to move.

    The old key joins ``aliases``. When an entry already sits under the uid key
    (the record was adopted by uid too) the two merge by :func:`merge_values`
    over :data:`INDEX_CARRIED_FIELDS` and the stamps; identity (``host``,
    ``project``, ``slug``, ``uid``) and ``source`` are the uid entry's own, and
    any of them it lacks comes from the old entry. A carried field both hold
    with different values cannot stay in place as an ``{"old", "new"}`` pair —
    every reader of the index takes it as text — so the uid entry's value stays
    in place and the pair is kept under ``carried`` (never dropped). Only a
    differing ``note``, the operator's own text, marks the entry ``conflict``
    with the store ``index``; the rest are launch facts of two sessions of one
    group, which differ as a matter of course. ``carried.last_session_id.old``
    is what lets :mod:`lmer_platform.run_transition` still canonicalize a live
    registry entry the old slug entry named.
    """
    if not (isinstance(uid, str) and UID_RE.match(uid)):
        raise RunIndexError(f"uid must be 32 lowercase hex, got {uid!r}")
    old_key = run_key(host, project, slug)
    new_key = run_key(*key_parts({"host": host, "project": project, "slug": slug, "uid": uid}))
    with mutating(_index_path()):
        snapshot = _snapshot()
        runs = snapshot["runs"]
        old = runs.get(old_key)
        if not isinstance(old, dict):
            return None
        moved = dict(old)
        moved.pop("unresolved", None)
        moved.pop("transition", None)
        moved["uid"] = uid
        existing = runs.get(new_key)
        entry = _merged_entry(moved, existing) if isinstance(existing, dict) else moved
        aliases = [a for a in (entry.get("aliases") or []) + (old.get("aliases") or [])
                   if isinstance(a, str)]
        if old_key not in aliases:
            aliases.append(old_key)
        entry["aliases"] = aliases
        transition = Transition(old_key=old_key, new_key=new_key)
        entry["transition"] = transition.to_dict()
        runs.pop(old_key)
        runs[new_key] = entry
        _save_index(runs, snapshot["forgotten"])
    logger.info("platform_run_transition_begun old=%s new=%s", old_key, new_key)
    return transition


def _merged_entry(moved: dict, existing: dict) -> dict:
    """The uid entry *existing* after the slug entry *moved* lands on it."""
    entry = dict(existing)
    for name, value in moved.items():
        if name in ("aliases", "conflict", "carried"):
            continue
        if entry.get(name) is None:
            entry[name] = value
    policy = set(INDEX_CARRIED_FIELDS) | BOOKKEEPING_STAMPS
    merged, _ = merge_values(
        {k: v for k, v in moved.items() if k in policy},
        {k: v for k, v in existing.items() if k in policy},
    )
    carried = _carried_of(existing)
    for k, v in (_carried_of(moved)).items():
        for pair in _pairs(v):
            _keep_pair(carried, k, pair)
    for name, value in merged.items():
        if is_conflict_pair(value):
            _keep_pair(carried, name, value)
            entry[name] = value["new"]
        else:
            entry[name] = value
    if carried:
        entry["carried"] = carried
    if "note" in carried:
        entry["conflict"] = _with_conflict_stores(existing.get("conflict"), ["index"])
    return entry


def _pairs(value: object) -> list:
    """The ``{"old", "new"}`` pairs a ``carried`` field holds: one, or a list."""
    items = value if isinstance(value, list) else [value]
    return [item for item in items if is_conflict_pair(item)]


def _keep_pair(carried: dict, name: str, pair: Mapping) -> None:
    """Add *pair* under ``carried[name]`` without losing one already kept.

    A field carried once holds its pair; a second carry onto the same uid entry
    (another slug entry bound to it later) appends, so ``carried[name]`` becomes
    a list of pairs rather than the newer overwriting the older.
    """
    kept = _pairs(carried.get(name))
    if pair in kept:
        return
    kept.append(dict(pair))
    carried[name] = kept[0] if len(kept) == 1 else kept


def _carried_of(entry: Mapping) -> dict:
    carried = entry.get("carried")
    return dict(carried) if isinstance(carried, Mapping) else {}


def _drop_carried(entry: dict, name: str) -> None:
    """Forget the kept pair of *name* on *entry*: it has been written since."""
    carried = _carried_of(entry)
    if carried.pop(name, None) is None:
        return
    if carried:
        entry["carried"] = carried
    else:
        entry.pop("carried", None)


def _with_conflict_stores(marked: object, stores: Iterable[str]) -> dict:
    """*marked* (an entry's ``conflict``) naming *stores* too, other keys kept."""
    marked = dict(marked) if isinstance(marked, Mapping) else {}
    known = [s for s in marked.get("stores") or [] if isinstance(s, str)]
    marked["stores"] = known + [s for s in stores if s and s not in known]
    return marked


def carried_session_ids(key: str) -> list:
    """The session ids *key*'s entry names: its own, then a carried older one.

    For :mod:`lmer_platform.run_transition`'s registry step, which must
    canonicalize a live registry entry named by the slug entry's
    ``last_session_id`` even when the uid entry it merged into had its own.
    """
    entry = load_index().get(key)
    if not isinstance(entry, dict):
        return []
    ids = [entry.get("last_session_id")]
    ids += [pair["old"] for pair in _pairs(_carried_of(entry).get("last_session_id"))]
    return [i for i in dict.fromkeys(ids) if isinstance(i, str) and i]


def conflict_stores(key: str) -> list:
    """The stores *key*'s ``conflict`` mark names; empty when it has none."""
    entry = load_index().get(key)
    marked = entry.get("conflict") if isinstance(entry, dict) else None
    if not isinstance(marked, Mapping):
        return []
    return [s for s in marked.get("stores") or [] if isinstance(s, str)]


def settle_conflict(key: str, still: Iterable[str]) -> list:
    """Narrow *key*'s ``conflict`` mark to the stores in *still*; what remains.

    What clears a conflict mark (spec §6.1, the lossless policy): the operator
    re-adopts the run by uid (``POST /api/runs/adopt {uid}``) after resolving
    the kept pairs, and :func:`lmer_platform.run_transition.recheck_conflict`
    asks every store the mark names whether its value under *key* still holds
    an ``{"old", "new"}`` pair (:func:`holds_conflict`); those that do are
    *still*. A store with none left is dropped from the mark, and the mark goes
    once no store remains. A pair is resolved by writing the field it is in —
    :func:`lmer_platform.meta.write` with an explicit title replaces the title
    pair, :func:`track` with an explicit note replaces the index's ``note``
    pair — so nothing is ever cleared by a value merely being looked at. The
    ``index`` store is still conflicted while ``carried`` holds a ``note`` pair.
    """
    still = list(still)
    with mutating(_index_path()):
        snapshot = _snapshot()
        entry = snapshot["runs"].get(key)
        if not isinstance(entry, dict) or not isinstance(entry.get("conflict"), Mapping):
            return []
        known = [s for s in entry["conflict"].get("stores") or [] if isinstance(s, str)]
        remaining = [s for s in known if s in still]
        if remaining == known:
            return remaining
        if remaining:
            entry["conflict"] = {**entry["conflict"], "stores": remaining}
        else:
            entry.pop("conflict")
        _save_index(snapshot["runs"], snapshot["forgotten"])
    logger.info("platform_run_conflict_settled key=%s remaining=%s", key, remaining)
    return remaining


def index_conflicted(key: str) -> bool:
    """Whether *key*'s entry still keeps two differing notes (the ``index`` store)."""
    entry = load_index().get(key)
    return isinstance(entry, dict) and "note" in _carried_of(entry)


def mark_transition(key: str, stores: Iterable[str], *, conflict: Iterable[str] = ()) -> Optional[Transition]:
    """Record *stores* done on *key*'s pending transition, and any *conflict*.

    Written after each store moves, so a crash after it resumes at the next
    store rather than moving this one again.
    """
    with mutating(_index_path()):
        snapshot = _snapshot()
        entry = snapshot["runs"].get(key)
        if not (isinstance(entry, dict) and isinstance(entry.get("transition"), Mapping)):
            return None
        transition = Transition.from_dict(entry["transition"])
        for store in stores:
            transition.stores[store] = "done"
        entry["transition"] = transition.to_dict()
        clashed = [s for s in conflict if s]
        if clashed:
            entry["conflict"] = _with_conflict_stores(entry.get("conflict"), clashed)
        _save_index(snapshot["runs"], snapshot["forgotten"])
    return transition


def finish_transition(key: str) -> bool:
    """Clear *key*'s transition record once every store is done.

    Refuses (returns ``False``) while a store is still pending: the record is
    what a restart resumes from, so it must outlive every unfinished store.
    """
    with mutating(_index_path()):
        snapshot = _snapshot()
        entry = snapshot["runs"].get(key)
        if not (isinstance(entry, dict) and isinstance(entry.get("transition"), Mapping)):
            return False
        if Transition.from_dict(entry["transition"]).pending_stores():
            return False
        entry.pop("transition")
        _save_index(snapshot["runs"], snapshot["forgotten"])
    logger.info("platform_run_transition_done key=%s", key)
    return True


def pending_transitions() -> list:
    """``[(key, Transition)]`` for every entry with a transition still pending."""
    pending = []
    for key, entry in load_index().items():
        record = entry.get("transition") if isinstance(entry, dict) else None
        if not isinstance(record, Mapping):
            continue
        try:
            transition = Transition.from_dict(record)
        except RunIndexError as exc:
            logger.warning("platform_run_transition_malformed key=%s error=%s", key, exc)
            continue
        if not transition.done:
            pending.append((key, transition))
    return pending


def mark_unresolved(key: str, candidates: list) -> bool:
    """Mark *key*'s entry unresolved with *candidates*; whether anything changed.

    Writes only on a change, so a state build that finds the same group again
    leaves the index alone.
    """
    with mutating(_index_path()):
        snapshot = _snapshot()
        entry = snapshot["runs"].get(key)
        if not isinstance(entry, dict):
            return False
        marked = {"candidates": list(candidates)}
        if entry.get("unresolved") == marked:
            return False
        entry["unresolved"] = marked
        _save_index(snapshot["runs"], snapshot["forgotten"])
    logger.info("platform_run_unresolved key=%s candidates=%d", key, len(candidates))
    return True


def readopt_marks() -> dict:
    """``{key: mark}`` for every entry a fleet row must ask to re-adopt by uid.

    A mark is ``{"reason", "candidates"}`` for an unresolved entry and
    ``{"reason", "stores"}`` for one whose carry kept two differing values; the
    reason is :data:`READOPT_BY_UID`, qualified for a conflict
    (``re-adopt by uid: conflicting meta state``).
    """
    marks = {}
    for key, entry in load_index().items():
        if not isinstance(entry, dict):
            continue
        unresolved = entry.get("unresolved")
        if isinstance(unresolved, Mapping):
            candidates = unresolved.get("candidates")
            marks[key] = {
                "reason": READOPT_BY_UID,
                "candidates": list(candidates) if isinstance(candidates, list) else [],
            }
            continue
        conflict = entry.get("conflict")
        if isinstance(conflict, Mapping):
            stores = [s for s in conflict.get("stores") or [] if isinstance(s, str)]
            marks[key] = {
                "reason": f"{READOPT_BY_UID}: conflicting {', '.join(stores) or 'carried'} state",
                "stores": stores,
            }
    return marks
