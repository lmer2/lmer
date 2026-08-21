"""Tests for the non-claude harness runner scripts (libexec/*-runner.sh) and
the harness-aware runner dispatch in clone_and_exec.py.

Follows the stub-binary pattern of tests/_claude_runner_harness.py: each
runner is executed with a fake harness binary on PATH that records its argv
and environment, a scratch HOME, and the work/global agent-files roots pointed
away from the real container layout so provisioning is fully controlled.
"""

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from lmer_cli.container import clone_and_exec
from lmer_cli.harness import HARNESSES
from tests._claude_runner_harness import run_claude_runner, skip_if_npm_claude_present

LIBEXEC = Path(__file__).parent.parent / "libexec"
SRC = Path(__file__).parent.parent / "src"


def _global_claude_command(tmp_path, name, content):
    """Create a fake global lmer dir carrying agent-files/claude/commands/<name>."""
    global_dir = tmp_path / "global"
    commands = global_dir / "agent-files" / "claude" / "commands"
    commands.mkdir(parents=True, exist_ok=True)
    (commands / name).write_text(content)
    return global_dir


def _stub_work_cli(tmp_path):
    """Drop a `work` stub into the runner's fake bin dir, recording its argv."""
    record = tmp_path / "work-calls.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    stub = fake_bin / "work"
    stub.write_text(f'#!/bin/bash\necho "$@" >> "{record}"\nexit 0\n')
    _make_executable(stub)
    return record

# The runners prepend the container npm bin dir to PATH; a real binary there
# would shadow the test stub (same concern as tests/_claude_runner_harness.py
# documents for claude).
def _skip_if_real_binary(name: str):
    hit = Path("/home/developer/.npm-global/bin") / name
    return pytest.mark.skipif(
        hit.exists(),
        reason=f"Real {name} at {hit} would shadow the test stub",
    )


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def run_harness_runner(binary: str, tmp_path, env=None):
    """Run libexec/<binary>-runner.sh with a stubbed harness binary.

    Returns (output, argv, env_dump) where argv is the stub's captured
    argument list and env_dump maps the stub's observed environment.
    """
    argv_file = tmp_path / "argv.txt"
    env_file = tmp_path / "env.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)

    stub = fake_bin / binary
    stub.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$@" > "{argv_file}"\n'
        f'env > "{env_file}"\n'
        "exit 0\n"
    )
    _make_executable(stub)

    run_env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        # Point both agent-files roots away from the real container layout.
        "LMER_WORK_AGENT_FILES_ROOT": str(tmp_path / "no-work-agent-files"),
        "LMER_GLOBAL_DIR": str(tmp_path / "no-global"),
    }
    if env:
        run_env.update(env)

    result = subprocess.run(
        ["bash", str(LIBEXEC / f"{binary}-runner.sh")],
        env=run_env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    argv = []
    if argv_file.exists():
        argv = [line for line in argv_file.read_text().splitlines() if line]
    env_dump = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                env_dump[k] = v
    return result.stdout + result.stderr, argv, env_dump


def _global_agent_files(tmp_path, rel, content):
    """Create a fake global lmer dir carrying agent-files/<rel>."""
    global_dir = tmp_path / "global"
    target = global_dir / "agent-files" / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return global_dir


class TestRunnerDispatch:
    """clone_and_exec routes harness runner tokens to the right script."""

    def test_known_runner_commands_match_registry(self):
        # KNOWN_HARNESS_RUNNERS is a standalone copy (clone_and_exec runs
        # without the lmer_cli import path); this guard keeps them in sync.
        assert clone_and_exec.KNOWN_HARNESS_RUNNERS == {
            h.runner_command for h in HARNESSES.values()
        }

    def test_find_runner_claude_falls_back_to_binary(self):
        with patch.object(clone_and_exec.Path, "exists", return_value=False):
            assert clone_and_exec.find_runner("claude") == "claude"

    def test_find_runner_non_claude_returns_none_when_missing(self):
        with patch.object(clone_and_exec.Path, "exists", return_value=False):
            assert clone_and_exec.find_runner("codex") is None

    def test_find_runner_prefers_global_libexec_script(self):
        real_exists = clone_and_exec.Path.exists

        def fake_exists(self):
            if str(self) == "/Agents/global/libexec/codex-runner.sh":
                return True
            if str(self).startswith("/home/developer"):
                return False
            return real_exists(self)

        with patch.object(clone_and_exec.Path, "exists", fake_exists):
            assert (
                clone_and_exec.find_runner("codex")
                == "/Agents/global/libexec/codex-runner.sh"
            )

    def test_default_token_is_claude_runner(self):
        # The historical default when no explicit command is given.
        assert "claude-runner" in clone_and_exec.KNOWN_HARNESS_RUNNERS

    def test_non_claude_ignores_legacy_home_copy(self):
        # Only claude-runner.sh is baked to /home/developer; a non-claude
        # runner launched from there couldn't source harness-common.sh, so
        # that legacy candidate must not be consulted for other harnesses.
        real_exists = clone_and_exec.Path.exists

        def fake_exists(self):
            if str(self) == "/home/developer/codex-runner.sh":
                return True
            if str(self).startswith(("/Agents/global", "/home/developer/.lmer")):
                return False
            return real_exists(self)

        with patch.object(clone_and_exec.Path, "exists", fake_exists):
            assert clone_and_exec.find_runner("codex") is None

    def test_claude_still_uses_legacy_home_copy(self):
        real_exists = clone_and_exec.Path.exists

        def fake_exists(self):
            if str(self) == "/home/developer/claude-runner.sh":
                return True
            if str(self).startswith(("/Agents/global", "/home/developer/.lmer")):
                return False
            return real_exists(self)

        with patch.object(clone_and_exec.Path, "exists", fake_exists):
            assert clone_and_exec.find_runner("claude") == "/home/developer/claude-runner.sh"


@_skip_if_real_binary("codex")
class TestCodexRunner:
    def test_default_sandbox_and_approvals(self, tmp_path):
        out, argv, env = run_harness_runner("codex", tmp_path)
        assert "--sandbox" in argv and "danger-full-access" in argv
        assert "--ask-for-approval" in argv and "on-request" in argv
        assert "--dangerously-bypass-approvals-and-sandbox" not in argv
        assert env.get("LMER_SESSION_ID")
        assert env.get("LMER_HARNESS") == "codex"

    def test_danger_zone_bypasses_approvals(self, tmp_path):
        _, argv, _ = run_harness_runner("codex", tmp_path, env={"LMER_DANGER_ZONE": "1"})
        assert "--dangerously-bypass-approvals-and-sandbox" in argv
        assert "--sandbox" not in argv

    def test_model_flag(self, tmp_path):
        _, argv, _ = run_harness_runner("codex", tmp_path, env={"LMER_LLM_NAME": "gpt-5.5"})
        assert argv[argv.index("--model") + 1] == "gpt-5.5"

    def test_effort_passthrough_and_max_maps_to_xhigh(self, tmp_path):
        _, argv, _ = run_harness_runner(
            "codex", tmp_path, env={"LMER_REASONING_EFFORT": "high"}
        )
        assert "model_reasoning_effort=high" in argv
        _, argv, _ = run_harness_runner(
            "codex", tmp_path, env={"LMER_REASONING_EFFORT": "max"}
        )
        assert "model_reasoning_effort=xhigh" in argv
        # xhigh is itself a valid session-level tier (matching claude's
        # LMER_REASONING_EFFORT vocabulary) and passes through directly
        _, argv, _ = run_harness_runner(
            "codex", tmp_path, env={"LMER_REASONING_EFFORT": "xhigh"}
        )
        assert "model_reasoning_effort=xhigh" in argv

    def test_invalid_effort_warns_and_skips(self, tmp_path):
        out, argv, _ = run_harness_runner(
            "codex", tmp_path, env={"LMER_REASONING_EFFORT": "turbo"}
        )
        assert "Ignoring LMER_REASONING_EFFORT" in out
        assert not any("model_reasoning_effort" in a for a in argv)

    def test_config_provisioned_from_global_agent_files(self, tmp_path):
        global_dir = _global_agent_files(tmp_path, "codex/config.toml", "# base\n")
        run_harness_runner("codex", tmp_path, env={"LMER_GLOBAL_DIR": str(global_dir)})
        assert (tmp_path / ".codex" / "config.toml").read_text() == "# base\n"

    def test_existing_config_wins_over_provisioning(self, tmp_path):
        global_dir = _global_agent_files(tmp_path, "codex/config.toml", "# base\n")
        cfg = tmp_path / ".codex" / "config.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("# mine\n")
        run_harness_runner("codex", tmp_path, env={"LMER_GLOBAL_DIR": str(global_dir)})
        assert cfg.read_text() == "# mine\n"

    def test_work_agent_files_override_global(self, tmp_path):
        global_dir = _global_agent_files(tmp_path, "codex/config.toml", "# base\n")
        work_root = tmp_path / "work-agent-files"
        (work_root / "codex").mkdir(parents=True)
        (work_root / "codex" / "config.toml").write_text("# work\n")
        run_harness_runner(
            "codex", tmp_path,
            env={
                "LMER_GLOBAL_DIR": str(global_dir),
                "LMER_WORK_AGENT_FILES_ROOT": str(work_root),
            },
        )
        assert (tmp_path / ".codex" / "config.toml").read_text() == "# work\n"

    def test_user_agents_md_rendered_to_global_context(self, tmp_path):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        (global_dir / "AGENTS.md").write_text("user additions\n")
        run_harness_runner("codex", tmp_path, env={"LMER_GLOBAL_DIR": str(global_dir)})
        context = (tmp_path / ".codex" / "AGENTS.md").read_text()
        assert "user additions" in context
        assert "lmer-managed" in context

    def test_user_authored_context_file_is_untouched(self, tmp_path):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        (global_dir / "AGENTS.md").write_text("user additions\n")
        target = tmp_path / ".codex" / "AGENTS.md"
        target.parent.mkdir(parents=True)
        target.write_text("hand-written\n")
        run_harness_runner("codex", tmp_path, env={"LMER_GLOBAL_DIR": str(global_dir)})
        assert target.read_text() == "hand-written\n"

    def test_removed_custom_prompt_mechanism_is_not_provisioned(self, tmp_path):
        global_dir = _global_claude_command(
            tmp_path, "start.md",
            "---\ndescription: Start the task\nallowed-tools: Bash(work:*)\n---\n"
            "!bash /Agents/global/hooks/start.sh\n",
        )
        run_harness_runner(
            "codex", tmp_path,
            env={"LMER_GLOBAL_DIR": str(global_dir), "PYTHONPATH": str(SRC)},
        )
        assert not (tmp_path / ".codex" / "prompts").exists()

    def test_memory_restored_when_persistence_enabled(self, tmp_path):
        record = _stub_work_cli(tmp_path)
        run_harness_runner("codex", tmp_path, env={"LMER_PERSIST_AGENT_MEMORY": "1"})
        assert "memory restore" in record.read_text()
        context = (tmp_path / ".codex" / "AGENTS.md").read_text()
        assert "Agent memory" in context


@_skip_if_real_binary("pi")
class TestPiRunner:
    def test_default_does_not_trust_project_resources(self, tmp_path):
        out, argv, env = run_harness_runner("pi", tmp_path)
        assert "--no-approve" in argv
        assert "--approve" not in [a for a in argv if a != "--no-approve"]
        assert env.get("LMER_HARNESS") == "pi"
        assert env.get("PI_SKIP_VERSION_CHECK") == "1"
        assert "without permission prompts" in out

    def test_danger_zone_trusts_project_resources(self, tmp_path):
        _, argv, _ = run_harness_runner("pi", tmp_path, env={"LMER_DANGER_ZONE": "1"})
        assert "--approve" in argv
        assert "--no-approve" not in argv

    def test_model_and_thinking(self, tmp_path):
        _, argv, _ = run_harness_runner(
            "pi", tmp_path,
            env={"LMER_LLM_NAME": "sonnet", "LMER_REASONING_EFFORT": "max"},
        )
        assert argv[argv.index("--model") + 1] == "sonnet"
        assert argv[argv.index("--thinking") + 1] == "xhigh"

    def test_settings_provisioned(self, tmp_path):
        global_dir = _global_agent_files(tmp_path, "pi/settings.json", "{}\n")
        run_harness_runner("pi", tmp_path, env={"LMER_GLOBAL_DIR": str(global_dir)})
        assert (tmp_path / ".pi" / "agent" / "settings.json").read_text() == "{}\n"

    def test_slash_commands_rendered_as_prompt_templates(self, tmp_path):
        global_dir = _global_claude_command(
            tmp_path, "followup.md",
            "---\ndescription: Load follow-up instructions\nallowed-tools: Bash(work:*)\n---\n"
            "!bash /Agents/global/hooks/followup.sh\n",
        )
        out, _, _ = run_harness_runner(
            "pi", tmp_path,
            env={"LMER_GLOBAL_DIR": str(global_dir), "PYTHONPATH": str(SRC)},
        )
        template = (tmp_path / ".pi" / "agent" / "prompts" / "followup.md").read_text()
        assert "description: Load follow-up instructions" in template
        assert "allowed-tools" not in template
        assert "Run `bash /Agents/global/hooks/followup.sh`" in template
        assert "Rendered 1 slash-command prompt template(s)" in out

    def test_work_repo_command_overrides_global_template(self, tmp_path):
        global_dir = _global_claude_command(
            tmp_path, "followup.md", "---\ndescription: global\n---\ng\n"
        )
        work_root = tmp_path / "work-agent-files"
        (work_root / "claude" / "commands").mkdir(parents=True)
        (work_root / "claude" / "commands" / "followup.md").write_text(
            "---\ndescription: work\n---\nw\n"
        )
        run_harness_runner(
            "pi", tmp_path,
            env={
                "LMER_GLOBAL_DIR": str(global_dir),
                "LMER_WORK_AGENT_FILES_ROOT": str(work_root),
                "PYTHONPATH": str(SRC),
            },
        )
        template = (tmp_path / ".pi" / "agent" / "prompts" / "followup.md").read_text()
        assert "description: work" in template

    def test_memory_instructions_rendered_when_persistence_enabled(self, tmp_path):
        run_harness_runner("pi", tmp_path, env={"LMER_PERSIST_AGENT_MEMORY": "1"})
        context = (tmp_path / ".pi" / "agent" / "AGENTS.md").read_text()
        assert "Agent memory" in context
        assert "work memory persist" in context

    def test_no_memory_instructions_when_persistence_disabled(self, tmp_path):
        run_harness_runner("pi", tmp_path)
        assert not (tmp_path / ".pi" / "agent" / "AGENTS.md").exists()

    def test_memory_restored_when_persistence_enabled(self, tmp_path):
        record = _stub_work_cli(tmp_path)
        run_harness_runner("pi", tmp_path, env={"LMER_PERSIST_AGENT_MEMORY": "1"})
        assert "memory restore" in record.read_text()

    def test_memory_not_restored_when_persistence_disabled(self, tmp_path):
        record = _stub_work_cli(tmp_path)
        run_harness_runner("pi", tmp_path)
        assert not record.exists()


class TestProvisionConfigFallback:
    """harness_provision_config's optional third argument: the
    lowest-priority fallback source a user-installed harness's runner passes
    for the base config shipped in its own directory (issue #132)."""

    def _provision(self, tmp_path, work_root, global_dir, fallback):
        target = tmp_path / "target" / "settings.json"
        script = (
            f'source "{LIBEXEC}/harness-common.sh"\n'
            "harness_find_global_dir\n"
            f'harness_provision_config "acme/settings.json" "{target}" "{fallback}"\n'
        )
        subprocess.run(
            ["bash", "-c", script],
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(tmp_path),
                "LMER_WORK_AGENT_FILES_ROOT": str(work_root),
                "LMER_GLOBAL_DIR": str(global_dir),
            },
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return target

    def test_fallback_used_when_no_other_source(self, tmp_path):
        fallback = tmp_path / "acme" / "agent-files" / "settings.json"
        fallback.parent.mkdir(parents=True)
        fallback.write_text('{"from": "fallback"}')
        target = self._provision(
            tmp_path, tmp_path / "no-work", tmp_path / "no-global", fallback
        )
        assert target.read_text() == '{"from": "fallback"}'

    def test_work_repo_overrides_fallback(self, tmp_path):
        fallback = tmp_path / "acme" / "agent-files" / "settings.json"
        fallback.parent.mkdir(parents=True)
        fallback.write_text('{"from": "fallback"}')
        work_root = tmp_path / "work-agent-files"
        override = work_root / "acme" / "settings.json"
        override.parent.mkdir(parents=True)
        override.write_text('{"from": "work"}')
        target = self._provision(tmp_path, work_root, tmp_path / "no-global", fallback)
        assert target.read_text() == '{"from": "work"}'

    def test_existing_target_wins_over_fallback(self, tmp_path):
        fallback = tmp_path / "acme" / "agent-files" / "settings.json"
        fallback.parent.mkdir(parents=True)
        fallback.write_text('{"from": "fallback"}')
        target = tmp_path / "target" / "settings.json"
        target.parent.mkdir(parents=True)
        target.write_text('{"from": "session"}')
        self._provision(tmp_path, tmp_path / "no-work", tmp_path / "no-global", fallback)
        assert target.read_text() == '{"from": "session"}'


@skip_if_npm_claude_present
class TestPersonalConfigMerge:
    """claude-runner.sh folds the operator's personal settings.local.json and
    .mcp.local.json into the base configs (#251).

    Behavioral rather than source-level: both jq filters indexed the *merged*
    object (`.[0]` after `.[0] * .[1] |`), so jq exited 5, the 2>/dev/null hid
    it, and the runner announced a fallback to base-only config. Reading the
    file the runner leaves behind is what tells the two apart.
    """

    def _run(self, tmp_path, base_name, base, local_name, local, env_keys):
        """Write base/local config pairs, run the runner, return the base file."""
        base_file = tmp_path / base_name
        base_file.write_text(base if isinstance(base, str) else json.dumps(base))
        local_file = tmp_path / local_name
        local_file.write_text(local if isinstance(local, str) else json.dumps(local))
        base_key, local_key = env_keys
        result = run_claude_runner(
            tmp_path,
            env={base_key: str(base_file), local_key: str(local_file)},
        )
        return base_file, result

    def _run_settings(self, tmp_path, base, local):
        return self._run(
            tmp_path,
            "settings.json", base,
            "settings.local.json", local,
            ("LMER_SETTINGS_FILE", "LMER_SETTINGS_LOCAL_FILE"),
        )

    def _run_mcp(self, tmp_path, base, local):
        return self._run(
            tmp_path,
            "mcp.json", base,
            "mcp.local.json", local,
            ("LMER_MCP_FILE", "LMER_MCP_LOCAL_FILE"),
        )

    def test_personal_permissions_are_merged_and_deduped(self, tmp_path):
        base_file, result = self._run_settings(
            tmp_path,
            {
                "model": "sonnet",
                "permissions": {"allow": ["Bash(ls:*)", "Read"], "deny": ["Bash(rm:*)"]},
            },
            {
                "permissions": {"allow": ["Read", "WebFetch"], "deny": ["Bash(curl:*)"]},
            },
        )

        assert "✅ Merged personal permissions into settings.json" in result.output
        merged = json.loads(base_file.read_text())
        assert merged["permissions"]["allow"] == ["Bash(ls:*)", "Read", "WebFetch"]
        assert merged["permissions"]["deny"] == ["Bash(curl:*)", "Bash(rm:*)"]
        # Keys the local file says nothing about survive the merge.
        assert merged["model"] == "sonnet"

    def test_malformed_settings_local_falls_back_to_base(self, tmp_path):
        base = {"model": "sonnet", "permissions": {"allow": ["Read"]}}
        base_file, result = self._run_settings(tmp_path, base, "{not json")

        assert "⚠️  Failed to merge settings.local.json, using base settings" in result.output
        assert json.loads(base_file.read_text()) == base

    def test_personal_mcp_servers_are_merged(self, tmp_path):
        base_file, result = self._run_mcp(
            tmp_path,
            {"mcpServers": {"base": {"command": "base-server"}}, "other": 1},
            {"mcpServers": {"personal": {"command": "personal-server"}}},
        )

        assert "✅ Merged personal MCP servers into .mcp.json" in result.output
        merged = json.loads(base_file.read_text())
        assert merged["mcpServers"] == {
            "base": {"command": "base-server"},
            "personal": {"command": "personal-server"},
        }
        assert merged["other"] == 1

    def test_malformed_mcp_local_falls_back_to_base(self, tmp_path):
        base = {"mcpServers": {"base": {"command": "base-server"}}}
        base_file, result = self._run_mcp(tmp_path, base, "{not json")

        assert "⚠️  Failed to merge .mcp.local.json, using base MCP config" in result.output
        assert json.loads(base_file.read_text()) == base


@skip_if_npm_claude_present
class TestPromptFileScoping:
    """A runner's startup cleanup reaches only its own prompt files (#276).

    The files live in the container's shared /tmp and must outlive the shell
    (it ends in ``exec``), so the cleanup runs at startup — where a bare
    ``agents-prompt.*.md`` glob deleted the prompt file of a session that was
    still running, dropping its AGENTS.md injection. This exercises the real
    script against a seeded stand-in for that concurrent session's file.
    """

    def _run_with_prompt_file(self, tmp_path):
        """Run the runner on a path that forces it to mktemp a prompt file."""
        return run_claude_runner(
            tmp_path,
            # The non-interactive fragment is appended to a temp file even when
            # no AGENTS.md is in play, so a prompt file is always created here.
            env={"LMER_NONINTERACTIVE": "1", "LMER_GLOBAL_DIR": str(tmp_path / "no-global")},
        )

    def test_a_concurrent_runners_prompt_file_survives(self, tmp_path):
        # A live owner's pid plus a mkstemp-unique tail: the pid is what puts
        # this on the liveness branch (a random name is skipped as unparseable
        # and would pass without testing anything), and mkstemp keeps it from
        # colliding with a real session's file.
        fd, seeded = tempfile.mkstemp(
            prefix=f"agents-prompt.{os.getpid()}.", suffix=".md", dir="/tmp"
        )
        os.close(fd)
        seeded = Path(seeded)
        seeded.write_text("another runner's AGENTS.md\n")
        own_prompt = None
        try:
            result = self._run_with_prompt_file(tmp_path)

            idx = result.argv.index("--append-system-prompt-file")
            own_prompt = Path(result.argv[idx + 1])
            assert own_prompt != seeded
            assert seeded.exists(), (
                "the runner's startup cleanup deleted a concurrent session's "
                f"prompt file ({seeded})"
            )
            assert seeded.read_text() == "another runner's AGENTS.md\n"
        finally:
            seeded.unlink(missing_ok=True)
            if own_prompt is not None and str(own_prompt).startswith("/tmp/agents-prompt."):
                own_prompt.unlink(missing_ok=True)

    def test_a_dead_runners_prompt_file_is_collected(self, tmp_path):
        """#276's fix scoped the sweep to the runner's own live pid, so it
        collected nothing; collection is by owner liveness now."""
        dead = subprocess.Popen([sys.executable, "-c", ""])
        dead.wait()
        dead_file = Path(f"/tmp/agents-prompt.{dead.pid}.deadowner.md")
        dead_file.write_text("a dead runner's leftovers\n")
        live_file = Path(f"/tmp/agents-prompt.{os.getpid()}.liveowner.md")
        live_file.write_text("a live runner's file\n")
        own_prompt = None
        try:
            result = self._run_with_prompt_file(tmp_path)

            idx = result.argv.index("--append-system-prompt-file")
            own_prompt = Path(result.argv[idx + 1])
            assert not dead_file.exists(), (
                f"prompt file of dead pid {dead.pid} was not collected"
            )
            assert live_file.exists(), (
                f"prompt file of live pid {os.getpid()} was collected — the #276 "
                "regression"
            )
        finally:
            dead_file.unlink(missing_ok=True)
            live_file.unlink(missing_ok=True)
            if own_prompt is not None and str(own_prompt).startswith("/tmp/agents-prompt."):
                own_prompt.unlink(missing_ok=True)

    def test_the_prompt_file_name_carries_the_runners_pid(self, tmp_path):
        """Scoping is in the name, so the cleanup glob can be narrowed to it."""
        own_prompt = None
        try:
            result = self._run_with_prompt_file(tmp_path)
            idx = result.argv.index("--append-system-prompt-file")
            own_prompt = Path(result.argv[idx + 1])
            assert re.fullmatch(r"agents-prompt\.\d+\.[A-Za-z0-9]+\.md", own_prompt.name), (
                f"prompt file {own_prompt.name} is not scoped to one runner"
            )
        finally:
            if own_prompt is not None and str(own_prompt).startswith("/tmp/agents-prompt."):
                own_prompt.unlink(missing_ok=True)


# The pin only exists where an operational tree exists; off-container (e.g.
# upstream CI) these skip, and the substring tripwire in
# tests/test_import_provenance.py still guards the scripts' source everywhere.
OPERATIONAL_TREE = Path("/Agents/global/src/lmer_cli")

needs_operational_tree = pytest.mark.skipif(
    not OPERATIONAL_TREE.is_dir(),
    reason="no operational tree at /Agents/global/src (not a session container)",
)


@needs_operational_tree
class TestSupervisorPin:
    """The runners exec the supervisor through the operational-tree pin (#236).

    Behavioral, unlike the source-level tripwire: the real script runs with a
    stub interpreter (via ``LMER_PYTHON``) and a stub ``lmer-supervisor`` on
    PATH, and the assertion is which one was exec'd and with what argv — so
    moving the pinned exec out of reach, breaking its guard condition, or
    launching the console script anyway all fail here even with the pin's
    text still present in the file.
    """

    def _stub(self, path: Path, capture: Path) -> None:
        path.write_text(
            "#!/bin/bash\n"
            f'printf "%s\\n" "$@" > "{capture}"\n'
            "exit 0\n"
        )
        _make_executable(path)

    def _assert_pinned_argv(self, capture: Path, harness: str) -> None:
        assert capture.exists(), "the pinned interpreter was never exec'd"
        argv = capture.read_text().splitlines()
        assert argv[0] == "-c"
        assert 'sys.path.insert(0, "/Agents/global/src")' in argv[1]
        assert "from lmer_cli.supervisor import main" in argv[1]
        separator = argv.index("--")
        assert argv[separator + 1] == harness, (
            f"the wrapped command should follow '--', got {argv[separator:]}"
        )

    def test_codex_runner_execs_the_pinned_interpreter(self, tmp_path):
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir(exist_ok=True)
        supervisor_argv = tmp_path / "supervisor_argv.txt"
        self._stub(fake_bin / "lmer-supervisor", supervisor_argv)
        python_argv = tmp_path / "python_argv.txt"
        pinned_python = tmp_path / "pinned-python"
        self._stub(pinned_python, python_argv)

        run_harness_runner(
            "codex", tmp_path, env={"LMER_PYTHON": str(pinned_python)}
        )

        self._assert_pinned_argv(python_argv, "codex")
        assert not supervisor_argv.exists(), (
            "the unpinned console script ran too — the pin was bypassed"
        )

    def test_codex_runner_falls_back_when_no_interpreter_exists(self, tmp_path):
        """No usable interpreter beats no supervisor at all — but unpinned."""
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir(exist_ok=True)
        supervisor_argv = tmp_path / "supervisor_argv.txt"
        self._stub(fake_bin / "lmer-supervisor", supervisor_argv)

        run_harness_runner(
            "codex", tmp_path,
            env={"LMER_PYTHON": str(tmp_path / "no-such-python")},
        )

        assert supervisor_argv.exists(), (
            "with no pinnable interpreter the console script is the fallback"
        )
        argv = supervisor_argv.read_text().splitlines()
        assert argv[0] == "--"
        assert argv[1] == "codex"

    def test_claude_runner_execs_the_pinned_interpreter(self, tmp_path):
        extra_bin = tmp_path / "extra-bin"
        extra_bin.mkdir()
        supervisor_argv = tmp_path / "supervisor_argv.txt"
        self._stub(extra_bin / "lmer-supervisor", supervisor_argv)
        python_argv = tmp_path / "python_argv.txt"
        pinned_python = tmp_path / "pinned-python"
        self._stub(pinned_python, python_argv)

        run_claude_runner(
            tmp_path,
            env={
                # run_claude_runner owns tmp_path/bin; the supervisor stub
                # rides on a second dir appended to the same PATH shape.
                "PATH": f"{tmp_path / 'bin'}:{extra_bin}:/usr/bin:/bin",
                "LMER_PYTHON": str(pinned_python),
            },
        )

        self._assert_pinned_argv(python_argv, "claude")
        assert not supervisor_argv.exists(), (
            "the unpinned console script ran too — the pin was bypassed"
        )
