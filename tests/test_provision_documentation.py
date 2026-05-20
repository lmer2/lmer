"""Tests for documentation provisioning in clone_and_exec and gate-check handling."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from lmer_cli.container.clone_and_exec import provision_documentation
from lmer_cli.gates import GateSystem, CheckStatus


class TestProvisionDocumentation:
    """Test provision_documentation() function."""

    def _init_git_repo(self, path: Path) -> None:
        """Initialize a git repo at the given path."""
        subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(path), "config", "user.email", "test@test.com"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(path), "config", "user.name", "Test"],
            check=True, capture_output=True,
        )

    def test_provisions_claude_md_from_global(self, tmp_path):
        """Missing AGENTS.md is copied from lmer global."""
        workspace = tmp_path / "workspace"
        work_repo = tmp_path / "work"
        global_path = tmp_path / "global"

        workspace.mkdir()
        work_repo.mkdir()
        global_path.mkdir()
        self._init_git_repo(workspace)

        # Create global AGENTS.md
        (global_path / "AGENTS.md").write_text("# Global CLAUDE config")

        provisioned = provision_documentation(workspace, work_repo, global_path)

        assert "AGENTS.md" in provisioned
        assert (workspace / "AGENTS.md").read_text() == "# Global CLAUDE config"

    def test_provisions_rules_from_global(self, tmp_path):
        """Missing rules/ files are copied from lmer global."""
        workspace = tmp_path / "workspace"
        work_repo = tmp_path / "work"
        global_path = tmp_path / "global"

        workspace.mkdir()
        work_repo.mkdir()
        global_path.mkdir()
        self._init_git_repo(workspace)

        # Create global rules
        (global_path / "rules").mkdir()
        (global_path / "rules" / "git.md").write_text("# Git rules")
        (global_path / "rules" / "testing.md").write_text("# Testing rules")

        provisioned = provision_documentation(workspace, work_repo, global_path)

        assert "rules/git.md" in provisioned
        assert "rules/testing.md" in provisioned
        assert (workspace / "rules" / "git.md").read_text() == "# Git rules"
        assert (workspace / "rules" / "testing.md").read_text() == "# Testing rules"

    def test_project_files_not_overwritten(self, tmp_path):
        """Project's own files are never overwritten (highest priority)."""
        workspace = tmp_path / "workspace"
        work_repo = tmp_path / "work"
        global_path = tmp_path / "global"

        workspace.mkdir()
        work_repo.mkdir()
        global_path.mkdir()
        self._init_git_repo(workspace)

        # Project has its own AGENTS.md
        (workspace / "AGENTS.md").write_text("# Project-specific config")
        # Global also has one
        (global_path / "AGENTS.md").write_text("# Global config")

        provisioned = provision_documentation(workspace, work_repo, global_path)

        assert "AGENTS.md" not in provisioned
        assert (workspace / "AGENTS.md").read_text() == "# Project-specific config"

    def test_work_repo_overrides_global(self, tmp_path, monkeypatch):
        """Work repo files take priority over lmer global."""
        workspace = tmp_path / "workspace"
        work_repo = tmp_path / "work"
        global_path = tmp_path / "global"

        workspace.mkdir()
        work_repo.mkdir()
        global_path.mkdir()
        self._init_git_repo(workspace)

        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
        monkeypatch.setenv("LMER_REPO_PROJECT", "myorg/myproject")

        # Global AGENTS.md
        (global_path / "AGENTS.md").write_text("# Global config")

        # Work repo AGENTS.md (higher priority)
        work_info = work_repo / "git.example.com" / "myorg" / "myproject" / "info"
        work_info.mkdir(parents=True)
        (work_info / "AGENTS.md").write_text("# Work repo project config")

        provisioned = provision_documentation(workspace, work_repo, global_path)

        assert "AGENTS.md" in provisioned
        assert (workspace / "AGENTS.md").read_text() == "# Work repo project config"

    def test_work_repo_rules_override_global(self, tmp_path, monkeypatch):
        """Work repo rules/ files take priority over lmer global rules."""
        workspace = tmp_path / "workspace"
        work_repo = tmp_path / "work"
        global_path = tmp_path / "global"

        workspace.mkdir()
        work_repo.mkdir()
        global_path.mkdir()
        self._init_git_repo(workspace)

        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
        monkeypatch.setenv("LMER_REPO_PROJECT", "myorg/myproject")

        # Global rules
        (global_path / "rules").mkdir()
        (global_path / "rules" / "testing.md").write_text("# Global testing rules")

        # Work repo rules (higher priority)
        work_rules = work_repo / "git.example.com" / "myorg" / "myproject" / "info" / "rules"
        work_rules.mkdir(parents=True)
        (work_rules / "testing.md").write_text("# Project-specific testing rules")

        provisioned = provision_documentation(workspace, work_repo, global_path)

        assert "rules/testing.md" in provisioned
        assert (workspace / "rules" / "testing.md").read_text() == "# Project-specific testing rules"

    def test_git_exclude_updated(self, tmp_path):
        """Provisioned files are added to .git/info/exclude."""
        workspace = tmp_path / "workspace"
        work_repo = tmp_path / "work"
        global_path = tmp_path / "global"

        workspace.mkdir()
        work_repo.mkdir()
        global_path.mkdir()
        self._init_git_repo(workspace)

        (global_path / "AGENTS.md").write_text("# Global config")

        provision_documentation(workspace, work_repo, global_path)

        exclude_file = workspace / ".git" / "info" / "exclude"
        exclude_content = exclude_file.read_text()
        assert "AGENTS.md" in exclude_content
        assert ".lmer-provisioned-docs" in exclude_content
        assert "# lmer provisioned documentation" in exclude_content

    def test_marker_file_written(self, tmp_path):
        """A .lmer-provisioned-docs marker file is created listing provisioned files."""
        workspace = tmp_path / "workspace"
        work_repo = tmp_path / "work"
        global_path = tmp_path / "global"

        workspace.mkdir()
        work_repo.mkdir()
        global_path.mkdir()
        self._init_git_repo(workspace)

        (global_path / "AGENTS.md").write_text("# Global config")
        (global_path / "rules").mkdir()
        (global_path / "rules" / "git.md").write_text("# Git rules")

        provision_documentation(workspace, work_repo, global_path)

        marker = workspace / ".lmer-provisioned-docs"
        assert marker.exists()
        marker_content = marker.read_text()
        assert "AGENTS.md" in marker_content
        assert "rules/git.md" in marker_content

    def test_nothing_provisioned_when_all_present(self, tmp_path):
        """No provisioning when project has all files."""
        workspace = tmp_path / "workspace"
        work_repo = tmp_path / "work"
        global_path = tmp_path / "global"

        workspace.mkdir()
        work_repo.mkdir()
        global_path.mkdir()
        self._init_git_repo(workspace)

        # Project has everything
        (workspace / "AGENTS.md").write_text("# Project config")
        (workspace / "rules").mkdir()
        (workspace / "rules" / "git.md").write_text("# Project git rules")

        # Global also has them
        (global_path / "AGENTS.md").write_text("# Global config")
        (global_path / "rules").mkdir()
        (global_path / "rules" / "git.md").write_text("# Global git rules")

        provisioned = provision_documentation(workspace, work_repo, global_path)

        assert provisioned == []
        assert not (workspace / ".lmer-provisioned-docs").exists()

    def test_no_global_path_does_not_fail(self, tmp_path):
        """Gracefully handles missing global path content."""
        workspace = tmp_path / "workspace"
        work_repo = tmp_path / "work"
        global_path = tmp_path / "global"

        workspace.mkdir()
        work_repo.mkdir()
        global_path.mkdir()
        self._init_git_repo(workspace)

        # No AGENTS.md or rules in global either
        provisioned = provision_documentation(workspace, work_repo, global_path)
        assert provisioned == []

    def test_self_dev_workspace_skipped(self, tmp_path):
        """Self-dev workspace (lmer repo itself) is left untouched."""
        workspace = tmp_path / "workspace"
        work_repo = tmp_path / "work"
        global_path = tmp_path / "global"

        workspace.mkdir()
        work_repo.mkdir()
        global_path.mkdir()
        self._init_git_repo(workspace)

        # Mark workspace as the lmer repo itself
        (workspace / "pyproject.toml").write_text(
            '[project]\nname = "lmer"\nversion = "0.0.0"\n'
        )

        # Global has docs that would normally be provisioned
        (global_path / "AGENTS.md").write_text("# Global config")
        (global_path / "rules").mkdir()
        (global_path / "rules" / "git.md").write_text("# Global git rules")

        provisioned = provision_documentation(workspace, work_repo, global_path)

        # Nothing provisioned, no marker, no exclude pollution
        assert provisioned == []
        assert not (workspace / "AGENTS.md").exists()
        assert not (workspace / "rules" / "git.md").exists()
        assert not (workspace / ".lmer-provisioned-docs").exists()
        exclude_file = workspace / ".git" / "info" / "exclude"
        if exclude_file.exists():
            content = exclude_file.read_text()
            assert "# lmer provisioned documentation" not in content
            assert ".lmer-provisioned-docs" not in content

    def test_self_dev_workspace_skipped_for_lmer_cli_name(self, tmp_path):
        """Self-dev detection also recognizes the 'lmer-cli' project name."""
        workspace = tmp_path / "workspace"
        work_repo = tmp_path / "work"
        global_path = tmp_path / "global"

        workspace.mkdir()
        work_repo.mkdir()
        global_path.mkdir()
        self._init_git_repo(workspace)

        (workspace / "pyproject.toml").write_text(
            '[project]\nname = "lmer-cli"\nversion = "0.0.0"\n'
        )

        (global_path / "AGENTS.md").write_text("# Global config")

        provisioned = provision_documentation(workspace, work_repo, global_path)

        assert provisioned == []
        assert not (workspace / "AGENTS.md").exists()

    def test_non_lmer_pyproject_still_provisions(self, tmp_path):
        """A workspace with a pyproject.toml for some other project still gets provisioning."""
        workspace = tmp_path / "workspace"
        work_repo = tmp_path / "work"
        global_path = tmp_path / "global"

        workspace.mkdir()
        work_repo.mkdir()
        global_path.mkdir()
        self._init_git_repo(workspace)

        (workspace / "pyproject.toml").write_text(
            '[project]\nname = "some-other-project"\nversion = "0.0.0"\n'
        )

        (global_path / "AGENTS.md").write_text("# Global config")

        provisioned = provision_documentation(workspace, work_repo, global_path)

        assert "AGENTS.md" in provisioned
        assert (workspace / "AGENTS.md").read_text() == "# Global config"

    def test_log_message_shows_per_file_source(self, tmp_path, monkeypatch, capsys):
        """Log message attributes each file to its actual source."""
        workspace = tmp_path / "workspace"
        work_repo = tmp_path / "work"
        global_path = tmp_path / "global"

        workspace.mkdir()
        work_repo.mkdir()
        global_path.mkdir()
        self._init_git_repo(workspace)

        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
        monkeypatch.setenv("LMER_REPO_PROJECT", "myorg/myproject")

        # AGENTS.md from work repo
        work_info = work_repo / "git.example.com" / "myorg" / "myproject" / "info"
        work_info.mkdir(parents=True)
        (work_info / "AGENTS.md").write_text("# Work repo config")

        # rules/git.md from global only
        (global_path / "AGENTS.md").write_text("# Global config")
        (global_path / "rules").mkdir()
        (global_path / "rules" / "git.md").write_text("# Global git rules")

        provision_documentation(workspace, work_repo, global_path)

        stderr = capsys.readouterr().err
        assert "AGENTS.md (work-repo)" in stderr
        assert "rules/git.md (lmer-defaults)" in stderr

    def test_provisioned_files_excluded_from_git_status(self, tmp_path):
        """Provisioned files do not show in git status."""
        workspace = tmp_path / "workspace"
        work_repo = tmp_path / "work"
        global_path = tmp_path / "global"

        workspace.mkdir()
        work_repo.mkdir()
        global_path.mkdir()
        self._init_git_repo(workspace)

        # Create an initial commit so git status works cleanly
        (workspace / "README.md").write_text("# Test")
        subprocess.run(["git", "-C", str(workspace), "add", "README.md"],
                        check=True, capture_output=True)
        subprocess.run(["git", "-C", str(workspace), "commit", "-m", "init"],
                        check=True, capture_output=True)

        (global_path / "AGENTS.md").write_text("# Global config")
        (global_path / "rules").mkdir()
        (global_path / "rules" / "git.md").write_text("# Git rules")

        provision_documentation(workspace, work_repo, global_path)

        # git status should not show provisioned files
        result = subprocess.run(
            ["git", "-C", str(workspace), "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        )
        assert "AGENTS.md" not in result.stdout
        assert "rules/git.md" not in result.stdout
        assert ".lmer-provisioned-docs" not in result.stdout


class TestGateCheckProvisionedDocs:
    """Test that gate-check handles provisioned documentation correctly."""

    def test_warning_when_docs_provisioned(self, tmp_path):
        """Gate-check shows WARNING when docs are lmer-provisioned."""
        # Create required files
        (tmp_path / "README.md").write_text("# Test")
        (tmp_path / "AGENTS.md").write_text("# Config")
        (tmp_path / "rules").mkdir()
        (tmp_path / "rules" / "git.md").write_text("# Git")
        (tmp_path / "rules" / "testing.md").write_text("# Testing")

        # Write provisioned marker
        (tmp_path / ".lmer-provisioned-docs").write_text("AGENTS.md\nrules/git.md\n")

        gate = GateSystem()
        gate.project_root = tmp_path
        result = gate.check_documentation()

        assert result.status == CheckStatus.WARNING
        assert "lmer default" in result.message.lower()
        assert not result.is_critical

    def test_passed_when_no_provisioned_marker(self, tmp_path):
        """Gate-check shows PASSED when all docs are project-native."""
        (tmp_path / "README.md").write_text("# Test")
        (tmp_path / "AGENTS.md").write_text("# Config")
        (tmp_path / "rules").mkdir()
        (tmp_path / "rules" / "git.md").write_text("# Git")
        (tmp_path / "rules" / "testing.md").write_text("# Testing")

        gate = GateSystem()
        gate.project_root = tmp_path
        result = gate.check_documentation()

        assert result.status == CheckStatus.PASSED

    def test_failed_when_docs_still_missing(self, tmp_path):
        """Gate-check still fails when docs are missing entirely."""
        gate = GateSystem()
        gate.project_root = tmp_path
        result = gate.check_documentation()

        assert result.status == CheckStatus.FAILED
        assert "Critical documentation missing" in result.message

    def test_warning_only_for_provisioned_required_docs(self, tmp_path):
        """Warning only triggers when provisioned docs overlap with required docs."""
        (tmp_path / "README.md").write_text("# Test")
        (tmp_path / "AGENTS.md").write_text("# Config")
        (tmp_path / "rules").mkdir()
        (tmp_path / "rules" / "git.md").write_text("# Git")
        (tmp_path / "rules" / "testing.md").write_text("# Testing")

        # Only non-required docs are provisioned
        (tmp_path / ".lmer-provisioned-docs").write_text("rules/security.md\n")

        gate = GateSystem()
        gate.project_root = tmp_path
        result = gate.check_documentation()

        assert result.status == CheckStatus.PASSED
