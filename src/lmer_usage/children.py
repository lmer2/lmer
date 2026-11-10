"""Fan-out children's usage (#647 Q6).

``spawn-harness`` children are counted in their parent's record, as ``agent``
rows carrying ``child_session``. Each child owns one directory,
``<session_dir>/.lmer-children/<child-session>/``, under its harness's
transcript directory (:data:`CHILDREN_DIRNAME`). The directory holds a manifest
(:data:`MANIFEST_NAME`) and, for a harness that keeps its transcript there, the
transcript itself; readers skip it by name (:func:`is_child_path`).

claude and pi children are counted from their transcripts (claude's is named by
:func:`transcript_id` and moved in by :func:`move_transcript`), codex children
from their JSON output (:class:`CodexStream`), and any other harness is
``not_collected`` (:func:`unsupported_reason`). Per harness, and why:
docs/USAGE-TRACKING.md, "Fan-out children".

Neutral: imports nothing from ``lmer_platform``, ``work_repo`` or ``lmer_cli``.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterable, Mapping, Optional

from . import buckets, codex, record
from .claude import _read_small_json
from .summarise import summarise

MANIFEST_VERSION = 1
#: The reserved subtree of a harness's transcript dir that holds its children.
CHILDREN_DIRNAME = ".lmer-children"
MANIFEST_NAME = "child.json"
#: Below ``Path.home()``, for a harness that declares no transcript dir.
FALLBACK_ROOT = (".lmer-usage", "children")
_MAX_ROWS = 100
_TEXT_FIELDS = ("harness", "model", "agent", "child_session", "reason")
_MODEL_EVENTS = ("turn_context", "session_configured")
_CHILD_ID = re.compile(r"^[0-9a-f]{32}$")

#: Harnesses whose JSON output this module can turn back into text exactly.
SUPPORTED = ("codex",)


class Extraction:
    """One child's output: the text the caller sees and the child's rows.

    ``text`` is ``None`` when the output did not parse: the caller then passes
    the child's raw stdout through unchanged.
    """

    __slots__ = ("text", "rows")

    def __init__(self, text: Optional[bytes], rows: list):
        self.text = text
        self.rows = rows


def supports(harness: str) -> bool:
    return harness in SUPPORTED


def unsupported_reason(harness: str) -> str:
    return f"usage capture unsupported for {harness}"


def not_collected_row(*, harness: str, model: Optional[str], agent: str,
                      child_session: str, reason: str) -> dict:
    return {"harness": harness, "model": model, "agent": agent, "child_session": child_session,
            "status": "not_collected", "reason": reason[:record._TEXT_LIMIT], **buckets.zero()}


def _codex_usage(event: dict) -> Optional[tuple]:
    """The cumulative usage tuple an event carries, in ``codex._FIELDS`` order."""
    if event.get("type") == "turn.completed":
        usage = event.get("usage")
        if not isinstance(usage, dict):
            return None
        values = tuple(buckets.count(usage.get(name)) for name in codex._FIELDS)
        return None if any(v is None for v in values) else values
    return codex._snapshot(event)


def _codex_model(event: dict) -> Optional[str]:
    payload = event.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    if event.get("type") not in _MODEL_EVENTS and payload.get("type") not in _MODEL_EVENTS:
        return None
    for source in (payload, event):
        model = source.get("model")
        if isinstance(model, str) and model:
            return model
    return None


def _codex_text(last_message: Optional[bytes]) -> bytes:
    """codex text mode on a non-TTY stdout prints ``println!("{}", message)``,
    and nothing when there is no final message; ``--output-last-message``
    writes the message verbatim, or empty content when there is none."""
    return last_message + b"\n" if last_message else b""


#: Raw stdout kept for the passthrough fallback; JSON events are parsed as they
#: arrive, so only output that is not JSON events needs the raw bytes.
RAW_LIMIT = 4 * 1024 * 1024


def _json_or_none(raw: bytes):
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        return None


class CodexStream:
    """``codex exec --json`` stdout, parsed line by line as it arrives.

    JSON mode reports failures only as ``error`` / ``turn.failed`` events on
    stdout, where text mode prints ``ERROR: <message>`` on stderr. Each one is
    handed to ``on_error`` as that line, so the caller's stderr and its failure
    footer still name the cause. Only the last cumulative usage snapshot, the
    model and the last ``agent_message`` are kept, plus raw bytes up to
    :data:`RAW_LIMIT` for output that turns out not to be JSON events.

    A last line with no newline is a write the child was killed in the middle
    of (codex ends every event with one). Once earlier lines were events, such
    a fragment that does not parse is dropped, so a timed-out child keeps the
    answer and usage it had already reported.

    The pump thread feeds while an interrupted caller may already finish, so
    both take one lock: finishing reads a consistent prefix.
    """

    def __init__(self, on_error=None):
        self._lock = threading.Lock()
        self._on_error = on_error
        self._snapshot = None
        self._seen = 0
        self._named = None
        self._message: Optional[str] = None
        self._not_json = False
        self._events = 0
        self._raw: Optional[bytearray] = bytearray()
        self._partial = b""

    @property
    def raw(self) -> Optional[bytes]:
        """The raw stdout, or ``None`` once it outgrew :data:`RAW_LIMIT`."""
        with self._lock:
            return None if self._raw is None else bytes(self._raw)

    def feed(self, chunk: bytes) -> None:
        with self._lock:
            if self._raw is not None:
                if len(self._raw) + len(chunk) > RAW_LIMIT:
                    self._raw = None
                else:
                    self._raw.extend(chunk)
            if self._not_json:
                return  # nothing more is parsed; keep no partial line either
            lines = (self._partial + chunk).split(b"\n")
            self._partial = lines.pop()
            for line in lines:
                self._line(line)

    def close(self) -> None:
        with self._lock:
            tail, self._partial = self._partial, b""
            if not tail or self._not_json:
                return
            if self._events and not isinstance(_json_or_none(tail), dict):
                return  # cut off mid-write: see the class docstring
            self._line(tail)

    def _error(self, message) -> None:
        if self._on_error is not None and isinstance(message, str) and message:
            self._on_error(f"ERROR: {message}")

    def _line(self, raw: bytes) -> None:
        if not raw.strip() or self._not_json:
            return
        event = _json_or_none(raw)
        if not isinstance(event, dict):
            self._not_json = True
            self._partial = b""
            return
        self._events += 1
        kind = event.get("type")
        if kind == "error":
            self._error(event.get("message"))
        elif kind == "turn.failed":
            self._error((event.get("error") or {}).get("message") if isinstance(event.get("error"), dict) else None)
        elif kind == "item.completed" and isinstance(event.get("item"), dict):
            item = event["item"]
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                self._message = item["text"]
        self._named = _codex_model(event) or self._named
        current = _codex_usage(event)
        if current is not None and current != self._snapshot:
            self._snapshot = current
            self._seen += 1

    def finish(self, last_message: Optional[bytes], *, model: Optional[str], agent: str,
               child_session: str) -> Extraction:
        """The text and rows. The text is the last-message file's whenever codex
        wrote one; without it, the last ``agent_message`` event's; and for output
        that is not JSON events, ``None`` (pass the raw bytes through)."""
        self.close()
        with self._lock:
            return self._finish(last_message, model=model, agent=agent, child_session=child_session)

    def _finish(self, last_message, *, model, agent, child_session) -> Extraction:
        if last_message is not None:
            text = _codex_text(last_message)
        elif self._not_json:
            text = None
        else:
            text = _codex_text(self._message.encode() if self._message else None)
        if self._not_json:
            return Extraction(text, [not_collected_row(
                harness=codex.HARNESS, model=model, agent=agent, child_session=child_session,
                reason="child output was not codex JSON events")])
        counters = codex._counters(self._snapshot) if self._snapshot is not None else None
        if counters is None or buckets.is_zero(counters):
            return Extraction(text, [not_collected_row(
                harness=codex.HARNESS, model=model, agent=agent, child_session=child_session,
                reason="no usage in child output")])
        counters["requests"] = self._seen
        return Extraction(text, [{
            "harness": codex.HARNESS, "model": self._named or model or "unknown", "agent": agent,
            "child_session": child_session, "status": "collected", **counters,
        }])


def extract_codex(lines: Iterable[bytes], last_message: Optional[bytes], *,
                  model: Optional[str], agent: str, child_session: str, on_error=None) -> Extraction:
    """:class:`CodexStream` over ``lines`` at once (a whole captured stdout)."""
    stream = CodexStream(on_error=on_error)
    for line in lines:
        stream.feed(line)
    return stream.finish(last_message, model=model, agent=agent, child_session=child_session)


class _UnsupportedStream:
    """A capture for a harness this module cannot read: raw bytes only."""

    def __init__(self, harness: str):
        self._harness = harness
        self._raw: Optional[bytearray] = bytearray()

    @property
    def raw(self) -> Optional[bytes]:
        return None if self._raw is None else bytes(self._raw)

    def feed(self, chunk: bytes) -> None:
        if self._raw is not None:
            self._raw = None if len(self._raw) + len(chunk) > RAW_LIMIT else self._raw + chunk

    def close(self) -> None:
        pass

    def finish(self, last_message, *, model, agent, child_session) -> Extraction:
        return Extraction(None, [not_collected_row(
            harness=self._harness, model=model, agent=agent, child_session=child_session,
            reason=unsupported_reason(self._harness))])


def stream(harness: str, on_error=None):
    """The incremental capture for ``harness``'s JSON stdout."""
    return CodexStream(on_error=on_error) if harness == codex.HARNESS else _UnsupportedStream(harness)


def extract(harness: str, stdout: BinaryIO, last_message: Optional[bytes], *,
            model: Optional[str], agent: str, child_session: str) -> Extraction:
    capture = stream(harness)
    for chunk in stdout:
        capture.feed(chunk)
    return capture.finish(last_message, model=model, agent=agent, child_session=child_session)


def valid_row(row: object) -> Optional[dict]:
    """``row`` normalised to a child record row, or ``None`` when invalid.

    The checks are :func:`lmer_usage.record.validate`'s row checks, so a row
    that passes here never invalidates the parent's record.
    """
    if not isinstance(row, Mapping) or not buckets.valid(row):
        return None
    status = row.get("status", "collected")
    if status not in record.ROW_STATUSES:
        return None
    if not all(record._text(row.get(name)) for name in _TEXT_FIELDS):
        return None
    if not isinstance(row.get("child_session"), str) or not row.get("child_session"):
        return None
    if status == "not_collected" and not buckets.is_zero(row):
        return None
    clean = {name: row.get(name) for name in ("harness", "model", "agent", "child_session")}
    clean["status"] = status
    if row.get("reason") is not None:
        clean["reason"] = row["reason"]
    clean.update({name: int(row.get(name, 0)) for name in buckets.COUNTERS})
    return clean


def valid_child_session(value: object) -> bool:
    return isinstance(value, str) and bool(_CHILD_ID.match(value))


def transcript_id(child_session: str) -> str:
    """The UUID a claude child is started with (``--session-id``)."""
    return str(uuid.UUID(hex=child_session))


def children_root(session_dir: Optional[Path]) -> Path:
    """Where a harness's children live: its transcript dir's reserved subtree,
    or a container-local fallback for a harness that declares none."""
    if session_dir is not None:
        return Path(session_dir) / CHILDREN_DIRNAME
    return Path.home().joinpath(*FALLBACK_ROOT)


def _as_child_id(name: str) -> Optional[str]:
    stem = name[:-len(".jsonl")] if name.endswith(".jsonl") else name
    flat = stem.replace("-", "").lower()
    return flat if _CHILD_ID.match(flat) else None


def is_child_path(path: Path, base: Path) -> bool:
    """Whether *path*, found under *base*, belongs to a fan-out child.

    True inside a :data:`CHILDREN_DIRNAME` subtree, and for a claude child's
    transcript still at its original place: a file or directory named by an id
    whose child directory exists beside it. Only names under *base* are
    compared, so a session can only ever exclude its own files.
    """
    try:
        parts = Path(path).relative_to(base).parts
    except ValueError:
        return False
    if CHILDREN_DIRNAME in parts[:-1]:
        return True
    ids = {found for found in map(_as_child_id, parts) if found}
    if not ids:
        return False
    directory = Path(base)
    for part in (None, *parts[:-1]):
        if part is not None:
            directory = directory / part
        if any((directory / CHILDREN_DIRNAME / found).is_dir() for found in ids):
            return True
    return False


def write_manifest(directory: Path, data: dict) -> Path:
    """Write a child's manifest atomically: directories 0700, file 0600."""
    if not valid_child_session(Path(directory).name):
        raise ValueError(f"unsafe child directory: {directory}")
    for path in (Path(directory).parent, Path(directory)):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink():
            raise OSError(f"refusing a symlinked child directory: {path}")
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".child.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"version": MANIFEST_VERSION, **data}, handle)
        target = Path(directory) / MANIFEST_NAME
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def move_transcript(session_dir: Path, child_session: str, directory: Path) -> int:
    """Move a claude child's files from ``<session_dir>/<project>/`` into its
    child directory, keeping the project level; returns how many moved."""
    wanted = transcript_id(child_session)
    moved = 0
    for project in sorted(Path(session_dir).iterdir()):
        if project.name == CHILDREN_DIRNAME or project.is_symlink() or not project.is_dir():
            continue
        for name in (f"{wanted}.jsonl", wanted):
            source = project / name
            if source.is_symlink() or not source.exists():
                continue
            target = Path(directory) / project.name / name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.replace(source, target)
            moved += 1
    return moved


def _jsonl_in(directory: Path) -> list:
    """Regular ``.jsonl`` files resolving inside *directory*."""
    try:
        base = Path(directory).resolve()
        found = sorted(p for p in Path(directory).rglob("*.jsonl") if not p.is_symlink())
    except (OSError, RuntimeError):
        return []
    kept = []
    for path in found:
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            continue
        if str(resolved).startswith(str(base) + os.sep) and path.is_file():
            kept.append(path)
    return kept


def _transcript_rows(directory: Path, data: Mapping, days: Optional[dict] = None) -> list:
    """The child's rows from its own transcript, attributed to the child; its
    per-UTC-day counts are added to ``days`` when given.

    A claude sub-agent inside the child is ``<agent>/<sub-agent>``. Files not
    yet moved in (the child still running) are found by the child's id.
    """
    child_session, agent = data["child_session"], data.get("agent")
    files = _jsonl_in(directory)
    session_dir = Path(directory).parent.parent
    for path in _jsonl_in(session_dir):
        parts = path.relative_to(session_dir).parts
        if CHILDREN_DIRNAME not in parts and child_session in map(_as_child_id, parts):
            files.append(path)
    summary = summarise(files)
    if days is not None:
        for day, counters in summary.days.items():
            buckets.add(days.setdefault(day, buckets.zero()), counters)
    rows = []
    for row in summary.row_list():
        sub = row["agent"]
        row["agent"] = (agent if sub is None else f"{agent}/{sub}")[:record._TEXT_LIMIT]
        row["child_session"] = child_session
        clean = valid_row(row)
        if clean is not None:
            rows.append(clean)
    if rows:
        return rows
    reason = "no usage in child transcript" if files else "no child transcript"
    return [not_collected_row(harness=data.get("harness") or "unknown", model=data.get("model"),
                              agent=agent, child_session=child_session, reason=reason)]


def _manifest_day(path: Path) -> Optional[str]:
    """The UTC day a manifest was last written: when its child finished."""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).date().isoformat()
    except (OSError, ValueError, OverflowError):
        return None


def read_rows(roots: Iterable[Path], *, parent_session: str) -> list:
    """Every valid row of every child spawned by ``parent_session``."""
    return read(roots, parent_session=parent_session)[0]


def read(roots: Iterable[Path], *, parent_session: str) -> tuple:
    """``(rows, days)`` for every child spawned by ``parent_session``.

    ``days`` splits the collected rows by UTC day, as a record's ``days`` does:
    a transcript by its own timestamps, and manifest rows (codex), which carry
    none, on the day the manifest was written. A symlinked root or child
    directory, an unreadable or foreign manifest and a row that fails
    :func:`valid_row` are skipped, never raised.
    """
    rows: list = []
    days: dict = {}
    for root in roots:
        root = Path(root)
        if root.is_symlink() or not root.is_dir():
            continue
        for directory in sorted(root.iterdir()):
            if directory.is_symlink() or not directory.is_dir() \
                    or not valid_child_session(directory.name):
                continue
            data = _read_small_json(directory / MANIFEST_NAME)
            if not data or data.get("version") != MANIFEST_VERSION \
                    or data.get("parent_session") != parent_session \
                    or data.get("child_session") != directory.name \
                    or not record._text(data.get("agent"), nullable=False):
                continue
            found = data.get("rows")
            if isinstance(found, list) and len(found) <= _MAX_ROWS:
                kept = [clean for clean in map(valid_row, found)
                        if clean is not None and clean["child_session"] == directory.name]
                rows.extend(kept)
                day = _manifest_day(directory / MANIFEST_NAME)
                for row in kept:
                    if day is not None and row["status"] == "collected":
                        buckets.add(days.setdefault(day, buckets.zero()), row)
            if data.get("transcript") is True:
                rows.extend(_transcript_rows(directory, data, days))
    return rows, days
