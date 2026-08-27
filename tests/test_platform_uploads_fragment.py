"""The prompt fragment that tells a session it can be handed a file (#246).

A mount an agent is never told about is a mount nothing reads, so the store's
other half is this fragment — gated on ``LMER_UPLOADS_DIR``, delivered through
both paths a fragment travels: claude's ``--append-system-prompt-file`` and
every other harness's global context file
(:mod:`tests.test_orchestrator_ask_fragment` covers the same two layers for the
ask channel, and this module deliberately mirrors it).

The gate is the property worth guarding. The variable is set only where the
platform actually prepared a store (:func:`lmer_platform.spawn.spawn_session`),
so a session that cannot be handed a file must be told nothing at all — an agent
that believes in a directory it has not got is one that reports an operator's
screenshot as missing.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

from lmer_platform import uploads
from tests._claude_runner_harness import run_claude_runner, skip_if_npm_claude_present
from tests.test_harness_runners import run_harness_runner

REPO_ROOT = Path(__file__).parent.parent
RENDERER = REPO_ROOT / "libexec" / "render-prompt-fragment.py"
TEMPLATE = REPO_ROOT / "prompts" / "orchestrator-uploads.md.jinja2"

#: The heading the fragment renders under; every injection test looks for it.
HEADING = "## Files your operator hands you"

#: What the fragment is announced as by both runners.
LABEL = "Operator upload store"


def render_fragment():
    """The fragment as a session receives it, through the shipped renderer."""
    env = {**os.environ, uploads.UPLOADS_DIR_ENV: uploads.CONTAINER_UPLOADS_DIR}
    result = subprocess.run(
        [sys.executable, str(RENDERER), str(TEMPLATE)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


class TestPackagedTemplate:
    def test_template_exists(self):
        assert TEMPLATE.is_file(), f"missing template: {TEMPLATE}"

    def test_it_names_the_directory_the_files_arrive_in(self):
        """Rendered from the variable rather than written out, so the path in the
        instructions is the path that was mounted."""
        rendered = render_fragment()
        assert HEADING in rendered
        assert uploads.CONTAINER_UPLOADS_DIR in rendered
        assert "{{" not in rendered, "an unrendered placeholder reached the prompt"

    def test_it_says_the_message_is_the_notification(self):
        """Nothing polls this directory. An agent waiting to be told twice waits
        forever."""
        rendered = render_fragment().lower()
        assert "names its full path" in rendered or "names the path" in rendered

    def test_it_tells_a_worker_what_keeping_a_file_means(self):
        """Clarification 4: the run owns commit/push/discard, so the fragment has
        to name the place and leave the decision."""
        rendered = render_fragment()
        assert "uploads/" in rendered
        assert "run directory" in rendered or "run dir" in rendered
        assert "commit" in rendered

    def test_it_warns_that_the_work_repo_is_shared(self):
        """A screenshot can carry anything that was on the operator's screen, and
        the place a worker is told to put one is a repo every developer reads."""
        rendered = render_fragment()
        assert "shared" in rendered
        assert "credentials" in rendered

    def test_it_tells_the_supervising_session_the_store_is_its_own(self):
        """Clarification 3: uber lmer manages the host store — reads, organises,
        deletes — and nothing prunes it for it."""
        rendered = render_fragment()
        assert "uber lmer" in rendered
        assert "delete" in rendered

    def test_it_does_not_assume_the_harness_can_see_an_image(self):
        """claude can open one; codex and pi are not equivalent here, and the
        honest fallback is the mechanism itself — the file and its path."""
        rendered = render_fragment().lower()
        assert "if they cannot" in rendered

    def test_it_does_not_offer_the_store_as_a_reply_channel(self):
        """Writing a file into it shows the operator nothing: the channel back is
        lmer-ask, which the session's other fragment describes."""
        rendered = render_fragment()
        assert "lmer-ask" in rendered

    def test_it_names_the_env_var_it_is_gated_on(self):
        assert uploads.UPLOADS_DIR_ENV in TEMPLATE.read_text(encoding="utf-8")


@skip_if_npm_claude_present
class TestClaudeRunner:
    """claude gets the fragment through --append-system-prompt-file."""

    def _run(self, tmp_path, value=None):
        env = {} if value is None else {uploads.UPLOADS_DIR_ENV: value}
        return run_claude_runner(tmp_path, env, expose_python3=True)

    def test_unset_injects_nothing(self, tmp_path):
        result = self._run(tmp_path)
        assert LABEL not in result.output
        if result.prompt is not None:
            assert HEADING not in result.prompt

    def test_blank_injects_nothing(self, tmp_path):
        """The spawn *blanks* the variable for a session with no store rather
        than deleting it (a child `lmer` re-seeds its own environment from .env
        files), so blank is the case that actually occurs."""
        result = self._run(tmp_path, "")
        assert LABEL not in result.output
        if result.prompt is not None:
            assert HEADING not in result.prompt

    def test_whitespace_only_injects_nothing(self, tmp_path):
        result = self._run(tmp_path, "   ")
        assert LABEL not in result.output

    def test_set_injects_the_fragment(self, tmp_path):
        result = self._run(tmp_path, uploads.CONTAINER_UPLOADS_DIR)
        assert f"{LABEL} injected into system prompt" in result.output
        assert result.prompt is not None, "no --append-system-prompt-file was passed"
        assert HEADING in result.prompt
        assert uploads.CONTAINER_UPLOADS_DIR in result.prompt

    def test_a_value_with_shell_metacharacters_is_not_evaluated(self, tmp_path):
        """The gate reads the value and the template prints it, so an unquoted
        expansion in either would run whatever a spawner put in the variable, on
        every session start.

        Unlike the ask fragment, this one *does* interpolate the path — so the
        property is that the value arrives as text: the canary is never created,
        and the metacharacters are still there in the prompt, unexecuted."""
        canary = tmp_path / "pwned"
        value = f"/tmp/x$(touch {canary})`touch {canary}`"
        result = self._run(tmp_path, value)
        assert not canary.exists(), "the fragment path evaluated a shell command"
        assert value in (result.prompt or ""), (
            "the value reached the prompt as something other than the text it is"
        )


class TestOtherHarnesses:
    """codex and pi get the same text through their global context file."""

    def test_codex_gets_the_fragment(self, tmp_path):
        run_harness_runner(
            "codex", tmp_path,
            env={
                uploads.UPLOADS_DIR_ENV: uploads.CONTAINER_UPLOADS_DIR,
                "LMER_PYTHON": sys.executable,
            },
        )
        context = (tmp_path / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
        assert HEADING in context
        assert uploads.CONTAINER_UPLOADS_DIR in context

    def test_pi_gets_the_fragment(self, tmp_path):
        run_harness_runner(
            "pi", tmp_path,
            env={
                uploads.UPLOADS_DIR_ENV: uploads.CONTAINER_UPLOADS_DIR,
                "LMER_PYTHON": sys.executable,
            },
        )
        context = (tmp_path / ".pi" / "agent" / "AGENTS.md").read_text(encoding="utf-8")
        assert HEADING in context

    def test_a_session_with_no_store_is_told_nothing(self, tmp_path):
        """The gate's whole point: an agent that believes in a directory it has
        not got reports the operator's screenshot as missing."""
        run_harness_runner("codex", tmp_path, env={"LMER_PYTHON": sys.executable})
        context = tmp_path / ".codex" / "AGENTS.md"
        assert not context.exists() or HEADING not in context.read_text(encoding="utf-8")


def test_the_mount_the_variable_and_the_instruction_name_one_directory():
    """One fact in three places, and the pairs that can drift are checked here:
    the destination the platform mounts at, the value it exports, and the path
    the fragment prints."""
    assert uploads.CONTAINER_UPLOADS_DIR in render_fragment()
    spawn_source = (REPO_ROOT / "src" / "lmer_platform" / "spawn.py").read_text(
        encoding="utf-8"
    )
    assert "uploads.CONTAINER_UPLOADS_DIR" in spawn_source, (
        "the spawn spells the destination itself instead of reading the constant"
    )
    assert re.search(
        r"child_env\[uploads\.UPLOADS_DIR_ENV\] = uploads\.CONTAINER_UPLOADS_DIR",
        spawn_source,
    ), "the variable carries something other than the mount destination"
