"""Service checkout policy through the supported instruction renderers."""

from pathlib import Path
import shutil

import pytest

from hooks import followup, start
from tests.conftest import strip_lmer_env


ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    strip_lmer_env(monkeypatch)
    monkeypatch.setenv("LMER_TASKDEF_ROOT", str(ROOT / "taskdef"))
    monkeypatch.setattr(start, "builtin_taskdef_root", lambda: ROOT / "taskdef")
    monkeypatch.setattr(start, "run_state_session_start", lambda: "")


@pytest.mark.parametrize("group", [False, True])
def test_service_policy_precedes_branch_setup_and_preserves_existing_work(
    group, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LMER_SERVICE_MODE", "1")
    monkeypatch.setenv("LMER_SERVICE_NAME", "web")
    if group:
        monkeypatch.setenv("LMER_SERVICE_GROUP", "dev")
        monkeypatch.delenv("LMER_SERVICE_NAME")
    task = tmp_path / "taskdef" / "develop"
    task.mkdir(parents=True)
    template = task / "instructions.txt"
    template.write_text(
        "{% extends 'base-task.jinja2' %}\n"
    )
    existing = tmp_path / "existing.py"
    existing.write_text("# another task's unfinished work\n")
    monkeypatch.chdir(tmp_path)

    assert start.read_and_display_instructions(template)
    output = capsys.readouterr().out
    prose = " ".join(output.split())

    assert output.index("Before using the supplied checkout") < output.index("git checkout")
    assert "unexpected pre-existing changes" in prose
    assert "branch appears to belong to another issue" in prose
    assert "STOP and flag the service environment" in prose
    assert "Report the service or group, the branch, and the unexpected state" in prose
    assert "`lmer-signal ask`" in output
    assert "Preserve the environment and wait" in prose
    assert "creating or switching worktrees, switching branches, stashing, resetting" in prose
    assert "This check takes precedence over later branch-setup instructions" in prose
    assert "Test runners stay unchanged" in prose
    assert "this run's own later task changes, known provisioned local instructions, and ignored files are normal" in prose
    assert "do not turn normal task progress into repeated stops" in prose
    assert "A clean, appropriate checkout can be used normally" in prose
    assert existing.read_text() == "# another task's unfinished work\n"


def test_non_service_render_does_not_impose_service_checkout_policy(tmp_path):
    task = tmp_path / "taskdef" / "develop"
    task.mkdir(parents=True)
    template = task / "instructions.txt"
    template.write_text("{% extends 'base-task.jinja2' %}")

    output = start.render_taskdef_template(template)

    assert "Before using the supplied checkout" not in output
    assert "STOP and flag the service environment" not in output
    assert "git checkout -b" in output


@pytest.mark.parametrize("task_name", ["develop", "followup"])
@pytest.mark.parametrize("mode", ["direct", "group", "non-service"])
def test_real_plain_followup_entries_resolve_and_render_lightweight_policy(
    task_name, mode, tmp_path, monkeypatch, capsys
):
    work_repo = tmp_path / "work"
    task = work_repo / "taskdef" / task_name
    shutil.copytree(ROOT / "tests/data/service-followup" / task_name, task)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work_repo))
    monkeypatch.setenv("LMER_TASK", task_name)
    if mode != "non-service":
        monkeypatch.setenv("LMER_SERVICE_MODE", "1")
        monkeypatch.setenv("LMER_SERVICE_NAME" if mode == "direct" else "LMER_SERVICE_GROUP", "web" if mode == "direct" else "dev")
    existing = tmp_path / "existing.py"
    existing.write_text("unfinished work\n")
    resolved = followup.find_followup_file()
    assert resolved == task / "followup.txt"
    assert followup.read_and_display_followup(resolved)
    output = capsys.readouterr().out
    prose = " ".join(output.split())
    assert "routine commits within authorized work" in prose
    assert "explicit user hold" in prose
    assert "Check if your MR has been reviewed since your last push" in output
    assert "Do not blindly accept the critique" in output
    assert "git checkout" not in output
    assert "Phase -1" not in output
    assert "Choosing which service you target" not in output
    if mode == "non-service":
        assert "Before using the supplied checkout" not in output
    else:
        assert output.index("Before using the supplied checkout") < output.index("apply fixes")
        assert "STOP and flag the service environment" in prose
        assert "Preserve the environment and wait" in prose
        assert "lmer-signal ask" in output
        assert "do not turn normal task progress into repeated stops" in prose
        assert "web" in output if mode == "direct" else "dev" in output
    assert existing.read_text() == "unfinished work\n"
