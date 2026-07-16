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

Segments, each omitted when its inputs are unavailable:

- repo — ``LMER_REPO_PROJECT``, falling back to the basename of the git
  toplevel at the payload's cwd.
- branch — ``git branch --show-current`` at the payload's cwd (empty on a
  detached HEAD → omitted).
- task — ``LMER_TASK``.
- context — percentage of the context window used, from the payload's
  ``context_window`` object. The payload schema evolves, so several shapes
  are tolerated and absence just drops the segment.

The 📦 (container) and ⚡ (danger zone) indicators previously appended by
the ``claude-status`` bash wrapper are preserved here.

Constraints (this runs on every status render): stdlib only, no network,
no project imports, and the only subprocesses are two short-timeout git
calls. Every failure path degrades to fewer segments — never a traceback,
never a non-zero exit.
"""
from __future__ import annotations

import json
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


# ---------------------------------------------------------------------------
# Pure logic — unit-testable seams. No env reads, no filesystem, no
# subprocesses; every input is injected by the caller.
# ---------------------------------------------------------------------------


def _number(value) -> float | None:
    """*value* as a float when it is a real number, else None (bools are not
    token counts)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


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
            return max(0, min(100, round(percent)))

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
    return max(0, min(100, round(100 * used / size)))


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


def build_line(
    *,
    repo: str | None,
    branch: str | None,
    task: str | None,
    percent: int | None,
    container: bool = False,
    danger_zone: bool = False,
    model: str | None = None,
) -> str:
    """
    Assemble the status line from fully injected segment values.

    ``repo @ branch | task | ctx N%`` with absent segments omitted; when
    every segment is missing, fall back to the model display name so the
    line is never blank in a healthy session. Indicators (📦 container,
    ⚡ danger zone) are appended after the segments.
    """
    head = " @ ".join(part for part in (repo, branch) if part)
    parts = [part for part in (head, task) if part]
    if percent is not None:
        parts.append(f"ctx {percent}%")
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

    cwd = payload_cwd(payload)
    line = build_line(
        repo=gather_repo(cwd),
        branch=gather_branch(cwd),
        task=os.environ.get("LMER_TASK", "").strip() or None,
        percent=context_percent(payload),
        container=os.environ.get("CLAUDE_CONTAINER") == "true",
        danger_zone=os.environ.get("LMER_DANGER_ZONE") == "1",
        model=model_name(payload),
    )
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
