#!/usr/bin/env python3
"""Tests for the top-level lmer CLI argument parser."""

import re
import sys
from pathlib import Path

import pytest
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
        the entry must be flag-only, mirroring LMER_START_PROMPT."""
        source = CLI_PY.read_text()
        pattern = re.compile(
            r"""["']LMER_ANSWER["']\s*:\s*ns\.answer\s+if\s+ns\.answer\s+else\s+None"""
        )
        assert pattern.search(source), "LMER_ANSWER entry missing from cli.py env dict"

    def test_host_env_answer_is_not_forwarded(self):
        """A host-set LMER_ANSWER (exported or layered in via a .env) must
        NEVER reach the container: answers are one-shot data, and an env
        fallback would silently auto-answer every future question-stop with
        stale text. Only the --answer flag sources the value, so the host
        CLI must have no os.environ read of LMER_ANSWER at all (the dict
        entry itself — None without the flag — blocks the .env merge)."""
        source = CLI_PY.read_text()
        pattern = re.compile(
            r"""os\.environ\.get\(\s*["']LMER_ANSWER["']"""
        )
        assert not pattern.search(source), (
            "LMER_ANSWER must be flag-only in the host CLI — no os.environ fallback"
        )

    def test_answer_help_text_mentions_LMER_ANSWER(self, capsys):
        with patch.object(sys, "argv", ["lmer", "--help"]):
            try:
                parse_args(["--help"])
            except SystemExit:
                pass
        help_text = capsys.readouterr().out
        assert "--answer" in help_text
        assert "LMER_ANSWER" in help_text

    def test_answer_help_does_not_claim_env_seeding(self, capsys):
        """Review on !154: the help text must not carry the `(env: VAR)`
        convention this file uses everywhere else to mean "the env var seeds
        the flag" — for --answer it deliberately does not (see
        test_host_env_answer_is_not_forwarded). LMER_ANSWER may only be
        described as the container-side export."""
        with patch.object(sys, "argv", ["lmer", "--help"]):
            try:
                parse_args(["--help"])
            except SystemExit:
                pass
        help_text = capsys.readouterr().out
        assert "(env: LMER_ANSWER" not in help_text


# --- the quiet parser seam (issue #245, review iteration 3) -------------------

def test_quiet_parse_raises_instead_of_printing():
    """For callers that parse on a server path.

    ``lmer_platform.slots`` asks "would these preset args rebind the slot?" on
    every fleet-view poll. argparse's default is to print usage to stderr and
    raise SystemExit; capturing that with ``contextlib.redirect_stderr`` swaps a
    process-global stream, which two overlapping threadpool requests can leave
    permanently pointed at an in-memory buffer.
    """
    import sys

    from lmer_cli.cli import ParseRefused, parse_args

    before_out, before_err = sys.stdout, sys.stderr
    with pytest.raises(ParseRefused):
        parse_args(["--ports"], quiet=True)
    assert sys.stdout is before_out and sys.stderr is before_err


def test_quiet_parse_silences_help_too():
    """``-h`` reaches exit() through print_help, not through error()."""
    from lmer_cli.cli import ParseRefused, parse_args

    with pytest.raises(ParseRefused):
        parse_args(["-h"], quiet=True)


def test_quiet_parse_returns_normally_on_good_argv():
    from lmer_cli.cli import parse_args

    namespace, rest = parse_args(["--service", "web", "chat"], quiet=True)

    assert namespace.service == "web"
    assert namespace.task == "chat"
    assert rest == []


def test_the_command_line_still_exits_the_way_it_did(capsys):
    """The flag is off by default, so nothing about the CLI changes."""
    from lmer_cli.cli import parse_args

    with pytest.raises(SystemExit):
        parse_args(["--ports"])
    assert "usage:" in capsys.readouterr().err
