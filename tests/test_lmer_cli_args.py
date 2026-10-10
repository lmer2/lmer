#!/usr/bin/env python3
"""Tests for the top-level lmer CLI argument parser."""

import os
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


class TestResourceLimitFlags:
    """``--memory`` / ``--cpus``: a launch-side override of the env ladder
    (issue #453). The platform's assistant launch passes them; they are not a
    spawn API field."""

    def test_both_flags_parse(self):
        ns, _ = parse_args(["develop", "https://example.com/x/y", "--memory", "4g", "--cpus", "2"])
        assert (ns.memory, ns.cpus) == ("4g", "2")

    def test_absent_flags_are_none_so_the_ladder_answers(self):
        ns, _ = parse_args(["develop", "https://example.com/x/y"])
        assert ns.memory is None and ns.cpus is None

    def test_an_invalid_memory_value_is_refused_not_defaulted(self, capsys):
        """The other posture from the environment tiers: an attached caller
        asked for something, and starting with a different bound is the
        failure, so this aborts instead of warning."""
        ns, _ = parse_args(["develop", "https://example.com/x/y", "--memory", "8"])
        assert _validate_parsed_args(ns) == 2
        assert "--memory" in capsys.readouterr().out

    def test_an_invalid_cpus_value_is_refused(self):
        ns, _ = parse_args(["develop", "https://example.com/x/y", "--cpus", "all"])
        assert _validate_parsed_args(ns) == 2

    def test_valid_values_pass_validation(self):
        ns, _ = parse_args(["develop", "https://example.com/x/y", "--memory", "512m", "--cpus", "0.5"])
        assert _validate_parsed_args(ns) is None

    def test_surrounding_whitespace_is_accepted_as_the_env_tiers_do(self):
        ns, _ = parse_args(["develop", "https://example.com/x/y", "--memory", "  4g  "])
        assert _validate_parsed_args(ns) is None

    def test_a_blank_value_is_no_override_rather_than_a_refusal(self):
        """A wrapper passing --memory "$OVERRIDE" with the variable unset has
        expressed no preference; the env tiers read blank as unset and so does
        this."""
        ns, _ = parse_args(["develop", "https://example.com/x/y", "--memory", ""])
        assert _validate_parsed_args(ns) is None
        assert ns.memory is None

    def test_an_accepted_value_is_stored_normalised(self):
        """The validators may normalise; the env path passes what the validator
        returned, so the flag path must too."""
        ns, _ = parse_args(["develop", "https://example.com/x/y", "--cpus", "  2  "])
        assert _validate_parsed_args(ns) is None
        assert ns.cpus == "2"

    def test_help_points_at_the_scoped_variables(self, capsys):
        """The flag is where an operator meets this feature; the help has to
        name the ladder it overrides."""
        with pytest.raises(SystemExit):
            parse_args(["--help"])
        out = capsys.readouterr().out
        assert "LMER_<TASK>_MEMORY" in out
        assert "LMER_<TASK>__<REPO>__MEMORY" in out


class TestResourceLimitDiagnostic:
    """The 📐 line is verbose-only, like the preset and harness lines beside
    it — asserted so the docs' claim about it stays checkable."""

    def _announce(self, verbose):
        import io
        import contextlib

        from lmer_cli.cli import _announce_resource_limits
        from lmer_cli.runtime import ResourceLimit

        env = {"LMER_VERBOSE": "1"} if verbose else {}
        plan = [ResourceLimit("--memory", "4g", "LMER_DEVELOP_MEMORY")]
        buffer = io.StringIO()
        with patch.dict(os.environ, env, clear=False):
            if not verbose:
                os.environ.pop("LMER_VERBOSE", None)
            with contextlib.redirect_stdout(buffer):
                _announce_resource_limits(plan)
        return buffer.getvalue()

    def test_verbose_names_the_value_and_its_source(self):
        out = self._announce(verbose=True)
        assert "memory=4g (LMER_DEVELOP_MEMORY)" in out

    def test_without_verbose_it_says_nothing(self):
        assert self._announce(verbose=False) == ""

    def test_an_empty_plan_prints_no_header(self):
        from lmer_cli.cli import _announce_resource_limits
        import io
        import contextlib

        buffer = io.StringIO()
        with patch.dict(os.environ, {"LMER_VERBOSE": "1"}):
            with contextlib.redirect_stdout(buffer):
                _announce_resource_limits([])
        assert buffer.getvalue() == ""

    def test_gpu_grant_is_shown_beside_the_limits(self):
        import contextlib
        import io

        from lmer_cli.cli import _announce_resource_limits
        from lmer_cli.runtime import GPUGrant, ResourceLimit

        buffer = io.StringIO()
        with patch.dict(os.environ, {"LMER_VERBOSE": "1"}):
            with contextlib.redirect_stdout(buffer):
                _announce_resource_limits(
                    [ResourceLimit("--cpus", "2", "LMER_CPUS")],
                    GPUGrant("0,1", "LMER_DEVELOP_GPU", ()),
                )
        out = buffer.getvalue()
        assert "cpus=2 (LMER_CPUS), gpu=0,1 (LMER_DEVELOP_GPU)" in out

    def test_unsupported_gpu_request_is_shown_as_no_grant(self):
        import contextlib
        import io

        from lmer_cli.cli import _announce_resource_limits
        from lmer_cli.runtime import GPUGrant

        buffer = io.StringIO()
        with patch.dict(os.environ, {"LMER_VERBOSE": "1"}):
            with contextlib.redirect_stdout(buffer):
                _announce_resource_limits([], GPUGrant(None, "LMER_GPU; unsupported"))
        assert "gpu=none (LMER_GPU; unsupported)" in buffer.getvalue()
class TestRootUsernsFlag:
    """--root-userns / LMER_ROOT_USERNS opt-in (issue #351)."""

    def test_flag_parses(self):
        ns, _ = parse_args(["chat", "https://example.com/x/y", "--root-userns"], quiet=True)
        assert ns.root_userns is True

    def test_defaults_to_off(self):
        ns, _ = parse_args(["chat", "https://example.com/x/y"], quiet=True)
        assert ns.root_userns is False

    def test_conflicts_with_user(self, capsys):
        from lmer_cli.cli import _validate_parsed_args

        ns, _ = parse_args(["chat", "https://example.com/x/y", "--root-userns", "--user", "developer"], quiet=True)
        assert _validate_parsed_args(ns) == 2
        assert "--root-userns" in capsys.readouterr().out

    def test_conflicts_with_match_uid(self, capsys):
        from lmer_cli.cli import _validate_parsed_args

        ns, _ = parse_args(["chat", "https://example.com/x/y", "--root-userns", "--match-uid"], quiet=True)
        assert _validate_parsed_args(ns) == 2
        assert "--root-userns" in capsys.readouterr().out

    def test_alone_passes_validation(self):
        from lmer_cli.cli import _validate_parsed_args

        ns, _ = parse_args(["chat", "https://example.com/x/y", "--root-userns"], quiet=True)
        assert _validate_parsed_args(ns) is None

    def test_env_dict_injects_is_sandbox_only_for_root_userns_optin(self):
        """Claude Code refuses --dangerously-skip-permissions as root;
        IS_SANDBOX=1 is its documented bypass. It must be asserted ONLY for
        the explicit --root-userns opt-in (whose rootless-podman sandbox
        premise is checked at launch) — never for an ad-hoc root --user, and
        never as a host passthrough (source-level guard, per the
        container-passthrough convention)."""
        source = CLI_PY.read_text()
        assert re.search(
            r'"IS_SANDBOX":\s*"1"\s*if\s*root_userns_active\s*else\s*None',
            source,
        ), "container env dict no longer scopes IS_SANDBOX to the --root-userns opt-in"

    def test_resolution_flag_selects_root_user(self):
        from lmer_cli.cli import _resolve_container_user

        ns, _ = parse_args(["chat", "https://example.com/x/y", "--root-userns"], quiet=True)
        assert _resolve_container_user(ns) == ("0:0", True)

    def test_resolution_env_selects_root_user(self, monkeypatch):
        from lmer_cli.cli import _resolve_container_user

        monkeypatch.setenv("LMER_ROOT_USERNS", "1")
        ns, _ = parse_args(["chat", "https://example.com/x/y"], quiet=True)
        assert _resolve_container_user(ns) == ("0:0", True)

    def test_resolution_explicit_user_beats_env(self, monkeypatch):
        from lmer_cli.cli import _resolve_container_user

        monkeypatch.setenv("LMER_ROOT_USERNS", "1")
        ns, _ = parse_args(["chat", "https://example.com/x/y", "--user", "developer"], quiet=True)
        assert _resolve_container_user(ns) == ("developer", False)

    def test_resolution_default_is_developer_and_not_root_userns(self, monkeypatch):
        from lmer_cli.cli import _resolve_container_user

        monkeypatch.delenv("LMER_ROOT_USERNS", raising=False)
        monkeypatch.delenv("LMER_CONTAINER_USER", raising=False)
        ns, _ = parse_args(["chat", "https://example.com/x/y"], quiet=True)
        assert _resolve_container_user(ns) == ("developer", False)

    def test_resolution_adhoc_root_user_is_not_the_optin(self):
        """--user 0:0 skips keep-id but must NOT claim the IS_SANDBOX opt-in."""
        from lmer_cli.cli import _resolve_container_user

        ns, _ = parse_args(["chat", "https://example.com/x/y", "--user", "0:0"], quiet=True)
        assert _resolve_container_user(ns) == ("0:0", False)


class TestRootUsernsRuntimeGate:
    """MR !249 review (cli.py:2240): the rootless-podman refusal is the
    security premise the IS_SANDBOX bypass relies on — pin the condition AND
    its position ahead of any image build."""

    def _run_main(self, runtime, euid, ensure, engine_rootless=True):
        import os

        from unittest.mock import patch as _patch

        from tests.test_lmer_cli_slack_target import (
            _BASE_ENV,
            _make_main_mocks,
            REPO_URL,
        )

        argv = ["chat", "--root-userns", "--no-task", "--exec", "true", REPO_URL]
        with _patch.dict(os.environ, _BASE_ENV, clear=True):
            with _make_main_mocks():
                with _patch("lmer_cli.cli.detect_runtime", return_value=runtime), \
                     _patch("lmer_cli.cli.os.geteuid", return_value=euid), \
                     _patch("lmer_cli.cli.engine_is_rootless", return_value=engine_rootless), \
                     _patch("lmer_cli.cli.ensure_image", ensure):
                    from lmer_cli.cli import main

                    return main(argv)

    def test_docker_runtime_refused_before_any_build(self):
        from unittest.mock import MagicMock

        ensure = MagicMock(
            side_effect=AssertionError(
                "ensure_image ran after a refused --root-userns"
            )
        )
        assert self._run_main("docker", 1000, ensure) == 2
        ensure.assert_not_called()

    def test_rootful_engine_refused_before_any_build(self):
        """A root-run podman reports `Host.Security.Rootless: false`. The gate
        asks the engine rather than reading euid, because a non-root client can
        also be pointed at a rootful *service* — !309 adversarial review F1."""
        from unittest.mock import MagicMock

        ensure = MagicMock(
            side_effect=AssertionError(
                "ensure_image ran after a refused --root-userns"
            )
        )
        assert self._run_main("podman", 0, ensure, engine_rootless=False) == 2
        ensure.assert_not_called()

    def test_a_non_root_client_on_a_rootful_engine_is_refused(self):
        """The case euid alone cannot see: local uid 1000, rootful engine."""
        from unittest.mock import MagicMock

        ensure = MagicMock(
            side_effect=AssertionError(
                "ensure_image ran after a refused --root-userns"
            )
        )
        assert self._run_main("podman", 1000, ensure, engine_rootless=False) == 2
        ensure.assert_not_called()

    def test_an_unanswerable_engine_is_refused_before_any_build(self):
        """`podman info` not answering leaves the premise unestablished, which
        must refuse — IS_SANDBOX=1 hangs off this branch."""
        from unittest.mock import MagicMock

        ensure = MagicMock(
            side_effect=AssertionError(
                "ensure_image ran after a refused --root-userns"
            )
        )
        assert self._run_main("podman", 1000, ensure, engine_rootless=None) == 2
        ensure.assert_not_called()

    def test_rootless_podman_accepted(self):
        """Positive control: the same harness passes the gate under rootless
        podman, so the refusals above are the gate and not a broken setup."""
        from unittest.mock import MagicMock

        from lmer_cli.build import ImageReadiness

        ensure = MagicMock(return_value=ImageReadiness(available=True))
        assert self._run_main("podman", 1000, ensure) == 0
        ensure.assert_called_once()
