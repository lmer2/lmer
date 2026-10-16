#!/usr/bin/env python3
"""Tests for work_repo.utils module"""

import sys
from pathlib import Path

import pytest

from work_repo.utils import (
    LEGACY_FORM,
    CoreUnavailableError,
    core_module,
    instance_memory_dir,
    project_area,
    project_info_dir,
    project_memory_dir,
    sanitize_task_target,
    task_info_dir,
    task_target_dir,
    work_repo_layout,
)

# Env vars the info-dir helpers read; cleared before each helper test so the
# host environment can't leak real LMER_* values into assertions.
# LMER_REPO_URL joined the list with dual-read (issue #353): it is the lookup
# key in a schema-form tree, so a leaked one could resolve a base these
# legacy-tree assertions do not expect.
_INFO_DIR_ENV_VARS = (
    "LMER_WORK_REPO_PATH",
    "LMER_REPO_HOST",
    "LMER_REPO_PROJECT",
    "LMER_REPO_URL",
    "LMER_TASK",
    "LMER_TASK_TARGET",
)


class TestSanitizeTaskTarget:
    """Test sanitize_task_target function"""

    @pytest.mark.parametrize("input_value", ["", None])
    def test_empty_or_none_returns_default(self, input_value):
        """Test empty string or None returns default"""
        assert sanitize_task_target(input_value) == "default"

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://gitlab.example.com/group/project/-/merge_requests/756", "mr-756"),
            ("https://gitlab.com/group/project/-/merge_requests/123?view=diff", "mr-123"),
            ("https://gitlab.com/group/project/-/merge_requests/456", "mr-456"),
        ],
    )
    def test_gitlab_mr_urls(self, url, expected):
        """Test GitLab merge request URLs"""
        assert sanitize_task_target(url) == expected

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://gitlab.com/group/project/-/issues/456", "issue-456"),
            ("https://gitlab.com/group/project/-/issues/789?view=details", "issue-789"),
        ],
    )
    def test_gitlab_issue_urls(self, url, expected):
        """Test GitLab issue URLs"""
        assert sanitize_task_target(url) == expected

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://gitlab.com/group/project/-/work_items/70", "issue-70"),
            ("https://gitlab.example.com/group/project/-/work_items/72", "issue-72"),
            ("https://gitlab.com/group/project/-/work_items/789?work_item_iid=789", "issue-789"),
        ],
    )
    def test_gitlab_work_item_urls(self, url, expected):
        """GitLab work_items URLs (newer issue form) normalize like issues (#72)"""
        assert sanitize_task_target(url) == expected

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://github.com/owner/repo/pull/123", "pr-123"),
            ("https://github.com/owner/repo/pull/456?tab=files", "pr-456"),
        ],
    )
    def test_github_pr_urls(self, url, expected):
        """Test GitHub pull request URLs"""
        assert sanitize_task_target(url) == expected

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://github.com/owner/repo/issues/789", "issue-789"),
            ("https://github.com/owner/repo/issues/101", "issue-101"),
        ],
    )
    def test_github_issue_urls(self, url, expected):
        """Test GitHub issue URLs"""
        assert sanitize_task_target(url) == expected

    @pytest.mark.parametrize(
        "input_value,expected",
        [
            ("feature/new-feature", "feature-new-feature"),
            ("feature/new@feature#123", "feature-new-feature-123"),
            ("bugfix/fix-123", "bugfix-fix-123"),
        ],
    )
    def test_branch_names(self, input_value, expected):
        """Test branch name sanitization"""
        assert sanitize_task_target(input_value) == expected

    @pytest.mark.parametrize(
        "sha",
        [
            "abc123def456",
            "a1b2c3d4e5f6",
            "1234567890abcdef",
        ],
    )
    def test_commit_shas(self, sha):
        """Test commit SHA handling"""
        assert sanitize_task_target(sha) == sha

    def test_full_sha_is_not_truncated_here(self):
        """SHA truncation is a slug concern (run_state.derive_slug), NOT a
        sanitize concern: legacy {task_type}/{target}/ fallback paths are
        built from this function and must keep matching the full-SHA dirs
        that exist on disk."""
        sha = "2a418231d83e1b7f4ff952656724f4de18a15d6e"
        assert sanitize_task_target(sha) == sha

    @pytest.mark.parametrize(
        "input_value",
        [
            "test---multiple----dashes",
            "test--double--dashes",
            "---leading-dashes",
            "trailing-dashes---",
        ],
    )
    def test_multiple_dashes_collapsed(self, input_value):
        """Test multiple dashes are collapsed"""
        result = sanitize_task_target(input_value)
        assert "--" not in result

    @pytest.mark.parametrize(
        "input_value",
        [
            "-test-",
            "--test--",
            "-leading",
            "trailing-",
        ],
    )
    def test_leading_trailing_dashes_removed(self, input_value):
        """Test leading and trailing dashes are removed"""
        result = sanitize_task_target(input_value)
        assert not result.startswith("-")
        assert not result.endswith("-")

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("http://example.com/path/to/resource", "resource"),
            ("https://example.com/path/to/resource", "resource"),
            ("http://example.com/path/to/resource?query=value", "resource"),
            ("https://example.com/path/to/resource#fragment", "resource"),
        ],
    )
    def test_url_fallback(self, url, expected):
        """Test URL fallback behavior for unrecognized URLs"""
        assert sanitize_task_target(url) == expected


@pytest.fixture
def clean_info_env(monkeypatch):
    """Clear all LMER_* env vars the info-dir helpers consult."""
    for name in _INFO_DIR_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


class TestInfoDirHelpers:
    """Test project_info_dir / task_info_dir / task_target_dir helpers."""

    @pytest.mark.parametrize(
        "helper", [project_info_dir, project_memory_dir, task_info_dir, task_target_dir]
    )
    def test_returns_none_when_host_and_project_unset(self, clean_info_env, helper):
        """All helpers return None when host/project env vars are absent."""
        assert helper() is None

    @pytest.mark.parametrize(
        "helper", [project_info_dir, task_info_dir, task_target_dir]
    )
    def test_returns_none_when_only_host_set(self, clean_info_env, helper):
        """Host without project is insufficient -> None."""
        clean_info_env.setenv("LMER_REPO_HOST", "git.example.com")
        assert helper() is None

    @pytest.mark.parametrize(
        "helper", [project_info_dir, task_info_dir, task_target_dir]
    )
    def test_returns_none_when_only_project_set(self, clean_info_env, helper):
        """Project without host is insufficient -> None."""
        clean_info_env.setenv("LMER_REPO_PROJECT", "group/proj")
        assert helper() is None

    def test_project_info_dir_path(self, clean_info_env):
        """project_info_dir assembles {work}/{host}/{project}/info."""
        clean_info_env.setenv("LMER_WORK_REPO_PATH", "/wr")
        clean_info_env.setenv("LMER_REPO_HOST", "git.example.com")
        clean_info_env.setenv("LMER_REPO_PROJECT", "group/proj")
        assert project_info_dir() == Path("/wr/git.example.com/group/proj/info")

    def test_project_memory_dir_path(self, clean_info_env):
        """project_memory_dir assembles {work}/{host}/{project}/memory."""
        clean_info_env.setenv("LMER_WORK_REPO_PATH", "/wr")
        clean_info_env.setenv("LMER_REPO_HOST", "git.example.com")
        clean_info_env.setenv("LMER_REPO_PROJECT", "group/proj")
        assert project_memory_dir() == Path("/wr/git.example.com/group/proj/memory")

    def test_task_info_dir_path(self, clean_info_env):
        """task_info_dir assembles {work}/{host}/{project}/{task}/info."""
        clean_info_env.setenv("LMER_WORK_REPO_PATH", "/wr")
        clean_info_env.setenv("LMER_REPO_HOST", "git.example.com")
        clean_info_env.setenv("LMER_REPO_PROJECT", "group/proj")
        clean_info_env.setenv("LMER_TASK", "develop")
        assert task_info_dir() == Path("/wr/git.example.com/group/proj/develop/info")

    def test_task_target_dir_path(self, clean_info_env):
        """task_target_dir assembles {work}/{host}/{project}/{task}/{target}."""
        clean_info_env.setenv("LMER_WORK_REPO_PATH", "/wr")
        clean_info_env.setenv("LMER_REPO_HOST", "git.example.com")
        clean_info_env.setenv("LMER_REPO_PROJECT", "group/proj")
        clean_info_env.setenv("LMER_TASK", "develop")
        clean_info_env.setenv("LMER_TASK_TARGET", "my-branch")
        assert task_target_dir() == Path(
            "/wr/git.example.com/group/proj/develop/my-branch"
        )

    def test_work_repo_path_defaults_to_work(self, clean_info_env):
        """LMER_WORK_REPO_PATH defaults to /work when unset."""
        clean_info_env.setenv("LMER_REPO_HOST", "git.example.com")
        clean_info_env.setenv("LMER_REPO_PROJECT", "group/proj")
        assert project_info_dir() == Path("/work/git.example.com/group/proj/info")

    def test_task_and_target_default_to_default(self, clean_info_env):
        """LMER_TASK and LMER_TASK_TARGET default to 'default' when unset."""
        clean_info_env.setenv("LMER_WORK_REPO_PATH", "/wr")
        clean_info_env.setenv("LMER_REPO_HOST", "git.example.com")
        clean_info_env.setenv("LMER_REPO_PROJECT", "group/proj")
        assert task_info_dir() == Path(
            "/wr/git.example.com/group/proj/default/info"
        )
        assert task_target_dir() == Path(
            "/wr/git.example.com/group/proj/default/default"
        )

    def test_task_target_dir_sanitizes_target(self, clean_info_env):
        """task_target_dir passes LMER_TASK_TARGET through sanitize_task_target."""
        clean_info_env.setenv("LMER_WORK_REPO_PATH", "/wr")
        clean_info_env.setenv("LMER_REPO_HOST", "git.example.com")
        clean_info_env.setenv("LMER_REPO_PROJECT", "group/proj")
        clean_info_env.setenv("LMER_TASK", "review")
        clean_info_env.setenv(
            "LMER_TASK_TARGET",
            "https://git.example.com/group/proj/-/merge_requests/756",
        )
        assert task_target_dir() == Path(
            "/wr/git.example.com/group/proj/review/mr-756"
        )


class TestLegacyFormHelpers:
    """The dual-read accessors (issue #353 D20) answering for a tree with no
    `_lmer.yaml` — the only form these env-only tests build. Schema form
    and the failure modes live in tests/test_work_repo_schema_base.py."""

    def test_layout_is_legacy_and_names_no_manifest(self, clean_info_env, tmp_path):
        clean_info_env.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
        clean_info_env.setenv("LMER_REPO_HOST", "git.example.com")
        clean_info_env.setenv("LMER_REPO_PROJECT", "group/proj")
        layout = work_repo_layout()
        assert layout.form == LEGACY_FORM
        assert layout.is_schema is False
        assert layout.root == tmp_path
        assert layout.base == tmp_path / "git.example.com" / "group/proj"
        assert layout.project is None
        assert layout.manifest is None
        assert layout.problem is None

    def test_layout_without_run_context(self, clean_info_env, tmp_path):
        """No host/project is no base — the form is still answered."""
        clean_info_env.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
        layout = work_repo_layout()
        assert layout.form == LEGACY_FORM
        assert layout.base is None

    def test_project_area_uses_the_legacy_name(self, clean_info_env):
        clean_info_env.setenv("LMER_WORK_REPO_PATH", "/wr")
        clean_info_env.setenv("LMER_REPO_HOST", "git.example.com")
        clean_info_env.setenv("LMER_REPO_PROJECT", "group/proj")
        assert project_area("runs", "_runs") == Path(
            "/wr/git.example.com/group/proj/runs"
        )
        assert project_area("specs", "_specs") == Path(
            "/wr/git.example.com/group/proj/specs"
        )

    def test_project_area_without_run_context(self, clean_info_env):
        assert project_area("runs", "_runs") is None

    @pytest.mark.parametrize("with_context", [True, False])
    def test_no_instance_memory_scope(self, clean_info_env, with_context):
        """D17's instance scope exists only in schema form; a legacy tree
        keeps every memory type in `{project}/memory/`."""
        clean_info_env.setenv("LMER_WORK_REPO_PATH", "/wr")
        if with_context:
            clean_info_env.setenv("LMER_REPO_HOST", "git.example.com")
            clean_info_env.setenv("LMER_REPO_PROJECT", "group/proj")
        assert instance_memory_dir() is None


class TestCoreModule:
    """The one core import for the tooling verbs (issue #353 D20): `adopt`,
    `migrate` and their successors reach `lmer_schema` through
    `core_module()`, so the import is spelled in `work_repo.utils` alone
    (tests/test_work_repo_schema_base.py guards the siblings)."""

    def test_returns_the_real_module(self):
        import lmer_schema.checks.walk
        import lmer_schema.writer

        assert core_module("writer") is lmer_schema.writer
        assert core_module("checks.walk") is lmer_schema.checks.walk

    def test_a_missing_name_raises_the_one_type(self):
        with pytest.raises(CoreUnavailableError) as info:
            core_module("no_such_module")
        assert "lmer_schema.no_such_module" in str(info.value)
        assert isinstance(info.value.__cause__, ImportError)

    def test_an_absent_core_raises_the_one_type(self, monkeypatch):
        """An old checkout with no core: the single type, an `ImportError`
        underneath so `str()` is still the loader's own text."""
        for name in ("lmer_schema", "lmer_schema.writer"):
            monkeypatch.setitem(sys.modules, name, None)
        with pytest.raises(CoreUnavailableError) as info:
            core_module("writer")
        assert isinstance(info.value, ImportError)
        assert "lmer_schema.writer" in str(info.value)
        assert isinstance(info.value.__cause__, ImportError)
