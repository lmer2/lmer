"""The human-legible project index: a generated Markdown block per project README.

A work repo's run records are legible to tooling and opaque to a person
browsing the repository: an opaque uid names a directory, the name and the
goal sit inside `state.yaml`, the sign-out inside `events.jsonl`. This module
turns a project's records into one Markdown table — newest first, one row per
execution record under `_runs/` — and splices it between
:data:`START_MARKER` and :data:`END_MARKER` in `<project>/README.md`, leaving
every byte outside the markers alone (trailing newlines excepted on the first
append). The root README gets the same treatment from the plexus manifest:
the project list plus a constant legend.

Three layers, kept apart on purpose:

- **Pure core** — :class:`RecordSummary` in, text out. No filesystem, no
  environment, no clock, no counts: a row is a function of its own record,
  so two regenerations of one project differ only where records differ,
  git's three-way merge of them is the fresh render whenever it is clean,
  and the validator can compare an existing block with a fresh render
  byte for byte. The style of ``plan_index.py`` and ``goals.py``.
- **Readers** — lock-free, never raising past their wrapper, never renaming
  a corrupt file: :func:`read_state_fast` cuts the nested top-level
  ``context:`` block out of a continuation record textually (a chain's
  records nest their whole ancestry, so the deepest is hundreds of
  kilobytes while the kernel needs a kilobyte of it) and falls back to a
  full parse whenever the cut is not clean; events go through
  ``run_state.read_events``; `log.yaml` is read only when a record has
  neither name nor goal. :func:`tracked_record_filter` is the one git call
  (``git ls-files``): a row is rendered only for a record whose state file
  the commit will contain, so no link can point at a directory origin will
  not have.
- **Writers** — :func:`write_project_readme` writes only when the bytes
  differ (tmp + ``os.replace``) and refuses a README it must not touch: a
  symlink, a directory, malformed markers, a block of a newer format, or
  hand text holding a line git would read as a conflict marker (the staged
  conflict-marker gate would otherwise refuse every push of the project).
  :func:`project_dir_refusal` is the write boundary its callers check first:
  a project directory that is a symlink, resolves outside the checkout or
  lies inside a run record is never written into.

Import direction: this module imports nothing from ``lmer_schema``;
``lmer_schema.checks`` and ``lmer_schema.cli`` import it lazily inside
functions. Nothing here writes under `_runs/**` and nothing takes a lock
(callers do). The state vocabulary (:func:`display_state`,
:data:`STALE_AFTER_SECONDS`) lives in ``session_record`` beside
``signed_out`` and is re-exported here.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence
from urllib.parse import quote

import yaml

from . import run_state
from .session_record import (
    STALE_AFTER_SECONDS,
    UID_RE,
    display_state,
    is_seeded,
    signed_out,
)
from .utils import MANIFEST_NAME, redact_secrets

__all__ = [
    "START_MARKER", "END_MARKER", "README_NAME", "RUNS_DIR", "FORMAT", "LABEL_MAX",
    "LABEL_CUT_FLOOR", "LOG_READ_CAP", "MARKER_LINE_RE", "PROJECT_LEGEND", "ROOT_LEGEND",
    "EMPTY_BODY", "STALE_AFTER_SECONDS", "display_state", "IndexSkipped", "MalformedMarkers",
    "RecordSummary", "sort_key", "label", "flatten", "fmt_stamp", "render_row",
    "render_project_block", "render_root_block", "extract_block", "strip_block",
    "block_format", "hand_text_has_marker", "splice_block", "project_rels_for_paths",
    "build_summary", "read_state_fast", "summarize", "tracked_record_filter", "scan_project",
    "project_dir_refusal", "render_project_readme", "write_project_readme", "render_root_readme",
]

START_MARKER = "<!-- lmer:index:start -->"
END_MARKER = "<!-- lmer:index:end -->"
README_NAME = "README.md"
RUNS_DIR = "_runs"
#: Rendered into the legend as "lmer index format 1". A writer never rewrites,
#: and the validator never flags, a block whose token is newer than this.
FORMAT = 1
LABEL_MAX = 80
#: A long label is cut at its last space at or after this many characters.
LABEL_CUT_FLOOR = 40
#: `log.yaml` is consulted for a label only up to this size.
LOG_READ_CAP = 64 * 1024
#: One `git ls-files` per project; a timeout is a git error, which is permissive.
GIT_TIMEOUT_SECONDS = 60.0
#: git's ``is_conflict_marker``: seven identical ``<``, ``=``, ``>`` or ``|``
#: at column 0 followed by whitespace or the end of the line.
MARKER_LINE_RE = re.compile(r"^([<=>|])\1{6}(?:[ \t\r]|$)", re.M)
#: The table body when a project holds no record.
EMPTY_BODY = "_No run records yet._"
MISSING = "—"
#: Characters left unquoted in a link target (RFC 3986 unreserved plus ``/``).
LINK_SAFE = "/-._~"

PROJECT_LEGEND = (
    "One row per execution record under `_runs/`, newest first — rendered by lmer from each "
    "record's own `state.yaml`, `events.jsonl` and `log.yaml` on every work-repo push and by "
    f"`lmer-schema index --write` (lmer index format {FORMAT}). States: **closed** = status "
    "complete or archived · **waiting on question** = stopped on a recorded question · "
    "**seeded** = a `work seed` record nobody has run in yet · **ended, not closed** = in "
    "progress with a recorded session end · **open** = in progress with no sign-out recorded; "
    "the last-activity column is the date the record last changed. "
    "Nothing here ever ends a run. Edit this file outside the markers only."
)
TABLE_HEADER = "| started (UTC) | run | state | last activity | record |\n|---|---|---|---|---|"
ROOT_INTRO = (
    f"Generated from `{MANIFEST_NAME}` by `lmer-schema index --write` (lmer index format "
    f"{FORMAT}). Each project directory shows its own run index below its file list. Edit "
    "this file outside the markers only."
)
#: Constant: a fixed name list with one pointer, never a description of what is on disk.
#: The manifest's name comes from ``utils``, the one module that spells it (issue #353 D20).
ROOT_LEGEND = (
    f"Other names at this level: `{MANIFEST_NAME}`, `_agents/`, `_knowledge/`, `_napkin/`, "
    "`_taskdef/`, `_archive/`, `_memory_retirements.json`, `napkin/`, `taskdef/`, "
    "`agent-files/`, `mirrors/`, `knowledge/` — see lmer `docs/SCHEMA.md`; everything else "
    "is human content."
)
ROOT_TABLE_HEADER = "| project | repository | description |\n|---|---|---|"
ROOT_EMPTY_BODY = "_No projects registered yet._"

#: A marker is a whole line — column 0, nothing after it but trailing whitespace —
#: as the block grammar spells it. The marker text anywhere else (inside a row,
#: a heading, a human's sentence) is prose and never counts.
_START_LINE_RE = re.compile(rf"^{re.escape(START_MARKER)}[ \t]*\r?$", re.M)
_END_LINE_RE = re.compile(rf"^{re.escape(END_MARKER)}[ \t]*\r?$", re.M)
_FORMAT_RE = re.compile(r"\(lmer index format (\d+)\)")
_STOP_REASON_SUFFIXES = frozenset({"yield", "critical_error", "aborted"})
_ESCAPES = {"\\": "\\\\", "|": "\\|", "[": "\\[", "]": "\\]", "`": "\\`", "<": "\\<", "&": "\\&"}
_ESCAPE_RE = re.compile("[" + re.escape("".join(_ESCAPES)) + "]")

#: libyaml when the wheel bundles it (the container image); pure Python otherwise.
_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
#: A column-0 ``context:`` line opening a block (a flow value on the line is not cut).
_CONTEXT_LINE_RE = re.compile(r"^context:[ \t]*(?:#.*)?$", re.M)
#: The first line after the block: column 0, not blank, not a comment.
_BLOCK_END_RE = re.compile(r"^[^\s#]", re.M)
#: The record's own ``source_path`` inside the cut block (deeper nestings are indented more).
_SOURCE_PATH_RE = re.compile(r"^  source_path: (.+)$", re.M)
#: A value that goes on at a deeper indent on the next line (after any blank lines):
#: the emitter folds a plain scalar past column 80 at a space, so the anchored
#: ``source_path`` expression saw only the value's first line.
_CONTINUED_RE = re.compile(r"\n(?:[ \t]*\n)*[ \t]{3,}\S")
#: What a one-line scalar may load as; a collection means the text was not a scalar.
_SCALAR_TYPES = (str, int, float, bool, type(None), datetime, date)
#: The characters that can open or close something spanning lines in a YAML block.
_TOKEN_RE = re.compile(r"[\"'\[\]{}#]")
#: A top-level sequence item in `log.yaml`; the text before the second one holds the first entry.
_LOG_ITEM_RE = re.compile(r"^-(?:[ \t]|$)", re.M)
_INVALID = object()


class MalformedMarkers(ValueError):
    """Index markers that cannot delimit one block: a start without an end,
    an end without a start, markers out of order, or more than one pair."""


class IndexSkipped(Exception):
    """A README the generator refuses to touch; ``str(exc)`` is the reason."""


@dataclass(frozen=True)
class RecordSummary:
    """What one row needs from one record — every field a function of that record."""

    dir_rel: str            # project-relative POSIX dir: "_runs/<slug>/<dir>" ("_runs/<slug>" when flat)
    dir_name: str
    uid: str | None         # run_state.session_id_of(dir_name); None for a flat record
    readable: bool          # state.yaml parsed to a mapping
    slug: str | None        # state.slug, else the group directory name
    name: str | None
    goal_line: str | None   # first non-empty line of state.goal
    first_log: str | None   # first log.yaml entry's message; read only when name and goal are both null
    status: str | None
    stop_reason: str | None
    phase: str | None
    started_at: str | None  # session.started_at, else created — always a str (see _stamp)
    updated: str | None     # always a str
    signed_out: bool | None  # session_record.signed_out(events); None when no events could be read
    seeded: bool            # session_record.is_seeded(state, events); D13
    source_uid: str | None  # session.source_session_uid
    source_rel: str | None  # the source record's project-relative dir, resolved by uid


# ---------------------------------------------------------------------------
# Pure core
# ---------------------------------------------------------------------------


def _stamp(value) -> str | None:
    """Coerce a stamp read from YAML to a string before anything touches it.

    A hand-edited record whose ``created``/``updated``/``session.started_at``
    is unquoted resolves to a ``datetime`` under YAML's timestamp rule; it
    renders and sorts exactly like a quoted one. A naive datetime is read as
    UTC (never as local time, which would differ between hosts). A datetime
    whose UTC instant is outside the representable range (year 1 with a
    positive offset, 9999 with a negative one) keeps its own spelling, which
    is what its quoted twin would have read as — unparseable, never an error.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        try:
            return value.astimezone(timezone.utc).isoformat()
        except OverflowError:
            return value.isoformat()
    if isinstance(value, str):
        return value or None
    return str(value)


def _parse_stamp(stamp: str) -> datetime | None:
    """The stamp as an aware UTC datetime, ``None`` when it does not parse —
    including a stamp that parses but whose UTC instant overflows the
    datetime range (year 1 with a positive offset, 9999 with a negative one)."""
    try:
        parsed = datetime.fromisoformat(stamp.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.astimezone(timezone.utc)
    except OverflowError:
        return None


def _utc_key(stamp: str | None) -> tuple[int, str]:
    """A total order on stamps: parsed instants first (by UTC instant, whatever
    the offset or spelling), then unparseable ones by their text, then missing."""
    if stamp is None:
        return (-1, "")
    parsed = _parse_stamp(stamp)
    if parsed is None:
        return (0, stamp)
    return (1, parsed.isoformat(timespec="microseconds"))


def sort_key(summary: RecordSummary) -> tuple[int, str, str]:
    """Rendered in reverse: newest start first, dir name descending as tiebreak."""
    return (*_utc_key(summary.started_at), summary.dir_name)


def label(summary: RecordSummary) -> tuple[str, bool]:
    """``(text, derived)``: the name, else the goal's first line, else the
    first log message, else ``(unnamed)``; derived labels are marked in the row."""
    if summary.name:
        return summary.name, False
    if summary.goal_line:
        return summary.goal_line, True
    if summary.first_log:
        return summary.first_log, True
    return "(unnamed)", True


def _escape(text: str) -> str:
    return _ESCAPE_RE.sub(lambda match: _ESCAPES[match.group(0)], text)


def _fold(char: str) -> str:
    """One character of generated text: whitespace kept for the collapse, a
    control character dropped, a lone surrogate replaced — a directory name
    that is not valid UTF-8 is listed with ``surrogateescape``, and a
    hand-edited ``"\\uD800"`` can put any surrogate in a record — so the
    text always encodes as UTF-8."""
    if char.isspace():
        return char
    category = unicodedata.category(char)
    if category == "Cs":
        return "\ufffd"
    return "" if category == "Cc" else char


def _one_line(text) -> str:
    """One line from any text: control characters dropped, lone surrogates
    replaced, whitespace collapsed.

    Every free-text fragment of a generated line passes through here, so no
    value read from a record — a hand-edited stamp or slug, a directory
    name — can start a new line inside the block, let alone one that git
    would read as a conflict marker, and none can stop the README encoding.
    """
    return " ".join("".join(_fold(char) for char in str(text)).split())


def _code(text) -> str:
    """A code span safe inside a table cell: one line, no backticks, pipes escaped."""
    return "`" + _one_line(text).replace("`", "").replace("|", "\\|") + "`"


def flatten(text, limit: int = LABEL_MAX) -> str:
    """One table-safe line from free text.

    Redaction is environment independent — ``redact_secrets`` with no secret
    values, so token shapes and URL userinfo are caught and the renderer's
    own environment is never consulted (two hosts must render one record
    identically). Control characters are dropped, whitespace collapsed, the
    table and markup characters escaped, and a long label cut at its last
    space at or after :data:`LABEL_CUT_FLOOR` characters (else hard) with an
    ellipsis appended. The cut comes before the escaping (the design spec
    lists the two steps the other way round), so a cut can never split an
    escape sequence; the bytes differ only for a label over the limit that
    holds an escapable character.
    """
    text = _one_line(redact_secrets(str(text), secret_values=[]))
    if len(text) <= limit:
        return _escape(text)
    space = text.rfind(" ", 0, limit + 1)
    head = text[:space] if space >= LABEL_CUT_FLOOR else text[:limit]
    return _escape(head.rstrip()) + "…"


def fmt_stamp(stamp: str | None) -> str:
    """UTC ``YYYY-MM-DD HH:MM`` for a parseable stamp (an offset is converted),
    the raw text cut to 16 characters otherwise, ``—`` when missing."""
    if stamp is None:
        return MISSING
    parsed = _parse_stamp(str(stamp))
    if parsed is None:
        return _escape(_one_line(stamp)[:16])
    return parsed.strftime("%Y-%m-%d %H:%M")


def _link(rel: str) -> str:
    """A link target relative to the README: the path's bytes URL-quoted, so a
    directory name that is not valid UTF-8 (listed with ``surrogateescape``)
    links to its real byte (``%FF``), which the server resolves; any other
    lone surrogate names no file and quotes like a ``?``."""
    try:
        raw = rel.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        raw = rel.encode("utf-8", "replace")
    return quote(raw, safe=LINK_SAFE) + "/"


def _short_id(session_id: str) -> str:
    return session_id[:8] if UID_RE.fullmatch(session_id) else session_id


def render_row(summary: RecordSummary) -> str:
    """One table row; see the Index format section of the design spec."""
    link = _link(summary.dir_rel)
    slug_cell = _code(summary.slug if summary.slug is not None else MISSING)
    id_cell = MISSING if summary.uid is None else _code(_short_id(summary.uid))
    if not summary.readable:
        return (f"| {MISSING} | [{_escape(_one_line(summary.dir_name))}]({link}) | unreadable "
                f"| {MISSING} | {slug_cell} · {id_cell} |")
    text, derived = label(summary)
    run = f"[{flatten(text)}]({link})"
    if derived:
        run = f"_(unnamed)_ {run}"
    if summary.source_uid:
        short = _short_id(summary.source_uid)
        if summary.source_rel:
            run += f" · continues [{_escape(_one_line(short))}]({_link(summary.source_rel)})"
        else:
            run += f" · continues {_code(short)}"
    state = display_state(
        readable=True, status=summary.status, stop_reason=summary.stop_reason,
        signed_out=summary.signed_out, updated=summary.updated,
        seeded=summary.seeded,
    )
    if summary.stop_reason in _STOP_REASON_SUFFIXES:
        state += f" ({summary.stop_reason})"
    if summary.phase:
        state += f" · phase {flatten(summary.phase)}"
    return (f"| {fmt_stamp(summary.started_at)} | {run} | {state} | {fmt_stamp(summary.updated)} "
            f"| {slug_cell} · {id_cell} |")


def _block(title: str, intro: str, body: str, legend: str | None = None) -> str:
    tail = f"\n\n{legend}" if legend else ""
    return f"{START_MARKER}\n\n## {title}\n\n{intro}\n\n{body}{tail}\n\n{END_MARKER}"


def render_project_block(summaries: Sequence[RecordSummary]) -> str:
    """The project block: legend, then the table newest first (or the empty body)."""
    rows = [render_row(summary) for summary in sorted(summaries, key=sort_key, reverse=True)]
    body = "\n".join([TABLE_HEADER, *rows]) if rows else EMPTY_BODY
    return _block("Runs", PROJECT_LEGEND, body)


def render_root_block(manifest_document: Mapping) -> str:
    """The root block: the manifest's projects in manifest order plus the constant legend."""
    projects = manifest_document.get("projects") if isinstance(manifest_document, Mapping) else None
    rows = []
    for name, entry in (projects.items() if isinstance(projects, Mapping) else ()):
        entry = entry if isinstance(entry, Mapping) else {}
        repo = entry.get("repo")
        description = entry.get("description")
        rows.append(
            f"| [{_escape(_one_line(name))}]({_link(str(name))}) "
            f"| {_code(repo) if repo else MISSING} "
            f"| {flatten(description) if description else MISSING} |"
        )
    body = "\n".join([ROOT_TABLE_HEADER, *rows]) if rows else ROOT_EMPTY_BODY
    return _block("Projects", ROOT_INTRO, body, ROOT_LEGEND)


def _span(text: str) -> tuple[int, int] | None:
    """``(start, end)`` of the marked block, inclusive of both markers, or ``None``.

    A marker is a whole line (column 0, trailing whitespace allowed). The
    marker text inside a row, a heading or a sentence is prose and never
    counts, so a goal, a log message or a slug that spells one cannot make
    the README it lands in malformed. The span ends right after the end
    marker's text: whatever follows it on its line (a CR, trailing spaces)
    stays with the hand text.
    """
    starts = [match.start() for match in _START_LINE_RE.finditer(text)]
    ends = [match.start() for match in _END_LINE_RE.finditer(text)]
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1:
        raise MalformedMarkers(
            f"expected one start and one end marker, found {len(starts)} and {len(ends)}")
    if ends[0] < starts[0]:
        raise MalformedMarkers("end marker precedes start marker")
    return starts[0], ends[0] + len(END_MARKER)


def extract_block(text: str) -> str | None:
    """The marked block (markers included), ``None`` without markers."""
    span = _span(text)
    return None if span is None else text[span[0]:span[1]]


def strip_block(text: str) -> str:
    """The hand text with the block removed and trailing newlines dropped — the
    conflict handler's "same prose on both sides" comparison. Raises
    :class:`MalformedMarkers` when the markers cannot delimit one block."""
    span = _span(text)
    if span is not None:
        text = text[:span[0]] + text[span[1]:]
    return text.rstrip("\n")


def block_format(block: str | None) -> int | None:
    """The legend's format token, ``None`` when the block carries none."""
    if not block:
        return None
    match = _FORMAT_RE.search(block)
    return int(match.group(1)) if match else None


def hand_text_has_marker(text: str) -> bool:
    """Whether the text outside the markers (all of it when there are none, or
    when they are malformed) holds a line git reads as a conflict marker."""
    try:
        hand = strip_block(text)
    except MalformedMarkers:
        hand = text
    return MARKER_LINE_RE.search(hand) is not None


def splice_block(existing: str | None, block: str, *, heading: str) -> str:
    """The README text with *block* in place.

    No file (or an empty one): a heading and the block. Markers present: the
    marked span is replaced inclusively and every other byte kept. No
    markers: the block is appended after one blank line, normalising the
    hand text's trailing newlines once. The heading is flattened to one line
    like every other generated fragment, so no name can plant a line git
    would read as a conflict marker in the hand text.
    """
    heading = _one_line(heading)
    if existing is None or not existing.strip():
        return f"# {heading}\n\n{block}\n"
    span = _span(existing)
    if span is not None:
        return existing[:span[0]] + block + existing[span[1]:]
    return existing.rstrip("\n") + "\n\n" + block + "\n"


def _clean_rel(rel) -> str:
    """A repo-relative POSIX path without leading ``./`` or surrounding slashes —
    the one spelling every key, pathspec and prefix in this module is built from."""
    rel = str(rel or "").strip("/")
    while rel.startswith("./"):
        rel = rel[2:].lstrip("/")
    return rel


def project_rels_for_paths(paths: Iterable[str], projects: Sequence[str]) -> list[str]:
    """The projects whose ``_runs`` area the staged *paths* touch, deduplicated,
    in first-seen order; the longest matching project rel wins for each path."""
    ordered = sorted({_clean_rel(project) for project in projects if project}, key=len, reverse=True)
    found: list[str] = []
    for path in paths:
        rel = _clean_rel(path)
        for project in ordered:
            prefix = f"{project}/{RUNS_DIR}"
            if rel == prefix or rel.startswith(prefix + "/"):
                if project not in found:
                    found.append(project)
                break
    return found


def _first_line(value) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return next((line.strip() for line in text.splitlines() if line.strip()), None)


def _text(value) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _name_of(state: Mapping) -> str | None:
    """The recorded name, ``None`` when absent or blank (so the label chain applies)."""
    return (_text(state.get("name")) or "").strip() or None


def _goal_line_of(state: Mapping) -> str | None:
    """The first non-empty line of the recorded goal, ``None`` when there is none."""
    return _first_line(state.get("goal"))


def _source_rel(source_path) -> str | None:
    """``context.source_path`` from its first ``_runs`` component onward."""
    if not isinstance(source_path, str):
        return None
    parts = [part for part in source_path.split("/") if part]
    if RUNS_DIR in parts[:-1]:
        return "/".join(parts[parts.index(RUNS_DIR):])
    return None


def build_summary(*, dir_rel: str, dir_name: str, flat: bool, group: str, state,
                  events: list[dict] | None, first_log: str | None,
                  seeded: bool = False,
                  source_map: Mapping[str, str] | None = None) -> RecordSummary:
    """A :class:`RecordSummary` from already-read inputs (pure).

    *source_map* is the reader-built ``uid -> project-relative dir`` index for
    the project; a record's ``continues`` link is addressed by its source's
    uid (:func:`_source_dir_map`), falling back to ``context.source_path``
    when the uid is unknown.
    """
    uid = None if flat else run_state.session_id_of(dir_name)
    if not isinstance(state, dict):
        return RecordSummary(
            dir_rel=dir_rel, dir_name=dir_name, uid=uid, readable=False, slug=group, name=None,
            goal_line=None, first_log=None, status=None, stop_reason=None, phase=None,
            started_at=None, updated=None, signed_out=None, seeded=False,
            source_uid=None, source_rel=None,
        )
    session = state.get("session")
    session = session if isinstance(session, dict) else {}
    context = state.get("context")
    context = context if isinstance(context, dict) else {}
    source_uid = _text(session.get("source_session_uid")) or None
    if source_uid and source_map and source_uid in source_map:
        source_rel = source_map[source_uid]
    else:
        source_rel = _source_rel(context.get("source_path"))
    return RecordSummary(
        dir_rel=dir_rel,
        dir_name=dir_name,
        uid=uid,
        readable=True,
        slug=_text(state.get("slug")) or group,
        name=_name_of(state),
        goal_line=_goal_line_of(state),
        first_log=first_log,
        status=_text(state.get("status")),
        stop_reason=_text(state.get("stop_reason")),
        phase=_text(state.get("phase")),
        started_at=_stamp(session.get("started_at")) or _stamp(state.get("created")),
        updated=_stamp(state.get("updated")),
        signed_out=None if events is None else signed_out(events),
        seeded=seeded,
        source_uid=source_uid,
        source_rel=source_rel,
    )


# ---------------------------------------------------------------------------
# Readers (IO, lock-free, never raising past the wrapper)
# ---------------------------------------------------------------------------


def _scalar(raw: str):
    """A one-line YAML scalar's value, or :data:`_INVALID` when the text is not
    one: a block indicator, a property, an alias, text the loader rejects, or
    text that loads as a collection (``a: b`` is a mapping where a scalar
    belongs — the loader rejects the whole file for it, and so must the cut)."""
    raw = raw.strip()
    if not raw or raw[0] in "|>&*!":
        return _INVALID
    try:
        value = yaml.load(raw, Loader=_LOADER)
    except Exception:  # YAMLError, or a constructor's ValueError (an impossible date)
        return _INVALID
    return value if isinstance(value, _SCALAR_TYPES) else _INVALID


def _starts_node(block: str, index: int) -> bool:
    """Whether the quote or bracket at *index* begins a YAML node (block context).

    In block context an entry or key indicator is one only at a node position
    — first on its line or after other indicators — and a quote or bracket
    begins a node only right after one, separated by whitespace. So the token
    begins a node when it is first on its line, or when every
    whitespace-separated word before it on the line is structural: a ``-`` or
    ``?`` entry indicator, a key ending in ``:`` (the words before a key are
    that key's own text), or a tag or anchor property. A quote in the middle
    of prose (``x - "p``) is text: taking it for a node could pair it with a
    later real opener and hide that opener's leak.
    """
    if index > 0 and block[index - 1] not in " \t\n":
        return False
    at_node = True
    for word in block[block.rfind("\n", 0, index) + 1:index].split():
        if word[0] in "!&":
            continue
        if at_node and word in ("-", "?"):
            continue
        at_node = word.endswith(":")
    return at_node


def _string_end(block: str, start: int) -> int | None:
    """The index of the quote closing the string opened at *start*, or ``None``
    when it never closes inside *block* (``''`` and ``\\"`` are escapes)."""
    quote = block[start]
    position = start + 1
    while True:
        found = block.find(quote, position)
        if found == -1:
            return None
        if quote == "'":
            if block[found + 1:found + 2] == "'":
                position = found + 2
                continue
            return found
        backslashes = 0
        while found - 1 - backslashes > start and block[found - 1 - backslashes] == "\\":
            backslashes += 1
        if backslashes % 2:
            position = found + 1
            continue
        return found


def _block_is_self_contained(block: str) -> bool:
    """Whether every flow collection and quoted scalar opened in *block* closes in it.

    Those are the only YAML constructs that may continue onto a column-0
    line (a block scalar and a plain multi-line scalar both end at a
    less-indented line), so when each one closes inside the block, cutting
    the block at the next column-0 line removes exactly the context mapping
    and the rest parses as the loader would parse the whole. A quote or an
    opening bracket that begins a node starts a string or a flow collection;
    a string runs to its unescaped closing quote; inside a flow every bracket
    counts and every quote begins a string; a ``#`` after whitespace begins a
    comment. Brackets and quotes inside prose (``don't``, ``[WIP] goal``,
    ``x - "p``) are text (:func:`_starts_node`), so the folded scalars and
    empty ``{}`` / ``[]`` that ``yaml.safe_dump`` writes keep a tool-written
    chain on the fast path, and a hand edit that really leaks falls back.

    The check does not track block indentation, so it has one honest
    residual: a quote or bracket that is first on its line is taken for a
    node start even on the continuation line of a plain multi-line (or
    block) scalar, where it is text, and a phantom string opened there could
    close on a later real opener and hide that opener's leak past column 0.
    Both halves need a hand edit of the tool-owned block: the emitter indents
    every continuation line, so nothing it writes leaks past column 0, and a
    phantom pairing in its output can at worst cost a fallback. An invalid
    block (one the full parse rejects) may likewise still read as a
    context-less mapping here.
    """
    depth = 0
    position = 0
    while True:
        found = _TOKEN_RE.search(block, position)
        if found is None:
            return depth == 0
        index = found.start()
        char = block[index]
        position = index + 1
        if char == "#":
            if index == 0 or block[index - 1] in " \t\n":
                newline = block.find("\n", index)
                if newline == -1:
                    return depth == 0
                position = newline + 1
        elif char in "\"'":
            if depth or _starts_node(block, index):
                close = _string_end(block, index)
                if close is None:
                    return False
                position = close + 1
        elif char in "[{":
            if depth or _starts_node(block, index):
                depth += 1
        elif depth:
            depth -= 1


def _cut_context(text: str) -> tuple[str, dict] | None:
    """``(text without the context block, stub context)`` when the cut is clean.

    Clean means: exactly one column-0 ``context:`` line; nothing opened in the
    text above it still open there (a quoted scalar spanning that line owns
    it, and cutting would drop the scalar's middle); the block below it
    self-contained; and, when the block holds a ``source_path``, the anchored
    expression matched a one-line scalar that does not go on at a deeper
    indent on the next line (the emitter folds a long plain scalar at a
    space, and the first line alone would be a truncated path).
    """
    matches = list(_CONTEXT_LINE_RE.finditer(text))
    if len(matches) != 1:
        return None
    match = matches[0]
    head = text[:match.start()]
    if not _block_is_self_contained(head):
        return None
    line_end = text.find("\n", match.end())
    block_start = len(text) if line_end == -1 else line_end + 1
    end_match = _BLOCK_END_RE.search(text, block_start)
    block_end = len(text) if end_match is None else end_match.start()
    block = text[block_start:block_end]
    if not _block_is_self_contained(block):
        return None
    stub: dict = {}
    if "source_path" in block:
        found = _SOURCE_PATH_RE.search(block)
        if found is None or _CONTINUED_RE.match(block, found.end()):
            return None
        value = _scalar(found.group(1))
        if value is _INVALID:
            return None
        stub = {"source_path": value}
    return head + text[block_end:], stub


def _parse_mapping(text: str) -> dict | None:
    try:
        data = yaml.load(text, Loader=_LOADER)
    except Exception:  # YAMLError, a constructor's ValueError, RecursionError on deep nesting
        return None
    return data if isinstance(data, dict) else None


def read_state_fast(path: Path) -> dict | None:
    """The state mapping with the nested ``context`` reduced to its
    ``source_path`` — the only context field the kernel reads.

    A continuation's ``context:`` embeds its source's whole state, which
    embeds its own source's, so a chain's records grow with their depth and
    a full parse of the deepest costs more than every other row combined.
    When a column-0 ``context:`` line opens a block that the next column-0
    line (or the end of the file) delimits, the block is cut out textually,
    the rest is parsed, and ``source_path`` is read from the block with one
    anchored expression (parsed with the same loader). Anything less clean —
    no such line, a cut that does not parse, a ``source_path`` the
    expression did not match, one that continues onto a deeper-indented
    line or is not a scalar, a quoted scalar or flow collection that does
    not close inside the text above the line or inside the block — falls
    back to a full parse, so both paths yield identical kernel fields.
    Residuals only a hand edit of the tool-owned block can reach: a block
    the loader rejects for its indentation may still read as a context-less
    mapping here, and a quote or bracket first on the continuation line of a
    plain multi-line scalar is taken for a node start, which costs at worst
    a fallback (:func:`_block_is_self_contained`). ``None`` when the file is
    unreadable or not a mapping; nothing is ever renamed or written.
    """
    try:
        text = Path(path).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    cut = _cut_context(text)
    if cut is not None:
        data = _parse_mapping(cut[0])
        if data is not None:
            data["context"] = cut[1]
            return data
    return _parse_mapping(text)


def _first_message(text: str) -> str | None:
    """The first `log.yaml` entry's ``message`` (pure).

    The text up to the second top-level item is parsed first — the first
    entry is determined by its own lines — and the whole text only when
    that head does not parse on its own.
    """
    candidates = [text]
    items = list(_LOG_ITEM_RE.finditer(text))
    if len(items) > 1:
        candidates.insert(0, text[:items[1].start()])
    for candidate in candidates:
        try:
            data = yaml.load(candidate, Loader=_LOADER)
        except Exception:  # YAMLError, or a constructor's ValueError on a hand-edited stamp
            continue
        if isinstance(data, list) and data and isinstance(data[0], dict):
            message = data[0].get("message")
            return message if isinstance(message, str) and message.strip() else None
        return None
    return None


def _first_log_message(rdir: Path) -> str | None:
    path = rdir / "log.yaml"
    try:
        if not path.is_file() or path.stat().st_size > LOG_READ_CAP:
            return None
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return _first_message(text)


def _read_events(rdir: Path) -> list[dict] | None:
    """The record's events, ``None`` when the file cannot be read.

    A line whose ``type`` is not a string (a hand edit) carries no lifecycle
    meaning and is dropped the way ``read_events`` drops a torn line, so
    ``signed_out``'s membership test only ever sees strings.
    """
    try:
        events = run_state.read_events(rdir, last_n=0)
    except (OSError, UnicodeDecodeError):
        return None
    return [event for event in events if isinstance(event.get("type"), str)]


def summarize(rdir: Path, project_dir: Path, *,
              source_map: Mapping[str, str] | None = None) -> RecordSummary:
    """Read one record directory under *project_dir* into a :class:`RecordSummary`.

    *source_map* is the reader-built uid index for the project
    (:func:`_source_dir_map`); it is how a ``continues`` link resolves a
    folded, freeze-renamed or archived source. A reader surprise (an
    unreadable file, a YAML error, an OSError, a value of a shape the kernel
    did not expect) is one unreadable row, never an exception out of the scan.
    """
    rdir, project_dir = Path(rdir), Path(project_dir)
    runs = project_dir / RUNS_DIR
    flat = rdir.parent == runs
    dir_rel = rdir.relative_to(project_dir).as_posix()
    group = rdir.name if flat else rdir.parent.name
    try:
        state_path = run_state._state_path(rdir)
        state = None if state_path is None else read_state_fast(state_path)
        events = _read_events(rdir)
        seeded = bool(isinstance(state, dict) and events is not None
                      and is_seeded(state, events))
        first_log = None
        if isinstance(state, dict) and _name_of(state) is None and _goal_line_of(state) is None:
            first_log = _first_log_message(rdir)
        return build_summary(dir_rel=dir_rel, dir_name=rdir.name, flat=flat, group=group,
                             state=state, events=events, first_log=first_log,
                             seeded=seeded, source_map=source_map)
    except Exception:  # a reader surprise is one unreadable row, never a lost index
        return build_summary(dir_rel=dir_rel, dir_name=rdir.name, flat=flat, group=group,
                             state=None, events=None, first_log=None,
                             source_map=source_map)


def _always(_rel: str) -> bool:
    return True


def tracked_record_filter(repo_root: Path, project_rel: str,
                          staged: Sequence[str] = ()) -> Callable[[str], bool]:
    """A predicate over a record's repo-relative dir: tracked or staged.

    One ``git ls-files -z`` over the project's run area. The predicate is
    true when the record's state file is in the index (tracked, or newly
    staged) or the dir equals or lies under one of *staged* — exactly what
    the commit will contain. Any git error (not a checkout, no git, a
    timeout) yields an always-true predicate: a missing filter renders what
    is on disk rather than nothing.
    """
    project_rel = _clean_rel(project_rel)
    # Both the live run area and C's archive home: the uid map resolves a
    # `continues` source that housekeep archived, and an untracked archive dir
    # must be filtered exactly as an untracked live one is (its link would
    # dangle on the forge).
    pathspecs = [
        f"{project_rel}/{RUNS_DIR}" if project_rel else RUNS_DIR,
        f"{project_rel}/_archive/{RUNS_DIR}" if project_rel else f"_archive/{RUNS_DIR}",
    ]
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", *pathspecs],
            cwd=str(repo_root), capture_output=True, check=False,
            env={**os.environ, "LC_ALL": "C"}, timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return _always
    if result.returncode != 0:
        return _always
    tracked = set(result.stdout.decode("utf-8", "surrogateescape").split("\0")) - {""}
    staged_dirs = tuple(cleaned for cleaned in (_clean_rel(path) for path in staged) if cleaned)
    state_files = (run_state.STATE_FILE, run_state.LEGACY_STATE_FILE)

    def include(rel: str) -> bool:
        rel = _clean_rel(rel)
        if any(f"{rel}/{name}" in tracked for name in state_files):
            return True
        return any(rel == path or rel.startswith(path + "/") for path in staged_dirs)

    return include


def _source_dir_map(project_dir: Path, *, include: Callable[[str], bool] | None = None,
                    project_rel: str = "") -> dict[str, str]:
    """``uid -> project-relative record dir`` for every record of the project.

    A ``continues`` link is addressed by ``session.source_session_uid``
    (A1-rest.index-labels), never by its old path: a folded
    (``_runs/chat-./<uid>`` -> ``_runs/chat/<uid>``), freeze-renamed
    (``<uid>`` -> ``<uid>--<name>``) or archived source still resolves. The
    archive home (``_archive/_runs/<slug>/<uid>[--<name>]``) is walked too,
    and honours *include* like the live area so a link to an untracked dir
    never dangles on the forge. IO lives here so :func:`render_row` stays
    pure; built once per scan.
    """
    project_dir = Path(project_dir)
    project_rel = _clean_rel(project_rel)
    found: dict[str, str] = {}

    def _key(rel: str) -> str:
        return f"{project_rel}/{rel}" if project_rel else rel

    def _add(rdir: Path) -> None:
        uid = run_state.session_id_of(rdir.name)
        if not uid:
            return
        rel = rdir.relative_to(project_dir).as_posix()
        if include is not None and not include(_key(rel)):
            return
        found.setdefault(uid, rel)

    for rdir in run_state.iter_record_dirs(project_dir / RUNS_DIR):
        _add(rdir)
    archive = project_dir / "_archive" / RUNS_DIR
    if archive.is_dir():
        for group in sorted(archive.iterdir()):
            if not group.is_dir():
                continue
            for rdir in sorted(group.iterdir()):
                if rdir.is_dir():
                    _add(rdir)
    return found


def scan_project(project_dir: Path, *, include: Callable[[str], bool] | None = None,
                 project_rel: str = "") -> list[RecordSummary]:
    """Every record the resolvers' own walk finds under ``<project>/_runs``,
    filtered by ``include(<repo-relative dir>)`` when a filter is given; the
    *project_rel* prefix is normalised like the filter's own, so a trailing
    slash or a ``./`` cannot build a key the tracked set never holds."""
    project_dir = Path(project_dir)
    project_rel = _clean_rel(project_rel)
    source_map = _source_dir_map(project_dir, include=include, project_rel=project_rel)
    summaries = []
    for rdir in run_state.iter_record_dirs(project_dir / RUNS_DIR):
        dir_rel = rdir.relative_to(project_dir).as_posix()
        if include is not None and not include(f"{project_rel}/{dir_rel}" if project_rel else dir_rel):
            continue
        summaries.append(summarize(rdir, project_dir, source_map=source_map))
    return summaries


# ---------------------------------------------------------------------------
# Writers (the README only; never anything under _runs/)
# ---------------------------------------------------------------------------


def project_dir_refusal(repo_root: Path, project_rel: str) -> str | None:
    """Why no README may be written in ``<repo_root>/<project_rel>``, or ``None``.

    The write boundary both index writers hold a project directory to before
    they touch it (the publication choke point and ``lmer-schema index
    --write``): it must be the real directory its repo-relative path names.
    Refused, in this order: one that resolves outside the resolved root; one
    inside a run record (a ``_runs`` component between the root and the
    directory — a record is history, never rewritten); one that is itself a
    symlink or lies below one. The walk hands a registered short name over by
    name and ``is_dir()`` follows a link, while the README's own symlink check
    sees only the final file, so without this an alias would carry the write
    wherever it points. The root itself is resolved, so a checkout reached
    through a symlink is fine. A missing directory is the caller's to report.
    """
    root = Path(repo_root)
    parts = Path(_clean_rel(project_rel)).parts
    try:
        real_root = root.resolve()
        real = root.joinpath(*parts).resolve()
    except (OSError, RuntimeError):  # a symlink loop, an unreadable level
        return "project directory cannot be resolved"  # no exception text: it names machine paths
    if not real.is_relative_to(real_root):
        return "project directory resolves outside the work repo"
    if RUNS_DIR in parts or RUNS_DIR in real.relative_to(real_root).parts:
        return f"project directory lies inside a {RUNS_DIR}/ record"
    level = root
    for index, part in enumerate(parts):
        level = level / part
        if level.is_symlink():
            return ("project directory is a symlink" if index == len(parts) - 1
                    else "project directory lies below a symlink")
    return None


def _read_readme(directory: Path) -> tuple[bytes, str] | None:
    """``(raw bytes, text)`` of a directory's README, ``None`` when absent;
    refuses (:class:`IndexSkipped`) a symlink, a directory, a file that cannot
    be read and text that is not UTF-8."""
    path = Path(directory) / README_NAME
    if path.is_symlink():
        raise IndexSkipped(f"{README_NAME} is a symlink")
    if path.is_dir():
        raise IndexSkipped(f"{README_NAME} is a directory")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise IndexSkipped(f"{README_NAME} is not readable ({exc.strerror or exc})") from exc
    try:
        return raw, raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IndexSkipped(f"{README_NAME} is not UTF-8 text") from exc


def _refuse_untouchable(existing: str) -> None:
    """Raise :class:`IndexSkipped` for a README the generator must leave alone."""
    try:
        block = extract_block(existing)
    except MalformedMarkers as exc:
        raise IndexSkipped(f"markers malformed ({exc})") from exc
    token = block_format(block)
    if token is not None and token > FORMAT:
        raise IndexSkipped(f"index block is of a newer format ({token} > {FORMAT}); upgrade lmer")
    if hand_text_has_marker(existing):
        raise IndexSkipped("hand text holds a conflict-marker line (seven = < > or |); "
                           "indent or lengthen the line")


def _atomic_write(path: Path, data: bytes, previous_mode: int | None) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, previous_mode if previous_mode is not None else 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def render_project_readme(project_dir: Path, *, include: Callable[[str], bool] | None = None,
                          project_rel: str = "", heading: str) -> str:
    """The project README's full new text; raises :class:`MalformedMarkers`
    (and :class:`IndexSkipped` for a symlink, a directory or non-UTF-8 text)."""
    found = _read_readme(project_dir)
    block = render_project_block(scan_project(project_dir, include=include, project_rel=project_rel))
    return splice_block(None if found is None else found[1], block, heading=heading)


def write_project_readme(project_dir: Path, *, include: Callable[[str], bool] | None = None,
                         project_rel: str = "", heading: str) -> tuple[bool, bytes | None]:
    """Write the project README when its bytes would change.

    Returns ``(changed, previous bytes or None when the file did not exist)``
    so a caller can restore the file when a later step refuses the commit.
    Raises :class:`IndexSkipped` for a symlink, a directory, malformed
    markers, a block of a newer format, or hand text with a conflict-marker
    line; the file is untouched in every refused case.
    """
    project_dir = Path(project_dir)
    path = project_dir / README_NAME
    found = _read_readme(project_dir)
    existing = None if found is None else found[1]
    if existing is not None:
        _refuse_untouchable(existing)
    block = render_project_block(scan_project(project_dir, include=include, project_rel=project_rel))
    new = splice_block(existing, block, heading=heading).encode("utf-8")
    previous = None if found is None else found[0]
    if previous == new:
        return False, previous
    mode = None if previous is None else (path.stat().st_mode & 0o7777)
    _atomic_write(path, new, mode)
    return True, previous


def render_root_readme(root: Path, manifest_document: Mapping) -> str:
    """The root README's full new text with the manifest's project block."""
    found = _read_readme(root)
    name = manifest_document.get("name") if isinstance(manifest_document, Mapping) else None
    heading = str(name) if name else Path(root).name
    return splice_block(None if found is None else found[1], render_root_block(manifest_document),
                        heading=heading)
