"""Bind the slug-keyed entries a pre-A2 daemon left for schema-2 groups.

Why this exists
---------------
Before the run index learned execution uids (spec §6.1), a schema-2 launch was
tracked under ``host/project/<slug>`` like every other run, and its record was
found by walking the slug's ``_runs/<slug>/`` group and taking the newest record.
That is wrong the moment a group holds two launches: the older row silently
shows the newer launch's record. A daemon upgraded onto such an index must not
keep those semantics for one more build (spec §12), so the first state build
after the upgrade — and every ``POST /api/rescan`` — runs :func:`reconcile`
from :func:`lmer_platform.api.build_state`.

For each tracked entry with no uid whose slug group holds schema-2 records
(:func:`lmer_platform.workrepo.group_records`; a record is schema-2 when its
directory names a uid, :attr:`lmer_platform.workrepo.RunDirRef.uid`) it either
**binds** the entry to one record, when that is provable, or marks it
**unresolved** with the group's uid records as candidates. It never falls back
to "the newest record in the group". Records without a uid are never touched,
and an entry whose group holds none is left exactly as it was — a schema-1 run
keeps its slug key.

When binding is provable
------------------------
1. Exactly one record in the group carries launcher facts that **match** the
   registry entry of the tracked ``last_session_id`` (below), and that record
   has a uid; or
2. the group holds exactly one record, it is schema-2, and the registry entry
   does not **contradict** it.

"Exactly one schema-2 record" counts the whole group, so a MIXED group (one
schema-1 record beside one uid record) stays unresolved: the uid record being
the only schema-2 one does not say which execution the old row meant. Rule 2
yields to a contradiction because a slug row whose last session is still
running, with a record not yet pushed, would otherwise be bound to the group's
older launch; the entry stays unresolved and the next build re-examines it,
by which time the session's own record may have landed and matched by rule 1.

The match predicate
-------------------
:func:`launcher_verdict` compares a record's ``state.session`` with the
registry entry (:func:`lmer_platform.registry.read_session`) of the tracked
entry's ``last_session_id``, and answers ``True`` (match), ``False``
(contradiction) or ``None`` (proves nothing):

- no registry entry → ``None``. A clean exit removes the entry, so its absence
  says nothing about which record was the run's;
- the entry's ``run`` block does not name this tracked ``{host, project, slug}``
  → ``None``: it is another run's session;
- the entry's ``run.uid`` is set (a launch by a post-A2 daemon) → ``True`` for
  that record and ``False`` for any other;
- the record's ``session.launcher`` lacks an integer ``pid`` or a ``hostname``
  → ``None``: a record written before the launcher block existed;
- a ``launcher.pid`` other than the entry's ``pid``, or a ``launcher.hostname``
  other than this host (``socket.gethostname()`` — the registry is host-local,
  so its host is the daemon's; the host-side ``lmer`` the daemon spawns is the
  process that composes ``launcher``, and its pid is the one the registry
  records) → ``False``;
- either ``started_at`` (the launcher's, the entry's) missing or unparseable
  → ``None``. pid and hostname alone never match: a pid is recycled, and on a
  long-lived host (``pid_max`` 32768) a later launch in the same group can draw
  the pid of an entry whose session died uncleanly;
- otherwise ``True`` exactly when the two stamps lie within
  :data:`CLOCK_SKEW_SECONDS` of each other, either way, and ``False`` when they
  do not — same pid, another launch, which is a contradiction as much as a
  differing pid is (and, unlike ``None``, keeps rule 2 from binding that launch
  when it is the group's only record).

Binding is a durable, resumable transition
------------------------------------------
Under the operation serialization :func:`lmer_platform.runs.track` already
takes (:func:`lmer_platform.runs.transition_lock`), one index write re-keys the
entry to ``host/project/<uid>``, records the old key as an alias that
:func:`lmer_platform.runs.get_tracked` follows, and writes a ``pending``
:class:`lmer_platform.runs.Transition` with every store pending. Each store is
then moved and marked done in its own write, in
:data:`lmer_platform.runs.TRANSITION_STORES` order: meta, relations, check-in
marks, references, the project home together with the durable thread owners
(:func:`lmer_platform.projects.rekey` moves both in one transaction, so both
names are marked done by one call), and last the registry entry. The record is
cleared only when every store is done. :func:`reconcile` resumes every entry
with a ``pending`` transition before it looks for new work, and resumes only
the stores still pending — the record, not each store's own idempotence, is
what prevents a store being moved twice.

The registry step canonicalizes the entry of the tracked ``last_session_id``
when it still names ``{host, project, slug}`` with no uid and the predicate does
not contradict the bound record: its ``run`` block gains the uid, so the
inventory joins one row rather than a uid record row beside a slug session row.
A crashed session's retained entry is canonicalized the same way, for the same
reason.

A carry that meets a differing value under the new key keeps both
(:func:`lmer_platform.runs.merge_values`) and the entry is marked ``conflict``
with the stores that clashed; the fleet row then says
``re-adopt by uid: conflicting <store> state``. A store answers ``conflict``
from what is on disk under the new key, not only from what one call merged
(:func:`lmer_platform.runs.holds_conflict`): a crash between a store's write
and :func:`lmer_platform.runs.mark_transition` resumes into a ``rekey`` that
finds the old key gone, and it must still report the pair it left. Re-adopting
by uid runs :func:`recheck_conflict`, which clears the mark for every store no
longer holding a pair (:func:`lmer_platform.runs.settle_conflict`). An
unresolved entry's row says ``re-adopt by uid`` and lists its candidates
(``uid``, ``started_at``, ``name``); :func:`lmer_platform.api.build_state`
resolves such an entry to no record at all, and :func:`bind` — behind
``POST /api/runs/adopt {uid}`` — binds it.

Cost: an entry that carries a uid and no pending transition is skipped without
reading anything. A uid-less entry is examined against the mirror, and that
answer is remembered per mirror HEAD (:func:`_unchanged_since`): a group with no
uid-named record — every schema-1 run, which never gains a uid — is not walked
again until the mirror moves, and an unresolved entry is not re-read while
neither the mirror nor its registry entry has changed. With no readable HEAD
nothing is remembered and every build examines every uid-less entry.
"""

from __future__ import annotations

import logging
import socket
from datetime import datetime, timezone
from typing import Optional

from lmer_cli import runref

from . import checkin, meta, projects, references, registry, relations, runs, workrepo
from .config import PlatformConfig

logger = logging.getLogger("lmer_platform.run_transition")

__all__ = [
    "CLOCK_SKEW_SECONDS", "reconcile", "bind", "settle", "launcher_verdict",
    "readopt_marks", "recheck_conflict",
]

#: How far a launcher's ``started_at`` may precede its registry entry's and
#: still be the same launch. The two stamps come from one host's clock, but from
#: two processes, and the registry's is taken before the child has run at all.
CLOCK_SKEW_SECONDS = 60

#: The stores :func:`lmer_platform.projects.rekey` moves in one call.
_PROJECT_STORES = ("projects", "threads")

#: Each per-run store's module. Its ``rekey`` and ``conflicted`` are attribute
#: lookups at call time, so a module's current function is the one called.
_STORE_MODULES = {
    "meta": meta,
    "relations": relations,
    "checkin": checkin,
    "references": references,
}

#: ``{entry key: what it was examined against}`` (:func:`_examined`) for uid-less
#: entries whose examination left them where they were (see "Cost" above).
_EXAMINED: dict = {}
_UNREAD = object()


def readopt_marks() -> dict:
    """The fleet rows to mark ``re-adopt by uid`` (:func:`lmer_platform.runs.readopt_marks`)."""
    return runs.readopt_marks()


def reconcile(config: PlatformConfig) -> dict:
    """Finish pending transitions, then bind or mark every eligible entry.

    Returns ``{"resumed", "bound", "unresolved"}`` lists of keys, for logging
    and tests. Never raises: a transition that fails part-way keeps its pending
    record for the next build, and one entry's failure must not cost the fleet
    view.
    """
    summary = {"resumed": [], "bound": [], "unresolved": []}
    for key, _transition in runs.pending_transitions():
        try:
            if _resume(config, key):
                summary["resumed"].append(key)
        except Exception as exc:  # noqa: BLE001 — the record survives for the next build
            logger.warning("platform_run_transition_failed key=%s error=%s", key, exc)
    head = None
    uidless = set()
    for entry in runs.list_tracked():
        if entry.uid is not None:
            continue
        uidless.add(entry.key)
        if head is None:
            head = _mirror_head(config) or ""
        try:
            outcome = _settle(config, entry, head)
        except Exception as exc:  # noqa: BLE001 — same reason as above
            logger.warning("platform_run_transition_failed key=%s error=%s", entry.key, exc)
            continue
        if outcome is not None:
            summary[outcome].append(entry.key)
    # Bound by the index: an entry bound, forgotten or re-keyed since is gone.
    for key in [key for key in _EXAMINED if key not in uidless]:
        _EXAMINED.pop(key, None)
    return summary


def _mirror_head(config: PlatformConfig) -> Optional[str]:
    """The mirror and its HEAD, which every answer :func:`_settle` remembers is
    keyed by; ``None`` when there is no HEAD to key by."""
    try:
        sha = workrepo.mirror_status(config).head_sha
    except Exception:  # noqa: BLE001 — no HEAD only means nothing is remembered
        return None
    return f"{config.mirror_path}@{sha}" if sha else None


def _unchanged_since(entry: runs.TrackedRun, head: str, session_fingerprint) -> bool:
    """Whether *entry* was examined against this HEAD and registry state already.

    ``first_seen`` is part of the answer so a run forgotten and tracked again —
    a fresh entry under the same key, without the mark the old one carried — is
    examined afresh.
    """
    return bool(head) and _EXAMINED.get(entry.key) == _examined(entry, head, session_fingerprint)


def _remember(entry: runs.TrackedRun, head: str, session_fingerprint) -> None:
    if head:
        _EXAMINED[entry.key] = _examined(entry, head, session_fingerprint)


def _examined(entry: runs.TrackedRun, head: str, session_fingerprint) -> tuple:
    return (head, entry.first_seen, entry.last_session_id, session_fingerprint)


def bind(config: PlatformConfig, host: str, project: str, slug: str, uid: str) -> runs.TrackedRun:
    """Bind the slug-keyed entry ``host/project/slug`` to the record *uid*.

    The operator's answer to an unresolved row (``POST /api/runs/adopt {uid}``):
    the same transition :func:`reconcile` runs when binding is provable. *uid*
    must name a record of this project in the mirror. An entry already bound
    to *uid* (its old key is an alias) is finished if pending and returned.
    """
    if not (isinstance(uid, str) and runs.UID_RE.match(uid)):
        raise runs.RunIndexError(f"uid must be 32 lowercase hex, got {uid!r}")
    old_key = runs.run_key(host, project, slug)
    tracked = runs.get_tracked(host, project, slug)
    if tracked is None:
        raise runs.RunIndexError(f"run {old_key} is not tracked")
    if tracked.uid is not None:
        if tracked.uid != uid:
            raise runs.RunIndexError(f"run {old_key} is already bound to {tracked.uid}")
        _resume(config, tracked.key)
        return runs.get_tracked(host, project, uid=uid)
    if workrepo.resolve_run_dir(config, host, project, uid=uid) is None:
        raise runs.RunIndexError(f"no record {uid} for {host}/{project} in the mirror")
    _bind(config, tracked, uid)
    return runs.get_tracked(host, project, uid=uid)


def settle(config: PlatformConfig, host: str, project: str, slug: str) -> Optional[str]:
    """Examine the uid-less entry ``host/project/slug`` now, as :func:`reconcile` would.

    For a caller that is about to act on such an entry outside a state build —
    an adopt by uid (:func:`lmer_platform.api.adopt_run`) from the daemon CLI,
    which never builds state, or one that lands before the first build after
    an upgrade. The entry gets the transition's own answer first: bound when
    the launcher rule proves its record, unresolved with candidates otherwise.
    Returns what :func:`_settle` returns; ``None`` too for an entry that is
    untracked or already carries a uid. No memo is read or written — the
    answer is computed afresh, and the next build's is unchanged by it.
    """
    entry = runs.get_tracked(host, project, slug)
    if entry is None or entry.uid is not None:
        return None
    return _settle(config, entry)


# --- deciding ----------------------------------------------------------------


def _settle(config: PlatformConfig, entry: runs.TrackedRun, head: str = "") -> Optional[str]:
    """Bind *entry*, mark it unresolved, or leave it: ``"bound"``/``"unresolved"``/``None``.

    A group with no uid record is remembered as such for *head* without reading
    the registry; an unresolved one with the registry entry it was judged
    against, since a session's record landing or its entry going away is what
    can change the verdict.
    """
    if _unchanged_since(entry, head, None):
        return None
    session = _registry_entry(entry) if head and entry.key in _EXAMINED else _UNREAD
    if session is not _UNREAD and _unchanged_since(entry, head, _fingerprint(session)):
        return None
    refs = workrepo.group_records(config, entry.host, entry.project, entry.slug)
    if not any(ref.uid for ref in refs):
        _remember(entry, head, None)
        return None
    if session is _UNREAD:
        session = _registry_entry(entry)
    states = [workrepo._read_run_state(ref.path) or {} for ref in refs]
    verdicts = [launcher_verdict(state, session, entry) for state in states]
    matched = [ref for ref, verdict in zip(refs, verdicts) if verdict is True]
    chosen = None
    if len(matched) == 1 and matched[0].uid:
        chosen = matched[0]
    elif len(refs) == 1 and refs[0].uid and verdicts[0] is not False:
        chosen = refs[0]
    if chosen is not None:
        _bind(config, entry, chosen.uid)
        return "bound"
    candidates = [
        _candidate(ref, state) for ref, state in zip(refs, states) if ref.uid
    ]
    changed = runs.mark_unresolved(entry.key, candidates)
    _remember(entry, head, _fingerprint(session))
    return "unresolved" if changed else None


def _fingerprint(session: Optional[dict]):
    """A registry entry as a comparable value; ``"absent"`` for none."""
    if not isinstance(session, dict):
        return "absent"
    return repr(sorted(session.items(), key=lambda item: str(item[0])))


def _registry_entry(entry: runs.TrackedRun) -> Optional[dict]:
    if not entry.last_session_id:
        return None
    try:
        return registry.read_session(entry.last_session_id)
    except registry.RegistryError:
        return None


def _names_run(session: dict, entry: runs.TrackedRun) -> bool:
    run = session.get("run")
    return (
        isinstance(run, dict)
        and run.get("host") == entry.host
        and run.get("project") == entry.project
        and run.get("slug") == entry.slug
    )


def launcher_verdict(state: dict, session: Optional[dict], entry) -> Optional[bool]:
    """Whether a record's launcher facts match the tracked session's registry entry.

    ``True`` match, ``False`` contradiction, ``None`` proves nothing — the
    predicate is spelled out in the module docstring.
    """
    if not isinstance(session, dict) or not _names_run(session, entry):
        return None
    record = state.get("session") if isinstance(state, dict) else None
    record = record if isinstance(record, dict) else {}
    recorded_uid = session["run"].get("uid")
    if isinstance(recorded_uid, str) and runs.UID_RE.match(recorded_uid):
        return record.get("uid") == recorded_uid
    launcher = record.get("launcher")
    if not isinstance(launcher, dict):
        return None
    pid, hostname = launcher.get("pid"), launcher.get("hostname")
    if not (isinstance(pid, int) and not isinstance(pid, bool) and isinstance(hostname, str)):
        return None
    if pid != session.get("pid") or hostname != socket.gethostname():
        return False
    launched, registered = _instant(launcher.get("started_at")), _instant(session.get("started_at"))
    if launched is None or registered is None:
        return None
    return abs((registered - launched).total_seconds()) <= CLOCK_SKEW_SECONDS


def _instant(stamp: object) -> Optional[datetime]:
    if not isinstance(stamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _candidate(ref: workrepo.RunDirRef, state: dict) -> dict:
    session = state.get("session") if isinstance(state.get("session"), dict) else {}
    name = state.get("name")
    if not isinstance(name, str) and ref.path.name.startswith(f"{ref.uid}--"):
        name = ref.path.name[len(ref.uid) + 2:]
    started = session.get("started_at") or state.get("created")
    return {
        "uid": ref.uid,
        "started_at": started if isinstance(started, str) else None,
        "name": name if isinstance(name, str) else None,
    }


# --- moving ------------------------------------------------------------------


def _bind(config: PlatformConfig, entry: runs.TrackedRun, uid: str) -> None:
    with runs.transition_lock():
        current = runs.get_tracked(entry.host, entry.project, entry.slug)
        if current is None or current.uid is not None:
            # Bound or forgotten while this build read the mirror.
            return
        transition = runs.begin_transition(entry.host, entry.project, entry.slug, uid)
        if transition is None:
            return
        _run(config, transition.new_key)


def _resume(config: PlatformConfig, key: str) -> bool:
    """Finish *key*'s pending transition; whether there was one."""
    with runs.transition_lock():
        return _run(config, key)


def _run(config: PlatformConfig, key: str) -> bool:
    pending = dict(runs.pending_transitions())
    transition = pending.get(key)
    if transition is None:
        return False
    old_parts = runref.split(transition.old_key)
    new_parts = runref.split(transition.new_key)
    for store in transition.pending_stores():
        if transition.stores.get(store) == "done":
            continue  # marked by the call that moved its partner
        if store in _PROJECT_STORES:
            outcome = projects.rekey(transition.old_key, transition.new_key)
            done = _PROJECT_STORES
        elif store == "registry":
            _canonicalize(config, key, old_parts, new_parts)
            outcome, done = {"conflict": False}, (store,)
        else:
            outcome = _STORE_MODULES[store].rekey(old_parts, new_parts)
            done = (store,)
        clashed = [store] if outcome.get("conflict") else []
        transition = runs.mark_transition(key, done, conflict=clashed) or transition
    runs.finish_transition(key)
    return True


def _canonicalize(config: PlatformConfig, key: str, old_parts, new_parts) -> None:
    """Give the tracked session's registry entry the uid it was launched without.

    Every session id the bound entry names (:func:`lmer_platform.runs.carried_session_ids`):
    its own, and the slug entry's when a merge onto an existing uid entry kept
    the two apart — a still-live session of the old slug entry would otherwise
    stay a second row.
    """
    state = None
    stand_in = runs.TrackedRun(host=old_parts[0], project=old_parts[1], slug=old_parts[2])
    for session_id in runs.carried_session_ids(key):
        try:
            session = registry.read_session(session_id)
        except registry.RegistryError:
            continue
        run = session.get("run") if isinstance(session, dict) else None
        if not (isinstance(run, dict) and run.get("host") == old_parts[0]
                and run.get("project") == old_parts[1] and run.get("slug") == old_parts[2]
                and not run.get("uid")):
            continue
        if state is None:
            ref = workrepo.resolve_run_dir(config, new_parts[0], new_parts[1], uid=new_parts[2])
            state = (workrepo._read_run_state(ref.path) or {}) if ref is not None else {}
        if launcher_verdict(state, session, stand_in) is False:
            continue
        registry.update(session_id, run={**run, "uid": new_parts[2]})
        logger.info("platform_run_registry_canonicalized key=%s session=%s", key, session_id)


def recheck_conflict(key: str) -> list:
    """Clear *key*'s ``conflict`` mark for every store no longer holding a pair.

    Behind a re-adopt by uid (``POST /api/runs/adopt {uid}``) of an entry
    already under its uid; the rule is :func:`lmer_platform.runs.settle_conflict`'s.
    Each store answers from its own value under *key*: the per-run stores by
    their ``conflicted``, the project home by re-running
    :func:`lmer_platform.projects.rekey` from every alias (it leaves a
    differing home in place and reports it again, and moves it once the
    operator has made the two agree), and the index by whether it still keeps
    two notes. Returns the stores still in conflict.
    """
    stores = runs.conflict_stores(key)
    if not stores:
        return []
    parts = runref.split(key)
    with runs.transition_lock():
        aliases = runs.load_index().get(key, {}).get("aliases") or []
        still = []
        for store in stores:
            if store == "index":
                conflicted = runs.index_conflicted(key)
            elif store in _PROJECT_STORES:
                conflicted = any(
                    projects.rekey(alias, key).get("conflict")
                    for alias in aliases if isinstance(alias, str)
                )
            elif store in _STORE_MODULES:
                conflicted = _STORE_MODULES[store].conflicted(parts)
            else:
                conflicted = True  # a store this build does not know: keep its mark
            if conflicted:
                still.append(store)
        return runs.settle_conflict(key, still)
