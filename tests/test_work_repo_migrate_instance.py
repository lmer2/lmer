"""`work migrate --instance` — the instance-surface fold (#368; run-identity
spec §13.B, §10 rollout E→B; astra R7/PR1).

What these tests pin:

- the version floor: the running release must carry E.probe's
  ``MANIFEST_TIER_SUPPORTED`` marker, or ``--instance`` refuses in both dry
  run and apply — removing ``sources.yaml`` would switch declared sources
  off on a pre-#370 host;
- the three instance moves (`agent-files/claude`, `mirrors`, `taskdef/`);
  `napkin/` stays, with a note, because no release reads `_napkin/` yet and the standalone ``sources.yaml`` folded into
  ``_lmer.yaml sources:`` through the writer, removed in ONE commit;
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
from unittest.mock import patch

import pytest
from lmer_schema.manifest import load_manifest

from lmer_cli import gate_lock
from lmer_cli.container import clone_and_exec
from lmer_cli.container import sources as sources_mod
from lmer_cli.container.sources import derive_clone_url, load_sources
from tests.conftest import strip_lmer_env
from work_repo import git_ops, migrate

PROJECT = "lmer"
WORK_URL = "https://git.example.test/example-group/example-project.git"
MANIFEST = (
    "schema: 1\n"
    "name: example\n"
    "projects:\n"
    f"  {PROJECT}:\n"
    "    repo: git.example.test/example-group/example-project\n"
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


@pytest.fixture(autouse=True)
def _manifest_tier_release(monkeypatch):
    """Run as a release that reads the manifest tier. E's capability marker
    is set here rather than assumed present, so the floor tests below decide
    absence for themselves."""
    monkeypatch.setattr(sources_mod, "MANIFEST_TIER_SUPPORTED", True, raising=False)


def _git(cwd, *args) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )
    return result.stdout


def _build_instance_tree(root):
    root.mkdir(parents=True)
    (root / "_lmer.yaml").write_text(MANIFEST, encoding="utf-8")
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


AUTHED_WORK_URL = "https://oauth2:s3cret@git.example.test/example-group/example-project.git"


def _container_resolution(root, work_url):
    """What a launch against *root* does with its declared sources: the
    container seam (manifest tier, then the standalone loader) with the clone
    stubbed. ``(rc, env, clones)``."""
    env = {"LMER_WORK_REPO_PATH": str(root)}
    clones = []

    def clone(dest, url, branch, ref, *, manage_existing):
        clones.append((str(dest), url, ref))

    with patch.object(clone_and_exec, "ensure_clone", side_effect=clone):
        rc = clone_and_exec._resolve_declared_sources(
            root, work_url, env=env, isatty=False, input_fn=lambda *_: "n"
        )
    return rc, env, clones


class TestPlan:
    def test_dry_run_lists_every_move_and_changes_nothing(self, work):
        before = _snapshot(work)
        rc, text = _render(work)
        assert rc == 0
        for src, _dst in migrate.INSTANCE_RENAMES:
            assert f"git mv {src}" in text
        assert "napkin/: left in place: no lmer release reads `_napkin/` yet" in text
        assert "git mv napkin" not in text
        assert "fold sources.yaml" in text
        assert _snapshot(work) == before

    def test_the_plan_is_data(self, work):
        plan = migrate.plan_instance(root=work, work_repo_url=WORK_URL)
        assert {move.src for move in plan.moves} == {
            "agent-files/claude", "mirrors", "taskdef"
        }
        assert any(note.startswith("napkin/: left in place") for note in plan.notes)
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
        # napkin/ is never moved: nothing reads `_napkin/` yet.
        assert (work / "napkin" / "example" / "idea.md").is_file()
        assert not (work / "_napkin").exists()
        assert (work / "_taskdef" / "review.md").is_file()
        assert not (work / "sources.yaml").exists()
        assert _manifest_sources(work) == {
            "taskdef": {"repo": "https://git.example.test/agents/taskdef.git", "ref": "main"},
            "napkin": {"repo": "https://git.example.test/agents/napkin.git"},
        }
        assert "sources.yaml" in commits[0].paths
        assert "_lmer.yaml" in commits[0].paths

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
            manifest_path=work / "_lmer.yaml",
        )
        assert before["sources"]["taskdef"] == after["sources"]["taskdef"]
        # And the derived clone is identical on both sides.
        assert derive_clone_url(
            before["sources"]["taskdef"]["repo"], WORK_URL
        ) == derive_clone_url(after["sources"]["taskdef"]["repo"], WORK_URL)

    @pytest.mark.parametrize("work_url,legacy", [
        # An anonymous HTTPS `…/tasks.git` source.
        (WORK_URL, "  taskdef:\n    repo: https://git.example.test/agents/tasks.git\n"
                   "    ref: main\n"),
        # An authenticated work-repo URL: the declared clone still derives
        # its credential from it.
        (AUTHED_WORK_URL, "  taskdef:\n    repo: https://git.example.test/agents/tasks.git\n"
                          "  napkin:\n    repo: https://git.example.test/agents/napkin.git\n"),
        # An ssh transport on a non-default port.
        (WORK_URL, "  taskdef:\n    repo: ssh://git@git.example.test:2222/agents/tasks.git\n"),
    ])
    def test_the_container_seam_resolves_identically_before_and_after(
        self, work, commits, work_url, legacy
    ):
        (work / "sources.yaml").write_text("schema: 1\nsources:\n" + legacy, encoding="utf-8")
        _commit_all(work, "declared sources")
        before = _container_resolution(work, work_url)
        assert before[0] == 0 and before[2]
        if "@" in work_url:
            # The declared clone borrows the work repo's credential.
            assert all("oauth2:s3cret@" in url for _dest, url, _ref in before[2])
        rc = migrate.migrate_instance(
            root=work, apply=True, work_repo_url=work_url, out=lambda _l: None,
            err=lambda _l: None,
        )
        assert rc == 0
        assert not (work / "sources.yaml").exists()
        assert _container_resolution(work, work_url) == before

    def test_a_manifest_entry_wins_and_warns(self, work, commits):
        (work / "_lmer.yaml").write_text(
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
        assert text.count("takes precedence") == 1

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

    def test_the_trust_rule_uses_the_work_repo_url_from_the_environment(
        self, work, monkeypatch
    ):
        # The command path passes no URL: it must resolve LMER_WORK_REPO,
        # never skip the same-host check (and never use LMER_REPO_URL).
        (work / "sources.yaml").write_text(
            "schema: 1\nsources:\n  napkin:\n"
            "    repo: https://evil.example.org/20c/napkin.git\n",
            encoding="utf-8",
        )
        _commit_all(work, "cross-host source")
        before = _snapshot(work)
        monkeypatch.setenv("LMER_WORK_REPO", WORK_URL)
        monkeypatch.setenv("LMER_REPO_URL", "https://evil.example.org/20c/project.git")
        lines = []
        rc = migrate.migrate_instance(root=work, apply=True, out=lines.append, err=lines.append)
        assert rc == 1
        assert "LMER_NAPKIN_REPO" in "\n".join(lines)
        assert _snapshot(work) == before

    def test_the_trust_rule_falls_back_to_the_work_repo_origin(self, work):
        _git(work, "remote", "add", "origin", WORK_URL)
        assert migrate._work_repo_url(work) == WORK_URL

    def test_an_unknown_work_repo_url_refuses_rather_than_skipping_the_check(self, work):
        lines = []
        rc = migrate.migrate_instance(root=work, out=lines.append, err=lines.append)
        text = "\n".join(lines)
        assert rc == 0  # the dry run reports; the apply refuses
        assert "trust rule cannot be checked" in text
        before = _snapshot(work)
        rc = migrate.migrate_instance(root=work, apply=True, out=lines.append, err=lines.append)
        assert rc == 1
        assert _snapshot(work) == before

    @pytest.mark.parametrize("schema_line", ["schema: 2\n", ""])
    def test_a_file_the_loader_refuses_is_never_folded(self, work, schema_line):
        (work / "sources.yaml").write_text(
            schema_line + "sources:\n  taskdef:\n"
            "    repo: https://git.example.test/agents/taskdef.git\n",
            encoding="utf-8",
        )
        _commit_all(work, "unsupported schema")
        before = _snapshot(work)
        rc, text = _render(work, apply=True)
        assert rc == 1
        assert "schema" in text
        assert _snapshot(work) == before


class TestLoaderParity:
    def test_an_empty_sources_section_folds_to_nothing(self, work, commits):
        (work / "sources.yaml").write_text("schema: 1\nsources:\n", encoding="utf-8")
        _commit_all(work, "empty sources")
        rc, _text = _render(work, apply=True)
        assert rc == 0
        assert not (work / "sources.yaml").exists()
        assert _manifest_sources(work) == {}

    def test_names_the_loader_ignores_are_not_folded(self, work, commits):
        (work / "sources.yaml").write_text(
            "schema: 1\nsources:\n  bogus: hello\n  extra:\n    repo: x\n"
            "  taskdef:\n    repo: https://git.example.test/agents/taskdef.git\n",
            encoding="utf-8",
        )
        _commit_all(work, "unknown names")
        rc, text = _render(work, apply=True)
        assert rc == 0
        assert set(_manifest_sources(work)) == {"taskdef"}
        assert "ignored" in text


class TestVersionFloor:
    @pytest.mark.parametrize("apply", [False, True])
    def test_probe_absent_refuses_with_nothing_written(self, work, monkeypatch, apply):
        before = _snapshot(work)
        monkeypatch.delattr(sources_mod, "MANIFEST_TIER_SUPPORTED", raising=False)
        rc, text = _render(work, apply=apply)
        assert rc == 1
        assert "version floor" in text
        assert "deploy #370 fleet-wide before applying the instance fold" in text
        assert _snapshot(work) == before

    def test_probe_false_refuses(self, work, monkeypatch):
        monkeypatch.setattr(sources_mod, "MANIFEST_TIER_SUPPORTED", False)
        rc, text = _render(work)
        assert rc == 1
        assert "version floor" in text

    def test_the_dry_run_names_the_rollout_floor(self, work):
        rc, text = _render(work)
        assert rc == 0
        assert "deploy #370 fleet-wide before applying the instance fold" in text

    def test_a_legacy_tree_without_a_manifest_refuses(self, tmp_path, monkeypatch):
        root = tmp_path / "legacy"
        root.mkdir()
        (root / "sources.yaml").write_text(LEGACY_SOURCES, encoding="utf-8")
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(root))
        rc, text = _render(root)
        assert rc == 1
        assert "work adopt" in text
