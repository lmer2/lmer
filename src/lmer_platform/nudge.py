"""The digest nudge: the platform says "something is waiting" out loud (#317).

Why a push exists at all in a pull-only design
----------------------------------------------
:mod:`lmer_platform.assistant` spools digests and nothing takes them until the
assistant does, which is the right shape and stays the shape: the spool is
bounded and scrubbed, and every digest still leaves it through
``POST /api/assistant/pending``. What has to be pushed is one bit — *there is
something waiting* — because the mechanism that was supposed to notice it is a
watch the assistant arms on itself, and a watch is a thing that stops. Measured
on 2026-08-19: ten digests over seventeen minutes, live questions among them,
while the watch missed the 0→pending transition.

So this module decides when the spool has been ignored, and
:class:`lmer_platform.detect.Detector` types :func:`prompt` into the assistant's
own session. What travels is a sentence, never a digest.

The behaviour — the five conditions, the once-per-interval stamp, the error paths
and the configuration — is documented for operators in
``docs/PLATFORM-QUICKSTART.md``, "Digest nudges", and is not restated here.
:func:`due` is the implementation of that section; what follows is what a reader
of *this code* needs and the docs do not carry.

What is a fact here, and what is not
------------------------------------
The count and the stamps are the daemon's own file. The idle reading is measured
by our supervisor off the PTY, with ``None`` reserved for "not knowable" rather
than folded into zero.

**"The assistant heard it" is not a fact and nothing here treats it as one.**
:func:`lmer_platform.session_io.send_input` proves the bytes reached the control
plane and cannot prove the harness's TUI registered the Enter that submits them
(``submit_confirmed`` can be false; issues #210, #231 are that failure). The
remedy is the repeat, not a claim.

**Unknowable idleness proceeds; a session that has never drawn a byte does not.**
Those look identical at :func:`lmer_platform.session_io.session_activity`, which
collapses "no answer", "an image without the fields" and "no output yet" into one
``None``. The first two are genuinely unknown and proceeding is the sanctioned
choice — blocking would switch the safety net off on the hosts least able to
notice. The third is knowable: ``/healthz`` reports ``cursor``, so
:func:`lmer_platform.session_io.session_output_state` carries it apart and
*produced_output* of ``False`` refuses rather than typing into a PTY nobody is
reading yet.

Nothing in this module writes anything
--------------------------------------
:func:`due` is a function of a state, two settings and two readings;
:func:`prompt` is a function of a count and an age. The mark, the event and the
send belong to the caller, which is what lets every boundary be tested without a
container, a daemon or a clock to sleep on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .assistant import AssistantState, MAX_NOTE_CHARS
from .lifecycle import PLATFORM_PREFIX
from .store import age_seconds

__all__ = [
    "PROMPT_ROUTE", "MAX_PROMPT_CHARS", "Nudge", "due", "prompt",
]

#: The route the nudge tells the assistant to call. An agent told "digests are
#: waiting" without it improvises, and what it improvises is a poll.
PROMPT_ROUTE = "POST /api/assistant/pending"

#: Ceiling on the sentence. A regression guard, not a runtime limit: the text is a
#: constant with two numbers in it, nothing truncates, and a test asserts it fits.
MAX_PROMPT_CHARS = MAX_NOTE_CHARS


@dataclass(frozen=True)
class Nudge:
    """A nudge that is due, and the facts that made it due.

    Returned rather than acted on so the caller can log what it is about to do
    and record what it observed. ``idle`` is carried — including as ``None`` —
    because a nudge that arrived at a busy assistant has to be explainable
    afterwards, and the reading is the explanation.
    """

    #: Digests waiting when the decision was taken.
    count: int
    #: How long this accumulation has been waiting, in seconds — **uncapped**,
    #: because the sentence reports it as a fact about the digests. The capped
    #: number lives in :func:`_window` and never leaves the decision.
    waited_seconds: float
    #: The assistant's idle reading, or ``None`` when the platform could not get
    #: one (an older image, a control plane that did not answer).
    idle: Optional[float]
    #: Whether a previous nudge about this accumulation is being repeated. For
    #: the log line; it changes nothing about the text.
    repeat: bool

    @property
    def waited_minutes(self) -> int:
        """The wait in whole minutes, floored at 1 — what the sentence says.

        The floor engages below a minute, reachable only on a host that set the
        interval under 60s: it reads "1 minute" rather than "0 minutes", an
        over-statement of at most 59s against a sentence claiming no wait at all.
        """
        return max(1, int(self.waited_seconds // 60))


def due(
    state: AssistantState,
    *,
    running: bool,
    idle_seconds: Optional[float],
    after_seconds: int,
    pending_threshold: int,
    produced_output: Optional[bool] = None,
    now=None,
) -> Optional[Nudge]:
    """The nudge this spool is owed, or ``None``. See the module docstring.

    Every input is passed in rather than fetched: *state* so the decision and the
    mark it may write come from one read, the two readings because obtaining them
    is an HTTP call into a container, and *now* so no test has to sleep.
    *produced_output* is ``False`` only when the session is *known* never to have
    drawn a byte, ``None`` when that is unknowable.
    """
    if after_seconds <= 0 or not running:
        return None
    count = len(state.pending)
    if count < max(1, pending_threshold):
        return None
    waited = _accumulation_age(state, now=now)
    if waited is None:
        return None
    if _window(waited, state, now=now) < after_seconds:
        return None
    # No TUI is reading yet, so a reminder typed now goes nowhere and would still
    # be marked. Refused rather than proceeded-on-unknown: this case is knowable.
    if produced_output is False:
        return None
    # ``None`` proceeds; see the module docstring for what that costs and why the
    # other direction is worse.
    if idle_seconds is not None and idle_seconds < after_seconds:
        return None
    return Nudge(
        count=count,
        waited_seconds=waited,
        idle=idle_seconds,
        repeat=state.nudged_at is not None,
    )


def _accumulation_age(state: AssistantState, *, now=None) -> Optional[float]:
    """How long this accumulation has been waiting, in seconds.

    ``pending_since`` when the state carries one, because the spool is bounded:
    an age read off the retained notes drifts younger as older ones are evicted,
    so a fleet loud enough to matter would never be nudged. The notes are the
    fallback for a state written before that stamp existed, and there the oldest
    owns the wait.

    ``None`` when nothing readable dates the accumulation: better silence than a
    sentence claiming a wait this cannot measure. A future stamp reads negative
    and fails the threshold on its own.
    """
    if state.pending_since:
        stamped = age_seconds(state.pending_since, now=now)
        if stamped is not None:
            return stamped
    ages = [
        age
        for age in (age_seconds(note.at, now=now) for note in state.pending)
        if age is not None
    ]
    if not ages:
        return None
    return max(ages)


def _window(waited: float, state: AssistantState, *, now=None) -> float:
    """*waited*, capped by the last nudge — the number the **rate limit** reads.

    Kept apart from :func:`_accumulation_age` because one number cannot be both:
    the cap makes this one nudge per window, and rendering it in the sentence told
    an assistant a two-hour backlog was three minutes old.

    An unreadable nudge stamp is ignored rather than fatal — one repeated
    reminder, where honouring it would suppress every future one.
    """
    if not state.nudged_at:
        return waited
    since_nudge = age_seconds(state.nudged_at, now=now)
    if since_nudge is None:
        return waited
    return min(waited, since_nudge)


def prompt(nudge: Nudge) -> str:
    """The exact sentence the platform types. One paragraph, no newlines.

    No newlines is correctness, not formatting: this goes into a TUI that submits
    on Enter, so a newline would send the first line and leave the rest behind.

    It says who is talking, that this is not new work, and what to call — in that
    order, because that is the order they stop an agent answering the operator or
    treating a reminder as a task. It closes on what the nudge implies: the
    assistant's own watch did not wake it.
    """
    one = nudge.count == 1
    digests = "digest has" if one else "digests have"
    minutes = nudge.waited_minutes
    # Claimed only when measured: the nudge still goes out on a host with no idle
    # reading, and must not then assert something it did not check.
    quiet = " and this session has been quiet" if nudge.idle is not None else ""
    return (
        f"{PLATFORM_PREFIX} {nudge.count} {digests} been waiting in your "
        f"spool for {minutes} minute{'' if minutes == 1 else 's'}{quiet}. "
        "This is an automatic reminder from the daemon, "
        "not the operator, and it is not a new task: take the spool with "
        f"{PROMPT_ROUTE}, act on what you took, and tell the operator anything "
        "they need to hear. If you have a watch armed on the pending count, this "
        "line means it did not wake you — check it, because the next reminder is "
        "another wait away."
    )
