"""Tests for the shipped lane agent definitions (dispatch-model-routing).

The five dispatch lanes (REVIEW, DESIGN, CODE, MECHANICAL, EXPLORE) each
map to a shipped agent def under agent-files/claude/agents/. None of them
may carry a hardcoded ``model:``/``effort:`` pin — per-lane configuration
(``LMER_DISPATCH_<LANE>``) is the only source of model pins, and an unset
lane inherits the session model (spec G3; explorer's former ``model:
sonnet`` pin was deliberately removed).
"""
import re
from pathlib import Path

import pytest

from lmer_cli.container.dispatch_agents import LANE_AGENTS

AGENTS_DIR = Path(__file__).parent.parent / "agent-files" / "claude" / "agents"


def _frontmatter(text: str) -> dict:
    """Parse the leading --- fence into a key->value dict."""
    lines = text.split("\n")
    assert lines[0].strip() == "---", "agent def must start with a frontmatter fence"
    close = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    fields = {}
    for line in lines[1:close]:
        match = re.match(r"^(\w[\w-]*):\s*(.*)$", line)
        if match:
            fields[match.group(1)] = match.group(2).strip()
    return fields


@pytest.mark.parametrize("lane,stem", sorted(LANE_AGENTS.items()))
def test_lane_agent_def_exists(lane, stem):
    assert (AGENTS_DIR / f"{stem}.md").is_file(), (
        f"lane {lane} requires agent-files/claude/agents/{stem}.md"
    )


@pytest.mark.parametrize("stem", sorted(LANE_AGENTS.values()))
def test_lane_agent_frontmatter_valid(stem):
    fields = _frontmatter((AGENTS_DIR / f"{stem}.md").read_text())
    assert fields.get("name") == stem
    assert fields.get("description")
    assert fields.get("tools")


@pytest.mark.parametrize("stem", sorted(LANE_AGENTS.values()))
def test_lane_agent_has_no_hardcoded_pin(stem):
    """Config is the only source of model/effort pins (spec §5.3)."""
    fields = _frontmatter((AGENTS_DIR / f"{stem}.md").read_text())
    assert "model" not in fields, (
        f"{stem}.md pins model: {fields.get('model')!r} — lane pins must come "
        "from LMER_DISPATCH_* configuration only"
    )
    assert "effort" not in fields, (
        f"{stem}.md pins effort: {fields.get('effort')!r} — lane pins must "
        "come from LMER_DISPATCH_* configuration only"
    )


def test_read_only_lanes_carry_no_write_tools():
    """designer and explorer are read-only roles; adversarial-reviewer too."""
    for stem in ("designer", "explorer", "adversarial-reviewer"):
        fields = _frontmatter((AGENTS_DIR / f"{stem}.md").read_text())
        tools = {t.strip() for t in fields["tools"].split(",")}
        assert not {"Edit", "Write"} & tools, f"{stem} must be read-only"
