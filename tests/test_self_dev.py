"""Test self-development mode detection and Jinja2 rendering."""
import io
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest

from hooks.start import read_and_display_instructions
from lmer_cli.runtime import _is_lmer_pyproject


class TestSelfDevJinja2:
    """Test Jinja2 conditional rendering for self-development mode."""

    def test_self_dev_block_shown_when_enabled(self, tmp_path, monkeypatch):
        """When LMER_SELF_DEV=1, the self-development block is rendered."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("LMER_SELF_DEV", "1")

        instructions_file = tmp_path / "instructions.txt"
        instructions_file.write_text(
            '{% if LMER_SELF_DEV == "1" %}\n'
            "Self-Development Mode Active\n"
            "{% endif %}\n"
            "Normal content\n"
        )

        with patch("hooks.start.Path.home", return_value=tmp_path):
            f = io.StringIO()
            with redirect_stdout(f):
                read_and_display_instructions(instructions_file, "finish")

            output = f.getvalue()
            assert "Self-Development Mode Active" in output
            assert "Normal content" in output

    def test_self_dev_block_hidden_when_disabled(self, tmp_path, monkeypatch):
        """When LMER_SELF_DEV=0, the self-development block is not rendered."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("LMER_SELF_DEV", "0")

        instructions_file = tmp_path / "instructions.txt"
        instructions_file.write_text(
            '{% if LMER_SELF_DEV == "1" %}\n'
            "Self-Development Mode Active\n"
            "{% endif %}\n"
            "Normal content\n"
        )

        with patch("hooks.start.Path.home", return_value=tmp_path):
            f = io.StringIO()
            with redirect_stdout(f):
                read_and_display_instructions(instructions_file, "finish")

            output = f.getvalue()
            assert "Self-Development Mode Active" not in output
            assert "Normal content" in output

    def test_self_dev_block_hidden_when_unset(self, tmp_path, monkeypatch):
        """When LMER_SELF_DEV is not set at all, the block is not rendered."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("LMER_SELF_DEV", raising=False)

        instructions_file = tmp_path / "instructions.txt"
        instructions_file.write_text(
            '{% if LMER_SELF_DEV == "1" %}\n'
            "Self-Development Mode Active\n"
            "{% endif %}\n"
            "Normal content\n"
        )

        with patch("hooks.start.Path.home", return_value=tmp_path):
            f = io.StringIO()
            with redirect_stdout(f):
                read_and_display_instructions(instructions_file, "finish")

            output = f.getvalue()
            assert "Self-Development Mode Active" not in output
            assert "Normal content" in output


class TestSelfDevDetection:
    """Test pyproject.toml detection logic via _is_lmer_pyproject."""

    def test_detects_lmer_project(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "lmer"\nversion = "1.0.0"\n')
        assert _is_lmer_pyproject(pyproject) is True

    def test_detects_lmer_cli_project(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "lmer-cli"\nversion = "1.0.0"\n')
        assert _is_lmer_pyproject(pyproject) is True

    def test_rejects_other_project(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "some-other-project"\nversion = "1.0.0"\n')
        assert _is_lmer_pyproject(pyproject) is False

    def test_handles_missing_project_section(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[tool.pytest]\naddopts = '-v'\n")
        assert _is_lmer_pyproject(pyproject) is False

    def test_handles_invalid_toml(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("this is not valid toml {{{}}")
        assert _is_lmer_pyproject(pyproject) is False

    def test_handles_missing_file(self, tmp_path):
        pyproject = tmp_path / "nonexistent" / "pyproject.toml"
        assert _is_lmer_pyproject(pyproject) is False
