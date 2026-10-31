"""Tests for LMER_REASONING_EFFORT env var → claude --effort flag translation.

Covers two layers:
1. Python (cli.py): the env var is declared in the host→container env dict.
   Verified by a source-level sanity check (the env dict is built inline in
   main(), so we guard against accidental removal rather than re-testing the
   local logic). ``lmer --effort`` is the flag spelling of the variable, with
   ``--model``'s precedence (flag over exports and a preset's env) but a closed
   vocabulary: an unknown level is refused rather than skipped.
2. Bash (claude-runner.sh): the env var is translated into claude's --effort
   flag with normalization and validation. Values are lowercased before
   matching, "auto"/unset pass no flag, and invalid values warn and skip.
"""
import json
import os
import re
from pathlib import Path

import pytest

from lmer_cli.cli import _apply_effort_selection, _resolve_afk_timeout_ms, parse_args
from lmer_cli.harness import EFFORT_LEVELS, REASONING_EFFORT_ENV
from tests._claude_runner_harness import run_claude_runner, skip_if_npm_claude_present
from tests.test_agents_flag import _EXEC_ARGS, _run_main


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


def test_cli_env_dict_declares_afk_timeout_ms():
    """Guard against accidental removal of CLAUDE_AFK_TIMEOUT_MS from cli.py's env dict."""
    source = CLI_PY.read_text()
    pattern = re.compile(
        r"""["']CLAUDE_AFK_TIMEOUT_MS["']\s*:\s*os\.environ\.get\(\s*["']CLAUDE_AFK_TIMEOUT_MS["']\s*\)"""
    )
    assert pattern.search(source), "CLAUDE_AFK_TIMEOUT_MS entry missing from cli.py env dict"


def test_cli_env_dict_declares_run_state_guard():
    """Guard against accidental removal of LMER_RUN_STATE_GUARD from cli.py's env dict."""
    source = CLI_PY.read_text()
    pattern = re.compile(
        r"""["']LMER_RUN_STATE_GUARD["']\s*:\s*os\.environ\.get\(\s*["']LMER_RUN_STATE_GUARD["']\s*\)"""
    )
    assert pattern.search(source), "LMER_RUN_STATE_GUARD entry missing from cli.py env dict"


class TestResolveAfkTimeoutMs:
    """Behavior tests for cli._resolve_afk_timeout_ms (Slack-default AFK timeout)."""

    def test_slack_bridged_defaults_to_300000_when_unset(self):
        assert _resolve_afk_timeout_ms(None, slack_bridged=True) == "300000"

    @pytest.mark.parametrize("slack_bridged", [True, False])
    def test_explicit_value_is_preserved(self, slack_bridged):
        assert _resolve_afk_timeout_ms("120000", slack_bridged=slack_bridged) == "120000"

    def test_plain_terminal_stays_untouched_when_unset(self):
        assert _resolve_afk_timeout_ms(None, slack_bridged=False) is None


class TestEffortFlag:
    """``lmer --effort <level>``, the flag spelling of LMER_REASONING_EFFORT."""

    @pytest.fixture(autouse=True)
    def _no_ambient_effort(self, monkeypatch):
        """Unset the variable so monkeypatch restores what the code under test
        writes (``delenv`` on an absent variable records nothing to undo)."""
        monkeypatch.setenv(REASONING_EFFORT_ENV, "")
        monkeypatch.delenv(REASONING_EFFORT_ENV)

    def test_the_parser_takes_it(self):
        namespace, rest = parse_args(["develop", "https://x/y", "--effort", "high"])
        assert namespace.effort == "high"
        assert rest == []

    @pytest.mark.parametrize("level", EFFORT_LEVELS)
    def test_every_level_becomes_the_session_s_effort(self, level):
        assert _apply_effort_selection(level, {}) == level
        assert os.environ[REASONING_EFFORT_ENV] == level

    def test_it_beats_an_exported_value(self, monkeypatch):
        monkeypatch.setenv(REASONING_EFFORT_ENV, "low")
        assert _apply_effort_selection("xhigh", {}) == "xhigh"
        assert os.environ[REASONING_EFFORT_ENV] == "xhigh"

    def test_no_flag_leaves_the_environment_alone(self, monkeypatch):
        monkeypatch.setenv(REASONING_EFFORT_ENV, "low")
        assert _apply_effort_selection(None, {}) == "low"
        assert os.environ[REASONING_EFFORT_ENV] == "low"

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_value_is_not_a_value(self, monkeypatch, blank):
        monkeypatch.setenv(REASONING_EFFORT_ENV, "low")
        assert _apply_effort_selection(blank, {}) == "low"
        assert os.environ[REASONING_EFFORT_ENV] == "low"

    def test_nothing_anywhere_resolves_to_no_effort(self):
        assert _apply_effort_selection(None, {}) is None

    def test_the_case_is_normalised(self):
        assert _apply_effort_selection("  XHigh ", {}) == "xhigh"
        assert os.environ[REASONING_EFFORT_ENV] == "xhigh"

    @pytest.mark.parametrize("bad", ["turbo", "hi", "--high", "high,low"])
    def test_an_unknown_level_is_refused(self, monkeypatch, bad):
        monkeypatch.setenv(REASONING_EFFORT_ENV, "low")
        with pytest.raises(ValueError, match="not a reasoning effort level"):
            _apply_effort_selection(bad, {})
        assert os.environ[REASONING_EFFORT_ENV] == "low"

    def test_the_show_env_table_attributes_it_to_the_flag(self):
        sources: dict = {}
        _apply_effort_selection("high", sources)
        assert sources[REASONING_EFFORT_ENV] == "--effort flag"


class TestEffortFlagThroughMain:
    """The real ``main()`` with the container runtime mocked out."""

    @pytest.fixture
    def presets_file(self, tmp_path):
        path = tmp_path / "presets.json"
        path.write_text(json.dumps({
            "deep": {"env": {REASONING_EFFORT_ENV: "max"}},
            "deep-args": {"args": ["--effort", "max"]},
        }))
        return path

    def test_it_beats_a_preset_s_env_and_an_export(self, presets_file, tmp_path):
        captured: dict = {}
        rc = _run_main(
            ["--preset", "deep", "--effort", "Medium", *_EXEC_ARGS],
            env_in={"LMER_PRESETS_FILE": str(presets_file), REASONING_EFFORT_ENV: "low"},
            captured_env=captured,
            home=tmp_path,
        )
        assert rc == 0
        assert captured[REASONING_EFFORT_ENV] == "medium"

    def test_without_the_flag_the_preset_s_env_still_applies(self, presets_file, tmp_path):
        captured: dict = {}
        rc = _run_main(
            ["--preset", "deep", *_EXEC_ARGS],
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            captured_env=captured,
            home=tmp_path,
        )
        assert rc == 0
        assert captured[REASONING_EFFORT_ENV] == "max"

    def test_the_command_line_beats_a_preset_s_args(self, presets_file, tmp_path):
        captured: dict = {}
        rc = _run_main(
            ["--preset", "deep-args", "--effort", "low", *_EXEC_ARGS],
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            captured_env=captured,
            home=tmp_path,
        )
        assert rc == 0
        assert captured[REASONING_EFFORT_ENV] == "low"

    def test_an_unknown_level_fails_the_launch(self, tmp_path, capsys):
        captured: dict = {}
        rc = _run_main(
            ["--effort", "turbo", *_EXEC_ARGS], captured_env=captured, home=tmp_path,
        )
        assert rc == 2
        assert captured == {}, "a refused launch must not build a container env"
        output = capsys.readouterr()
        assert "turbo" in output.out + output.err

    def test_show_env_names_the_flag(self, tmp_path, capsys):
        rc = _run_main(
            ["--show-env", "--effort", "high", *_EXEC_ARGS],
            env_in={REASONING_EFFORT_ENV: "low"},
            home=tmp_path,
        )
        assert rc == 0
        output = capsys.readouterr()
        row = next(
            line for line in (output.out + output.err).splitlines()
            if REASONING_EFFORT_ENV in line
        )
        assert "high" in row and "--effort flag" in row


def _run_claude_runner(tmp_path, env_value=None):
    env = {} if env_value is None else {"LMER_REASONING_EFFORT": env_value}
    result = run_claude_runner(tmp_path, env)
    return result.output, result.argv


@skip_if_npm_claude_present
class TestClaudeRunnerEffortFlag:
    """Verify claude-runner.sh translates LMER_REASONING_EFFORT → --effort."""

    @pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
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
        assert "low|medium|high|xhigh|max|auto" in output

    def test_empty_string_does_not_pass_flag(self, tmp_path):
        output, argv = _run_claude_runner(tmp_path, env_value="")

        assert "--effort" not in argv
