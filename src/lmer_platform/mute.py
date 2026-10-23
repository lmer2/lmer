"""Muted runs: routine chatter from a run the operator steers directly (#458).

A run the operator is driving by hand — a ``chat`` run on a Slack thread is the
usual case — still spools every operator message, agent reply and note to the
assistant as ``session_comms``. None of it is for the assistant, and each
record wakes it. Muting the run stops those records reaching the assistant's
spool and nothing else: the journal, the UI chat pane and every connector see
them as before.

What a mute suppresses is an allow-list of *routine* records
(:data:`ROUTINE_SOURCES`): a kind together with the channel that wrote it, so a
question, a milestone, any kind added later, and anything the platform itself
says — a Codex delivery that needs review, a thread that disconnected — still
spool. A muted run cannot go deaf on the assistant. The detector's own digests
(``live_question``, ``crashed``, ``complete`` and the rest) are untouched, with
two exceptions the operator chose because the operator is driving a muted run
and watches it directly: a muted run is never named in a ``stale_runs`` check-in
(:func:`lmer_platform.checkin.is_eligible`), and its ``stalled`` condition is not
spooled (:mod:`lmer_platform.detect`) — whichever way the stall was recognised,
silence, unanswered input or the provider refusing. Nothing is buffered, so
unmuting replays nothing.

Two scopes, one answer
----------------------
A tracked run is muted by its run key, in this module's own snapshot, in the
shape of :mod:`lmer_platform.meta`: platform state local to this orchestrator,
carried across a slug→uid key transition (:func:`rekey`) and to the uid-keyed
row a schema-2 resume or answer starts (:func:`prime`, before that session is
published), and dropped when the run is forgotten (:func:`drop`). A session
with no tracked run — a chat run with no identity — has no key to hold the
flag, so it is muted on its registry entry and the flag ends with the session.

Which mute covers a record or a row is answered by one resolver
(:func:`run_muted`) for the record filter, the fleet fold and therefore the
detector, so a row never says muted while its chatter still spools.
"""

from __future__ import annotations

import logging
from typing import Iterable, Mapping, Optional

from lmer_cli.comms import (
    CHANNEL_AGENT, CHANNEL_MATRIX, CHANNEL_PLATFORM, CHANNEL_SLACK,
    CHANNEL_TERMINAL, CHANNEL_UBER,
    KIND_ANSWER, KIND_CLOSE, KIND_COMMAND, KIND_MESSAGE, KIND_NOTE,
)

from . import registry
from .runs import (
    get_tracked, holds_conflict, key_parts_lenient, load_index, merge_values,
    run_key, run_key_of_entry, run_of_session,
)
from .store import (
    StoreError, append_event, mutating, read_json, snapshot_path, utc_now_iso,
    write_json,
)

logger = logging.getLogger("lmer_platform.mute")

__all__ = [
    "MUTE_FILE", "ROUTINE_SOURCES", "MUTED_KINDS", "LOCALITY_NOTE", "RUN_MUTED",
    "RUN_UNMUTED", "MuteError", "RunNotTracked", "SessionNotFound",
    "load_all", "is_muted", "run_muted", "routine", "set_run", "set_session",
    "muted_sessions", "suppressed", "source_of", "prime", "drop", "rekey",
    "conflicted",
]

MUTE_FILE = "run_mute.json"

#: The channels a person writes on: the operator at a terminal, in a chat
#: thread, through the platform's own UI or API, and the assistant relaying.
_PEOPLE = frozenset({CHANNEL_TERMINAL, CHANNEL_SLACK, CHANNEL_MATRIX, CHANNEL_UBER, CHANNEL_PLATFORM})

#: What a mute keeps off the assistant's spool: each routine kind, and the
#: channels that write it as chatter. An allow-list on purpose, of both: a
#: question, a milestone, a kind added later, and a routine kind from any other
#: writer — the supervisor's own notes, a connector's channel nobody listed
#: here — still spool, because the safety rule is that a muted run can still
#: reach the assistant with anything that is not routine chatter. A ``platform``
#: record counts only when a person authored it (:func:`routine`): the platform
#: also writes on that channel when it reports its own failures.
ROUTINE_SOURCES: Mapping[str, frozenset] = {
    KIND_MESSAGE: _PEOPLE | {CHANNEL_AGENT},
    KIND_NOTE: frozenset({CHANNEL_AGENT}),
    KIND_ANSWER: _PEOPLE,
    KIND_COMMAND: frozenset({CHANNEL_PLATFORM, CHANNEL_UBER}),
    KIND_CLOSE: _PEOPLE | {CHANNEL_AGENT},
}

#: The kinds :data:`ROUTINE_SOURCES` names, for the docs and the API reply.
MUTED_KINDS = frozenset(ROUTINE_SOURCES)

#: Question bookkeeping: an answer or a withdrawal of a question that reached
#: the assistant. See :func:`suppressed` for when it is let through.
_REPLY_KINDS = frozenset({KIND_ANSWER, KIND_CLOSE})

RUN_MUTED = "run_muted"
RUN_UNMUTED = "run_unmuted"

LOCALITY_NOTE = (
    "mute is this orchestrator's own setting: routine records from the run "
    "(messages, replies and notes an agent or a person wrote, answers, relayed "
    "commands, closes) are not spooled to the assistant, and a muted run is left "
    "out of stale_runs check-ins and stalled digests. Questions, milestones, the "
    "platform's own notices, crashes and complete still spool. The journal, chat "
    "pane and connectors are unaffected, and unmuting replays nothing."
)


class MuteError(RuntimeError):
    """A refusal, carrying the status a route should answer."""

    status = 400


class RunNotTracked(MuteError):
    status = 404


class SessionNotFound(MuteError):
    status = 404


def _path():
    return snapshot_path(MUTE_FILE)


def load_all() -> dict:
    """Raw ``{run key: entry}``. Empty when nothing is muted or the file is bad."""
    try:
        stored = read_json(_path())
    except StoreError as exc:
        logger.error("platform_run_mute_unreadable error=%s — treating as empty", exc)
        return {}
    entries = (stored or {}).get("muted") if isinstance(stored, dict) else None
    return entries if isinstance(entries, dict) else {}


def _save(entries: dict) -> None:
    write_json(_path(), {"muted": entries})


def is_muted(parts: Optional[tuple], entries: Optional[dict] = None) -> bool:
    """Whether the run key ``(host, project, key segment)`` composes is muted.

    The stored key only. Anything that names a run the way a record or a row
    does — a slug left behind by a key transition, a renamed legacy run's
    recorded slug — goes through :func:`run_muted`.
    """
    if not parts:
        return False
    entries = load_all() if entries is None else entries
    return isinstance(entries.get(run_key(*parts)), dict)


def _covering_keys(parts: tuple, index: dict) -> set:
    """Every tracked key the run ``parts`` names could be filed under.

    - the key it composes;
    - the entry a key transition left that key on as an alias (a session's
      registry block can stay slug-spelled after its run was bound to a uid),
      and the other way round, the aliases of the entry the key names (a mute
      still filed under the slug key until reconcile moves it);
    - a renamed legacy run: the container renames a named run's directory to
      ``<slug>--<name>`` and the run is tracked under that name, while every
      session for it registers under the recorded ``<slug>``
      (:func:`lmer_platform.spawn.live_session_for_run`). Taken only when the
      bare ``<slug>`` is not itself tracked: a tracked bare key is a run of its
      own, and a mute on its renamed neighbour must not silence it. The one
      designed way for both to be tracked is a resume of the renamed run, whose
      spawn tracks the recorded slug too; that resume carries the mute onto the
      bare key explicitly (:func:`prime`), so nothing is lost by not guessing
      here. And only when exactly one tracked run carries that name: a slug is
      only as unique as the target it was derived from, and two such runs are
      two runs nothing here can tell apart —
      :func:`lmer_platform.workrepo._renamed_run_dir` refuses the same
      ambiguity, and so does this, by not muting.
    """
    key = run_key(*parts)
    found = {key}
    # The other direction of the same transition: a mute still filed under a key
    # this entry took over, which reconcile has not moved yet.
    own = index.get(key)
    if isinstance(own, dict) and isinstance(own.get("aliases"), list):
        found.update(alias for alias in own["aliases"] if isinstance(alias, str))
    aliased_by = [
        candidate_key for candidate_key, candidate in index.items()
        if isinstance(candidate, dict) and isinstance(candidate.get("aliases"), list)
        and key in candidate["aliases"]
    ]
    found.update(aliased_by)
    if key in index or aliased_by:
        return found
    renamed = [candidate_key for candidate_key in index if candidate_key.startswith(f"{key}--")]
    if len(renamed) == 1:
        found.add(renamed[0])
    return found


def run_muted(parts: Optional[tuple], entries: Optional[dict] = None,
              index: Optional[dict] = None) -> bool:
    """Whether the run ``parts`` names, however it is spelled, is muted.

    The one answer the record filter (:func:`suppressed`) and the fleet fold
    (:func:`lmer_platform.api._fold_mute`, which the detector reads) share.
    """
    if not parts:
        return False
    entries = load_all() if entries is None else entries
    if not entries:
        return False
    if isinstance(entries.get(run_key(*parts)), dict):
        return True
    index = load_index() if index is None else index
    return any(isinstance(entries.get(key), dict) for key in _covering_keys(parts, index))


def _by(caller: Optional[Mapping]) -> dict:
    """``{"by": …}`` for an event, when there is a caller to name."""
    return {"by": dict(caller)} if caller else {}


def _event(muted: bool, data: dict, by: Optional[Mapping] = None) -> None:
    append_event(RUN_MUTED if muted else RUN_UNMUTED, data={**data, **_by(by)})


def set_run(host, project, slug=None, *, uid=None, muted: bool,
            by: Optional[Mapping] = None) -> dict:
    """Mute or unmute a tracked run. Returns ``{"muted", "changed", "key"}``.

    Keyed by the tracked entry's own key, so a slug reference that reached a
    uid-keyed run through its alias mutes the run rather than a key nothing
    reads. Refuses a run this orchestrator does not track, for
    :func:`lmer_platform.meta.write`'s reason: nothing would ever drop it.

    Unmuting also clears the registry flag of any session of this run that was
    muted on its own entry before the run was tracked: otherwise "unmute this
    run" would answer that it had nothing to do while that session stayed
    silent. *by* names the caller on the event.
    """
    tracked = get_tracked(host, project, slug, uid=uid)
    if tracked is None:
        raise RunNotTracked(
            f"{run_key(host, project, uid or slug)} is not tracked by this "
            "orchestrator. Adopt it first (POST /api/runs/adopt), or mute its "
            "session instead (POST /api/sessions/<id>/mute)."
        )
    key = tracked.key
    with mutating(_path()):
        entries = load_all()
        changed = isinstance(entries.get(key), dict) != muted
        linked = {} if muted else _rename_links(key, entries)
        if changed or linked:
            if muted:
                entries[key] = {"muted": True, "updated_at": utc_now_iso()}
            else:
                entries.pop(key, None)
                for linked_key in linked:
                    entries.pop(linked_key, None)
            _save(entries)
    if changed:
        _event(muted, {"run": key}, by)
        logger.info("platform_run_mute key=%s muted=%s", key, muted)
    for linked_key, link in linked.items():
        _event(False, {"run": linked_key, **link}, by)
        logger.info("platform_run_mute key=%s muted=False %s", linked_key, link)
    changed = changed or bool(linked)
    if not muted:
        changed = bool(_clear_session_flags(key, by)) or changed
    return {"muted": muted, "changed": changed, "key": key}


def _rename_links(key: str, entries: dict) -> dict:
    """The other half of a renamed run's mute, which unmuting *key* takes with it.

    A resume or answer of a renamed legacy run carries its mute from the renamed
    key onto the bare slug its session registers under (:func:`prime`). The two
    are one run to the operator, and either can be the one they unmute: the
    renamed row by key, or the live row by its session, which resolves to the
    bare key. Unmuting one leaves the other behind otherwise — and the next
    resume would read the renamed key's mute and carry it back. So, mapped to
    the event data that names the link:

    - unmuting the renamed key: every bare key carried from it;
    - unmuting a bare key whose mute was carried: the renamed key it came from.

    A mute the operator set on the bare key itself carries no ``carried_from``
    and links nothing; nor does a schema-2 continuation's, a row of its own.
    """
    linked = {
        bare: {"carried_from": key} for bare, entry in entries.items()
        if isinstance(entry, dict) and entry.get("carried_from") == key
        and key.startswith(f"{bare}--")
    }
    own = entries.get(key)
    source = own.get("carried_from") if isinstance(own, dict) else None
    if isinstance(source, str) and source.startswith(f"{key}--") and source in entries:
        linked[source] = {"carried_to": key}
    return linked


def _clear_session_flags(key: str, by: Optional[Mapping]) -> list:
    """Unmute every registry entry flagged muted whose run resolves to *key*."""
    index = load_index()
    cleared = []
    for entry in registry.list_sessions(live_only=False):
        if not (isinstance(entry, dict) and entry.get("muted") is True and entry.get("id")):
            continue
        parts = run_key_of_entry(entry)
        if parts and key in _covering_keys(parts, index) and _set_session_flag(entry["id"], False, by):
            cleared.append(entry["id"])
    return cleared


def set_session(session_id: str, *, muted: bool, by: Optional[Mapping] = None) -> dict:
    """Mute or unmute by session id. Returns ``{"muted", "changed", "run"|"session"}``.

    A session whose run this orchestrator tracks mutes the *run* — mute is a
    per-run flag, and a run's next session must stay muted. Only a session with
    no tracked run is flagged on its registry entry. Unmuting clears that entry
    flag on either branch: a session muted before its run was adopted would
    otherwise stay silent while both answers said it was not.
    """
    try:
        entry = registry.read_session(session_id)
    except registry.RegistryError as exc:
        raise MuteError(str(exc)) from exc
    run = run_of_session(session_id)
    tracked = None if run is None else get_tracked(
        run["host"], run["project"], run.get("slug"), uid=run.get("uid"))
    flag_changed = False
    if entry is not None and (tracked is None or not muted):
        flag_changed = _set_session_flag(session_id, muted, by)
    if tracked is not None:
        outcome = set_run(tracked.host, tracked.project, tracked.slug,
                          uid=tracked.uid, muted=muted, by=by)
        return {"muted": muted, "changed": outcome["changed"] or flag_changed,
                "run": outcome["key"]}
    if entry is None:
        raise SessionNotFound(f"no session {session_id} in this orchestrator's registry")
    return {"muted": muted, "changed": flag_changed, "session": session_id}


def _set_session_flag(session_id: str, muted: bool, by: Optional[Mapping] = None) -> bool:
    """Write the entry flag; whether it changed. Decided under the entry's lock."""
    with mutating(registry.session_path(session_id)):
        entry = registry.read_session(session_id)
        if not isinstance(entry, dict) or (entry.get("muted") is True) == muted:
            return False
        registry.update(session_id, muted=muted)
    _event(muted, {"session": session_id}, by)
    logger.info("platform_session_mute session=%s muted=%s", session_id, muted)
    return True


def muted_sessions(sessions: Iterable) -> set:
    """Ids of the registry entries in *sessions* flagged muted themselves."""
    return {
        entry.get("id") for entry in sessions
        if isinstance(entry, dict) and entry.get("muted") is True and entry.get("id")
    }


def routine(message: object) -> bool:
    """Whether *message* is chatter a mute may hold back (:data:`ROUTINE_SOURCES`).

    Never a record the platform classified for attention
    (``meta.notification_class``), and never a ``platform`` record nobody
    authored: that channel is also where the platform reports its own failures
    — a Codex delivery that needs review, input that was not sent.
    """
    if not isinstance(message, Mapping):
        return False
    kind = message.get("kind")
    source = message.get("source")
    channel = source.get("channel") if isinstance(source, Mapping) else None
    if not (isinstance(kind, str) and channel in ROUTINE_SOURCES.get(kind, ())):
        return False
    meta = message.get("meta")
    if isinstance(meta, Mapping) and meta.get("notification_class"):
        return False
    return channel != CHANNEL_PLATFORM or bool(source.get("author"))


def suppressed(session_id: str, message: dict, run: Optional[dict] = None) -> bool:
    """Whether *message* from *session_id* stays off the assistant's spool.

    The record's own shape first (:func:`routine`), so a question or a
    platform notice answers without a file read. Then the run's mute, through
    :func:`run_muted`; then the session's own registry flag. Under the session
    flag alone an answer or a withdrawal of a question is let through: that
    session's run has no identity, so the detector has no row to report the
    question settled from, and the assistant, which did get the question, would
    otherwise hold it open for good.
    """
    if not routine(message):
        return False
    muted = run_muted(key_parts_lenient(run) if run else None)
    if not muted and session_id:
        entry = registry.read_session(session_id)
        muted = isinstance(entry, dict) and entry.get("muted") is True and not (
            message.get("kind") in _REPLY_KINDS and message.get("in_reply_to"))
    if muted:
        logger.debug("platform_mute_suppressed session=%s kind=%s", session_id, message.get("kind"))
    return muted


def source_of(host: str, project: str, segment: str) -> Optional[tuple]:
    """``(host, project, segment)`` when that run is muted, else ``None``.

    What a resume or answer hands the spawn as
    :attr:`lmer_platform.spawn.SpawnRequest.mute_from`. Resolved through
    :func:`run_muted`, so a mute still filed under a key a transition has not
    moved yet is found. A store that cannot be read carries nothing: the
    continuation starts unmuted rather than not at all.
    """
    parts = (host, project, segment)
    try:
        return parts if run_muted(parts) else None
    except StoreError as exc:
        logger.warning("platform_run_mute_unreadable run=%s error=%s — not carried",
                       run_key(*parts), exc)
        return None


def prime(source: tuple, started: tuple) -> bool:
    """Mute *started* because *source* is muted. Returns whether it wrote.

    For a schema-2 resume or answer, whose continuation is a new uid-keyed run:
    the operator muted the run, and a continuation of it is the same run as far
    as they are concerned. And for a resume or answer of a renamed legacy run,
    whose session registers under the recorded slug that the spawn now tracks as
    well — the renamed lookup in :func:`_covering_keys` stops at a tracked bare
    key, so this is what keeps that session muted. Never for an override, which
    starts a different run.

    Called by the spawn (:func:`lmer_platform.spawn.spawn_session`) once the
    uid is minted and *before* the session is published, so no record the new
    session writes is read against an unmuted key. The run is not tracked yet
    at that point — the spawn tracks it right after — which is why this writes
    without :func:`set_run`'s check; a spawn that fails before tracking drops it
    again (:func:`drop`).
    """
    source_key, started_key = run_key(*source), run_key(*started)
    if source_key == started_key:
        return False
    with mutating(_path()):
        entries = load_all()
        if isinstance(entries.get(started_key), dict):
            return False
        entries[started_key] = {"muted": True, "updated_at": utc_now_iso(),
                                "carried_from": source_key}
        _save(entries)
    _event(True, {"run": started_key, "carried_from": source_key})
    logger.info("platform_run_mute_carried from=%s to=%s", source_key, started_key)
    return True


def drop(host: str, project: str, slug: str) -> bool:
    """Forget a run's mute. Called by :func:`lmer_platform.runs.forget`."""
    key = run_key(host, project, slug)
    with mutating(_path()):
        entries = load_all()
        if key not in entries:
            return False
        entries.pop(key)
        _save(entries)
    logger.info("platform_run_mute_dropped key=%s", key)
    return True


def rekey(old_parts: tuple, new_parts: tuple) -> dict:
    """Carry a mute from one key to another (spec §6.1); :func:`lmer_platform.meta.rekey`'s contract."""
    old_key = run_key(*old_parts)
    new_key = run_key(*new_parts)
    if old_key == new_key:
        return {"moved": False, "conflict": False}
    with mutating(_path()):
        entries = load_all()
        if old_key not in entries:
            return {"moved": False, "conflict": holds_conflict(entries.get(new_key))}
        merged, conflict = merge_values(entries.pop(old_key), entries.get(new_key))
        conflict = conflict or holds_conflict(merged)
        entries[new_key] = merged
        _save(entries)
    logger.info("platform_run_mute_rekeyed old=%s new=%s conflict=%s", old_key, new_key, conflict)
    return {"moved": True, "conflict": conflict}


def conflicted(parts: tuple) -> bool:
    return holds_conflict(load_all().get(run_key(*parts)))
