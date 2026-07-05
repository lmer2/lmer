"""Tests for the masterplan session-provisioning wiring (T2).

Covers three seams:
  * toggle parsing + run-dir computation in lmer_cli.container.masterplan
    (get_bool_env gating, LMER_TASK=masterplan implies it, bundle root nests
    under the run dir),
  * the cli.py container-env passthrough source guard (LMER_MASTERPLAN must
    stay in the dict so the toggle reaches inside the container),
  * claude-runner.sh gating — the plugin provisioning fires only for a
    masterplan session and stays silent otherwise.
"""
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from lmer_cli.container import masterplan

CLI_PY = Path(__file__).parent.parent / "src" / "lmer_cli" / "cli.py"
CLAUDE_RUNNER = Path(__file__).parent.parent / "libexec" / "claude-runner.sh"


# ── Toggle parsing (get_bool_env gating) ──────────────────────────────


@pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE", "Yes"])
def test_masterplan_enabled_truthy(monkeypatch, value):
    monkeypatch.delenv("LMER_TASK", raising=False)
    monkeypatch.setenv("LMER_MASTERPLAN", value)
    assert masterplan.masterplan_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "FALSE", ""])
def test_masterplan_disabled_falsy(monkeypatch, value):
    monkeypatch.delenv("LMER_TASK", raising=False)
    monkeypatch.setenv("LMER_MASTERPLAN", value)
    assert masterplan.masterplan_enabled() is False


def test_masterplan_disabled_when_unset(monkeypatch):
    monkeypatch.delenv("LMER_TASK", raising=False)
    monkeypatch.delenv("LMER_MASTERPLAN", raising=False)
    assert masterplan.masterplan_enabled() is False


def test_masterplan_task_implies_enabled(monkeypatch):
    """LMER_TASK=masterplan turns it on even without the toggle."""
    monkeypatch.setenv("LMER_TASK", "masterplan")
    monkeypatch.delenv("LMER_MASTERPLAN", raising=False)
    assert masterplan.masterplan_enabled() is True


def test_other_task_does_not_imply_enabled(monkeypatch):
    monkeypatch.setenv("LMER_TASK", "develop")
    monkeypatch.delenv("LMER_MASTERPLAN", raising=False)
    assert masterplan.masterplan_enabled() is False


# ── Run-dir computation (bundle root nests under the run dir) ──────────


def test_masterplan_runs_dir_nests_under_run_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
    monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
    monkeypatch.setenv("LMER_REPO_PROJECT", "group/proj")
    monkeypatch.setenv("LMER_TASK", "masterplan")
    monkeypatch.delenv("LMER_TASK_TARGET", raising=False)

    rdir = masterplan.masterplan_runs_dir()
    assert rdir is not None
    # <work>/<host>/<project>/runs/<slug>/masterplan
    assert rdir == tmp_path / "git.example.com" / "group/proj" / "runs" / "masterplan" / "masterplan"
    assert rdir.name == "masterplan"
    assert rdir.parent.parent.name == "runs"


def test_masterplan_runs_dir_none_without_repo_env(monkeypatch):
    monkeypatch.delenv("LMER_REPO_HOST", raising=False)
    monkeypatch.delenv("LMER_REPO_PROJECT", raising=False)
    assert masterplan.masterplan_runs_dir() is None


def test_main_prints_runs_dir_when_enabled(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
    monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
    monkeypatch.setenv("LMER_REPO_PROJECT", "proj")
    monkeypatch.setenv("LMER_MASTERPLAN", "1")
    monkeypatch.delenv("LMER_TASK", raising=False)
    # Clear the target too, so the run slug is "default" regardless of any
    # LMER_TASK_TARGET leaking in from the ambient session env.
    monkeypatch.delenv("LMER_TASK_TARGET", raising=False)

    rc = masterplan.main([])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out.endswith("/runs/default/masterplan")


def test_main_exits_nonzero_when_disabled(monkeypatch, capsys):
    monkeypatch.delenv("LMER_TASK", raising=False)
    monkeypatch.delenv("LMER_MASTERPLAN", raising=False)
    rc = masterplan.main([])
    assert rc == 1
    assert capsys.readouterr().out.strip() == ""


def test_main_exit_2_when_run_dir_indeterminate(monkeypatch, capsys):
    """Enabled but no resolvable run dir => exit 2 (distinct from 1), no output.

    Exit 2 lets claude-runner.sh warn that an explicit masterplan opt-in did
    nothing, instead of silently conflating it with a plain (exit 1) session.
    """
    monkeypatch.setenv("LMER_MASTERPLAN", "1")
    monkeypatch.delenv("LMER_TASK", raising=False)
    monkeypatch.delenv("LMER_REPO_HOST", raising=False)
    monkeypatch.delenv("LMER_REPO_PROJECT", raising=False)
    rc = masterplan.main([])
    assert rc == 2
    assert capsys.readouterr().out.strip() == ""


def test_main_exit_2_when_runs_dir_raises(monkeypatch, capsys):
    """Enabled + unexpected error resolving the run dir => exit 2, not 1.

    Exit 1 is the silent "plain session" path; an enabled-but-broken session
    (e.g. work_repo import failure) must still trip the caller's warning.
    """
    monkeypatch.setenv("LMER_MASTERPLAN", "1")
    monkeypatch.delenv("LMER_TASK", raising=False)
    monkeypatch.setattr(
        masterplan,
        "masterplan_runs_dir",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    rc = masterplan.main([])
    assert rc == 2
    assert capsys.readouterr().out.strip() == ""


# ── cli.py container-env passthrough source guard ─────────────────────


def test_cli_env_dict_declares_masterplan():
    """Guard: LMER_MASTERPLAN must be in cli.py's container env dict.

    Without this entry, setting LMER_MASTERPLAN=1 on the host has no effect
    because the var never reaches the container where claude-runner.sh reads it.
    """
    source = CLI_PY.read_text()
    pattern = re.compile(
        r"""["']LMER_MASTERPLAN["']\s*:\s*os\.environ\.get\(\s*["']LMER_MASTERPLAN["']\s*\)"""
    )
    assert pattern.search(source), "LMER_MASTERPLAN entry missing from cli.py container env dict"


# ── claude-runner.sh gating (provisioning fires only when active) ─────


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_runner_capturing_plugin_calls(tmp_path, env):
    """Run claude-runner.sh with a claude stub that records every invocation.

    The default harness stub overwrites its capture file each call, which loses
    the intermediate `claude plugin ...` calls before the final `exec claude`.
    This stub appends instead, so the plugin provisioning calls are observable.
    Returns the list of captured argv lines (one per claude invocation, joined).
    """
    calls_file = tmp_path / "claude_calls.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    fake_claude = fake_bin / "claude"
    fake_claude.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$*" >> "{calls_file}"\n'
        "exit 0\n"
    )
    _make_executable(fake_claude)

    # Expose the test interpreter as python3 so `python3 -m
    # lmer_cli.container.masterplan` resolves the package (editable install /
    # PYTHONPATH). A wrapper (not a symlink) preserves venv detection.
    python_wrapper = fake_bin / "python3"
    python_wrapper.write_text(f'#!/bin/bash\nexec {sys.executable} "$@"\n')
    _make_executable(python_wrapper)

    run_env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "PYTHONPATH": str(Path(__file__).parent.parent / "src"),
    }
    run_env.update(env)

    subprocess.run(
        ["bash", str(CLAUDE_RUNNER)],
        env=run_env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    if calls_file.exists():
        return [line for line in calls_file.read_text().splitlines() if line]
    return []


def test_runner_provisions_plugin_when_masterplan_enabled(tmp_path):
    work_repo = tmp_path / "work"
    work_repo.mkdir()
    calls = _run_runner_capturing_plugin_calls(
        tmp_path,
        {
            "LMER_MASTERPLAN": "1",
            "LMER_WORK_REPO_PATH": str(work_repo),
            "LMER_REPO_HOST": "git.example.com",
            "LMER_REPO_PROJECT": "proj",
        },
    )
    joined = "\n".join(calls)
    assert "plugin marketplace add /work/mirrors/masterplan" in joined
    assert "plugin install masterplan@rasatpetabit-masterplan" in joined
    assert "plugin enable masterplan" in joined


def test_runner_skips_plugin_when_masterplan_disabled(tmp_path):
    calls = _run_runner_capturing_plugin_calls(
        tmp_path,
        {
            "LMER_REPO_HOST": "git.example.com",
            "LMER_REPO_PROJECT": "proj",
        },
    )
    joined = "\n".join(calls)
    assert "plugin marketplace add" not in joined
    assert "plugin install masterplan" not in joined


# ── RO-symlink materialization (the second commit's live branch) ──────


def test_runner_materializes_readonly_symlink_settings(tmp_path):
    """A settings.json symlinked to a read-only mount is materialized to a
    writable regular file before the plugin calls run.

    Reproduces the runtime shape after the image-bake fix: with no baked
    settings.json, the runtime file starts as a symlink to the RO global
    settings. `claude plugin` would fail to persist enable-state through that
    symlink, so the masterplan block must cp it to a writable regular file
    first. LMER_SETTINGS_FILE points the runner at a tmp path we control.
    """
    work_repo = tmp_path / "work"
    work_repo.mkdir()

    # Read-only "global settings" the symlink points at (mode 0444).
    ro_source = tmp_path / "global_settings.json"
    ro_source.write_text('{"permissions": {"allow": ["Bash(git status:*)"]}}')
    ro_source.chmod(0o444)

    settings_file = tmp_path / "settings.json"
    settings_file.symlink_to(ro_source)
    assert settings_file.is_symlink()

    _run_runner_capturing_plugin_calls(
        tmp_path,
        {
            "LMER_MASTERPLAN": "1",
            "LMER_WORK_REPO_PATH": str(work_repo),
            "LMER_REPO_HOST": "git.example.com",
            "LMER_REPO_PROJECT": "proj",
            "LMER_SETTINGS_FILE": str(settings_file),
        },
    )

    # Now a regular file (not a symlink), writable, with the source content.
    assert not settings_file.is_symlink()
    assert settings_file.is_file()
    assert settings_file.stat().st_mode & stat.S_IWUSR, "materialized file must be writable"
    assert "Bash(git status:*)" in settings_file.read_text()


# ── Containerfile bake guard (critical: must not shadow global settings) ──


CONTAINERFILE = Path(__file__).parent.parent / "Containerfile"


def test_containerfile_removes_baked_settings_json():
    """Guard: the superpowers bake must remove ~/.claude/settings.json.

    `claude plugin marketplace add`/`install` persist state into
    ~/.claude/settings.json. If that regular file survives into the image, the
    runtime settings-link guards (`[ ! -e settings.json ]` in entrypoint.sh and
    claude-runner.sh) silently skip linking the global settings for EVERY
    session, dropping the permissions allowlist, hooks, and statusLine. The bake
    must end by removing it so the runtime symlink is created instead.
    """
    source = CONTAINERFILE.read_text()
    assert "claude plugin install superpowers@claude-plugins-official" in source
    assert re.search(
        r"rm\s+-f\s+/home/developer/\.claude/settings\.json", source
    ), "Containerfile must rm the baked ~/.claude/settings.json after the plugin bake"


def test_containerfile_does_not_leave_disable_all_as_persistence():
    """With settings.json removed, `plugin disable --all` is redundant.

    Leaving it would re-create the shadowing settings.json (enabledPlugins),
    reintroducing the bug the rm fixes.
    """
    source = CONTAINERFILE.read_text()
    assert "plugin disable --all" not in source
