"""Test followup hook functionality."""
import io
import os
from contextlib import redirect_stdout

import pytest

from hooks.followup import (
    find_followup_file,
    main,
    read_and_display_followup,
)


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    """Strip LMER_* env vars so tests start from a clean slate."""
    for key in list(os.environ):
        if key.startswith("LMER_"):
            monkeypatch.delenv(key, raising=False)


class TestFollowupHook:
    """Test followup hook behavior."""

    def test_find_followup_file_from_taskdef_dir(self, tmp_path, monkeypatch):
        """find_followup_file resolves via LMER_TASKDEF_DIR."""
        taskdef = tmp_path / "develop"
        taskdef.mkdir()
        (taskdef / "followup.txt").write_text("do a followup")

        monkeypatch.setenv("LMER_TASK", "develop")
        monkeypatch.setenv("LMER_TASKDEF_DIR", str(taskdef))

        resolved = find_followup_file()
        assert resolved == taskdef / "followup.txt"

    def test_find_followup_file_via_task_instructions_sibling(
        self, tmp_path, monkeypatch
    ):
        """Falls back to LMER_TASK_INSTRUCTIONS parent when TASKDEF_DIR unset."""
        taskdef = tmp_path / "develop"
        taskdef.mkdir()
        (taskdef / "followup.txt").write_text("do a followup")
        (taskdef / "instructions.txt").write_text("do the task")

        monkeypatch.setenv("LMER_TASK", "develop")
        monkeypatch.setenv(
            "LMER_TASK_INSTRUCTIONS", str(taskdef / "instructions.txt")
        )

        resolved = find_followup_file()
        assert resolved == taskdef / "followup.txt"

    def test_find_followup_file_via_taskdef_paths(self, tmp_path, monkeypatch):
        """Falls back to LMER_TASKDEF_PATHS search when direct env vars absent."""
        external = tmp_path / "external"
        taskdef = external / "develop"
        taskdef.mkdir(parents=True)
        (taskdef / "followup.txt").write_text("from external")

        monkeypatch.setenv("LMER_TASK", "develop")
        monkeypatch.setenv("LMER_TASKDEF_PATHS", str(external))

        resolved = find_followup_file()
        assert resolved == taskdef / "followup.txt"

    def test_find_followup_file_missing_returns_none(self, tmp_path, monkeypatch):
        """Missing followup.txt returns None and prints a helpful error."""
        taskdef = tmp_path / "develop"
        taskdef.mkdir()

        monkeypatch.setenv("LMER_TASK", "develop")
        monkeypatch.setenv("LMER_TASKDEF_DIR", str(taskdef))

        f = io.StringIO()
        with redirect_stdout(f):
            resolved = find_followup_file()

        assert resolved is None
        output = f.getvalue()
        assert "followup.txt not found" in output
        assert "develop" in output

    def test_find_followup_file_no_task_context_errors(self, tmp_path, monkeypatch):
        """With no task context at all, returns None with usage hint."""
        # Ensure no task-related env vars are set.
        monkeypatch.delenv("LMER_TASK", raising=False)
        monkeypatch.delenv("LMER_TASKDEF", raising=False)
        monkeypatch.delenv("LMER_TASKDEF_DIR", raising=False)
        monkeypatch.delenv("LMER_TASK_INSTRUCTIONS", raising=False)

        f = io.StringIO()
        with redirect_stdout(f):
            resolved = find_followup_file()

        assert resolved is None
        assert "No task definition specified" in f.getvalue()

    def test_read_and_display_followup_renders_jinja(self, tmp_path, monkeypatch):
        """followup.txt is rendered as a Jinja2 template with LMER_* context."""
        monkeypatch.setenv("LMER_REPO_URL", "https://git.example.com/foo/bar")
        monkeypatch.setenv(
            "LMER_TASK_TARGET", "https://git.example.com/foo/bar/-/merge_requests/42"
        )

        taskdef = tmp_path / "develop"
        taskdef.mkdir()
        followup_file = taskdef / "followup.txt"
        followup_file.write_text(
            "Repo: {{ LMER_REPO_URL }}\n"
            "Target: {{ LMER_TASK_TARGET }}\n"
            "Task: {{ taskdef_name }}\n"
            "File: {{ followup_file }}\n"
        )

        f = io.StringIO()
        with redirect_stdout(f):
            read_and_display_followup(followup_file)

        output = f.getvalue()
        assert "Repo: https://git.example.com/foo/bar" in output
        assert "Target: https://git.example.com/foo/bar/-/merge_requests/42" in output
        assert "Task: develop" in output
        assert str(followup_file) in output

    def test_read_and_display_followup_excludes_non_lmer_env(
        self, tmp_path, monkeypatch
    ):
        """Non-LMER env vars must not leak into the template context."""
        monkeypatch.setenv("LMER_REPO_URL", "https://example.com/repo")
        monkeypatch.setenv("GITLAB_HOST", "gitlab.example.com")
        monkeypatch.setenv("PATH", "/usr/bin")

        taskdef = tmp_path / "develop"
        taskdef.mkdir()
        followup_file = taskdef / "followup.txt"
        followup_file.write_text(
            "{% if LMER_REPO_URL %}Repo: {{ LMER_REPO_URL }}{% endif %}\n"
            "{% if GITLAB_HOST %}GitLab: {{ GITLAB_HOST }}{% endif %}\n"
            "{% if PATH %}Path: {{ PATH }}{% endif %}\n"
        )

        f = io.StringIO()
        with redirect_stdout(f):
            read_and_display_followup(followup_file)

        output = f.getvalue()
        assert "Repo: https://example.com/repo" in output
        assert "GitLab:" not in output
        assert "Path:" not in output

    def test_main_happy_path(self, tmp_path, monkeypatch):
        """main() renders the resolved followup.txt and returns success."""
        taskdef = tmp_path / "develop"
        taskdef.mkdir()
        (taskdef / "followup.txt").write_text(
            "Follow up on {{ LMER_TASK_TARGET }}"
        )

        monkeypatch.setenv("LMER_TASK", "develop")
        monkeypatch.setenv("LMER_TASKDEF_DIR", str(taskdef))
        monkeypatch.setenv("LMER_TASK_TARGET", "mr-42")

        f = io.StringIO()
        with redirect_stdout(f):
            main()

        output = f.getvalue()
        assert "Loading follow-up instructions" in output
        assert "Follow up on mr-42" in output
        assert "Follow-up instructions loaded" in output

    def test_main_missing_followup_exits_nonzero(self, tmp_path, monkeypatch):
        """main() exits with code 1 when followup.txt cannot be found."""
        taskdef = tmp_path / "develop"
        taskdef.mkdir()

        monkeypatch.setenv("LMER_TASK", "develop")
        monkeypatch.setenv("LMER_TASKDEF_DIR", str(taskdef))

        f = io.StringIO()
        with redirect_stdout(f):
            with pytest.raises(SystemExit) as excinfo:
                main()

        assert excinfo.value.code == 1
        assert "followup.txt not found" in f.getvalue()

    def test_followup_supports_jinja_includes(self, tmp_path, monkeypatch):
        """Jinja2 `{% include %}` resolves shared templates from the taskdef root."""
        taskdef_root = tmp_path / "taskdef_root"
        shared = taskdef_root / "shared.jinja2"
        taskdef = taskdef_root / "develop"
        taskdef.mkdir(parents=True)
        shared.write_text("shared content")
        (taskdef / "followup.txt").write_text(
            "header\n{% include 'shared.jinja2' %}\nfooter"
        )

        monkeypatch.setenv("LMER_TASK", "develop")
        monkeypatch.setenv("LMER_TASKDEF_DIR", str(taskdef))
        monkeypatch.setenv("LMER_TASKDEF_PATHS", str(taskdef_root))

        f = io.StringIO()
        with redirect_stdout(f):
            main()

        output = f.getvalue()
        assert "header" in output
        assert "shared content" in output
        assert "footer" in output
