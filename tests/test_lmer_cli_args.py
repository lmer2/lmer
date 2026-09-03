#!/usr/bin/env python3
"""Tests for the top-level lmer CLI argument parser."""

import re
import sys
from pathlib import Path

import pytest
from unittest.mock import patch

from lmer_cli.cli import _validate_parsed_args, parse_args

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


class TestSessionFlag:
    """--session → LMER_SESSION_ID passthrough (issue #353 D22 resume).

    The session is the unit of record, so this one variable decides which
    directory the whole container writes in. Three properties are pinned
    here; the loop they add up to is `tests/test_session_resume_loop.py`.
    """

    def test_session_flag_parses(self):
        ns, _ = parse_args(
            ["develop", "https://example.com/x/y", "--session", "20260824-113000-1-2"]
        )
        assert ns.session == "20260824-113000-1-2"

    def test_session_defaults_to_none(self):
        ns, _ = parse_args(["develop", "https://example.com/x/y"])
        assert ns.session is None

    def test_cli_env_dict_declares_session(self):
        """Source-level guard against the entry being dropped or given a
        fallback (same pattern as test_cli_env_dict_declares_answer): the
        value is the flag's, and the no-flag case is None — never ""."""
        source = CLI_PY.read_text()
        pattern = re.compile(
            r"""["']LMER_SESSION_ID["']\s*:\s*ns\.session\s+if\s+ns\.session\s+else\s+None"""
        )
        assert pattern.search(source), (
            "LMER_SESSION_ID entry missing from cli.py env dict"
        )

    def test_host_env_session_id_is_not_forwarded(self):
        """The strongest of the flag-only cases. LMER_SESSION_ID is exported
        inside every lmer container (libexec/harness-common.sh), so an agent
        that runs `lmer` to spawn a child session HAS one in its own
        environment; an os.environ fallback would route the child into its
        parent's record and put two containers on one state file."""
        source = CLI_PY.read_text()
        pattern = re.compile(r"""os\.environ\.get\(\s*["']LMER_SESSION_ID["']""")
        assert not pattern.search(source), (
            "LMER_SESSION_ID must be flag-only in the host CLI — no os.environ "
            "fallback"
        )

    def test_session_help_text_mentions_LMER_SESSION_ID(self, capsys):
        with patch.object(sys, "argv", ["lmer", "--help"]):
            try:
                parse_args(["--help"])
            except SystemExit:
                pass
        help_text = capsys.readouterr().out
        assert "--session" in help_text
        assert "LMER_SESSION_ID" in help_text

    def test_session_help_does_not_claim_env_seeding(self, capsys):
        """No `(env: VAR)` — that convention means "the env var seeds the
        flag", which is exactly what must not happen here."""
        with patch.object(sys, "argv", ["lmer", "--help"]):
            try:
                parse_args(["--help"])
            except SystemExit:
                pass
        assert "(env: LMER_SESSION_ID" not in capsys.readouterr().out


class TestSessionIdIsRefusedWhereTheOperatorCanSeeIt:
    """A session id becomes a directory name in the work repo, so the host
    CLI refuses one that cannot be. Everything below this point degrades
    quietly instead: the container-side guards read an unusable id as NO
    launch id, so the session would start, open no record at all, and look
    entirely normal until an operator tried to resume it."""

    @staticmethod
    def _args(*extra):
        ns, _ = parse_args(["develop", "https://example.com/x/y", *extra])
        return ns

    @pytest.mark.parametrize(
        "session",
        [
            pytest.param("", id="empty"),
            pytest.param("   ", id="whitespace"),
            pytest.param(" 20260824-113000-1-2 ", id="padded"),
            pytest.param(".", id="dot"),
            pytest.param("..", id="traversal"),
            pytest.param("../elsewhere", id="separator"),
            pytest.param("a\\b", id="backslash"),
            pytest.param("2026*", id="pattern"),
        ],
    )
    def test_an_unusable_id_exits_two(self, session):
        assert _validate_parsed_args(self._args("--session", session)) == 2

    def test_the_refusal_names_what_a_session_id_is(self, capsys):
        _validate_parsed_args(self._args("--session", "runs/../etc"))
        out = capsys.readouterr().out
        assert "--session" in out
        assert "ONE launch" in out
        assert "_runs/<slug>/<session-id>" in out

    @pytest.mark.parametrize(
        "session",
        [
            pytest.param("20260824-113000-1-22222", id="minted-form"),
            pytest.param("20260824-113000-1-22222--second-look", id="named-dir"),
            pytest.param("a session someone named", id="unconstrained-name-half"),
        ],
    )
    def test_a_usable_id_passes(self, session):
        """No grammar beyond "it can be a directory name": the `--<name>`
        half of a session dir is unconstrained (docs/SCHEMA.md), and a dir
        named by an out-of-form launcher id is on disk right now."""
        assert _validate_parsed_args(self._args("--session", session)) is None

    def test_no_session_flag_is_not_a_refusal(self):
        assert _validate_parsed_args(self._args()) is None

    def test_surrounding_whitespace_is_the_one_extra_refusal(self):
        """The single deliberate difference from the container-side rule, and
        it only ever refuses MORE: a padded id would become a directory name
        with spaces in it, and stripping it silently would send the launch to
        a directory the operator did not type. Nothing downstream can make
        that call — by the time the kernel sees the value it is already the
        name it will resolve."""
        from lmer_cli.cli import _session_id_refusal
        from work_repo import run_state

        padded = " 20260824-113000-1-22222 "
        assert _session_id_refusal(padded) is not None
        assert run_state._usable_session_id(padded) is True

    def test_the_rule_is_otherwise_the_one_the_container_applies(self):
        """The host refusal and the container-side guards must agree, or the
        CLI refuses ids that work (or accepts ids that silently open no
        record). Both container copies are checked: the work CLI's kernel and
        the entrypoint's standalone mirror of it."""
        from lmer_cli.cli import _session_id_refusal
        from lmer_cli.container import clone_and_exec
        from work_repo import run_state

        for session in (
            "20260824-113000-1-22222",
            "20260824-113000-1-22222--second-look",
            "a session someone named",
            "",
            ".",
            "..",
            "a/b",
            "a\\b",
            "2026*",
            "run?",
            "[abc]",
            # Dot-leading: the only shape in this list that used to be
            # ACCEPTED by all three and open a record no walk over the group
            # would ever list (!268 review). Refusing it in one place would
            # have been a host-only asymmetry; the defect was in all three.
            ".hidden",
            ".new-20260824-113000-1-22222-abc",
        ):
            host_ok = _session_id_refusal(session) is None
            assert host_ok == run_state._usable_session_id(session), session
            assert host_ok == clone_and_exec._usable_session_id(session), session


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
