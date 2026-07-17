#!/usr/bin/env python3
"""
Claude Code status line renderer for lmer sessions (issue #106).

Claude Code invokes the configured statusLine command (settings.json →
``claude-status`` → this script) on every render, piping a JSON payload on
stdin (model info, workspace dirs, context/token usage), and displays the
single line the command prints. Inside an lmer container you previously
could not tell at a glance which repo/branch the session was on or how much
context it had burned — this script renders exactly that:

    group/project @ feature/x | develop | ctx 42%

Which segments render — and in what order — is configurable via
``LMER_STATUSLINE`` (issue #121): a comma-separated, case-insensitive
segment list. Unset or blank keeps the default ``repo,branch,task,ctx``
(byte-identical to the line above); unknown names are ignored, and a list
that selects nothing falls back to the default rather than a blank line.

Segments, each omitted when its inputs are unavailable:

- repo — ``LMER_REPO_PROJECT``, falling back to the basename of the git
  toplevel at the payload's cwd.
- branch — ``git branch --show-current`` at the payload's cwd (empty on a
  detached HEAD → omitted). Joined to an immediately preceding repo
  segment with `` @ ``.
- task — ``LMER_TASK``.
- ctx — percentage of the context window used, from the payload's
  ``context_window`` object. The payload schema evolves, so several shapes
  are tolerated and absence just drops the segment.
- model — the payload's model display name (e.g. ``Fable``).
- cost — session cost from ``cost.total_cost_usd`` (e.g. ``$1.23``).
- 5h / 7d — subscription usage-limit windows from ``rate_limits``
  (e.g. ``5h 24%``); the payload only carries them on subscription
  sessions, so elsewhere they simply drop out.
- effort — reasoning effort level (e.g. ``eff high``).
- duration — session wall-clock time from ``cost.total_duration_ms``
  (e.g. ``1h23m``).
- lines — lines added/removed from the ``cost`` object (e.g. ``+156/-23``).

The 📦 (container) and ⚡ (danger zone) indicators previously appended by
the ``claude-status`` bash wrapper are preserved here and are not part of
the configurable segment list.

Constraints (this runs on every status render): stdlib only, no network,
no project imports, and the only subprocesses are up to two short-timeout
git calls — run only when the repo/branch segments are configured. Every
failure path degrades to fewer segments — never a traceback, never a
non-zero exit. The optional-segment payload shapes (``cost``,
``rate_limits``, ``effort``) follow the Claude Code statusLine docs
(https://code.claude.com/docs/en/statusline).
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys

# A status render must never stall the session on a slow/hung git.
GIT_TIMEOUT_SECONDS = 2

# Fallback workspace root when the payload carries no usable cwd.
DEFAULT_WORKSPACE = "/workspace"

# Direct percentage keys tried on the payload's context_window object, in
# order, before falling back to computing used/size ourselves.
PERCENT_KEYS = ("used_percentage", "percent_used", "usage_percent")

# Keys that may carry the total tokens used / the window size.
USED_KEYS = ("total_tokens_used", "used_tokens", "total_input_tokens")
SIZE_KEYS = ("context_window_size", "size", "max_tokens")

# Rendered when LMER_STATUSLINE is unset/blank — the pre-#121 line.
DEFAULT_SEGMENTS = ("repo", "branch", "task", "ctx")


# ---------------------------------------------------------------------------
# Pure logic — unit-testable seams. No env reads, no filesystem, no
# subprocesses; every input is injected by the caller.
# ---------------------------------------------------------------------------


def _number(value) -> float | None:
    """*value* as a float when it is a real, finite number, else None (bools
    are not token counts; json.loads admits NaN/Infinity literals, which
    would crash round()/int() downstream)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _clamp_percent(value: float) -> int:
    """*value* rounded and clamped to the displayable 0–100 range."""
    return max(0, min(100, round(value)))


def _usage_sum(usage) -> float | None:
    """Sum the numeric values of a usage dict (input/cache/output token
    counts); None when *usage* is not a dict or holds no numbers."""
    if not isinstance(usage, dict):
        return None
    totals = [n for n in (_number(v) for v in usage.values()) if n is not None]
    return sum(totals) if totals else None


def context_percent(payload) -> int | None:
    """
    Percentage of the context window used, from a statusline payload.

    Tries, in order: a direct percentage field on ``context_window``; a
    used-tokens field divided by a window-size field; the summed
    ``current_usage`` token counts divided by a window-size field. Returns
    an int clamped to 0–100, or None when the payload carries nothing
    usable — the caller omits the segment rather than guessing.
    """
    if not isinstance(payload, dict):
        return None
    window = payload.get("context_window")
    if not isinstance(window, dict):
        return None

    for key in PERCENT_KEYS:
        percent = _number(window.get(key))
        if percent is not None:
            return _clamp_percent(percent)

    size = next((n for n in (_number(window.get(k)) for k in SIZE_KEYS) if n), None)
    if size is None or size <= 0:
        return None
    used = next(
        (n for n in (_number(window.get(k)) for k in USED_KEYS) if n is not None),
        None,
    )
    if used is None:
        used = _usage_sum(window.get("current_usage"))
    if used is None:
        return None
    return _clamp_percent(100 * used / size)


def payload_cwd(payload) -> str:
    """The directory to run git in: workspace.current_dir, then cwd, then
    the conventional /workspace."""
    if isinstance(payload, dict):
        workspace = payload.get("workspace")
        if isinstance(workspace, dict):
            current = workspace.get("current_dir")
            if isinstance(current, str) and current.strip():
                return current
        cwd = payload.get("cwd")
        if isinstance(cwd, str) and cwd.strip():
            return cwd
    return DEFAULT_WORKSPACE


def model_name(payload) -> str | None:
    """The model display name from the payload, for the no-segment fallback."""
    if isinstance(payload, dict):
        model = payload.get("model")
        if isinstance(model, dict):
            name = model.get("display_name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return None


def _cost(payload) -> dict:
    """The payload's ``cost`` object, or {} when absent/malformed."""
    if isinstance(payload, dict):
        cost = payload.get("cost")
        if isinstance(cost, dict):
            return cost
    return {}


def ctx_text(payload) -> str | None:
    """The context segment, e.g. ``ctx 42%``."""
    percent = context_percent(payload)
    return None if percent is None else f"ctx {percent}%"


def cost_text(payload) -> str | None:
    """The session-cost segment from ``cost.total_cost_usd``, e.g. ``$1.23``."""
    usd = _number(_cost(payload).get("total_cost_usd"))
    return None if usd is None else f"${usd:.2f}"


def rate_limit_text(payload, window: str, label: str) -> str | None:
    """A usage-limit segment (``5h 24%``) from ``rate_limits.<window>``;
    None when the payload carries no such window (e.g. non-subscription
    sessions)."""
    if not isinstance(payload, dict):
        return None
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return None
    entry = limits.get(window)
    if not isinstance(entry, dict):
        return None
    percent = _number(entry.get("used_percentage"))
    if percent is None:
        return None
    return f"{label} {_clamp_percent(percent)}%"


def effort_text(payload) -> str | None:
    """The reasoning-effort segment from ``effort.level``, e.g. ``eff high``."""
    if not isinstance(payload, dict):
        return None
    effort = payload.get("effort")
    if not isinstance(effort, dict):
        return None
    level = effort.get("level")
    if isinstance(level, str) and level.strip():
        return f"eff {level.strip()}"
    return None


def duration_text(payload) -> str | None:
    """The session wall-clock segment from ``cost.total_duration_ms``:
    ``59s`` under a minute, ``23m`` under an hour, ``1h03m`` beyond."""
    ms = _number(_cost(payload).get("total_duration_ms"))
    if ms is None or ms < 0:
        return None
    seconds = int(ms // 1000)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60:02d}m"


def lines_text(payload) -> str | None:
    """The lines-changed segment from the ``cost`` object, e.g. ``+156/-23``;
    a missing half counts as 0, both missing drops the segment."""
    cost = _cost(payload)
    added = _number(cost.get("total_lines_added"))
    removed = _number(cost.get("total_lines_removed"))
    if added is None and removed is None:
        return None
    return f"+{max(0, int(added or 0))}/-{max(0, int(removed or 0))}"


def parse_segments(raw) -> tuple[str, ...]:
    """
    Parse an ``LMER_STATUSLINE`` value into an ordered segment-name tuple.

    Comma-separated, case-insensitive, whitespace-tolerant; names outside
    SEGMENT_NAMES are dropped. Unset/blank input — or a list that selects
    nothing — yields DEFAULT_SEGMENTS, so a typo'd config degrades to the
    default line instead of a blank one.
    """
    if not isinstance(raw, str) or not raw.strip():
        return DEFAULT_SEGMENTS
    # SEGMENT_NAMES lives beside SEGMENT_GATHERERS (end of file, after the
    # gatherers it names) — resolved at call time, so the forward reference
    # is deliberate rather than a top-constant to keep the vocabulary and
    # the dispatch table in one place.
    names = tuple(
        name
        for name in (part.strip().lower() for part in raw.split(","))
        if name in SEGMENT_NAMES
    )
    return names or DEFAULT_SEGMENTS


def build_line(
    values: dict,
    *,
    segments: tuple[str, ...] = DEFAULT_SEGMENTS,
    container: bool = False,
    danger_zone: bool = False,
    model: str | None = None,
) -> str:
    """
    Assemble the status line from rendered segment *values* in *segments*
    order.

    *values* maps segment names to their rendered text (or None); absent
    segments are omitted. A branch segment immediately following a repo
    segment joins it with `` @ ``; everything else joins with `` | ``.
    When every segment is missing, fall back to the model display name so
    the line is never blank in a healthy session. Indicators (📦 container,
    ⚡ danger zone) are appended after the segments.
    """
    parts: list[str] = []
    index = 0
    while index < len(segments):
        name = segments[index]
        if name == "repo" and index + 1 < len(segments) and segments[index + 1] == "branch":
            head = " @ ".join(
                part for part in (values.get("repo"), values.get("branch")) if part
            )
            if head:
                parts.append(head)
            index += 2
            continue
        value = values.get(name)
        if value:
            parts.append(value)
        index += 1
    if not parts and model:
        parts.append(model)

    line = " | ".join(parts)
    indicators = ("📦" if container else "") + ("⚡" if danger_zone else "")
    if indicators:
        line = f"{line} {indicators}" if line else indicators
    return line


# ---------------------------------------------------------------------------
# Impure gatherers — every one fails open by returning None on any error.
# ---------------------------------------------------------------------------


def _git(cwd: str, *args: str) -> str | None:
    """Run a git command; return stripped stdout, or None on any failure."""
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def gather_repo(cwd: str) -> str | None:
    """Repo segment: LMER_REPO_PROJECT, else basename of the git toplevel."""
    project = os.environ.get("LMER_REPO_PROJECT", "").strip()
    if project:
        return project
    toplevel = _git(cwd, "rev-parse", "--show-toplevel")
    if toplevel:
        return os.path.basename(toplevel) or None
    return None


def gather_branch(cwd: str) -> str | None:
    """Branch segment: the current branch name; None outside a repo or on a
    detached HEAD."""
    branch = _git(cwd, "branch", "--show-current")
    return branch or None


def gather_task() -> str | None:
    """Task segment: LMER_TASK."""
    return os.environ.get("LMER_TASK", "").strip() or None


# Segment name → gatherer. Called only for configured segments, so the git
# subprocesses in repo/branch never run when those segments are toggled off.
SEGMENT_GATHERERS = {
    "repo": lambda payload, cwd: gather_repo(cwd),
    "branch": lambda payload, cwd: gather_branch(cwd),
    "task": lambda payload, cwd: gather_task(),
    "ctx": lambda payload, cwd: ctx_text(payload),
    "model": lambda payload, cwd: model_name(payload),
    "cost": lambda payload, cwd: cost_text(payload),
    "5h": lambda payload, cwd: rate_limit_text(payload, "five_hour", "5h"),
    "7d": lambda payload, cwd: rate_limit_text(payload, "seven_day", "7d"),
    "effort": lambda payload, cwd: effort_text(payload),
    "duration": lambda payload, cwd: duration_text(payload),
    "lines": lambda payload, cwd: lines_text(payload),
}
SEGMENT_NAMES = frozenset(SEGMENT_GATHERERS)


def main() -> int:
    """
    Statusline entrypoint: payload on stdin, one line on stdout.

    Always returns 0 — a broken status line must degrade, never break the
    session's renderer.
    """
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except (ValueError, TypeError):
        payload = {}

    # Belt and braces: a statusline bug must degrade to a bare line, never
    # surface as a traceback in the session's renderer.
    try:
        cwd = payload_cwd(payload)
        segments = parse_segments(os.environ.get("LMER_STATUSLINE"))
        values = {
            name: SEGMENT_GATHERERS[name](payload, cwd) for name in set(segments)
        }
        line = build_line(
            values,
            segments=segments,
            container=os.environ.get("CLAUDE_CONTAINER") == "true",
            danger_zone=os.environ.get("LMER_DANGER_ZONE") == "1",
            model=model_name(payload),
        )
    except Exception:
        line = ""
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
