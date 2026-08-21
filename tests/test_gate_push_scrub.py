#!/usr/bin/env python3
"""
Push-gate output must never carry the credential of a tokenized remote URL.

The workspace origin is a clone URL shaped
``https://oauth2:<credential>@host/path.git``, so every push-gate line that
names a remote URL — the refusal listing and the grant listing — put a live
credential on stdout and into the agent transcript (#281). Authorization keeps
reading the RAW url; only the printed form is scrubbed, which these tests pin
from both sides.
"""

import importlib.util
import os
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lmer_cli.gates import GateSystem
from tests.conftest import strip_lmer_env

CRED_URL = ("https://oauth2:glpat-FAKEFAKEFAKEFAKEFAKE@"
            "git.example.com/group/project.git")
CRED_VALUE = "glpat-FAKEFAKEFAKEFAKEFAKE"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


def _fake_run_command(url, branch="feature-x"):
    """Stand in for GateSystem.run_command over run_push_gate's git calls.

    ``url`` of None makes ``git remote get-url`` fail, which is what drives
    run_push_gate onto its push-by-URL branch (``--remote <url>``).
    """
    def run_command(command, check=True):
        if command[:3] == ["git", "remote", "get-url"]:
            if url is None:
                return 128, "", "error: No such remote"
            return 0, url + "\n", ""
        if command[:3] == ["git", "branch", "--show-current"]:
            return 0, branch + "\n", ""
        return 0, "", ""

    return run_command


def _load_gate_push_module():
    """Import bin/gate-push as a module (it is a script, not a package)."""
    script_path = Path(__file__).parent.parent / "bin" / "gate-push"
    loader = SourceFileLoader("_gate_push_scrub_script", str(script_path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class TestPushGateOutputScrubbing:
    """Refusal and grant lines name the repository, never the credential."""

    def test_refusal_names_the_target_without_its_credential(self, capsys):
        gate = GateSystem()

        gate._report_push_refusal("origin", [CRED_URL], [CRED_URL],
                                  "refs/heads/main", [], [], None)

        out = capsys.readouterr().out
        assert "git.example.com/group/project" in out
        assert "<-- not allowed" in out
        assert CRED_VALUE not in out
        assert "oauth2" not in out
        assert "@git.example.com" not in out

    def test_refusal_example_entry_carries_no_credential(self, capsys):
        """The copy-pasteable suggestion is derived from the same tokenized
        URL, so pin that push_allow.split_target strips the userinfo."""
        gate = GateSystem()

        gate._report_push_refusal("origin", [CRED_URL], [CRED_URL],
                                  "refs/heads/main", [], [], None)

        out = capsys.readouterr().out
        example = [line for line in out.splitlines()
                   if line.startswith("Example entry")]
        assert example == ["Example entry that would allow this push: "
                           "git.example.com/group/project"]

    def test_grant_scrubs_the_url_and_still_authorizes(self, monkeypatch,
                                                       capsys):
        monkeypatch.setenv("LMER_PUSH_ALLOW_LIST",
                           "git.example.com/group/project")
        gate = GateSystem()
        monkeypatch.setattr(gate, "run_command", _fake_run_command(CRED_URL))
        monkeypatch.setattr(gate, "run_commit_gate", lambda: True)

        # Authorization runs on the raw (tokenized) URL: a green gate here is
        # what proves the scrub did not reach the matching path.
        assert gate.run_push_gate() is True

        out = capsys.readouterr().out
        assert "✅ Push target allowed" in out
        assert "git.example.com/group/project" in out
        assert "[refs/heads/feature-x]" in out
        assert CRED_VALUE not in out
        assert "oauth2" not in out
        assert "@git.example.com" not in out


class TestPushByUrlOutputScrubbing:
    """`gate-push --remote <url>`: the REMOTE is the tokenized URL too.

    On this branch the string named in every parenthetical is whatever
    ``--remote`` carried, so scrubbing only the push URL left the credential
    on stdout in the very mode an agent reaches for when origin is not the
    push target.
    """

    def test_refusal_scrubs_the_remote_as_well_as_the_url(self, capsys):
        gate = GateSystem()

        gate._report_push_refusal(CRED_URL, [CRED_URL], [CRED_URL],
                                  "refs/heads/main", [], [], None)

        out = capsys.readouterr().out
        assert "git.example.com/group/project" in out
        assert "<-- not allowed" in out
        assert CRED_VALUE not in out
        assert "oauth2" not in out
        assert "@git.example.com" not in out

    def test_grant_scrubs_the_remote_and_still_authorizes(self, monkeypatch,
                                                          capsys):
        # An anchored host/path entry: push-by-URL refuses path-only grants.
        monkeypatch.setenv("LMER_PUSH_ALLOW_LIST",
                           "git.example.com/group/project")
        gate = GateSystem()
        # `git remote get-url` fails, so the URL passed as --remote is what
        # gets gated (by_url=True).
        monkeypatch.setattr(gate, "run_command", _fake_run_command(None))
        monkeypatch.setattr(gate, "run_commit_gate", lambda: True)

        assert gate.run_push_gate(remote=CRED_URL) is True

        out = capsys.readouterr().out
        assert "✅ Push target allowed" in out
        assert "git.example.com/group/project" in out
        assert CRED_VALUE not in out
        assert "oauth2" not in out
        assert "@git.example.com" not in out

    def test_refusal_through_the_gate_carries_no_credential(self, monkeypatch,
                                                            capsys):
        """The unallowlisted push-by-URL case: every printed line is clean."""
        gate = GateSystem()
        monkeypatch.setattr(gate, "run_command", _fake_run_command(None))

        assert gate.run_push_gate(remote=CRED_URL) is False

        out = capsys.readouterr().out
        assert "❌ Push not allowed to this repository" in out
        assert CRED_VALUE not in out
        assert "oauth2" not in out
        assert "@git.example.com" not in out


class TestGatePushScriptOutputScrubbing:
    """bin/gate-push announces the push target it is about to dial."""

    def test_pushing_ref_line_scrubs_the_remote(self, monkeypatch, capsys):
        module = _load_gate_push_module()
        monkeypatch.setattr(module, "emit_gate_event",
                            lambda gate, outcome, **kwargs: None)
        monkeypatch.setenv("LMER_PUSH_ALLOW_LIST",
                           "git.example.com/group/project|refs/tags/*")
        pushes = []

        def fake_run(command, **kwargs):
            if command[:3] == ["git", "remote", "get-url"]:
                # No such remote: the push-by-URL branch of the gate.
                return MagicMock(returncode=2, stdout="", stderr="error")
            if command[:2] == ["git", "push"]:
                pushes.append(command)
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr("lmer_cli.gates.GateSystem.run_commit_gate",
                            lambda self: True)

        assert module.gate_push(tag="v0.2.0", remote=CRED_URL) == 0

        # git still dials the credentialed URL; only the announcement is
        # scrubbed.
        assert pushes == [["git", "push", CRED_URL, "refs/tags/v0.2.0"]]
        out = capsys.readouterr().out
        assert "Pushing ref: refs/tags/v0.2.0 -> " in out
        assert "git.example.com/group/project" in out
        assert CRED_VALUE not in out
        assert "oauth2" not in out
        assert "@git.example.com" not in out
