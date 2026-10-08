"""`work migrate <project>` — spec §7's plain-session fold (D10, #368).

What these tests pin:

- a record whose stored slug is the obsolete derivation of its own target
  moves into the bare taskdef group, keeping its session-dir name, and its
  `slug:` scalar is rewritten — every other byte identical, no YAML dump;
- the emptied group is removed; a second run is a no-op;
- the folded tree has zero `record.plain-session-slug` findings and
  `lmer-schema migrate` lists no plain-session blocker or move;
- a post-fold `derive_slug(taskdef, recorded target)` equals each new slug;
- `_specs/` entries resolve at the new path with their original dates;
- the fold refuses while a record in a folded group is live (no final
  `session_end`) or its lifecycle is unreadable, through the shared
  `preflight_no_live_records`, and never calls a record writer;
- R1's release record is untouched in both states;
- a legacy (adopted) tree folds in the same move as the nesting.
"""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from lmer_cli import gate_lock
from lmer_schema.checks import Form, Report, Severity
from lmer_schema.checks import record as record_checks
from lmer_schema.migration import migrate as schema_migrate
from tests import plexus_fixtures as pf
from tests.conftest import strip_lmer_env
from work_repo import git_ops, migrate, run_state

PROJECT = "lmer"
MANIFEST = (
    "schema: 1\n"
    "name: example\n"
    "projects:\n"
    f"  {PROJECT}:\n"
    "    repo: gitlab.example.com/group/project\n"
)


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout


def _commit_all(root, message="fixture"):
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", message)


def _configure(monkeypatch, root, tmp_path):
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(root))
    monkeypatch.setenv(gate_lock.LOCK_DIR_ENV, str(tmp_path / "gate-inflight"))


@pytest.fixture
def schema_work(tmp_path, monkeypatch):
    """A schema-form tree with §7's groups planted in `lmer/_runs/`."""
    tree = pf.build_schema_plexus(tmp_path / "work")
    pf.plant_plain_session_records(tree.project_dir)
    _git(tree.root, "init", "-q", "-b", "main")
    _git(tree.root, "config", "user.email", "tests@example.test")
    _git(tree.root, "config", "user.name", "tests")
    _commit_all(tree.root)
    _configure(monkeypatch, tree.root, tmp_path)
    return tree.root


def _build_adopted_legacy(root):
    """A registered project whose inside is still legacy: manifest present,
    flat short name, bare `runs/` holding the plain-session groups."""
    root.mkdir(parents=True)
    (root / "_plexus.yaml").write_text(MANIFEST, encoding="utf-8")
    project = root / PROJECT
    (project / "runs").mkdir(parents=True)
    pf.plant_plain_session_records(project, area="runs", nested=False)
    return project


@pytest.fixture
def legacy_work(tmp_path, monkeypatch):
    root = tmp_path / "work"
    _build_adopted_legacy(root)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "tests@example.test")
    _git(root, "config", "user.name", "tests")
    _commit_all(root)
    _configure(monkeypatch, root, tmp_path)
    return root


@pytest.fixture
def commits(monkeypatch):
    calls = []

    def record(paths, message=None, allow_during_gate=False):
        calls.append(SimpleNamespace(paths=list(paths), message=message))
        return 0

    monkeypatch.setattr(git_ops, "commit_work_path", record)
    return calls


def _render(root, **kwargs):
    lines = []
    rc = migrate.migrate_project(PROJECT, root=root, out=lines.append, err=lines.append, **kwargs)
    return rc, "\n".join(lines)


def _plain_session_findings(root):
    report = Report(Form.SCHEMA)
    record_checks.check_record(root, report)
    return [f for f in report.findings if f.code == "record.plain-session-slug"]


def _snapshot(root):
    out = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if rel == ".git" or rel.startswith(".git/"):
            continue
        if path.is_symlink():
            out[rel] = ("link", os.readlink(path))
        elif path.is_file():
            out[rel] = ("file", path.read_bytes())
    return out


class TestSchemaForm:
    def test_dry_run_lists_the_moves_and_the_slug_rewrite(self, schema_work):
        before = _snapshot(schema_work)
        rc, text = _render(schema_work)
        assert rc == 0
        assert "git mv lmer/_runs/chat-./20260901-100000-1-0 lmer/_runs/chat/20260901-100000-1-0" in text
        assert "slug: chat-. \u2192 chat" in text
        assert _snapshot(schema_work) == before

    def test_apply_moves_and_rewrites_only_slug(self, schema_work, commits):
        record_dir = schema_work / PROJECT / "_runs" / "chat-." / "20260901-100000-1-0"
        before_state = (record_dir / "state.yaml").read_bytes()
        before_events = (record_dir / "events.jsonl").read_bytes()
        rc, _text = _render(schema_work, apply=True)
        assert rc == 0
        new_dir = schema_work / PROJECT / "_runs" / "chat" / "20260901-100000-1-0"
        assert new_dir.is_dir()
        assert not (schema_work / PROJECT / "_runs" / "chat-.").exists()
        assert (new_dir / "events.jsonl").read_bytes() == before_events
        after_state = (new_dir / "state.yaml").read_text(encoding="utf-8")
        assert "slug: chat\n" in after_state
        assert "slug: chat-." not in after_state
        # Every other byte is identical.
        assert after_state.encode() == before_state.replace(b"slug: chat-.\n", b"slug: chat\n")

    def test_a_second_run_is_a_noop(self, schema_work, commits):
        assert _render(schema_work, apply=True)[0] == 0
        rc, text = _render(schema_work, apply=True)
        assert rc == 0
        assert "nothing to migrate" in text

    def test_the_folded_tree_has_no_plain_session_findings(self, schema_work, commits):
        assert _render(schema_work, apply=True)[0] == 0
        assert _plain_session_findings(schema_work) == []

    def test_lmer_schema_migrate_lists_no_plain_session_blocker_or_move(self, schema_work, commits):
        assert _render(schema_work, apply=True)[0] == 0
        result = schema_migrate(schema_work)
        assert not any("work migrate" in b for b in result["blockers"])
        assert result["moves"] == []

    def test_post_fold_derive_slug_equals_the_new_slug(self, schema_work, commits):
        assert _render(schema_work, apply=True)[0] == 0
        # Every record under the bare `chat` group derives `chat` from its
        # recorded target now (its slug was rewritten to the taskdef).
        children = list((schema_work / PROJECT / "_runs" / "chat").iterdir())
        assert children
        for child in children:
            state = run_state._read_sibling_state(child)
            assert run_state.derive_slug(state["taskdef"], state.get("target")) == "chat"

    def test_specs_entries_resolve_at_the_new_path_with_original_dates(self, schema_work, commits):
        project = schema_work / PROJECT
        record_dir = project / "_runs" / "chat-." / "20260901-100000-1-0"
        (record_dir / "spec.md").write_text("# spec\n", encoding="utf-8")
        specs = project / "_specs"
        entry = specs / "2026-09-01-chat-.--spec.md"
        entry.symlink_to("../_runs/chat-./20260901-100000-1-0/spec.md")
        _commit_all(schema_work, "specs entry")
        assert _render(schema_work, apply=True)[0] == 0
        moved = specs / "2026-09-01-chat-.--spec.md"
        assert moved.is_symlink()
        assert moved.exists(), "the entry must resolve at the new path"
        assert moved.resolve() == (
            project / "_runs" / "chat" / "20260901-100000-1-0" / "spec.md"
        ).resolve()
        assert moved.name.startswith("2026-09-01-"), "the registration date is kept"

    def test_the_release_record_is_untouched(self, schema_work, commits):
        before = _snapshot(schema_work)
        assert _render(schema_work, apply=True)[0] == 0
        after = _snapshot(schema_work)
        for release in pf.RELEASE_RECORDS:
            group = f"{PROJECT}/_runs/{release['slug']}/"
            assert any(rel.startswith(group) for rel in before)
            assert any(rel.startswith(group) for rel in after), "the release group must stay"
            for rel, value in before.items():
                if rel.startswith(group):
                    assert after[rel] == value

    def test_the_fold_never_calls_a_record_writer(self, schema_work, commits, monkeypatch):
        def boom(*_args, **_kwargs):
            raise AssertionError("the fold must not call a record writer")

        monkeypatch.setattr(run_state, "write_state", boom)
        monkeypatch.setattr(run_state, "append_event", boom)
        monkeypatch.setattr(run_state, "freeze_run_dir", boom)
        assert _render(schema_work, apply=True)[0] == 0


class TestLegacyForm:
    def test_the_nesting_and_the_fold_are_one_move(self, legacy_work, commits):
        rc, text = _render(legacy_work, apply=True)
        assert rc == 0
        # The record lands directly in the bare taskdef group: one move.
        assert (legacy_work / PROJECT / "_runs" / "chat" / "20260901-100000-0-0").is_dir()
        assert not (legacy_work / PROJECT / "_runs" / "chat-.").exists()
        assert not (legacy_work / PROJECT / "runs").exists()
        state = run_state._read_sibling_state(
            legacy_work / PROJECT / "_runs" / "chat" / "20260901-100000-0-0"
        )
        assert state["slug"] == "chat"
        assert _plain_session_findings(legacy_work) == []


class TestRefusals:
    def test_a_never_signed_out_record_refuses(self, tmp_path, monkeypatch):
        root = tmp_path / "work"
        tree = pf.build_schema_plexus(root)
        pf.plant_plain_session_records(tree.project_dir, signed_out=False, status="in-progress")
        _git(root, "init", "-q", "-b", "main")
        _git(root, "config", "user.email", "tests@example.test")
        _git(root, "config", "user.name", "tests")
        _commit_all(root)
        _configure(monkeypatch, root, tmp_path)
        before = _snapshot(root)
        rc, text = _render(root, apply=True)
        assert rc == 1
        assert "live" in text
        assert _snapshot(root) == before

    def test_a_complete_but_not_signed_out_record_refuses(self, tmp_path, monkeypatch):
        root = tmp_path / "work"
        tree = pf.build_schema_plexus(root)
        pf.plant_plain_session_records(tree.project_dir, signed_out=False, status="complete")
        _git(root, "init", "-q", "-b", "main")
        _git(root, "config", "user.email", "tests@example.test")
        _git(root, "config", "user.name", "tests")
        _commit_all(root)
        _configure(monkeypatch, root, tmp_path)
        before = _snapshot(root)
        rc, _text = _render(root, apply=True)
        assert rc == 1
        assert _snapshot(root) == before

    def test_an_unreadable_sibling_in_a_folded_group_refuses(self, tmp_path, monkeypatch):
        root = tmp_path / "work"
        tree = pf.build_schema_plexus(root)
        pf.plant_plain_session_records(tree.project_dir)
        # A second, unreadable record in a group that IS being folded: the
        # group dir is removed and the sibling moves with it, so its unknown
        # lifecycle must stop the whole group.
        bad = tree.project_dir / "_runs" / "chat-." / "20260901-109999-9-9"
        bad.mkdir(parents=True)
        (bad / "state.yaml").write_text("state: [unclosed\n", encoding="utf-8")
        (bad / "events.jsonl").write_text("", encoding="utf-8")
        _git(root, "init", "-q", "-b", "main")
        _git(root, "config", "user.email", "tests@example.test")
        _git(root, "config", "user.name", "tests")
        _commit_all(root)
        _configure(monkeypatch, root, tmp_path)
        before = _snapshot(root)
        rc, text = _render(root, apply=True)
        assert rc == 1
        assert "unreadable" in text
        assert _snapshot(root) == before
