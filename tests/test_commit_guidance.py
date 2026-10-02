"""Effective commit guidance and rule-hook success/failure behavior."""

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from hooks import followup, rgr, rgr_focused, rgrpc, start
from tests.conftest import strip_lmer_env


ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch, tmp_path):
    strip_lmer_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LMER_GLOBAL_DIR", str(ROOT))
    monkeypatch.setenv("LMER_TASKDEF_ROOT", str(ROOT / "taskdef"))
    monkeypatch.setattr(start, "builtin_taskdef_root", lambda: ROOT / "taskdef")
    monkeypatch.setattr(start, "run_state_session_start", lambda: "")


def assert_authorized_guidance(output):
    prose = " ".join(output.split())
    assert "routine commits within authorized work" in prose
    assert "explicit user hold" in prose
    assert "gate-check" in prose and "gate-commit" in prose
    assert "DO NOT commit without explicit user approval" not in prose
    assert "Waiting for explicit approval" not in prose
    assert "Awaiting user approval" not in prose


@pytest.mark.parametrize("reader", [start.read_and_display_instructions, followup.read_and_display_followup])
@pytest.mark.parametrize("schema", [1, 2])
def test_supported_instruction_readers_have_consistent_commit_guidance(
    reader, schema, tmp_path, capsys
):
    task = tmp_path / "taskdef" / "develop"
    task.mkdir(parents=True)
    (task.parent / "taskdef.yaml").write_text(f"schema: {schema}\n")
    template = task / "instructions.txt"
    template.write_text(
        "{% extends 'base-task.jinja2' %}" if schema == 2 else
        "# Development followup\n{% include 'commit-workflow.jinja2' %}"
    )
    assert reader(template)
    output = capsys.readouterr().out
    assert_authorized_guidance(output)
    for boundary in ("pushing", "history rewriting", "merging", "deploying", "destructive", "lifecycle"):
        assert boundary in output


@pytest.mark.parametrize("module", [None, "git"])
def test_rule_hook_reminds_about_gates_and_holds_without_generic_stop(
    module, monkeypatch, capsys
):
    monkeypatch.setattr(rgr.sys, "argv", ["rgr"] + ([module] if module else []))
    rgr.main()
    output = capsys.readouterr().out
    assert "authorized routine commits; respect explicit user holds" in output
    assert "NEVER commit without review and explicit approval" not in output
    assert "NEVER push without permission" in output


@pytest.mark.parametrize("tests_exit,precommit_exit", [(0, 0), (1, 0), (0, 1)])
def test_commit_hook_keeps_required_failure_enforcement(
    tests_exit, precommit_exit, monkeypatch, capsys
):
    def run(command, **kwargs):
        exit_code = tests_exit if command[0] == "python" else precommit_exit
        return subprocess.CompletedProcess(command, exit_code, "required check output", "failure details")

    monkeypatch.setattr(rgrpc.subprocess, "run", run)
    assert rgrpc.run_commit_gate() is (tests_exit == precommit_exit == 0)
    output = capsys.readouterr().out
    if tests_exit or precommit_exit:
        assert "COMMIT BLOCKED" in output
        assert "required check output" in output
    else:
        assert "No extra generic confirmation; explicit user holds remain binding" in output
    assert "Waiting for explicit approval" not in output


def test_commit_hook_completion_has_no_second_confirmation(monkeypatch, capsys):
    monkeypatch.setattr(rgrpc, "check_rules_were_read", lambda: True)
    monkeypatch.setattr(rgrpc, "get_git_status", lambda: None)
    monkeypatch.setattr(rgrpc, "run_commit_gate", lambda: True)
    rgrpc.main()
    output = capsys.readouterr().out
    assert "Use gate-commit within authorized scope; respect explicit user holds" in output
    assert "Awaiting user approval" not in output


def test_focused_rule_hook_renders_updated_rules(monkeypatch, tmp_path, capsys):
    paths = Mock(side_effect=lambda path: ROOT if str(path) in (
        "/home/developer/.lmer", "/Agents/global"
    ) else Path(path))
    paths.home.return_value = tmp_path
    monkeypatch.setattr(rgr_focused, "Path", paths)
    monkeypatch.setattr(rgr_focused.sys, "argv", ["rgr_focused", "rgr-git"])
    rgr_focused.main()
    output = capsys.readouterr().out
    assert "authorized routine commits; respect explicit user holds" in output
    assert "NEVER commit without review" not in output
    assert "NEVER push without permission" in output


def test_commit_hook_failure_exits_before_success_record(monkeypatch, tmp_path):
    monkeypatch.setattr(rgrpc, "check_rules_were_read", lambda: True)
    monkeypatch.setattr(rgrpc, "get_git_status", lambda: None)
    monkeypatch.setattr(rgrpc, "run_commit_gate", lambda: False)
    with pytest.raises(SystemExit) as exc:
        rgrpc.main()
    assert exc.value.code == 1
    assert not (tmp_path / ".claude" / "gate_passages.log").exists()
