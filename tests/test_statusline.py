"""Tests for the lmer status line renderer (hooks/statusline.py, issue #106)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hooks.statusline import (
    DEFAULT_WORKSPACE,
    build_line,
    context_percent,
    model_name,
    payload_cwd,
)

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "hooks" / "statusline.py"


def _payload(*, cwd=None, window=None, model="Fable"):
    payload = {"model": {"display_name": model}}
    if cwd is not None:
        payload["workspace"] = {"current_dir": str(cwd)}
    if window is not None:
        payload["context_window"] = window
    return payload


# ---- context_percent ------------------------------------------------------------

class TestContextPercent:
    def test_direct_percentage_field(self):
        assert context_percent({"context_window": {"used_percentage": 42.4}}) == 42

    @pytest.mark.parametrize("key", ["used_percentage", "percent_used", "usage_percent"])
    def test_all_direct_percentage_keys(self, key):
        assert context_percent({"context_window": {key: 21}}) == 21

    def test_used_over_size(self):
        window = {"context_window_size": 200000, "total_tokens_used": 84211}
        assert context_percent({"context_window": window}) == 42

    def test_current_usage_sum_over_size(self):
        window = {
            "context_window_size": 200000,
            "current_usage": {
                "input_tokens": 30000,
                "cache_creation_input_tokens": 10000,
                "cache_read_input_tokens": 40000,
                "output_tokens": 4000,
            },
        }
        assert context_percent({"context_window": window}) == 42

    def test_clamped_to_100(self):
        window = {"context_window_size": 100, "total_tokens_used": 250}
        assert context_percent({"context_window": window}) == 100

    def test_clamped_to_0(self):
        assert context_percent({"context_window": {"used_percentage": -3}}) == 0

    @pytest.mark.parametrize("payload", [
        {},
        {"context_window": {}},
        {"context_window": {"context_window_size": 200000}},   # no usage
        {"context_window": {"total_tokens_used": 42}},         # no size
        {"context_window": {"context_window_size": 0, "total_tokens_used": 1}},
        {"context_window": "not a dict"},
        {"context_window": {"used_percentage": "42"}},         # wrong type
        {"context_window": {"used_percentage": True}},         # bool is not a number
        "not a dict",
        None,
    ])
    def test_unusable_payloads_return_none(self, payload):
        assert context_percent(payload) is None

    def test_current_usage_ignored_when_not_a_dict(self):
        window = {"context_window_size": 200000, "current_usage": [1, 2, 3]}
        assert context_percent({"context_window": window}) is None


# ---- payload_cwd / model_name ---------------------------------------------------

class TestPayloadCwd:
    def test_workspace_current_dir_wins(self):
        payload = {"workspace": {"current_dir": "/a"}, "cwd": "/b"}
        assert payload_cwd(payload) == "/a"

    def test_falls_back_to_cwd(self):
        assert payload_cwd({"cwd": "/b"}) == "/b"

    @pytest.mark.parametrize("payload", [{}, {"workspace": {}}, {"cwd": "  "}, None, "x"])
    def test_falls_back_to_default_workspace(self, payload):
        assert payload_cwd(payload) == DEFAULT_WORKSPACE


class TestModelName:
    def test_display_name(self):
        assert model_name({"model": {"display_name": "Fable"}}) == "Fable"

    @pytest.mark.parametrize("payload", [{}, {"model": {}}, {"model": "x"}, None])
    def test_absent(self, payload):
        assert model_name(payload) is None


# ---- build_line -----------------------------------------------------------------

class TestBuildLine:
    def test_all_segments(self):
        line = build_line(repo="group/project", branch="feature/x",
                          task="develop", percent=42)
        assert line == "group/project @ feature/x | develop | ctx 42%"

    def test_missing_context_segment_omitted(self):
        line = build_line(repo="group/project", branch="feature/x",
                          task="develop", percent=None)
        assert line == "group/project @ feature/x | develop"

    def test_repo_without_branch(self):
        assert build_line(repo="group/project", branch=None, task=None,
                          percent=None) == "group/project"

    def test_branch_without_repo(self):
        assert build_line(repo=None, branch="main", task=None,
                          percent=None) == "main"

    def test_zero_percent_still_rendered(self):
        assert build_line(repo=None, branch=None, task=None,
                          percent=0) == "ctx 0%"

    def test_all_missing_falls_back_to_model(self):
        assert build_line(repo=None, branch=None, task=None, percent=None,
                          model="Fable") == "Fable"

    def test_all_missing_without_model_is_empty(self):
        assert build_line(repo=None, branch=None, task=None, percent=None) == ""

    def test_indicators_appended(self):
        line = build_line(repo="r", branch=None, task=None, percent=None,
                          container=True, danger_zone=True)
        assert line == "r 📦⚡"

    def test_indicators_alone_without_segments(self):
        line = build_line(repo=None, branch=None, task=None, percent=None,
                          container=True)
        assert line == "📦"


# ---- main() via subprocess ------------------------------------------------------

def _run_script(payload, env_extra=None, raw_stdin=None):
    """Run the statusline script with a stripped-LMER env plus *env_extra*."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("LMER_")}
    env.pop("CLAUDE_CONTAINER", None)
    env.update(env_extra or {})
    stdin = raw_stdin if raw_stdin is not None else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=stdin, capture_output=True, text=True, env=env,
    )


def _git_repo(path, branch="feature/x"):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    return path


class TestMainSubprocess:
    def test_full_payload_in_git_repo(self, tmp_path):
        repo = _git_repo(tmp_path / "checkout")
        payload = _payload(cwd=repo, window={"context_window_size": 200000,
                                             "total_tokens_used": 84211})
        r = _run_script(payload, {"LMER_REPO_PROJECT": "group/project",
                                  "LMER_TASK": "develop"})
        assert r.returncode == 0
        assert r.stdout.rstrip("\n") == "group/project @ feature/x | develop | ctx 42%"

    def test_missing_usage_fields_omit_ctx_segment(self, tmp_path):
        repo = _git_repo(tmp_path / "checkout")
        payload = _payload(cwd=repo, window={"context_window_size": 200000})
        r = _run_script(payload, {"LMER_REPO_PROJECT": "group/project",
                                  "LMER_TASK": "develop"})
        assert r.returncode == 0
        assert r.stdout.rstrip("\n") == "group/project @ feature/x | develop"

    def test_repo_falls_back_to_git_toplevel_basename(self, tmp_path):
        repo = _git_repo(tmp_path / "myproject", branch="main")
        r = _run_script(_payload(cwd=repo))
        assert r.returncode == 0
        assert r.stdout.rstrip("\n") == "myproject @ main"

    def test_no_git_repo_and_no_env(self, tmp_path):
        bare = tmp_path / "not-a-repo"
        bare.mkdir()
        r = _run_script(_payload(cwd=bare))
        assert r.returncode == 0
        # No repo, branch, task, or usage — falls back to the model name.
        assert r.stdout.rstrip("\n") == "Fable"

    def test_env_repo_wins_over_git_toplevel(self, tmp_path):
        repo = _git_repo(tmp_path / "clone-dir", branch="main")
        r = _run_script(_payload(cwd=repo), {"LMER_REPO_PROJECT": "group/project"})
        assert r.stdout.rstrip("\n") == "group/project @ main"

    def test_task_without_repo_context(self, tmp_path):
        bare = tmp_path / "not-a-repo"
        bare.mkdir()
        r = _run_script(_payload(cwd=bare), {"LMER_TASK": "develop"})
        assert r.stdout.rstrip("\n") == "develop"

    def test_container_and_danger_zone_indicators(self, tmp_path):
        bare = tmp_path / "not-a-repo"
        bare.mkdir()
        r = _run_script(_payload(cwd=bare),
                        {"LMER_TASK": "develop", "CLAUDE_CONTAINER": "true",
                         "LMER_DANGER_ZONE": "1"})
        assert r.stdout.rstrip("\n") == "develop 📦⚡"

    @pytest.mark.parametrize("raw", ["", "   ", "not json", "[1,2,3]", '"str"'])
    def test_garbage_or_empty_stdin_never_breaks(self, tmp_path, raw):
        r = _run_script(None, {"LMER_REPO_PROJECT": "group/project"}, raw_stdin=raw)
        assert r.returncode == 0
        assert r.stderr == ""
        # cwd falls back to /workspace; repo comes from env regardless.
        assert r.stdout.rstrip("\n").startswith("group/project")

    def test_single_line_of_output(self, tmp_path):
        repo = _git_repo(tmp_path / "checkout")
        r = _run_script(_payload(cwd=repo, window={"used_percentage": 10}))
        assert r.stdout.count("\n") == 1


# ---- provisioning ---------------------------------------------------------------

class TestStatuslineProvisioning:
    """Every lmer session must get the status line without user setup:
    settings.json points statusLine at claude-status (on PATH via
    /Agents/global/bin), and claude-status delegates to the renderer."""

    def test_settings_statusline_block(self):
        settings = json.loads(
            (REPO_ROOT / "agent-files" / "claude" / "settings.json").read_text()
        )
        assert settings["statusLine"] == {"type": "command", "command": "claude-status"}

    def test_claude_status_delegates_to_renderer(self):
        wrapper = (REPO_ROOT / "bin" / "claude-status").read_text()
        assert "hooks/statusline.py" in wrapper
        assert 'exec python3 "$STATUSLINE"' in wrapper

    def test_scripts_are_executable(self):
        assert os.access(REPO_ROOT / "bin" / "claude-status", os.X_OK)
        assert os.access(SCRIPT, os.X_OK)

    def test_wrapper_end_to_end(self, tmp_path):
        """bin/claude-status → hooks/statusline.py with a real payload."""
        repo = _git_repo(tmp_path / "checkout")
        payload = _payload(cwd=repo, window={"used_percentage": 7})
        env = {k: v for k, v in os.environ.items() if not k.startswith("LMER_")}
        env.pop("CLAUDE_CONTAINER", None)
        env["LMER_REPO_PROJECT"] = "group/project"
        env["LMER_TASK"] = "develop"
        r = subprocess.run(
            ["bash", str(REPO_ROOT / "bin" / "claude-status")],
            input=json.dumps(payload), capture_output=True, text=True, env=env,
        )
        assert r.returncode == 0
        assert r.stdout.rstrip("\n") == "group/project @ feature/x | develop | ctx 7%"
