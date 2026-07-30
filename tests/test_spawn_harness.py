"""Tests for spawn-harness (lmer_cli.container.spawn_harness).

Covers config parsing, agent resolution UX, child-env composition (including
the structural no-grandchildren strip), harness selection from the child env,
end-to-end child execution against stub binaries (argv/env capture, output
redirection, exit-code mirroring, timeout), and the bin/ wrapper.
"""

import argparse
import builtins
import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lmer_cli.container import spawn_harness
from lmer_cli.container.spawn_harness import (
    build_child_env,
    load_agents_config,
    parse_env_pairs,
    resolve_agent,
    select_harness,
)
from lmer_cli.harness import HARNESSES

BIN_WRAPPER = Path(__file__).parent.parent / "bin" / "spawn-harness"

CONFIG = {
    "opus-review": {"env": {"LMER_LLM_NAME": "opus", "LMER_REASONING_EFFORT": "max"}},
    "sol-review": {"env": {"LMER_HARNESS": "codex", "LMER_LLM_NAME": "gpt-5.6-sol"}},
    "second-pass": {"env": {"LMER_LLM_NAME": "opus"}, "prompt": "Second pass framing."},
}


def _config_env(config=CONFIG):
    return {"LMER_AGENTS_CONFIG": json.dumps(config)}


class TestLoadAgentsConfig:
    def test_absent_and_blank_yield_empty(self):
        assert load_agents_config({}) == {}
        assert load_agents_config({"LMER_AGENTS_CONFIG": "  "}) == {}

    def test_valid_config_parses(self):
        assert load_agents_config(_config_env()) == CONFIG

    def test_invalid_json_exits_2(self, capsys):
        with pytest.raises(SystemExit) as exc:
            load_agents_config({"LMER_AGENTS_CONFIG": "{nope"})
        assert exc.value.code == 2
        assert "not valid JSON" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "payload",
        [
            '["a"]',
            '{"a": "text"}',
            '{"a": {"env": ["not-a-map"]}}',
            '{"a": {"env": {}, "prompt": 5}}',
        ],
    )
    def test_wrong_shapes_exit_2(self, payload):
        with pytest.raises(SystemExit) as exc:
            load_agents_config({"LMER_AGENTS_CONFIG": payload})
        assert exc.value.code == 2


class TestResolveAgent:
    def test_known_name_resolves(self):
        assert resolve_agent("opus-review", CONFIG) is CONFIG["opus-review"]

    def test_unknown_name_lists_available(self, capsys):
        with pytest.raises(SystemExit) as exc:
            resolve_agent("nope", CONFIG)
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "nope" in err
        assert "opus-review, second-pass, sol-review" in err

    def test_no_agents_configured_points_at_launch_flag(self, capsys):
        with pytest.raises(SystemExit) as exc:
            resolve_agent("any", {})
        assert exc.value.code == 2
        assert "--agents" in capsys.readouterr().err


class TestParseEnvPairs:
    def test_pairs_parse_last_wins(self):
        assert parse_env_pairs(["A=1", "B=x=y", "A=2"]) == {"A": "2", "B": "x=y"}

    @pytest.mark.parametrize("bad", ["NOEQUALS", "=value"])
    def test_malformed_pair_exits_2(self, bad):
        with pytest.raises(SystemExit) as exc:
            parse_env_pairs([bad])
        assert exc.value.code == 2


class TestBuildChildEnv:
    def test_overlay_precedence(self):
        child = build_child_env(
            {"KEEP": "parent", "OVERRIDE": "parent", "CLI": "parent"},
            {"OVERRIDE": "preset", "CLI": "preset"},
            {"CLI": "cli"},
        )
        assert child["KEEP"] == "parent"
        assert child["OVERRIDE"] == "preset"
        assert child["CLI"] == "cli"

    def test_fanout_vars_stripped_no_grandchildren(self):
        child = build_child_env(
            {"LMER_AGENTS": "a,b", "LMER_AGENTS_CONFIG": "{}"},
            {"LMER_AGENTS": "sneaky"},
            {},
        )
        assert "LMER_AGENTS" not in child
        assert "LMER_AGENTS_CONFIG" not in child


class TestSelectHarness:
    def test_agent_explicit_harness_wins_over_model_hint(self):
        overlay = {"LMER_HARNESS": "codex", "LMER_LLM_NAME": "opus"}
        harness = select_harness(dict(overlay), overlay)
        assert harness.name == "codex"

    def test_agent_explicit_harness_normalized(self):
        overlay = {"LMER_HARNESS": " Codex "}
        assert select_harness(dict(overlay), overlay).name == "codex"

    def test_overlay_model_hint_selects_harness(self):
        for model, expected in (("gpt-5.2", "codex"), ("sonnet", "claude")):
            overlay = {"LMER_LLM_NAME": model}
            assert select_harness(dict(overlay), overlay).name == expected

    def test_inherited_model_never_reroutes_env_less_agent(self):
        # A session launched --harness pi with LMER_LLM_NAME=sonnet (a
        # legitimate keys-via-API setup): an agent with an empty overlay
        # must run pi — the inherited model hint must not outrank the
        # inherited explicit harness the operator already chose over it.
        child_env = {"LMER_HARNESS": "pi", "LMER_LLM_NAME": "sonnet"}
        assert select_harness(child_env, {}).name == "pi"
        # Same-harness child: the parent's model is valid there — kept.
        assert child_env["LMER_LLM_NAME"] == "sonnet"

    def test_model_hint_beats_inherited_parent_harness(self):
        # The container always carries the PARENT session's resolved
        # LMER_HARNESS; a model-only agent preset must still get the harness
        # its model implies, not the orchestrator's.
        child_env = {"LMER_HARNESS": "codex", "LMER_LLM_NAME": "opus"}
        assert select_harness(child_env, {"LMER_LLM_NAME": "opus"}).name == "claude"

    def test_inherited_parent_harness_used_without_hint(self):
        # No agent config at all → the child runs the session's own harness.
        assert select_harness({"LMER_HARNESS": "pi"}, {}).name == "pi"

    def test_default_is_claude(self):
        assert select_harness({}, {}).name == "claude"

    def test_selected_name_written_back_to_child_env(self):
        child_env = {"LMER_HARNESS": "codex", "LMER_LLM_NAME": "opus"}
        select_harness(child_env, {"LMER_LLM_NAME": "opus"})
        assert child_env["LMER_HARNESS"] == "claude"

    def test_unknown_agent_harness_exits_2(self, capsys):
        with pytest.raises(SystemExit) as exc:
            select_harness({"LMER_HARNESS": "not-a-harness"}, {"LMER_HARNESS": "not-a-harness"})
        assert exc.value.code == 2
        assert "not-a-harness" in capsys.readouterr().err

    def test_foreign_inherited_model_dropped_for_cross_harness_child(self):
        # Harness-only agent from a claude session running fable: the child
        # must not get `codex --model fable` — the inherited claude-family
        # model is dropped so the codex child runs its default model.
        parent = {"LMER_HARNESS": "claude", "LMER_LLM_NAME": "fable"}
        overlay = {"LMER_HARNESS": "codex"}
        child_env = {**parent, **overlay}
        assert select_harness(child_env, overlay, parent).name == "codex"
        assert "LMER_LLM_NAME" not in child_env

    def test_inherited_model_matching_child_harness_kept(self):
        # The inherited model implies the selected harness — native, kept.
        parent = {"LMER_HARNESS": "claude", "LMER_LLM_NAME": "gpt-5.2"}
        overlay = {"LMER_HARNESS": "codex"}
        child_env = {**parent, **overlay}
        select_harness(child_env, overlay, parent)
        assert child_env["LMER_LLM_NAME"] == "gpt-5.2"

    def test_hintless_inherited_model_kept(self):
        # A model implying no harness (custom registry name) is ambiguous —
        # never dropped.
        parent = {"LMER_HARNESS": "pi", "LMER_LLM_NAME": "my-custom-model"}
        overlay = {"LMER_HARNESS": "codex"}
        child_env = {**parent, **overlay}
        select_harness(child_env, overlay, parent)
        assert child_env["LMER_LLM_NAME"] == "my-custom-model"

    def test_overlay_model_always_kept(self):
        # The agent chose its model explicitly — the drop only guards the
        # inherited case.
        parent = {"LMER_HARNESS": "claude", "LMER_LLM_NAME": "fable"}
        overlay = {"LMER_HARNESS": "codex", "LMER_LLM_NAME": "gpt-5.6-sol"}
        child_env = {**parent, **overlay}
        select_harness(child_env, overlay, parent)
        assert child_env["LMER_LLM_NAME"] == "gpt-5.6-sol"

    def test_session_harness_child_keeps_session_model(self, capsys):
        # A --harness pi session running an Anthropic model via API keys:
        # a child re-pinned to pi by its overlay still runs the session's
        # model — same-harness children inherit the parent's working setup.
        parent = {"LMER_HARNESS": "pi", "LMER_LLM_NAME": "sonnet"}
        overlay = {"LMER_HARNESS": "pi"}
        child_env = {**parent, **overlay}
        assert select_harness(child_env, overlay, parent).name == "pi"
        assert child_env["LMER_LLM_NAME"] == "sonnet"


class TestWarnMissingCredentials:
    """Spawn-time complement of the launch-time credential warning (issue
    #131): a --env reroute can select a harness the launch computation never
    saw, whose credential files were never mounted — that must warn (on
    stderr, never error), while the session's own harness stays exempt."""

    def _exists(self, monkeypatch, present):
        monkeypatch.setattr(
            spawn_harness.os.path, "exists", lambda p: p in present
        )

    def test_rerouted_child_with_no_mounted_creds_warns(self, monkeypatch, capsys):
        self._exists(monkeypatch, set())
        spawn_harness.warn_missing_credentials(
            "sol", HARNESSES["codex"], {"LMER_HARNESS": "claude"}
        )
        err = capsys.readouterr().err
        assert "agent 'sol' runs on codex" in err
        assert "/home/developer/.codex/auth.json" in err
        assert "may fail to authenticate" in err

    def test_session_harness_child_is_exempt(self, monkeypatch, capsys):
        self._exists(monkeypatch, set())
        spawn_harness.warn_missing_credentials(
            "opus-review", HARNESSES["claude"], {"LMER_HARNESS": "claude"}
        )
        assert capsys.readouterr().err == ""

    def test_any_mounted_cred_file_silences(self, monkeypatch, capsys):
        self._exists(monkeypatch, {"/home/developer/.pi/agent/auth.json"})
        spawn_harness.warn_missing_credentials(
            "pi-agent", HARNESSES["pi"], {"LMER_HARNESS": "claude"}
        )
        assert capsys.readouterr().err == ""


class TestResolvePrompt:
    """The four prompt cases, unit-tested directly (not just end-to-end)."""

    def _ns(self, prompt=None, prompt_file=None):
        return argparse.Namespace(prompt=prompt, prompt_file=prompt_file)

    def test_caller_prompt_alone(self):
        assert spawn_harness.resolve_prompt(self._ns(prompt="task"), {}) == "task"

    def test_preamble_alone(self):
        agent = {"prompt": "canned persona"}
        assert spawn_harness.resolve_prompt(self._ns(), agent) == "canned persona"

    def test_preamble_prepended_to_caller_prompt(self):
        agent = {"prompt": "persona"}
        assert (
            spawn_harness.resolve_prompt(self._ns(prompt="task"), agent)
            == "persona\n\ntask"
        )

    def test_prompt_file_combined_with_preamble(self, tmp_path):
        path = tmp_path / "p.md"
        path.write_text("from file")
        agent = {"prompt": "persona"}
        result = spawn_harness.resolve_prompt(self._ns(prompt_file=str(path)), agent)
        assert result == "persona\n\nfrom file"

    def test_no_prompt_and_no_preamble_exits_2(self):
        with pytest.raises(SystemExit) as exc:
            spawn_harness.resolve_prompt(self._ns(), {})
        assert exc.value.code == 2

    def test_empty_caller_prompt_rejected_even_with_preamble(self):
        with pytest.raises(SystemExit) as exc:
            spawn_harness.resolve_prompt(self._ns(prompt="  "), {"prompt": "persona"})
        assert exc.value.code == 2

    def test_unreadable_prompt_file_exits_2(self, tmp_path):
        with pytest.raises(SystemExit) as exc:
            spawn_harness.resolve_prompt(
                self._ns(prompt_file=str(tmp_path / "nope.md")), {}
            )
        assert exc.value.code == 2


def _make_stub(
    tmp_path,
    binary,
    exit_code=0,
    sleep=None,
    self_kill=None,
    stderr_text=None,
    stdout_text=None,
):
    """Drop a stub harness binary recording argv/env, return its record files.

    ``stdout_text`` replaces the default ``"<binary> stdout"`` answer — the
    degenerate-output tests need a child that prints nothing at all (``""``),
    only whitespace, or a terse-but-real answer.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    argv_file = tmp_path / f"{binary}-argv.txt"
    env_file = tmp_path / f"{binary}-env.txt"
    stub = fake_bin / binary
    answer = f"{binary} stdout\n" if stdout_text is None else stdout_text
    lines = [
        "#!/bin/bash",
        f'printf "%s\\n" "$@" > "{argv_file}"',
        f'env > "{env_file}"',
    ]
    if answer:
        lines.append(f'printf "%s" {shlex.quote(answer)}')
    if stderr_text:
        lines.append(f'echo "{stderr_text}" >&2')
    if sleep:
        lines.append(f"sleep {sleep}")
    if self_kill:
        lines.append(f"kill -{self_kill} $$")
    lines.append(f"exit {exit_code}")
    stub.write_text("\n".join(lines) + "\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake_bin, argv_file, env_file


def _run_main(tmp_path, args, config=CONFIG, extra_env=None, stub="claude", **stub_kwargs):
    """Run spawn_harness.main() in-process with a stubbed binary on PATH."""
    fake_bin, argv_file, env_file = _make_stub(tmp_path, stub, **stub_kwargs)
    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "LMER_AGENTS": ",".join(config),
        **_config_env(config),
        **(extra_env or {}),
    }
    with pytest.MonkeyPatch.context() as mp:
        for key in list(spawn_harness.os.environ):
            if key.startswith("LMER_"):
                mp.delenv(key, raising=False)
        for key, value in env.items():
            mp.setenv(key, value)
        with pytest.raises(SystemExit) as exc:
            spawn_harness.main(args)
    return exc.value.code, argv_file, env_file


class TestMainEndToEnd:
    def test_claude_child_runs_with_model_and_effort(self, tmp_path):
        code, argv_file, env_file = _run_main(
            tmp_path, ["opus-review", "--prompt", "review the diff"]
        )
        assert code == 0
        argv = argv_file.read_text().splitlines()
        assert argv == [
            "-p",
            "--no-session-persistence",
            "--dangerously-skip-permissions",
            "--model",
            "opus",
            "--effort",
            "max",
            "--",
            "review the diff",
        ]
        env_lines = env_file.read_text().splitlines()
        assert "LMER_LLM_NAME=opus" in env_lines
        # Line-anchored on purpose: in CI the child inherits variables like
        # CI_MERGE_REQUEST_DESCRIPTION whose *text* mentions LMER_AGENTS —
        # only actual fan-out variables must be absent.
        assert not any(
            line.startswith(("LMER_AGENTS=", "LMER_AGENTS_CONFIG="))
            for line in env_lines
        )

    def test_codex_child_selected_by_preset_env(self, tmp_path):
        code, argv_file, _ = _run_main(
            tmp_path, ["sol-review", "--prompt", "p"], stub="codex"
        )
        assert code == 0
        argv = argv_file.read_text().splitlines()
        assert argv[0] == "exec"
        assert "--dangerously-bypass-approvals-and-sandbox" in argv
        assert argv[-1] == "p"

    def test_env_flag_overrides_preset_env(self, tmp_path):
        code, _, env_file = _run_main(
            tmp_path,
            ["opus-review", "--prompt", "p", "--env", "LMER_REVIEW_ON_MR=0"],
        )
        assert code == 0
        assert "LMER_REVIEW_ON_MR=0" in env_file.read_text().splitlines()

    def test_output_captures_child_stdout(self, tmp_path, capsys):
        out = tmp_path / "result.md"
        code, _, _ = _run_main(
            tmp_path, ["opus-review", "--prompt", "p", "--output", str(out)]
        )
        assert code == 0
        assert out.read_text() == "claude stdout\n"
        assert "claude stdout" not in capsys.readouterr().out

    def test_prompt_file(self, tmp_path):
        prompt = tmp_path / "prompt.md"
        prompt.write_text("from a file")
        code, argv_file, _ = _run_main(
            tmp_path, ["opus-review", "--prompt-file", str(prompt)]
        )
        assert code == 0
        assert argv_file.read_text().splitlines()[-1] == "from a file"

    def test_child_exit_code_mirrored(self, tmp_path):
        code, _, _ = _run_main(
            tmp_path, ["opus-review", "--prompt", "p"], exit_code=3
        )
        assert code == 3

    def test_timeout_kills_child_and_exits_124(self, tmp_path, capsys):
        code, _, _ = _run_main(
            tmp_path,
            ["opus-review", "--prompt", "p", "--timeout", "0.2"],
            sleep=30,
        )
        assert code == spawn_harness.TIMEOUT_EXIT_CODE == 124
        assert "timed out" in capsys.readouterr().err

    def test_model_hint_beats_parent_harness_end_to_end(self, tmp_path):
        # Realistic container env: the parent session's LMER_HARNESS is set
        # (say codex); a model-only preset must still run claude.
        code, argv_file, env_file = _run_main(
            tmp_path,
            ["opus-review", "--prompt", "p"],
            extra_env={"LMER_HARNESS": "codex"},
        )
        assert code == 0
        assert argv_file.read_text().splitlines()[0] == "-p"
        assert "LMER_HARNESS=claude" in env_file.read_text().splitlines()

    def test_foreign_parent_model_not_passed_to_cross_harness_child(self, tmp_path):
        # Harness-only preset from a claude session running fable: the codex
        # child must get neither `--model fable` (it would die at spawn) nor
        # the inherited LMER_LLM_NAME in its environment.
        config = {"codex-agent": {"env": {"LMER_HARNESS": "codex"}}}
        code, argv_file, env_file = _run_main(
            tmp_path,
            ["codex-agent", "--prompt", "p"],
            config=config,
            extra_env={"LMER_HARNESS": "claude", "LMER_LLM_NAME": "fable"},
            stub="codex",
        )
        assert code == 0
        argv = argv_file.read_text().splitlines()
        assert "--model" not in argv
        assert "fable" not in argv
        env_lines = env_file.read_text().splitlines()
        assert not any(line.startswith("LMER_LLM_NAME=") for line in env_lines)

    def test_unwritable_output_exits_2(self, tmp_path, capsys):
        code, _, _ = _run_main(
            tmp_path,
            [
                "opus-review",
                "--prompt",
                "p",
                "--output",
                str(tmp_path / "no-such-dir" / "out.md"),
            ],
        )
        assert code == 2
        assert "Cannot write --output" in capsys.readouterr().err

    def test_missing_harness_binary_exits_2(self, tmp_path, capsys):
        # Stub only claude on PATH; the sol-review agent needs codex.
        code, _, _ = _run_main(tmp_path, ["sol-review", "--prompt", "p"])
        assert code == 2
        assert "Cannot run 'codex'" in capsys.readouterr().err

    def test_signal_killed_child_maps_to_128_plus_n(self, tmp_path):
        code, _, _ = _run_main(
            tmp_path, ["opus-review", "--prompt", "p"], self_kill="TERM"
        )
        assert code == 128 + 15

    def test_failed_child_writes_footer_with_stderr_tail(self, tmp_path):
        # A dead child's output file must explain itself — the error lives
        # in the child's stderr, which the caller's shell may never surface.
        out = tmp_path / "result.md"
        code, _, _ = _run_main(
            tmp_path,
            ["opus-review", "--prompt", "p", "--output", str(out)],
            exit_code=3,
            stderr_text="No API key found for kimi-coding",
        )
        assert code == 3
        content = out.read_text()
        assert "[spawn-harness] child FAILED: exit code 3" in content
        assert "No API key found for kimi-coding" in content

    def test_successful_child_gets_no_footer(self, tmp_path):
        out = tmp_path / "result.md"
        code, _, _ = _run_main(
            tmp_path,
            ["opus-review", "--prompt", "p", "--output", str(out)],
            stderr_text="benign diagnostics",
        )
        assert code == 0
        assert out.read_text() == "claude stdout\n"

    def test_timed_out_child_writes_footer(self, tmp_path):
        out = tmp_path / "result.md"
        code, _, _ = _run_main(
            tmp_path,
            ["opus-review", "--prompt", "p", "--output", str(out), "--timeout", "0.2"],
            sleep=30,
        )
        assert code == 124
        assert "[spawn-harness] child FAILED: timed out after 0.2s" in out.read_text()

    def test_child_stderr_passes_through(self, tmp_path, capsys):
        code, _, _ = _run_main(
            tmp_path,
            ["opus-review", "--prompt", "p"],
            stderr_text="live diagnostics",
        )
        assert code == 0
        assert "live diagnostics" in capsys.readouterr().err

    def test_heartbeat_ticks_while_child_runs(self, tmp_path, capsys):
        code, _, _ = _run_main(
            tmp_path,
            ["opus-review", "--prompt", "p", "--heartbeat", "0.2"],
            sleep=1,
        )
        assert code == 0
        assert "still running" in capsys.readouterr().err

    def test_heartbeat_zero_disables_ticks(self, tmp_path, capsys):
        code, _, _ = _run_main(
            tmp_path,
            ["opus-review", "--prompt", "p", "--heartbeat", "0"],
            sleep=0.5,
        )
        assert code == 0
        assert "still running" not in capsys.readouterr().err

    def test_preset_prompt_prepended_to_supplied_prompt(self, tmp_path):
        code, argv_file, _ = _run_main(
            tmp_path, ["second-pass", "--prompt", "review the diff"]
        )
        assert code == 0
        # The stub records one argv element per line; the multi-line prompt
        # is the tail of the file.
        recorded = argv_file.read_text()
        assert recorded.endswith("Second pass framing.\n\nreview the diff\n")

    def test_preset_prompt_alone_suffices(self, tmp_path):
        code, argv_file, _ = _run_main(tmp_path, ["second-pass"])
        assert code == 0
        assert argv_file.read_text().endswith("Second pass framing.\n")

    def test_unknown_effort_warns_and_omits_flag(self, tmp_path, capsys):
        config = {"weird": {"env": {"LMER_REASONING_EFFORT": "turbo"}}}
        code, argv_file, _ = _run_main(
            tmp_path, ["weird", "--prompt", "p"], config=config
        )
        assert code == 0
        assert "--effort" not in argv_file.read_text().splitlines()
        assert "turbo" in capsys.readouterr().err


class TestClassifyDegenerateOutput:
    """The pure detection half (issue #139) — byte-level signals only."""

    @pytest.mark.parametrize(
        "content, expected_fragment",
        [
            ("", "empty"),
            ("   \n\n\t\n", "whitespace only"),
            ("ok\n", "below the"),
            ("done.\n", "below the"),
            ("n/a\n", "below the"),
        ],
    )
    def test_degenerate_shapes_are_named(self, content, expected_fragment):
        reason = spawn_harness.classify_degenerate_output(content)
        assert reason is not None
        assert expected_fragment in reason

    @pytest.mark.parametrize(
        "content",
        [
            # The floor exists to catch stubs, not terse answers: this is the
            # canonical short-but-real review result and must survive.
            "no findings\n",
            "No findings.\n",
            "LGTM — nothing blocking.\n",
        ],
    )
    def test_usable_output_is_not_flagged(self, content):
        assert spawn_harness.classify_degenerate_output(content) is None

    @pytest.mark.parametrize(
        "content",
        [
            # The #137 shape itself. Detecting it means deciding that prose is
            # an incomplete answer, and nothing distinguishes these from a
            # terse real answer without guessing at phrasing — a child can halt
            # for any number of reasons, worded any number of ways. Left to
            # #153 (harness result envelope) and #138 (hook-side session
            # signal); asserted here so re-adding a prose heuristic is a
            # deliberate act with a failing test, not a quiet drift back.
            "I found a problem in auth.py.\n\nShall I proceed with this fix? (yes/no)\n",
            "Analysis complete.\n\nDo you want me to apply the patch?\n",
            "Report body.\n\n**Shall I proceed?**\n",
            "Which of these should I prioritize?\n",
            "Ready to deploy? (y/n)\n",
            # ...and the terse real answers those heuristics collided with.
            "Looks fine. Should we also check the retry path?\n",
            "The remaining puzzle: why does the cache miss on retry?\n",
            "Given the trade-offs above, should we proceed?\n",
        ],
    )
    def test_prose_is_never_judged(self, content):
        assert len(content.strip()) > spawn_harness.DEGENERATE_MIN_CHARS
        assert spawn_harness.classify_degenerate_output(content) is None

    def test_floor_spares_the_canonical_terse_answer(self):
        # Guards the floor against being raised past the answer the issue
        # names explicitly: "no findings" is 11 characters.
        assert len("no findings") > spawn_harness.DEGENERATE_MIN_CHARS


class TestDegenerateOutputEndToEnd:
    """A child exiting 0 with nothing usable must not pass as success."""

    def test_empty_output_warns_and_footers_without_changing_exit_code(
        self, tmp_path, capsys
    ):
        out = tmp_path / "result.md"
        code, _, _ = _run_main(
            tmp_path,
            ["opus-review", "--prompt", "p", "--output", str(out)],
            stdout_text="",
        )
        # Warn only — the child genuinely exited 0 and a fan-out that treated a
        # terse answer as a hard failure would be worse than the bug.
        assert code == 0
        err = capsys.readouterr().err
        assert "opus-review" in err
        assert "no usable output" in err
        assert str(out) in err
        content = out.read_text()
        assert "[spawn-harness] child produced NO USABLE OUTPUT" in content
        assert "empty" in content
        # Distinguishable from a dead child: the orchestrator scans for the
        # FAILED marker to tell "agent died" from "agent returned nothing".
        assert "child FAILED" not in content

    def test_whitespace_only_output_is_flagged(self, tmp_path, capsys):
        out = tmp_path / "result.md"
        code, _, _ = _run_main(
            tmp_path,
            ["opus-review", "--prompt", "p", "--output", str(out)],
            stdout_text="   \n\n\t\n",
        )
        assert code == 0
        assert "whitespace only" in capsys.readouterr().err
        assert "NO USABLE OUTPUT" in out.read_text()

    def test_terse_but_valid_output_is_left_alone(self, tmp_path, capsys):
        out = tmp_path / "result.md"
        code, _, _ = _run_main(
            tmp_path,
            ["opus-review", "--prompt", "p", "--output", str(out)],
            stdout_text="no findings\n",
        )
        assert code == 0
        assert "NO USABLE OUTPUT" not in capsys.readouterr().err
        # Untouched: no footer, no reformatting of the agent's own answer.
        assert out.read_text() == "no findings\n"

    def test_halt_shaped_output_is_left_alone(self, tmp_path, capsys):
        # #137's exact output, end to end: content above the floor is the
        # child's business, so nothing warns and nothing is appended. The
        # feature no longer claims to catch this — see #153 / #138.
        halted = "Found one issue in auth.py.\n\nShall I proceed with this fix? (yes/no)\n"
        out = tmp_path / "result.md"
        code, _, _ = _run_main(
            tmp_path,
            ["opus-review", "--prompt", "p", "--output", str(out)],
            stdout_text=halted,
        )
        assert code == 0
        err = capsys.readouterr().err
        assert "NO USABLE OUTPUT" not in err
        assert "approval question" not in err
        assert out.read_text() == halted

    def test_failed_child_keeps_only_the_failure_footer(self, tmp_path, capsys):
        # A dead child with an empty output file is a failure, not a
        # degenerate success — one marker, not both.
        out = tmp_path / "result.md"
        code, _, _ = _run_main(
            tmp_path,
            ["opus-review", "--prompt", "p", "--output", str(out)],
            exit_code=3,
            stdout_text="",
        )
        assert code == 3
        content = out.read_text()
        assert "[spawn-harness] child FAILED: exit code 3" in content
        assert "NO USABLE OUTPUT" not in content
        assert "no usable output" not in capsys.readouterr().err

    def test_without_output_file_nothing_is_checked(self, tmp_path, capsys):
        # Stdout flows straight through to ours, so there is no captured file
        # to inspect — documented limitation, not a silent failure.
        code, _, _ = _run_main(
            tmp_path, ["opus-review", "--prompt", "p"], stdout_text=""
        )
        assert code == 0
        assert "no usable output" not in capsys.readouterr().err

    def test_unreadable_output_does_not_accuse_the_agent(self, tmp_path, capsys):
        missing = tmp_path / "vanished.md"
        assert spawn_harness.warn_degenerate_output("opus-review", str(missing)) is None
        err = capsys.readouterr().err
        assert "cannot re-read --output" in err
        assert "no usable output" not in err

    def test_footer_append_failure_warns_and_still_reports_the_reason(
        self, tmp_path, capsys, monkeypatch
    ):
        # The sibling of test_unreadable_output_...: if appending the footer
        # raised, the exception would escape run_child after the child already
        # exited 0 and change the mirrored exit code — the one contract this
        # feature promises to leave alone.
        out = tmp_path / "result.md"
        out.write_text("")
        real_open = builtins.open

        def refuse_appends(path, mode="r", *args, **kwargs):
            if "a" in mode:
                raise OSError("read-only file system")
            return real_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", refuse_appends)
        reason = spawn_harness.warn_degenerate_output("opus-review", str(out))
        assert reason is not None
        err = capsys.readouterr().err
        assert "no usable output" in err
        assert "cannot append the no-usable-output footer" in err


class _UnwritableOutput:
    """An output handle that fails the way a full filesystem does.

    ``fileno`` stays real — the child's stdout is redirected to the fd, so the
    subprocess must still receive a genuine file — while *our* footer writes
    (``fail_writes``) or the final ``close`` (``fail_close``) raise. That is
    what ``run_child`` sees on a full or read-only filesystem, without a
    ``chmod`` a root-run suite would ignore.
    """

    def __init__(self, handle, fail_writes=True, fail_close=False):
        self._handle = handle
        self._fail_writes = fail_writes
        self._fail_close = fail_close

    def fileno(self):
        return self._handle.fileno()

    def write(self, *args):
        if self._fail_writes:
            raise OSError("No space left on device")
        return self._handle.write(*args)

    def writelines(self, *args):
        if self._fail_writes:
            raise OSError("No space left on device")
        return self._handle.writelines(*args)

    def flush(self):
        if self._fail_writes:
            raise OSError("No space left on device")
        return self._handle.flush()

    def close(self):
        self._handle.close()
        if self._fail_close:
            raise OSError("No space left on device")


def _unwritable_output(monkeypatch, output, **kwargs):
    """Hand run_child an output handle that cannot be written (or closed)."""
    real_open = builtins.open

    def wrap(path, mode="r", *args, **more):
        handle = real_open(path, mode, *args, **more)
        if str(path) == str(output) and "w" in mode:
            return _UnwritableOutput(handle, **kwargs)
        return handle

    monkeypatch.setattr(builtins, "open", wrap)


class TestFooterWriteFailuresKeepTheExitCode:
    """The mirrored exit code is the one contract spawn-harness promises its
    callers, and a fan-out caller needs it most on the paths where the child
    died. Our own inability to append a footer must therefore warn and get out
    of the way, never replace the code with a traceback (issue #151)."""

    def test_failed_child_keeps_its_own_code(self, tmp_path, capsys, monkeypatch):
        out = tmp_path / "result.md"
        _unwritable_output(monkeypatch, out)
        code, _, _ = _run_main(
            tmp_path,
            ["opus-review", "--prompt", "p", "--output", str(out)],
            exit_code=3,
            stderr_text="No API key found for kimi-coding",
        )
        assert code == 3
        assert "cannot append the failure footer" in capsys.readouterr().err

    def test_timed_out_child_still_reports_124(self, tmp_path, capsys, monkeypatch):
        out = tmp_path / "result.md"
        _unwritable_output(monkeypatch, out)
        code, _, _ = _run_main(
            tmp_path,
            ["opus-review", "--prompt", "p", "--output", str(out), "--timeout", "0.2"],
            sleep=30,
        )
        assert code == spawn_harness.TIMEOUT_EXIT_CODE
        assert "cannot append the failure footer" in capsys.readouterr().err

    def test_interrupt_still_maps_to_128_plus_sigint(
        self, tmp_path, capsys, monkeypatch
    ):
        # The guard here wraps only the footer write: the KeyboardInterrupt
        # still has to unwind into SystemExit(128 + SIGINT), so a swallowed
        # interrupt would show up as a 0/3 exit instead of 130.
        out = tmp_path / "result.md"
        _unwritable_output(monkeypatch, out)
        real_wait = subprocess.Popen.wait
        interrupted = []

        def interrupt_first_wait(self, timeout=None):
            if not interrupted:
                interrupted.append(True)
                raise KeyboardInterrupt
            return real_wait(self, timeout=timeout)

        monkeypatch.setattr(subprocess.Popen, "wait", interrupt_first_wait)
        code, _, _ = _run_main(
            tmp_path,
            ["opus-review", "--prompt", "p", "--output", str(out)],
            sleep=30,
        )
        assert code == 128 + signal.SIGINT
        err = capsys.readouterr().err
        assert "interrupted — child killed" in err
        assert "cannot append the failure footer" in err

    def test_close_failure_after_a_footer_keeps_the_childs_code(
        self, tmp_path, capsys, monkeypatch
    ):
        # A buffered handle can hold a doomed write until close() drains it, so
        # the close is guarded too — otherwise the same ENOSPC clobbers the
        # exit code one line past the footer guard.
        out = tmp_path / "result.md"
        _unwritable_output(monkeypatch, out, fail_writes=False, fail_close=True)
        code, _, _ = _run_main(
            tmp_path,
            ["opus-review", "--prompt", "p", "--output", str(out)],
            exit_code=3,
        )
        assert code == 3
        assert "cannot close --output" in capsys.readouterr().err
        # The footer itself wrote fine — only the close failed.
        assert "[spawn-harness] child FAILED: exit code 3" in out.read_text()

    def test_close_failure_on_the_success_path_keeps_zero(
        self, tmp_path, capsys, monkeypatch
    ):
        out = tmp_path / "result.md"
        _unwritable_output(monkeypatch, out, fail_writes=False, fail_close=True)
        code, _, _ = _run_main(
            tmp_path, ["opus-review", "--prompt", "p", "--output", str(out)]
        )
        assert code == 0
        err = capsys.readouterr().err
        assert "cannot close --output" in err
        assert "no usable output" not in err


class TestMainValidation:
    def _main(self, args, environ):
        with pytest.MonkeyPatch.context() as mp:
            for key in list(spawn_harness.os.environ):
                if key.startswith("LMER_"):
                    mp.delenv(key, raising=False)
            for key, value in environ.items():
                mp.setenv(key, value)
            with pytest.raises(SystemExit) as exc:
                spawn_harness.main(args)
        return exc.value.code

    def test_missing_agent_name_exits_2(self):
        assert self._main([], _config_env()) == 2

    def test_missing_prompt_exits_2(self):
        assert self._main(["opus-review"], _config_env()) == 2

    def test_empty_prompt_exits_2(self):
        assert self._main(["opus-review", "--prompt", "  "], _config_env()) == 2

    def test_unreadable_prompt_file_exits_2(self, tmp_path):
        missing = tmp_path / "nope.md"
        code = self._main(
            ["opus-review", "--prompt-file", str(missing)], _config_env()
        )
        assert code == 2

    def test_list_prints_agents_with_harness_summary(self, capsys):
        assert self._main(["--list"], _config_env()) == 0
        out = capsys.readouterr().out
        assert "opus-review" in out and "harness=claude" in out and "model=opus" in out
        assert "sol-review" in out and "harness=codex" in out

    def test_list_without_config_is_friendly(self, capsys):
        assert self._main(["--list"], {}) == 0
        assert "No agents configured" in capsys.readouterr().out


class TestInterruptKillsChild:
    """SIGTERM/SIGINT on spawn-harness must not orphan the detached child
    process group: start_new_session detaches it from terminal signal
    delivery, so a cancelled fan-out has to kill the group itself."""

    def test_sigterm_kills_child_process_group(self, tmp_path):
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        pid_file = tmp_path / "child.pid"
        stub = fake_bin / "claude"
        stub.write_text(f'#!/bin/bash\necho $$ > "{pid_file}"\nsleep 30\n')
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        wrapper = subprocess.Popen(
            [str(BIN_WRAPPER), "opus-review", "--prompt", "p"],
            env={
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "HOME": str(tmp_path),
                "LMER_PYTHON": sys.executable,
                **_config_env(),
            },
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 15
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert pid_file.exists(), "stub child never started"
            child_pid = int(pid_file.read_text())

            wrapper.send_signal(signal.SIGTERM)
            _, stderr = wrapper.communicate(timeout=15)
            assert wrapper.returncode == 128 + signal.SIGTERM
            assert "interrupted — child killed" in stderr

            # The whole child process group must be gone (SIGKILL is
            # asynchronous — poll briefly before judging).
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            with pytest.raises(ProcessLookupError):
                os.kill(child_pid, 0)
        finally:
            if wrapper.poll() is None:
                wrapper.kill()
                wrapper.wait()


class TestBinWrapper:
    def test_wrapper_is_executable(self):
        assert BIN_WRAPPER.stat().st_mode & stat.S_IEXEC

    def test_wrapper_runs_module(self, tmp_path):
        result = subprocess.run(
            [str(BIN_WRAPPER), "--list"],
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(tmp_path),
                "LMER_PYTHON": sys.executable,
                **_config_env(),
            },
        )
        assert result.returncode == 0
        assert "opus-review" in result.stdout
