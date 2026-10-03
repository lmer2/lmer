"""``lmer-ask`` is a one-release shim over ``lmer-signal`` (issue #407).

The file-protocol tests that lived here tested a CLI that no longer exists:
``lmer-ask`` forwards every verb to ``lmer-signal`` (``tests/test_lmer_signal_cli.py``
pins those), and what is pinned here is the forwarding itself — same stdout,
same exit codes, one deprecation line on stderr, the old ``--dir`` flag
tolerated.
"""
from __future__ import annotations

import pytest

from ask_channel import args as ask_args
from ask_channel import bus, cli as ask_cli, signal_cli
from tests.conftest import strip_lmer_env
from tests.test_lmer_signal_cli import Rig


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


# ---------------------------------------------------------------------------


class TestAskShim:
    @pytest.mark.parametrize("argv, expected", [
        (["ask", "q?"], ["ask", "q?"]),
        (["--dir", "/x", "note", "n"], ["note", "n"]),
        (["--dir=/x", "list", "--open"], ["list", "--open"]),
        ([], []),
    ])
    def test_the_old_dir_flag_is_dropped(self, argv, expected):
        assert ask_cli.translate_argv(argv) == expected

    def test_forwards_with_a_deprecation_line_on_stderr_only(self, rig, capsys):
        assert ask_cli.main(["note", "still works"]) == 0
        captured = capsys.readouterr()
        assert captured.out.strip() == "000001"
        assert "deprecated" in captured.err and "lmer-signal" in captured.err
        (record,) = rig.records()
        assert record.kind == "note"

    def test_exit_codes_pass_through(self, rig, capsys, monkeypatch):
        assert ask_cli.main(["ask", "q?", "--timeout", "0.2", "--interval", "0.1"]) == ask_args.TIMEOUT_EXIT_CODE
        assert ask_cli.main(["wait", "9"]) == 1
        monkeypatch.delenv(bus.PORT_ENV, raising=False)
        monkeypatch.setattr(signal_cli, "_client", lambda: bus.BusClient(bus.resolve_endpoint()))
        assert ask_cli.main(["note", "n"]) == ask_args.NO_CHANNEL_EXIT_CODE

    def test_verbs_lmer_ask_never_had_are_refused(self, capsys):
        assert ask_cli.main(["post", "x"]) == 1
        assert ask_cli.main([]) == 1
        assert "usage" in capsys.readouterr().err

    def test_the_old_helper_names_still_resolve(self):
        assert ask_cli.TIMEOUT_EXIT_CODE == ask_args.TIMEOUT_EXIT_CODE
        assert ask_cli.NO_CHANNEL_EXIT_CODE == ask_args.NO_CHANNEL_EXIT_CODE
        assert callable(ask_cli._add_text_source) and callable(ask_cli._resolve_text)
