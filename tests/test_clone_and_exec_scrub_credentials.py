"""Tests for clone_and_exec._scrub_credentials.

A failed `git clone https://oauth2:<token>@host/...` raises
subprocess.CalledProcessError whose str() embeds the tokenized command. The
container's clone error handlers must scrub that credential before printing it
to stderr (CodeQL does not track this leak path — the token flows through
subprocess + stdlib exception stringification).
"""

import subprocess

from lmer_cli.container.clone_and_exec import _scrub_credentials


# A realistic but fake token value (matches the glpat- shape, not a real secret).
_FAKE_URL = "https://oauth2:glpat-FAKEtoken1234567890abcd@git.example.com/org/repo.git"


class TestScrubCredentials:
    def test_strips_token_from_url(self):
        result = _scrub_credentials(_FAKE_URL)
        assert "glpat-" not in result
        assert "oauth2" not in result
        assert result == "https://git.example.com/org/repo.git"

    def test_strips_token_from_called_process_error_string(self):
        # This is the actual leak vector: str(CalledProcessError) includes the
        # command list, which carries the tokenized clone URL.
        cmd = ["git", "clone", _FAKE_URL, "/workspace"]
        err = subprocess.CalledProcessError(128, cmd)
        scrubbed = _scrub_credentials(str(err))
        assert "glpat-" not in scrubbed
        assert "oauth2:" not in scrubbed
        # The non-secret context (host, exit status) is preserved for debugging.
        assert "git.example.com" in scrubbed
        assert "128" in scrubbed

    def test_strips_basic_userinfo(self):
        text = "fatal: could not read from https://user:hunter2@git.example.com/x.git"
        scrubbed = _scrub_credentials(text)
        assert "hunter2" not in scrubbed
        assert "git.example.com" in scrubbed

    def test_preserves_text_without_credentials(self):
        text = "fatal: repository 'https://git.example.com/x.git' not found"
        assert _scrub_credentials(text) == text

    def test_handles_empty(self):
        assert _scrub_credentials("") == ""
