#!/usr/bin/env python3
"""Coverage for the widened redaction patterns (issue #124, C1 + C2).

Complements tests/test_work_repo_redact.py, which covers the original
glpat-/sk- prefixes and the TOKEN/SECRET/PASSWORD env var names. Token-shaped
values here are built by concatenation so no literal ever looks like a real
credential to the repo's secret scanner.
"""

import os
from unittest.mock import patch

import pytest

from work_repo.utils import (
    _MIN_SECRET_LENGTH,
    _REDACTED,
    _collect_secret_values,
    redact_secrets,
)


class TestValuePrefixFamilies:
    """Known token prefixes are redacted without any matching env var."""

    @pytest.mark.parametrize(
        "sample",
        [
            "ghp_" + "A" * 30,
            "gho_" + "B" * 30,
            "ghu_" + "C" * 30,
            "ghs_" + "D" * 30,
            "ghr_" + "E" * 30,
            "github_pat_" + "A" * 30,
            "github_pat_" + "1" * 12 + "_" + "a" * 20,
            "xoxb-" + "1234567890abc",
            "xoxp-" + "1234567890abc",
            "xapp-" + "1-A0123456789-abcdef",
            "glrt-" + "x" * 20,
            "glrt-" + "t1_" + "a" * 12 + ".b" + "9" * 6,
            "gldt-" + "y" * 20,
        ],
    )
    def test_prefix_family_is_redacted(self, sample):
        with patch.dict(os.environ, {}, clear=True):
            result = redact_secrets(f"Authenticated with {sample} just now")
            assert sample not in result
            assert _REDACTED in result

    @pytest.mark.parametrize(
        "sample",
        [
            "glpat-" + "a1b2c3d4e5f6g7h8i9j0",
            "sk-proj-" + "a1b2c3d4e5f6g7h8i9j0",
        ],
    )
    def test_original_prefixes_still_redacted(self, sample):
        """Regression: widening the pattern kept the pre-existing families."""
        with patch.dict(os.environ, {}, clear=True):
            result = redact_secrets(f"Using {sample} for the call")
            assert sample not in result
            assert _REDACTED in result

    @pytest.mark.parametrize(
        "text",
        [
            "The ghp_short value should not match",
            "The xoxb-abc value should not match",
            "The glrt-abc value should not match",
        ],
    )
    def test_short_tails_are_not_redacted(self, text):
        """Minimum tail lengths keep prefix-lookalikes out of the output."""
        with patch.dict(os.environ, {}, clear=True):
            assert redact_secrets(text) == text


class TestUrlUserinfoBackstop:
    """Credentials embedded in URLs are stripped whatever their shape."""

    def test_userinfo_is_replaced_and_url_preserved(self):
        credentialed = "https://oauth2:" + "A" * 24 + "@git.example.com/g/p.git"
        with patch.dict(os.environ, {}, clear=True):
            result = redact_secrets(f"Cloning {credentialed} now")
            assert "oauth2" not in result
            assert "A" * 24 not in result
            assert result == f"Cloning https://{_REDACTED}@git.example.com/g/p.git now"

    def test_url_without_userinfo_is_untouched(self):
        text = "Cloning https://git.example.com/g/p.git now"
        with patch.dict(os.environ, {}, clear=True):
            assert redact_secrets(text) == text

    def test_bare_email_is_untouched(self):
        text = "contact user@example.com for access"
        with patch.dict(os.environ, {}, clear=True):
            assert redact_secrets(text) == text

    def test_url_without_userinfo_emits_no_warning(self, capsys):
        with patch.dict(os.environ, {}, clear=True):
            redact_secrets("See https://git.example.com/g/p.git for details")
            assert capsys.readouterr().err == ""

    def test_userinfo_redaction_warns(self, capsys):
        credentialed = "https://user:" + "p" * 16 + "@git.example.com/g/p.git"
        with patch.dict(os.environ, {}, clear=True):
            redact_secrets(f"Remote is {credentialed}")
            assert "secret value(s) detected and redacted" in capsys.readouterr().err


class TestWidenedNamePatterns:
    """KEY and CREDENTIALS env var names now feed _collect_secret_values."""

    def test_key_and_credentials_values_are_redacted(self):
        key_value = "deploy-" + "k" * 12
        creds_value = "service-" + "c" * 12
        env = {"DEPLOY_KEY": key_value, "SERVICE_CREDENTIALS": creds_value}
        with patch.dict(os.environ, env, clear=True):
            result = redact_secrets(
                f"Deployed with {key_value} and {creds_value} today"
            )
            assert key_value not in result
            assert creds_value not in result
            assert result.count(_REDACTED) == 2

    def test_api_key_name_still_collected(self):
        """Regression: bare KEY subsumes the old API_KEY pattern."""
        value = "api-" + "v" * 16
        with patch.dict(os.environ, {"MY_API_KEY": value}, clear=True):
            assert value in _collect_secret_values()

    def test_short_key_value_is_not_collected(self):
        """The length floor still guards KEY-named vars against false positives."""
        value = "yes"
        assert len(value) < _MIN_SECRET_LENGTH
        with patch.dict(os.environ, {"FEATURE_KEY": value}, clear=True):
            assert _collect_secret_values() == []

    def test_short_key_value_is_not_replaced_in_text(self):
        with patch.dict(os.environ, {"FEATURE_KEY": "yes"}, clear=True):
            text = "the answer is yes for every key"
            assert redact_secrets(text) == text
