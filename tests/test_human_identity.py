"""Tests for LMER_HUMAN_IDENTITY plumbing.

Covers four layers:

1. ``resolve_human_identity`` (lmer_cli.util): explicit env var wins,
   fallback to host ``git config --global user.name``/``user.email``,
   ``None`` when nothing is available.
2. cli.py env dict: source-level guard that the entry is still wired into
   the host→container env dict (mirrors ``test_lmer_reasoning_effort``).
3. ``render-prompt-fragment.py``: standalone renderer for Jinja2 prompt
   fragment templates with LMER_* env vars.
4. ``claude-runner.sh``: when the env var is set, the rendered identity
   fragment is appended to the system-prompt file passed via
   ``--append-system-prompt-file``.
"""
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from lmer_cli.util import _git_config, resolve_human_identity
from tests._claude_runner_harness import run_claude_runner, skip_if_npm_claude_present


REPO_ROOT = Path(__file__).parent.parent
CLI_PY = REPO_ROOT / "src" / "lmer_cli" / "cli.py"
RENDERER = REPO_ROOT / "libexec" / "render-prompt-fragment.py"
IDENTITY_TEMPLATE = REPO_ROOT / "prompts" / "human-identity.md.jinja2"


class TestResolveHumanIdentity:
    """resolve_human_identity() resolution order."""

    def test_explicit_env_var_wins(self, monkeypatch):
        monkeypatch.setenv("LMER_HUMAN_IDENTITY", "Alice Example <alice>")
        # git config should not be consulted when explicit value is set
        with patch("lmer_cli.util._git_config") as git_mock:
            assert resolve_human_identity() == "Alice Example <alice>"
            git_mock.assert_not_called()

    def test_explicit_env_var_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv("LMER_HUMAN_IDENTITY", "  Alice  ")
        assert resolve_human_identity() == "Alice"

    def test_empty_env_var_falls_back(self, monkeypatch):
        monkeypatch.setenv("LMER_HUMAN_IDENTITY", "")
        with patch("lmer_cli.util._git_config") as git_mock:
            git_mock.side_effect = lambda key: {
                "user.name": "Bob",
                "user.email": "bob@example.com",
            }[key]
            assert resolve_human_identity() == "Bob <bob@example.com>"

    def test_whitespace_only_env_var_falls_back(self, monkeypatch):
        monkeypatch.setenv("LMER_HUMAN_IDENTITY", "   ")
        with patch("lmer_cli.util._git_config") as git_mock:
            git_mock.side_effect = lambda key: {
                "user.name": "Carol",
                "user.email": "carol@example.com",
            }[key]
            assert resolve_human_identity() == "Carol <carol@example.com>"

    def test_git_name_and_email(self, monkeypatch):
        monkeypatch.delenv("LMER_HUMAN_IDENTITY", raising=False)
        with patch("lmer_cli.util._git_config") as git_mock:
            git_mock.side_effect = lambda key: {
                "user.name": "Dave",
                "user.email": "dave@example.com",
            }[key]
            assert resolve_human_identity() == "Dave <dave@example.com>"

    def test_git_name_only(self, monkeypatch):
        monkeypatch.delenv("LMER_HUMAN_IDENTITY", raising=False)
        with patch("lmer_cli.util._git_config") as git_mock:
            git_mock.side_effect = lambda key: {"user.name": "Eve", "user.email": None}[key]
            assert resolve_human_identity() == "Eve"

    def test_git_email_only(self, monkeypatch):
        monkeypatch.delenv("LMER_HUMAN_IDENTITY", raising=False)
        with patch("lmer_cli.util._git_config") as git_mock:
            git_mock.side_effect = lambda key: {
                "user.name": None,
                "user.email": "frank@example.com",
            }[key]
            assert resolve_human_identity() == "frank@example.com"

    def test_returns_none_when_nothing_available(self, monkeypatch):
        monkeypatch.delenv("LMER_HUMAN_IDENTITY", raising=False)
        with patch("lmer_cli.util._git_config", return_value=None):
            assert resolve_human_identity() is None

    def test_git_missing_returns_none(self, monkeypatch):
        """If git is not on PATH, helper returns None — never raises."""
        monkeypatch.delenv("LMER_HUMAN_IDENTITY", raising=False)
        with patch("lmer_cli.util.subprocess.run", side_effect=FileNotFoundError("git")):
            assert _git_config("user.name") is None
            assert resolve_human_identity() is None

    def test_git_nonzero_return_is_none(self, monkeypatch):
        """If git returns non-zero (no such key), helper returns None."""
        monkeypatch.delenv("LMER_HUMAN_IDENTITY", raising=False)

        class FakeCompleted:
            returncode = 1
            stdout = ""

        with patch("lmer_cli.util.subprocess.run", return_value=FakeCompleted()):
            assert _git_config("user.name") is None
            assert resolve_human_identity() is None


def test_cli_env_dict_declares_human_identity():
    """Guard against accidental removal of LMER_HUMAN_IDENTITY from cli.py's env dict.

    The env dict in main() is constructed inline, so a true unit test would
    require extracting it into a helper. A source-level check is sufficient
    to catch drift, with tolerance for formatting changes.
    """
    source = CLI_PY.read_text()
    pattern = re.compile(
        r"""["']LMER_HUMAN_IDENTITY["']\s*:\s*resolve_human_identity\(\s*\)"""
    )
    assert pattern.search(source), "LMER_HUMAN_IDENTITY entry missing from cli.py env dict"


class TestRenderPromptFragment:
    """The standalone Jinja2 renderer used by claude-runner.sh."""

    def _run_renderer(self, template_path, env=None, extra_args=()):
        full_env = {"PATH": os.environ.get("PATH", "")}
        if env:
            full_env.update(env)
        return subprocess.run(
            [sys.executable, str(RENDERER), str(template_path), *extra_args],
            env=full_env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_renders_lmer_env_var(self, tmp_path):
        template = tmp_path / "frag.md.jinja2"
        template.write_text("Hello {{ LMER_HUMAN_IDENTITY }}.\n")
        result = self._run_renderer(template, env={"LMER_HUMAN_IDENTITY": "Vegu"})
        assert result.returncode == 0, result.stderr
        assert result.stdout == "Hello Vegu.\n"

    def test_non_lmer_vars_not_in_context(self, tmp_path):
        template = tmp_path / "frag.md.jinja2"
        template.write_text("{{ HOME | default('NOPE') }}\n")
        result = self._run_renderer(template, env={"HOME": "/tmp"})
        assert result.returncode == 0
        # HOME is not LMER_*, so the default fires
        assert result.stdout.strip() == "NOPE"

    def test_missing_template_returns_nonzero(self, tmp_path):
        result = self._run_renderer(tmp_path / "does-not-exist.jinja2")
        assert result.returncode != 0
        assert "not found" in result.stderr

    def test_no_args_prints_usage(self):
        result = subprocess.run(
            [sys.executable, str(RENDERER)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 2
        assert "Usage" in result.stderr

    def test_sensitive_named_vars_excluded_from_context(self, tmp_path):
        """Env vars whose names match TOKEN/KEY/SECRET/... are not exposed."""
        template = tmp_path / "frag.md.jinja2"
        template.write_text("[{{ LMER_FOO_TOKEN | default('SAFE') }}]\n")
        result = self._run_renderer(
            template,
            env={"LMER_FOO_TOKEN": "glpat-abc123"},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "[SAFE]"
        assert "glpat-abc123" not in result.stdout

    def test_urls_with_embedded_credentials_excluded(self, tmp_path):
        """LMER_REPO_URL / LMER_WORK_REPO with oauth tokens must be filtered."""
        template = tmp_path / "frag.md.jinja2"
        template.write_text("[{{ LMER_REPO_URL | default('SAFE') }}]\n")
        result = self._run_renderer(
            template,
            env={"LMER_REPO_URL": "https://oauth2:glpat-secret@gitlab.example.com/g/p.git"},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "[SAFE]"
        assert "glpat-secret" not in result.stdout

    def test_plain_urls_without_credentials_are_exposed(self, tmp_path):
        """URLs without embedded user/password still pass through normally."""
        template = tmp_path / "frag.md.jinja2"
        template.write_text("{{ LMER_REPO_URL }}\n")
        result = self._run_renderer(
            template,
            env={"LMER_REPO_URL": "https://gitlab.example.com/g/p.git"},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "https://gitlab.example.com/g/p.git"

    def test_renders_packaged_identity_template(self):
        """The shipped template renders without error when the var is set."""
        env = {**os.environ, "LMER_HUMAN_IDENTITY": "Alice Example <alice@example.com>"}
        result = subprocess.run(
            [sys.executable, str(RENDERER), str(IDENTITY_TEMPLATE)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "## Human user identity" in result.stdout
        assert "Alice Example <alice@example.com>" in result.stdout


class TestPackagedIdentityTemplate:
    """Sanity checks on the shipped prompt fragment template."""

    def test_template_file_exists(self):
        assert IDENTITY_TEMPLATE.is_file(), f"missing template: {IDENTITY_TEMPLATE}"

    def test_template_references_identity_var(self):
        assert "{{ LMER_HUMAN_IDENTITY }}" in IDENTITY_TEMPLATE.read_text()

    def test_template_does_not_contain_dropped_phrasing(self):
        """The 'do not ask' phrasing was intentionally dropped per user request."""
        assert "do not ask" not in IDENTITY_TEMPLATE.read_text().lower()


def _run_claude_runner(tmp_path, env_value=None):
    env = {} if env_value is None else {"LMER_HUMAN_IDENTITY": env_value}
    result = run_claude_runner(tmp_path, env, expose_python3=True)
    return result.output, result.argv, result.prompt


@skip_if_npm_claude_present
class TestClaudeRunnerHumanIdentity:
    """Verify claude-runner.sh injects LMER_HUMAN_IDENTITY into the system prompt."""

    def test_unset_does_not_inject(self, tmp_path):
        output, argv, prompt = _run_claude_runner(tmp_path, env_value=None)
        assert "Human identity injected" not in output
        # Prompt may be None (no AGENTS.md) or contain workspace AGENTS.md content,
        # but must not contain the identity section.
        if prompt is not None:
            assert "## Human user identity" not in prompt

    def test_empty_does_not_inject(self, tmp_path):
        output, argv, prompt = _run_claude_runner(tmp_path, env_value="")
        assert "Human identity injected" not in output
        if prompt is not None:
            assert "## Human user identity" not in prompt

    def test_whitespace_only_does_not_inject(self, tmp_path):
        """Whitespace-only values (e.g. from a manual .env) must be rejected."""
        output, argv, prompt = _run_claude_runner(tmp_path, env_value="   \t  ")
        assert "Human identity injected" not in output
        if prompt is not None:
            assert "## Human user identity" not in prompt

    def test_set_injects_identity_section(self, tmp_path):
        identity = "Alice Example <alice@example.com>"
        output, argv, prompt = _run_claude_runner(tmp_path, env_value=identity)

        assert "Human identity injected" in output
        assert "--append-system-prompt-file" in argv
        assert prompt is not None
        assert "## Human user identity" in prompt
        assert identity in prompt
        # Phrasing dropped per user request must not have crept back into the
        # identity section. Scope the check to the rendered fragment (the
        # workspace AGENTS.md preceding it can legitimately contain "do not
        # ask" in unrelated guidance).
        identity_section = prompt.split("## Human user identity", 1)[1]
        assert "do not ask" not in identity_section.lower()

    def test_value_with_shell_metacharacters_is_safe(self, tmp_path):
        """Identity values must not be evaluated as shell or by Jinja2."""
        identity = "Mallory $(touch /tmp/lmer_test_pwned) `id` <m@example.com>"
        sentinel = Path("/tmp/lmer_test_pwned")
        if sentinel.exists():
            sentinel.unlink()

        output, argv, prompt = _run_claude_runner(tmp_path, env_value=identity)

        try:
            assert not sentinel.exists(), "Shell injection succeeded — identity must not be evaluated"
            assert prompt is not None
            assert identity in prompt
        finally:
            if sentinel.exists():
                sentinel.unlink()
