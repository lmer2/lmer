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
MASTERPLAN_ENABLE = Path(__file__).parent.parent / "libexec" / "masterplan-enable.sh"
CONTAINERFILE = Path(__file__).parent.parent / "Containerfile"


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


def test_other_task_does_not_imply_enabled(monkeypatch, tmp_path):
    _isolate_taskdef_tiers(monkeypatch, tmp_path)
    monkeypatch.setenv("LMER_TASK", "develop")
    assert masterplan.masterplan_enabled() is False


# ── Taskdef-declared masterplan (task.yaml beside instructions.txt) ────
#
# A taskdef that requires the masterplan plugin (e.g. a work-repo `spec`
# taskdef) declares it in a per-task manifest instead of relying on the
# operator to remember LMER_MASTERPLAN=1 at launch. The manifest resolves
# through the same tier precedence as the taskdef's other files.


def _isolate_taskdef_tiers(monkeypatch, tmp_path):
    """Point the taskdef search at a tmp work repo, away from ambient tiers.

    Without this, masterplan_enabled()'s manifest lookup would consult the
    running session's real work repo via the inherited LMER_* env.
    """
    work = tmp_path / "work"
    (work / "taskdef").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))
    monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
    monkeypatch.setenv("LMER_REPO_PROJECT", "group/proj")
    for var in (
        "LMER_TASKDEF_PATHS",
        "LMER_TASKDEF_DIR",
        "LMER_TASK_INSTRUCTIONS",
        "LMER_MASTERPLAN",
        "LMER_TASKDEF",
        "LMER_TASK_TARGET",
    ):
        monkeypatch.delenv(var, raising=False)
    return work


def _write_task_manifest(work, body, task="spectask", tier="global"):
    """Create taskdef <task> with a task.yaml carrying *body* in a tier."""
    if tier == "global":
        tdir = work / "taskdef" / task
    else:
        tdir = work / "git.example.com" / "group/proj" / "taskdef" / task
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "instructions.txt").write_text("body\n")
    (tdir / "task.yaml").write_text(body)


def test_taskdef_manifest_declares_masterplan(monkeypatch, tmp_path):
    work = _isolate_taskdef_tiers(monkeypatch, tmp_path)
    _write_task_manifest(work, "masterplan: true\n")
    monkeypatch.setenv("LMER_TASK", "spectask")
    assert masterplan.masterplan_enabled() is True


@pytest.mark.parametrize("body", ["masterplan: false\n", "masterplan: no\n", 'masterplan: "0"\n'])
def test_taskdef_manifest_falsy_not_enabled(monkeypatch, tmp_path, body):
    work = _isolate_taskdef_tiers(monkeypatch, tmp_path)
    _write_task_manifest(work, body)
    monkeypatch.setenv("LMER_TASK", "spectask")
    assert masterplan.masterplan_enabled() is False


@pytest.mark.parametrize("body", ['masterplan: "yes"\n', 'masterplan: "1"\n', "masterplan: 1\n"])
def test_taskdef_manifest_truthy_variants(monkeypatch, tmp_path, body):
    work = _isolate_taskdef_tiers(monkeypatch, tmp_path)
    _write_task_manifest(work, body)
    monkeypatch.setenv("LMER_TASK", "spectask")
    assert masterplan.masterplan_enabled() is True


def test_taskdef_without_manifest_not_enabled(monkeypatch, tmp_path):
    work = _isolate_taskdef_tiers(monkeypatch, tmp_path)
    tdir = work / "taskdef" / "spectask"
    tdir.mkdir(parents=True)
    (tdir / "instructions.txt").write_text("body\n")
    monkeypatch.setenv("LMER_TASK", "spectask")
    assert masterplan.masterplan_enabled() is False


@pytest.mark.parametrize("body", ["{ not yaml\n", "- a\n- list\n", ""])
def test_taskdef_manifest_malformed_not_enabled(monkeypatch, tmp_path, body):
    """Unreadable/malformed/non-dict manifests count as "not declared".

    Provisioning is logged-never-fatal; a bad YAML file must not take the
    session down or (worse) silently flip masterplan on.
    """
    work = _isolate_taskdef_tiers(monkeypatch, tmp_path)
    _write_task_manifest(work, body)
    monkeypatch.setenv("LMER_TASK", "spectask")
    assert masterplan.masterplan_enabled() is False


def test_project_tier_manifest_wins_over_global(monkeypatch, tmp_path):
    """The manifest resolves like any other taskdef file: the project tier
    shadows the work-global tier, so a project can flip the flag either way."""
    work = _isolate_taskdef_tiers(monkeypatch, tmp_path)
    _write_task_manifest(work, "masterplan: true\n", tier="global")
    _write_task_manifest(work, "masterplan: false\n", tier="project")
    monkeypatch.setenv("LMER_TASK", "spectask")
    assert masterplan.masterplan_enabled() is False


def test_declaration_wins_over_falsy_toggle(monkeypatch, tmp_path):
    """LMER_MASTERPLAN=0 does not veto a taskdef declaration — same contract
    as LMER_TASK=masterplan, which the falsy toggle does not veto either: a
    taskdef whose instructions require masterplan stays provisioned."""
    work = _isolate_taskdef_tiers(monkeypatch, tmp_path)
    _write_task_manifest(work, "masterplan: true\n")
    monkeypatch.setenv("LMER_TASK", "spectask")
    monkeypatch.setenv("LMER_MASTERPLAN", "0")
    assert masterplan.masterplan_enabled() is True


def test_main_provisions_for_declaring_taskdef(monkeypatch, tmp_path, capsys):
    """End-to-end through main(): a declaring taskdef gets exit 0 and the
    bundle root — the exact shape the failed spec sessions needed."""
    work = _isolate_taskdef_tiers(monkeypatch, tmp_path)
    _write_task_manifest(work, "masterplan: true\n")
    monkeypatch.setenv("LMER_TASK", "spectask")
    rc = masterplan.main([])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out.endswith("/runs/spectask/masterplan")


def test_taskdef_search_mirrors_start_hook():
    """Guard: lmer_cli.container.taskdefs mirrors hooks/start.py's taskdef
    tier search. start.py deliberately does not import lmer_cli, so the
    search is mirrored rather than shared — keep the bodies in sync (same
    pattern as _is_github_host / lmer_cli.tokens)."""
    from hooks import start as start_hook

    from lmer_cli.container import taskdefs
    from tests.conftest import ast_body_lines

    for name in (
        "work_repo_taskdef_dirs",
        "builtin_taskdef_root",
        "taskdef_search_dirs",
        "find_taskdef_file",
    ):
        assert ast_body_lines(getattr(taskdefs, name)) == ast_body_lines(
            getattr(start_hook, name)
        ), f"{name} body diverged between lmer_cli.container.taskdefs and hooks/start.py"


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


# ── main() force mode (mid-session enablement, no launch gating) ───────


def test_main_force_bypasses_gating(monkeypatch, tmp_path, capsys):
    """--force resolves the bundle root even when nothing enables masterplan:
    the mid-session path has no LMER_TASK/LMER_MASTERPLAN/task.yaml signal."""
    _isolate_taskdef_tiers(monkeypatch, tmp_path)
    monkeypatch.delenv("LMER_TASK", raising=False)
    monkeypatch.delenv("LMER_MASTERPLAN", raising=False)
    rc = masterplan.main(["--force"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out.endswith("/runs/default/masterplan")


def test_main_force_repo_flags_supply_missing_target(monkeypatch, tmp_path, capsys):
    """--repo-host/--repo-project make resolution succeed in a session that
    was launched without a repo target (the ask-the-user path)."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))
    for var in ("LMER_REPO_HOST", "LMER_REPO_PROJECT", "LMER_TASK",
                "LMER_MASTERPLAN", "LMER_TASK_TARGET"):
        monkeypatch.delenv(var, raising=False)
    rc = masterplan.main([
        "--force", "--repo-host", "git.example.com", "--repo-project", "group/proj",
    ])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert "/git.example.com/group/proj/runs/" in out
    assert out.endswith("/masterplan")


def test_main_force_without_target_exits_2(monkeypatch, tmp_path, capsys):
    """--force with no host/project anywhere keeps the exit-2 contract:
    silent stdout, distinct code, so the caller can ask for a project."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))
    for var in ("LMER_REPO_HOST", "LMER_REPO_PROJECT", "LMER_TASK",
                "LMER_MASTERPLAN"):
        monkeypatch.delenv(var, raising=False)
    rc = masterplan.main(["--force"])
    assert rc == 2
    assert capsys.readouterr().out.strip() == ""


def test_main_flagless_behavior_unchanged(monkeypatch, tmp_path, capsys):
    """No flags → the launch-gating path is untouched (exit 1 when nothing
    enables masterplan)."""
    _isolate_taskdef_tiers(monkeypatch, tmp_path)
    monkeypatch.delenv("LMER_TASK", raising=False)
    monkeypatch.delenv("LMER_MASTERPLAN", raising=False)
    rc = masterplan.main([])
    assert rc == 1
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


def test_cli_env_dict_declares_mirror_candidates():
    """Guard: LMER_MASTERPLAN_MIRROR_CANDIDATES must be in cli.py's container
    env dict.

    The var is read inside the container (masterplan-enable.sh's mirror
    search); without this entry a host-shell export never reaches it and the
    knob only works via .env files.
    """
    source = CLI_PY.read_text()
    pattern = re.compile(
        r"""["']LMER_MASTERPLAN_MIRROR_CANDIDATES["']\s*:\s*"""
        r"""os\.environ\.get\(\s*["']LMER_MASTERPLAN_MIRROR_CANDIDATES["']\s*\)"""
    )
    assert pattern.search(source), (
        "LMER_MASTERPLAN_MIRROR_CANDIDATES entry missing from cli.py container env dict"
    )


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


def test_runner_provisions_plugin_when_taskdef_declares(tmp_path):
    """End-to-end through claude-runner.sh: a taskdef shipping
    `masterplan: true` in task.yaml provisions the plugin without
    LMER_TASK=masterplan or LMER_MASTERPLAN — the exact gap that left spec
    sessions without /masterplan and MASTERPLAN_RUNS_DIR."""
    work_repo = tmp_path / "work"
    tdir = work_repo / "taskdef" / "spectask"
    tdir.mkdir(parents=True)
    (tdir / "instructions.txt").write_text("body\n")
    (tdir / "task.yaml").write_text("masterplan: true\n")
    calls = _run_runner_capturing_plugin_calls(
        tmp_path,
        {
            "LMER_TASK": "spectask",
            "LMER_WORK_REPO_PATH": str(work_repo),
            "LMER_REPO_HOST": "git.example.com",
            "LMER_REPO_PROJECT": "proj",
        },
    )
    joined = "\n".join(calls)
    assert "plugin marketplace add /work/mirrors/masterplan" in joined
    assert "plugin install masterplan@rasatpetabit-masterplan" in joined
    assert "plugin enable masterplan" in joined


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


# ── masterplan-enable.sh (shared provisioning script) ──────────────────


def _run_enable_script(tmp_path, env, args=()):
    """Run masterplan-enable.sh with a fake `claude` that records calls.

    Returns (returncode, stdout, stderr, plugin_call_lines).
    """
    calls_file = tmp_path / "claude_calls.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)

    fake_claude = fake_bin / "claude"
    fake_claude.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$*" >> "{calls_file}"\n'
        "exit 0\n"
    )
    _make_executable(fake_claude)

    python_wrapper = fake_bin / "python3"
    python_wrapper.write_text(f'#!/bin/bash\nexec {sys.executable} "$@"\n')
    _make_executable(python_wrapper)

    run_env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "PYTHONPATH": str(Path(__file__).parent.parent / "src"),
    }
    run_env.update(env)

    proc = subprocess.run(
        ["bash", str(MASTERPLAN_ENABLE), *args],
        env=run_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    calls = []
    if calls_file.exists():
        calls = [line for line in calls_file.read_text().splitlines() if line]
    return proc.returncode, proc.stdout, proc.stderr, calls


def _forced_env(tmp_path):
    work = tmp_path / "work"
    (work / "mirrors" / "masterplan").mkdir(parents=True, exist_ok=True)
    return {
        "LMER_WORK_REPO_PATH": str(work),
        "LMER_REPO_HOST": "git.example.com",
        "LMER_REPO_PROJECT": "group/proj",
        "LMER_MASTERPLAN_MIRROR_CANDIDATES": str(work / "mirrors" / "masterplan"),
    }


def test_enable_forced_provisions_and_persists_env(tmp_path):
    rc, out, err, calls = _run_enable_script(tmp_path, _forced_env(tmp_path))
    assert rc == 0
    bundle = out.strip()
    assert bundle.endswith("/runs/default/masterplan")
    assert "/git.example.com/group/proj/" in bundle
    joined = "\n".join(calls)
    assert "plugin install masterplan@rasatpetabit-masterplan" in joined
    assert "plugin enable masterplan" in joined
    dropin = tmp_path / ".bashrc.d" / "masterplan-env.sh"
    assert dropin.exists()
    assert f'export MASTERPLAN_RUNS_DIR="{bundle}"' in dropin.read_text()
    assert "/reload-plugins" in err


def test_enable_forced_repo_flags_persisted(tmp_path):
    env = _forced_env(tmp_path)
    del env["LMER_REPO_HOST"]
    del env["LMER_REPO_PROJECT"]
    rc, out, err, _calls = _run_enable_script(
        tmp_path, env,
        args=("--repo-host", "git.example.com", "--repo-project", "group/proj"),
    )
    assert rc == 0
    text = (tmp_path / ".bashrc.d" / "masterplan-env.sh").read_text()
    assert 'export LMER_REPO_HOST="git.example.com"' in text
    assert 'export LMER_REPO_PROJECT="group/proj"' in text


def test_enable_forced_without_target_asks_for_project(tmp_path):
    env = _forced_env(tmp_path)
    del env["LMER_REPO_HOST"]
    del env["LMER_REPO_PROJECT"]
    rc, out, err, _calls = _run_enable_script(tmp_path, env)
    assert rc == 2
    assert out.strip() == ""
    assert "--repo-host" in err and "--repo-project" in err
    assert not (tmp_path / ".bashrc.d" / "masterplan-env.sh").exists()


def test_enable_mirror_first_existing_candidate_wins(tmp_path):
    env = _forced_env(tmp_path)
    taskdef_mirror = tmp_path / "taskdef" / "mirrors" / "masterplan"
    taskdef_mirror.mkdir(parents=True)
    work_mirror = tmp_path / "work" / "mirrors" / "masterplan"
    env["LMER_MASTERPLAN_MIRROR_CANDIDATES"] = f"{taskdef_mirror}:{work_mirror}"
    rc, _out, _err, calls = _run_enable_script(tmp_path, env)
    assert rc == 0
    assert any(f"plugin marketplace add {taskdef_mirror}" in c for c in calls)


def test_enable_mirror_falls_back_to_last_candidate_blindly(tmp_path):
    """No candidate exists → the last one is still attempted (today's
    fail-soft posture: the claude call itself warns and continues), and the
    deprecation warning fires because the taskdef candidate was absent."""
    env = _forced_env(tmp_path)
    missing_a = tmp_path / "nope-a"
    missing_b = tmp_path / "nope-b"
    env["LMER_MASTERPLAN_MIRROR_CANDIDATES"] = f"{missing_a}:{missing_b}"
    rc, _out, err, calls = _run_enable_script(tmp_path, env)
    assert rc == 0
    assert any(f"plugin marketplace add {missing_b}" in c for c in calls)
    assert "deprecated" in err and "LMER_TASKDEF_REPO" in err


def test_enable_mirror_taskdef_hit_has_no_deprecation_warning(tmp_path):
    env = _forced_env(tmp_path)
    taskdef_mirror = tmp_path / "taskdef" / "mirrors" / "masterplan"
    taskdef_mirror.mkdir(parents=True)
    env["LMER_MASTERPLAN_MIRROR_CANDIDATES"] = (
        f"{taskdef_mirror}:{tmp_path / 'work' / 'mirrors' / 'masterplan'}"
    )
    rc, _out, err, _calls = _run_enable_script(tmp_path, env)
    assert rc == 0
    assert "deprecated" not in err


def test_enable_mirror_work_repo_fallback_warns_deprecation(tmp_path):
    """The work-repo mirror exists but the taskdef one does not → the
    fallback is used AND the operator is told to move it."""
    env = _forced_env(tmp_path)
    work_mirror = tmp_path / "work" / "mirrors" / "masterplan"
    env["LMER_MASTERPLAN_MIRROR_CANDIDATES"] = (
        f"{tmp_path / 'taskdef' / 'mirrors' / 'masterplan'}:{work_mirror}"
    )
    rc, _out, err, calls = _run_enable_script(tmp_path, env)
    assert rc == 0
    assert any(f"plugin marketplace add {work_mirror}" in c for c in calls)
    assert "deprecated" in err and "LMER_TASKDEF_REPO" in err


def test_enable_forced_is_idempotent(tmp_path):
    env = _forced_env(tmp_path)
    rc1, out1, _e1, _c1 = _run_enable_script(tmp_path, env)
    rc2, out2, _e2, _c2 = _run_enable_script(tmp_path, env)
    assert (rc1, rc2) == (0, 0)
    assert out1 == out2
    text = (tmp_path / ".bashrc.d" / "masterplan-env.sh").read_text()
    assert text.count("MASTERPLAN_RUNS_DIR") == 1


def test_enable_forced_flagless_rerun_keeps_persisted_target(tmp_path):
    """A flag-less re-run must not clobber a previously persisted repo target.

    First run: session launched without a repo target, flags supply it (the
    exit-2 recovery path). Second run: flag-less, with the drop-in's exports
    applied to the environment — exactly what a fresh profile-initialized
    shell that sourced ~/.bashrc.d would carry. The drop-in rewrite must keep
    the LMER_REPO_HOST/LMER_REPO_PROJECT exports, or every later shell loses
    run context.
    """
    env = _forced_env(tmp_path)
    del env["LMER_REPO_HOST"]
    del env["LMER_REPO_PROJECT"]
    rc1, _out, _err, _calls = _run_enable_script(
        tmp_path, env,
        args=("--repo-host", "git.example.com", "--repo-project", "group/proj"),
    )
    assert rc1 == 0
    rerun_env = dict(env)
    rerun_env["LMER_REPO_HOST"] = "git.example.com"
    rerun_env["LMER_REPO_PROJECT"] = "group/proj"
    rc2, _out2, _err2, _calls2 = _run_enable_script(tmp_path, rerun_env)
    assert rc2 == 0
    text = (tmp_path / ".bashrc.d" / "masterplan-env.sh").read_text()
    assert 'export LMER_REPO_HOST="git.example.com"' in text
    assert 'export LMER_REPO_PROJECT="group/proj"' in text


@pytest.mark.parametrize(
    "broken_python",
    ["/bin/false", "/nonexistent/python3"],
    ids=["exit-1", "exit-127"],
)
def test_enable_forced_interpreter_failure_is_loud(tmp_path, broken_python):
    """Forced mode only gets 0 or 2 from main(), so any other rc means an
    interpreter-level failure (broken LMER_PYTHON, lmer_cli not importable)
    whose stderr the script's 2>/dev/null swallowed. That must surface as the
    warn-and-exit-2 contract naming the interpreter — neither a silent skip
    nor the ask-for-a-project message, which would steer the caller into a
    retry that cannot succeed."""
    env = _forced_env(tmp_path)
    env["LMER_PYTHON"] = broken_python
    rc, out, err, calls = _run_enable_script(tmp_path, env)
    assert rc == 2
    assert out.strip() == ""
    assert "failed" in err and "lmer_cli" in err
    assert "--repo-host" not in err
    assert calls == []


def test_enable_gated_skips_silently_when_not_masterplan(tmp_path):
    env = _forced_env(tmp_path)
    rc, out, err, calls = _run_enable_script(tmp_path, env, args=("--gated",))
    assert rc == 1
    assert out.strip() == ""
    assert calls == []


def test_enable_gated_provisions_when_enabled(tmp_path):
    env = _forced_env(tmp_path)
    env["LMER_MASTERPLAN"] = "1"
    rc, out, err, calls = _run_enable_script(tmp_path, env, args=("--gated",))
    assert rc == 0
    assert out.strip().endswith("/masterplan")
    assert any("plugin enable masterplan" in c for c in calls)
    # gated mode must NOT write the mid-session drop-in
    assert not (tmp_path / ".bashrc.d" / "masterplan-env.sh").exists()


def test_agents_md_teaches_on_demand_flow():
    """Guard: the standing instructions must teach the mid-session enable
    flow — the script to run, the ask-for-project fallback, and the one
    manual /reload-plugins step. Without this fragment the feature is
    undiscoverable by the model."""
    agents_md = (Path(__file__).parent.parent / "AGENTS.md").read_text()
    assert "masterplan-enable.sh" in agents_md
    assert "/reload-plugins" in agents_md
    assert "--repo-host" in agents_md
