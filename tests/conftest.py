"""Shared fixtures for tests."""
import os
from pathlib import Path
import pytest


@pytest.fixture
def project_root():
    """Get the project root directory."""
    return Path(__file__).parent.parent


@pytest.fixture
def rules_dir(project_root):
    """Get the rules directory."""
    return project_root / "rules"


@pytest.fixture
def clean_env(monkeypatch):
    """Fixture to track and clean environment variables."""
    original_env = os.environ.copy()

    # Track any env vars we set
    set_vars = set()

    original_setitem = monkeypatch.setenv

    def tracking_setenv(name, value):
        set_vars.add(name)
        original_setitem(name, value)

    monkeypatch.setenv = tracking_setenv

    yield monkeypatch

    # Verify no secrets were set
    for var in set_vars:
        assert not any(secret in var.upper() for secret in ['PASSWORD', 'TOKEN', 'KEY', 'SECRET']), \
            f"Potential secret in env var name: {var}"


@pytest.fixture
def all_rule_files(rules_dir):
    """Get all rule markdown files."""
    return list(rules_dir.glob("*.md"))


@pytest.fixture
def main_config(project_root):
    """Get the main AGENTS.md file."""
    return project_root / "AGENTS.md"


@pytest.fixture
def lmer_subprocess_env():
    """Env dict for tests that shell out to the `lmer` CLI.

    The CLI requires ``LMER_WORK_REPO`` early, before unrelated codepaths
    (e.g. .env loading) run. Tests that exercise those unrelated paths need a
    value present even when CI doesn't set one — this fixture supplies a dummy
    if the real one isn't already in the environment.
    """
    return {
        **os.environ,
        "LMER_WORK_REPO": os.environ.get(
            "LMER_WORK_REPO", "git@example.com:fixture/work-repo.git"
        ),
    }
