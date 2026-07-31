"""Tests for the built-in `release` taskdef's needs manifest (task.yaml).

The manifest declares what a release session REQUIRES — the release
credentials (by stable need-name; provisioning/scoping semantics and the
names themselves are owned by the credentials subsystem), the push-allow
targets for the GitHub mirror and GitLab tag pushes, and the masterplan
flag. It declares needs only: no secret material, no key-file paths, no
env var names, and no fallback that lets an unprovisioned session proceed.

Critically, it carries NO `release:` parameter mapping — the fail-loud
parameter contract (Phase 0.5 of instructions.txt, covered by
tests/test_release_taskdef.py) keys on the first task.yaml in tier
precedence that CONTAINS a `release:` mapping, so a needs-only built-in
manifest does not weaken it.

Resolution goes through the same tier-resolved lookup that
`taskdef_declares_masterplan` reads (`lmer_cli.container.taskdefs.
find_taskdef_file`); tier-isolation conventions mirror
tests/test_masterplan_provisioning.py, builtin-root pinning mirrors
tests/test_release_taskdef.py.
"""
from pathlib import Path

import pytest
import yaml

from lmer_cli.container import masterplan, taskdefs
from tests.conftest import strip_lmer_env

REPO_TASKDEF = Path(__file__).parent.parent / "taskdef"
MANIFEST = REPO_TASKDEF / "release" / "task.yaml"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    """Strip LMER_* env vars so each test builds its own config."""
    strip_lmer_env(monkeypatch)


@pytest.fixture(autouse=True)
def _repo_builtin_root(monkeypatch):
    """Pin the builtin tier to this checkout's taskdef/ — the tests must
    exercise the manifest under development, not a container mount."""
    monkeypatch.setattr(
        "lmer_cli.container.taskdefs.builtin_taskdef_root",
        lambda: REPO_TASKDEF,
    )


def _isolate_taskdef_tiers(monkeypatch, tmp_path):
    """Point the work-repo tiers at a tmp work repo, away from ambient env.

    Without this, the tier search would consult the running session's real
    work repo via inherited LMER_* vars (same isolation as
    tests/test_masterplan_provisioning.py).
    """
    work = tmp_path / "work"
    (work / "taskdef").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))
    monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
    monkeypatch.setenv("LMER_REPO_PROJECT", "group/proj")
    return work


def _write_release_manifest(work, body, tier="project"):
    """Ship a release/task.yaml override in a work-repo tier."""
    if tier == "global":
        tdir = work / "taskdef" / "release"
    else:
        tdir = work / "git.example.com" / "group/proj" / "taskdef" / "release"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "instructions.txt").write_text("body\n")
    (tdir / "task.yaml").write_text(body)
    return tdir / "task.yaml"


def _load():
    return yaml.safe_load(MANIFEST.read_text())


class TestManifestShape:
    """The built-in manifest parses and declares every required need."""

    def test_manifest_parses_to_a_mapping(self):
        assert MANIFEST.exists()
        data = _load()
        assert isinstance(data, dict)

    def test_declares_both_release_credentials(self):
        """The PAT and the signing key, by stable need-name — the
        credentials subsystem owns the names and their scoping."""
        creds = _load()["needs"]["credentials"]
        assert "release_github_pat" in creds
        assert "release_signing_key" in creds

    def test_declares_push_allow_targets(self):
        """Push-target needs for the GitHub mirror (main + tag) and the
        GitLab tag push — roles, never concrete repo URLs (those are
        per-adopter `release:` parameters)."""
        data = _load()
        entries = {e["target"]: e["refs"] for e in data["needs"]["push_targets"]}
        assert set(entries) == {"github_mirror", "gitlab_origin"}
        assert "refs/heads/main" in entries["github_mirror"]
        assert "refs/tags/*" in entries["github_mirror"]
        assert entries["gitlab_origin"] == ["refs/tags/*"]
        # The documentation block must NOT share the name of the live grant
        # key: `push_allow` at the top level IS a push grant (#107), and a
        # same-named block one indent away is one stray dedent from
        # becoming one.
        assert "push_allow" not in data
        assert "push_allow" not in data["needs"]

    def test_masterplan_flag_declared_off(self):
        """The release flow's durable state is `work` run state, not the
        masterplan plugin — the flag is declared, and declared false."""
        assert _load()["masterplan"] is False

    def test_masterplan_not_provisioned_for_release_sessions(
        self, monkeypatch, tmp_path
    ):
        """End-to-end through the same reader the provisioning gate uses:
        a release session resolves this manifest and stays plain."""
        _isolate_taskdef_tiers(monkeypatch, tmp_path)
        monkeypatch.setenv("LMER_TASK", "release")
        assert masterplan.masterplan_enabled() is False

    def test_carries_no_release_mapping_and_says_why(self):
        """Needs only — the fail-loud parameter contract keys on the first
        tier whose task.yaml CONTAINS a `release:` mapping, so the built-in
        must not carry one (and must document the deliberate absence)."""
        assert "release" not in _load()
        assert "NO `release:`" in MANIFEST.read_text()

    def test_no_secret_material_paths_or_env_names(self):
        """Needs declarations only: no env var names, no container key
        path, no key or token material, no unprovisioned-fallback knob."""
        text = MANIFEST.read_text()
        assert "LMER_" not in text
        assert "/release-signing-key" not in text
        assert "PRIVATE KEY" not in text
        assert "ghp_" not in text and "github_pat_" not in text
        assert "fallback: " not in text and "optional" not in text


class TestTierResolution:
    """The manifest resolves through the standard taskdef tier precedence —
    the same lookup `taskdef_declares_masterplan` reads."""

    def test_builtin_resolves_with_no_higher_tier(self, monkeypatch, tmp_path):
        _isolate_taskdef_tiers(monkeypatch, tmp_path)
        monkeypatch.setenv("LMER_TASK", "release")
        assert taskdefs.find_taskdef_file("task.yaml", "release") == MANIFEST

    def test_project_tier_shadows_builtin(self, monkeypatch, tmp_path):
        work = _isolate_taskdef_tiers(monkeypatch, tmp_path)
        override = _write_release_manifest(
            work, "release:\n  tag_prefix: v\n", tier="project"
        )
        assert taskdefs.find_taskdef_file("task.yaml", "release") == override

    def test_work_global_tier_shadows_builtin(self, monkeypatch, tmp_path):
        work = _isolate_taskdef_tiers(monkeypatch, tmp_path)
        override = _write_release_manifest(
            work, "release:\n  tag_prefix: v\n", tier="global"
        )
        assert taskdefs.find_taskdef_file("task.yaml", "release") == override


class TestMalformedManifestDegrades:
    """A malformed manifest counts as "not declared" — provisioning is
    logged-never-fatal, so bad YAML must never take the session down."""

    @pytest.mark.parametrize("body", ["{ not yaml\n", "- a\n- list\n", ""])
    def test_malformed_override_degrades_not_raises(
        self, monkeypatch, tmp_path, body
    ):
        work = _isolate_taskdef_tiers(monkeypatch, tmp_path)
        _write_release_manifest(work, body, tier="project")
        monkeypatch.setenv("LMER_TASK", "release")
        # Must not raise, and must read as "not declared".
        assert masterplan.taskdef_declares_masterplan() is False
        assert masterplan.masterplan_enabled() is False

    @pytest.mark.parametrize("body", ["{ not yaml\n", "- a\n- list\n", ""])
    def test_malformed_builtin_manifest_degrades(
        self, monkeypatch, tmp_path, body
    ):
        """Same guarantee when the built-in tier itself is the bad file."""
        _isolate_taskdef_tiers(monkeypatch, tmp_path)
        broken_root = tmp_path / "builtin"
        rdir = broken_root / "release"
        rdir.mkdir(parents=True)
        (rdir / "instructions.txt").write_text("body\n")
        (rdir / "task.yaml").write_text(body)
        monkeypatch.setattr(
            "lmer_cli.container.taskdefs.builtin_taskdef_root",
            lambda: broken_root,
        )
        monkeypatch.setenv("LMER_TASK", "release")
        assert masterplan.taskdef_declares_masterplan() is False
        assert masterplan.masterplan_enabled() is False
