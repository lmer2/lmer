"""A worker is told its supervisor authority and the rules around it (issue #594)."""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from lmer_cli.comms import SUPERVISOR_AUTHORITY_LEVELS

REPO = Path(__file__).resolve().parents[1]
RENDERER = REPO / "libexec" / "render-prompt-fragment.py"


def _render(env):
    clean = {key: value for key, value in os.environ.items() if not key.startswith("LMER_")}
    result = subprocess.run([sys.executable, str(RENDERER), str(REPO / "prompts/orchestrator-ask.md.jinja2")],
                            env={**clean, "LMER_ASK_DIR": "/x", **env}, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize("level", SUPERVISOR_AUTHORITY_LEVELS)
def test_the_fragment_names_the_launch_level_and_every_level(level):
    out = _render({"LMER_SUPERVISOR_AUTHORITY": level})
    assert f"This session started at `{level}`" in out
    for each in SUPERVISOR_AUTHORITY_LEVELS:
        assert f"- `{each}`:" in out


def test_an_unset_level_reads_as_none_and_the_operator_only_rules_are_there():
    out = _render({})
    assert "This session started at `none`" in out
    assert "lmer-signal ask --operator-only" in out
    assert "counts for nothing" in out
    assert "[Ratchet · authority=approvals]" in out


def test_the_fragment_follows_the_configured_name():
    out = _render({"LMER_PLATFORM_ASSISTANT_NAME": "Sprocket"})
    assert "### What Sprocket may approve" in out
    assert "[Sprocket · authority=approvals]" in out


def test_cli_env_dict_forwards_the_level():
    source = (REPO / "src/lmer_cli/cli.py").read_text(encoding="utf-8")
    assert re.search(r'"LMER_SUPERVISOR_AUTHORITY":\s*os\.environ\.get\("LMER_SUPERVISOR_AUTHORITY"\)', source)


def test_documented_in_the_env_var_reference():
    assert "**`LMER_SUPERVISOR_AUTHORITY`**" in (REPO / "docs/LMER-CLI.md").read_text(encoding="utf-8")
