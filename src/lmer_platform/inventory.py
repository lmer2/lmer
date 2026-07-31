"""Run-centric inventory: what is running, and what needs a human (spec §5.4).

The entity is the run, not the session
--------------------------------------
A run blocked on a question has *exited* — the run-state contract is record the
question, ``work commit``, then stop, with the answer arriving via a fresh seeded
session. So an inventory of live containers would omit precisely the rows that
need attention (spec D15). This module therefore keys on runs and treats sessions
as incarnations: a run has zero or one live session, and many over its life.

Attention is a second axis
--------------------------
Liveness and needing-you are independent (spec D23). A run can be alive *and*
waiting on you, or stopped and waiting on you, or stopped and wanting nothing.
Each run therefore carries an attention record separate from its state, and the
UI sorts on it.

Two judgement calls worth reading before changing anything here
--------------------------------------------------------------
**"crashed" requires evidence, not just absence.** The mirror holds every run the
work repo has ever seen, and plenty of long-finished ones sit at
``status: in-progress`` because they ended without being marked complete. Calling
all of those crashed would make the attention list useless. A crash is claimed
only when a *stale registry entry* exists — a session whose PID is gone. Clean
exits remove their entry, so a leftover entry with a dead PID is real evidence
that something died. Everything else with no live session is ``dormant``.

This is why the inventory must run **before** :func:`registry.prune_dead`: the
stale entry *is* the crash signal, and pruning it is how an operator
acknowledges the crash and clears it from the list.

**Sessions can exist before their run dir does.** A freshly spawned session does
not appear in the mirror until it runs its first ``work commit`` — seconds or
minutes later. Iterating run dirs alone would make a just-spawned session
invisible, which breaks the most common question the UI answers ("I started that,
where is it?"). So live sessions with no matching run dir become rows of their
own, built from the session entry.

Which row is the platform itself
--------------------------------
One row in a fleet view is the orchestrator's own session
(:mod:`lmer_platform.assistant`), and an operator reading a list of runs has to be
able to tell it from the runs it orchestrates — the operator asked: "i think the
orchestrator run needs to be clearly marked that it is that, both in the list and
the detail of the run". :attr:`RunView.orchestrator` is that fact, and it is read
from the registry ``kind`` and from nothing else: the taskdef and the target are
arguments an operator can pass to an ordinary worker, so a row that inferred the
role from either would badge a hand-started ``orchestrate`` run as the platform
itself.

It crosses as a field of the *row*, for the reason the title does (T57): the session
block is a narrow projection of a registry entry, and the raw ``kind`` is not
something a client should have to interpret.

The one field here that is *not* the run's
------------------------------------------
``title`` comes from :mod:`lmer_platform.meta` — this orchestrator's own note
about a run, which is deliberately not stored on the run and not on the tracked
index (that module carries the whole argument). It is joined onto the row here
because the alternative is the browser asking for one run's metadata per row,
which on the landing screen is a request per tracked run for a label.

Joined, never moved: the storage stays where T52 put it. Writing a title through
``runs.track`` would refresh ``last_seen``, and ``last_seen`` is what orders the
fleet — renaming a finished run would shove it to the top as if it had just done
something — while a lost update in ``runs.json`` costs a whole *run* rather than
a title.

The one field here that is not on disk at all
---------------------------------------------
``session.activity`` — how long a live session's harness has been quiet — is read
from the session's own control plane while the row is being built
(:func:`lmer_platform.session_io.session_activity`), because it exists nowhere
else. Run state moves when a session **ends** (spec D24, and :func:`_derive`
depends on it), so a run that finished its work and is sitting at its prompt is
indistinguishable here from one that is still working: same ``running`` state,
same everything, and the only way an operator could tell used to be to open the
terminal and read the scrollback. The supervisor inside the container is the one
process that knows, so the row asks it.

What bounds the cost, since this is a network read on the fleet's read path:

- **live sessions only, and only ones with a control plane on their entry.** A
  dormant run, a crashed one and a session spawned without ``--fastapi`` are
  skipped without a syscall. What is left is bounded by
  ``max_concurrent_sessions`` (default 8) plus the orchestrator's own session —
  nine loopback GETs on a build that already reads a ``state.yaml``, a ledger, an
  events file and an ask channel per row;
- **a one-second budget** each (:data:`lmer_platform.session_io
  .ACTIVITY_TIMEOUT_SECONDS`), rather than the five a write gets, so a couple of
  wedged containers cannot hold the whole view past the interval that asked for it;
- **every failure is ``None``**, which renders as nothing. A fleet view must not
  fail because one container did not answer, and must not fabricate an idle of
  zero for a session whose image is too old to have the fact.

It is *not* cached, and that is the same call :mod:`lmer_platform.reattach` makes
about which log is canonical: the answer is a fact about right now, a stale one
is worse than none, and the read is already gated on the handful of sessions that
can answer it. Callers that hold the mapping already — or want no reads at all —
pass ``activity=`` (see :func:`build_inventory`).

Idleness is a **row fact and not an attention reason**, deliberately. Every member
of :data:`ATTENTION_REASONS` is picked up automatically by detection (T69) and
becomes a digest class, so adding one here would mean inventing a threshold — how
many minutes of quiet is a problem — and there is no policy anywhere that says. A
run's right answer differs by taskdef and by phase, and a wrong number spools a
notification per session per baseline change. So the fact crosses, the assistant
and the operator read it and decide, and the reason is one entry in that tuple plus
one branch in :func:`_derive` on the day a threshold has an owner.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from work_repo import run_state

from . import assistant, meta
from .reattach import OUTPUT_CONTROL_PLANE, OUTPUT_SESSION_LOG, detached_record
from .registry import is_live
from .runs import RunIndexError, run_key
from .session_io import session_activity
from .workrepo import RunDirRef

logger = logging.getLogger("lmer_platform.inventory")

__all__ = [
    "RUN_STATES", "ATTENTION_REASONS", "ATTENTION_PRIORITY", "SESSION_FIELDS",
    "RunView", "Inventory", "build_inventory",
]

#: Run states (spec §5.4). ``held`` and ``feedback`` arrive with the lifecycle
#: verbs in M3; they are listed here so the vocabulary is defined in one place.
#:
#: ``detached`` is neither ``running`` nor ``crashed`` and is the third thing on
#: purpose (T36): the session's process is alive, but the daemon restart that
#: destroyed its host PTY also destroyed the platform's only way of seeing it,
#: and its control plane did not answer either. Calling that ``running`` asserts
#: liveness nobody verified; calling it ``crashed`` asserts a death that did not
#: happen. See :mod:`lmer_platform.reattach`.
RUN_STATES = (
    "running",
    "detached",
    "held",
    "feedback",
    "waiting_on_you",
    "yielded",
    "parked",
    "failed",
    "crashed",
    "dormant",
    "complete",
    "unknown",
)

#: Why a run needs a human. ``feedback``, ``cap_reached`` and ``slot_contention``
#: are raised by machinery that lands in later slices.
#:
#: ``question`` and ``live_question`` are two different situations and not two
#: names for one: ``question`` is a run that recorded a question stop and
#: **exited**, so answering it respawns the run (:mod:`lmer_platform.answer`),
#: while ``live_question`` is a session that is **running** and blocked in a poll
#: on its ask channel, so answering it is a file the container is already
#: watching (:mod:`lmer_platform.ask`). Merging them would put an operator one
#: tap from starting a container when they meant to reply to a waiting session.
ATTENTION_REASONS = (
    "question",
    "live_question",
    "feedback",
    "yield",
    "critical_error",
    "crashed",
    "unreadable",
    "cap_reached",
    "slot_contention",
)

#: Sort order for the attention list: a direct question outranks a crash, because
#: one is blocked on the human and the other is merely broken. A *live* question
#: comes first of all — the session is up, holding a slot, and doing nothing
#: until it is answered.
ATTENTION_PRIORITY = {
    "live_question": 0,
    "question": 1,
    "feedback": 2,
    "yield": 3,
    "cap_reached": 4,
    "critical_error": 5,
    "crashed": 6,
    "slot_contention": 7,
    "unreadable": 8,
}

_TERMINAL_STATUSES = ("complete", "archived")

#: Fields of a session's registry entry that cross into the fleet payload.
#:
#: An allowlist, because the alternative defaults to sharing: the row used to
#: carry the entry as read, so every key any part of the platform has ever
#: written onto one — the container id, the Slack thread, the host-side
#: transcript and log layout, ``control.token_ref`` — reached a client because
#: nothing stopped it, and the next key added to an entry would have too. None of
#: that has a reader in the browser. The precedent is
#: :class:`lmer_platform.transcripts.Source`, which keeps its ``path`` out of
#: ``to_dict`` because a host filesystem layout is not the browser's business.
#:
#: ``log_path`` is the one host path that stays, and deliberately: the detail view
#: prints it so an operator can go read the PTY log on the box when the browser is
#: not enough. Everything else here answers a question the UI actually asks — see
#: the tests, which pin each field to the code that reads it, so widening this is
#: a deliberate act rather than the side effect of writing a new key onto an entry.
#:
#: ``activity`` is the one member that is not on the registry entry as written: it
#: is folded onto a copy of it while the row is built (see the module docstring on
#: why it can only come from the container, and what bounds the read). It crosses
#: *inside* the block rather than as a row field — unlike the title and the
#: orchestrator mark, whose guards pin the opposite — because it is a fact about
#: the live session and nothing else: it is unknowable for a run with no session,
#: it dies with the entry, and a row-level ``idle`` would invite a client to read
#: it on a dormant run and get a null it could not explain.
SESSION_FIELDS = ("id", "pid", "started_at", "log_path", "lifecycle", "activity")


@dataclass(frozen=True)
class Attention:
    """Why a run needs a human, and what to do about it."""

    reason: str
    note: Optional[str] = None
    url: Optional[str] = None
    since: Optional[str] = None

    @property
    def priority(self) -> int:
        return ATTENTION_PRIORITY.get(self.reason, 99)

    def to_dict(self) -> dict:
        return {
            "reason": self.reason,
            "note": self.note,
            "url": self.url,
            "since": self.since,
            "priority": self.priority,
        }


@dataclass
class RunView:
    """One row of the inventory."""

    host: str
    project: str
    slug: str
    state: str
    name: Optional[str] = None
    #: What this orchestrator says the run is about, one line, or ``None``
    #: (:mod:`lmer_platform.meta`). Beside :attr:`label` rather than replacing it:
    #: a view that shows one string shows the title and falls back to the label,
    #: and the detail header shows both — the title is what the run is *about*,
    #: the label is what it is *called*.
    title: Optional[str] = None
    taskdef: Optional[str] = None
    target: Optional[str] = None
    goal: Optional[str] = None
    phase: Optional[str] = None
    status: Optional[str] = None
    stop_reason: Optional[str] = None
    updated: Optional[str] = None
    rel_path: Optional[str] = None
    attention: Optional[Attention] = None
    session: Optional[dict] = None
    ledger: Optional[dict] = None
    events: list = field(default_factory=list)
    #: Questions the run's *live* session is waiting on, oldest first, from its
    #: ask channel (:mod:`lmer_platform.ask`). Carried on the row rather than
    #: fetched per run by the client, because the fleet view is polled from a
    #: phone and a second request per waiting run is the wrong trade. Empty for
    #: every run that is not being asked something right now.
    questions: list = field(default_factory=list)
    #: The most recent session this orchestrator ran for the run, from the tracked
    #: index — which outlives the session itself. A clean exit removes the registry
    #: entry, so ``session`` goes ``None`` while the PTY log deliberately remains
    #: (spec D16); without this id a client has nothing to ask the log endpoint
    #: for, and the history a crashed run can show would vanish the moment the
    #: crash was acknowledged.
    last_session_id: Optional[str] = None

    @property
    def live(self) -> bool:
        return bool(self.session and self.session.get("live"))

    @property
    def orchestrator(self) -> bool:
        """Whether this row is the platform's own orchestrating session (T85).

        The registry ``kind`` (:data:`lmer_platform.assistant.KIND`), which the
        spawn path writes and nothing else sets — never the taskdef or the target.
        Those two are arguments, and an operator can hand ``orchestrate`` and
        ``fleet`` to a worker; a row that read them would badge that worker as the
        platform itself.

        True for a *stale* entry too, which is deliberate: the row an operator most
        needs marked is the one saying the orchestrator crashed. It goes false only
        when there is no entry left at all — after which the row is a run like any
        other, and nothing on it claims otherwise.
        """
        return (self.session or {}).get("kind") == assistant.KIND

    @property
    def detached(self) -> Optional[dict]:
        """The session's ``detached`` record, when its host PTY is gone (T36).

        Carried on the row separately from ``state`` because the two answer
        different questions and only sometimes agree. A detached session whose
        output the platform recovered over its control plane is still
        ``running`` — it is up, it is working, and it can be watched — but the
        row must still be able to say the terminal it is showing has a seam in
        it and no host-side ``lmer`` output after that point. Only when nothing
        is reaching the log at all does the state itself become ``detached``.
        """
        return detached_record(self.session)

    @property
    def label(self) -> str:
        """Best human name for the run: its recorded name, else its slug.

        Never the title, and it stays on the row next to one: a client renders
        the title where a run is named and this is what it falls back to, so
        ``label`` has to keep answering "what is this run called" whether or not
        anybody has written a note about it.
        """
        return self.name or self.slug

    @property
    def ports(self) -> list:
        """Published host ports, so the UI can link what the session is serving."""
        return list((self.session or {}).get("ports") or [])

    @property
    def _task(self) -> dict:
        """The session entry's ``task`` block, or ``{}``.

        Tolerant of a missing or wrong-shaped block for the same reason
        :func:`_load_state_tolerantly` is tolerant of a bad ``state.yaml``: an
        entry written by an older or newer daemon must cost one row's metadata,
        never the fleet view.
        """
        task = (self.session or {}).get("task")
        return task if isinstance(task, dict) else {}

    @property
    def preset(self) -> Optional[str]:
        """The startup preset the run was spawned with (``--preset``), if any.

        Source and lifetime, because both matter to a caller: this comes from the
        session's registry entry, which :mod:`lmer_platform.spawn` writes from the
        typed ``SpawnRequest`` field (T37). Nothing else records it — the run
        state schema has no field for it and the tracked index does not carry it —
        so it is known for exactly as long as the entry exists. A run whose
        session exited cleanly reports ``None``, which is why a client must render
        this only when present rather than reserving a slot for it.

        The *name* only, which is all that ever crossed into the platform: the
        preset body stays host-side because its ``env`` can hold credentials
        (:mod:`lmer_cli.presets`).
        """
        return self._task.get("preset")

    @property
    def agents(self) -> Optional[str]:
        """Preset names the session may fan work out to (``--agents``), if any.

        Same source and same lifetime as :attr:`preset`, and the same string the
        operator selected — one comma-delimited value rather than a list, because
        that is the shape ``lmer`` takes and records.
        """
        return self._task.get("agents")

    @property
    def harness(self) -> Optional[str]:
        """The agent harness the spawn *named*, if it named one.

        ``None`` is not "the default one": with no ``--harness`` the harness is
        resolved inside the session (``LMER_HARNESS``, then the model hint in
        ``LMER_LLM_NAME`` — :mod:`lmer_cli.harness`) and the host never learns
        which one won. Reporting a guess here would put a wrong name on the row of
        every session started from a preset that sets ``LMER_HARNESS``.
        """
        return self._task.get("harness")

    @property
    def model(self) -> Optional[str]:
        """The model driving the harness, as the session itself resolved it.

        ``None`` only when the session predates T51 or resolved no model at all —
        it is no longer the standing answer. The reason this could not simply be
        read off the daemon still holds and is why the value takes the long way
        round: the model is resolved *inside* the session, and an exported
        ``LMER_LLM_NAME`` beats a preset's value host-side (:mod:`lmer_cli.cli`),
        so inferring it from the daemon's own environment would be wrong for
        precisely the runs that name a preset — the ones whose model actually
        varies. So the session reports what it resolved, late, through
        :func:`lmer_platform.spawn.ports_file_for`, and ``absorb_ports`` folds it
        in without overwriting a model the spawn request named.
        """
        return self._task.get("model")

    @property
    def session_for_client(self) -> Optional[dict]:
        """The live session's entry, narrowed to :data:`SESSION_FIELDS`.

        ``None`` rather than ``{}`` when nothing is running: a client asks "does
        this run have a session" by testing this object, and an empty mapping is
        truthy in JavaScript — it would put a blank live-session card on every
        dormant run.

        A field the entry does not carry is present as ``None`` rather than
        omitted, so the keys a client sees do not depend on how much the daemon
        had learned by the last time it wrote the entry.
        """
        if not self.session:
            return None
        return {key: self.session.get(key) for key in SESSION_FIELDS}

    @property
    def session_id_for_history(self) -> Optional[str]:
        """The session id a client should ask for this run's terminal history.

        The live entry when there is one, else the last session the tracked index
        remembers. Both point at the same on-disk PTY log, and the log outlives the
        container — so a run whose session exited cleanly can still show what it
        did, which is the whole reason the log is kept.
        """
        live_id = (self.session or {}).get("id")
        return live_id or self.last_session_id

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "project": self.project,
            "slug": self.slug,
            "label": self.label,
            # This orchestrator's note about the run, not the run's own state, and
            # on the row rather than fetched per run: it is what a listing names a
            # run by, and a request per row for a label is one per tracked run on
            # the first screen the app draws.
            "title": self.title,
            "state": self.state,
            "live": self.live,
            # Which of these rows is the platform itself, on the row rather than
            # left for a client to infer from the taskdef: see the module
            # docstring. Always present and always a boolean, so a listing tests
            # one field instead of branching on whether the daemon sent it.
            "orchestrator": self.orchestrator,
            "name": self.name,
            "taskdef": self.taskdef,
            "target": self.target,
            # The run's own account of itself, read off its state.yaml, beside the
            # derived ``state`` above and never folded into it: liveness outranks
            # the committed record there by design (:func:`_derive`), so a live
            # session's row says "running" while these three can say it is paused,
            # or say nothing yet. They cross so a listing can show that
            # disagreement rather than hide it — a row that renders only the
            # derived word cannot. ``None`` is ordinary for all three, which is why
            # a client renders them only when present: a run in its first seconds
            # has committed no state at all (:func:`_view_from_session`).
            "goal": self.goal,
            "phase": self.phase,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "updated": self.updated,
            "rel_path": self.rel_path,
            "attention": self.attention.to_dict() if self.attention else None,
            # The entry narrowed to SESSION_FIELDS, never the entry itself: a
            # registry file is a host-side debugging artifact, and most of what is
            # in one has no reader on the other end of an HTTP response.
            "session": self.session_for_client,
            "detached": self.detached,
            "last_session_id": self.session_id_for_history,
            "ledger": self.ledger,
            "events": self.events,
            "ports": self.ports,
            "questions": self.questions,
            # How the session was launched. Flat fields, read out of the entry's
            # ``task`` block here rather than left for a client to dig out of:
            # that block does not cross at all (SESSION_FIELDS), and a UI reading
            # into it would have been coupled to the spawn record's shape and
            # would show nothing for a run whose entry is gone without saying why.
            "preset": self.preset,
            "agents": self.agents,
            "harness": self.harness,
            "model": self.model,
        }


@dataclass
class Inventory:
    """The whole fleet view."""

    runs: list = field(default_factory=list)

    @property
    def attention(self) -> list:
        """Runs needing a human, most urgent first."""
        return sorted(
            (r for r in self.runs if r.attention),
            key=lambda r: (r.attention.priority, r.updated or "", r.slug),
        )

    @property
    def live(self) -> list:
        return [r for r in self.runs if r.live]

    def counts(self) -> dict:
        counts: dict[str, int] = {}
        for run in self.runs:
            counts[run.state] = counts.get(run.state, 0) + 1
        return counts

    def to_dict(self) -> dict:
        return {
            "runs": [r.to_dict() for r in self.sorted_runs()],
            "attention": [r.to_dict() for r in self.attention],
            "counts": self.counts(),
            "totals": {
                "runs": len(self.runs),
                "live": len(self.live),
                "attention": len(self.attention),
            },
        }

    def sorted_runs(self) -> list:
        """Attention first, then live, then most recently updated.

        "Quickly identify and get to lmer instances that require my input" is a
        sort order, not a separate screen.
        """
        def key(run: RunView):
            return (
                0 if run.attention else 1,
                run.attention.priority if run.attention else 0,
                0 if run.live else 1,
                _recency_key(run.updated),
                run.slug,
            )

        return sorted(self.runs, key=key)


def _recency_key(value: Optional[str]) -> tuple:
    """Sort key placing the most recent ISO timestamp first, unknowns last.

    Digits are extracted and negated rather than inverting the string's
    codepoints: ISO-8601 is fixed-width and zero-padded, so digit order is
    chronological order, and a negated integer sorts descending without needing a
    sentinel that has to out-rank every possible transformed timestamp.
    """
    if not value:
        return (1, 0)
    digits = "".join(c for c in value if c.isdigit())
    if not digits:
        return (1, 0)
    return (0, -int(digits))


def _titles() -> dict:
    """This orchestrator's run titles, keyed as :func:`lmer_platform.runs.run_key`.

    One read of one snapshot for the whole fleet, which is the only reason this
    can ride the read path at all: metadata lives in a single ``run_meta.json``
    (:mod:`lmer_platform.meta`), so joining it on costs the same as reading the
    tracked index and nothing per run — where a file per run dir, or the client
    asking per row, would both scale with the fleet.

    Quiet, like every other read here: an unreadable or hand-broken snapshot
    means nobody's title is known, not that the fleet view fails. ``load_all``
    already swallows and logs that; the empty mapping it returns is what makes
    every row fall back to its label.

    Only non-empty titles are kept, so "cleared" and "never described" are one
    state on the row exactly as they are on disk.
    """
    titles: dict[str, str] = {}
    for key, payload in meta.load_all().items():
        record = meta.RunMeta.from_dict(key, payload)
        if record.title:
            titles[key] = record.title
    return titles


def _title_for(
    titles: Mapping,
    host: Optional[str],
    project: Optional[str],
    slug: Optional[str],
) -> Optional[str]:
    """One run's title out of the map, or ``None``.

    Keyed through :func:`lmer_platform.runs.run_key` rather than by formatting the
    triple here, so the row and the writer cannot disagree about what identifies a
    run. A row built from a session entry that named no run has nothing to look up
    — ``run_key`` refuses it, and that refusal costs the title rather than the row.
    """
    if not titles:
        return None
    try:
        key = run_key(host, project, slug)
    except RunIndexError:
        return None
    return titles.get(key) or None


def _load_state_tolerantly(ref: RunDirRef) -> tuple[Optional[dict], Optional[str]]:
    """Read one run's state, returning ``(state, error)``.

    A corrupt or newer-schema ``state.yaml`` must not take out the whole
    inventory, so the failure becomes this run's problem alone and surfaces as an
    ``unreadable`` attention reason rather than an exception.
    """
    try:
        return run_state.load_state(ref.path), None
    except run_state.RunStateError as exc:
        logger.warning(
            "platform_run_state_unreadable run=%s error=%s", ref.rel_path, exc
        )
        return None, str(exc)
    except OSError as exc:
        logger.warning("platform_run_state_unreadable run=%s error=%s", ref.rel_path, exc)
        return None, str(exc)


def _ledger_summary(ref: RunDirRef) -> Optional[dict]:
    try:
        return run_state.summarize_ledger(run_state.load_ledger(ref.path))
    except (run_state.RunStateError, OSError):
        return None


def _recent_events(ref: RunDirRef, last_n: int) -> list:
    if last_n <= 0:
        return []
    try:
        return run_state.read_events(ref.path, last_n=last_n)
    except OSError:
        return []


def _session_key(entry: dict) -> Optional[tuple]:
    run = entry.get("run") or {}
    host, project, slug = run.get("host"), run.get("project"), run.get("slug")
    if not (host and project and slug):
        return None
    return (host, project, slug)


def _activity_for(entry: dict, known: Optional[Mapping]) -> Optional[dict]:
    """One live session's idle record, or ``None`` when there is none to have.

    *known* is the caller's mapping of session id to record, and ``None`` there
    means "read it" — the same convention *titles* uses in
    :func:`build_inventory`, and for the same reason: every caller that serves a
    fleet view wants the fact and has nothing to decide about it. A mapping is
    taken as authoritative, so ``{}`` is how a caller asks for rows with no idle
    readings on them at all.

    The two gates before any I/O are the cost control the module docstring
    describes, and both are answers the entry already contains:

    - **not live** — a stale entry is a corpse and a run with no session has
      nothing to ask. Its harness's last output is not a fact about now;
    - **no control plane** — a session the platform did not spawn, or spawned
      before there was one, cannot be asked anything. ``session_activity`` would
      reach the same conclusion, but only after a registry read and a token read
      per row, and this is on the read path of every poll.
    """
    session_id = entry.get("id")
    if not isinstance(session_id, str) or not session_id:
        return None
    if known is not None:
        record = known.get(session_id)
        return record if isinstance(record, dict) else None
    if not entry.get("live"):
        return None
    control = entry.get("control")
    if not isinstance(control, dict) or not control.get("port"):
        return None
    return session_activity(session_id)


def _is_blind(session: Optional[dict]) -> bool:
    """Whether a live session is one the platform can no longer see (T36).

    True only when the entry is marked detached *and* nothing is appending to its
    log — i.e. its host PTY died with a daemon and its control plane could not be
    reached to replace it. A detached session being read over its control plane
    is not blind: the seam in its scrollback is a historical fact about the log,
    not a statement about now. Neither is one recording itself from inside its
    container (T71/T78) — its record is being written *without* this daemon's
    help, which is the opposite of blind.
    """
    record = detached_record(session)
    return record is not None and record.get("output") not in (
        OUTPUT_CONTROL_PLANE, OUTPUT_SESSION_LOG,
    )


def _live_question_note(questions: list) -> str:
    """The attention note for a session waiting on its ask channel.

    The oldest unanswered question, because that is the one the session is
    blocked in — plus a count when there are more, so a row never implies one
    reply will unblock the run when three are outstanding.
    """
    first = questions[0]
    text = first.get("text") or "the session asked something but recorded no text"
    extra = len(questions) - 1
    return f"{text} (+{extra} more)" if extra > 0 else text


def _derive(
    state: Optional[dict],
    session: Optional[dict],
    *,
    unreadable: Optional[str] = None,
    questions: Optional[list] = None,
) -> tuple[str, Optional[Attention]]:
    """Map run state plus session liveness onto a run state and attention record.

    The ordering of these branches is the design: a live session means running
    regardless of what the last committed run state says, because the run state is
    git-eventual (spec D24) while liveness is immediate. Only once nothing is
    alive does the recorded stop reason decide what the run is waiting for.

    A live session with an open question on its ask channel is the one case where
    "running" and "needs a human" are true at once, which is exactly what the two
    axes are for (spec D23): the state stays ``running`` — the container is up and
    holding a slot — while the attention record says a person is the only thing it
    is waiting for.
    """
    if unreadable:
        return "unknown", Attention(
            reason="unreadable",
            note=f"run state could not be read: {unreadable}",
        )

    state = state or {}
    status = state.get("status")
    stop_reason = state.get("stop_reason")
    question = state.get("open_question")
    updated = state.get("updated")

    live = bool(session and session.get("live"))
    stale_entry = bool(session and not session.get("live"))

    if live:
        # A live session outranks the committed run state, which lags. Unless the
        # platform cannot see it: then the PID is *all* that is known, because the
        # host PTY died with a daemon and the container did not answer either, and
        # "running" would be asserting liveness on the strength of a process-table
        # entry nobody could ask anything (T36).
        state = "detached" if _is_blind(session) else "running"
        if questions:
            # Still on the attention list, and answerable: the ask channel is a
            # mounted directory, not the control plane, so a session the platform
            # is blind to can be replied to exactly as before. The two axes are
            # independent (spec D23) and this is the case that proves it.
            return state, Attention(
                reason="live_question",
                note=_live_question_note(questions),
                since=questions[0].get("at"),
            )
        return state, None

    if unreadable is None and status in _TERMINAL_STATUSES:
        return "complete", None

    if stop_reason == "question":
        # `open_question` is optional in practice: a run can record
        # `--stop-reason=question` without `--question "<text>"`, and plenty in
        # the wild do. Saying so is more useful than a vague "waiting for an
        # answer", because the operator has to open the run either way and
        # should know the row cannot tell them more.
        return "waiting_on_you", Attention(
            reason="question",
            note=question or "question text was not recorded — open the run to see",
            since=updated,
        )
    if stop_reason == "paused":
        return "parked", None
    if stop_reason == "yield":
        return "yielded", Attention(
            reason="yield",
            note="stopped at a phase boundary for review",
            since=updated,
        )
    if stop_reason == "critical_error":
        detail = state.get("critical_error") or {}
        note = detail.get("summary") if isinstance(detail, dict) else None
        return "failed", Attention(
            reason="critical_error",
            note=note or "run recorded an unrecoverable error",
            since=updated,
        )

    if stale_entry:
        # Evidence, not inference: a clean exit removes its registry entry, so a
        # leftover entry with a dead PID means something actually died.
        return "crashed", Attention(
            reason="crashed",
            note="session process is gone with no recorded stop reason",
            since=(session or {}).get("started_at"),
        )

    return "dormant", None


def _view_from_run_dir(
    ref: RunDirRef,
    session: Optional[dict],
    *,
    event_count: int,
    last_session_id: Optional[str] = None,
    questions: Optional[list] = None,
    title: Optional[str] = None,
) -> RunView:
    state, error = _load_state_tolerantly(ref)
    run_state_state, attention = _derive(
        state, session, unreadable=error, questions=questions
    )
    state = state or {}
    return RunView(
        questions=list(questions or []),
        last_session_id=last_session_id,
        host=ref.host,
        project=ref.project,
        slug=ref.slug,
        state=run_state_state,
        name=state.get("name"),
        title=title,
        taskdef=state.get("taskdef"),
        target=state.get("target"),
        goal=state.get("goal"),
        phase=state.get("phase"),
        status=state.get("status"),
        stop_reason=state.get("stop_reason"),
        updated=state.get("updated"),
        rel_path=ref.rel_path,
        attention=attention,
        session=session,
        ledger=_ledger_summary(ref),
        events=_recent_events(ref, event_count),
    )


def _view_from_session(
    entry: dict,
    questions: Optional[list] = None,
    *,
    title: Optional[str] = None,
) -> RunView:
    """A session whose run dir has not reached the mirror yet.

    Everything shown comes from the session entry, because there is no committed
    run state to read. The row exists so a just-spawned session is visible
    immediately rather than after its first ``work commit``.

    A question asked in those first minutes is the *likeliest* one — "which repo
    did you mean?" comes before the first ``work commit``, not after — so this row
    carries the ask channel too rather than being the one place it goes missing.
    """
    run = entry.get("run") or {}
    task = entry.get("task") or {}
    live = bool(entry.get("live"))
    if not live:
        state = "crashed"
    else:
        state = "detached" if _is_blind(entry) else "running"
    attention = None
    if not live:
        attention = Attention(
            reason="crashed",
            note="session process is gone and no run state was ever committed",
            since=entry.get("started_at"),
        )
    elif questions:
        attention = Attention(
            reason="live_question",
            note=_live_question_note(questions),
            since=questions[0].get("at"),
        )
    return RunView(
        host=run.get("host") or "—",
        project=run.get("project") or "—",
        slug=run.get("slug") or entry.get("id") or "—",
        state=state,
        name=run.get("name"),
        title=title,
        taskdef=task.get("taskdef"),
        target=task.get("target"),
        rel_path=run.get("rel_path"),
        updated=entry.get("started_at"),
        attention=attention,
        session=entry,
        questions=list(questions or []),
    )


def _view_from_tracked(entry, *, title: Optional[str] = None) -> RunView:
    """A tracked run whose directory is not in the mirror.

    Either it has not been pushed yet or the mirror is stale (spec D24). Showing
    the row with what the index knows beats omitting a run the operator explicitly
    tracks — an empty space is indistinguishable from "not tracked", which is the
    confusion D25 exists to remove.

    And what the index knows is identity, not an address: this row exists *because*
    no directory was found for the run, so it is the one row that cannot say where
    the run lives. It used to carry a composed ``runs/<slug>``, which is wrong for
    every named run (the container renames the dir to ``runs/<slug>--<name>``) —
    the same guess :attr:`lmer_platform.runs.TrackedRun.key` exists instead of.
    ``rel_path`` is therefore ``None`` here, exactly as it is for the row of a
    session whose run dir has not landed yet, and a client renders it only when
    there is one.
    """
    return RunView(
        host=entry.host,
        project=entry.project,
        slug=entry.slug,
        state="dormant",
        title=title,
        taskdef=entry.taskdef,
        target=entry.target,
        rel_path=None,
        updated=entry.last_seen or entry.first_seen,
        last_session_id=getattr(entry, "last_session_id", None),
    )


def build_inventory(
    run_refs: Iterable[RunDirRef],
    sessions: Iterable[dict],
    *,
    event_count: int = 3,
    tracked: Optional[Iterable[Any]] = None,
    questions: Optional[Mapping] = None,
    titles: Optional[Mapping] = None,
    activity: Optional[Mapping] = None,
) -> Inventory:
    """Join run dirs against session entries into the fleet view.

    *sessions* should come from ``registry.list_sessions(live_only=False)``:
    stale entries are the crash signal (see the module docstring), so filtering
    them out here would silently drop crashed runs from the attention list.

    *tracked* is the local run index (:mod:`lmer_platform.runs`), which is what
    scopes the view (spec D25). Callers pass the tracked entries plus the refs they
    resolved; any tracked run whose dir is missing from the mirror still gets a row
    built from index metadata. Passing ``None`` means "no index" and yields exactly
    the refs and sessions given — the shape the unit tests use.

    *questions* maps a session id to its unanswered questions
    (:func:`lmer_platform.ask.pending_by_session`). It is passed in rather than
    read here for the same reason the run dirs are: this function reads only the
    paths it is given, which is what makes the whole fleet view testable against
    planted state.

    *titles* is the one exception to that, and the exception is what makes the
    field reach a client at all: it maps ``run_key`` to this orchestrator's title
    for the run (:mod:`lmer_platform.meta`), and ``None`` means "read the
    snapshot" rather than "no titles". A title is a *label* — every caller that
    serves a fleet view wants it, none of them has anything to decide about it,
    and it is one read of one file for the whole fleet however many runs there
    are. A caller that already holds the mapping passes it; ``{}`` is how a caller
    asks for rows with no titles on them at all.

    *activity* is the second such exception and carries the same ``None`` means
    "read it" convention: it maps a session id to that session's idle record
    (:func:`lmer_platform.session_io.session_activity`). Unlike the titles this is
    a *network* read, so the module docstring says what bounds it — live sessions
    with a control plane, one second each, every failure ``None`` — and ``{}`` is
    how a caller (or a test) asks for rows built without touching a container.
    """
    by_session_questions = dict(questions or {})
    by_title = _titles() if titles is None else dict(titles)
    by_activity = None if activity is None else dict(activity)
    by_key: dict[tuple, dict] = {}
    unmatched: list[dict] = []
    for entry in sessions:
        if not isinstance(entry, dict):
            continue
        # Tolerate callers that passed live_only=True (no computed flag) as well
        # as raw entries read straight off disk.
        if "live" not in entry:
            entry = {**entry, "live": is_live(entry)}
        # Folded onto a copy, never written to the registry: this changes every
        # second by definition, and a registry write per live session per poll
        # would put the fleet view's read path into the state directory's single
        # writer (spec §6.1). Absent when unknown rather than present as null —
        # ``session_for_client`` fills the key in from the allowlist either way, so
        # the payload's shape does not depend on whether a container answered.
        record = _activity_for(entry, by_activity)
        if record is not None:
            entry = {**entry, "activity": record}
        key = _session_key(entry)
        if key is None:
            unmatched.append(entry)
            continue
        existing = by_key.get(key)
        # Prefer a live session when two entries claim the same run (a crashed
        # predecessor and its replacement).
        if existing is None or (entry.get("live") and not existing.get("live")):
            if existing is not None:
                unmatched.append(existing)
            by_key[key] = entry
        else:
            unmatched.append(entry)

    # The tracked index remembers the last session per run, which the registry
    # cannot: a clean exit removes its entry. That id is what lets a finished run
    # still serve its terminal history (see RunView.session_id_for_history).
    last_sessions = {
        (entry.host, entry.project, entry.slug): getattr(entry, "last_session_id", None)
        for entry in tracked or []
    }

    def questions_for(entry: Optional[dict]) -> list:
        session_id = (entry or {}).get("id")
        return list(by_session_questions.get(session_id) or [])

    def title_for(entry: dict) -> Optional[str]:
        run = entry.get("run") or {}
        return _title_for(
            by_title, run.get("host"), run.get("project"), run.get("slug")
        )

    runs: list[RunView] = []
    seen: set[tuple] = set()
    for ref in run_refs:
        key = (ref.host, ref.project, ref.slug)
        seen.add(key)
        entry = by_key.get(key)
        runs.append(
            _view_from_run_dir(
                ref, entry, event_count=event_count,
                last_session_id=last_sessions.get(key),
                questions=questions_for(entry),
                title=_title_for(by_title, ref.host, ref.project, ref.slug),
            )
        )

    for tracked_entry in tracked or []:
        key = (tracked_entry.host, tracked_entry.project, tracked_entry.slug)
        if key in seen:
            continue  # a run-dir row already covers it
        if key in by_key:
            # Tracked with a live or stale session but no run dir yet: the session
            # loop below emits that row, which carries strictly more. Do NOT mark
            # it seen here, or that row would be skipped and the run would vanish.
            continue
        seen.add(key)
        runs.append(_view_from_tracked(tracked_entry, title=_title_for(by_title, *key)))

    for key, entry in by_key.items():
        if key in seen:
            continue
        runs.append(
            _view_from_session(
                entry, questions_for(entry), title=title_for(entry)
            )
        )
    for entry in unmatched:
        runs.append(
            _view_from_session(
                entry, questions_for(entry), title=title_for(entry)
            )
        )

    return Inventory(runs=runs)
