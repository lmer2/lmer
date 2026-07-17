"""Tests for the LMER_DISPATCH_* env plumbing (dispatch-model-routing).

Source-level guard tests in the repo's established pattern
(test_lmer_reasoning_effort.py::test_cli_env_dict_declares_reasoning_effort):
the container-env dict in cli.py is built inline, so we guard against
accidental removal of the five lane vars rather than re-testing the dict
logic. Also guards the session-level effort vocabulary in claude-runner.sh
(xhigh joined it so the session and lane surfaces accept the same set).
"""
import re
from pathlib import Path

import pytest

from lmer_cli.container.dispatch_agents import ENV_PREFIX, LANE_AGENTS

REPO_ROOT = Path(__file__).parent.parent
CLI_PY = REPO_ROOT / "src" / "lmer_cli" / "cli.py"
CLAUDE_RUNNER = REPO_ROOT / "libexec" / "claude-runner.sh"


@pytest.mark.parametrize("lane", sorted(LANE_AGENTS))
def test_cli_env_dict_declares_dispatch_lane(lane):
    """Guard against removal of a LMER_DISPATCH_* entry from cli.py's env dict.

    Without the passthrough entry, setting the var on the host has no
    effect on container-side behavior (env-vars.md convention).
    """
    var = ENV_PREFIX + lane
    source = CLI_PY.read_text()
    pattern = re.compile(
        rf"""["']{var}["']\s*:\s*os\.environ\.get\(\s*["']{var}["']\s*\)"""
    )
    assert pattern.search(source), f"{var} entry missing from cli.py env dict"


def test_claude_runner_accepts_xhigh_effort():
    """The session effort vocabulary includes xhigh (spec §5.5).

    Lane efforts (LMER_DISPATCH_*) accept xhigh; a session surface that
    rejects it would be a confusingly divergent operator surface.
    """
    source = CLAUDE_RUNNER.read_text()
    assert re.search(r"^\s*low\|medium\|high\|xhigh\|max\)", source, re.MULTILINE), (
        "claude-runner.sh effort case pattern must accept low|medium|high|xhigh|max"
    )


def test_claude_runner_warning_names_xhigh():
    """The invalid-effort warning documents the full accepted set."""
    source = CLAUDE_RUNNER.read_text()
    assert "low|medium|high|xhigh|max|auto" in source
