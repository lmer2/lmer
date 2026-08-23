"""Fleet state → Matrix, on transitions only (spec D9).

There is no event stream out of the platform, so this polls ``GET /api/state``
every ``poll_seconds`` and compares each snapshot with the one before it. What
comes out of that comparison is the whole feature: a run that *enters* an
attention state opens a thread, a run that stays there gets at most one reminder
per ``remind_seconds``, a run that leaves gets one line saying so, and a tick
that changed nothing produces silence.

Silence is the part worth defending. A bridge that posted the fleet view every
fifteen seconds would be muted within the hour, and a muted room answers no
questions — which is the entire point of the feature. So this module's default
on every path is to say nothing.

Two shapes of state, deliberately separated:

- :func:`transitions` is pure. Two snapshots in, a list of things-to-say out. It
  is where every cadence rule lives, and it can be read and tested without a
  homeserver, a platform or a clock.
- :class:`Outbound` holds the loop, the thread map and the HTTP. It decides
  nothing about *what* to post.

**What a message carries** (D9): the run's title or name, the reason in plain
words, the attention note the platform already renders, and a link back. Not the
goal, the ledger, the events or the transcript — those are what the link is for.

**The link comes from ``matrix.control_url`` and nothing else.** Everything the
bridge otherwise knows about the daemon is a bind pair, and
``http://127.0.0.1:8765`` in a chat room is worse than no link: it costs the
reader a tap to discover it goes nowhere. Unset means no link (!244 review).
Even set, it is the control UI's front page rather than a per-run URL — the SPA
holds its selection in memory and has no route per run — so the message names
the run and the link opens the view it is in.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from lmer_platform import config as platform_config
from lmer_platform.client import Call, Endpoint, PlatformError, request
from matrix_bridge.config import MatrixConfig
from matrix_bridge.threads import RunKey, ThreadMap

logger = logging.getLogger(__name__)

#: How long the fleet view may take. Generous, because the daemon may be behind
#: a work-repo mirror refresh, and a slow answer is worth more than a retry.
STATE_TIMEOUT_SECONDS = 60.0

#: What :func:`transitions` can emit.
ANNOUNCE = "announce"
REMIND = "remind"
RESOLVED = "resolved"

#: The reasons, in the words a person reads at a bus stop. The two question
#: reasons say what answering *does*, because that is the difference between
#: them and the difference matters: one continues a session that is already
#: running, the other starts a container.
REASON_WORDS = {
    "live_question": (
        "a running session is waiting on an answer — replying here answers it "
        "and the session continues"
    ),
    "question": (
        "stopped on a question — replying here starts a session to continue "
        "the run"
    ),
    "feedback": "waiting on feedback",
    "yield": "stopped at a phase boundary for review",
    "critical_error": "stopped on an unrecoverable error",
    "crashed": "the session is gone and the run recorded no ending",
    "stalled": "the session is up but has stopped producing anything",
    "unreadable": "the run's state could not be read",
    "cap_reached": "waiting on the concurrency cap",
    "slot_contention": "waiting on a service slot",
}


@dataclass(frozen=True)
class Attention:
    """The part of a run's row this module compares between snapshots."""

    reason: str
    note: Optional[str] = None
    since: Optional[str] = None

    @classmethod
    def from_row(cls, row: Mapping) -> Optional["Attention"]:
        attention = row.get("attention")
        if not attention or not attention.get("reason"):
            return None
        return cls(
            reason=attention["reason"],
            note=attention.get("note"),
            since=attention.get("since"),
        )


@dataclass(frozen=True)
class Posted:
    """What the room already knows about one run."""

    attention: Attention
    at: float


@dataclass(frozen=True)
class Transition:
    """One thing to say, and about which run."""

    kind: str
    run: RunKey
    row: Mapping
    attention: Optional[Attention] = None


def transitions(
    previous: Mapping[str, Posted],
    rows: Iterable[Mapping],
    *,
    now: float,
    remind_seconds: int,
) -> tuple:
    """``(what to say, the new record of what the room knows)``.

    The rules, all of them:

    - a run with attention the record does not have → :data:`ANNOUNCE`
    - a run whose attention *reason* changed → :data:`ANNOUNCE` again, because
      ``live_question`` becoming ``question`` is a different situation with a
      different consequence for the person replying, not a continuation
    - the same attention, older than *remind_seconds* since the last message
      about it → :data:`REMIND`
    - a run the record has and this snapshot does not, or one whose attention
      cleared → :data:`RESOLVED`
    - anything else → nothing at all

    *previous* empty means "the room knows nothing", which is what a first tick
    passes and why :meth:`Outbound.start` takes a baseline instead: at startup
    the room usually knows plenty, and re-announcing it is the flood this
    guards against.
    """
    current: dict[str, Posted] = {}
    said: list[Transition] = []
    seen: set = set()

    for row in rows:
        try:
            run = RunKey.from_row(row)
        except (KeyError, TypeError):
            logger.warning("matrix_state_row_unusable row=%r", row)
            continue
        key = str(run)
        seen.add(key)
        attention = Attention.from_row(row)
        known = previous.get(key)

        if attention is None:
            if known is not None:
                said.append(Transition(RESOLVED, run, row, known.attention))
            continue

        if known is None or known.attention.reason != attention.reason:
            said.append(Transition(ANNOUNCE, run, row, attention))
            current[key] = Posted(attention, now)
            continue

        if now - known.at >= remind_seconds:
            said.append(Transition(REMIND, run, row, attention))
            current[key] = Posted(attention, now)
            continue

        # Still waiting, still recent: the room has already been told, and the
        # reminder is not due. Carry the record forward untouched — refreshing
        # ``at`` here would push the reminder out by one tick forever.
        current[key] = known

    for key, known in previous.items():
        if key not in seen:
            said.append(Transition(
                RESOLVED, RunKey.parse(key), {"slug": key}, known.attention,
            ))

    return said, current


def compose(transition: Transition, *, control_url: Optional[str]) -> str:
    """The message for one transition — D9's four things and nothing else."""
    row = transition.row
    label = row.get("title") or row.get("name") or row.get("slug") or "a run"
    attention = transition.attention

    if transition.kind == RESOLVED:
        was = REASON_WORDS.get(
            attention.reason if attention else "", "waiting",
        ).split(" — ")[0]
        return f"{label}: resolved ({was})."

    words = REASON_WORDS.get(
        attention.reason, f"needs a human ({attention.reason})",
    )
    lines = [f"{label} — {words}"]
    if transition.kind == REMIND:
        lines[0] = f"still waiting: {lines[0]}"
    if attention.note:
        lines.append(attention.note)
    if control_url:
        lines.append(control_url)
    return "\n".join(lines)


def platform_endpoint() -> Endpoint:
    """The local daemon, from its own state directory.

    Not :func:`lmer_platform.ctl.resolve_endpoint`: that reads the pair the host
    writes *into a container*, and the bridge is host-side (D1). The credential
    is read from the file and goes straight into a header — it is never an
    argument, an environment variable of this process, or part of a URL.
    """
    config = platform_config.load()
    credential = platform_config.read_secret(config)
    if not credential:
        raise PlatformError(
            f"no platform credential at {config.secret_path}: the bridge talks "
            f"to the daemon on this host, and reads what opens it from that "
            f"file. Start the daemon, or point the bridge at the right state "
            f"directory."
        )
    return Endpoint(config.base_url, credential)


class Outbound:
    """The poll loop: snapshot, diff, post, remember."""

    def __init__(
        self,
        config: MatrixConfig,
        client,
        threads: ThreadMap,
        endpoint: Endpoint,
        *,
        transport=None,
        clock=time.monotonic,
    ):
        self.config = config
        self.client = client
        self.threads = threads
        self.endpoint = endpoint
        self.transport = transport
        self.clock = clock
        self.posted: dict[str, Posted] = {}
        self._unreachable = False

    @property
    def control_url(self) -> Optional[str]:
        return self.config.control_url

    async def snapshot(self) -> Optional[list]:
        """The fleet's rows, or ``None`` when the platform cannot be reached.

        A daemon that is down is not an error worth a message in the room: the
        room is for runs that need a human, and "the bridge cannot see the
        fleet" is an operator's problem, logged once rather than every fifteen
        seconds until someone mutes the room.

        The call itself is ``requests``, which blocks, so it goes to a thread:
        this coroutine shares its loop with the appservice's HTTP server, and a
        60-second state read on that loop is 60 seconds in which no inbound
        answer is delivered (!244 review).
        """
        try:
            response = await asyncio.to_thread(
                request, self.endpoint, Call("GET", "/api/state"),
                timeout=STATE_TIMEOUT_SECONDS, transport=self.transport,
            )
        except PlatformError as exc:
            self._log_unreachable(str(exc))
            return None
        if response.status_code != 200:
            self._log_unreachable(f"the platform answered {response.status_code}")
            return None
        if self._unreachable:
            logger.info("matrix_platform_reachable — polling resumed")
            self._unreachable = False
        payload = response.json()
        return list(payload.get("runs") or [])

    async def start(self) -> None:
        """Take one snapshot without posting — for the runs the room already shows.

        The baseline exists so a restart does not re-announce what is already in
        the room, and **the thread map is the record of what is already in the
        room**. A run with no thread has never been mentioned: recording it here
        would mean it is never announced, its reminders are skipped for having
        no thread to go in, and its resolution is skipped too — permanent
        silence about a run that wants a human (!244 review). That is the first
        install, and every start after a lost or corrupt ``threads.json``.

        So only runs that already have a thread are baselined. The rest are
        announced on the first tick, which is what an operator who just pointed
        the bridge at a waiting fleet expects to see.
        """
        rows = await self.snapshot()
        if rows is None:
            return
        _, baseline = transitions(
            {}, rows, now=self.clock(), remind_seconds=self.config.remind_seconds,
        )
        self.posted = {
            key: record for key, record in baseline.items()
            if self.threads.root_for(RunKey.parse(key)) is not None
        }
        logger.info(
            "matrix_outbound_baseline known=%d unannounced=%d",
            len(self.posted), len(baseline) - len(self.posted),
        )

    async def tick(self) -> list:
        """One poll. Returns the transitions actually posted.

        The record advances **behind** the message, one run at a time. The
        obvious shape — take the new state, then post — loses an announcement
        permanently the first time the homeserver answers 500: the room never
        heard it and the bridge believes it was said (!244 review). Here a send
        that fails leaves that run's record as it was, so the next tick says it
        again; every other run in the same snapshot still advances.
        """
        rows = await self.snapshot()
        if rows is None:
            return []
        said, current = transitions(
            self.posted, rows, now=self.clock(),
            remind_seconds=self.config.remind_seconds,
        )
        touched = {str(transition.run) for transition in said}
        settled = {
            key: record for key, record in current.items() if key not in touched
        }

        posted = []
        for transition in said:
            key = str(transition.run)
            if await self._post(transition):
                posted.append(transition)
                if key in current:
                    settled[key] = current[key]
            elif key in self.posted:
                settled[key] = self.posted[key]
        self.posted = settled
        return posted

    async def _post(self, transition: Transition) -> bool:
        """Say one thing. ``False`` when the homeserver would not take it.

        Only an announcement may **open** a thread. A reminder or a resolution
        for a run the room has never heard of has nothing to remind anyone of:
        opening a thread whose root message is "resolved" is noise at best, and
        after a restart baseline it is the *only* message that run would ever
        get (!244 review).
        """
        text = compose(transition, control_url=self.control_url)
        root = self.threads.root_for(transition.run)
        try:
            if root is None:
                if transition.kind != ANNOUNCE:
                    logger.info(
                        "matrix_outbound_skipped run=%s kind=%s — no thread to "
                        "say it in", transition.run, transition.kind,
                    )
                    return True
                root = await self.client.send_thread_root(text)
                self.threads.bind(root, transition.run)
                logger.info(
                    "matrix_thread_opened run=%s reason=%s", transition.run,
                    transition.attention.reason if transition.attention else None,
                )
                return True
            await self.client.send_in_thread(root, text)
        except Exception:
            logger.exception(
                "matrix_outbound_send_failed run=%s kind=%s", transition.run,
                transition.kind,
            )
            return False
        return True

    def _log_unreachable(self, detail: str) -> None:
        if not self._unreachable:
            logger.warning("matrix_platform_unreachable detail=%s", detail)
            self._unreachable = True
