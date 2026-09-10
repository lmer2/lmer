"""Who posted a message into a chat thread, stamped host-side (issue #427).

Every lmer session posts into Slack through one app and into matrix as one bot
account, so "the bot wrote it" does not answer the question ``docs/COMMS.md``
§7 rule 3 asks — *did I send this?* A :class:`Poster` is that answer, composed
by the host that made the post and never read out of the message body.

``docs/COMMS.md`` §9, *Posts from other runs, and from the platform*, has the
contract: the two origins, the mechanism per transport, why Slack message
metadata is deliberately not one of them, and what a connector does with a
message it cannot attribute.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .runtime import lmer_state_dir
from .threads import CHANNEL_SLACK

logger = logging.getLogger(__name__)

__all__ = [
    "ORIGIN_SESSION", "ORIGIN_PLATFORM", "MATRIX_CONTENT_KEY", "Poster",
    "matrix_content", "poster_from_matrix", "poster_from_slack", "record",
    "lookup", "REGISTRY_DIR", "ENTRY_TTL_SECONDS", "MAX_ENTRIES",
    "Attribution", "INGEST", "SKIP", "WAIT", "GRACE_SECONDS", "HELD_LIMIT",
    "WROTE_LIMIT",
]

#: A run said it, through its thread servicer.
ORIGIN_SESSION = "session"
#: The daemon said it, through ``POST /api/comms/thread`` — the UI's post button
#: or ``lmer-ctl thread post``. There is no session behind such a post, so the
#: label is the one the caller's credential earns.
ORIGIN_PLATFORM = "platform"

_ORIGINS = (ORIGIN_SESSION, ORIGIN_PLATFORM)

#: The matrix event-content key, in the ``net.20c.lmer.*`` namespace
#: :mod:`matrix_bridge.client` already uses.
MATRIX_CONTENT_KEY = "net.20c.lmer.session"

#: Overridable so tests can point the registry at a temp dir, as
#: :mod:`slack_chat.registry` does with its own.
REGISTRY_DIR: Optional[str] = None

#: How long an entry is worth keeping: a connector reads from its cursor, and
#: after its own death replays a bounded window, so an entry older than a long
#: outage answers nothing. Pruned on write.
ENTRY_TTL_SECONDS = 7 * 24 * 3600

#: And how many per conversation. Only lmer's own posts are recorded, so this
#: reaches much further back than any replay window.
MAX_ENTRIES = 500


@dataclass(frozen=True)
class Poster:
    """Who made one post into a thread.

    *session* is set only for :data:`ORIGIN_SESSION`, and is what lets a
    connector tell another run's post from its own.
    """

    origin: str
    session: Optional[str] = None
    label: Optional[str] = None

    @classmethod
    def for_session(cls, session: Optional[str], run: object = None) -> Optional["Poster"]:
        """``None`` when the session id is unknown.

        A session-origin stamp without one is not half-attributed, it is
        unattributable: `is_session` can never match it, so every connector —
        including the one whose session made the post — would read it as another
        run's and could answer its own question with it (review of !300).

        *run* is the run's own name, not the ``[label]`` humans see in the
        thread: both of those labels already begin with "lmer", and only one
        reader may own that word.
        """
        if not session:
            return None
        name = str(run or "").strip()
        return cls(ORIGIN_SESSION, session=session, label=name or None)

    @classmethod
    def for_platform(cls, label: object = None) -> "Poster":
        text = str(label or "").strip()
        return cls(ORIGIN_PLATFORM, label=text or None)

    @property
    def author(self) -> str:
        """How the receiving session's bus names this poster — read by an agent
        as ``[slack · lmer chat-mr-288]`` or ``[slack · platform · operator]``.

        Four forms, and no more: `lmer <run name>`, `lmer session <id>` when the
        run has no name yet, `platform · <credential>`, and bare `platform`.
        """
        if self.origin == ORIGIN_PLATFORM:
            return f"platform · {self.label}" if self.label else "platform"
        if self.label:
            return f"lmer {self.label}"
        return f"lmer session {self.session}"

    def is_session(self, session: Optional[str]) -> bool:
        """Whether this post came from *session* itself."""
        return bool(
            self.origin == ORIGIN_SESSION and self.session and session
            and self.session == session
        )

    def to_dict(self) -> dict:
        out: dict = {"origin": self.origin}
        if self.session:
            out["session"] = self.session
        if self.label:
            out["label"] = self.label
        return out

    @classmethod
    def from_dict(cls, raw: object) -> Optional["Poster"]:
        if not isinstance(raw, dict):
            return None
        origin = raw.get("origin")
        if origin not in _ORIGINS:
            return None
        session = raw.get("session")
        session = session if isinstance(session, str) and session else None
        if origin == ORIGIN_SESSION and session is None:
            return None  # see :meth:`for_session`
        label = raw.get("label")
        return cls(
            origin, session=session,
            label=label if isinstance(label, str) and label else None,
        )


# ---------------------------------------------------------------------------
# matrix: on the event
# ---------------------------------------------------------------------------


def matrix_content(poster: Optional[Poster]) -> dict:
    """The content keys a matrix post carries for *poster*, or nothing."""
    return {MATRIX_CONTENT_KEY: poster.to_dict()} if poster is not None else {}


def poster_from_matrix(content: object) -> Optional[Poster]:
    """The poster a matrix event's content names, or ``None``."""
    if not isinstance(content, dict):
        return None
    return Poster.from_dict(content.get(MATRIX_CONTENT_KEY))


# ---------------------------------------------------------------------------
# Slack: the host-side registry
# ---------------------------------------------------------------------------


def _dir() -> Path:
    """Slack's, and only Slack's: matrix attribution rides on the event content,
    so nothing else has ever written here."""
    base = Path(REGISTRY_DIR) if REGISTRY_DIR is not None else lmer_state_dir() / "thread-posts"
    return base / CHANNEL_SLACK


def _path(conversation: str) -> Path:
    """One file per conversation. *conversation* is a Slack channel id, which
    cannot hold a path separator; stripped anyway so a crafted value cannot name
    a file outside the directory."""
    safe = conversation.replace("/", "_").replace(os.sep, "_")
    return _dir() / f"{safe}.json"


@contextlib.contextmanager
def _locked(path: Path):
    """Serialise one conversation's read-modify-write.

    The daemon runs every session's thread servicer as a thread of itself, so
    two runs posting into one channel at the same moment would otherwise each
    write the file they read and one entry would go missing — costing that post
    its attribution. A fresh ``open`` per call is required rather than
    incidental: ``flock`` is owned by the open file description, so two threads
    of one process contend only while each holds its own fd (the reason
    :mod:`lmer_platform.workrepo` records for the same choice). Locking that is
    unavailable at all leaves the unserialised write, which is the behaviour
    without this.
    """
    try:
        handle = open(str(path) + ".lock", "a+")
    except OSError:
        yield
        return
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            if exc.errno not in (errno.ENOLCK, errno.EOPNOTSUPP, errno.EACCES):
                raise
            logger.debug("thread_post_registry_unlocked path=%s error=%s", path, exc)
        yield
    finally:
        handle.close()


def _load(path: Path) -> list:
    try:
        with path.open("r", encoding="utf-8") as handle:
            entries = json.load(handle)
    except (OSError, ValueError):
        return []
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def record(
    conversation: str, ref: str, poster: Poster, *, now: Optional[float] = None,
) -> bool:
    """Remember that *poster* posted the message *ref* into *conversation*.

    Keyed by the conversation and the message id, deliberately not by the
    thread — the poster and a shell session's connector can hold different
    ``thread_ts`` for one conversation (docs/COMMS.md §9). Best-effort: a
    registry that cannot be written costs the receiving session one message,
    never the post that was already made.
    """
    if not conversation or not ref:
        return False
    stamp = time.time() if now is None else now
    path = _path(conversation)
    entry = {"ref": ref, "at": stamp, **poster.to_dict()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _locked(path):
            entries = [
                item for item in _load(path)
                if item.get("ref") != ref and stamp - _at(item) < ENTRY_TTL_SECONDS
            ]
            entries.append(entry)
            tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(entries[-MAX_ENTRIES:], handle)
            os.replace(tmp, path)
    except OSError as exc:
        logger.warning(
            "thread_post_registry_write_failed conversation=%s error=%s", conversation, exc,
        )
        return False
    return True


def _at(entry: dict) -> float:
    value = entry.get("at")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def lookup(conversation: str, ref: str) -> Optional[Poster]:
    """The poster of message *ref* in *conversation*, as far as this host
    remembers."""
    if not conversation or not ref:
        return None
    for entry in reversed(_load(_path(conversation))):
        if entry.get("ref") == ref:
            return Poster.from_dict(entry)
    return None


def poster_from_slack(channel: str, message: object) -> Optional[Poster]:
    """The poster of one Slack message, from the registry."""
    if not isinstance(message, dict):
        return None
    ts = message.get("ts")
    if not isinstance(ts, str) or not ts:
        return None
    return lookup(channel, ts)


# ---------------------------------------------------------------------------
# The connector's side of the decision
# ---------------------------------------------------------------------------

#: :meth:`Attribution.decide` — relay it, it is another poster's.
INGEST = "ingest"
#: This connector wrote it, or this session did through its own servicer.
SKIP = "skip"
#: Nothing here knows yet. See :data:`GRACE_SECONDS`.
WAIT = "wait"

#: How long a message nothing can attribute is kept and re-asked about: the
#: Slack registry entry is written just *after* Slack accepts the post, so a
#: poll can land in between (docs/COMMS.md §9).
GRACE_SECONDS = 30.0

#: Bounds for unknown-attribution grace and recently written references.
#: Known pending input remains retained until supervisor acceptance.
HELD_LIMIT = 32
WROTE_LIMIT = 256

#: How rarely the "nothing attributed this" line repeats. Not once per
#: connector: the benign unattributable cases (a listener's own ack) would spend
#: the alarm on the first minute after attaching, and a stamp that broke an hour
#: later would then be as silent as the bug this warning exists to reveal
#: (review of !300).
WARN_INTERVAL_SECONDS = 600.0


class Attribution:
    """Retain attributed input until acceptance; batch claims are process-local."""

    def __init__(self, *, poster_of, ref_of, session_of, clock=time.monotonic,
                 warn=None, grace: float = GRACE_SECONDS) -> None:
        self._poster_of, self._ref_of = poster_of, ref_of
        self._session_of, self._clock = session_of, clock
        self._warn, self._grace = warn, grace
        self._session: Optional[str] = None
        self._held: dict = {}
        self._ready: dict = {}
        self._claims: set = set()
        self._wrote: dict = {}
        self._warned_at: Optional[float] = None
        self.lock = threading.RLock()

    @property
    def session(self) -> Optional[str]:
        with self.lock:
            if self._session is None:
                self._session = self._session_of() or None
            return self._session

    def wrote(self, ref: object) -> None:
        with self.lock:
            if not isinstance(ref, str) or not ref:
                return
            self._wrote[ref] = True
            while len(self._wrote) > WROTE_LIMIT:
                self._wrote.pop(next(iter(self._wrote)))

    def decide(self, message: dict) -> tuple:
        with self.lock:
            ref = self._ref_of(message)
            if isinstance(ref, str) and ref in self._wrote:
                self.ack(message)
                return SKIP, None
            poster = self._ready.get(ref) or self._poster_of(message)
            if poster is None or (poster.origin == ORIGIN_SESSION and self.session is None):
                self._hold(ref, message)
                return WAIT, None
            if poster.is_session(self.session):
                self.ack(message)
                return SKIP, None
            self._ready[ref] = poster
            self._hold(ref, message)
            self._claims.add(ref)
            return INGEST, poster

    def due(self) -> list:
        with self.lock:
            ready = []
            for ref, (message, deadline) in list(self._held.items()):
                if ref in self._claims:
                    continue
                if ref in self._wrote:
                    self.ack(message)
                    continue
                poster = self._ready.get(ref) or self._poster_of(message)
                if poster is not None and not (
                    poster.origin == ORIGIN_SESSION and self.session is None
                ):
                    if poster.is_session(self.session):
                        self.ack(message)
                    else:
                        self._ready[ref] = poster
                        self._claims.add(ref)
                        ready.append((message, poster))
                elif self._clock() >= deadline:
                    self.ack(message)
                    self._unattributed(ref)
            return ready

    def ack(self, message: dict) -> None:
        self.ack_ref(self._ref_of(message))

    def ack_ref(self, ref: str) -> None:
        with self.lock:
            self._held.pop(ref, None)
            self._ready.pop(ref, None)
            self._claims.discard(ref)

    def release(self) -> None:
        with self.lock:
            self._claims.clear()

    def snapshot(self) -> dict:
        with self.lock:
            now = time.time()
            return {
                "held": [{"ref": ref, "message": message,
                          "expires": now + max(0, deadline - self._clock()),
                          "poster": self._ready[ref].to_dict() if ref in self._ready else None}
                         for ref, (message, deadline) in self._held.items()],
                "wrote": list(self._wrote),
            }

    def restore(self, state: dict) -> None:
        with self.lock:
            self._held.clear()
            self._ready.clear()
            self._claims.clear()
            self._wrote = dict.fromkeys(state.get("wrote", ()), True)
            for item in state.get("held", []):
                ref = item["ref"]
                self._held[ref] = (item["message"], self._clock() + max(0, item["expires"] - time.time()))
                poster = Poster.from_dict(item.get("poster"))
                if poster is not None:
                    self._ready[ref] = poster

    def _hold(self, ref: object, message: dict) -> None:
        if not isinstance(ref, str) or not ref or ref in self._held:
            return
        unknown = [key for key in self._held if key not in self._ready]
        if ref not in self._ready and len(unknown) >= HELD_LIMIT:
            oldest = unknown[0]
            del self._held[oldest]
            self._unattributed(oldest)
        self._held[ref] = (message, self._clock() + self._grace)

    def _unattributed(self, ref: str) -> None:
        """Say that a post by this app reached nobody — otherwise the failure
        this module exists to fix looks like it did before: silent.

        Rate-limited rather than one-shot, for the reason
        :data:`WARN_INTERVAL_SECONDS` records.
        """
        if self._warn is None:
            return
        now = self._clock()
        if self._warned_at is not None and now - self._warned_at < WARN_INTERVAL_SECONDS:
            return
        self._warned_at = now
        self._warn(
            f"a message this app posted ({ref}) is not being relayed to the "
            "session: nothing on this host recorded which run posted it. Posts "
            "from other runs and from the platform are attributed host-side — "
            "see docs/COMMS.md §9."
        )
