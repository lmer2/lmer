"""The context-fullness reminder: the platform says "you are nearly full" (#446).

The daemon holds the threshold because the assistant cannot: reading its own
fullness on a schedule is the polling :mod:`lmer_platform.assistant` exists to
keep out of its turns. What the operator needs to know — the conditions, the two
constants, the configuration, and why an idle session is told nothing — is
``docs/PLATFORM-QUICKSTART.md``, "Context-fullness reminders".

Three things a reader of this code cannot get from there:

- **Nothing here rotates anything.** :func:`due` recommends; the breakpoint rule
  is the ``orchestrate`` taskdef's, because only the assistant knows whether it
  is mid-decision.
- **``percent`` of ``None`` is "not knowable", never 0.** It can never satisfy a
  threshold, and the caller reports it rather than alerting on a figure it does
  not have.
- **Nothing in this module writes anything.** The send, the stamp and the event
  are :mod:`lmer_platform.detect`'s, which is what lets every boundary below be
  tested without a container, a daemon or a clock to sleep on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .assistant import MAX_NOTE_CHARS
from .provenance import PLATFORM_PREFIX
from .store import age_seconds

__all__ = [
    "REMINDER_INTERVAL_SECONDS", "MAX_READING_AGE_SECONDS", "MAX_PROMPT_CHARS",
    "Alert", "due", "prompt",
]

#: How long the platform stays quiet after typing one of these, while the reading
#: stays at or above the threshold. A constant rather than a setting by operator
#: decision (#446): the config group carries one knob.
REMINDER_INTERVAL_SECONDS = 900

#: How old a reading may be and still decide anything. Past it the reading is
#: absent rather than low — undated evidence must not end a context window.
MAX_READING_AGE_SECONDS = 600

#: Ceiling on the sentence: a regression guard, not a runtime limit.
MAX_PROMPT_CHARS = MAX_NOTE_CHARS


@dataclass(frozen=True)
class Alert:
    """A reminder that is due, and the two numbers that made it due.

    ``repeat`` is for the log line alone: it changes nothing about the text,
    because an assistant that missed the first one must not be handed a sentence
    written as though it had read it.
    """

    #: The published reading, in percent of the context window used.
    percent: int
    #: The threshold it reached, in the same unit.
    threshold: int
    #: Whether a previous reminder about this stay above the threshold is being
    #: repeated.
    repeat: bool


def due(
    *,
    threshold: int,
    percent: Optional[int],
    reading_age: Optional[float],
    alerted_at: Optional[str],
    now=None,
) -> Optional[Alert]:
    """The reminder this reading is owed, or ``None``.

    Every input is passed in rather than fetched, so the decision is a pure
    function of what the caller measured — including *now*, so no test sleeps.
    The conditions are the table in ``docs/PLATFORM-QUICKSTART.md``.
    """
    if threshold <= 0:
        return None
    if percent is None:
        return None
    if reading_age is not None and reading_age > MAX_READING_AGE_SECONDS:
        return None
    if percent < threshold:
        return None
    if alerted_at:
        since = age_seconds(alerted_at, now=now)
        # An unreadable or future stamp costs one repeated reminder; honouring it
        # would suppress every future one.
        if since is not None and 0 <= since < REMINDER_INTERVAL_SECONDS:
            return None
    return Alert(percent=percent, threshold=threshold, repeat=bool(alerted_at))


def prompt(alert: Alert) -> str:
    """The exact sentence the platform types. One paragraph, no newlines.

    No newlines is correctness, not formatting: this goes into a TUI that submits
    on Enter. The order — who is talking, the numbers, that nothing has been
    decided — is what stops an agent reading it as the operator asking for a
    rotation now.
    """
    return (
        f"{PLATFORM_PREFIX} Your context window is at {alert.percent} percent and "
        f"this host's rotation threshold is {alert.threshold} percent. This is an "
        "automatic reminder from the daemon, not the operator, and it is not a "
        "new task: nothing has been rotated and nothing will be without you. "
        "Finish what is in front of you, then rotate yourself at the next clean "
        "breakpoint — no question of yours open, no digest taken and not yet "
        "acted on, no half-finished brief. Write your successor a handover, tell "
        "the operator in one line that you are rotating and why, and go. If you "
        "are mid-decision, keep working: this repeats."
    )
