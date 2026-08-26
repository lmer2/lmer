"""The release ASKPASS helper only answers Git credential prompts."""

import os
import subprocess
from pathlib import Path


ASKPASS = Path(__file__).parent.parent / "bin" / "lmer-git-askpass"


def _run(prompt, **overrides):
    env = os.environ.copy()
    env.update(
        {
            "LMER_GIT_ASKPASS_USERNAME": "release-user",
            "LMER_GIT_ASKPASS_PASSWORD": "release-password",
            **overrides,
        }
    )
    return subprocess.run(
        [str(ASKPASS), prompt],
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )


def test_username_prompt_returns_only_the_configured_username():
    result = _run("Username for 'https://git.example.com':")

    assert result.returncode == 0
    assert result.stdout == "release-user\n"
    assert result.stderr == ""


def test_password_prompt_returns_only_the_configured_password():
    result = _run("Password for 'https://git.example.com':")

    assert result.returncode == 0
    assert result.stdout == "release-password\n"
    assert result.stderr == ""


def test_username_value_in_password_prompt_cannot_select_username_branch():
    result = _run(
        "Password for 'https://username@git.example.com':",
        LMER_GIT_ASKPASS_USERNAME="username",
    )

    assert result.returncode == 0
    assert result.stdout == "release-password\n"
    assert result.stderr == ""


def test_git_credential_fill_consumes_both_responses(tmp_path):
    """Exercise Git's real ASKPASS prompt contract without network access.

    The probe is intentionally isolated from all ambient Git configuration.
    For example, ``credential.interactive=never`` disables ASKPASS whether it
    arrived through numbered, global, or repository-local configuration.
    """
    env = {
        "GIT_ASKPASS": str(ASKPASS),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": str(tmp_path),
        "LC_ALL": "C",
        "LMER_GIT_ASKPASS_USERNAME": "release-user",
        "LMER_GIT_ASKPASS_PASSWORD": "release-password",
        "PATH": os.environ["PATH"],
    }

    result = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=git.example.com\n\n",
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=5,
    )

    assert result.returncode == 0
    assert "username=release-user" in result.stdout
    assert "password=release-password" in result.stdout
    assert result.stderr == ""


def test_unknown_prompt_is_refused_with_a_diagnostic():
    result = _run("Approve release?")

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == (
        "lmer-git-askpass: unrecognized Git prompt: Approve release?\n"
    )


def test_missing_requested_value_fails_without_echoing_the_other_value():
    result = _run(
        "Password for 'https://git.example.com':",
        LMER_GIT_ASKPASS_PASSWORD="",
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "release-user" not in result.stderr
