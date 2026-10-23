"""The one operator signal when the assistant is not taking its spool (#584).

After :data:`lmer_platform.nudge.MAX_UNANSWERED_NUDGES` reminders go
unanswered, :class:`lmer_platform.detect.Detector` stops typing and posts this
message, once per episode, into the assistant's standing threads. Behaviour and
wording rules are in ``docs/PLATFORM-QUICKSTART.md``, "When the assistant is not
taking its spool".

The reason is a marker, never a diagnosis and never terminal text:
:func:`terminal_reason` names which of :data:`REASON_MARKERS` the assistant's
terminal last showed, so nothing the terminal drew leaves the host.
"""

from __future__ import annotations

import re
from typing import Optional

from lmer_cli import thread_access

from . import session_io
from .store import clamp_text
from .transcripts import scrub_credentials

__all__ = [
    "LABEL", "REASON_MARKERS", "TAIL_BYTES", "deliver", "message", "terminal_reason",
]

#: The attribution ``post_thread`` puts in front of the text.
LABEL = "lmer platform"

#: What the signal may name. Only the marker itself is reported, so a match can
#: never carry the rest of the screen into a thread; a miss says "no known reason".
REASON_MARKERS = ("Login expired", "Please run /login", "authentication_failed", "rate_limit")

#: How much of the terminal's tail is searched. A refusal is drawn by the last
#: thing the harness tried, so it sits near the end.
TAIL_BYTES = 64 * 1024

# Cursor-forward is how a TUI draws a run of spaces, and absolute positioning
# starts another row; every other escape is noise.
_CURSOR_FORWARD = re.compile(r"\x1b\[\d*C")
_CURSOR_ROW = re.compile(r"\x1b(?:\[[\d;]*[HfdEF]|[ED])")
_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_MARKERS = [
    (marker, re.compile(r"[ \t]+".join(map(re.escape, marker.split())), re.IGNORECASE))
    for marker in REASON_MARKERS
]


def terminal_reason(session_id: str) -> Optional[str]:
    """The marker *session_id*'s terminal tail showed last, or ``None``."""
    path, source = session_io.canonical_log(session_id)
    chunk = session_io.read_log_at(path, -TAIL_BYTES, TAIL_BYTES, source=source)
    text = chunk.data.decode("utf-8", errors="replace")
    text = _ESCAPE.sub("", _CURSOR_ROW.sub("\n", _CURSOR_FORWARD.sub(" ", text)))
    found = [(match.end(), marker) for marker, pattern in _MARKERS
             for match in pattern.finditer(text)]
    return max(found)[1] if found else None


def message(*, project: Optional[str], pending: int, waited_minutes: int,
            nudges: int, reason: Optional[str]) -> str:
    """The signal's text: what was seen, what the platform stopped doing."""
    whose = f"The {project} assistant" if project else "The assistant"
    digests = "digest has" if pending == 1 else "digests have"
    minutes = f"{waited_minutes} minute{'' if waited_minutes == 1 else 's'}"
    seen = (f"Its terminal shows `{reason}`." if reason
            else "The platform saw no known reason in its terminal.")
    return (
        f"{whose} is not taking its spool: {pending} {digests} waited {minutes}, "
        f"and {nudges} reminders typed into its session went unanswered, so the "
        f"platform has stopped reminding it. {seen} Reminders and this message stay "
        "off until the spool is taken or a new assistant starts."
    )


def deliver(text: str, threads: list) -> tuple:
    """Post *text* into each of *threads*. Returns ``(posted, failed)``.

    No poster is passed: a connector watching the thread then skips the post as
    its own echo, so it is not typed into the assistant it is about.
    """
    posted, failed = [], []
    for permalink in threads:
        try:
            thread_access.post_thread(permalink, text, label=LABEL)
        except Exception as exc:  # noqa: BLE001 - one thread must not cost the others
            failed.append({"thread": permalink, "error": clamp_text(scrub_credentials(str(exc)), 300)})
        else:
            posted.append(permalink)
    return posted, failed
