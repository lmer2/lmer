"""Preset defaults through the platform, real CLI, container env and runners."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lmer_cli import runtime
from lmer_platform import api, answer, assistant, config as cfg, daemon, registry, resume, slots, spawn, store
from tests.conftest import strip_lmer_env
from tests.test_platform_answer import HOST, PROJECT, TARGET, TASKDEF, plant_run, track_run

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def clean_environment(tmp_path, monkeypatch):
    strip_lmer_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((str(ROOT), str(ROOT / "src"))))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runtime, "_LMER_STATE_DIR", tmp_path / ".lmer")
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))


@pytest.fixture
def launcher(tmp_path):
    """Run main() in the spawned child, replacing only container/runtime work."""
    script = tmp_path / "lmer"
    report = tmp_path / "container.json"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "from unittest.mock import patch\n"
        "from lmer_cli.cli import main\n"
        "from tests.test_lmer_cli_slack_target import _make_main_mocks\n"
        "os.environ.setdefault('LMER_WORK_REPO', 'https://github.com/example/work-repo.git')\n"
        "os.environ.setdefault('LMER_IMAGE', 'lmer-test-image:latest')\n"
        "captured = {}\n"
        "with _make_main_mocks(captured_env=captured), "
        "patch('lmer_cli.cli._get_taskdef_paths', return_value=[]), "
        "patch('lmer_cli.cli.resolve_container', return_value='test-container'), "
        "patch('lmer_cli.cli.inspect_container_workdir', return_value='/app'), "
        "patch('lmer_cli.cli.build_service_mode_mounts', return_value=[]):\n"
        "    rc = main(sys.argv[1:])\n"
        "keys = ('LMER_LLM_NAME', 'LMER_REASONING_EFFORT', 'LMER_HARNESS', "
        "'LMER_PRESET_CONTROL', 'LMER_SERVICE_NAME', 'LMER_NO_REPO', 'LMER_FASTAPI_PORT')\n"
        "payload = {'rc': rc, 'env': {k: captured.get(k) for k in keys}}\n"
        f"Path({str(report)!r}).write_text(json.dumps(payload))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script, report


def wait_for_report(report, session):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if report.is_file():
            try:
                return json.loads(report.read_text())
            except ValueError:
                pass
        time.sleep(.02)
    pytest.fail(f"launcher did not report container env: {session.log_path.read_text()}")


def runner_argv(tmp_path, harness, env):
    """Intercept exec after the real shell runner constructs its final argv.

    A shell function intercepts exec even when an installed harness would shadow
    a PATH stub. No real harness or credentials are used.
    """
    captured = tmp_path / "runner-argv"
    result = subprocess.run(
        ["bash", "-c", 'exec() { printf "%s\\0" "$@" > "$ARGV_FILE"; exit 0; }; source "$RUNNER_FILE"',
         str(ROOT / "libexec" / f"{harness}-runner.sh")],
        env={
            "PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
            "ARGV_FILE": str(captured),
            "RUNNER_FILE": str(ROOT / "libexec" / f"{harness}-runner.sh"),
            "LMER_GLOBAL_DIR": str(tmp_path / "no-global"),
            "LMER_WORK_AGENT_FILES_ROOT": str(tmp_path / "no-work"),
            "LMER_SETTINGS_LOCAL_FILE": str(tmp_path / "no-settings"),
            "LMER_MCP_LOCAL_FILE": str(tmp_path / "no-mcp"),
            "LMER_CLAUDE_CONFIG_FILE": str(tmp_path / "claude.json"),
            "LMER_DISABLE_SUPERVISOR": "1",
            **{key: value for key, value in env.items() if value is not None},
        },
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert captured.is_file(), result.stdout + result.stderr
    argv = captured.read_bytes().decode().rstrip("\0").split("\0")
    assert argv[0] == harness
    return argv


@pytest.mark.parametrize("harness,model", [("codex", "gpt-6-astra"), ("claude", "opus")])
@pytest.mark.parametrize("path", ["spawn", "resume", "answer", "slot", "assistant"])
@pytest.mark.parametrize("source", ["defaults", "exports"])
def test_preset_effort_survives_every_launch_path(tmp_path, monkeypatch, launcher, path, harness, model, source):
    script, report = launcher
    preset = {"env": {
        "LMER_LLM_NAME": model, "LMER_HARNESS": harness,
        "LMER_REASONING_EFFORT": "xhigh", "LMER_PRESET_CONTROL": "preset",
    }}
    if path == "slot":
        preset.update(checkout=str(tmp_path), service="web")
        monkeypatch.setattr(slots, "_detected_runtime", lambda: "podman")
        monkeypatch.setattr(slots, "resolve_container", lambda *a, **kw: "test-container")
        slots.clear_probe_cache()
        cfg.update_stored({"slots": [{"name": "web", "preset": "chosen"}]})
    presets = tmp_path / "presets.json"
    presets.write_text(json.dumps({"chosen": preset}))
    monkeypatch.setenv("LMER_PRESETS_FILE", str(presets))
    host_model = "gpt-5.6-sol" if harness == "codex" else "sonnet"
    (tmp_path / ".env").write_text(
        f"LMER_LLM_NAME={host_model}\nLMER_REASONING_EFFORT=low\nLMER_PRESET_CONTROL=host\n"
    )
    if source == "exports":
        monkeypatch.setenv("LMER_REASONING_EFFORT", "low")
        monkeypatch.setenv("LMER_LLM_NAME", host_model)
    daemon._load_env_files()
    daemon._load_env_files()
    (tmp_path / ".env").unlink()
    expected_model = model if source == "defaults" else host_model
    expected_effort = "xhigh" if source == "defaults" else "low"
    config = cfg.load({"lmer_bin": str(script)})

    if path in {"resume", "answer"}:
        monkeypatch.setenv(f"LMER_{TASKDEF.upper()}_PRESET", "chosen")
        plant_run(config, stop_reason="question" if path == "answer" else None,
                  question="Continue?" if path == "answer" else None)
        tracked = track_run()
        if path == "answer":
            result = answer.answer_run(config, answer.AnswerRequest(
                host=HOST, project=PROJECT, slug=tracked.slug, answer="yes",
            ))
        else:
            result = resume.resume_run(config, resume.ResumeRequest(
                host=HOST, project=PROJECT, slug=tracked.slug,
            ))
        session = result.session
    elif path == "assistant":
        monkeypatch.setattr(assistant, "_get_taskdef_paths", lambda *a: [])
        status = assistant.start(config, settings={"preset": "chosen"})
        entry = registry.read_session(status.session_id)
        session = spawn.SpawnResult(status.session_id, status.pid,
                                    Path(entry["log_path"]), None, None, None, [])
    else:
        session = spawn.spawn_session(config, spawn.SpawnRequest(
            taskdef=TASKDEF, target=TARGET,
            preset="chosen" if path == "spawn" else None,
            slot="web" if path == "slot" else None,
        ))

    try:
        payload = wait_for_report(report, session)
        assert payload["rc"] == 0, session.log_path.read_text()
        env = payload["env"]
        assert env["LMER_REASONING_EFFORT"] == expected_effort
        assert env["LMER_LLM_NAME"] == expected_model
        assert env["LMER_PRESET_CONTROL"] == "preset"
        if path == "slot":
            assert env["LMER_SERVICE_NAME"] == "web"
        argv = runner_argv(tmp_path, harness, env)
        assert argv[argv.index("--model") + 1] == expected_model
        if harness == "codex":
            assert f"model_reasoning_effort={expected_effort}" in argv
        else:
            assert argv[argv.index("--effort") + 1] == expected_effort
        assert session.to_dict()["effort"] == expected_effort
        assert session.to_dict()["model"] == expected_model
        if path in {"resume", "answer"}:
            assert result.to_dict()["session"]["effort"] == expected_effort
            assert result.to_dict()["session"]["model"] == expected_model
        entry = spawn.absorb_ports([registry.read_session(session.session_id)])[0]
        assert entry["task"]["effort"] == expected_effort
        assert entry["task"]["model"] == expected_model
        if path != "assistant":
            client = TestClient(api.create_app(config, "test-secret"))
            response = client.get("/api/state", headers={"Authorization": "Bearer test-secret"})
            assert response.status_code == 200
            run = next(run for run in response.json()["runs"] if run["slug"] == session.slug)
            assert run["effort"] == expected_effort
            assert run["model"] == expected_model
    finally:
        os.killpg(session.pid, signal.SIGKILL)
        assert spawn.wait_for_exit_recorded(session.session_id, timeout=30)
        slots.clear_probe_cache()


@pytest.mark.parametrize("no_repo", [False, True])
def test_platform_invocation_controls_beat_preset_and_defaults(tmp_path, monkeypatch, launcher, no_repo):
    script, report = launcher
    presets = tmp_path / "presets.json"
    presets.write_text(json.dumps({"chosen": {"env": {
        "LMER_NO_REPO": "" if no_repo else "1", "LMER_FASTAPI_PORT": "1",
    }}}))
    monkeypatch.setenv("LMER_PRESETS_FILE", str(presets))
    (tmp_path / ".env").write_text(f"LMER_NO_REPO={'1' if no_repo else ''}\n")
    daemon._load_env_files()
    config = cfg.load({"lmer_bin": str(script)})
    session = spawn.spawn_session(config, spawn.SpawnRequest(
        taskdef=TASKDEF, target=TARGET, preset="chosen", no_repo=no_repo,
    ))
    try:
        payload = wait_for_report(report, session)
        assert payload["rc"] == 0, session.log_path.read_text()
        assert payload["env"]["LMER_NO_REPO"] == ("1" if no_repo else None)
        assert payload["env"]["LMER_FASTAPI_PORT"] == str(session.control_port)
    finally:
        os.killpg(session.pid, signal.SIGKILL)
        assert spawn.wait_for_exit_recorded(session.session_id, timeout=30)


@pytest.mark.parametrize("payload", [{}, {"effort": None}, {"effort": " "}, {"effort": 7}])
def test_unreported_effort_stays_unknown(tmp_path, payload):
    session = spawn.SpawnResult("s-pending", 1, tmp_path / "log", None, None, None, [])
    path = spawn.ports_file_for(session.session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    assert session.to_dict()["effort"] is None
    registry.register(session.session_id, pid=os.getpid(), task={"model": "opus", "harness": "claude"})
    entry = spawn.absorb_ports(registry.list_sessions())[0]
    assert entry["task"].get("effort") is None


def test_effort_arrives_after_model_harness_and_ports(tmp_path, monkeypatch):
    from lmer_cli.cli import _record_session_model

    registry.register("s-late", pid=os.getpid(),
                      task={"model": "opus", "harness": "claude", "target": TARGET},
                      ports=[{"host": 30001, "container": 30001}])
    report = spawn.ports_file_for("s-late")
    report.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LMER_PLATFORM_PORTS_FILE", str(report))
    _record_session_model("opus", "claude", "xhigh")
    entry = spawn.absorb_ports(registry.list_sessions())[0]
    assert entry["task"] == {"model": "opus", "harness": "claude", "target": TARGET, "effort": "xhigh"}
    assert registry.read_session("s-late")["task"]["effort"] == "xhigh"
