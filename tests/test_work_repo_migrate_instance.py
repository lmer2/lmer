"""`work migrate --instance` — the instance-surface fold (#368; run-identity
spec §13.B, §10 rollout E→B; astra R7/PR1).

What these tests pin:

- the version floor: the running release must carry E.probe's
  ``MANIFEST_TIER_SUPPORTED`` marker, or ``--instance`` refuses in both dry
  run and apply — removing ``sources.yaml`` would switch declared sources
  off on a pre-#370 host;
- the four instance moves (`agent-files/claude`, `mirrors`, `napkin/`,
  `taskdef/`) and the standalone ``sources.yaml`` folded into
  ``_plexus.yaml sources:`` through the writer, removed in ONE commit;
- the transport spelling is carried byte-for-byte (no registry-identity
  normalization), so the container's own resolution derives the same clone
  before and after the fold;
- a key declared in both files keeps the manifest entry and warns; a legacy
  entry that fails the loader's per-entry trust rule refuses the whole fold
  with nothing written; a second run is a no-op;
- dry-run by default.
"""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from lmer_schema.manifest import load_manifest

from lmer_cli import gate_lock
from lmer_cli.container.sources import derive_clone_url, load_sources
from tests.conftest import strip_lmer_env
from work_repo import git_ops, migrate

PROJECT = "lmer"
WORK_URL = "https://git.example.test/agents/global.git"
MANIFEST = (
    "schema: 1\n"
    "name: example\n"
    "projects:\n"
    f"  {PROJECT}:\n"
    "    repo: git.example.test/agents/global\n"
)
LEGACY_SOURCES = (
    "schema: 1\n"
    "sources:\n"
    "  taskdef:\n"
    "    repo: https://git.example.test/agents/taskdef.git\n"
    "    ref: main\n"
    "  napkin:\n"
    "    repo: https://git.example.test/agents/napkin.git\n"
)


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


def _git(cwd, *args) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )
    return result.stdout


def _build_instance_tree(root):
    root.mkdir(parents=True)
    (root / "_plexus.yaml").write_text(MANIFEST, encoding="utf-8")
    (root / "README.md").write_text("# work repo\n", encoding="utf-8")
    (root / "sources.yaml").write_text(LEGACY_SOURCES, encoding="utf-8")
    (root / "agent-files" / "claude" / "skills" / "hello").mkdir(parents=True)
    (root / "agent-files" / "claude" / "skills" / "hello" / "SKILL.md").write_text(
        "# hello\n", encoding="utf-8"
    )
    (root / "mirrors" / "masterplan").mkdir(parents=True)
    (root / "mirrors" / "masterplan" / "MIRROR.md").write_text("# mirror\n", encoding="utf-8")
    (root / "napkin" / "example").mkdir(parents=True)
    (root / "napkin" / "example" / "idea.md").write_text("# idea\n", encoding="utf-8")
    (root / "taskdef").mkdir()
    (root / "taskdef" / "review.md").write_text("# review\n", encoding="utf-8")
    return root


def _commit_all(root, message="fixture"):
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", message)


@pytest.fixture
def work(tmp_path, monkeypatch):
    """An instance tree in a committed git checkout that IS the configured
    work repo, with a same-host ``sources.yaml`` to fold."""
    root = tmp_path / "work"
    _build_instance_tree(root)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "tests@example.test")
    _git(root, "config", "user.name", "tests")
    _commit_all(root)
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(root))
    monkeypatch.setenv(gate_lock.LOCK_DIR_ENV, str(tmp_path / "gate-inflight"))
    return root


@pytest.fixture
def commits(monkeypatch):
    """Stand in for the commit → rebase → push helper: records every call,
    reports success, performs no git write."""
    calls = []

    def record(paths, message=None, allow_during_gate=False):
        calls.append(SimpleNamespace(paths=list(paths), message=message))
        return 0

    monkeypatch.setattr(git_ops, "commit_work_path", record)
    return calls


def _snapshot(root):
    out = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if rel == ".git" or rel.startswith(".git/"):
            continue
        if path.is_symlink():
            out[rel] = ("link", str(path.readlink()))
        elif path.is_file():
            out[rel] = ("file", path.read_bytes())
    return out


def _render(root, **kwargs):
    lines = []
    rc = migrate.migrate_instance(
        root=root, out=lines.append, err=lines.append, work_repo_url=WORK_URL, **kwargs
    )
    return rc, "\n".join(lines)


def _manifest_sources(root):
    return dict(load_manifest(root).sources or {})


class TestPlan:
    def test_dry_run_lists_every_move_and_changes_nothing(self, work):
        before = _snapshot(work)
        rc, text = _render(work)
        assert rc == 0
        for src, _dst in migrate.INSTANCE_RENAMES:
            assert f"git mv {src}" in text
        assert "fold sources.yaml" in text
        assert _snapshot(work) == before

    def test_the_plan_is_data(self, work):
        plan = migrate.plan_instance(root=work, work_repo_url=WORK_URL)
        assert {move.src for move in plan.moves} == {
            "agent-files/claude", "mirrors", "napkin", "taskdef"
        }
        assert plan.sources_file == work / "sources.yaml"
        assert plan.sources["taskdef"] == {
            "repo": "https://git.example.test/agents/taskdef.git", "ref": "main"
        }
        assert plan.sources["napkin"] == {
            "repo": "https://git.example.test/agents/napkin.git"
        }


class TestApply:
    def test_one_commit_moves_every_surface_and_folds_the_file(self, work, commits):
        rc, text = _render(work, apply=True)
        assert rc == 0
        assert len(commits) == 1
        assert (work / "_agents" / "claude" / "skills" / "hello" / "SKILL.md").is_file()
        assert (work / "_agents" / "mirrors" / "masterplan" / "MIRROR.md").is_file()
        assert (work / "_napkin" / "example" / "idea.md").is_file()
        assert (work / "_taskdef" / "review.md").is_file()
        assert not (work / "sources.yaml").exists()
        assert _manifest_sources(work) == {
            "taskdef": {"repo": "https://git.example.test/agents/taskdef.git", "ref": "main"},
            "napkin": {"repo": "https://git.example.test/agents/napkin.git"},
        }
        assert "sources.yaml" in commits[0].paths
        assert "_plexus.yaml" in commits[0].paths

    def test_the_transport_spelling_is_carried_byte_for_byte(self, work, commits):
        # The declared repo is a transport, never a registry identity: an
        # ssh non-default port and a .git suffix survive the fold.
        (work / "sources.yaml").write_text(
            "schema: 1\nsources:\n  taskdef:\n"
            "    repo: ssh://git@git.example.test:2222/agents/taskdef.git\n"
            "    ref: main\n",
            encoding="utf-8",
        )
        _commit_all(work, "ssh source")
        rc, _text = _render(work, apply=True)
        assert rc == 0
        assert _manifest_sources(work)["taskdef"] == {
            "repo": "ssh://git@git.example.test:2222/agents/taskdef.git", "ref": "main"
        }

    def test_the_container_resolves_the_same_transport_before_and_after(self, work, commits):
        before, _ = load_sources(work / "sources.yaml", WORK_URL)
        rc, _text = _render(work, apply=True)
        assert rc == 0
        after, _warnings = load_sources(
            work / "sources.yaml",
            WORK_URL,
            manifest_sources=_manifest_sources(work),
            manifest_path=work / "_plexus.yaml",
        )
        assert before["sources"]["taskdef"] == after["sources"]["taskdef"]
        # And the derived clone is identical on both sides.
        assert derive_clone_url(
            before["sources"]["taskdef"]["repo"], WORK_URL
        ) == derive_clone_url(after["sources"]["taskdef"]["repo"], WORK_URL)

    def test_a_manifest_entry_wins_and_warns(self, work, commits):
        (work / "_plexus.yaml").write_text(
            MANIFEST
            + "sources:\n"
            "  taskdef:\n"
            "    repo: https://git.example.test/agents/other-taskdef.git\n"
            "    ref: stable\n",
            encoding="utf-8",
        )
        _commit_all(work, "manifest declares taskdef")
        rc, text = _render(work, apply=True)
        assert rc == 0
        assert _manifest_sources(work)["taskdef"] == {
            "repo": "https://git.example.test/agents/other-taskdef.git", "ref": "stable"
        }
        assert "takes precedence" in text

    def test_a_second_run_is_a_noop(self, work, commits):
        assert _render(work, apply=True)[0] == 0
        rc, text = _render(work, apply=True)
        assert rc == 0
        assert "nothing to migrate" in text


class TestRefusals:
    def test_a_trust_rule_violation_refuses_with_nothing_written(self, work):
        before = _snapshot(work)
        (work / "sources.yaml").write_text(
            "schema: 1\nsources:\n  napkin:\n"
            "    repo: https://evil.example.org/20c/napkin.git\n",
            encoding="utf-8",
        )
        _commit_all(work, "cross-host source")
        before = _snapshot(work)
        rc, text = _render(work, apply=True)
        assert rc == 1
        assert "LMER_NAPKIN_REPO" in text
        assert _snapshot(work) == before

    def test_probe_absent_refuses_with_nothing_written(self, work, monkeypatch):
        before = _snapshot(work)
        monkeypatch.setattr(migrate, "_instance_probe", lambda: False)
        rc, text = _render(work, apply=True)
        assert rc == 1
        assert "version floor" in text
        assert "#370" in text
        assert _snapshot(work) == before

    def test_a_legacy_tree_without_a_manifest_refuses(self, tmp_path, monkeypatch):
        root = tmp_path / "legacy"
        root.mkdir()
        (root / "sources.yaml").write_text(LEGACY_SOURCES, encoding="utf-8")
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(root))
        rc, text = _render(root)
        assert rc == 1
        assert "work adopt" in text
