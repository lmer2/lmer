"""Test start hook functionality with Jinja2 rendering."""
import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
import pytest

from hooks.start import main, read_and_display_instructions


class TestStartHook:
    """Test start hook Jinja2 rendering and work mode functionality."""

    def test_jinja2_rendering_with_lmer_env_vars(self, tmp_path, monkeypatch):
        """Test that Jinja2 templates render with LMER_* environment variables."""
        # Setup test environment
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("LMER_REPO_URL", "https://example.com/repo")
        monkeypatch.setenv("LMER_TASK_TARGET", "target123")
        monkeypatch.setenv("OTHER_VAR", "should_not_appear")  # Non-LMER var

        # Create test instructions file
        instructions_file = tmp_path / "instructions.txt"
        instructions_file.write_text(
            "Repository: {{ LMER_REPO_URL }}\n"
            "Target: {{ LMER_TASK_TARGET }}\n"
        )

        # Mock Path.home() for timestamp file
        with patch('hooks.start.Path.home', return_value=tmp_path):
            # Capture print output
            f = io.StringIO()
            with redirect_stdout(f):
                read_and_display_instructions(instructions_file, "finish")

            output = f.getvalue()
            assert "Repository: https://example.com/repo" in output
            assert "Target: target123" in output
            assert "should_not_appear" not in output  # Non-LMER vars shouldn't appear

    def test_work_mode_defaults_to_finish(self, tmp_path, monkeypatch):
        """Test that work_mode defaults to 'finish'."""
        monkeypatch.setenv("HOME", str(tmp_path))

        instructions_file = tmp_path / "instructions.txt"
        instructions_file.write_text("Work mode: {{ work_mode }}")

        with patch('hooks.start.Path.home', return_value=tmp_path):
            f = io.StringIO()
            with redirect_stdout(f):
                read_and_display_instructions(instructions_file, "finish")

            output = f.getvalue()
            assert "Work mode: finish" in output

    def test_work_mode_phasic(self, tmp_path, monkeypatch):
        """Test that work_mode can be set to 'phasic'."""
        monkeypatch.setenv("HOME", str(tmp_path))

        instructions_file = tmp_path / "instructions.txt"
        instructions_file.write_text("Work mode: {{ work_mode }}")

        with patch('hooks.start.Path.home', return_value=tmp_path):
            f = io.StringIO()
            with redirect_stdout(f):
                read_and_display_instructions(instructions_file, "phasic")

            output = f.getvalue()
            assert "Work mode: phasic" in output

    def test_computed_values_available(self, tmp_path, monkeypatch):
        """Test that computed values (taskdef_name, instructions_file) are available."""
        monkeypatch.setenv("HOME", str(tmp_path))

        taskdef_dir = tmp_path / "taskdef" / "test_task"
        taskdef_dir.mkdir(parents=True)
        instructions_file = taskdef_dir / "instructions.txt"
        instructions_file.write_text(
            "Taskdef: {{ taskdef_name }}\n"
            "File: {{ instructions_file }}"
        )

        with patch('hooks.start.Path.home', return_value=tmp_path):
            f = io.StringIO()
            with redirect_stdout(f):
                read_and_display_instructions(instructions_file, "finish")

            output = f.getvalue()
            assert "Taskdef: test_task" in output
            assert "File:" in output
            assert str(instructions_file) in output

    def test_jinja2_conditionals_with_work_mode(self, tmp_path, monkeypatch):
        """Test Jinja2 conditionals work with work_mode."""
        monkeypatch.setenv("HOME", str(tmp_path))

        instructions_file = tmp_path / "instructions.txt"
        instructions_file.write_text(
            "{% if work_mode == 'phasic' %}\n"
            "This is phasic mode\n"
            "{% else %}\n"
            "This is finish mode\n"
            "{% endif %}"
        )

        with patch('hooks.start.Path.home', return_value=tmp_path):
            # Test finish mode
            f = io.StringIO()
            with redirect_stdout(f):
                read_and_display_instructions(instructions_file, "finish")
            output = f.getvalue()
            assert "This is finish mode" in output
            assert "This is phasic mode" not in output

            # Test phasic mode
            f = io.StringIO()
            with redirect_stdout(f):
                read_and_display_instructions(instructions_file, "phasic")
            output = f.getvalue()
            assert "This is phasic mode" in output
            assert "This is finish mode" not in output

    def test_only_lmer_env_vars_included(self, tmp_path, monkeypatch):
        """Test that only LMER_* environment variables are included in context."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("LMER_REPO_URL", "https://example.com/repo")
        monkeypatch.setenv("GITLAB_HOST", "gitlab.com")  # Should not be included
        monkeypatch.setenv("PATH", "/usr/bin")  # Should not be included

        instructions_file = tmp_path / "instructions.txt"
        instructions_file.write_text(
            "{% if LMER_REPO_URL %}Repo: {{ LMER_REPO_URL }}{% endif %}\n"
            "{% if GITLAB_HOST %}GitLab: {{ GITLAB_HOST }}{% endif %}\n"
            "{% if PATH %}Path: {{ PATH }}{% endif %}"
        )

        with patch('hooks.start.Path.home', return_value=tmp_path):
            f = io.StringIO()
            with redirect_stdout(f):
                read_and_display_instructions(instructions_file, "finish")

            output = f.getvalue()
            assert "Repo: https://example.com/repo" in output
            assert "GitLab:" not in output  # GITLAB_HOST should not be available
            assert "Path:" not in output  # PATH should not be available

    def test_main_with_work_mode_parameter(self, tmp_path, monkeypatch):
        """Test main() function parses work_mode parameter correctly."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("LMER_TASK", "test_task")

        instructions_file = tmp_path / "taskdef" / "test_task" / "instructions.txt"
        instructions_file.parent.mkdir(parents=True)
        instructions_file.write_text("Work mode: {{ work_mode }}")

        # Mock find_taskdef_instructions to return our test file
        with patch('hooks.start.find_taskdef_instructions', return_value=instructions_file):
            with patch('hooks.start.Path.home', return_value=tmp_path):
                # Test with phasic mode
                with patch('sys.argv', ['start.py', 'phasic']):
                    f = io.StringIO()
                    with redirect_stdout(f):
                        main()
                    output = f.getvalue()
                    assert "work mode: phasic" in output.lower()
                    assert "Work mode: phasic" in output

    def test_main_defaults_to_finish(self, tmp_path, monkeypatch):
        """Test main() defaults to 'finish' when no parameter provided."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("LMER_TASK", "test_task")

        instructions_file = tmp_path / "taskdef" / "test_task" / "instructions.txt"
        instructions_file.parent.mkdir(parents=True)
        instructions_file.write_text("Work mode: {{ work_mode }}")

        with patch('hooks.start.find_taskdef_instructions', return_value=instructions_file):
            with patch('hooks.start.Path.home', return_value=tmp_path):
                # Test with no arguments
                with patch('sys.argv', ['start.py']):
                    f = io.StringIO()
                    with redirect_stdout(f):
                        main()
                    output = f.getvalue()
                    assert "work mode: finish" in output.lower()
                    assert "Work mode: finish" in output

    def test_main_invalid_work_mode_warning(self, tmp_path, monkeypatch):
        """Test main() warns and defaults when invalid work_mode provided."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("LMER_TASK", "test_task")

        instructions_file = tmp_path / "taskdef" / "test_task" / "instructions.txt"
        instructions_file.parent.mkdir(parents=True)
        instructions_file.write_text("Work mode: {{ work_mode }}")

        with patch('hooks.start.find_taskdef_instructions', return_value=instructions_file):
            with patch('hooks.start.Path.home', return_value=tmp_path):
                # Test with invalid mode
                with patch('sys.argv', ['start.py', 'invalid_mode']):
                    f = io.StringIO()
                    with redirect_stdout(f):
                        main()
                    output = f.getvalue()
                    assert "WARNING" in output or "warning" in output.lower()
                    assert "invalid_mode" in output
                    assert "Work mode: finish" in output  # Should default to finish
