"""Tests for the claude-agent-files.sh helpers.

The helpers symlink slash commands and skills under ~/.claude from two
sources (lmer global tree + work repository) and merge a limited slice of
the work-repo settings.json into ~/.claude/settings.json.

Tests invoke each helper through a small bash driver that sources the
helpers file and forwards arguments, so file-system effects can be
inspected without running the full claude-runner.sh main flow.
"""
import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent
HELPERS = REPO_ROOT / "libexec" / "claude-agent-files.sh"


def _run_helper(func_name: str, *args: str) -> subprocess.CompletedProcess:
    """Source the helpers file and call <func_name> with <args>."""
    quoted = " ".join(f'"{a}"' for a in args)
    script = f'source "{HELPERS}"\n{func_name} {quoted}\n'
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.fixture
def trees(tmp_path: Path):
    """Build a fake (home_claude, global_src, work_src) trio."""
    home_claude = tmp_path / "home" / ".claude"
    home_claude.mkdir(parents=True)
    global_src = tmp_path / "global" / "agent-files" / "claude"
    work_src = tmp_path / "work" / "agent-files" / "claude"
    return home_claude, global_src, work_src


class TestClaudeLinkAgentFiles:
    """claude_link_agent_files <home_claude> <global_src> <work_src>."""

    def test_links_global_commands_and_skills(self, trees):
        home_claude, global_src, _work_src = trees
        (global_src / "commands").mkdir(parents=True)
        (global_src / "commands" / "rgr.md").write_text("# rgr\n")
        (global_src / "skills" / "review").mkdir(parents=True)
        (global_src / "skills" / "review" / "SKILL.md").write_text("---\nname: review\n---\n")

        result = _run_helper(
            "claude_link_agent_files", str(home_claude), str(global_src), ""
        )
        assert result.returncode == 0, result.stderr

        cmd_link = home_claude / "commands" / "rgr.md"
        assert cmd_link.is_symlink()
        assert cmd_link.resolve() == (global_src / "commands" / "rgr.md").resolve()

        skill_link = home_claude / "skills" / "review"
        assert skill_link.is_symlink()
        assert skill_link.resolve() == (global_src / "skills" / "review").resolve()

    def test_links_work_repo_commands_and_skills(self, trees):
        home_claude, _global_src, work_src = trees
        (work_src / "skills" / "deploy").mkdir(parents=True)
        (work_src / "skills" / "deploy" / "SKILL.md").write_text("---\nname: deploy\n---\n")
        (work_src / "commands").mkdir(parents=True)
        (work_src / "commands" / "ship.md").write_text("# ship\n")

        result = _run_helper(
            "claude_link_agent_files", str(home_claude), "", str(work_src)
        )
        assert result.returncode == 0, result.stderr

        assert (home_claude / "commands" / "ship.md").is_symlink()
        assert (home_claude / "skills" / "deploy").is_symlink()

    def test_work_repo_overrides_global_on_name_collision(self, trees):
        home_claude, global_src, work_src = trees
        (global_src / "commands").mkdir(parents=True)
        (global_src / "commands" / "rgr.md").write_text("GLOBAL\n")
        (work_src / "commands").mkdir(parents=True)
        (work_src / "commands" / "rgr.md").write_text("WORK_REPO\n")

        result = _run_helper(
            "claude_link_agent_files",
            str(home_claude),
            str(global_src),
            str(work_src),
        )
        assert result.returncode == 0, result.stderr

        # Reading through the symlink must return the work-repo content
        linked = home_claude / "commands" / "rgr.md"
        assert linked.is_symlink()
        assert linked.read_text() == "WORK_REPO\n"

    def test_replaces_legacy_dir_symlink(self, trees):
        """Pre-refactor runners left ~/.claude/commands as a dir-symlink."""
        home_claude, global_src, _work_src = trees
        (global_src / "commands").mkdir(parents=True)
        (global_src / "commands" / "rgr.md").write_text("# rgr\n")

        # Simulate the old layout: ~/.claude/commands as a directory symlink
        legacy_target = home_claude.parent / "legacy_commands"
        legacy_target.mkdir()
        (legacy_target / "stale.md").write_text("legacy\n")
        os.symlink(legacy_target, home_claude / "commands")
        assert (home_claude / "commands").is_symlink()

        result = _run_helper(
            "claude_link_agent_files", str(home_claude), str(global_src), ""
        )
        assert result.returncode == 0, result.stderr

        # Now it should be a real directory containing only the global entry
        cmd_dir = home_claude / "commands"
        assert cmd_dir.is_dir() and not cmd_dir.is_symlink()
        assert (cmd_dir / "rgr.md").is_symlink()
        # The stale legacy file must not appear under the new layout
        assert not (cmd_dir / "stale.md").exists()

    def test_no_sources_creates_empty_dirs(self, trees):
        """Both args empty: still create real dirs, no entries."""
        home_claude, _global_src, _work_src = trees
        result = _run_helper(
            "claude_link_agent_files", str(home_claude), "", ""
        )
        assert result.returncode == 0, result.stderr
        assert (home_claude / "commands").is_dir()
        assert (home_claude / "skills").is_dir()
        assert list((home_claude / "commands").iterdir()) == []
        assert list((home_claude / "skills").iterdir()) == []

    def test_missing_source_subdir_is_skipped(self, trees):
        """If only commands/ exists in a source (no skills/), skills target stays empty."""
        home_claude, global_src, _work_src = trees
        (global_src / "commands").mkdir(parents=True)
        (global_src / "commands" / "rgr.md").write_text("# rgr\n")
        # No skills/ subdir

        result = _run_helper(
            "claude_link_agent_files", str(home_claude), str(global_src), ""
        )
        assert result.returncode == 0, result.stderr
        assert (home_claude / "commands" / "rgr.md").is_symlink()
        assert (home_claude / "skills").is_dir()
        assert list((home_claude / "skills").iterdir()) == []

    def test_idempotent_re_run(self, trees):
        """Running twice should leave the same result and not error."""
        home_claude, global_src, _work_src = trees
        (global_src / "commands").mkdir(parents=True)
        (global_src / "commands" / "rgr.md").write_text("# rgr\n")

        for _ in range(2):
            result = _run_helper(
                "claude_link_agent_files", str(home_claude), str(global_src), ""
            )
            assert result.returncode == 0, result.stderr

        link = home_claude / "commands" / "rgr.md"
        assert link.is_symlink()
        assert link.resolve() == (global_src / "commands" / "rgr.md").resolve()


class TestClaudeMergeWorkSettings:
    """claude_merge_work_settings <home_claude> <work_src>."""

    def _write_settings(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))

    def test_appends_permissions_allow(self, trees):
        home_claude, _global_src, work_src = trees
        self._write_settings(
            home_claude / "settings.json",
            {"permissions": {"allow": ["Bash(ls:*)"], "deny": ["Bash(rm:*)"]}},
        )
        self._write_settings(
            work_src / "settings.json",
            {"permissions": {"allow": ["Bash(make:*)"]}},
        )

        result = _run_helper(
            "claude_merge_work_settings", str(home_claude), str(work_src)
        )
        assert result.returncode == 0, result.stderr

        merged = json.loads((home_claude / "settings.json").read_text())
        assert sorted(merged["permissions"]["allow"]) == sorted(
            ["Bash(ls:*)", "Bash(make:*)"]
        )
        # Deny list and other fields must be preserved from the base
        assert merged["permissions"]["deny"] == ["Bash(rm:*)"]

    def test_deduplicates_overlapping_allow_entries(self, trees):
        home_claude, _global_src, work_src = trees
        self._write_settings(
            home_claude / "settings.json",
            {"permissions": {"allow": ["Bash(ls:*)", "Bash(make:*)"]}},
        )
        self._write_settings(
            work_src / "settings.json",
            {"permissions": {"allow": ["Bash(make:*)", "Bash(go:*)"]}},
        )

        result = _run_helper(
            "claude_merge_work_settings", str(home_claude), str(work_src)
        )
        assert result.returncode == 0, result.stderr

        merged = json.loads((home_claude / "settings.json").read_text())
        assert sorted(merged["permissions"]["allow"]) == sorted(
            ["Bash(ls:*)", "Bash(make:*)", "Bash(go:*)"]
        )

    def test_does_not_merge_work_repo_deny(self, trees):
        """The work repo must not be able to deny things the global config allows."""
        home_claude, _global_src, work_src = trees
        self._write_settings(
            home_claude / "settings.json",
            {"permissions": {"allow": ["Bash(ls:*)"], "deny": []}},
        )
        self._write_settings(
            work_src / "settings.json",
            {"permissions": {"allow": [], "deny": ["Bash(ls:*)"]}},
        )

        result = _run_helper(
            "claude_merge_work_settings", str(home_claude), str(work_src)
        )
        assert result.returncode == 0, result.stderr

        merged = json.loads((home_claude / "settings.json").read_text())
        assert merged["permissions"]["deny"] == []  # Work-repo deny ignored

    def test_replaces_symlink_settings_file(self, trees):
        """settings.json may be a symlink to a read-only mount — replace with regular file."""
        home_claude, _global_src, work_src = trees
        real_settings = home_claude.parent / "real_settings.json"
        self._write_settings(real_settings, {"permissions": {"allow": ["Bash(ls:*)"]}})
        os.symlink(real_settings, home_claude / "settings.json")
        self._write_settings(
            work_src / "settings.json",
            {"permissions": {"allow": ["Bash(make:*)"]}},
        )

        result = _run_helper(
            "claude_merge_work_settings", str(home_claude), str(work_src)
        )
        assert result.returncode == 0, result.stderr

        settings_path = home_claude / "settings.json"
        assert not settings_path.is_symlink()
        merged = json.loads(settings_path.read_text())
        assert sorted(merged["permissions"]["allow"]) == sorted(
            ["Bash(ls:*)", "Bash(make:*)"]
        )
        # The original real_settings file must not have been mutated
        original = json.loads(real_settings.read_text())
        assert original["permissions"]["allow"] == ["Bash(ls:*)"]

    def test_missing_work_settings_is_noop(self, trees):
        home_claude, _global_src, work_src = trees
        self._write_settings(
            home_claude / "settings.json",
            {"permissions": {"allow": ["Bash(ls:*)"]}},
        )
        # work_src has no settings.json
        work_src.mkdir(parents=True)

        result = _run_helper(
            "claude_merge_work_settings", str(home_claude), str(work_src)
        )
        assert result.returncode == 0, result.stderr

        # Unchanged
        merged = json.loads((home_claude / "settings.json").read_text())
        assert merged["permissions"]["allow"] == ["Bash(ls:*)"]

    def test_missing_base_settings_warns_and_skips(self, trees):
        """No base settings file: warn the operator and skip the merge."""
        home_claude, _global_src, work_src = trees
        self._write_settings(
            work_src / "settings.json",
            {"permissions": {"allow": ["Bash(make:*)"]}},
        )

        result = _run_helper(
            "claude_merge_work_settings", str(home_claude), str(work_src)
        )
        assert result.returncode == 0, result.stderr
        assert not (home_claude / "settings.json").exists()
        # The warning is the only signal a work-repo maintainer has that
        # their permissions.allow entry was dropped.
        assert "no global settings.json" in result.stdout


class TestHelpersFileExistsAndSourceableByRunner:
    """Guard against accidental rename/move of the helpers file."""

    def test_helpers_file_exists(self):
        assert HELPERS.is_file(), f"Missing helpers file: {HELPERS}"

    def test_runner_sources_the_helpers_file(self):
        runner = REPO_ROOT / "libexec" / "claude-runner.sh"
        text = runner.read_text()
        # Require an actual `source`/`.` invocation on a non-comment line —
        # the previous substring check matched the `# shellcheck source=…`
        # comment, so the test passed even if the real source line was deleted.
        pattern = re.compile(
            r"^[^#\n]*\b(?:source|\.)\s+[^\n]*claude-agent-files\.sh",
            re.MULTILINE,
        )
        assert pattern.search(text), (
            "claude-runner.sh must source claude-agent-files.sh on a real "
            "(non-comment) line — otherwise the helper functions won't be "
            "defined when the main flow calls them."
        )
