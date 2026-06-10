"""Tests for LMER_LLM_NAME env var → claude --model flag translation.

Covers two layers:
1. Python (cli.py): the env var is declared in the host→container env dict.
   Verified by a source-level sanity check (the env dict is built inline in
   main(), so we guard against accidental removal rather than re-testing the
   local logic).
2. Bash (claude-runner.sh): the env var is passed verbatim to claude's
   --model flag. No validation or normalization happens — claude itself
   rejects unknown models. Unset or empty means no flag is passed.
"""
import re
from pathlib import Path

import pytest

from tests._claude_runner_harness import run_claude_runner, skip_if_npm_claude_present


CLI_PY = Path(__file__).parent.parent / "src" / "lmer_cli" / "cli.py"


def test_cli_env_dict_declares_llm_name():
    """Guard against accidental removal of LMER_LLM_NAME from cli.py's env dict.

    The env dict in main() is constructed inline, so a true unit test would
    require extracting it into a helper. For now a source-level check is
    sufficient to catch drift, with tolerance for formatting changes.
    """
    source = CLI_PY.read_text()
    pattern = re.compile(
        r"""["']LMER_LLM_NAME["']\s*:\s*os\.environ\.get\(\s*["']LMER_LLM_NAME["']\s*\)"""
    )
    assert pattern.search(source), "LMER_LLM_NAME entry missing from cli.py env dict"


def _run_claude_runner(tmp_path, env_value=None):
    env = {} if env_value is None else {"LMER_LLM_NAME": env_value}
    result = run_claude_runner(tmp_path, env)
    return result.output, result.argv


@skip_if_npm_claude_present
class TestClaudeRunnerModelFlag:
    """Verify claude-runner.sh translates LMER_LLM_NAME → --model."""

    @pytest.mark.parametrize("model", ["sonnet", "opus", "haiku", "claude-sonnet-4-6"])
    def test_value_passes_model_flag_verbatim(self, tmp_path, model):
        output, argv = _run_claude_runner(tmp_path, env_value=model)

        assert "--model" in argv, f"--model missing from claude argv: {argv}"
        idx = argv.index("--model")
        assert argv[idx + 1] == model
        assert f"Claude model: {model}" in output

    def test_no_case_normalization(self, tmp_path):
        """The value reaches claude exactly as given — no lowercasing."""
        output, argv = _run_claude_runner(tmp_path, env_value="Sonnet")

        assert "--model" in argv
        idx = argv.index("--model")
        assert argv[idx + 1] == "Sonnet"

    def test_unset_does_not_pass_flag(self, tmp_path):
        output, argv = _run_claude_runner(tmp_path, env_value=None)

        assert "--model" not in argv

    def test_empty_string_does_not_pass_flag(self, tmp_path):
        output, argv = _run_claude_runner(tmp_path, env_value="")

        assert "--model" not in argv
