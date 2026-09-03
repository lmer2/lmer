"""Tests for the work repo's agent-files tree having a second name (D18).

Two shell surfaces resolve the work repo's agent-files tree: harness-common.sh
(codex/pi config provisioning and prompt-template rendering) and
claude-runner.sh, which computes ``WORK_AGENT_FILES`` (fed to
claude_link_agent_files and claude_merge_work_settings). In schema form the
tree is ``_agents/`` (holding ``claude/``, ``mirrors/``, ``memory/``); the
legacy bare ``agent-files/`` is read in place until the tree migrates, and
``LMER_WORK_AGENT_FILES_ROOT`` keeps overriding both.

Both surfaces go through one resolver, ``harness_work_agent_files_root
<leaf> [work-dir]``. The migration is not one move -- spec §9 step 3 is three
independent ones, so ``_agents/`` can exist (holding only ``memory/``) while
``agent-files/claude/`` is still the live tree. Resolution is therefore PER
LEAF: the root that actually holds the requested leaf wins,
``_agents/<leaf>`` before ``agent-files/<leaf>``; when both hold it the
schema form is read and a one-line notice on stderr names the ignored legacy
copy, so a session never loses its skills silently.

claude-runner.sh does not source harness-common.sh (it keeps inline copies of
the shared steps by design), so the resolver is mirrored verbatim and a drift
guard here keeps the two copies identical -- the shell analogue of the
taskdefs / hooks/start.py AST guard in test_masterplan_provisioning.py.

Precedence under test: explicit LMER_WORK_AGENT_FILES_ROOT (taken as-is,
blank when the path is absent, no leaf check) > ``_agents/`` holding the leaf
> ``agent-files/`` holding the leaf > blank. The work dir defaults to
``$LMER_WORK_REPO_PATH`` (``/work``).
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests._claude_runner_harness import run_claude_runner, skip_if_npm_claude_present
from tests.test_harness_runners import _skip_if_real_binary, run_harness_runner


LIBEXEC = Path(__file__).parent.parent / "libexec"
HARNESS_COMMON = LIBEXEC / "harness-common.sh"
CLAUDE_RUNNER = LIBEXEC / "claude-runner.sh"
RESOLVER = "harness_work_agent_files_root"

# Real-container default: the resolver falls back to /work when neither a
# work dir nor LMER_WORK_REPO_PATH is given. Tests never write there, so the
# expectation is computed from whatever the filesystem actually holds.
REAL_WORK = Path("/work")


def _function_text(script: Path) -> str:
    """Extract the resolver's definition (``name() {`` through the closing
    ``}`` at column 0) from a shell script. The shell has no ast module; the
    definition text is the unit the drift guard compares."""
    match = re.search(
        rf"^{RESOLVER}\(\) \{{\n.*?^\}}$", script.read_text(), re.MULTILINE | re.DOTALL
    )
    assert match, f"{RESOLVER} not defined in {script}"
    return match.group(0)


def _resolve(source: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Define the resolver from <source> and call it with <args>.

    harness-common.sh is sourced (as the runners do); claude-runner.sh cannot
    be sourced without running its main flow, so its copy is evaluated from
    the extracted definition text -- which proves that text is what the
    guard says it is: a self-contained, runnable function.
    """
    if source == HARNESS_COMMON:
        preamble = f'source "{HARNESS_COMMON}"\n'
    else:
        preamble = _function_text(source) + "\n"
    quoted = " ".join(f'"{a}"' for a in args)
    run_env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/")}
    if env:
        run_env.update(env)
    result = subprocess.run(
        ["bash", "-c", f"{preamble}{RESOLVER} {quoted}\n"],
        env=run_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result


def _run_resolver(source: Path, *args: str, env: dict | None = None) -> str:
    return _resolve(source, *args, env=env).stdout.strip()


def _expected_real_work_root(leaf: str = "claude") -> str:
    for name in ("_agents", "agent-files"):
        if (REAL_WORK / name / leaf).exists():
            return str(REAL_WORK / name)
    return ""


@pytest.fixture
def work(tmp_path: Path) -> Path:
    """A fake work repo root with neither agent-files spelling present."""
    root = tmp_path / "work"
    root.mkdir()
    return root


# ── The resolver (both copies) ────────────────────────────────────────

@pytest.mark.parametrize("source", [HARNESS_COMMON, CLAUDE_RUNNER], ids=["harness-common", "claude-runner"])
class TestResolverPrecedence:
    """harness_work_agent_files_root <leaf> [work-dir] -> echoes the root
    holding <leaf>, or nothing."""

    def test_schema_form_found(self, source, work):
        (work / "_agents" / "claude").mkdir(parents=True)
        assert _run_resolver(source, "claude", str(work)) == str(work / "_agents")

    def test_legacy_form_found(self, source, work):
        (work / "agent-files" / "claude").mkdir(parents=True)
        assert _run_resolver(source, "claude", str(work)) == str(work / "agent-files")

    def test_schema_form_wins_when_both_hold_the_leaf(self, source, work):
        (work / "_agents" / "claude").mkdir(parents=True)
        (work / "agent-files" / "claude").mkdir(parents=True)
        assert _run_resolver(source, "claude", str(work)) == str(work / "_agents")

    def test_both_hold_the_leaf_notice_names_both(self, source, work):
        """The schema form is read, but the ignored legacy copy is named on
        stderr (stdout is the return channel) -- a shadowed tree must never
        be silent."""
        (work / "_agents" / "claude").mkdir(parents=True)
        (work / "agent-files" / "claude").mkdir(parents=True)
        result = _resolve(source, "claude", str(work))
        assert result.stdout.strip() == str(work / "_agents")
        assert result.stderr.count("\n") == 1, result.stderr
        assert str(work / "_agents" / "claude") in result.stderr
        assert str(work / "agent-files" / "claude") in result.stderr

    def test_partial_migration_reads_the_tree_holding_the_leaf(self, source, work):
        """Spec §9 step 3 lifts memory independently of agent-files: a
        `_agents/` holding only `memory/` must NOT hide the live
        `agent-files/claude/` (this is the reviewer's reproduction)."""
        (work / "_agents" / "memory").mkdir(parents=True)
        (work / "agent-files" / "claude" / "skills" / "my-skill").mkdir(parents=True)
        result = _resolve(source, "claude", str(work))
        assert result.stdout.strip() == str(work / "agent-files")
        assert result.stderr == ""

    def test_each_leaf_resolves_independently(self, source, work):
        (work / "_agents" / "codex").mkdir(parents=True)
        (work / "agent-files" / "claude").mkdir(parents=True)
        assert _run_resolver(source, "codex", str(work)) == str(work / "_agents")
        assert _run_resolver(source, "claude", str(work)) == str(work / "agent-files")

    def test_single_tree_is_silent(self, source, work):
        (work / "_agents" / "claude").mkdir(parents=True)
        assert _resolve(source, "claude", str(work)).stderr == ""

    def test_leaf_in_neither_tree_is_blank(self, source, work):
        (work / "_agents" / "memory").mkdir(parents=True)
        (work / "agent-files" / "codex").mkdir(parents=True)
        assert _run_resolver(source, "claude", str(work)) == ""

    def test_neither_present_is_blank(self, source, work):
        assert _run_resolver(source, "claude", str(work)) == ""

    def test_env_override_wins_over_both_trees(self, source, work, tmp_path):
        (work / "_agents" / "claude").mkdir(parents=True)
        (work / "agent-files" / "claude").mkdir(parents=True)
        override = tmp_path / "elsewhere"
        override.mkdir()
        assert (
            _run_resolver(source, "claude", str(work), env={"LMER_WORK_AGENT_FILES_ROOT": str(override)})
            == str(override)
        )

    def test_env_override_is_taken_as_is_without_a_leaf_check(self, source, work, tmp_path):
        """The override names the root; whether it holds the leaf is the
        consumer's check (claude-runner blanks WORK_AGENT_FILES when
        `<root>/claude` is absent -- see TestClaudeRunnerSurface)."""
        override = tmp_path / "elsewhere"
        (override / "memory").mkdir(parents=True)
        assert (
            _run_resolver(source, "claude", str(work), env={"LMER_WORK_AGENT_FILES_ROOT": str(override)})
            == str(override)
        )

    def test_env_override_pointing_nowhere_is_blank_not_a_fallthrough(self, source, work, tmp_path):
        """An explicit override that does not exist blanks the root (the
        pre-D18 contract the runner tests rely on to isolate from /work);
        it must NOT fall through to a tree the caller pointed away from."""
        (work / "_agents" / "claude").mkdir(parents=True)
        (work / "agent-files" / "claude").mkdir(parents=True)
        assert (
            _run_resolver(
                source, "claude", str(work),
                env={"LMER_WORK_AGENT_FILES_ROOT": str(tmp_path / "no-such-dir")},
            )
            == ""
        )

    def test_empty_env_override_is_unset(self, source, work):
        """`LMER_WORK_AGENT_FILES_ROOT=` behaves like unset (``:-`` semantics
        of the pre-D18 default)."""
        (work / "_agents" / "claude").mkdir(parents=True)
        assert (
            _run_resolver(source, "claude", str(work), env={"LMER_WORK_AGENT_FILES_ROOT": ""})
            == str(work / "_agents")
        )

    def test_work_dir_defaults_to_lmer_work_repo_path(self, source, work):
        """No work-dir argument -> $LMER_WORK_REPO_PATH, the in-container
        clone location every other reader of the work repo honours."""
        (work / "agent-files" / "claude").mkdir(parents=True)
        assert (
            _run_resolver(source, "claude", env={"LMER_WORK_REPO_PATH": str(work)})
            == str(work / "agent-files")
        )

    def test_default_work_dir_is_the_container_work_repo(self, source):
        assert _run_resolver(source, "claude") == _expected_real_work_root("claude")


# ── Drift guard ──────────────────────────────────────────────────────

class TestResolverMirrored:
    def test_claude_runner_copy_matches_harness_common(self):
        """claude-runner.sh does not source harness-common.sh, so the
        resolver is mirrored rather than shared -- keep the definitions
        byte-identical (same pattern as the taskdefs / hooks/start.py guard)."""
        assert _function_text(CLAUDE_RUNNER) == _function_text(HARNESS_COMMON), (
            f"{RESOLVER} diverged between claude-runner.sh and harness-common.sh"
        )

    def test_both_surfaces_go_through_the_resolver(self):
        """Neither surface may grow its own spelling of the default again,
        and neither may go back to a leafless whole-tree root."""
        common = HARNESS_COMMON.read_text()
        runner = CLAUDE_RUNNER.read_text()
        assert f'WORK_AGENT_FILES_ROOT="$({RESOLVER} claude)"' in runner
        assert f'$({RESOLVER} "${{rel%%/*}}")' in common, "harness_provision_config must resolve per leaf"
        assert f'$({RESOLVER} claude)' in common, "harness_render_prompt_templates must resolve the claude leaf"
        for text in (common, runner):
            assert f'$({RESOLVER})' not in text, "leafless (whole-tree) call"
            assert "/work/agent-files" not in text
            assert "/work/_agents" not in text


# ── harness-common.sh surface (codex/pi) ─────────────────────────────

class TestHarnessCommonSurface:
    def _provision(self, tmp_path, rel, target, env):
        script = (
            f'source "{HARNESS_COMMON}"\n'
            "harness_find_global_dir\n"
            f'harness_provision_config "{rel}" "{target}"\n'
        )
        run_env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "LMER_GLOBAL_DIR": str(tmp_path / "no-global"),
        }
        run_env.update(env)
        result = subprocess.run(
            ["bash", "-c", script],
            env=run_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return result

    def test_provision_config_reads_a_schema_form_root(self, tmp_path):
        """harness_provision_config finds `<root>/<harness>/<file>` under a
        root spelled `_agents` exactly as it did under `agent-files`."""
        agents = tmp_path / "work" / "_agents"
        (agents / "codex").mkdir(parents=True)
        (agents / "codex" / "config.toml").write_text("# schema-form\n")
        target = tmp_path / "target" / "config.toml"
        self._provision(
            tmp_path, "codex/config.toml", target, {"LMER_WORK_AGENT_FILES_ROOT": str(agents)},
        )
        assert target.read_text() == "# schema-form\n"

    def test_provision_config_resolves_per_leaf_from_the_work_repo(self, tmp_path):
        """A partially migrated work repo (`_agents/memory` lifted,
        `agent-files/codex` still live) provisions from the legacy tree."""
        work = tmp_path / "work"
        (work / "_agents" / "memory").mkdir(parents=True)
        (work / "agent-files" / "codex").mkdir(parents=True)
        (work / "agent-files" / "codex" / "config.toml").write_text("# legacy\n")
        target = tmp_path / "target" / "config.toml"
        result = self._provision(
            tmp_path, "codex/config.toml", target, {"LMER_WORK_REPO_PATH": str(work)},
        )
        assert target.read_text() == "# legacy\n"
        assert f"from {work / 'agent-files' / 'codex' / 'config.toml'}" in result.stdout
        assert result.stderr == ""

    def test_provision_config_notice_when_both_trees_hold_the_leaf(self, tmp_path):
        work = tmp_path / "work"
        for root in ("_agents", "agent-files"):
            (work / root / "codex").mkdir(parents=True)
            (work / root / "codex" / "config.toml").write_text(f"# {root}\n")
        target = tmp_path / "target" / "config.toml"
        result = self._provision(
            tmp_path, "codex/config.toml", target, {"LMER_WORK_REPO_PATH": str(work)},
        )
        assert target.read_text() == "# _agents\n"
        assert str(work / "_agents" / "codex") in result.stderr
        assert str(work / "agent-files" / "codex") in result.stderr

    @_skip_if_real_binary("codex")
    def test_codex_runner_provisions_from_schema_form_root(self, tmp_path):
        agents = tmp_path / "work" / "_agents"
        (agents / "codex").mkdir(parents=True)
        (agents / "codex" / "config.toml").write_text("# schema-form\n")
        run_harness_runner(
            "codex", tmp_path, env={"LMER_WORK_AGENT_FILES_ROOT": str(agents)},
        )
        assert (tmp_path / ".codex" / "config.toml").read_text() == "# schema-form\n"

    @_skip_if_real_binary("codex")
    def test_codex_runner_reads_a_partially_migrated_work_repo(self, tmp_path):
        """End to end through codex-runner.sh: `_agents/memory` beside a
        live `agent-files/codex` must not blank the codex config."""
        work = tmp_path / "work"
        (work / "_agents" / "memory").mkdir(parents=True)
        (work / "agent-files" / "codex").mkdir(parents=True)
        (work / "agent-files" / "codex" / "config.toml").write_text("# legacy\n")
        run_harness_runner(
            "codex", tmp_path,
            # The harness default pins the override to a non-existent path
            # to isolate from /work; blank it (== unset) so the work-repo
            # resolution is exercised against the tmp work repo instead.
            env={"LMER_WORK_AGENT_FILES_ROOT": "", "LMER_WORK_REPO_PATH": str(work)},
        )
        assert (tmp_path / ".codex" / "config.toml").read_text() == "# legacy\n"


# ── claude-runner.sh surface ─────────────────────────────────────────

@skip_if_npm_claude_present
class TestClaudeRunnerSurface:
    """claude-runner.sh resolves `<root>/claude` from the root holding the
    `claude` leaf. The fake trees stay empty so nothing from tmp_path is
    linked into the real ~/.claude (the runner pins HOME); the resolved path
    is asserted from the runner's own status line."""

    def test_schema_form_claude_tree_is_used(self, tmp_path):
        agents = tmp_path / "work" / "_agents"
        (agents / "claude").mkdir(parents=True)
        result = run_claude_runner(tmp_path, env={"LMER_WORK_AGENT_FILES_ROOT": str(agents)})
        assert f"Work agent-files: {agents / 'claude'}" in result.output

    def test_root_without_a_claude_tree_is_blank(self, tmp_path):
        agents = tmp_path / "work" / "_agents"
        (agents / "memory").mkdir(parents=True)
        result = run_claude_runner(tmp_path, env={"LMER_WORK_AGENT_FILES_ROOT": str(agents)})
        assert "Work agent-files:" not in result.output

    def test_override_pointing_nowhere_is_blank(self, tmp_path):
        result = run_claude_runner(
            tmp_path, env={"LMER_WORK_AGENT_FILES_ROOT": str(tmp_path / "no-such-dir")},
        )
        assert "Work agent-files:" not in result.output

    def test_partial_migration_keeps_the_legacy_claude_tree(self, tmp_path):
        """`_agents/memory` lifted first (spec §9 step 3) must not make the
        runner drop `agent-files/claude` -- the reviewer's scenario, driven
        through the runner via LMER_WORK_REPO_PATH."""
        work = tmp_path / "work"
        (work / "_agents" / "memory").mkdir(parents=True)
        (work / "agent-files" / "claude").mkdir(parents=True)
        result = run_claude_runner(tmp_path, env={"LMER_WORK_REPO_PATH": str(work)})
        assert f"Work agent-files: {work / 'agent-files' / 'claude'}" in result.output

    def test_both_claude_trees_present_reads_schema_form_and_says_so(self, tmp_path):
        work = tmp_path / "work"
        (work / "_agents" / "claude").mkdir(parents=True)
        (work / "agent-files" / "claude").mkdir(parents=True)
        result = run_claude_runner(tmp_path, env={"LMER_WORK_REPO_PATH": str(work)})
        assert f"Work agent-files: {work / '_agents' / 'claude'}" in result.output
        assert str(work / "agent-files" / "claude") in result.output
