"""Confirm terminal submissions from transcript evidence inside the supervisor."""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import math
import os
from pathlib import Path
import stat
import threading
import time
from typing import Optional

from . import comms

logger = logging.getLogger(__name__)
READ_BYTES = 256 * 1024
MAX_LINE_BYTES = 1024 * 1024
POLL_SECONDS = 0.25


def window_from_env(env=None) -> int:
    """Read the standalone supervisor override; daemon values arrive resolved."""
    raw = (os.environ if env is None else env).get(comms.TERMINAL_CONFIRM_ENV, "")
    try:
        value = int(raw)
        if value > 0:
            return value
    except (ValueError, TypeError):
        pass
    if raw:
        logger.warning("terminal_confirm_window_invalid — using the default")
    return comms.DEFAULT_TERMINAL_CONFIRM_SECONDS


@dataclass
class _File:
    identity: tuple
    offset: int
    baseline: int
    pending: bytes = b""
    oversized: bool = False
    unsupported: bool = False
    sidechain: bool = False


class TranscriptReader:
    """Read newly appended prompts without revisiting previous conversation.

    File reads are bounded and nonblocking on special files. Existing bytes are
    scanned for format declarations but cannot confirm a new terminal line.
    """

    def __init__(self, root: Optional[str], harness: str):
        self.root = Path(root).resolve() if root else None
        self._native = harness if harness in ("claude", "codex", "pi") else None
        self.supported = bool(root) and harness in ("claude", "codex", "pi")
        self._files: dict[Path, _File] = {}
        for path in self._paths():
            try:
                info = path.stat()
                state = self._files[path] = _File(
                    (info.st_dev, info.st_ino),
                    info.st_size if self._native else 0, info.st_size,
                )
                if self._native == "codex":
                    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                    with os.fdopen(fd, "rb") as stream:
                        if stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                            self._metadata(json.loads(stream.readline(MAX_LINE_BYTES)), state)
            except (OSError, ValueError):
                continue

    def _paths(self):
        if self.root is None:
            return
        for directory, subdirs, files in os.walk(self.root, followlinks=False):
            subdirs[:] = sorted(
                d for d in subdirs if self._native != "claude" or d != "subagents"
            )
            for name in sorted(files):
                if not name.endswith(".jsonl"):
                    continue
                yield Path(directory) / name

    @staticmethod
    def _metadata(record, state):
        if not isinstance(record, dict) or record.get("type") != "session_meta":
            return
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return
        source = payload.get("source")
        state.sidechain |= (
            payload.get("thread_source") not in (None, "user")
            or (isinstance(source, dict) and "subagent" in source)
        )

    def read(self) -> list[tuple[str, Optional[float]]]:
        from lmer_platform import transcripts

        prompts = []
        paths = set()
        for path in self._paths():
            paths.add(path)
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    identity = (info.st_dev, info.st_ino)
                    state = self._files.get(path)
                    if state is None or state.identity != identity or info.st_size < state.offset:
                        state = self._files[path] = _File(identity, 0, 0)
                    stream.seek(state.offset)
                    chunk = stream.read(READ_BYTES)
                    state.offset += len(chunk)
                combined = state.pending + chunk
                lines = combined.split(b"\n")
                state.pending = lines.pop()
                end = state.offset - len(combined)
                for line in lines:
                    end += len(line) + 1
                    if state.oversized:
                        state.oversized = False
                        continue
                    if len(line) > MAX_LINE_BYTES:
                        continue
                    try:
                        record = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if not isinstance(record, dict):
                        continue
                    self._metadata(record, state)
                    vocabulary = transcripts._harness_of_record(record)
                    if vocabulary == "codex" and state.sidechain:
                        continue
                    if self._native and vocabulary != self._native:
                        continue
                    if vocabulary == "lmer":
                        declared = transcripts._lmer_declared_format(record)
                        if declared is not None and declared > transcripts._LMER_FORMAT:
                            state.unsupported = True
                        if state.unsupported:
                            continue
                    if vocabulary is not None:
                        self.supported = True
                    if end <= state.baseline:
                        continue
                    text = transcripts.prompt_from_record(record)
                    if text:
                        at = record.get("at") if vocabulary == "lmer" else record.get("timestamp")
                        prompts.append((text, transcripts._timestamp_key(at)))
                if len(state.pending) > MAX_LINE_BYTES:
                    state.pending = b""
                    state.oversized = True
            except (OSError, ValueError) as exc:
                logger.debug("terminal_transcript_unreadable path=%s error=%s", path, exc)
        self._files = {path: state for path, state in self._files.items() if path in paths}
        return prompts


@dataclass
class _Candidate:
    message: comms.Message
    words: str
    at: float
    started: float
    window: int


class TerminalMirror:
    """Keep candidates locally; publish one linked record after confirmation."""

    def __init__(self, log, reader, *, window=None, enabled=True,
                 clock=time.monotonic, wall_clock=time.time):
        self.log = log
        self.reader = reader
        self.window = window_from_env() if window is None else window
        self.enabled = enabled
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._candidates: list[_Candidate] = []

    def configure(self, seconds: int) -> None:
        """Set the window for subsequent submissions; existing deadlines stand."""
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 1:
            raise ValueError("window_seconds must be a positive integer")
        with self._lock:
            self.window = seconds

    def feed(self, tracker, data: bytes, author=None) -> None:
        if not self.enabled:
            return
        with self._lock:
            for line in tracker.feed(data):
                held = self.reader.supported
                try:
                    message, _ = self.log.append(
                        kind=comms.KIND_MESSAGE, text=line,
                        source=comms.Source(comms.CHANNEL_TERMINAL, author=author),
                        meta={"fidelity": "reconstructed"}, local_only=held,
                    )
                except comms.CommsError as exc:
                    logger.warning("terminal_mirror_line_rejected error=%s", exc)
                    continue
                if held:
                    self._candidates.append(_Candidate(
                        message, " ".join(line.split()),
                        math.floor(self._wall_clock() * 1000) / 1000,
                        self._clock(), self.window,
                    ))

    def run(self, stop: threading.Event) -> None:
        """Poll independently of the ask-directory watcher and PTY forwarding."""
        while not stop.wait(POLL_SECONDS):
            try:
                self.poll()
            except comms.CommsError as exc:
                logger.warning("terminal_confirmation_failed error=%s", exc)

    def poll(self) -> None:
        if not self.enabled:
            return
        with self._poll_lock:
            self._poll()

    def _poll(self) -> None:
        # Read outside the input lock: a transcript cannot stall PTY forwarding.
        prompts = self.reader.read()
        with self._lock:
            now = self._clock()
            for text, at in prompts:
                words = " ".join(text.split())
                for candidate in self._candidates:
                    elapsed = now - candidate.started if at is None else at - candidate.at
                    if candidate.words != words or not 0 <= elapsed < candidate.window:
                        continue
                    self.log.append(
                        kind=comms.KIND_MESSAGE, text=candidate.message.text,
                        source=candidate.message.source,
                        in_reply_to=candidate.message.id,
                        meta={"fidelity": "confirmed"},
                    )
                    self._candidates.remove(candidate)
                    break
            self._candidates = [c for c in self._candidates if now - c.started < c.window]
