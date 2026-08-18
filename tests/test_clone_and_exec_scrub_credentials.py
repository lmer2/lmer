"""Tests for clone_and_exec._scrub_credentials.

A failed `git clone https://oauth2:<token>@host/...` raises
subprocess.CalledProcessError whose str() embeds the tokenized command. The
container's clone error handlers must scrub that credential before printing it
to stderr (CodeQL does not track this leak path — the token flows through
subprocess + stdlib exception stringification).
"""

import subprocess
from unittest.mock import patch

from lmer_cli.container import clone_and_exec
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


class TestCheckoutFailureIsScrubbed:
    """The MR checkout handlers print str(CalledProcessError) too, and the
    fetch/checkout commands reach a remote whose URL may be tokenized."""

    def test_secondary_mr_checkout_failure(self, tmp_path, capsys):
        target = "https://git.example.com/group/project/-/merge_requests/7"
        err = subprocess.CalledProcessError(
            128, ["git", "-C", str(tmp_path), "fetch", _FAKE_URL, "merge-requests/7/head"]
        )

        def boom(workspace, mr_id, remote="origin", credential=None):
            raise err

        with patch.object(clone_and_exec, "_clone_with_cache"), \
             patch.object(clone_and_exec, "_persist_lfs_skip_config"), \
             patch.object(clone_and_exec, "check_call"), \
             patch.object(clone_and_exec, "_fetch_and_checkout_mr", boom):
            clone_and_exec.clone_secondary_mr(target, tmp_path)

        captured = capsys.readouterr().err
        assert "Failed to checkout secondary MR 7 branch" in captured
        assert "glpat-" not in captured
        assert "oauth2:" not in captured
        assert "git.example.com" in captured
