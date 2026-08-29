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

from lmer_cli.container.dispatch_agents import ENV_PREFIX, LANE_AGENTS


REPO_ROOT = Path(__file__).parent.parent
HELPERS = REPO_ROOT / "libexec" / "claude-agent-files.sh"


def _run_helper(
    func_name: str, *args: str, env: dict | None = None
) -> subprocess.CompletedProcess:
    """Source the helpers file and call <func_name> with <args>.

    `env` entries overlay os.environ for the bash subprocess (used by the
    dispatch-lane tests to plant LMER_DISPATCH_* and point PYTHONPATH at
    the development tree).
    """
    quoted = " ".join(f'"{a}"' for a in args)
    script = f'source "{HELPERS}"\n{func_name} {quoted}\n'
    merged_env = None
    if env is not None:
        merged_env = {**os.environ, **env}
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env=merged_env,
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
        for subdir in ("commands", "skills", "output-styles"):
            assert (home_claude / subdir).is_dir(), subdir
            assert list((home_claude / subdir).iterdir()) == [], subdir

    def test_missing_source_subdir_is_skipped(self, trees):
        """If only commands/ exists in a source, the other targets stay empty."""
        home_claude, global_src, _work_src = trees
        (global_src / "commands").mkdir(parents=True)
        (global_src / "commands" / "rgr.md").write_text("# rgr\n")
        # No skills/ or output-styles/ subdir

        result = _run_helper(
            "claude_link_agent_files", str(home_claude), str(global_src), ""
        )
        assert result.returncode == 0, result.stderr
        assert (home_claude / "commands" / "rgr.md").is_symlink()
        for subdir in ("skills", "output-styles"):
            assert (home_claude / subdir).is_dir(), subdir
            assert list((home_claude / subdir).iterdir()) == [], subdir

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

    def test_links_agents_from_global(self, trees):
        home_claude, global_src, work_src = trees
        (global_src / "agents").mkdir(parents=True)
        (global_src / "agents" / "explorer.md").write_text("global explorer")
        result = _run_helper(
            "claude_link_agent_files", str(home_claude), str(global_src), ""
        )
        assert result.returncode == 0, result.stderr
        linked = home_claude / "agents" / "explorer.md"
        assert linked.is_symlink()
        assert linked.read_text() == "global explorer"

    def test_work_agents_override_global(self, trees):
        home_claude, global_src, work_src = trees
        (global_src / "agents").mkdir(parents=True)
        (global_src / "agents" / "explorer.md").write_text("global explorer")
        (work_src / "agents").mkdir(parents=True)
        (work_src / "agents" / "explorer.md").write_text("work explorer")
        result = _run_helper(
            "claude_link_agent_files", str(home_claude), str(global_src), str(work_src)
        )
        assert result.returncode == 0, result.stderr
        assert (home_claude / "agents" / "explorer.md").read_text() == "work explorer"

    def test_links_output_styles_from_global(self, trees):
        """Issue #249: a style in the lmer tree reaches ~/.claude/output-styles/."""
        home_claude, global_src, _work_src = trees
        (global_src / "output-styles").mkdir(parents=True)
        (global_src / "output-styles" / "terse.md").write_text(
            "---\nname: terse\n---\nglobal terse\n"
        )
        result = _run_helper(
            "claude_link_agent_files", str(home_claude), str(global_src), ""
        )
        assert result.returncode == 0, result.stderr
        linked = home_claude / "output-styles" / "terse.md"
        assert linked.is_symlink()
        assert linked.resolve() == (global_src / "output-styles" / "terse.md").resolve()
        assert linked.read_text() == "---\nname: terse\n---\nglobal terse\n"

    def test_links_output_styles_from_work_repo(self, trees):
        """A style shipped by the work repo reaches the same directory."""
        home_claude, _global_src, work_src = trees
        (work_src / "output-styles").mkdir(parents=True)
        (work_src / "output-styles" / "runbook.md").write_text(
            "---\nname: runbook\n---\nwork runbook\n"
        )
        result = _run_helper(
            "claude_link_agent_files", str(home_claude), "", str(work_src)
        )
        assert result.returncode == 0, result.stderr
        linked = home_claude / "output-styles" / "runbook.md"
        assert linked.is_symlink()
        assert linked.read_text() == "---\nname: runbook\n---\nwork runbook\n"

    def test_work_output_styles_override_global(self, trees):
        home_claude, global_src, work_src = trees
        (global_src / "output-styles").mkdir(parents=True)
        (global_src / "output-styles" / "terse.md").write_text("global terse")
        (work_src / "output-styles").mkdir(parents=True)
        (work_src / "output-styles" / "terse.md").write_text("work terse")
        result = _run_helper(
            "claude_link_agent_files", str(home_claude), str(global_src), str(work_src)
        )
        assert result.returncode == 0, result.stderr
        assert (home_claude / "output-styles" / "terse.md").read_text() == "work terse"


class TestClaudeRenderDispatchLanes:
    """The post-link dispatch-lane render pass (LMER_DISPATCH_<LANE>).

    claude_link_agent_files calls claude_render_dispatch_lanes at the end;
    a configured lane's agent symlink becomes a real file with the
    configured model/effort in its frontmatter, an unset lane keeps (or is
    restored to) the bare symlink. The python side needs the development
    tree on PYTHONPATH.
    """

    ENV = {"PYTHONPATH": str(REPO_ROOT / "src")}

    def _lane_env(self, **lanes: str) -> dict:
        env = dict(self.ENV)
        # Explicitly blank every lane so ambient config can't leak in.
        for lane in ("REVIEW", "DESIGN", "CODE", "MECHANICAL", "EXPLORE"):
            env[f"LMER_DISPATCH_{lane}"] = lanes.get(lane, "")
        return env

    def _agents_tree(self, trees):
        home_claude, global_src, work_src = trees
        (global_src / "agents").mkdir(parents=True)
        for stem in ("adversarial-reviewer", "designer", "coder",
                     "mechanical", "explorer"):
            (global_src / "agents" / f"{stem}.md").write_text(
                f"---\nname: {stem}\ntools: Read\n---\n# {stem}\n"
            )
        return home_claude, global_src, work_src

    def test_configured_lane_materializes_real_file(self, trees):
        home_claude, global_src, _ = self._agents_tree(trees)
        result = _run_helper(
            "claude_link_agent_files", str(home_claude), str(global_src), "",
            env=self._lane_env(REVIEW="fable:high"),
        )
        assert result.returncode == 0, result.stderr
        target = home_claude / "agents" / "adversarial-reviewer.md"
        assert not target.is_symlink()
        text = target.read_text()
        assert "model: fable" in text
        assert "effort: high" in text
        assert "Dispatch lane REVIEW" in result.stdout

    def test_unset_lanes_stay_symlinks(self, trees):
        home_claude, global_src, _ = self._agents_tree(trees)
        result = _run_helper(
            "claude_link_agent_files", str(home_claude), str(global_src), "",
            env=self._lane_env(CODE="sonnet"),
        )
        assert result.returncode == 0, result.stderr
        assert not (home_claude / "agents" / "coder.md").is_symlink()
        for stem in ("adversarial-reviewer", "designer", "mechanical", "explorer"):
            assert (home_claude / "agents" / f"{stem}.md").is_symlink(), stem

    def test_no_config_is_pure_link_pass(self, trees):
        home_claude, global_src, _ = self._agents_tree(trees)
        result = _run_helper(
            "claude_link_agent_files", str(home_claude), str(global_src), "",
            env=self._lane_env(),
        )
        assert result.returncode == 0, result.stderr
        for stem in ("adversarial-reviewer", "designer", "coder",
                     "mechanical", "explorer"):
            assert (home_claude / "agents" / f"{stem}.md").is_symlink(), stem
        assert "Dispatch lane" not in result.stdout

    def test_set_then_unset_restores_symlink(self, trees):
        """The staleness transition across two provisioning runs."""
        home_claude, global_src, _ = self._agents_tree(trees)
        _run_helper(
            "claude_link_agent_files", str(home_claude), str(global_src), "",
            env=self._lane_env(EXPLORE="haiku"),
        )
        target = home_claude / "agents" / "explorer.md"
        assert not target.is_symlink()
        result = _run_helper(
            "claude_link_agent_files", str(home_claude), str(global_src), "",
            env=self._lane_env(),
        )
        assert result.returncode == 0, result.stderr
        assert target.is_symlink()

    def test_work_overlay_wins_as_render_source(self, trees):
        home_claude, global_src, work_src = self._agents_tree(trees)
        (work_src / "agents").mkdir(parents=True)
        (work_src / "agents" / "explorer.md").write_text(
            "---\nname: explorer\n---\n# overlay explorer\n"
        )
        result = _run_helper(
            "claude_link_agent_files", str(home_claude), str(global_src),
            str(work_src), env=self._lane_env(EXPLORE="sonnet:low"),
        )
        assert result.returncode == 0, result.stderr
        text = (home_claude / "agents" / "explorer.md").read_text()
        assert "# overlay explorer" in text
        assert "model: sonnet" in text
        assert "effort: low" in text

    def test_explorer_effective_def_unpinned_when_unset(self, trees):
        """G3: with EXPLORE unset the def explorer actually loads has no pin."""
        home_claude, global_src, _ = self._agents_tree(trees)
        result = _run_helper(
            "claude_link_agent_files", str(home_claude), str(global_src), "",
            env=self._lane_env(),
        )
        assert result.returncode == 0, result.stderr
        effective = (home_claude / "agents" / "explorer.md").read_text()
        frontmatter = effective.split("---")[1]
        assert "model:" not in frontmatter

    def test_bash_stem_list_matches_lane_agents(self):
        """Drift guard: the skip-gate's two bash-side copies of the lane
        list in claude-agent-files.sh must match the python side's
        LANE_AGENTS — a lane added to one but not the other silently loses
        its staleness repair (stem loop) or, worse, has its configuration
        silently ignored when it is the only lane set (env-var gate)."""
        source = HELPERS.read_text()
        match = re.search(r"for stem in ([^;]+); do", source)
        assert match, "skip-gate stem loop not found in claude-agent-files.sh"
        bash_stems = sorted(match.group(1).split())
        assert bash_stems == sorted(LANE_AGENTS.values())

        prefix = re.escape(ENV_PREFIX)
        gate = re.search(
            rf'if \[ -n "((?:\$\{{{prefix}[A-Z]+\}})+)" \]', source
        )
        assert gate, "env-var skip-gate not found in claude-agent-files.sh"
        gate_lanes = sorted(re.findall(rf"{prefix}([A-Z]+)", gate.group(1)))
        assert gate_lanes == sorted(LANE_AGENTS.keys())

    def test_dangling_lane_symlink_triggers_repair(self, trees):
        """A dangling lane symlink (overlay deleted between sessions) must
        be repaired to the surviving source even with no lane configured."""
        home_claude, global_src, work_src = self._agents_tree(trees)
        (work_src / "agents").mkdir(parents=True)
        overlay = work_src / "agents" / "explorer.md"
        overlay.write_text("---\nname: explorer\n---\noverlay\n")
        _run_helper(
            "claude_link_agent_files", str(home_claude), str(global_src),
            str(work_src), env=self._lane_env(),
        )
        overlay.unlink()  # the overlay disappears; the link now dangles
        result = _run_helper(
            "claude_link_agent_files", str(home_claude), str(global_src), "",
            env=self._lane_env(),
        )
        assert result.returncode == 0, result.stderr
        target = home_claude / "agents" / "explorer.md"
        assert target.is_symlink()
        assert target.resolve() == (global_src / "agents" / "explorer.md").resolve()

    def test_render_failure_is_fail_soft(self, trees):
        home_claude, global_src, _ = self._agents_tree(trees)
        env = self._lane_env(REVIEW="fable")
        env["LMER_PYTHON"] = "/nonexistent/python3"  # render pass cannot run
        result = _run_helper(
            "claude_link_agent_files", str(home_claude), str(global_src), "",
            env=env,
        )
        assert result.returncode == 0, result.stderr
        assert "Dispatch lane render failed" in result.stdout
        # The linked layout still stands.
        assert (home_claude / "agents" / "adversarial-reviewer.md").is_symlink()


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

    def test_does_not_merge_work_repo_output_style(self, trees):
        """A work repo may SHIP an output style (Issue #249) but not SELECT one.

        Honoring outputStyle from the work repo would let a shared repo replace
        the main agent's system prompt — a far bigger lever than the permission
        grants #48 limited this merge to. This assertion is what keeps that
        decision true in code; docs/LMER-CLI.md carries the rationale.
        """
        home_claude, _global_src, work_src = trees
        self._write_settings(
            home_claude / "settings.json",
            {"permissions": {"allow": ["Bash(ls:*)"]}},
        )
        self._write_settings(
            work_src / "settings.json",
            {
                "outputStyle": "work-repo-style",
                "permissions": {"allow": ["Bash(make:*)"]},
            },
        )

        result = _run_helper(
            "claude_merge_work_settings", str(home_claude), str(work_src)
        )
        assert result.returncode == 0, result.stderr

        merged = json.loads((home_claude / "settings.json").read_text())
        assert "outputStyle" not in merged
        # The permission grant it shipped alongside still lands, so this is
        # about the key and not about the merge having been skipped.
        assert sorted(merged["permissions"]["allow"]) == sorted(
            ["Bash(ls:*)", "Bash(make:*)"]
        )

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


class TestClaudeApplyOutputStyle:
    """claude_apply_output_style <settings_file> <styles_dir> (issue #257).

    Two properties: the key lands in the effective settings file whatever else
    merged into it, and an unrecognised name warns rather than refuses.
    """

    def _write_settings(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))

    def _apply(self, settings: Path, styles: Path, style: str | None):
        env = {} if style is None else {"LMER_CLAUDE_OUTPUT_STYLE": style}
        return _run_helper(
            "claude_apply_output_style", str(settings), str(styles), env=env
        )

    def test_unset_variable_leaves_settings_untouched(self, trees):
        home_claude, _global_src, _work_src = trees
        settings = home_claude / "settings.json"
        self._write_settings(settings, {"permissions": {"allow": ["Bash(ls:*)"]}})
        before = settings.read_text()

        result = self._apply(settings, home_claude / "output-styles", None)
        assert result.returncode == 0, result.stderr
        assert settings.read_text() == before
        assert "outputStyle" not in result.stdout

    def test_sets_the_key_and_keeps_the_rest_of_the_file(self, trees):
        home_claude, _global_src, _work_src = trees
        settings = home_claude / "settings.json"
        styles = home_claude / "output-styles"
        styles.mkdir(parents=True)
        (styles / "napkin.md").write_text("---\nname: napkin\n---\nbody\n")
        self._write_settings(
            settings,
            {"permissions": {"allow": ["Bash(ls:*)"], "deny": ["Bash(rm:*)"]}},
        )

        result = self._apply(settings, styles, "napkin")
        assert result.returncode == 0, result.stderr

        merged = json.loads(settings.read_text())
        assert merged["outputStyle"] == "napkin"
        assert merged["permissions"]["allow"] == ["Bash(ls:*)"]
        assert merged["permissions"]["deny"] == ["Bash(rm:*)"]
        assert "⚠️" not in result.stdout

    def test_replaces_a_symlinked_settings_file(self, trees):
        """The read-only-mount case the sibling merges also handle."""
        home_claude, global_src, _work_src = trees
        real = global_src / "settings.json"
        self._write_settings(real, {"permissions": {"allow": ["Bash(ls:*)"]}})
        settings = home_claude / "settings.json"
        settings.symlink_to(real)

        result = self._apply(settings, home_claude / "output-styles", "default")
        assert result.returncode == 0, result.stderr
        assert not settings.is_symlink()
        assert json.loads(settings.read_text())["outputStyle"] == "default"
        # The mount is untouched — writing through the link would have edited it.
        assert "outputStyle" not in json.loads(real.read_text())

    def test_missing_settings_file_is_created_with_only_the_key(self, trees):
        home_claude, _global_src, _work_src = trees
        settings = home_claude / "nested" / "settings.json"

        result = self._apply(settings, home_claude / "output-styles", "default")
        assert result.returncode == 0, result.stderr
        assert json.loads(settings.read_text()) == {"outputStyle": "default"}

    @pytest.mark.parametrize(
        "filename, contents, requested",
        [
            # The file stem, with and without matching case.
            ("napkin.md", "---\nname: Something Else\n---\n", "napkin"),
            ("Napkin.md", "---\nname: Something Else\n---\n", "napkin"),
            # The frontmatter name — the other spelling claude could match.
            ("style-one.md", "---\nname: napkin\n---\n", "napkin"),
            ("style-two.md", '---\nname: "napkin"\n---\n', "napkin"),
            ("style-three.md", "---\nname: napkin  \n---\n", "napkin"),
        ],
    )
    def test_plausible_names_do_not_warn(self, trees, filename, contents, requested):
        home_claude, _global_src, _work_src = trees
        settings = home_claude / "settings.json"
        self._write_settings(settings, {})
        styles = home_claude / "output-styles"
        styles.mkdir(parents=True)
        (styles / filename).write_text(contents)

        result = self._apply(settings, styles, requested)
        assert result.returncode == 0, result.stderr
        assert "⚠️" not in result.stdout, result.stdout
        assert json.loads(settings.read_text())["outputStyle"] == requested

    @pytest.mark.parametrize("builtin", ["default", "Explanatory", "Learning"])
    def test_builtin_names_do_not_warn_without_any_style_files(self, trees, builtin):
        """No styles directory at all is normal for a built-in."""
        home_claude, _global_src, _work_src = trees
        settings = home_claude / "settings.json"
        self._write_settings(settings, {})

        result = self._apply(settings, home_claude / "output-styles", builtin)
        assert result.returncode == 0, result.stderr
        assert "⚠️" not in result.stdout, result.stdout
        assert json.loads(settings.read_text())["outputStyle"] == builtin

    def test_unknown_name_warns_and_still_sets_the_key(self, trees):
        home_claude, _global_src, _work_src = trees
        settings = home_claude / "settings.json"
        self._write_settings(settings, {})
        styles = home_claude / "output-styles"
        styles.mkdir(parents=True)
        (styles / "napkin.md").write_text("---\nname: napkin\n---\n")

        result = self._apply(settings, styles, "nope")
        # Warning, not failure: claude's own resolution is the authority.
        assert result.returncode == 0, result.stderr
        assert "⚠️" in result.stdout
        assert "nope" in result.stdout
        assert json.loads(settings.read_text())["outputStyle"] == "nope"

    def test_style_name_is_not_a_pattern(self, trees):
        """An operator-supplied name is text, never a regex reaching a matcher."""
        home_claude, _global_src, _work_src = trees
        settings = home_claude / "settings.json"
        self._write_settings(settings, {})
        styles = home_claude / "output-styles"
        styles.mkdir(parents=True)
        (styles / "napkin.md").write_text("---\nname: napkin\n---\n")

        result = self._apply(settings, styles, ".*")
        assert result.returncode == 0, result.stderr
        assert "⚠️" in result.stdout, "a wildcard must not match a real style"
        assert json.loads(settings.read_text())["outputStyle"] == ".*"

    def test_runner_applies_it_after_the_settings_local_merge(self):
        """Order is the feature. Read from the runner source, because the merges
        it outranks are in the main flow these tests do not execute."""
        runner = REPO_ROOT / "libexec" / "claude-runner.sh"
        text = runner.read_text()
        apply_at = text.index("claude_apply_output_style \"$SETTINGS_FILE\"")
        for earlier in (
            "claude_merge_work_settings",
            "Merging personal permissions from settings.local.json",
        ):
            assert text.index(earlier) < apply_at, (
                f"{earlier} must run before claude_apply_output_style, or the "
                "merge would overwrite the selected outputStyle"
            )

    def test_cli_env_dict_declares_output_style(self):
        """Guard the container passthrough: the runner that reads it runs inside
        the container (env-var convention step 4)."""
        source = (REPO_ROOT / "src" / "lmer_cli" / "cli.py").read_text()
        pattern = re.compile(
            r"""["']LMER_CLAUDE_OUTPUT_STYLE["']\s*:\s*os\.environ\.get\(\s*"""
            r"""["']LMER_CLAUDE_OUTPUT_STYLE["']\s*\)"""
        )
        assert pattern.search(source), (
            "LMER_CLAUDE_OUTPUT_STYLE entry missing from cli.py's container env dict"
        )
