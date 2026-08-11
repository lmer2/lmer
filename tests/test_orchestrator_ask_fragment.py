"""Tests for the orchestrated-session prompt fragment (issue #141, T23).

The mechanism is the one docs/PROMPT-FRAGMENTS.md describes and the one
``human-identity`` already uses: a Jinja2 fragment under ``prompts/``, gated on
an env var, injected into the system prompt by ``claude-runner.sh`` and into the
global context file by ``harness_render_global_context`` for every other harness.

Chosen over a taskdef block because the orchestrator spawns *existing* taskdefs
(develop, review, followup) that live in a work repo or a taskdef repo and know
nothing about it — a fragment reaches all of them, and a taskdef partial would
only reach the ones whose base template this repo owns.

The layers, mirroring tests/test_human_identity.py: the shipped template, the
host→container env passthrough, and each runner's gate-and-append behaviour.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

from ask_channel import cli, protocol
from tests._claude_runner_harness import run_claude_runner, skip_if_npm_claude_present
from tests.test_harness_runners import run_harness_runner

REPO_ROOT = Path(__file__).parent.parent
CLI_PY = REPO_ROOT / "src" / "lmer_cli" / "cli.py"
RENDERER = REPO_ROOT / "libexec" / "render-prompt-fragment.py"
TEMPLATE = REPO_ROOT / "prompts" / "orchestrator-ask.md.jinja2"

#: The heading the fragment renders under; every injection test looks for it.
HEADING = "## Asking your operator"


def render_fragment():
    """The fragment as a session receives it, through the shipped renderer."""
    env = {**os.environ, protocol.ASK_DIR_ENV: protocol.CONTAINER_ASK_DIR}
    result = subprocess.run(
        [sys.executable, str(RENDERER), str(TEMPLATE)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


class TestPackagedTemplate:
    """Sanity checks on the shipped fragment."""

    def test_template_exists(self):
        assert TEMPLATE.is_file(), f"missing template: {TEMPLATE}"

    def test_it_renders_and_names_the_tool(self):
        rendered = render_fragment()
        assert HEADING in rendered
        assert "lmer-ask ask" in rendered
        assert "lmer-ask note" in rendered

    def test_it_tells_the_agent_what_the_exit_codes_mean(self):
        """The two an agent has to branch on, and they must match the CLI.

        Read from the CLI's own constants, so renumbering an exit code without
        rewriting the sentence that explains it fails here rather than in
        production, where the symptom is an agent treating "no answer yet" as an
        error and giving up.
        """
        text = TEMPLATE.read_text(encoding="utf-8")
        assert f"Exit code {cli.TIMEOUT_EXIT_CODE} means" in text
        assert f"Exit code {cli.NO_CHANNEL_EXIT_CODE} means" in text

    def test_it_names_the_close_verb_beside_the_resume_it_amends(self):
        """The fragment is how an agent learns the verbs exist (T34): a close
        verb only reachable from one stderr line is a question that stays open
        on the channel after the agent moved on, which is the answer-into-the-
        void case the verb was built to end."""
        text = TEMPLATE.read_text(encoding="utf-8")
        assert "lmer-ask close" in text

    def test_it_tells_the_agent_to_re_run_the_wait_between_tasks(self):
        """The drift this fragment failed to prevent once: a wait timed out, the
        agent worked for an hour, never waited again, and reported the question
        unanswered while the reply was on the channel. "Resume with `lmer-ask wait
        <id>`" was already there, so the re-arm loop has to be its own line."""
        text = TEMPLATE.read_text(encoding="utf-8")
        assert "Answers arrive while you work" in text
        assert "re-run that wait between them" in text

    def test_it_makes_the_final_check_a_rule_not_a_suggestion(self):
        """Stated imperatively and tied to the two moments it is skipped at:
        concluding nobody answered, and ending the session."""
        text = TEMPLATE.read_text(encoding="utf-8")
        assert "Before you conclude the operator has not answered" in text
        assert "record any stop reason" in text
        assert "one final time" in text

    def test_it_points_at_the_mechanical_backstops(self):
        """Prose-only gates do not hold in this fleet, so the fragment names the
        two things that will notice on the agent's behalf."""
        text = TEMPLATE.read_text(encoding="utf-8")
        assert "answered (unread)" in text, "the list marker is how it is seen"
        assert "lmer-end-session" in text, "the refusal is what enforces the rule"

    def test_it_suggests_a_watch_tool_without_assuming_one(self):
        """Harnesses differ: some have a monitor tool and some have none, so this
        is phrased as a conditional rather than as an instruction that half the
        fleet cannot follow."""
        text = TEMPLATE.read_text(encoding="utf-8")
        assert "If you have a monitor or watch tool" in text

    def test_it_makes_one_question_per_ask_a_contract(self):
        """Live failure: an agent packed a multi-part decision into one entry.

        One entry is one card with one answer, and its options send on tap, so a
        packed entry offers the operator options that each claim to answer the
        whole blob. The channel already carries several open questions at once,
        which is what the contract line points the agent at instead.
        """
        rendered = render_fragment()
        assert "One question per `lmer-ask ask`" in rendered
        assert "several open questions at once" in rendered
        assert "Options belong to exactly one question" in rendered

    def test_it_forbids_the_harness_own_question_dialog(self):
        """Live failure: an agent asked through its harness's built-in dialog
        instead of the channel, and the question was invisible — the operator is
        on the web view, and a blocked TUI prompt is neither shown nor answerable
        there. Phrased without naming any one harness's tool, which the D27 pin
        below forbids and which would leave the other harnesses uncovered.
        """
        rendered = render_fragment()
        assert "only way to reach your operator" in rendered
        assert "question or permission dialog" in rendered
        assert "not an invitation" in rendered

    def test_it_says_the_terminal_is_not_the_channel(self):
        text = TEMPLATE.read_text(encoding="utf-8").lower()
        assert "nobody is watching this terminal" in text

    def test_it_does_not_promise_a_harness_specific_tool(self):
        """D27: text plus options, so codex and pi get the same contract."""
        text = TEMPLATE.read_text(encoding="utf-8")
        assert "AskUserQuestion" not in text

    def test_it_names_the_env_var_it_is_gated_on(self):
        assert protocol.ASK_DIR_ENV in TEMPLATE.read_text(encoding="utf-8")

    def test_it_teaches_the_signal_tool_and_who_reads_it(self):
        """T122: the fragment is the only place an agent learns a command exists.

        Both halves have to be in it. The *when* — a milestone, so an MR pushed or a
        review finished rather than every step — because a signal per tool call is
        the context flooding this channel is designed around; and the *who*, because
        an agent that reads it as another way to reach the operator will use it for
        questions nobody will ever answer.
        """
        rendered = render_fragment()
        assert "lmer-signal" in rendered
        assert "milestone" in rendered
        assert "One-way" in rendered
        assert "rather than to your operator" in rendered


def test_the_commands_the_fragment_names_are_installed():
    """Prose is not a tool: a taught command that is not a console script is a
    ``command not found`` at the moment an agent finally reaches for it."""
    scripts = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'lmer-ask = "ask_channel.cli:main"' in scripts
    assert 'lmer-signal = "ask_channel.signal_cli:main"' in scripts


def test_cli_env_dict_forwards_the_ask_dir():
    """Source-level guard, as for LMER_HUMAN_IDENTITY.

    Without this entry the variable never crosses the container boundary: the
    channel is mounted, the fragment never renders, and the agent has no way to
    know it can ask. The env dict in ``main()`` is built inline, so a source
    check is what catches its removal.
    """
    source = CLI_PY.read_text(encoding="utf-8")
    pattern = re.compile(
        r"""["']LMER_ASK_DIR["']\s*:\s*os\.environ\.get\(\s*["']LMER_ASK_DIR["']\s*\)"""
    )
    assert pattern.search(source), "LMER_ASK_DIR entry missing from cli.py env dict"


@skip_if_npm_claude_present
class TestClaudeRunner:
    """claude gets the fragment through --append-system-prompt-file."""

    def _run(self, tmp_path, value=None):
        env = {} if value is None else {protocol.ASK_DIR_ENV: value}
        return run_claude_runner(tmp_path, env, expose_python3=True)

    def test_unset_injects_nothing(self, tmp_path):
        result = self._run(tmp_path)
        assert "Operator ask channel" not in result.output
        if result.prompt is not None:
            assert HEADING not in result.prompt

    def test_whitespace_only_injects_nothing(self, tmp_path):
        result = self._run(tmp_path, "   ")
        assert "Operator ask channel" not in result.output
        if result.prompt is not None:
            assert HEADING not in result.prompt

    def test_set_injects_the_fragment(self, tmp_path):
        result = self._run(tmp_path, protocol.CONTAINER_ASK_DIR)
        assert "Operator ask channel injected into system prompt" in result.output
        assert result.prompt is not None, "no --append-system-prompt-file was passed"
        assert HEADING in result.prompt
        assert "lmer-ask ask" in result.prompt

    def test_a_value_with_shell_metacharacters_is_not_evaluated(self, tmp_path):
        """The gate reads the value; it must never let the shell run it.

        The fragment's text does not interpolate the path at all, so the exposure
        is the gate itself — an unquoted expansion there would execute whatever a
        spawner put in the variable, on every session start.
        """
        canary = tmp_path / "pwned"
        result = self._run(tmp_path, f"/tmp/x$(touch {canary})`touch {canary}`")
        assert not canary.exists(), "the fragment path evaluated a shell command"
        assert result.prompt is None or "pwned" not in (result.prompt or "")


class TestOtherHarnesses:
    """codex and pi get the same text through their global context file."""

    def test_codex_gets_the_fragment(self, tmp_path):
        run_harness_runner(
            "codex", tmp_path,
            env={
                protocol.ASK_DIR_ENV: protocol.CONTAINER_ASK_DIR,
                "LMER_PYTHON": sys.executable,
            },
        )
        context = (tmp_path / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
        assert HEADING in context
        assert "lmer-ask ask" in context

    def test_pi_gets_the_fragment(self, tmp_path):
        run_harness_runner(
            "pi", tmp_path,
            env={
                protocol.ASK_DIR_ENV: protocol.CONTAINER_ASK_DIR,
                "LMER_PYTHON": sys.executable,
            },
        )
        context = (tmp_path / ".pi" / "agent" / "AGENTS.md").read_text(encoding="utf-8")
        assert HEADING in context

    def test_an_unorchestrated_session_is_told_nothing(self, tmp_path):
        """No channel, no instructions — the fragment would only mislead."""
        run_harness_runner("codex", tmp_path, env={"LMER_PYTHON": sys.executable})
        context = tmp_path / ".codex" / "AGENTS.md"
        assert not context.exists() or HEADING not in context.read_text(encoding="utf-8")


def test_the_mount_and_the_instruction_name_the_same_directory():
    """One fact, three places: the mount destination, the env var, the fragment.

    The fragment tells the agent to run ``lmer-ask``, which resolves the channel
    from ``LMER_ASK_DIR``, which the spawn sets to the destination it mounted at.
    A drift between any two of those is a session that cannot ask anything.
    """
    from lmer_platform import ask as platform_ask

    assert platform_ask.CONTAINER_ASK_DIR == protocol.CONTAINER_ASK_DIR
    assert platform_ask.ASK_DIR_ENV == protocol.ASK_DIR_ENV
