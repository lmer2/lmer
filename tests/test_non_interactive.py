"""Tests for LMER_NONINTERACTIVE delivery (issue #137).

The variable is only a signal — nothing in lmer renders an ``LMER_*`` value
into a model's context, and Claude Code discovers only ``CLAUDE.md`` natively,
so a session that is told "no human is attached" knows it because the *rule
text* was delivered. Three delivery paths, one per launch shape:

1. ``libexec/claude-runner.sh`` appends ``prompts/non-interactive.md`` to the
   file passed via ``--append-system-prompt-file`` (claude sessions).
2. ``harness_render_global_context`` writes the same fragment into the
   harness's global context file (codex/pi sessions).
3. ``spawn_harness.resolve_prompt`` prepends the rule in-band to every child
   prompt — a fan-out child execs its harness binary with no runner script,
   so neither path above runs for it. Covered in ``test_spawn_harness.py``.

Truthy parsing (``1``/``true``/``yes``) is asserted on paths 1 and 2: a strict
``=1`` check would make ``LMER_NONINTERACTIVE=true`` silently mean "a human is
present", which is the exact silent-failure class the rule exists to prevent.
"""
import pytest

from tests._claude_runner_harness import run_claude_runner, skip_if_npm_claude_present
from tests.test_harness_runners import run_harness_runner


HEADING = "## Non-interactive session — nobody is attached to answer"
TRUTHY = ["1", "true", "TRUE", "yes", "Yes"]
FALSY = ["0", "false", "no", "", "   "]


@skip_if_npm_claude_present
class TestClaudeRunnerInjection:
    """Path 1: the fragment reaches claude's system prompt."""

    def _run(self, tmp_path, env_value=None):
        env = {} if env_value is None else {"LMER_NONINTERACTIVE": env_value}
        result = run_claude_runner(tmp_path, env=env)
        return result.output, result.argv, result.prompt

    @pytest.mark.parametrize("value", TRUTHY)
    def test_truthy_injects_fragment(self, tmp_path, value):
        output, argv, prompt = self._run(tmp_path, value)
        assert "Non-interactive session notice injected" in output
        assert "--append-system-prompt-file" in argv
        assert prompt is not None
        assert HEADING in prompt

    @pytest.mark.parametrize("value", FALSY)
    def test_falsy_and_blank_do_not_inject(self, tmp_path, value):
        """`=0` must be an off-switch, not "the variable is set" (#137)."""
        output, _, prompt = self._run(tmp_path, value)
        assert "Non-interactive session notice injected" not in output
        if prompt is not None:
            assert HEADING not in prompt

    def test_unset_does_not_inject(self, tmp_path):
        output, _, prompt = self._run(tmp_path)
        assert "Non-interactive session notice injected" not in output
        if prompt is not None:
            assert HEADING not in prompt

    def test_fragment_follows_the_workspace_rules(self, tmp_path):
        """The notice is appended, so it is the most recent thing the model read."""
        _, _, prompt = self._run(tmp_path, "1")
        assert prompt is not None
        assert prompt.index(HEADING) > 0
        assert prompt.rstrip().endswith(
            "notice restates them so they apply even where that file is not in context."
        )


class TestGlobalContextInjection:
    """Path 2: the fragment reaches codex/pi through the global context file."""

    # Each runner names its own global context file (codex-runner.sh,
    # pi-runner.sh); harness_render_global_context only knows the target path.
    CONTEXT_FILES = {"codex": ".codex/AGENTS.md", "pi": ".pi/agent/AGENTS.md"}

    @pytest.mark.parametrize("harness", ["codex", "pi"])
    def test_truthy_writes_fragment_to_context(self, tmp_path, harness):
        run_harness_runner(harness, tmp_path, env={"LMER_NONINTERACTIVE": "true"})
        context = (tmp_path / self.CONTEXT_FILES[harness]).read_text()
        assert HEADING in context

    @pytest.mark.parametrize("value", FALSY)
    def test_falsy_writes_nothing(self, tmp_path, value):
        run_harness_runner("codex", tmp_path, env={"LMER_NONINTERACTIVE": value})
        context = tmp_path / ".codex" / "AGENTS.md"
        if context.exists():
            assert HEADING not in context.read_text()

    def test_fragment_alone_is_enough_to_write_the_file(self, tmp_path):
        """No user AGENTS.md, no identity, no memory — the notice still lands.

        harness_render_global_context only writes when something contributed
        content, so the notice has to count as content on its own.
        """
        run_harness_runner("codex", tmp_path, env={"LMER_NONINTERACTIVE": "1"})
        context = (tmp_path / ".codex" / "AGENTS.md").read_text()
        assert HEADING in context
        assert "lmer-managed" in context
