"""Tests for `work setup-workspace` — on-demand /workspace bootstrap (issue #69).

Covers the engine in lmer_cli.container.clone_and_exec.setup_workspace and its
URL/token/dependency helpers, plus the work_repo.cli command wrapper that writes
the sourceable routing-env file.
"""

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from lmer_cli.container import clone_and_exec
from lmer_cli.container.clone_and_exec import (
    WorkspaceExistsError,
    _detect_and_sync_deps,
    _inject_token_into_repo_url,
    _parse_host_project,
    _resolve_clone_url,
    _trust_mise_config,
    setup_workspace,
)


def _init_git_repo(path: Path) -> None:
    """Initialize a git repo at the given path (mirrors provision tests)."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )


class TestParseHostProject:
    def test_https_with_token_credentials(self):
        host, project = _parse_host_project(
            "https://oauth2:TOKEN@git.example.com/group/project.git"
        )
        assert host == "git.example.com"
        assert project == "group/project"

    def test_https_without_credentials_or_git_suffix(self):
        host, project = _parse_host_project(
            "https://git.example.com/group/sub/project"
        )
        assert host == "git.example.com"
        assert project == "group/sub/project"

    def test_ssh_form(self):
        host, project = _parse_host_project("git@github.com:owner/repo.git")
        assert host == "github.com"
        assert project == "owner/repo"

    def test_ssh_form_without_git_suffix(self):
        host, project = _parse_host_project("git@github.com:owner/repo")
        assert host == "github.com"
        assert project == "owner/repo"

    def test_credentials_never_returned_in_host(self):
        host, _ = _parse_host_project("https://oauth2:secret@git.example.com/a/b.git")
        assert host == "git.example.com"
        assert "secret" not in (host or "")

    def test_garbage_returns_none(self):
        assert _parse_host_project("") == (None, None)
        assert _parse_host_project("git@nohostcolon") == (None, None)


class TestResolveCloneUrl:
    def test_gitlab_issue_url_with_token(self):
        with patch.dict(os.environ, {"GITLAB_TOKEN_gitlab_example_com": "TESTTOK"}, clear=True):
            url = _resolve_clone_url("https://gitlab.example.com/group/project/-/issues/10")
        assert url == "https://oauth2:TESTTOK@gitlab.example.com/group/project.git"

    def test_gitlab_mr_url_with_token(self):
        with patch.dict(os.environ, {"GITLAB_TOKEN_gitlab_example_com": "TESTTOK"}, clear=True):
            url = _resolve_clone_url(
                "https://gitlab.example.com/group/project/-/merge_requests/5"
            )
        assert url == "https://oauth2:TESTTOK@gitlab.example.com/group/project.git"

    def test_gitlab_work_items_url_with_token(self):
        with patch.dict(os.environ, {"GITLAB_TOKEN_gitlab_example_com": "TESTTOK"}, clear=True):
            url = _resolve_clone_url(
                "https://gitlab.example.com/group/project/-/work_items/12"
            )
        assert url == "https://oauth2:TESTTOK@gitlab.example.com/group/project.git"

    def test_plain_https_repo_url_gets_token(self):
        with patch.dict(os.environ, {"GITLAB_TOKEN_gitlab_example_com": "TESTTOK"}, clear=True):
            url = _resolve_clone_url("https://gitlab.example.com/group/project")
        assert url == "https://oauth2:TESTTOK@gitlab.example.com/group/project.git"

    def test_ssh_repo_url_converted_to_https_with_token(self):
        with patch.dict(os.environ, {"GITLAB_TOKEN_gitlab_example_com": "TESTTOK"}, clear=True):
            url = _resolve_clone_url("git@gitlab.example.com:group/project.git")
        assert url == "https://oauth2:TESTTOK@gitlab.example.com/group/project.git"

    def test_ssh_repo_url_without_token_stays_ssh(self):
        with patch.dict(os.environ, {}, clear=True):
            url = _resolve_clone_url("git@gitlab.example.com:group/project.git")
        assert url == "git@gitlab.example.com:group/project.git"

    def test_plain_https_without_token_unchanged(self):
        with patch.dict(os.environ, {}, clear=True):
            url = _resolve_clone_url("https://gitlab.example.com/group/project")
        assert url == "https://gitlab.example.com/group/project"

    def test_unrecognized_target_returns_none(self):
        with patch.dict(os.environ, {}, clear=True):
            assert _resolve_clone_url("not-a-url") is None
            assert _resolve_clone_url("") is None


class TestInjectTokenIntoRepoUrl:
    def test_does_not_double_inject_existing_credentials(self):
        with patch.dict(os.environ, {"GITLAB_TOKEN_gitlab_example_com": "TESTTOK"}, clear=True):
            url = _inject_token_into_repo_url(
                "https://oauth2:already@gitlab.example.com/a/b.git"
            )
        assert url == "https://oauth2:already@gitlab.example.com/a/b.git"

    def test_non_https_non_ssh_unchanged(self):
        with patch.dict(os.environ, {"GITLAB_TOKEN_x": "T"}, clear=True):
            assert _inject_token_into_repo_url("ftp://x/y") == "ftp://x/y"


class TestDetectAndSyncDeps:
    def test_skips_when_not_uv_project(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
        status = _detect_and_sync_deps(tmp_path)
        assert "skipped" in status
        assert "uv.lock" in status

    def test_skips_when_no_python_at_all(self, tmp_path):
        status = _detect_and_sync_deps(tmp_path)
        assert "skipped" in status

    def test_detects_uv_lock_and_runs_sync(self, tmp_path):
        (tmp_path / "uv.lock").write_text("")
        with patch.object(clone_and_exec.shutil, "which", return_value="/usr/bin/uv"), \
             patch.object(clone_and_exec.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")
            status = _detect_and_sync_deps(tmp_path)
        assert status == "synced (uv sync)"
        # Ran `uv sync` in the workspace dir
        args, kwargs = mock_run.call_args
        assert args[0] == ["uv", "sync"]
        assert kwargs["cwd"] == str(tmp_path)

    def test_detects_tool_uv_in_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\n[tool.uv]\npackage = true\n'
        )
        with patch.object(clone_and_exec.shutil, "which", return_value="/usr/bin/uv"), \
             patch.object(clone_and_exec.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")
            status = _detect_and_sync_deps(tmp_path)
        assert status == "synced (uv sync)"

    def test_skips_when_uv_not_on_path(self, tmp_path):
        (tmp_path / "uv.lock").write_text("")
        with patch.object(clone_and_exec.shutil, "which", return_value=None):
            status = _detect_and_sync_deps(tmp_path)
        assert "uv not found" in status

    def test_reports_failure_on_nonzero_sync(self, tmp_path):
        (tmp_path / "uv.lock").write_text("")
        with patch.object(clone_and_exec.shutil, "which", return_value="/usr/bin/uv"), \
             patch.object(clone_and_exec.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 1, "", "boom")
            status = _detect_and_sync_deps(tmp_path)
        assert status.startswith("FAILED")
        assert "1" in status


class TestTrustMiseConfig:
    """_trust_mise_config: trust the workspace .mise.toml only when opted in.

    Regression coverage for the gap where `work setup-workspace` left a cloned
    repo's .mise.toml untrusted (mise warned on every command) because the
    trust step only ran on the container-startup path, not the mid-session
    bootstrap. The helper is now shared by both.
    """

    def _record_run(self, calls):
        def fake_run(cmd):
            calls.append(cmd)
            return 0
        return fake_run

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "Yes"])
    def test_trusts_when_opted_in(self, tmp_path, value):
        mise_toml = tmp_path / ".mise.toml"
        mise_toml.write_text("[tools]\n")
        calls = []
        with patch.dict(os.environ, {"LMER_TRUST_MISE": value}, clear=True):
            with patch.object(clone_and_exec, "run", self._record_run(calls)):
                _trust_mise_config(tmp_path)
        assert calls == [["mise", "trust", str(mise_toml)]]

    @pytest.mark.parametrize("value", ["", "0", "false", "no"])
    def test_warns_and_skips_when_not_opted_in(self, tmp_path, capsys, value):
        (tmp_path / ".mise.toml").write_text("[tools]\n")
        calls = []
        env = {"LMER_TRUST_MISE": value} if value else {}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(clone_and_exec, "run", self._record_run(calls)):
                _trust_mise_config(tmp_path)
        # Never trusted, and the opt-in hint is surfaced.
        assert calls == []
        assert "LMER_TRUST_MISE is not set" in capsys.readouterr().err

    def test_noop_when_no_mise_config(self, tmp_path, capsys):
        calls = []
        with patch.dict(os.environ, {"LMER_TRUST_MISE": "1"}, clear=True):
            with patch.object(clone_and_exec, "run", self._record_run(calls)):
                _trust_mise_config(tmp_path)
        # No .mise.toml -> completely silent, nothing trusted.
        assert calls == []
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_trust_failure_is_swallowed(self, tmp_path):
        (tmp_path / ".mise.toml").write_text("[tools]\n")

        def boom(cmd):
            raise RuntimeError("mise not installed")

        with patch.dict(os.environ, {"LMER_TRUST_MISE": "1"}, clear=True):
            with patch.object(clone_and_exec, "run", boom):
                # Best-effort: a failing `mise trust` must not raise.
                _trust_mise_config(tmp_path)


class TestSetupWorkspaceGuards:
    def test_hard_error_when_git_checkout_present(self, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / ".git").mkdir()
        with pytest.raises(WorkspaceExistsError, match="already contains a git checkout"):
            setup_workspace(
                "https://gitlab.example.com/a/b/-/issues/1",
                workspace=ws,
            )

    def test_hard_error_when_non_empty(self, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "stray.txt").write_text("hi")
        with pytest.raises(WorkspaceExistsError, match="not empty"):
            setup_workspace(
                "https://gitlab.example.com/a/b/-/issues/1",
                workspace=ws,
            )

    def test_value_error_on_unresolvable_target(self, tmp_path):
        ws = tmp_path / "workspace"
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="Could not derive"):
                setup_workspace("not-a-url", workspace=ws)

    def test_clone_failure_scrubs_token_from_error(self, tmp_path):
        """A failing clone must not surface the tokenized URL (the !104 bug
        class): str(CalledProcessError) includes e.cmd with the live token,
        and cmd_setup_workspace prints the exception verbatim — so
        setup_workspace itself must re-raise a scrubbed error."""
        ws = tmp_path / "workspace"

        def failing_clone(workspace, repo_url, branch, ref):
            raise subprocess.CalledProcessError(
                128, ["git", "clone", repo_url, str(workspace)]
            )

        with patch.dict(
            os.environ, {"GITLAB_TOKEN_gitlab_example_com": "TESTTOK"}, clear=True
        ):
            with patch.object(clone_and_exec, "ensure_clone", failing_clone):
                with pytest.raises(RuntimeError, match="clone failed") as excinfo:
                    setup_workspace(
                        "https://gitlab.example.com/group/project/-/issues/10",
                        workspace=ws,
                        work_repo_path=tmp_path / "work",
                        sync_deps=False,
                    )
        message = str(excinfo.value)
        assert "TESTTOK" not in message
        assert "oauth2" not in message
        # The command context itself is preserved for diagnosis.
        assert "git" in message and "clone" in message


class TestSetupWorkspaceHappyPath:
    def _fake_clone_with_git_init(self, ws_holder):
        """Return an ensure_clone replacement that git-inits the workspace.

        provision_documentation needs a real .git/info dir, so the fake clone
        initializes one instead of contacting a remote.
        """
        def fake_ensure_clone(workspace, repo_url, branch, ref):
            Path(workspace).mkdir(parents=True, exist_ok=True)
            _init_git_repo(Path(workspace))
            ws_holder["repo_url"] = repo_url
        return fake_ensure_clone

    def test_full_setup_provisions_and_sets_env(self, tmp_path):
        ws = tmp_path / "workspace"
        work_repo = tmp_path / "work"
        global_path = tmp_path / "global"
        work_repo.mkdir()
        global_path.mkdir()
        (global_path / "AGENTS.md").write_text("# Global config")
        (global_path / "rules").mkdir()
        (global_path / "rules" / "git.md").write_text("# Git rules")

        holder = {}
        with patch.dict(os.environ, {"GITLAB_TOKEN_gitlab_example_com": "TESTTOK"}, clear=True):
            with patch.object(
                clone_and_exec, "ensure_clone",
                self._fake_clone_with_git_init(holder),
            ):
                result = setup_workspace(
                    "https://gitlab.example.com/group/project/-/issues/10",
                    workspace=ws,
                    work_repo_path=work_repo,
                    global_path=global_path,
                    sync_deps=False,
                )

            # Routing env vars exported for the current process
            assert os.environ["LMER_REPO_HOST"] == "gitlab.example.com"
            assert os.environ["LMER_REPO_PROJECT"] == "group/project"
            assert os.environ["LMER_TASK"] == "develop"
            assert os.environ["LMER_TASK_TARGET"].endswith("/issues/10")

        assert result["host"] == "gitlab.example.com"
        assert result["project"] == "group/project"
        assert result["task"] == "develop"
        # Docs provisioned from global
        assert "AGENTS.md" in result["provisioned"]
        assert (ws / "AGENTS.md").read_text() == "# Global config"
        # Deps were skipped (sync_deps=False)
        assert "skipped" in result["deps_status"]
        # repo_url in the result carries no token
        assert "TESTTOK" not in result["repo_url"]
        assert result["repo_url"] == "https://gitlab.example.com/group/project"
        # The clone URL passed to ensure_clone did carry the token
        assert holder["repo_url"] == "https://oauth2:TESTTOK@gitlab.example.com/group/project.git"

    def test_custom_task_used_for_work_repo_layout(self, tmp_path):
        ws = tmp_path / "workspace"
        work_repo = tmp_path / "work"
        global_path = tmp_path / "global"
        work_repo.mkdir()
        global_path.mkdir()

        holder = {}
        with patch.dict(os.environ, {"GITLAB_TOKEN_gitlab_example_com": "TESTTOK"}, clear=True):
            with patch.object(
                clone_and_exec, "ensure_clone",
                self._fake_clone_with_git_init(holder),
            ):
                result = setup_workspace(
                    "https://gitlab.example.com/group/project/-/issues/10",
                    task="review",
                    workspace=ws,
                    work_repo_path=work_repo,
                    global_path=global_path,
                    sync_deps=False,
                )
        assert result["task"] == "review"
        # Work-repo task dir created under the chosen task type
        assert (work_repo / "gitlab.example.com" / "group" / "project" / "review").is_dir()

    def test_gitlab_mr_target_checks_out_mr_branch(self, tmp_path):
        ws = tmp_path / "workspace"
        work_repo = tmp_path / "work"
        global_path = tmp_path / "global"
        work_repo.mkdir()
        global_path.mkdir()

        holder = {}
        check_calls = []

        def fake_check_call(cmd):
            check_calls.append(cmd)

        with patch.dict(os.environ, {"GITLAB_TOKEN_gitlab_example_com": "TESTTOK"}, clear=True):
            with patch.object(
                clone_and_exec, "ensure_clone",
                self._fake_clone_with_git_init(holder),
            ), patch.object(clone_and_exec, "check_call", fake_check_call):
                setup_workspace(
                    "https://gitlab.example.com/group/project/-/merge_requests/5",
                    workspace=ws,
                    work_repo_path=work_repo,
                    global_path=global_path,
                    sync_deps=False,
                )

        # The MR fetch + checkout were attempted
        fetches = [c for c in check_calls if "fetch" in c]
        assert any("merge-requests/5/head:mr-5" in part for c in fetches for part in c)
        checkouts = [c for c in check_calls if "checkout" in c]
        assert any("mr-5" in part for c in checkouts for part in c)

    def test_runs_dependency_sync_when_enabled(self, tmp_path):
        ws = tmp_path / "workspace"
        work_repo = tmp_path / "work"
        global_path = tmp_path / "global"
        work_repo.mkdir()
        global_path.mkdir()

        holder = {}
        with patch.dict(os.environ, {"GITLAB_TOKEN_gitlab_example_com": "TESTTOK"}, clear=True):
            with patch.object(
                clone_and_exec, "ensure_clone",
                self._fake_clone_with_git_init(holder),
            ), patch.object(
                clone_and_exec, "_detect_and_sync_deps",
                return_value="synced (uv sync)",
            ) as mock_sync:
                result = setup_workspace(
                    "https://gitlab.example.com/group/project/-/issues/10",
                    workspace=ws,
                    work_repo_path=work_repo,
                    global_path=global_path,
                    sync_deps=True,
                )
        mock_sync.assert_called_once()
        assert result["deps_status"] == "synced (uv sync)"

    def test_trusts_workspace_mise_config(self, tmp_path):
        # setup_workspace must run the shared mise-trust step (the fix for the
        # `.mise.toml` untrusted gap), the same way container startup does.
        ws = tmp_path / "workspace"
        work_repo = tmp_path / "work"
        global_path = tmp_path / "global"
        work_repo.mkdir()
        global_path.mkdir()

        holder = {}
        with patch.dict(os.environ, {"GITLAB_TOKEN_gitlab_example_com": "TESTTOK"}, clear=True):
            with patch.object(
                clone_and_exec, "ensure_clone",
                self._fake_clone_with_git_init(holder),
            ), patch.object(clone_and_exec, "_trust_mise_config") as mock_trust:
                setup_workspace(
                    "https://gitlab.example.com/group/project/-/issues/10",
                    workspace=ws,
                    work_repo_path=work_repo,
                    global_path=global_path,
                    sync_deps=False,
                )
        mock_trust.assert_called_once_with(ws.resolve())


class TestCmdSetupWorkspace:
    """Tests for the work_repo.cli command wrapper."""

    @pytest.fixture(autouse=True)
    def stub_restore_memory(self):
        """Neutralize real agent-memory restore in the wrapper tests.

        cmd_setup_workspace calls restore_memory() after a successful setup;
        stub it so these tests never touch the real ~/.claude memory dir or
        depend on ambient LMER_PERSIST_AGENT_MEMORY / LMER_REPO_* state. Tests
        that care about the call take this fixture as a parameter.
        """
        with patch("work_repo.cli.restore_memory") as mock:
            mock.return_value = 0
            yield mock

    def test_already_set_up_returns_one(self, tmp_path, capsys):
        from work_repo.cli import cmd_setup_workspace

        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / ".git").mkdir()

        def fake_setup(target, **kwargs):
            raise WorkspaceExistsError(f"{ws} already contains a git checkout")

        with patch.object(clone_and_exec, "setup_workspace", fake_setup):
            rc = cmd_setup_workspace("https://gitlab.example.com/a/b/-/issues/1", "develop", True)

        assert rc == 1
        err = capsys.readouterr().err
        assert "already contains a git checkout" in err

    def test_value_error_returns_one(self, capsys):
        from work_repo.cli import cmd_setup_workspace

        def fake_setup(target, **kwargs):
            raise ValueError("Could not derive a repository URL")

        with patch.object(clone_and_exec, "setup_workspace", fake_setup):
            rc = cmd_setup_workspace("garbage", "develop", True)

        assert rc == 1
        assert "Could not derive" in capsys.readouterr().err

    def test_happy_path_writes_env_file_and_summary(self, tmp_path, capsys, monkeypatch):
        import work_repo.cli as wc

        env_file = tmp_path / "lmer-workspace-env.sh"
        monkeypatch.setattr(wc, "WORKSPACE_ENV_FILE", env_file)

        fake_result = {
            "host": "gitlab.example.com",
            "project": "group/project",
            "task": "develop",
            "task_target": "https://gitlab.example.com/group/project/-/issues/10",
            "branch": "main",
            "provisioned": ["AGENTS.md", "rules/git.md"],
            "deps_status": "synced (uv sync)",
            "repo_url": "https://gitlab.example.com/group/project",
            "workspace": "/workspace",
        }

        def fake_setup(target, **kwargs):
            return fake_result

        with patch.object(clone_and_exec, "setup_workspace", fake_setup):
            rc = wc.cmd_setup_workspace(
                "https://gitlab.example.com/group/project/-/issues/10", "develop", True
            )

        assert rc == 0
        assert env_file.exists()
        content = env_file.read_text()
        assert "export LMER_REPO_HOST=gitlab.example.com" in content
        assert "export LMER_REPO_PROJECT=group/project" in content
        assert "export LMER_TASK=develop" in content
        assert "export LMER_TASK_TARGET=" in content
        # No secrets ever written to the sourceable file
        assert "oauth2" not in content
        assert "TOKEN" not in content

        out = capsys.readouterr().out
        assert "set up for gitlab.example.com/group/project" in out
        assert "AGENTS.md" in out
        assert f"source {env_file}" in out

    def test_failed_deps_still_returns_zero_with_warning(self, tmp_path, capsys, monkeypatch):
        import work_repo.cli as wc

        env_file = tmp_path / "lmer-workspace-env.sh"
        monkeypatch.setattr(wc, "WORKSPACE_ENV_FILE", env_file)

        fake_result = {
            "host": "gitlab.example.com",
            "project": "group/project",
            "task": "develop",
            "task_target": "https://gitlab.example.com/group/project/-/issues/10",
            "branch": "main",
            "provisioned": [],
            "deps_status": "FAILED (uv sync exited 1)",
            "repo_url": "https://gitlab.example.com/group/project",
            "workspace": "/workspace",
        }

        with patch.object(clone_and_exec, "setup_workspace", lambda target, **kw: fake_result):
            rc = wc.cmd_setup_workspace(
                "https://gitlab.example.com/group/project/-/issues/10", "develop", True
            )

        # Workspace is set up, so exit 0 even though sync failed — but warn loudly.
        assert rc == 0
        out = capsys.readouterr().out
        assert "Dependency sync failed" in out

    def test_no_sync_flag_passes_through(self, tmp_path, monkeypatch):
        import work_repo.cli as wc

        env_file = tmp_path / "lmer-workspace-env.sh"
        monkeypatch.setattr(wc, "WORKSPACE_ENV_FILE", env_file)

        captured = {}

        def fake_setup(target, **kwargs):
            captured.update(kwargs)
            return {
                "host": "h", "project": "p", "task": "develop",
                "task_target": target, "branch": "main", "provisioned": [],
                "deps_status": "skipped (sync disabled)",
                "repo_url": "https://h/p", "workspace": "/workspace",
            }

        with patch.object(clone_and_exec, "setup_workspace", fake_setup):
            wc.cmd_setup_workspace("https://h/p/-/issues/1", "develop", False)

        assert captured["sync_deps"] is False

    def test_restores_agent_memory_after_setup(self, tmp_path, monkeypatch, stub_restore_memory):
        # The fix for note_76071: after the chat->dev pivot sets the routing env,
        # the command restores this project's persisted agent memory (the step
        # claude-runner performs at normal startup).
        import work_repo.cli as wc

        env_file = tmp_path / "lmer-workspace-env.sh"
        monkeypatch.setattr(wc, "WORKSPACE_ENV_FILE", env_file)

        fake_result = {
            "host": "gitlab.example.com", "project": "group/project", "task": "develop",
            "task_target": "https://gitlab.example.com/group/project/-/issues/10",
            "branch": "main", "provisioned": [],
            "deps_status": "skipped (sync disabled)",
            "repo_url": "https://gitlab.example.com/group/project", "workspace": "/workspace",
        }
        with patch.object(clone_and_exec, "setup_workspace", lambda target, **kw: fake_result):
            rc = wc.cmd_setup_workspace(
                "https://gitlab.example.com/group/project/-/issues/10", "develop", True
            )
        assert rc == 0
        stub_restore_memory.assert_called_once()

    def test_memory_restore_failure_does_not_fail_setup(
        self, tmp_path, monkeypatch, capsys, stub_restore_memory
    ):
        import work_repo.cli as wc

        env_file = tmp_path / "lmer-workspace-env.sh"
        monkeypatch.setattr(wc, "WORKSPACE_ENV_FILE", env_file)
        stub_restore_memory.side_effect = RuntimeError("kaboom")

        fake_result = {
            "host": "gitlab.example.com", "project": "group/project", "task": "develop",
            "task_target": "https://gitlab.example.com/group/project/-/issues/10",
            "branch": "main", "provisioned": [],
            "deps_status": "skipped (sync disabled)",
            "repo_url": "https://gitlab.example.com/group/project", "workspace": "/workspace",
        }
        with patch.object(clone_and_exec, "setup_workspace", lambda target, **kw: fake_result):
            rc = wc.cmd_setup_workspace(
                "https://gitlab.example.com/group/project/-/issues/10", "develop", True
            )
        # Workspace is already set up, so a restore failure must not fail the command.
        assert rc == 0
        assert "Agent memory restore failed" in capsys.readouterr().err


class TestParserWiring:
    def test_parser_accepts_setup_workspace(self):
        from work_repo.cli import create_parser

        parser = create_parser()
        args = parser.parse_args(
            ["setup-workspace", "https://gitlab.example.com/a/b/-/issues/1"]
        )
        assert args.command == "setup-workspace"
        assert args.target == "https://gitlab.example.com/a/b/-/issues/1"
        assert args.task == "develop"
        assert args.no_sync is False

    def test_parser_task_and_no_sync_flags(self):
        from work_repo.cli import create_parser

        parser = create_parser()
        args = parser.parse_args(
            ["setup-workspace", "https://x/y/-/issues/2", "--task", "review", "--no-sync"]
        )
        assert args.task == "review"
        assert args.no_sync is True
