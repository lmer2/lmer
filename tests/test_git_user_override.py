"""Tests for LMER_GIT_USER_NAME / LMER_GIT_USER_EMAIL plumbing.

Covers two layers:

1. cli.py env dict: source-level guard that both entries are still wired
   into the host→container env dict (mirrors ``test_human_identity`` and
   ``test_lmer_reasoning_effort``). The dict is built inline in ``main()``,
   so a source-level check guards against accidental removal without
   re-testing the trivial ``os.environ.get`` logic.
2. entrypoint.sh: the git-identity block exports git's native
   ``GIT_AUTHOR_*``/``GIT_COMMITTER_*`` vars from the LMER overrides,
   independently, and exports nothing when neither override is set. The
   real block is extracted from the shipped entrypoint and executed so the
   test exercises the actual code, not a re-implementation.
"""
import re
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent
CLI_PY = REPO_ROOT / "src" / "lmer_cli" / "cli.py"
ENTRYPOINT = REPO_ROOT / "Ctl" / "container" / "entrypoint.sh"

# Markers delimiting the git-identity block inside entrypoint.sh. Kept in sync
# with the comments in the script; if the block is renamed these tests fail
# loudly rather than silently passing on stale code.
BLOCK_START = "# ── Git identity overrides ──"
BLOCK_END = "# Check if we're in a git repository"


def test_cli_env_dict_declares_git_user_name():
    """Guard against accidental removal of LMER_GIT_USER_NAME from cli.py's env dict."""
    source = CLI_PY.read_text()
    pattern = re.compile(
        r"""["']LMER_GIT_USER_NAME["']\s*:\s*os\.environ\.get\(\s*["']LMER_GIT_USER_NAME["']\s*\)"""
    )
    assert pattern.search(source), "LMER_GIT_USER_NAME entry missing from cli.py env dict"


def test_cli_env_dict_declares_git_user_email():
    """Guard against accidental removal of LMER_GIT_USER_EMAIL from cli.py's env dict."""
    source = CLI_PY.read_text()
    pattern = re.compile(
        r"""["']LMER_GIT_USER_EMAIL["']\s*:\s*os\.environ\.get\(\s*["']LMER_GIT_USER_EMAIL["']\s*\)"""
    )
    assert pattern.search(source), "LMER_GIT_USER_EMAIL entry missing from cli.py env dict"


def _extract_block() -> str:
    """Return the git-identity block from the real entrypoint.sh.

    Extracting and running the actual lines (rather than a copy) keeps the
    test honest if the script changes.
    """
    text = ENTRYPOINT.read_text()
    assert BLOCK_START in text, f"entrypoint marker missing: {BLOCK_START!r}"
    assert BLOCK_END in text, f"entrypoint marker missing: {BLOCK_END!r}"
    start = text.index(BLOCK_START)
    end = text.index(BLOCK_END, start)
    return text[start:end]


def _run_block(name=None, email=None):
    """Execute the extracted entrypoint block, returning the resulting GIT_* env.

    The block is followed by an `env` dump so we can inspect exactly which
    git identity variables it exported. Vars are passed in (or left unset) to
    mirror how the entrypoint receives LMER_GIT_USER_NAME/EMAIL.
    """
    block = _extract_block()
    script = block + '\nfor v in GIT_AUTHOR_NAME GIT_COMMITTER_NAME GIT_AUTHOR_EMAIL GIT_COMMITTER_EMAIL; do\n  eval "val=\\${$v+set:\\$$v}"\n  echo "$v=$val"\ndone\n'

    env = {"PATH": "/usr/bin:/bin"}
    if name is not None:
        env["LMER_GIT_USER_NAME"] = name
    if email is not None:
        env["LMER_GIT_USER_EMAIL"] = email

    result = subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    parsed = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            parsed[k] = v  # "" when unset, "set:<value>" when exported
    return result.stdout, parsed


class TestEntrypointGitIdentityBlock:
    """The git-identity block in entrypoint.sh exports the right vars."""

    def test_neither_set_exports_nothing(self):
        out, parsed = _run_block(name=None, email=None)
        assert parsed["GIT_AUTHOR_NAME"] == ""
        assert parsed["GIT_COMMITTER_NAME"] == ""
        assert parsed["GIT_AUTHOR_EMAIL"] == ""
        assert parsed["GIT_COMMITTER_EMAIL"] == ""
        assert "overridden" not in out

    def test_name_only_exports_name_vars_only(self):
        out, parsed = _run_block(name="Bot Account", email=None)
        assert parsed["GIT_AUTHOR_NAME"] == "set:Bot Account"
        assert parsed["GIT_COMMITTER_NAME"] == "set:Bot Account"
        # Email half falls back to gitconfig — nothing exported.
        assert parsed["GIT_AUTHOR_EMAIL"] == ""
        assert parsed["GIT_COMMITTER_EMAIL"] == ""
        assert "LMER_GIT_USER_NAME" in out

    def test_email_only_exports_email_vars_only(self):
        out, parsed = _run_block(name=None, email="bot@example.com")
        assert parsed["GIT_AUTHOR_EMAIL"] == "set:bot@example.com"
        assert parsed["GIT_COMMITTER_EMAIL"] == "set:bot@example.com"
        assert parsed["GIT_AUTHOR_NAME"] == ""
        assert parsed["GIT_COMMITTER_NAME"] == ""
        assert "LMER_GIT_USER_EMAIL" in out

    def test_both_set_exports_all_four(self):
        out, parsed = _run_block(name="Bot Account", email="bot@example.com")
        assert parsed["GIT_AUTHOR_NAME"] == "set:Bot Account"
        assert parsed["GIT_COMMITTER_NAME"] == "set:Bot Account"
        assert parsed["GIT_AUTHOR_EMAIL"] == "set:bot@example.com"
        assert parsed["GIT_COMMITTER_EMAIL"] == "set:bot@example.com"

    def test_empty_string_is_treated_as_unset(self):
        """An exported-but-empty override must not blank out the git identity."""
        out, parsed = _run_block(name="", email="")
        assert parsed["GIT_AUTHOR_NAME"] == ""
        assert parsed["GIT_COMMITTER_NAME"] == ""
        assert parsed["GIT_AUTHOR_EMAIL"] == ""
        assert parsed["GIT_COMMITTER_EMAIL"] == ""
        assert "overridden" not in out
