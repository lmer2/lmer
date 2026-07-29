#!/usr/bin/env python3
"""
Tests for bin/gate-push tag/ref/remote support.

The script must pass the target ref and remote through to the extended
allow-list authorization (GateSystem.run_push_gate), refuse an uncovered
ref before any network operation, and preserve the receipt contract:
exactly ONE emit_gate_event per invocation, emitted last (issue #88).
"""

import importlib.util
import os
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_gate_push_module():
    script_path = Path(__file__).parent.parent / "bin" / "gate-push"
    loader = SourceFileLoader("_gate_push_script", str(script_path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class TestGatePushTagRef:
    """Drive gate_push() with the real allow-list authorization; only the
    commit-gate checks and subprocess layer are mocked."""

    def setup_method(self):
        self.module = _load_gate_push_module()
        # Record every receipt so each test can assert the exactly-one rule.
        self.receipts = []

        def record_receipt(gate, outcome, **kwargs):
            self.receipts.append((gate, outcome, kwargs))

        self.module.emit_gate_event = record_receipt

    def _mock_git(self, remotes, branch="feature-x"):
        """side_effect for subprocess.run covering the script's git calls.

        Push invocations are recorded in self.push_calls so tests can
        assert the exact argv (or its absence).
        """
        self.push_calls = []

        def fake_run(command, **kwargs):
            if command[:3] == ["git", "remote", "get-url"]:
                # `--push` sits between the subcommand and the remote name.
                remote = command[-1]
                url = remotes.get(remote)
                if url is None:
                    return MagicMock(returncode=2, stdout="", stderr=f"error: No such remote '{remote}'")
                return MagicMock(returncode=0, stdout=url + "\n", stderr="")
            if command[:3] == ["git", "branch", "--show-current"]:
                return MagicMock(returncode=0, stdout=branch + "\n", stderr="")
            if command[:2] == ["git", "push"]:
                self.push_calls.append(command)
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        return fake_run

    @patch.dict(os.environ, {"LMER_PUSH_ALLOW_LIST": "user/other-repo|refs/tags/*"})
    @patch('lmer_cli.gates.GateSystem.run_commit_gate')
    @patch('subprocess.run')
    def test_authorized_tag_push_runs_expected_argv(self, mock_run, mock_commit_gate):
        """A tag covered by a `repo|refs/tags/*` entry pushes the fully
        qualified tag ref to the requested remote."""
        mock_run.side_effect = self._mock_git(
            {"mirror": "https://github.com/user/other-repo.git"})
        mock_commit_gate.return_value = True

        rc = self.module.gate_push(tag="v0.2.0", remote="mirror")

        assert rc == 0
        assert self.push_calls == [["git", "push", "mirror", "refs/tags/v0.2.0"]]
        assert len(self.receipts) == 1
        gate_name, outcome, kwargs = self.receipts[0]
        assert gate_name == "gate-push"
        assert outcome == "pass"
        assert kwargs["exit_code"] == 0

    @patch.dict(os.environ, {"LMER_PUSH_ALLOW_LIST": "user/other-repo"})
    @patch('lmer_cli.gates.GateSystem.run_commit_gate')
    @patch('subprocess.run')
    def test_unauthorized_tag_refused_before_git_push(self, mock_run, mock_commit_gate):
        """A bare allow-list entry is branch-only: the tag push exits
        non-zero and `git push` is never invoked."""
        mock_run.side_effect = self._mock_git(
            {"origin": "git@github.com:user/other-repo.git"})
        mock_commit_gate.return_value = True

        rc = self.module.gate_push(tag="v0.2.0")

        assert rc != 0
        assert self.push_calls == []
        # The refusal happens before the commit-gate checks too.
        mock_commit_gate.assert_not_called()
        assert len(self.receipts) == 1
        gate_name, outcome, kwargs = self.receipts[0]
        assert gate_name == "gate-push"
        assert outcome == "fail"
        assert kwargs["exit_code"] == rc

    @patch.dict(os.environ, {"LMER_PUSH_ALLOW_LIST": "user/other-repo"})
    @patch('lmer_cli.gates.GateSystem.run_commit_gate')
    @patch('subprocess.run')
    def test_default_branch_push_preserved(self, mock_run, mock_commit_gate):
        """Without --tag/--ref, the historical behavior stands: current
        branch, upstream tracking, origin."""
        mock_run.side_effect = self._mock_git(
            {"origin": "git@github.com:user/other-repo.git"}, branch="feature-x")
        mock_commit_gate.return_value = True

        rc = self.module.gate_push()

        assert rc == 0
        assert self.push_calls == [["git", "push", "-u", "origin", "feature-x"]]
        assert len(self.receipts) == 1

    @patch.dict(os.environ, {"LMER_PUSH_ALLOW_LIST": "user/other-repo|refs/heads/main"})
    @patch('lmer_cli.gates.GateSystem.run_commit_gate')
    @patch('subprocess.run')
    def test_explicit_ref_pushed_to_named_remote(self, mock_run, mock_commit_gate):
        """--ref pushes the refspec verbatim to the named remote."""
        mock_run.side_effect = self._mock_git(
            {"mirror": "https://github.com/user/other-repo.git"})
        mock_commit_gate.return_value = True

        rc = self.module.gate_push(ref="refs/heads/main", remote="mirror")

        assert rc == 0
        assert self.push_calls == [["git", "push", "mirror", "refs/heads/main"]]
        assert len(self.receipts) == 1

    def test_tag_and_ref_flags_mutually_exclusive(self, capsys):
        """The CLI refuses --tag together with --ref."""
        import subprocess
        script_path = Path(__file__).parent.parent / "bin" / "gate-push"
        result = subprocess.run(
            [sys.executable, str(script_path), "--tag", "v1", "--ref", "refs/heads/main"],
            capture_output=True, text=True
        )
        assert result.returncode != 0
        assert "not allowed with" in result.stderr
