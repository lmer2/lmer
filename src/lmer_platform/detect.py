"""Detection: the daemon watches, the assistant is *told* (issue #141, T69; spec §8.3).

The direction of the arrow is the whole design
----------------------------------------------
Spec §8.3 is explicit — "the daemon detects, the assistant is notified, not
subscribed". The alternative, delegating detection to the assistant ("poll every
session each minute and tell me what is up"), fails in a specific and expensive
way: its context window fills with routine noise within hours, so it degrades
exactly when the fleet is busiest, and the UI's attention badge would then depend
on an LLM being alive. So the reading, the joining and the judging all happen
here, mechanically, on a timer — and the assistant is woken only for what
changed.

Nothing in this module asks the assistant anything. It calls
:func:`lmer_platform.assistant.notify`, which spools whether or not one is
running (that is the seam's contract, and why a digest is never lost to a chat
window nobody opened) and answers whether one was live purely so this can say so
in a log line.

What is material: the attention axis, plus arriving at a terminal state
----------------------------------------------------------------------
"Material" is the one policy this module chooses, and it is deliberately not a
new vocabulary. :mod:`lmer_platform.inventory` already computes the second axis
the fleet view sorts on — "does a human need to do something about this run" —
with :data:`lmer_platform.inventory.ATTENTION_REASONS` naming every case:
``question``, ``live_question``, ``feedback``, ``yield``, ``critical_error``,
``crashed``, ``unreadable``, and the ``cap_reached`` / ``slot_contention`` pair
that machinery in later slices raises. Every reason that axis can produce is
material *by construction*: a reason exists precisely because a person has to
act. So this module invents no categories, matches on no English, and picks up a
reason added to that axis later without being touched.

:data:`MATERIAL_STATES` is the one addition, and it is a state rather than a
reason for a reason: §8.3 lists "a task finished" among the events worth waking
the assistant for, and a finished run needs *nobody* — it has no attention record
by design (:func:`lmer_platform.inventory._derive` returns ``complete`` with no
attention at all). It is still the event a cycle's next step hangs off, so it is
read off the state axis, using that axis's own word.

Everything else a tick can see is deliberately not material: a run going
``running``, ``dormant`` or ``parked``, a row appearing because the operator just
spawned it, a note whose text was re-rendered. Those are the routine traffic
§8.3 exists to keep out of a context window.

A *change*, never a standing state — and why the first tick is silent
---------------------------------------------------------------------
Read §8.3's own list: a question **opened**, a task **finished**, a cap
**refusal**. Every item is a transition, not a condition, and that distinction is
what keeps this from being a firehose: notifying on every tick for a condition
that has not changed would spool a digest a minute for one unanswered question.

So a tick computes the set of conditions it can see, diffs it against the
previous tick's set, and notifies for the ones that are new. A condition that
persists is announced once. A condition that clears and comes back is announced
again — the second occurrence is a second event, and treating it as a duplicate
of the first would hide a run that asked, got answered, and asked again.

One *clear* is material, and only one (issue #254)
---------------------------------------------------
A run that stops having *any* question on the attention axis is the exception,
delivered as :data:`QUESTION_ANSWERED_KIND` for the kinds :data:`QUESTION_KINDS`
names. The consumer is the whole argument: the orchestrating assistant is asleep
between digests, so an answer given out of band — the UI's survey, ``POST
/api/runs/answer``, ``work answer`` in a shell someone had open — wakes nothing,
and it goes on believing the run is blocked on a human who has already replied.
Every *other* clear stays silent, because nothing is waiting on it: a run that
stops being crashed needs no next step from anybody.

The clear is keyed on the **run**, and not on the condition key the diff above
uses — which is the difference between a wake-up and a lie. Two ordinary
movements change that key while a human is still owed an answer. A run that is
*re-asked* between two ticks gets a new ``since`` (the new question's own
timestamp), so the baseline's key is gone from the current tick with the run
waiting right now. And a live ask most often ends by changing *shape* rather
than disappearing: a session records its blocking question and exits, turning
``live_question`` into the stopped run's ``question`` — two spellings of one fact
(:data:`lmer_platform.inventory.ATTENTION_REASONS` explains why they are two
reasons and not one name), which is why they are both in
:data:`QUESTION_KINDS`. Keyed on the tuple, each of those spools "no longer
waiting" about a run that is waiting, and the assistant drains oldest-first, so
the false all-clear would be its last word on the subject.

So both sides of the diff are reduced to the set of runs carrying at least one
:data:`QUESTION_KINDS` condition, and a clear is a run that *left* that set. A
re-ask and a shape change both keep the run in it, and say nothing here; the
fresh condition each of them raises still arrives through the diff above, which
is the digest that is true. What is given up is real and small: a run that has
one question answered and asks another in the same window is told about the new
question only, and the assistant learns the old one is gone by the run not being
on the attention list when it reads.

Three things this deliberately does not claim. A run that left the fleet between
ticks (archived, deleted) also loses its condition, and is not announced — its
question was not answered, and naming a run the assistant can no longer address
is a wake-up with no move behind it. A question can leave the axis without being
answered at all, if the session holding it dies; that run's ``crashed`` arrives
on the same tick through the diff above, saying so. And a session can *withdraw*
its own question — close it unanswered and go on running
(:func:`ask_channel.protocol.open_questions` drops a closed entry, which is what
both ask views render as "the session stopped waiting for this") — after which
the condition clears exactly as an answer clears it, on a run that is still in
the fleet and still healthy. Nothing in the payload tells those two apart: the
attention record that carried the question is precisely what is gone. So the
digest is worded to the strength this can measure — *answered or withdrawn* —
because "a human replied" is a claim an assistant would act on by skipping the
read that would have shown nobody had.

The first tick therefore establishes the baseline and notifies **nothing**, which
is a real choice with a real cost. What it gives up: a question that opened while
the daemon was down is never announced as an event — and, symmetrically, one that
was *answered* while it was down, since :attr:`Detector._seen` lives in memory
only and a clear is a diff against a baseline that restarted empty. What it buys:
a daemon restart on a host with a long history does not flood a spool bounded at
:data:`lmer_platform.assistant.MAX_PENDING` with digests about runs that finished
last week — and dropping the oldest to make room for stale ones is the failure
worth avoiding. The standing list is not lost either way: it is what
``GET /api/state`` answers, mechanically, and the ``orchestrate`` taskdef sends a
starting assistant to read exactly that.

Identity includes *when* the condition started
-----------------------------------------------
A condition's key is ``(run, kind, since)``, not ``(run, kind)``. ``since`` is
already on the attention record — the question's timestamp for
``live_question``, the run's ``updated`` for the reasons read off a stopped run —
so a session that gets one question answered and asks another produces a new key
and a second digest, while the same unanswered question keeps producing the same
key however many ticks it survives. Keying on the pair alone would silently
swallow the second question, which is the exact event this module exists to
deliver.

The *note* is not part of the identity. It is a rendering of the condition (and
the count in "…(+2 more)" moves on its own), and the current text is always one
``GET /api/state`` away.

Cost, and the throttle it must not defeat
-----------------------------------------
A tick reads the fleet view through :func:`lmer_platform.api.build_state`, which
is the same function the UI's poll and ``lmer platform status`` use — so the
badge and the digest can never disagree about what needs a human. It is called
**without** ``force_pull``, which is load-bearing: that flag is the ``rescan``
path, and forcing here would turn every tick into a ``git fetch`` and make the
``work_repo_pull_interval`` throttle mean nothing. Detection is therefore never
more expensive than the browser sitting on the same page.

There is one exception: a signal whose content is an assertion about the mirror
(:data:`ABSENCE_KINDS`) is re-read against a caught-up one before it is
delivered. See :meth:`Detector._confirmed`, and docs/PLATFORM-QUICKSTART.md,
"Confirming a crash", for what that costs.

:data:`DETECT_INTERVAL_SECONDS` matches :data:`lmer_platform.config
.DEFAULT_PULL_INTERVAL_SECONDS`, so on a default host a tick lands roughly in
step with the fastest the mirror can move. It is a constant rather than a read of
``work_repo_pull_interval`` because half of what this sees needs no pull at all —
a live session's open questions come from the ask channel and a crash from the
registry — and an operator who widens the mirror throttle to ten minutes did not
ask for a ten-minute delay on those.

A second job on the same tick: reconciling endings nothing watched
-----------------------------------------------------------------
:func:`sweep_finished_sessions` is not detection and does not pretend to be — it
notifies nobody and computes no signal. It is here because it needs exactly what
this module already has: a timer nothing waits on, in the one process that is the
single writer of platform state (spec §6.1).

A session's ending is recorded by the ``_watch`` thread in
:mod:`lmer_platform.spawn`, which removes the registry entry of a session that
exited cleanly — and that thread dies with the process that spawned the session.
Two ordinary cases therefore leave a *stale* entry behind on a session that
finished perfectly well: one that survived a daemon restart
(:mod:`lmer_platform.reattach`), and one spawned by ``lmer platform spawn``, which
exits immediately (:mod:`lmer_platform.lifecycle` says the same thing from the
signalling side).

A stale entry is evidence of a crash everywhere the fleet view reads it, and it is
read in two places that behave differently:

- :func:`lmer_platform.inventory._derive`, for a row built from a run dir, prefers
  the run's own *committed* status — so once a terminal status has reached the
  mirror that row says ``complete`` whatever the entry says. There the stale entry
  is a window rather than a standing lie (``crashed`` from the session's exit until
  its final commit is pulled), plus a dead session left hanging off a finished
  run's row afterwards.
- :func:`lmer_platform.inventory._view_from_session` consults no run state at all:
  every stale entry that no run-dir row claims becomes a row of its own reading
  ``crashed``, with a ``crashed`` record on the attention axis. That includes the
  ordinary shape of a resumed run (spec §5.4) — the predecessor's entry loses the
  run key to its live replacement and turns into a second row and a permanent badge
  for a session that finished normally.

The sweep removes such an entry when the run's own record says the work
finished, so neither of those outlives the run and :func:`_derive`'s rule is not
touched. It removes one more: an entry the operator asked to wind down that no
watcher saw fail (:func:`_ended_as_asked`), which the fleet now calls ``dormant``
rather than ``crashed`` and which the UI's clear-crashed card therefore no longer
offers to remove. Both conditions read the same facts as the label, so the sweep
can never clear a row the fleet still calls crashed. Anything else stays where it
is: that shape is a real crash and the entry is its only evidence.

Only the first of the two takes the transcript scrub with it — see
:func:`_reconcile`.

It also finishes the cleanup ``_watch`` would have done on the way out, which for
these sessions has never happened at all: the control token goes with the entry
(otherwise a credential for a container that is long gone stays on disk), the ports
file goes, and the transcript is scrubbed.

The exit code is the one thing no version of this can recover — nothing waited on
the process — so the event it appends says so rather than filing a ``clean``
nobody observed. See :data:`SESSION_ENDED_UNWATCHED`.

A third job on the same tick: milestones a session said out loud (T122)
----------------------------------------------------------------------
Everything above infers. A session running ``lmer-signal "pushed MR !167"`` is
the opposite — the agent is *telling* the platform something happened, on the
channel it already has (``NNNNNN.signal.json``,
:func:`ask_channel.protocol.read_signals`) — and the operator asked for it in
those terms: "a dedicated tool that just lets lmer send a signal to the
orchestrator explicitly when it pushes a PR or is done with a review"
(2026-07-29).

Two properties keep it out of everything above rather than inside it:

- **It is not an attention reason.** Every member of
  :data:`lmer_platform.inventory.ATTENTION_REASONS` means a *person* has to act,
  and each one automatically becomes a digest class here (the coupling this
  module is built on). A milestone needs the orchestrator, not the operator, so it
  travels as :data:`SIGNAL_DIGEST_KIND` — a digest class standing beside that
  mechanism, reaching the assistant through the same
  :func:`lmer_platform.assistant.notify` seam and putting nothing on the fleet
  view's badge.
- **It is a record, not a condition.** The diff above exists because a standing
  condition would otherwise be re-announced every tick; a signal is a file that
  was written once, so what dedupe means here is a *high-water mark*
  (:data:`SIGNAL_MARKS_FILE`, one seq per session) rather than a set diff, and it
  is on disk because a milestone re-delivered after every daemon restart is the
  same firehose arriving by another door.

Which is also why signals have no silent first tick. The baseline above drops a
condition that started while the daemon was down, on the argument that the
standing list is one ``GET /api/state`` away; a signal is nowhere else — the run
dir does not carry it and no fleet read derives it — so a milestone filed while
the daemon was restarting is delivered when it comes back, and delivered once.

The order within one signal is deliver-then-mark, so the failure mode is a
milestone the assistant hears twice rather than one it never hears: a crash
between the two costs a duplicate, and the assistant can see it is a duplicate
(same signal id) while a lost "the review is finished" is invisible to everyone.

A fourth job on the same tick: runs nobody has looked at (issue #244)
---------------------------------------------------------------------
Everything above needs something to *happen* — a condition to appear, a session
to end, an agent to signal. A run that just stops moving produces none of it, and
in a turn-based flow that idles both sides at once.

:mod:`lmer_platform.checkin` is the answer; :meth:`Detector._check_ins` runs it
off this tick's fleet read. Two things keep it from being the firehose everything
above avoids: one digest names every stale run, and a run it named waits another
window before it is named again.

A fifth job on the same tick: saying a spool has gone unread (issue #317)
-------------------------------------------------------------------------
Everything above ends at :func:`lmer_platform.assistant.notify`, which writes to a
file and pushes nothing. What reads that file is a watch the assistant arms on
itself — and a watch stops: a skipped re-arm, a stale edge detector, a harness
with no monitor primitive at all. On 2026-08-19 that left ten digests, live
questions among them, sitting for seventeen minutes while the operator noticed
first.

So :meth:`Detector.nudge_once` types **one sentence** into the assistant's own
session through :func:`lmer_platform.session_io.send_input` — the path a wind-down
already uses — when :mod:`lmer_platform.nudge` says the spool has waited past the
configured interval beside a quiet assistant. That module owns every boundary and
the wording; this one owns the send, the mark and the event. The operator-facing
account of both is ``docs/PLATFORM-QUICKSTART.md``, "Digest nudges".

Three things keep it from being the push this module spent its first four
sections arguing against. **No digest travels**: what is typed is "N are waiting,
take them". **It is rate-limited by a window, not a count**, anchored in
:attr:`lmer_platform.assistant.AssistantState.nudged_at` and bounded in memory
when that write fails (:attr:`Detector._nudged`), so a nudge whose submit never
registered is retried rather than lost. And **it cannot fire at a working
assistant**, which is also why there is no separate "is it draining" check:
draining is work, and work is not idle.

Failure is absorbed, and a persistent one goes quiet
----------------------------------------------------
A detection failure must never take the daemon down: the fleet view is what an
operator needs when something is broken, and it does not depend on this thread at
all. So every tick absorbs everything, logs it, and keeps ticking — there is no
give-up here, unlike :class:`lmer_platform.assistant.Supervisor`, because a tick
costs a state read rather than a clone and an image pull, and a detector that
gave up would go silent exactly when a host is already unwell.

Two consequences worth stating, because both are ways this goes wrong quietly:

- **Repeats are demoted, not repeated.** A mirror that cannot be read fails every
  tick, and an ``ERROR`` a minute for the same cause is how a log becomes
  unreadable. The first occurrence of each cause is logged loudly, per stage;
  identical repeats go to ``DEBUG`` with a count, and the recovery is logged so
  the silence is bounded at both ends.
- **A failed scan leaves the baseline alone.** Replacing it with "nothing"
  would make the next successful tick see every standing condition as new, so a
  flaky mirror would produce a digest per condition per failure — the firehose,
  arriving through the error path.
"""

from __future__ import annotations

import logging
import hashlib
import uuid
import threading
from dataclasses import dataclass, replace
from typing import Optional

from ask_channel import protocol as ask_protocol
from work_repo import run_state

from lmer_cli import runref

from . import (
    ask, assistant, checkin, nudge, registry, rotation, runs, teardown, transcripts,
)
from .api import build_state
from . import config as config_module
from .config import (
    PlatformConfig, checkin_settings, nudge_settings, rotation_settings,
)
from .session_io import SessionIOError, send_input, session_output_state, quiet_health
from .inventory import _TERMINAL_STATUSES, _observed_bad_exit, _was_wound_down
from .spawn import ports_file_for
from .store import (
    StoreError,
    age_seconds,
    clamp_text,
    append_event,
    mutating,
    read_json,
    snapshot_path,
    utc_now_iso,
    write_json,
)
from .transcripts import _scrub
from .workrepo import resolve_run_dir
from . import project_context, projects

logger = logging.getLogger("lmer_platform.detect")

__all__ = [
    "DETECT_INTERVAL_SECONDS", "MATERIAL_STATES", "QUESTION_ANSWERED_KIND",
    "ASSISTANT_NUDGED",
    "QUESTION_KINDS", "SESSION_ENDED_UNWATCHED",
    "SESSION_SIGNALLED", "SIGNAL_DIGEST_KIND", "SIGNAL_MARKS_FILE",
    "Signal", "SessionSignal", "Detector", "sweep_finished_sessions",
    "new_signals", "record_ingested_signals",
]

#: How often a tick runs. See the module docstring for why this is a constant
#: rather than ``config.work_repo_pull_interval``, and why it is the same number.
DETECT_INTERVAL_SECONDS = 30.0

#: What :func:`lmer_platform.inventory._view_from_session` puts in the identity
#: fields of a row it built from a session that has no run identity yet — one
#: spawned seconds ago, before its first ``work commit``. Copied rather than
#: imported because it is a rendering choice in that module rather than part of
#: its contract; if the two ever disagree the cost is one useless digest naming a
#: run the assistant cannot address, not a failure.
_UNIDENTIFIED = "—"

#: Event type for a session whose ending nobody was there to record — the sweep's
#: stand-in for ``spawn``'s ``session_exited``, and deliberately **not** that name.
#:
#: ``session_exited`` carries ``exit_code`` and ``clean``, both of which its watcher
#: got from ``Popen.wait``. The sweep has neither and cannot: the process it is
#: reconciling was waited on by a thread in a daemon that is gone. Reusing the name
#: would mean either inventing those fields (a ``clean`` nobody observed, on the one
#: event whose whole job is to say what is known) or emitting them empty, where any
#: consumer that reads ``data["clean"]`` as a truth value — the obvious way to read
#: it — would score every reconciled clean ending as a crash. A separate name keeps
#: every existing consumer of ``session_exited`` exactly as correct as it was: this
#: is not one of those events, and it does not arrive as one.
#:
#: What its ``data`` says instead: the session, the run key, the run's committed
#: status *at the time of the sweep*, and ``exit_code: null`` — explicitly unknown,
#: since the ending itself is evidence-based rather than observed.
#:
#: ``run_status`` is a reading and not an authorisation: :func:`_ended_as_asked`
#: fires precisely when the status is *not* terminal, and what authorised the
#: removal is then on the entry instead.
SESSION_ENDED_UNWATCHED = "session_ended_unwatched"

#: Event type for a milestone a session signalled (T122). The platform's own
#: history rather than the run's: the record is a fact about a *session*, and the
#: run dir is written from inside the container by ``work``, which this daemon does
#: not push to.
SESSION_SIGNALLED = "session_signalled"

#: Digest class for a signal on the assistant's spool. Deliberately not a member
#: of :data:`lmer_platform.inventory.ATTENTION_REASONS` — see the module docstring:
#: everything on that axis means a person must act, and this one means the
#: orchestrator has a next step to take. The name says which session-side act
#: produced it, because the assistant's own instructions talk about ``lmer-signal``.
SIGNAL_DIGEST_KIND = "session_signal"

#: Digest class for a question that is no longer being asked (issue #254). Beside
#: :data:`SIGNAL_DIGEST_KIND` rather than on the attention axis, and for the same
#: reason: an answered question means the *orchestrator* has a next step, while
#: every member of :data:`lmer_platform.inventory.ATTENTION_REASONS` means the
#: operator does — and this one exists precisely because the operator has already
#: acted. The name is what happened, not what cleared, because it is what the
#: assistant's own instructions call the event.
#:
#: It names the common case rather than the only one: a session that closes its
#: own question unanswered clears the condition the same way, and the payload
#: cannot tell the two apart (module docstring). The *note* says so —
#: :meth:`Signal.answered_digest` claims answered *or withdrawn* — while this
#: label stays the one word the spool sorts on and the instructions already use.
QUESTION_ANSWERED_KIND = "question_answered"

#: The attention reasons whose *disappearance* is material — the exception the
#: module docstring argues for, and the only one. Both are questions a human
#: answers (:data:`lmer_platform.inventory.ATTENTION_REASONS` explains why they
#: are two reasons and not one name), which is what makes their clearing an event
#: rather than a run merely getting better. Named here rather than derived,
#: unlike everything else on that axis: nothing on the record says "a person can
#: answer this", and a reason added there later must not silently start claiming
#: it was answered.
#:
#: Written in *lifecycle* order — the live session's ask first, then the record a
#: stopped run leaves behind — which is what :func:`_question_rank` reads when it
#: has to choose between two spellings on one run. The membership is what the
#: clear is keyed on; the order matters only to that tie-break.
QUESTION_KINDS = ("live_question", "question")

#: Where the high-water marks live: ``{"sessions": {"<id>": {"seq": N, ...}}}``,
#: beside the daemon's other snapshots (spec §6.1) and written only from here.
#: Persisted rather than held in memory because "exactly once" has to survive a
#: daemon restart, which is precisely when a session is signalling into a channel
#: nobody is reading yet.
SIGNAL_MARKS_FILE = "signals.json"

#: Longest send-error text an :data:`ASSISTANT_NUDGED` event carries. Bounded like
#: every other composed string in this package.
MAX_SEND_ERROR_CHARS = 500

#: What the platform records when it has typed a nudge into the assistant's
#: session (issue #317). An event and not only a log line because the platform
#: wrote into a session unasked, and "who typed that" must stay answerable from
#: the same history that records a wind-down.
ASSISTANT_NUDGED = "assistant_nudged"

#: What the platform records when it has recommended a rotation to the
#: assistant (issue #446). An event for :data:`ASSISTANT_NUDGED`'s reason —
#: the platform typed into a session unasked — and the only durable trace of
#: why an incarnation ended, since the rotation that follows is the
#: assistant's own decision and records nothing about what prompted it.
ASSISTANT_ROTATION_RECOMMENDED = "assistant_rotation_recommended"

#: What the platform records when the threshold is set and the assistant's
#: harness publishes no context figure to compare it against (issue #446).
#: Once per incarnation: an operator who set a threshold and hears nothing
#: has no other way to tell a quiet window from a reading that never arrives.
ASSISTANT_ROTATION_UNREADABLE = "assistant_rotation_unreadable"

#: Run states whose *arrival* is material even though nothing needs a human.
#: ``complete`` only: §8.3's "a task finished", read off the state axis because a
#: finished run deliberately carries no attention record. Names from
#: :data:`lmer_platform.inventory.RUN_STATES`; adding ``failed`` here would
#: duplicate the ``critical_error`` attention reason that already covers it.
MATERIAL_STATES = ("complete",)

#: Signal kinds whose claim is an **absence** in the work-repo mirror, and which
#: are therefore re-read against a caught-up mirror before they are delivered.
#: See :meth:`Detector._confirmed`.
ABSENCE_KINDS = ("crashed",)

#: What an absorbed failure cost, for the stages that simply do not run. Named
#: because one stage's failure costs something else — see
#: :meth:`Detector._absorb`.
_TICK_SKIPPED = "this tick is skipped and detection keeps going"


def _text(value: object) -> Optional[str]:
    """A non-empty string, or ``None`` for anything else.

    Every field read here comes out of a JSON payload assembled from files an
    operator can hand-edit, so the whole module treats a wrong-typed field as a
    missing one — the same tolerance :func:`lmer_platform.inventory
    ._load_state_tolerantly` applies one layer down.
    """
    return value if isinstance(value, str) and value else None


def _clamp(text: str, limit: int) -> str:
    """*text* cut to *limit* characters, ending in an ellipsis when cut.

    Not decoration: :func:`lmer_platform.assistant.notify` *refuses* a note over
    :data:`lmer_platform.assistant.MAX_NOTE_CHARS`, and the note here can carry a
    question an operator wrote at any length. Truncating is a shortened digest;
    not truncating is a refused notification for exactly the runs whose questions
    are long enough to need care.

    The rule is :func:`lmer_platform.store.clamp_text`'s; this name survives
    because it is what readers of this module reach for.
    """
    return clamp_text(text, limit)


@dataclass(frozen=True)
class Signal:
    """One material condition observed on one run.

    Frozen and value-typed so the diff is a set operation rather than a walk over
    mutable rows: :attr:`key` is what "the same condition" means (see the module
    docstring on why ``since`` is in it), and everything else on the signal is
    what gets said about it once it turns out to be new.
    """

    host: Optional[str]
    project: Optional[str]
    slug: Optional[str]
    #: An attention reason from :data:`lmer_platform.inventory.ATTENTION_REASONS`,
    #: or a state from :data:`MATERIAL_STATES`. Not an enum here on purpose — the
    #: vocabulary belongs to :mod:`lmer_platform.inventory`, and a copy of it
    #: would be a second list to forget to update.
    kind: str
    note: Optional[str] = None
    since: Optional[str] = None
    label: Optional[str] = None
    state: Optional[str] = None
    session: Optional[str] = None

    @property
    def ref(self) -> str:
        """``<host>/<project>/<slug>`` — the identity every run route takes."""
        if self.kind == "stuck_teardown" and self.session:
            return f"session {self.session}"
        return runref.compose(self.host, self.project, self.slug)

    @property
    def key(self) -> tuple:
        """What makes two observations the same condition."""
        return (self.ref, self.kind, self.since or "")

    @property
    def digest(self) -> str:
        """The one line the assistant is woken with.

        Leads with the run reference rather than the human label because the
        assistant's next move is an API call, and ``host``/``project``/``slug``
        is what ``/api/runs/answer`` and ``/api/runs/resume`` take. The label is
        on :meth:`data` for when it wants to speak about the run instead.
        """
        if self.kind == "stuck_teardown" and self.note:
            return _clamp(self.note, assistant.MAX_NOTE_CHARS)
        if self.kind in MATERIAL_STATES:
            text = f"{self.ref} is now {self.kind}"
        else:
            text = f"{self.ref} needs you — {self.kind}"
            if self.note:
                text = f"{text}: {self.note}"
        return _clamp(text, assistant.MAX_NOTE_CHARS)

    @property
    def answered_digest(self) -> str:
        """The one line for this condition having *gone* (:data:`QUESTION_KINDS`).

        Names the run and nothing else it would have to be read back to know: the
        answer's own text is on the run, not in this tick's payload — the
        attention record that carried the question is exactly what disappeared —
        and a fleet-wide sweep is what naming the run is here to save.

        "Answered **or withdrawn**" is the strongest thing that record's absence
        supports, and the weaker word is the load-bearing one: a session can close
        its own question unanswered and keep running, which clears the condition
        identically and leaves nothing in the payload to distinguish it (module
        docstring). Saying "answered" alone would tell the orchestrator a human
        replied, and the move it makes on that is to *skip* the read that would
        have shown nobody did.
        """
        return _clamp(
            f"{self.ref} is no longer waiting — its {self.kind} was answered "
            "or withdrawn",
            assistant.MAX_NOTE_CHARS,
        )

    def data(self) -> dict:
        """The structured half of the digest: enough to act without a fleet read."""
        data = {
            "host": self.host,
            "project": self.project,
            "slug": self.slug,
            "label": self.label,
            "kind": self.kind,
            "state": self.state,
            "since": self.since,
        }
        if self.session:
            data["session"] = self.session
        return data


def _identity(row: object) -> Optional[tuple]:
    """``(host, project, slug)`` off one fleet row, or ``None``.

    ``None`` for a row the inventory built from a session with no run identity
    yet (spawned seconds ago, nothing committed): :func:`lmer_platform.inventory
    ._view_from_session` fills those fields with :data:`_UNIDENTIFIED`. Nothing
    here can name such a row to the assistant in a way the assistant could act on
    — every run route takes host, project and slug — and it will have an identity
    by the time it has anything material to say.
    """
    if not isinstance(row, dict):
        return None
    identity = [_text(row.get(field)) for field in ("host", "project", "slug")]
    if not all(identity) or _UNIDENTIFIED in identity:
        return None
    return tuple(identity)


def _signals_of_row(row: object) -> list:
    """Every material condition one fleet row carries, or an empty list.

    A row can in principle contribute both an attention reason and a material
    state; today's :func:`lmer_platform.inventory._derive` never returns both at
    once (a ``complete`` run has no attention record), and this does not depend on
    that staying true.
    """
    identity = _identity(row)
    if isinstance(row, dict):
        attention = row.get("attention")
        session = row.get("session")
        if (isinstance(attention, dict) and attention.get("reason") == "stuck_teardown"
                and isinstance(session, dict) and _text(session.get("id"))):
            host, project, slug = identity or (None, None, None)
            return [Signal(
                host=host, project=project, slug=slug, kind="stuck_teardown",
                note=_text(attention.get("note")), since=_text(attention.get("since")),
                label=_text(row.get("label")), state=_text(row.get("state")),
                session=_text(session.get("id")),
            )]
    if identity is None:
        return []
    host, project, slug = identity

    state = _text(row.get("state"))
    label = _text(row.get("label"))
    found = []

    attention = row.get("attention")
    if isinstance(attention, dict):
        reason = _text(attention.get("reason"))
        if reason:
            found.append(Signal(
                host=host, project=project, slug=slug, kind=reason,
                note=_text(attention.get("note")),
                since=_text(attention.get("since")),
                label=label, state=state,
            ))

    if state in MATERIAL_STATES:
        found.append(Signal(
            host=host, project=project, slug=slug, kind=state,
            since=_text(row.get("updated")), label=label, state=state,
        ))
    return found


def _signals(payload: object) -> dict:
    """The whole fleet payload reduced to ``{key: Signal}``, in payload order.

    Reads ``runs`` rather than ``attention`` because the second is a subset of the
    first (:meth:`lmer_platform.inventory.Inventory.to_dict`) and a finished run
    is only in the first. Insertion order is the inventory's own sort — attention
    first, most urgent first — so a tick that finds several new conditions
    notifies about the most urgent one first.
    """
    rows = payload.get("runs") if isinstance(payload, dict) else None
    found: dict = {}
    for row in rows if isinstance(rows, list) else []:
        for signal in _signals_of_row(row):
            found.setdefault(signal.key, signal)
    return found


def _question_refs(signals: dict) -> set:
    """The runs in *signals* carrying at least one :data:`QUESTION_KINDS` condition.

    The whole of what a *clear* is keyed on (module docstring): not the condition
    key the diff uses, because a run that is re-asked or whose live ask becomes a
    stopped run's ``question`` changes that key without ceasing to wait on a
    person.
    """
    return {
        signal.ref for signal in signals.values() if signal.kind in QUESTION_KINDS
    }


def _question_rank(signal: Signal) -> int:
    """How late in a question's life this spelling is; higher wins a tie.

    Only reached when one run carries both spellings at once, which today's
    :func:`lmer_platform.inventory.build_inventory` does not produce — a row has
    at most one attention record, and the one shape that puts two rows on one run
    (a session entry no run-dir row claims) reads ``crashed``. It is a tie-break
    rather than an assertion because this module takes the fleet payload as it
    finds it, and "whichever came first in the payload" is the kind of ordering
    that changes under an unrelated sort. The stopped run's ``question`` wins: it
    is the later stage, so it is the condition the run is still remembered by.
    """
    return QUESTION_KINDS.index(signal.kind)


def _refs(payload: object) -> set:
    """Every run the payload names, material or not.

    Deliberately not derivable from :func:`_signals`: a run with nothing material
    to say is absent there and present here, and that difference is the whole of
    how a question that stopped being asked is told apart from a run that left the
    fleet (module docstring).
    """
    rows = payload.get("runs") if isinstance(payload, dict) else None
    found = set()
    for row in rows if isinstance(rows, list) else []:
        identity = _identity(row)
        if identity is not None:
            found.add("/".join(identity))
    return found


# --- reconciling endings nothing watched -------------------------------------
#
# Separate from everything above it: no signal is computed here, nothing is
# notified, and the diff neither reads nor writes any of it. What the two share is
# the tick they run on (see the module docstring).

def _run_identity(entry: dict) -> Optional[tuple]:
    """``(host, project, slug)`` off a session's registry entry, or ``None``.

    Same read :func:`lmer_platform.inventory._session_key` makes. A session with no
    run identity — spawned, nothing committed yet — has no run state to consult, so
    there is nothing that could authorise removing its entry.
    """
    run = entry.get("run")
    if not isinstance(run, dict):
        return None
    host, project, slug = run.get("host"), run.get("project"), run.get("slug")
    if not all(isinstance(value, str) and value for value in (host, project, slug)):
        return None
    return (host, project, slug)


def _run_uid(entry: dict) -> Optional[str]:
    """The registry entry's execution uid, or ``None`` for a schema-1 launch."""
    run = entry.get("run")
    uid = run.get("uid") if isinstance(run, dict) else None
    return uid if isinstance(uid, str) and runs.UID_RE.fullmatch(uid) else None


def _committed_status(
    config: PlatformConfig, identity: tuple, uid: Optional[str] = None
) -> Optional[str]:
    """The ``status`` in the run's committed state, or ``None`` if not knowable.

    Read out of the mirror as it stands, with no pull: the sweep is a read on the
    same throttled surface the fleet view uses (module docstring), and a run whose
    final commit has not arrived yet simply is not swept on this tick.

    Tolerant of everything, and every ``None`` here means "leave the entry alone":
    a run dir not in the mirror, a corrupt or newer-schema ``state.yaml`` (the case
    :func:`lmer_platform.inventory._load_state_tolerantly` surfaces as
    ``unreadable``), a state file with no status. The bar for removing the evidence
    of a possible crash is a run that says it finished, and nothing less.
    """
    # By uid when the launch has one: the slug lookup answers with the group's
    # newest record, which for a schema-2 group may be another launch entirely.
    ref = resolve_run_dir(config, *identity, uid=uid)
    if ref is None:
        return None
    try:
        state = run_state.load_state(ref.path)
    except (run_state.RunStateError, OSError) as exc:
        logger.debug(
            "platform_sweep_state_unreadable run=%s error=%s", ref.rel_path, exc
        )
        return None
    status = state.get("status") if isinstance(state, dict) else None
    return status if isinstance(status, str) else None


def _reconcile(
    session_id: str, identity: tuple, status: Optional[str], *, scrub: bool
) -> bool:
    """Remove one stale entry and record the ending. ``True`` when it went.

    The removal comes first and the event only follows a removal that happened:
    this event asserts that a session's ending was reconciled, so emitting it for
    an entry that is still sitting there would put a false ending in the one
    append-only file nobody prunes. ``_watch`` writes its event first for the
    opposite reason — it *observed* the exit, and that observation must not be lost
    if the removal fails.

    ``force`` for the reason :func:`lmer_platform.registry.prune_dead` gives
    verbatim: these sessions are dead by definition and their entries are
    frequently owned by a *previous* daemon, so honouring an owner that no longer
    exists would leave exactly the litter this exists to clear.

    The transcript scrub is the other half of what ``_watch`` does on the way out,
    and it is *needed more* here than there: for a re-attached session nothing has
    ever scrubbed it, so raw credential shapes have been sitting in the file since
    the daemon that would have done it died. Safe to run from here on the same
    grounds it is safe there — the session's process is gone — plus one this side
    has and ``_watch`` does not (the run has committed a terminal status, so the
    harness is finished, not merely detached from its parent) and one the operation
    has by construction: it is idempotent, since a second pass over an
    already-masked file matches nothing and rewrites nothing
    (:func:`lmer_platform.transcripts.scrub_transcript`). The hazard it documents —
    a lingering grandchild appending into the replaced inode — is the same hazard
    in both places and no larger in this one.

    Which is why *scrub* is the caller's decision: an entry admitted only by the
    wind-down mark is the population that ground excludes — a re-attached session
    whose registry pid died with the host PTY may still be writing its transcript,
    and scrubbing there would truncate a live file.
    """
    if not registry.remove(session_id, force=True):
        logger.debug(
            "platform_sweep_entry_not_removed id=%s — nothing was reconciled",
            session_id,
        )
        return False
    host, project, slug = identity
    append_event(
        SESSION_ENDED_UNWATCHED,
        note=session_id,
        data={
            "session": session_id,
            "run": {"host": host, "project": project, "slug": slug},
            "run_status": status,
            # Explicitly unknown, never a guessed ``clean`` — see
            # :data:`SESSION_ENDED_UNWATCHED`.
            "exit_code": None,
        },
    )
    # The ports file goes the way it does in ``_watch``: it has no readers once the
    # session is gone, while both *logs* do (they are the scrollback source) and
    # stay.
    try:
        ports_file_for(session_id).unlink()
    except OSError:
        pass
    if not scrub:
        logger.info(
            "platform_sweep_reconciled id=%s run=%s/%s/%s status=%s — the operator "
            "asked this session to end and no watcher saw it fail; its entry is "
            "cleared. The transcript is left alone: this run recorded no terminal "
            "status, so nothing here says the harness has stopped writing",
            session_id, host, project, slug, status,
        )
        return True
    try:
        transcripts.scrub_session_transcripts(session_id)
    except OSError as exc:
        # The entry is already gone, which is what the reconciliation was for. Loud
        # enough to notice, because what is left behind is an unscrubbed transcript.
        logger.warning(
            "platform_sweep_scrub_failed id=%s error=%s", session_id, exc
        )
    logger.info(
        "platform_sweep_reconciled id=%s run=%s/%s/%s status=%s — its entry was "
        "stale with no reaper, and the run's own record says it finished; the fleet "
        "view stops reading it as crashed. The exit code is not knowable",
        session_id, host, project, slug, status,
    )
    return True


def sweep_finished_sessions(config: PlatformConfig) -> list:
    """Clear the registry entries of sessions that ended with nobody watching.

    Returns the ids reconciled. The condition, all four parts of it, is the whole
    function: an entry that is **there**, a pid that is **dead**, a run identity on
    the entry, and either a **terminal committed status** for that run
    (:data:`lmer_platform.inventory._TERMINAL_STATUSES`) or an ending the operator
    asked for and no watcher saw fail (:func:`_ended_as_asked`). Anything else is
    left alone — most of all a dead entry whose run state is not terminal and
    whose ending nobody asked for, which is a crash, and whose entry is the only
    record that it happened.

    The terminal statuses are imported rather than restated, and that matters in
    both directions: a list here that lacked one would leave those runs reading
    ``crashed`` forever (the bug this exists to fix, wearing a new hat), and one
    that had a status ``inventory`` does not consider terminal would delete the
    evidence of a crash. One definition, one behaviour.

    Live entries are never touched, including a live session whose run has already
    committed ``complete`` — a run that marks itself finished while its session is
    still winding down is ordinary, and removing that entry would drop a running
    session out of the fleet view and out of the concurrency cap that counts it.
    """
    reconciled = []
    for entry in registry.list_sessions(live_only=False):
        if entry.get("live"):
            continue
        session_id = entry.get("id")
        if not isinstance(session_id, str) or not session_id:
            continue
        identity = _run_identity(entry)
        if identity is None:
            continue
        status = _committed_status(config, identity, _run_uid(entry))
        finished = status in _TERMINAL_STATUSES
        if not finished and not _ended_as_asked(entry):
            continue
        if _reconcile(session_id, identity, status, scrub=finished):
            reconciled.append(session_id)
    return reconciled


def _ended_as_asked(entry: dict) -> bool:
    """Did this dead entry end the way the operator asked it to?

    The second reconciliation condition, and the sweep is the only clearing path
    these entries have: the UI's "clear crashed sessions" card renders on a
    ``crashed`` row, which they no longer are
    (:func:`lmer_platform.inventory._derive`). Deliberately the same pair of
    facts the label is made of, so the two can never disagree.
    """
    return _was_wound_down(entry) and not _observed_bad_exit(entry)


# --- milestones a session signalled -------------------------------------------
#
# The third job on the tick (module docstring). Nothing here diffs anything: the
# session wrote a file saying what happened, and what this owes it is delivery
# exactly once.

@dataclass(frozen=True)
class SessionSignal:
    """One milestone one session filed on its ask channel.

    A different animal from :class:`Signal` beside it, and the names are worth
    reading carefully: that one is a *condition* this module inferred from a fleet
    row and identifies by ``(run, kind, since)``; this one is a *record* an agent
    wrote, identified by the session it came from and the entry id it was filed
    under. The two share the notify seam and nothing else.
    """

    session: str
    entry_id: str
    seq: int
    text: str
    at: str
    host: Optional[str] = None
    project: Optional[str] = None
    slug: Optional[str] = None

    @property
    def ref(self) -> Optional[str]:
        """``<host>/<project>/<slug>``, or ``None`` for a session with no run yet.

        A session spawned seconds ago has committed nothing, so there is no run to
        name — and unlike an attention condition, a signal from one is still worth
        delivering: the session id is enough to reach it
        (``/api/sessions/{id}/input``), and "I have pushed the MR" from a session
        the assistant just started is exactly the message that arrives before the
        first commit.
        """
        if not (self.host and self.project and self.slug):
            return None
        return runref.compose(self.host, self.project, self.slug)

    @property
    def digest(self) -> str:
        """The one line the assistant is woken with.

        Leads with the run reference where there is one, as :meth:`Signal.digest`
        does and for the same reason — that triple is what every run route takes —
        and falls back to the session id, which is what the session routes take.

        Clamped to what :func:`lmer_platform.assistant.notify` will accept. The
        text is already scrubbed (:func:`_signal_from_entry`), which is the order
        that matters: masking after this clamp could push a digest back over the
        bound and lose it, and clamping first could cut a credential in half and
        leave a prefix no pattern recognises.
        """
        subject = self.ref or f"session {self.session}"
        return _clamp(f"{subject} signalled: {self.text}", assistant.MAX_NOTE_CHARS)

    def data(self) -> dict:
        """The structured half of the digest.

        Flat ``host``/``project``/``slug``, like :meth:`Signal.data`, so the
        assistant reads one shape for both digest classes; plus the session and the
        entry id, which are what make a re-delivered signal recognisable as the
        same one.
        """
        return {
            "session": self.session,
            "signal": self.entry_id,
            "host": self.host,
            "project": self.project,
            "slug": self.slug,
            "text": self.text,
            "at": self.at,
        }

    def event_data(self) -> dict:
        """The structured half of the platform event.

        Nested ``run``, unlike :meth:`data` above, because that is the shape every
        neighbour in ``events.jsonl`` uses (:data:`SESSION_ENDED_UNWATCHED`,
        ``session_question_answered``) — one file, one shape, whichever consumer
        greps it.
        """
        run = None
        if self.ref is not None:
            run = {"host": self.host, "project": self.project, "slug": self.slug}
        return {
            "session": self.session,
            "signal": self.entry_id,
            "run": run,
            "text": self.text,
            "at": self.at,
        }


def _signal_from_entry(
    entry: object, session_id: str, identity
) -> Optional[SessionSignal]:
    """Build a :class:`SessionSignal` from a channel entry, or ``None``.

    The scrub is here, once, at the boundary where session-written text stops
    being a file in a container's mount and becomes something the daemon stores:
    both sinks (the platform event and the assistant's spool) get the masked text,
    and neither has to remember. Same rule and same single definition
    :meth:`lmer_platform.assistant.PendingNote.from_dict` applies to a digest's
    ``data`` (T92/T93) — ``notify`` would scrub the note again, and would not have
    scrubbed what ``append_event`` wrote.
    """
    text = getattr(entry, "text", None)
    entry_id = getattr(entry, "id", None)
    seq = getattr(entry, "seq", None)
    if not isinstance(text, str) or not text.strip():
        return None
    if not isinstance(entry_id, str) or not isinstance(seq, int):
        return None
    host, project, slug = identity if identity is not None else (None, None, None)
    return SessionSignal(
        session=session_id,
        entry_id=entry_id,
        seq=seq,
        text=_scrub(text.strip()),
        at=_text(getattr(entry, "at", None)) or "",
        host=host,
        project=project,
        slug=slug,
    )


def _signal_marks() -> dict:
    """``{session_id: highest ingested seq}``, or empty when there is no file.

    Never raises. An unreadable marks file has already been moved aside by
    :func:`lmer_platform.store.read_json`, and the empty answer is the safe
    direction: a milestone delivered twice against one that is never delivered.
    """
    try:
        stored = read_json(snapshot_path(SIGNAL_MARKS_FILE))
    except StoreError as exc:
        logger.warning(
            "platform_signal_marks_unreadable error=%s — starting from empty, so "
            "signals still on a live session's channel may be delivered again", exc,
        )
        return {}
    sessions = stored.get("sessions") if isinstance(stored, dict) else None
    if not isinstance(sessions, dict):
        return {}
    marks = {}
    for session_id, record in sessions.items():
        seq = record.get("seq") if isinstance(record, dict) else None
        if not isinstance(session_id, str) or not isinstance(seq, int):
            continue
        if isinstance(seq, bool):  # ``True`` is an int and would mark seq 1
            continue
        marks[session_id] = seq
    return marks


def new_signals() -> list:
    """Signals no tick has delivered yet, oldest first within each session.

    A pure read: it advances nothing, so a caller that fails between this and
    :func:`record_ingested_signals` re-reads the same list (module docstring on
    deliver-then-mark).

    **Live sessions only**, and tracked ones at that — the registry is what is
    iterated, so a channel directory belonging to no session this host knows about
    is never opened. The residual: a signal filed in the seconds before a session
    exits, on a tick that does not happen before the exit, is not delivered. The
    ending itself is not lost with it — a finished run arrives as ``complete``
    through the diff above — and reading a dead session's channel would instead
    re-announce, at reconciliation time, milestones from a session nothing can act
    on any more.

    Every read is contained the way :func:`sweep_finished_sessions` contains its
    own: one session's unreadable or unparseable channel costs that session's
    signals and nothing else, at ``DEBUG``, because a container that wrote junk
    into its mount must not be able to stop the tick.
    """
    marks = _signal_marks()
    found: list = []
    hub = _comms_hub()
    for entry in registry.list_sessions(live_only=True):
        session_id = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(session_id, str) or not session_id:
            continue
        # A session on the bus (#407) has its milestones delivered by the
        # comms hub; its ask directory is a projection of the same records, and
        # reading both would spool every milestone twice.
        if hub is not None and hub.handles(session_id):
            continue
        try:
            directory = ask.session_ask_dir(session_id)
            if not directory.is_dir():
                continue
            signals = ask_protocol.read_signals(directory)
        except (ask.AskChannelError, ask_protocol.AskError, OSError) as exc:
            logger.debug(
                "platform_signal_channel_unreadable id=%s error=%s — this "
                "session's milestones are skipped this tick", session_id, exc,
            )
            continue
        identity = _run_identity(entry)
        since = marks.get(session_id, 0)
        for signal in signals:
            if signal.seq <= since:
                continue
            record = _signal_from_entry(signal, session_id, identity)
            if record is None:
                logger.debug(
                    "platform_signal_unusable id=%s entry=%s — skipped",
                    session_id, getattr(signal, "id", "?"),
                )
                continue
            found.append(record)
    return found


def _comms_hub():
    """The running comms hub, if the daemon started one (issue #407)."""
    from . import comms

    return comms.get_hub()


def record_ingested_signals(signals: list) -> None:
    """Advance the high-water mark past every signal in *signals*.

    Called after delivery, and the write is what makes "exactly once" survive a
    restart. Nothing is written for an empty list, so a quiet fleet costs no
    snapshot write per tick.

    Marks for sessions the registry no longer holds are dropped on the way
    through: this file would otherwise keep one integer per session that ever
    signalled, for the life of the host, and a session id is never reused.
    Pruning here rather than on every tick is why it is safe to do at all — the
    sessions being kept are read in the same pass that writes the file.

    Raises :class:`lmer_platform.store.StoreError` when the file cannot be
    written, for the caller's stage to absorb: the signals were delivered, and the
    cost of a failed write is that they are delivered again next tick.
    """
    if not signals:
        return
    path = snapshot_path(SIGNAL_MARKS_FILE)
    # One high-water mark per session in one file, so the read and the write are
    # one operation: a concurrent detection pass reading before this one writes
    # would re-lower a mark it never saw raised, and the signal it covers would
    # be delivered a second time.
    with mutating(path):
        marks = _signal_marks()
        for signal in signals:
            if signal.seq > marks.get(signal.session, 0):
                marks[signal.session] = signal.seq
        known = {
            entry.get("id") for entry in registry.list_sessions(live_only=False)
            if isinstance(entry, dict)
        }
        stamp = utc_now_iso()
        write_json(
            path,
            {
                "sessions": {
                    session_id: {"seq": seq, "at": stamp}
                    for session_id, seq in sorted(marks.items())
                    if session_id in known
                }
            },
        )


def _reading_age(status, reading) -> "Optional[float]":
    """How old the context figure is, preferring the figure's own stamp.

    The document's ``updated`` is not the reading's: a codex status line carries
    a dated ``context`` block of its own, refreshed from the rollout counter,
    while the document around it is rewritten on every adapter poll — including
    the polls that re-publish the last reading because the counter has stopped
    being readable (``lmer_cli.codex_usage.CodexUsage.refresh``). Measuring the
    wrapper there would age-check a stamp that is always fresh and let an
    hours-old figure recommend ending a context window, which is the one thing
    :data:`lmer_platform.rotation.MAX_READING_AGE_SECONDS` exists to refuse.

    The document's age is the fallback, and it is the right one for a claude
    status line, whose ``context`` block is ``{"percent": N}`` and is exactly as
    old as the document that carries it.
    """
    if isinstance(reading, dict):
        own = reading.get("age_seconds")
        if isinstance(own, (int, float)) and not isinstance(own, bool):
            return float(own)
    return (status.statusline or {}).get("age_seconds")


class Detector:
    """Ticks, diffs, and notifies the assistant of what is new (§8.3).

    Started from ``lmer platform run`` and from nowhere else, for the same reason
    :class:`lmer_platform.assistant.Supervisor` is — a diagnostic verb that grew a
    background thread would be one an operator cannot run twice while working out
    what is wrong — and shaped like it deliberately: a :meth:`run` that does
    nothing but loop, and a :meth:`tick_once` where every decision actually lives.

    Every seam that would otherwise make a test wait on wall-clock time is a
    parameter (*interval*, *sleep*, *state_reader*, *notifier*), because this
    suite runs in a one-CPU container where a timing assertion is a flake. The
    diff is exercised by calling :meth:`tick_once` by hand; the thread gets a
    smoke test and no more.
    """

    def __init__(
        self,
        config: PlatformConfig,
        *,
        interval: float = DETECT_INTERVAL_SECONDS,
        state_reader=None,
        notifier=None,
        sender=None,
        sleep=None,
    ) -> None:
        self.config = config
        self.interval = interval
        #: Absorbed failures since this detector was created. A health counter,
        #: not a budget: there is no give-up (module docstring).
        self.failures = 0
        self.notified = 0
        #: Stale entries the reconciliation sweep has cleared. A counter for the
        #: same reason ``notified`` is one: the work is otherwise invisible.
        self.reconciled = 0
        #: Milestones ingested off session channels (T122), counted apart from
        #: ``notified`` because the two can disagree: a signal is recorded in
        #: platform history whether or not its digest reached the spool.
        self.signalled = 0
        #: Runs named in a check-in digest (issue #244) — runs rather than
        #: digests, because one digest names all of them.
        self.stale_reported = 0
        #: Questions that cleared while their run stayed in the fleet (issue
        #: #254), counted apart from ``notified`` because this is the one digest
        #: class raised by a condition *going away*.
        self.answered = 0
        #: Nudges typed into the assistant's session (issue #317). Counted
        #: apart because it is the only stage that writes *into* a session.
        self.nudged = 0
        #: Rotations recommended to the assistant (issue #446), counted apart
        #: from ``nudged`` because the two say opposite things about the session:
        #: one asks it to do something, the other to finish and go.
        self.recommended = 0
        #: Absence claims a forced pull withdrew before anyone was told (#324).
        #: A climbing number is a tuning fact — the mirror throttle and the rate
        #: runs finish are out of step — rather than a fault.
        self.unconfirmed = 0
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait
        self._read_state = state_reader or build_state
        self._notify = notifier or assistant.notify
        #: How a nudge reaches the session. A parameter because the real one is
        #: an HTTP call into a container.
        self._send = sender or send_input
        #: The previous tick's conditions. ``None`` means no baseline has been
        #: established, which is the one state in which nothing is notified.
        self._seen: Optional[dict] = None
        # A sweep can persist a new condition before the fleet snapshot fails.
        self._pending_teardowns: set[str] = set()
        #: stage -> (cause, consecutive count), for the log dedupe.
        self._failed: dict = {}
        #: ``{run ref: announced_at}`` for digests spooled but not recorded on
        #: disk; cleared by the first successful write (see :meth:`_check_ins`).
        self._announced: dict = {}
        self._project_detectors: dict = {}
        #: ``(pending_seq, stamp)`` for the nudge this process last typed —
        #: ``_announced``' shape, and the bound when the durable mark cannot be
        #: written (issue #317's review, module docstring). Keyed on the sequence
        #: rather than the accumulation's stamp, which two accumulations a second
        #: apart share. Per process: a restart re-nudges once.
        self._nudged: Optional[tuple] = None
        #: ``(session_id, stamp)`` for the rotation reminder this process last
        #: typed, or ``None`` while the reading is below the threshold — which is
        #: what makes the *next* crossing announce itself immediately instead of
        #: waiting out the window. One slot rather than a map: there is one
        #: assistant (D11), and a new session id replaces what the last one left.
        #:
        #: In memory rather than in ``assistant.json``, unlike the nudge's stamp.
        #: A spool outlives incarnations and drains rarely, so its bound has to
        #: survive a restart; a context reading is a fact about *this* live
        #: session, a rotation resets it to nothing, and a daemon restart while
        #: the window is full costs one extra reminder.
        self._rotation: Optional[tuple] = None
        #: The session already reported as publishing no context figure, so the
        #: notice is once per incarnation rather than once per tick.
        self._context_unreadable: Optional[str] = None
        #: ``(session_id, window, delivery_id, text)`` for the reminder being
        #: attempted — held across retries so the id and its prose stay one pair
        #: (:meth:`_rotation_text`), and dropped the moment one is marked.
        self._rotation_attempt: Optional[tuple] = None
        self._warned_unattributed = False

    def stop(self) -> None:
        """Ask the loop to finish, and cut short the wait it is sitting in."""
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    @property
    def notice(self) -> str:
        """The startup line an operator reads, beside the bind and assistant ones.

        Worth a line because the behaviour is otherwise invisible and looks like
        the assistant polling: an operator who sees it being told things should
        know it was told, by this, on a timer, and that nothing in the fleet view
        waits on any of it.
        """
        return (
            f"🔔 Detection every {self.interval:.0f}s: the daemon computes the "
            "attention list and notifies the assistant of what newly needs a "
            "human (spec §8.3)\n"
            "   — the fleet view and its badge are computed the same way and "
            "never wait on an assistant being alive.\n"
            "   — the same tick clears the registry entry of a session that ended "
            "with no reaper (its run says it finished), so a clean ending stops "
            "reading as a crash.\n"
            "   — and it picks up milestones a session announced itself with "
            "lmer-signal (an MR pushed, a review finished), which reach the "
            "assistant and not your attention list.\n"
            f"   — {self._checkin_notice()}\n"
            f"   — {self._nudge_notice()}\n"
            f"   — {self._rotation_notice()}"
        )

    def _rotation_notice(self) -> str:
        """The rotation half of :attr:`notice`, in one sentence (issue #446).

        Worth a line for :meth:`_nudge_notice`'s reason, and worth saying when it
        is off: this one is off by *default*, so an operator who has heard nothing
        cannot otherwise tell the feature apart from a quiet context window.
        """
        threshold = rotation_settings()["context_threshold"].value
        if not threshold:
            return (
                "context-fullness reminders are OFF on this host "
                "(rotation_context_threshold is 0): uber lmer is never told its "
                "window is filling. POST /api/assistant/config to set a percent."
            )
        return (
            f"context-fullness reminder ON: when uber lmer's own status line "
            f"reports {threshold}% of its context window used, it is told once "
            f"— and again at most every "
            f"{rotation.REMINDER_INTERVAL_SECONDS // 60}m while it stays there — "
            "to rotate itself at its next breakpoint. The daemon never rotates "
            "it; a harness publishing no context figure is reported once instead."
        )

    def _nudge_notice(self) -> str:
        """The nudge half of :attr:`notice`, in one sentence (issue #317).

        Worth a line for :meth:`_checkin_notice`'s reason, doubled: this is the
        one thing here that types into a session rather than spooling for it, and
        a feature that is switched off looks exactly like one that is broken from
        the chat window it would have written to.
        """
        settings = nudge_settings()
        after = settings["after_seconds"].value
        if not after:
            return (
                "digest nudges are OFF on this host (nudge_after_seconds is 0): "
                "an unretrieved spool waits for the assistant's own watch and "
                "nothing else. POST /api/assistant/config to set an interval."
            )
        threshold = settings["pending_threshold"].value
        return (
            f"digest nudge ON: if {threshold} or more digests sit unretrieved "
            f"for {after // 60 or 1}m beside an idle assistant, it types one "
            "reminder into that session — the backstop for a watch that stopped "
            "waking it. The digests themselves are never pushed."
        )

    def _checkin_notice(self) -> str:
        """The check-in half of :attr:`notice`, in one sentence (issue #244).

        Worth a line because it is the one thing here that produces a digest when
        *nothing* happened, and worth saying when it is off: a feature that is
        disabled and one that is broken look identical from the chat window.
        """
        window = checkin_settings()["window_seconds"].value
        if not window:
            return (
                "check-in digests are OFF on this host "
                "(checkin_window_seconds is 0): a run nobody looks at is never "
                "mentioned. POST /api/assistant/config to set a window."
            )
        return (
            f"every {window // 60 or 1}m it also tells the assistant which runs "
            "nobody has *looked at* — the silence case no event can cover, since "
            "a run that stops moving emits nothing at all."
        )

    def start(self) -> threading.Thread:
        """Run the detection loop on a daemon thread."""
        thread = threading.Thread(
            target=self.run,
            name="lmer-platform-detector",
            daemon=True,
        )
        thread.start()
        return thread

    def run(self) -> None:
        """The loop. Returns when the detector is stopped.

        Ticks first and waits afterwards, so the baseline is established while the
        server is still starting rather than an interval into serving. The wait is
        :meth:`threading.Event.wait` on the stop flag by default, so shutdown does
        not have to sit out a full interval.
        """
        while not self._stop.is_set():
            self.tick_once()
            self._sleep(self.interval)

    def tick_once(self) -> list:
        """One read-diff-notify pass. Returns the signals it notified about.

        Never raises. The return value is what tests drive and assert on, and it
        is deliberately the *notified* set rather than everything observed: the
        difference between the two is the entire point of the diff.

        The sweep runs first, and the order is a decision rather than a layout: an
        entry it clears is one the fleet read below no longer sees as a crash, so a
        run that finished with nobody watching arrives at ``complete`` in *this*
        tick's diff instead of the next one's. Its own stage, its own absorption —
        a mirror the sweep cannot read must not cost the detection that follows it,
        and the counters keep the two apart.

        The nudge is the last stage and is likewise absent from the return value:
        it is not a condition at all but a fact about the spool. It runs on **all
        three** exits (:meth:`_nudge_stage`) — the ordinary one, the
        baseline-establishing one, and the absorbed-scan one — because it depends
        on none of what they have or lack: a first tick and an unreadable mirror
        are both states in which a spool can already be sitting unread, and the
        second is the host least able to notice. The rotation reminder
        (:meth:`_rotation_stage`, issue #446) rides beside it on all three for the
        same reason and is absent for the same one: what it reads is the
        assistant's own context window, not the fleet.

        Signal ingestion is a third stage on the same terms, and it is *not* in the
        return value: what comes back is the diff's own fresh conditions, because
        that is what the tests of the diff assert on and a milestone is not one of
        them (:attr:`signalled` is where those are counted). An answered question
        stays out of it for the same reason, being a condition that has gone
        rather than one this tick found (:attr:`answered`).
        """
        try:
            self.reconciled += len(sweep_finished_sessions(self.config))
        except Exception as exc:  # noqa: BLE001 - the tick below is not its dependant
            self._absorb("sweep", exc)
        else:
            self._clear("sweep")

        try:
            self.signalled += len(self._ingest_signals())
        except Exception as exc:  # noqa: BLE001 - neither the sweep nor the diff
            self._absorb("signals", exc)
        else:
            self._clear("signals")

        try:
            self._pending_teardowns.update(teardown.sweep(self.config.stuck_teardown_seconds))
        except Exception as exc:  # noqa: BLE001 - one failed stage must not stop detection
            self._absorb("teardown", exc)
        else:
            self._clear("teardown")

        try:
            payload = self._read_state(self.config)
        except Exception as exc:  # noqa: BLE001 - the daemon serves regardless
            self._absorb("scan", exc)
            # The nudge stage depends on nothing this read provides, and a host
            # whose mirror will not read is where a spool sits unnoticed.
            self._nudge_stage()
            self._rotation_stage()
            return []
        self._clear("scan")
        current = _signals(payload)

        # The fourth stage, on the fleet read this tick already paid for. Its
        # own stage for the sweep's reason: a staleness pass that cannot write
        # must not cost the diff below.
        try:
            self.stale_reported += len(self._check_ins(payload))
        except Exception as exc:  # noqa: BLE001 - one digest, not the tick
            self._absorb("checkin", exc)
        else:
            self._clear("checkin")

        baseline, self._seen = self._seen, current
        newly_stuck, self._pending_teardowns = self._pending_teardowns, set()
        if baseline is None:
            # Existing conditions stay silent across restart. A teardown timer
            # that matured since startup is new, even on the first fleet read.
            fresh = [item for item in current.values()
                     if item.kind == "stuck_teardown" and item.session in newly_stuck]
            for item in fresh:
                self._deliver(item)
            logger.info(
                "platform_detection_baseline conditions=%d fresh_teardowns=%d — "
                "existing conditions stay silent; standing state is what "
                "GET /api/state answers", len(current), len(fresh),
            )
            # The nudge stage still runs: the baseline rule is about not
            # re-announcing conditions, and a spool inherited across a restart is
            # the one that has waited longest.
            self._nudge_stage()
            self._rotation_stage()
            return fresh

        fresh = [signal for key, signal in current.items() if key not in baseline]
        fresh = self._confirmed(fresh)
        for signal in fresh:
            self._deliver(signal)
        # After the fresh ones, so a run whose question cleared *into* another
        # condition — a session that died holding it — is described by what it
        # needs now before it is told what it no longer needs. Still the right
        # order now that the clear is keyed on the run: what that keying removes
        # is the case the order could not have saved (a stale all-clear beside a
        # fresh ask *for the same run*, which can no longer both be raised in one
        # tick), and what it leaves is exactly the case this order is for.
        for signal in self._answered_questions(baseline, current, payload):
            self._deliver_answered(signal)

        # Last on purpose, so it counts what this tick spooled.
        self._nudge_stage()
        self._rotation_stage()
        return fresh

    def _confirmed(self, fresh: list) -> list:
        """*fresh* minus the absence claims a caught-up mirror does not support.

        A ``crashed`` row says a session died having committed nothing, and the
        second half of that is read off a mirror this tick deliberately did not
        pull — so a run that pushed its final state inside the throttle window is
        indistinguishable from one that never pushed, and is announced as a death
        with its work declared lost (#324). The claim is checked before it is
        made, and only when such a signal is fresh in this tick's diff. What the
        forced read costs is in docs/PLATFORM-QUICKSTART.md, "Confirming a
        crash".

        A dropped signal leaves :attr:`_seen` too, so it stays a condition nobody
        was told about. A read that fails delivers anyway: the absence may be
        real, and a mirror that will not answer is the worst moment to go quiet
        about a session that died.
        """
        if not any(signal.kind in ABSENCE_KINDS for signal in fresh):
            return fresh
        try:
            payload = self._read_state(self.config, force_pull=True)
        except Exception as exc:  # noqa: BLE001 - the delivery below is not its dependant
            self._absorb(
                "confirm", exc,
                "the crash it was checking is delivered unconfirmed, because a "
                "mirror that will not answer is the worst moment to go quiet "
                "about a session that died",
            )
            return fresh
        self._clear("confirm")
        confirmed = _signals(payload)
        kept = []
        for signal in fresh:
            if signal.kind not in ABSENCE_KINDS or signal.key in confirmed:
                kept.append(signal)
                continue
            self.unconfirmed += 1
            if self._seen is not None:
                self._seen.pop(signal.key, None)
            logger.info(
                "platform_detection_absence_withdrawn run=%s kind=%s — the mirror "
                "was a pull behind; its run state is there and nothing was "
                "announced", signal.ref, signal.kind,
            )
        return kept

    def _nudge_stage(self) -> None:
        """:meth:`nudge_once` with its failures absorbed, as a tick stage.

        Called from all three of :meth:`tick_once`'s exits, because a spool can
        already be waiting in each — a restart carries one in, an unreadable
        mirror hides one.
        """
        if self._project_stage("_nudge_stage"):
            return
        try:
            self.codex_wake_once()
        except Exception as exc:
            self._absorb("codex-wake", exc)
        try:
            self.nudge_once()
        except Exception as exc:  # noqa: BLE001 - one reminder, not the tick
            self._absorb("nudge", exc)

    def _rotation_stage(self) -> None:
        """:meth:`rotation_once` with its failures absorbed, as a tick stage.

        Its own stage beside the nudge's: the two read different things, and one
        must not be able to cost the other. Called from all three tick exits.
        """
        if self._project_stage("_rotation_stage"):
            return
        try:
            self.rotation_once()
        except Exception as exc:  # noqa: BLE001 - one reminder, not the tick
            self._absorb("rotation", exc)

    def _project_stage(self, method):
        if not projects.enabled() or project_context.current():
            return False
        for project in projects.listing(archived=False):
            identifier = project["id"]
            if identifier not in self._project_detectors:
                self._project_detectors[identifier] = Detector(self.config, notifier=self._notify, sender=self._send)
            with project_context.bound(identifier):
                getattr(self._project_detectors[identifier], method)()
        return True

    def rotation_once(self) -> Optional[rotation.Alert]:
        """Recommend a rotation when the reading has reached the threshold.

        The reading comes from :func:`lmer_platform.assistant.status`, which
        resolves the document for the *live* session id — so a successor is never
        alerted on the reading its predecessor left behind. Nothing here rotates
        anything. Conditions and delivery rules:
        ``docs/PLATFORM-QUICKSTART.md``, "Context-fullness reminders".
        """
        threshold = rotation_settings()["context_threshold"].value
        if not threshold:
            return None
        status = assistant.status()
        if not status.running or not status.session_id:
            return None
        reading = (status.statusline or {}).get("context")
        percent = reading.get("percent") if isinstance(reading, dict) else None
        if percent is None:
            self._report_unreadable_context(status, threshold)
            return None
        # A figure arrived, so a publisher that comes back re-arms the notice.
        if self._context_unreadable == status.session_id:
            self._context_unreadable = None
        alerted_at = None
        if self._rotation and self._rotation[0] == status.session_id:
            alerted_at = self._rotation[1]
        reading_age = _reading_age(status, reading)
        due = rotation.due(
            threshold=threshold,
            percent=percent,
            reading_age=reading_age,
            alerted_at=alerted_at,
        )
        if due is None:
            if percent < threshold:
                # The stay above the threshold ended, so the next crossing is
                # announced when it happens rather than a window later — and it
                # is a *new* reminder, which is why the attempt goes too.
                self._rotation = None
                self._rotation_attempt = None
            return None
        ident, text = self._rotation_text(status.session_id, alerted_at, due)
        adapter = (quiet_health(status.session_id) or {}).get("codex_delivery")
        codex = isinstance(adapter, dict) or (
            getattr(status, "settings", None) or {}
        ).get("harness") == "codex"
        sent_error: Optional[str] = None
        try:
            if codex:
                # Submits only what the adapter has not seen; a state short of
                # confirmed comes back False rather than as a send to mark.
                if not self._codex_notice(status.session_id, adapter, ident, text):
                    return None
            else:
                self._send(status.session_id, text, append_newline=True)
        except SessionIOError as exc:
            self._absorb("rotation", exc)
            # `send_input` reports a native `uncertain` as a *delivered* error,
            # so codex must not read `delivered` as a send: an unknown submission
            # is not a recommendation anyone can be shown to have received.
            # Legacy transports keep their byte-delivery bound.
            if codex or not getattr(exc, "delivered", False):
                return None
            sent_error = _clamp(f"{type(exc).__name__}: {exc}", MAX_SEND_ERROR_CHARS)
        else:
            self._clear("rotation")
        self.recommended += 1
        self._rotation = (status.session_id, utc_now_iso())
        self._rotation_attempt = None
        append_event(
            ASSISTANT_ROTATION_RECOMMENDED,
            note=status.session_id,
            data={
                "session": status.session_id,
                "generation": status.generation,
                "percent": due.percent,
                "threshold": due.threshold,
                "repeat": due.repeat,
                # The age the decision was taken on, which on codex is the
                # reading's own rather than the document's (:func:`_reading_age`).
                "reading_age_seconds": reading_age,
                "delivery_id": ident,
                "send_error": sent_error,
            },
        )
        logger.info(
            "platform_assistant_rotation_recommended session=%s generation=%s "
            "percent=%d threshold=%d repeat=%s delivery=%s — the reminder "
            "recommends a rotation at the assistant's next breakpoint; nothing "
            "here ends an incarnation",
            status.session_id, status.generation, due.percent, due.threshold,
            due.repeat, ident,
        )
        return due

    def _rotation_text(self, session_id: str, alerted_at, due) -> tuple:
        """The delivery id and the frozen prose of the attempt in flight.

        One attempt is one id and one immutable text, because
        :class:`lmer_cli.codex_delivery.CodexDelivery` refuses a reused id
        carrying different text and answers a repeated one from its own receipt.
        A fresh identity is random rather than derived: a detector that restarted
        mid-attempt must not be able to mint the id of an attempt whose text it
        no longer has.
        """
        attempt = self._rotation_attempt
        if attempt and attempt[0] == session_id and attempt[1] == (alerted_at or ""):
            return attempt[2], attempt[3]
        ident = "rotation-" + uuid.uuid4().hex
        text = rotation.prompt(due)
        self._rotation_attempt = (session_id, alerted_at or "", ident, text)
        return ident, text

    def _report_unreadable_context(self, status, threshold: int) -> None:
        """Say once that the threshold has nothing to be compared against.

        The issue's own rule (#446): "when a harness publishes no context figure
        the daemon says so once and does not alert". It says it in the log and in
        ``events.jsonl`` rather than in the session, because the fact is about the
        *host* — an operator who set a threshold and hears nothing needs to know
        it can never fire here, while the assistant can do nothing with it.
        Harness parity for the figure itself is issue #122.

        **Young sessions are exempt.** Every incarnation has a window between its
        container starting and its harness first rendering a status line, and the
        tick lands in it routinely — so below
        :data:`lmer_platform.rotation.MAX_READING_AGE_SECONDS` absence is not yet
        evidence. "Once" means once per *stretch without a reading*: a figure
        arriving re-arms it.
        """
        session_id = status.session_id
        if self._context_unreadable == session_id:
            return
        age = status.age_seconds
        if age is not None and age < rotation.MAX_READING_AGE_SECONDS:
            return
        self._context_unreadable = session_id
        append_event(
            ASSISTANT_ROTATION_UNREADABLE,
            note=session_id,
            data={
                "session": session_id,
                "threshold": threshold,
                # Which of the two absences this was: no document at all, or a
                # document carrying no context block. They are different faults —
                # one is a harness that publishes nothing, the other a harness
                # that publishes without the figure — and only the event can keep
                # them apart afterwards.
                "document": status.statusline is not None,
                "session_age_seconds": age,
            },
        )
        logger.warning(
            "platform_assistant_rotation_unreadable session=%s threshold=%d "
            "document=%s — no context figure to compare the threshold against, "
            "so the rotation reminder cannot fire; said once until one arrives",
            session_id, threshold, status.statusline is not None,
        )

    def codex_wake_once(self) -> None:
        """Use the existing daemon tick to wake Codex even during conversation."""
        state = assistant.read_state()
        if not state.pending:
            return
        status = assistant.status()
        if not status.running or not status.session_id:
            return
        ident = self._codex_accumulation_id(status.session_id, state.pending_seq)
        if state.codex_wake == ident:
            return
        health = quiet_health(status.session_id) or {}
        adapter = health.get('codex_delivery')
        if not isinstance(adapter, dict) or not adapter.get('runtime'):
            return
        if self._codex_notice(status.session_id, adapter, ident,
                              '[platform] Your digest spool has pending items. This is an automatic wake-up, '
                              'not a new task. Take the spool with POST /api/assistant/pending, '
                              'act on what you took, and continue the current conversation.'):
            marked = assistant.mark_codex_wake(status.session_id, state.pending_seq, ident)
            if not marked:
                raise SessionIOError('Codex wake confirmed but acknowledgement was not persisted; idle held')
            append_event('assistant_codex_woken', note=status.session_id, data={
                'accumulation': state.pending_seq, 'pending': len(state.pending),
                'delivery_id': ident, 'marked': marked,
            })
            self._clear('codex-wake')

    @staticmethod
    def _codex_accumulation_id(session_id, pending_seq):
        return 'digest-' + hashlib.sha256(f'{session_id}:{pending_seq}'.encode()).hexdigest()[:32]

    def _codex_notice(self, session_id, adapter, ident, text) -> bool:
        """A stable notice is complete only when its native receipt confirms it."""
        if not adapter or not adapter.get('runtime'):
            return False
        receipt = next((r for r in adapter.get('receipts', []) if r.get('delivery_id') == ident), None)
        if receipt is None or (receipt.get('state') == 'rejected' and receipt.get('retryable')):
            receipt = self._send(session_id, text, append_newline=True, delivery_id=ident)
        if receipt.get('transport') != 'codex-app-server':
            raise SessionIOError('Codex digest notice did not use structured delivery')
        if receipt.get('state') in ('rejected', 'uncertain'):
            raise SessionIOError(f'Codex digest notice {ident} is {receipt["state"]}; original retained')
        return receipt.get('state') == 'confirmed' and receipt.get('submit_confirmed') is True

    def nudge_once(self) -> Optional[nudge.Nudge]:
        """Send a due reminder, then mark its delivery; otherwise return None.

        Codex requires a native receipt before advancing the interval. Legacy
        PTY sends retain their byte-delivery bound. See docs/CODEX-DELIVERY.md.
        """
        settings = nudge_settings()
        after = settings["after_seconds"].value
        if not after:
            return None
        status = assistant.status()
        if not status.running or not status.session_id:
            return None
        state = self._nudge_state()
        threshold = settings["pending_threshold"].value
        # The cheap gates first, with both container-sourced readings omitted —
        # ``None`` proceeds for each — so a young spool costs no HTTP call. The
        # second call is the real decision.
        if nudge.due(
            state,
            running=True,
            idle_seconds=None,
            after_seconds=after,
            pending_threshold=threshold,
        ) is None:
            return None
        reading = session_output_state(status.session_id) or {}
        due = nudge.due(
            state,
            running=True,
            idle_seconds=reading.get("idle_seconds"),
            produced_output=reading.get("produced"),
            after_seconds=after,
            pending_threshold=threshold,
        )
        if due is None:
            # Working, or not yet drawing bytes. Unmarked, so the tick after
            # that changes is the one that tells it.
            return None
        text = nudge.prompt(due)
        adapter = (quiet_health(status.session_id) or {}).get('codex_delivery')
        codex = isinstance(adapter, dict) or (getattr(status, 'settings', None) or {}).get('harness') == 'codex'
        sent_error: Optional[str] = None
        try:
            if codex:
                durable = assistant.read_state()
                primary = self._codex_accumulation_id(status.session_id, state.pending_seq)
                if primary != durable.codex_wake:
                    return None
                due = nudge.due(
                    durable, running=True, idle_seconds=reading.get('idle_seconds'),
                    produced_output=reading.get('produced'), after_seconds=after,
                    pending_threshold=threshold,
                )
                if due is None:
                    return None
                key = f'{status.session_id}:{durable.pending_seq}:{durable.nudged_at or ""}'
                ident = 'digest-idle-' + hashlib.sha256(key.encode()).hexdigest()[:32]
                text = ('[platform] Your digest spool is still pending. This is an automatic idle reminder, '
                        'not a new task. Take the spool with POST /api/assistant/pending, '
                        'act on what you took, and continue the current conversation.')
                if not self._codex_notice(status.session_id, adapter, ident, text):
                    return None
            else:
                self._send(status.session_id, text, append_newline=True)
        except SessionIOError as exc:
            # Codex waits for its receipt. Legacy transports keep their existing
            # positive byte-delivery bound, including a delivered error.
            self._absorb("nudge", exc)
            if codex or not getattr(exc, "delivered", False):
                return None
            sent_error = _clamp(f"{type(exc).__name__}: {exc}", MAX_SEND_ERROR_CHARS)
        else:
            self._clear("nudge")
        # From here on one condition: the bytes reached the session, however the
        # call ended.
        self.nudged += 1
        # Before the durable write, which can fail: this is the bound that does
        # not need a disk. Tagged with its accumulation so it cannot outlive it.
        self._nudged = (state.pending_seq, utc_now_iso())
        marked = assistant.mark_nudged()
        append_event(
            ASSISTANT_NUDGED,
            note=status.session_id,
            data={
                "session": status.session_id,
                "pending": due.count,
                "accumulation": state.pending_seq,
                "waited_seconds": round(due.waited_seconds, 1),
                # ``None`` is a value, not a missing field: idleness was not
                # measurable, which is what explains a mid-turn arrival.
                "idle_seconds": due.idle,
                "repeat": due.repeat,
                "after_seconds": after,
                "marked": bool(marked),
                # Always present; null unless the send raised after delivering.
                "send_error": sent_error,
            },
        )
        logger.info(
            "platform_assistant_nudged session=%s pending=%d waited=%.0fs "
            "idle=%s repeat=%s marked=%s send_error=%s — the spool was "
            "unretrieved past the %ds threshold; the digests themselves still "
            "travel only through POST /api/assistant/pending",
            status.session_id, due.count, due.waited_seconds, due.idle,
            due.repeat, bool(marked), sent_error, after,
        )
        return due

    def _nudge_state(self):
        """The assistant state the nudge decision reads, with this process's own
        nudge stamp folded in when the durable one is missing or older.

        :meth:`_with_pending_announcements`' shape: an in-memory stamp is the same
        fact as a stored one, the stored one wins when at least as new, and
        folding it here keeps :mod:`lmer_platform.nudge` a function of one state.

        Without it, a send that succeeds while ``assistant.json`` cannot be
        written repeats every tick — and that same outage stops the assistant
        draining the spool, so the repeats never end.
        """
        state = assistant.clamp_future_nudged_at()
        if self._nudged is None:
            return state
        seq, stamp = self._nudged
        if seq != state.pending_seq:
            # A different accumulation: the drain ended what this remembered.
            # Dropped, not ignored, so a cycling spool grows no dead windows.
            self._nudged = None
            return state
        stored = state.nudged_at
        stored_age = age_seconds(stored) if stored is not None else None
        # Usability, not text: a corrupt stamp sorts above an ISO one, and
        # ``_window`` discards it anyway — so a lexical compare lost both bounds.
        if stored is None or stored_age is None or stored < stamp:
            return replace(state, nudged_at=stamp)
        return state

    def _answered_questions(
        self, baseline: dict, current: dict, payload: object
    ) -> list:
        """One signal per run that *stopped* having a question this tick.

        Keyed on the run rather than on the condition key everything else uses,
        which is the module docstring's argument in code: a run still carrying a
        question of either spelling is still waiting on a person, however much
        the ``(run, kind, since)`` tuple moved. So a re-ask and a
        ``live_question`` → ``question`` shape change produce nothing here, and
        at most one clear is ever raised per run per tick.

        The signal returned is the baseline's own question for that run, because
        the digest and its ``data`` describe the condition that *went* — the
        payload no longer holds it. :func:`_question_rank` picks when a run
        somehow had both spellings at once.

        The ``payload`` read is the whole of the vanished-run rule: a run that is
        no longer in the fleet at all lost its question to an archive or a
        deletion rather than to an answer, and it is not announced.
        """
        refs = _refs(payload)
        outstanding = _question_refs(current)
        cleared: dict = {}
        for signal in baseline.values():
            if signal.kind not in QUESTION_KINDS:
                continue
            if signal.ref in outstanding or signal.ref not in refs:
                continue
            previous = cleared.get(signal.ref)
            if previous is None or _question_rank(signal) > _question_rank(previous):
                cleared[signal.ref] = signal
        # Baseline order, which is the inventory's own sort (:func:`_signals`).
        return list(cleared.values())

    def _check_ins(self, payload: dict) -> list:
        """Spool one digest naming every run nobody has checked. Returns them.

        The window is read fresh every tick, because a change through
        ``POST /api/assistant/config`` means the next sweep, not the next restart.

        A window of 0 is off — but the *prune* still runs, since ``observe`` is
        the only pruner and the API keeps stamping whatever the window says; an
        early return above it left ``checkins.json`` growing forever on a host
        with the feature switched off.

        A refused digest is retried, unlike a refused signal, because a stale run
        has no ``events.jsonl`` copy to fall back on. Retried per *window* though,
        held in :attr:`_announced` when the marks file will not take it: per tick,
        an unwritable file spooled 120 digests an hour into a 50-note spool and
        evicted every other digest class within half an hour.

        "Delivered" is narrower than it reads — ``notify`` reports liveness, not
        persistence — so a digest lost to a full disk is suppressed for one
        window. That self-heals; the opposite direction is the eviction above.
        """
        window = checkin_settings()["window_seconds"].value
        marks = checkin.observe(payload)
        if not window:
            return []
        marks = self._with_pending_announcements(marks)
        stale = checkin.stale_runs(payload, window=window, marks=marks)
        if not stale:
            return []
        groups = {}
        for run in stale:
            owner = (projects.run_home(run.ref) or {}).get("project_id") if projects.enabled() else None
            groups.setdefault(owner, []).append(run)
        delivered_runs, live = [], False
        for owner, group in groups.items():
            note, data = checkin.digest(group, window=window, caveat=self._unattributed_caveat())
            if owner:
                data["workspace_id"] = owner
            delivered, live = self._spool(note, checkin.STALE_DIGEST_KIND, data)
            if delivered:
                delivered_runs.extend(group)
        stale = delivered_runs
        if not stale:
            return []
        stamped = utc_now_iso()
        try:
            checkin.record_announced(stale)
        except StoreError as exc:
            # Absorbed rather than raised: the stage above would score this a
            # failed pass and re-announce everything next tick, which is the
            # behaviour being fixed.
            for run in stale:
                self._announced[run.ref] = stamped
            logger.warning(
                "platform_checkin_marks_unwritable error=%s — the digest was "
                "spooled and this daemon will remember it for one window in "
                "memory; a restart before the file is writable re-announces it",
                exc,
            )
        else:
            self._announced.clear()
        logger.info(
            "platform_checkin_digest runs=%d window=%ds oldest=%s "
            "assistant_live=%s",
            len(stale), window, stale[0].ref, bool(live),
        )
        return stale

    def _with_pending_announcements(self, marks: dict) -> dict:
        """*marks* with announcements this process made but could not persist.

        Folded in rather than consulted separately, so the staleness computation
        keeps one input: an in-memory ``announced_at`` is the same fact as a
        stored one, and a stored stamp wins because it is at least as new.
        """
        if not self._announced:
            return marks
        merged = dict(marks)
        for ref, at in self._announced.items():
            record = dict(merged.get(ref) or {})
            if at > (record.get("announced_at") or ""):
                record["announced_at"] = at
            merged[ref] = record
        return merged

    def _unattributed_caveat(self) -> Optional[str]:
        """Why this digest will keep arriving whatever the assistant does.

        The one state where the mechanism is knowably broken rather than quiet:
        an assistant on the **shared secret** attributes every read to the
        operator, so nothing it does clears a run. Not hypothetical — it is the
        first deploy that ships this, since an assistant surviving the daemon
        restart is adopted rather than re-launched and keeps its old ``.env``.

        In the digest and not only in the log, because the digest is what will
        not stop and its reader is the one that can act on it.
        """
        if config_module.active_assistant_credential() is not None:
            return None
        if not assistant.status().running:
            # Nothing to attribute, and a fresh start mints a credential.
            return None
        if not self._warned_unattributed:
            self._warned_unattributed = True
            logger.warning(
                "platform_checkin_unattributed — the running assistant holds the "
                "shared secret rather than a minted credential (started before "
                "check-ins existed, adopted across a daemon restart, or a mint "
                "that failed), so nothing it reads registers as a check-in and "
                "this digest repeats every window. Rotate it: POST /api/assistant/rotate"
            )
        return (
            "NOTE: you are running on the shared secret rather than your own "
            "credential, so nothing you read clears a run and this digest will "
            "repeat every window until you are rotated — tell the operator, and "
            "offer POST /api/assistant/rotate."
        )

    def _deliver(self, signal: Signal) -> None:
        """Hand one digest to the assistant's spool, absorbing a refusal.

        Not retried on the next tick, and the baseline is updated either way: a
        digest is best-effort by :func:`lmer_platform.assistant.notify`'s own
        contract — "a failed state write costs one notification, not the detection
        that produced it" — and a caller that retried would re-deliver the whole
        standing list every tick for as long as the spool was unwritable.
        """
        delivered, live = self._spool(
            signal.digest, _clamp(signal.kind, 64), signal.data()
        )
        if not delivered:
            return
        logger.info(
            "platform_detection_notified run=%s kind=%s assistant_live=%s",
            signal.ref, signal.kind, bool(live),
        )

    def _deliver_answered(self, signal: Signal) -> None:
        """Hand one *cleared* question to the spool, on :meth:`_deliver`'s terms.

        Its own method rather than a flag, because the two say opposite things
        about the same run and the digest class is what a reader of the spool
        sorts on. ``data`` is the question's own — the reason that cleared and
        when it started — so the assistant can match this against the digest it
        was woken with when the question opened.
        """
        delivered, live = self._spool(
            signal.answered_digest, QUESTION_ANSWERED_KIND, signal.data()
        )
        if not delivered:
            return
        self.answered += 1
        logger.info(
            "platform_detection_answered run=%s kind=%s assistant_live=%s — the "
            "condition cleared while the run stayed in the fleet, so it was "
            "answered or withdrawn somewhere this daemon was not watching",
            signal.ref, signal.kind, bool(live),
        )

    def _ingest_signals(self) -> list:
        """Deliver the milestones sessions have signalled. Returns what was taken.

        Three things happen per signal and the order is the whole of the dedupe
        contract (module docstring): the platform event first, because it is the
        record and it is cheap; the digest second, best-effort exactly as above; the
        high-water mark last and for the batch, so a failure anywhere above leaves
        the signal to be found again rather than swallowed.

        A refused digest is *not* re-delivered later, for :meth:`_deliver`'s reason,
        and the mark advances over it — the event is still in history, which is the
        copy that does not depend on a spool being writable.
        """
        found = new_signals()
        for signal in found:
            append_event(
                SESSION_SIGNALLED, note=signal.session, data=signal.event_data()
            )
            delivered, live = False, False
            entry = registry.read_session(signal.session) or {}
            if entry.get("kind") != assistant.KIND:
                delivered, live = self._spool(
                    signal.digest, SIGNAL_DIGEST_KIND, signal.data()
                )
            logger.info(
                "platform_signal_ingested session=%s signal=%s run=%s "
                "notified=%s assistant_live=%s",
                signal.session, signal.entry_id, signal.ref,
                bool(delivered), bool(live),
            )
        record_ingested_signals(found)
        return found

    def _spool(self, note: str, kind: str, data: Optional[dict]) -> tuple:
        """``(delivered, live)`` — one call into the notify seam, refusal absorbed.

        Shared by both digest classes so a failure in either is counted, demoted
        and recovered by the same machinery: the ``notify`` stage is about the seam,
        not about what was being said through it.
        """
        try:
            live = self._notify(note, kind=kind, data=data)
        except Exception as exc:  # noqa: BLE001 - one lost digest, not the tick
            self._absorb("notify", exc)
            return False, False
        self._clear("notify")
        self.notified += 1
        return True, bool(live)

    def _absorb(
        self, stage: str, exc: BaseException, outcome: str = _TICK_SKIPPED
    ) -> None:
        """Log a failure once per cause per stage, then demote its repeats.

        Per *stage* rather than one shared cause, so a permanently unwritable
        spool cannot make an unrelated first mirror failure look like a repeat and
        hide it at ``DEBUG``.

        *outcome* is a parameter because :meth:`_confirmed` delivers the crash it
        could not confirm: the shared "this tick is skipped" would tell whoever is
        reconciling a mirror outage that nothing was announced, while they are
        looking at the digest that was.
        """
        self.failures += 1
        cause = f"{type(exc).__name__}: {exc}"
        previous, count = self._failed.get(stage, (None, 0))
        if cause == previous:
            self._failed[stage] = (cause, count + 1)
            logger.debug(
                "platform_detection_failed_again stage=%s consecutive=%d error=%s",
                stage, count + 1, cause,
            )
            return
        self._failed[stage] = (cause, 1)
        logger.error(
            "platform_detection_failed stage=%s error=%s — %s; identical repeats "
            "are logged at debug, and the fleet view does not depend on any of it",
            stage, cause, outcome,
        )

    def _clear(self, stage: str) -> None:
        """Note that *stage* worked again, bounding the silence at the far end."""
        previous = self._failed.pop(stage, None)
        if previous is None:
            return
        logger.info(
            "platform_detection_recovered stage=%s after=%d failure(s) last=%s",
            stage, previous[1], previous[0],
        )
