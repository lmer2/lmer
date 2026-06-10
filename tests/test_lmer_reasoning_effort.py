"""Tests for LMER_REASONING_EFFORT env var → claude --effort flag translation.

Covers two layers:
1. Python (cli.py): the env var is declared in the host→container env dict.
   Verified by a source-level sanity check (the env dict is built inline in
   main(), so we guard against accidental removal rather than re-testing the
   local logic).
2. Bash (claude-runner.sh): the env var is translated into claude's --effort
   flag with normalization and validation. Values are lowercased before
   matching, "auto"/unset pass no flag, and invalid values warn and skip.
"""
import re
from pathlib import Path

import pytest

from tests._claude_runner_harness import run_claude_runner, skip_if_npm_claude_present


CLI_PY = Path(__file__).parent.parent / "src" / "lmer_cli" / "cli.py"


def test_cli_env_dict_declares_reasoning_effort():
    """Guard against accidental removal of LMER_REASONING_EFFORT from cli.py's env dict.

    The env dict in main() is constructed inline, so a true unit test would
    require extracting it into a helper. For now a source-level check is
    sufficient to catch drift, with tolerance for formatting changes.
    """
    source = CLI_PY.read_text()
    pattern = re.compile(
        r"""["']LMER_REASONING_EFFORT["']\s*:\s*os\.environ\.get\(\s*["']LMER_REASONING_EFFORT["']\s*\)"""
    )
    assert pattern.search(source), "LMER_REASONING_EFFORT entry missing from cli.py env dict"


def _run_claude_runner(tmp_path, env_value=None):
    env = {} if env_value is None else {"LMER_REASONING_EFFORT": env_value}
    result = run_claude_runner(tmp_path, env)
    return result.output, result.argv


@skip_if_npm_claude_present
class TestClaudeRunnerEffortFlag:
    """Verify claude-runner.sh translates LMER_REASONING_EFFORT → --effort."""

    @pytest.mark.parametrize("level", ["low", "medium", "high", "max"])
    def test_valid_levels_pass_effort_flag(self, tmp_path, level):
        output, argv = _run_claude_runner(tmp_path, env_value=level)

        assert "--effort" in argv, f"--effort missing from claude argv: {argv}"
        idx = argv.index("--effort")
        assert argv[idx + 1] == level
        assert f"Reasoning effort: {level}" in output

    @pytest.mark.parametrize("raw,normalized", [
        ("HIGH", "high"),
        ("High", "high"),
        ("Medium", "medium"),
        ("AUTO", "auto"),
    ])
    def test_case_insensitive_normalization(self, tmp_path, raw, normalized):
        output, argv = _run_claude_runner(tmp_path, env_value=raw)

        if normalized == "auto":
            assert "--effort" not in argv
        else:
            assert "--effort" in argv
            idx = argv.index("--effort")
            assert argv[idx + 1] == normalized

    def test_auto_does_not_pass_flag(self, tmp_path):
        output, argv = _run_claude_runner(tmp_path, env_value="auto")

        assert "--effort" not in argv
        # No warning should print for the documented "auto" value
        assert "Ignoring LMER_REASONING_EFFORT" not in output

    def test_unset_does_not_pass_flag(self, tmp_path):
        output, argv = _run_claude_runner(tmp_path, env_value=None)

        assert "--effort" not in argv
        assert "Ignoring LMER_REASONING_EFFORT" not in output

    def test_invalid_value_warns_and_skips(self, tmp_path):
        output, argv = _run_claude_runner(tmp_path, env_value="banana")

        assert "--effort" not in argv
        assert "Ignoring LMER_REASONING_EFFORT='banana'" in output
        assert "low|medium|high|max|auto" in output

    def test_empty_string_does_not_pass_flag(self, tmp_path):
        output, argv = _run_claude_runner(tmp_path, env_value="")

        assert "--effort" not in argv
