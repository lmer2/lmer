"""Which runs belong together, so the operator can switch between them (T53).

The motivating case is the followup cycle. An orchestrating assistant starts a
``develop`` run, and later a ``review`` run against the same target; from then on
the operator moves between those two repeatedly — read the review, go back to the
run being reviewed, come back. Nothing in the fleet view knew they had anything
to do with each other, so every crossing was a scroll through a list of runs
whose names differ by one word. A relation makes it two taps.

The relation is **symmetric and unlabelled**. "Switch between them" is what it is
for, and switching has no direction: from the review you want the run under
review, and from that run you want its review. There is deliberately no "kind"
field either — every name for one (``review_of``, ``parent``) is directional, and
storing a direction that nothing reads would be inventing a second, weaker idea
next to the one that was asked for.

Why this is platform state and not run state
--------------------------------------------
The same constraint :mod:`lmer_platform.meta` is built around, and it applies
unchanged (spec D3): the platform never writes run state, because its copy of the
work repo is a **read-only mirror the daemon force-resets on every pull**
(``git reset --hard FETCH_HEAD`` in :mod:`lmer_platform.workrepo`). A relation
written beside ``state.yaml`` would survive until the next fetch and then vanish
with no error anywhere. So it lives here, with the consequence stated rather than
discovered:

    **Relations are local to one orchestrator.** They do not travel with the
    runs. Another operator's fleet view does not have them, the agent inside the
    container cannot see them, and a second orchestrator of your own — a laptop
    as well as a server — has its own unrelated set.

Stored once under a canonical pair key, not on both runs
--------------------------------------------------------
The obvious shape is a list of related keys on each run, mirrored on write. It
was rejected because **it makes symmetry a property of a write path instead of a
property of the data**, and the write path is not the only writer: these files
are plain JSON precisely so an operator can repair them in an editor when the
orchestrator is wedged (spec D2). A hand-added entry on one side of a mirrored
store is a relation that A shows and B does not — the exact one-sided failure the
feature must not have — and a hand-removed one is worse, because the surviving
half still links to a run that no longer links back. Deleting has the same
problem twice over: two entries to find, and a half-finished delete leaves a
dangling link that reads as a live relation.

Under one canonical key — the two run keys sorted and joined with
:data:`PAIR_SEPARATOR`, the ``"<id>+<id>"`` spelling ``work_repo.plan_index``
already uses for its ``shared_files`` pairs — none of that is expressible. One
entry *is* the relation; both runs find it by looking for themselves in it
(:func:`list_for` indexes on read); removing it removes both directions because
there is only ever one thing to remove; and relating A to B twice, in either
order, lands on the same key and cannot double.

The cost is a scan per read instead of a lookup. It is not a real cost: the whole
set is one small snapshot file read once per request, bounded by
:data:`MAX_RELATIONS_PER_RUN` per run, and every other consumer of platform state
already reads whole snapshots this way (:func:`lmer_platform.runs.list_tracked`).

Why the entry names its runs in full instead of only in its key
---------------------------------------------------------------
Because a run key is not losslessly splittable. It is
``host/project/slug`` and a project is ``group/subgroup``, so recovering the
three parts from the string requires guessing where the project ends — and any
guess is wrong for some legitimate slug. The key is the pair's *identity* (dedup,
removal, the guarantee of symmetry); the ``runs`` list inside the entry is the
data. They must agree: an entry whose runs do not rebuild its own key is treated
as malformed and skipped, so a hand edit that changes one and not the other
degrades to a missing relation rather than to a link pointing at the wrong run.

A relation to a run this fleet does not track
---------------------------------------------
Allowed, on purpose, and **kept rather than pruned on read**. Two reasons:

- Relating ahead of adoption is legitimate. An assistant that has just started a
  review run may relate it before the spawn's ``track()`` has been reconciled, or
  relate a run it intends to adopt next; refusing would make correctness depend
  on ordering the caller cannot see.
- A read that deletes is neither loud nor quiet. :mod:`lmer_platform.store`'s
  whole convention is that writes are loud and reads are quiet, and pruning on
  read is a silent write — it would throw away a deliberate statement by an
  assistant the first time anything looked at the run, and a run forgotten in one
  tab would lose its relations to a refresh in another. A run that is re-adopted
  gets its relations back precisely because nothing pruned them.

So :class:`RelatedRun` carries ``tracked``, and the UI says "not on this host"
next to the key rather than offering a switch that would land on a blank view.

For the same reason :func:`lmer_platform.runs.forget` deliberately does **not**
drop relations, which is the opposite of what it does with
:mod:`lmer_platform.meta`: a title describes a run in this fleet and dies with
it, while a relation is a statement about *two* runs and is still true — and
still useful to the other end — after one of them is forgotten. The accepted cost
is that a relation between two runs that are both forgotten is reachable only by
editing the file, since no run's detail view lists it any more. It is two lines of
JSON, ``POST /api/runs/unrelate`` removes it without adopting anything, and the
alternative destroys relations an operator meant to keep.

Trust
-----
Relations are writable over the API by an agent holding the shared secret, so
requests are validated and bounded: both ends must parse as run keys
(:func:`lmer_platform.runs.run_key`, never a second grammar), a run cannot be
related to itself, and each run carries at most
:data:`MAX_RELATIONS_PER_RUN`. Nothing here is free text, so there is nothing to
strip — the one thing an agent could otherwise do to this file is fill it.

Writes are loud and reads are quiet, the same split
:mod:`lmer_platform.store` makes: a refused relation must reach whoever asked for
it, while an unreadable snapshot must not take a run's detail view down with it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .runs import RunIndexError, load_index, run_key
from .store import (
    StoreError, mutating, read_json, snapshot_path, utc_now_iso, write_json,
)

logger = logging.getLogger("lmer_platform.relations")

__all__ = [
    "RELATIONS_FILE", "PAIR_SEPARATOR", "MAX_RELATIONS_PER_RUN", "LOCALITY_NOTE",
    "RelationError", "RelatedRun",
    "load_all", "list_for", "relate", "unrelate",
]

RELATIONS_FILE = "relations.json"

#: Joins the two run keys of a pair, sorted, into the one key the pair is stored
#: under. The ``"<id>+<id>"`` spelling ``work_repo.plan_index`` uses for its
#: ``shared_files`` allowlist, so an operator reading either file reads one
#: convention. A run key containing it is refused rather than encoded: the
#: separator is what makes the stored key decodable at a glance, and two keys that
#: happen to contain one could otherwise produce the same pair key and silently
#: overwrite each other's relation.
PAIR_SEPARATOR = "+"

#: Ceiling per run, and it is a UI bound as much as a storage one. This element is
#: always visible at the bottom of a run's page, so twenty is already more than
#: fits a phone without becoming the page — and a run "related" to fifty others is
#: not a switcher, it is a second fleet view with none of the fleet view's
#: sorting. Bounded here rather than trusted because an agent writes these.
MAX_RELATIONS_PER_RUN = 20

#: Said in every reply that carries relations, for the reason
#: :data:`lmer_platform.meta.LOCALITY_NOTE` is: the surprising half of the feature
#: is what it is *not* — a property of the runs. Prose rather than a flag alone,
#: since a flag only means something to a reader who already knows.
LOCALITY_NOTE = (
    "relations are this orchestrator's own view of which runs belong together. "
    "They live in platform state, never in the work repo (spec D3, whose mirror is "
    "force-reset on every pull), so they are invisible to anyone else's fleet view "
    "and to the agent in the container. A related run that this orchestrator does "
    "not track is reported with tracked=false rather than hidden: relating a run "
    "before adopting it is allowed, and forgetting a run does not remove its "
    "relations."
)


class RelationError(RuntimeError):
    """A refusal, carrying the status a route should answer.

    Same convention as :mod:`lmer_platform.meta` and
    :mod:`lmer_platform.lifecycle`: the status rides on the exception so the routes
    get one handler and a refusal added later arrives with its own code instead of
    falling through to a 500 with a traceback.

    Everything here is 400 and there is deliberately no 404 sibling: an untracked
    run is not an error in this module (see the header), so the only refusals are
    about the request itself.
    """

    status = 400


@dataclass(frozen=True)
class RelatedRun:
    """The *other* run of one relation, as one run's page sees it.

    ``tracked`` is not a property of the relation — it is what this orchestrator
    currently knows — and it is on here because it decides how the run is rendered:
    a tracked run is a switch, an untracked one is a key with a hint. A client that
    had to ask separately would either make a request per relation or guess.
    """

    host: str
    project: str
    slug: str
    tracked: bool

    @property
    def key(self) -> str:
        return run_key(self.host, self.project, self.slug)

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "project": self.project,
            "slug": self.slug,
            # The composite key as well as its parts: the parts are what a client
            # opens the run with, and the key is what it shows for a run there is
            # nothing else to say about — which is exactly the untracked case.
            "key": self.key,
            "tracked": self.tracked,
        }


def _relations_path():
    return snapshot_path(RELATIONS_FILE)


def load_all() -> dict:
    """Raw ``{pair_key: entry}`` mapping. Empty when nothing is related yet."""
    try:
        stored = read_json(_relations_path())
    except StoreError as exc:
        # A corrupt file must not take a run's whole detail view with it; the bad
        # bytes have already been moved aside for post-mortem.
        logger.error(
            "platform_run_relations_unreadable error=%s — treating as empty", exc
        )
        return {}
    if not stored:
        return {}
    entries = stored.get("relations")
    return entries if isinstance(entries, dict) else {}


def _save(entries: dict) -> None:
    write_json(_relations_path(), {"relations": entries})


def _validated(run, *, field: str) -> tuple:
    """One ``(host, project, slug)`` triple, stripped. Refused, never coerced.

    ``run_key``'s own refusal names the offending part, and this prefixes it with
    which of the two runs it was about — the one thing that message cannot know
    and the reader most needs, since every request here carries two runs with the
    same three field names.
    """
    # A string is refused before it is unpacked, because a three-character one
    # unpacks into three parts: ``"abc"`` would otherwise relate a run called
    # ``a/b/c``. The mistake this catches is the composite key sent as one field,
    # which is the natural one — it is the shape every reply shows.
    if isinstance(run, str) or not isinstance(run, (tuple, list)):
        raise RelationError(
            f"{field} must name a run as host, project and slug, got {run!r}"
        )
    try:
        host, project, slug = run
    except ValueError as exc:
        raise RelationError(
            f"{field} must name a run as host, project and slug, got {run!r}"
        ) from exc
    try:
        run_key(host, project, slug)
    except RunIndexError as exc:
        raise RelationError(f"{field}: {exc}") from exc
    return (host.strip(), project.strip(), slug.strip())


def _pair_key(key_a: str, key_b: str) -> str:
    """The one key a relation between two runs is stored under, whichever way round.

    Sorted, so ``relate(a, b)`` and ``relate(b, a)`` are the same entry: that is
    what makes the store symmetric by shape rather than by a write path
    remembering to do two things.
    """
    for key in (key_a, key_b):
        if PAIR_SEPARATOR in key:
            raise RelationError(
                f"cannot relate {key!r}: a run key containing {PAIR_SEPARATOR!r} "
                "cannot be told apart from the pair of keys it would be stored "
                "under, and two such runs could silently overwrite each other's "
                "relation"
            )
    low, high = sorted((key_a, key_b))
    return f"{low}{PAIR_SEPARATOR}{high}"


def _members(pair: str, payload) -> Optional[tuple]:
    """The two runs one entry names, or ``None`` when the entry is unusable.

    Tolerant for :meth:`lmer_platform.runs.TrackedRun.from_dict`'s reason: one
    hand-edited or version-skewed entry must not empty every run's relations. The
    key check at the end is the one specific to this file — the pair key is the
    identity and the ``runs`` list is the data, so an entry where they disagree is
    ambiguous about *which run it points at*, and a guess there is a link to the
    wrong run.
    """

    def unusable(reason: str) -> None:
        logger.warning(
            "platform_run_relation_malformed pair=%s reason=%s", pair, reason
        )
        return None

    if not isinstance(payload, dict):
        return unusable("entry is not a mapping")
    refs = payload.get("runs")
    if not isinstance(refs, list) or len(refs) != 2:
        return unusable("entry does not name exactly two runs")

    triples = []
    for ref in refs:
        if not isinstance(ref, dict):
            return unusable("a run in the entry is not a mapping")
        try:
            run_key(ref.get("host"), ref.get("project"), ref.get("slug"))
        except RunIndexError as exc:
            return unusable(str(exc))
        triples.append((
            ref["host"].strip(), ref["project"].strip(), ref["slug"].strip(),
        ))

    keys = [run_key(*triple) for triple in triples]
    if keys[0] == keys[1]:
        return unusable("a run related to itself is not a switch target")
    try:
        canonical = _pair_key(*keys)
    except RelationError as exc:
        return unusable(str(exc))
    if canonical != pair:
        return unusable(f"the runs it names belong under {canonical}")
    return triples[0], triples[1]


def _neighbours(entries: dict, key: str) -> list:
    """Every run related to *key*, read out of the pair-keyed store.

    This is the "index on read" half of storing each relation once. Sorted by the
    other run's key so a page renders the same order twice — the entries come out
    of a JSON mapping, whose order is insertion order and therefore whichever way
    the operator happened to relate things.
    """
    found = []
    for pair, payload in entries.items():
        members = _members(pair, payload)
        if members is None:
            continue
        first, second = members
        if run_key(*first) == key:
            found.append(second)
        elif run_key(*second) == key:
            found.append(first)
    found.sort(key=lambda triple: run_key(*triple))
    return found


def list_for(host: str, project: str, slug: str) -> list:
    """The runs related to this one. Empty for most runs, which is not an error.

    Quiet about scope like :func:`lmer_platform.meta.read`, and one step further:
    neither this run nor the ones it names has to be tracked. The caller asked
    which runs this one is related to, and the answer is the answer whatever this
    orchestrator's index currently holds.
    """
    key = run_key(host, project, slug)
    # The index's keys, read once, rather than a ``get_tracked`` per neighbour:
    # ``tracked`` decides whether the UI offers a switch, and "does this fleet
    # carry this run at all" is exactly what that needs. A malformed index entry
    # counting as tracked is the harmless direction — it costs a switch to a run
    # whose row is already broken, not a hidden relation.
    tracked_keys = set(load_index())
    return [
        RelatedRun(
            host=host_, project=project_, slug=slug_,
            tracked=run_key(host_, project_, slug_) in tracked_keys,
        )
        for host_, project_, slug_ in _neighbours(load_all(), key)
    ]


def relate(run, related) -> bool:
    """Relate two runs. Returns whether this created a relation.

    Two positional runs rather than one run and three keyword parts, because the
    relation is symmetric: the arguments are two things of the same kind, and a
    signature that made one of them the subject would suggest a direction the
    store does not have. Each is a ``(host, project, slug)`` triple — the spelling
    :func:`lmer_platform.runs.run_key` takes, never a second grammar.

    Idempotent, and in either order: relating a pair that is already related
    changes nothing and returns ``False``. That is what makes this safe for an
    assistant to call without reading first, and it is why the cap below is
    checked *after* the duplicate — re-stating an existing relation must not be
    refused for a limit it does not move.

    Neither run has to be tracked. Relating ahead of adoption is legitimate (see
    the module header), so the only refusals are about the request: a part that is
    not a run key, a run related to itself, and the per-run cap.
    """
    first = _validated(run, field="run")
    second = _validated(related, field="related run")
    key_first, key_second = run_key(*first), run_key(*second)
    if key_first == key_second:
        raise RelationError(
            f"{key_first} cannot be related to itself: a relation is a run to "
            "switch to, and this one is the run you are on"
        )
    pair = _pair_key(key_first, key_second)

    # The per-run cap is counted from the same read that the write is derived
    # from, which is only true while the two are one operation: two relates
    # landing together would each see room for one more and both take it.
    with mutating(_relations_path()):
        entries = load_all()
        if pair in entries:
            return False
        for triple, key in ((first, key_first), (second, key_second)):
            current = len(_neighbours(entries, key))
            if current >= MAX_RELATIONS_PER_RUN:
                raise RelationError(
                    f"{key} already has {current} related runs and the limit is "
                    f"{MAX_RELATIONS_PER_RUN} — remove one "
                    "(POST /api/runs/unrelate) before adding another. Related "
                    "runs are for switching between a handful of runs that "
                    "belong together, not for grouping a fleet"
                )

        entries[pair] = {
            "runs": [
                {"host": first[0], "project": first[1], "slug": first[2]},
                {"host": second[0], "project": second[1], "slug": second[2]},
            ],
            "created_at": utc_now_iso(),
        }
        _save(entries)
    logger.info("platform_run_related pair=%s", pair)
    return True


def unrelate(run, related) -> bool:
    """Remove a relation. Returns whether there was one.

    Takes its two runs in either order for :func:`_pair_key`'s reason, and removes
    the single entry that *is* the relation — so both directions go at once, and
    there is no half-removed state in which one run still links to the other.

    ``False`` rather than a refusal for a pair that is not related: the caller
    asked for the relation to be gone, and it is.
    """
    first = _validated(run, field="run")
    second = _validated(related, field="related run")
    key_first, key_second = run_key(*first), run_key(*second)
    if key_first == key_second:
        raise RelationError(
            f"{key_first} cannot be related to itself, so there is no relation "
            "between it and itself to remove"
        )
    pair = _pair_key(key_first, key_second)

    with mutating(_relations_path()):
        entries = load_all()
        if pair not in entries:
            return False
        entries.pop(pair)
        _save(entries)
    logger.info("platform_run_unrelated pair=%s", pair)
    return True
