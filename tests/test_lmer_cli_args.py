#!/usr/bin/env python3
"""Tests for the top-level lmer CLI argument parser."""

import re
import sys
from pathlib import Path
from unittest.mock import patch

from lmer_cli.cli import parse_args

CLI_PY = Path(__file__).parent.parent / "src" / "lmer_cli" / "cli.py"


class TestVerboseDebugAlias:
    """--debug must be a discoverable alias for --verbose; both flip LMER_VERBOSE."""

    def test_debug_flag_parses(self):
        ns, _ = parse_args(["chat", "https://example.com/x/y", "--debug"])
        assert ns.debug is True
        assert ns.verbose is False

    def test_verbose_flag_still_parses(self):
        ns, _ = parse_args(["chat", "https://example.com/x/y", "--verbose"])
        assert ns.verbose is True
        assert ns.debug is False

    def test_neither_flag_means_both_false(self):
        ns, _ = parse_args(["chat", "https://example.com/x/y"])
        assert ns.verbose is False
        assert ns.debug is False

    def test_both_flags_together(self):
        ns, _ = parse_args(["chat", "https://example.com/x/y", "--verbose", "--debug"])
        assert ns.verbose is True
        assert ns.debug is True

    def test_debug_help_text_mentions_LMER_VERBOSE(self, capsys):
        """The help string must point at the env var so the alias is documented."""
        with patch.object(sys, "argv", ["lmer", "--help"]):
            try:
                parse_args(["--help"])
            except SystemExit:
                pass
        help_text = capsys.readouterr().out
        assert "--debug" in help_text
        assert "LMER_VERBOSE" in help_text


class TestAnswerFlag:
    """--answer → LMER_ANSWER passthrough (issue #98 resume-on-answer)."""

    def test_answer_flag_parses(self):
        ns, _ = parse_args(
            ["develop", "https://example.com/x/y", "--answer", "postgres"]
        )
        assert ns.answer == "postgres"

    def test_answer_defaults_to_none(self):
        ns, _ = parse_args(["develop", "https://example.com/x/y"])
        assert ns.answer is None

    def test_cli_env_dict_declares_answer(self):
        """Guard against accidental removal of LMER_ANSWER from cli.py's env
        dict (source-level check, same pattern as
        test_lmer_reasoning_effort.py::test_cli_env_dict_declares_reasoning_effort):
        the flag wins, a host-set LMER_ANSWER still passes through."""
        source = CLI_PY.read_text()
        pattern = re.compile(
            r"""["']LMER_ANSWER["']\s*:\s*ns\.answer\s+or\s+os\.environ\.get\(\s*["']LMER_ANSWER["']\s*\)"""
        )
        assert pattern.search(source), "LMER_ANSWER entry missing from cli.py env dict"

    def test_answer_help_text_mentions_LMER_ANSWER(self, capsys):
        with patch.object(sys, "argv", ["lmer", "--help"]):
            try:
                parse_args(["--help"])
            except SystemExit:
                pass
        help_text = capsys.readouterr().out
        assert "--answer" in help_text
        assert "LMER_ANSWER" in help_text
