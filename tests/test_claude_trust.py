"""Tests for the workspace pre-trust helper (issue #415).

Claude Code keys folder trust per resolved project path in ~/.claude.json.
The container's path is never the host path the user trusted, so lmer injects
the entry itself at every session start. These tests cover the helper directly
and its wiring into claude-runner.sh.
"""
import json
import stat
import subprocess
from pathlib import Path

from tests._claude_runner_harness import run_claude_runner, skip_if_npm_claude_present


REPO_ROOT = Path(__file__).parent.parent
HELPER = REPO_ROOT / "libexec" / "claude-trust.sh"
RUNNER = REPO_ROOT / "libexec" / "claude-runner.sh"


def _pretrust(config: Path, workspace: str) -> subprocess.CompletedProcess:
    """Source the helper and run claude_pretrust_workspace against a temp config."""
    script = f'source "{HELPER}"\nclaude_pretrust_workspace "{config}" "{workspace}"\n'
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30
    )


def test_seeds_config_when_absent(tmp_path):
    """No host config at all: seed trust *and* onboarding, or the wizard blocks."""
    config = tmp_path / ".claude.json"

    result = _pretrust(config, "/workspace")

    assert result.returncode == 0, result.stderr
    data = json.loads(config.read_text())
    assert data["projects"]["/workspace"]["hasTrustDialogAccepted"] is True
    assert data["hasCompletedOnboarding"] is True


def test_seeded_config_is_private(tmp_path):
    """Claude writes credentials into this file once it starts using it."""
    config = tmp_path / ".claude.json"

    _pretrust(config, "/workspace")

    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_merges_into_existing_config(tmp_path):
    """An existing config keeps every other key — it is the user's real config."""
    config = tmp_path / ".claude.json"
    config.write_text(
        json.dumps(
            {
                "numStartups": 7,
                "oauthAccount": {"accountUuid": "abc"},
                "projects": {
                    "/home/u/proj": {
                        "hasTrustDialogAccepted": True,
                        "allowedTools": ["Bash(ls:*)"],
                    }
                },
            }
        )
    )

    _pretrust(config, "/workspace")

    data = json.loads(config.read_text())
    assert data["projects"]["/workspace"]["hasTrustDialogAccepted"] is True
    assert data["numStartups"] == 7
    assert data["oauthAccount"] == {"accountUuid": "abc"}
    assert data["projects"]["/home/u/proj"]["allowedTools"] == ["Bash(ls:*)"]


def test_does_not_add_onboarding_to_existing_config(tmp_path):
    """Onboarding state is the user's; only a config we create gets one."""
    config = tmp_path / ".claude.json"
    config.write_text(json.dumps({"numStartups": 1}))

    _pretrust(config, "/workspace")

    data = json.loads(config.read_text())
    assert "hasCompletedOnboarding" not in data


def test_is_idempotent(tmp_path):
    """Runs on every container start, so a second run must change nothing."""
    config = tmp_path / ".claude.json"
    config.write_text(json.dumps({"projects": {}}))

    _pretrust(config, "/workspace")
    first = config.read_text()
    _pretrust(config, "/workspace")

    assert config.read_text() == first


def test_already_trusted_config_is_not_rewritten(tmp_path):
    """The host config is bind-mounted rw into every running container at once.

    Rewriting it on each start races the other sessions' claudes over a 73 KB
    non-atomic truncate. Stable *content* is not enough — the file must not be
    touched at all, which the compact-vs-pretty formatting here proves.
    """
    config = tmp_path / ".claude.json"
    config.write_text(
        '{"projects":{"/workspace":{"hasTrustDialogAccepted":true}},"oauthAccount":{"a":1}}'
    )
    before = config.read_bytes()

    result = _pretrust(config, "/workspace")

    assert config.read_bytes() == before
    assert "already trusted" in result.stdout


def test_recovers_from_a_zero_byte_config(tmp_path):
    """A failed write must not latch the dialog back on forever.

    jq reads empty input as no output, which the merge path cannot tell from
    invalid JSON — so an empty config takes the seed path instead.
    """
    config = tmp_path / ".claude.json"
    config.touch()
    config.chmod(0o644)

    _pretrust(config, "/workspace")

    data = json.loads(config.read_text())
    assert data["projects"]["/workspace"]["hasTrustDialogAccepted"] is True
    assert data["hasCompletedOnboarding"] is True
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_preserves_the_mode_of_an_existing_config(tmp_path):
    """umask governs what we create; it must not restyle what we merge into."""
    config = tmp_path / ".claude.json"
    config.write_text(json.dumps({"numStartups": 1}))
    config.chmod(0o644)

    _pretrust(config, "/workspace")

    assert stat.S_IMODE(config.stat().st_mode) == 0o644


def test_unwritable_target_warns_without_shell_noise(tmp_path):
    """The friendly warning is the whole message, not a trailer after bash's."""
    config = tmp_path / "config-is-a-directory"
    config.mkdir()

    result = _pretrust(config, "/workspace")

    assert result.returncode == 0
    assert "Is a directory" not in result.stdout + result.stderr


def test_normalizes_trailing_slash(tmp_path):
    """Claude matches the project key exactly — '/workspace/' would miss."""
    config = tmp_path / ".claude.json"

    _pretrust(config, "/workspace/")

    data = json.loads(config.read_text())
    assert list(data["projects"]) == ["/workspace"]


def test_uses_resolved_path(tmp_path):
    """The key claude looks up is the realpath, not the symlink we were given."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    config = tmp_path / ".claude.json"

    _pretrust(config, str(link))

    data = json.loads(config.read_text())
    assert list(data["projects"]) == [str(real.resolve())]


def test_leaves_invalid_json_untouched(tmp_path):
    """Better to show the dialog than to destroy a config we cannot parse."""
    config = tmp_path / ".claude.json"
    config.write_text("not json")

    result = _pretrust(config, "/workspace")

    assert result.returncode == 0
    assert config.read_text() == "not json"
    assert "leaving it untouched" in result.stdout


@skip_if_npm_claude_present
def test_runner_pretrusts_its_workspace(tmp_path):
    """End to end: a container with no host config still launches unblocked."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = tmp_path / "claude.json"

    result = run_claude_runner(
        tmp_path,
        {"LMER_CLAUDE_CONFIG_FILE": str(config)},
        cwd=workspace,
    )

    assert "--dangerously-skip-permissions" not in result.argv
    data = json.loads(config.read_text())
    assert data["projects"][str(workspace.resolve())]["hasTrustDialogAccepted"] is True
    assert data["hasCompletedOnboarding"] is True


@skip_if_npm_claude_present
def test_runner_merges_existing_config(tmp_path):
    """A host config that trusts other paths gains the workspace, loses nothing."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = tmp_path / "claude.json"
    config.write_text(
        json.dumps({"projects": {"/home/u/proj": {"hasTrustDialogAccepted": True}}})
    )

    run_claude_runner(
        tmp_path,
        {"LMER_CLAUDE_CONFIG_FILE": str(config)},
        cwd=workspace,
    )

    data = json.loads(config.read_text())
    assert data["projects"][str(workspace.resolve())]["hasTrustDialogAccepted"] is True
    assert data["projects"]["/home/u/proj"]["hasTrustDialogAccepted"] is True


def test_runner_sources_the_helper():
    """Source guard: the wiring is what makes the helper reach a session."""
    runner = RUNNER.read_text()
    assert "claude-trust.sh" in runner
    assert "claude_pretrust_workspace" in runner
