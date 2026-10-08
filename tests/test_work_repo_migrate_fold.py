"""`work migrate <project>` — spec §7's plain-session fold (D10, #368).

What these tests pin:

- a record whose stored slug is the obsolete derivation of its own target
  moves into the bare taskdef group, keeping its session-dir name, and its
  `slug:` scalar is rewritten — every other byte identical, no YAML dump;
- the emptied group is removed; a second run is a no-op;
- the folded tree has zero `record.plain-session-slug` findings and
  `lmer-schema migrate` lists no plain-session blocker or move;
- a post-fold `derive_slug(taskdef, recorded target)` equals each new slug;
- `_specs/` entries are re-registered at the new path under the record's
  new label, with their original dates; the README index is regenerated in
  the same ONE commit;
- a taken destination is reported and skipped; a state file the slug
  surgery cannot handle, or an unsafe taskdef, refuses before anything moves;
- the fold refuses while a record in a folded group is live (no final
  `session_end`) or its lifecycle is unreadable, through the shared
  `preflight_no_live_records`, and never calls a record writer;
- R1's release record is untouched in both states;
- a legacy (adopted) tree folds in the same move as the nesting.
"""

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from lmer_cli import gate_lock
from lmer_schema.checks import Form, Report, Severity
from lmer_schema.checks import record as record_checks
from lmer_schema.migration import migrate as schema_migrate
from tests import plexus_fixtures as pf
from tests.conftest import strip_lmer_env
from work_repo import git_ops, migrate, run_state, specs_index

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

    def test_specs_entries_are_reregistered_under_the_new_label_with_original_dates(
        self, schema_work, commits
    ):
        project = schema_work / PROJECT
        record_dir = project / "_runs" / "chat-." / "20260901-100000-1-0"
        (record_dir / "spec.md").write_text("# spec\n", encoding="utf-8")
        specs = project / "_specs"
        old_entry = specs / "2026-09-01-chat-.-20260901-100000-1-0--spec.md"
        old_entry.symlink_to("../_runs/chat-./20260901-100000-1-0/spec.md")
        _commit_all(schema_work, "specs entry")
        assert _render(schema_work, apply=True)[0] == 0
        new_dir = project / "_runs" / "chat" / "20260901-100000-1-0"
        expected = specs / specs_index._entry_name(
            specs_index.run_label(new_dir), "spec.md",
            datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        assert expected.name.startswith("2026-09-01-"), "the registration date is kept"
        assert expected.is_symlink()
        assert expected.resolve() == (new_dir / "spec.md").resolve()
        assert not old_entry.is_symlink(), "the old-label entry is replaced, not duplicated"
        assert [e.name for e in specs.iterdir() if e.name.endswith("--spec.md")] == [expected.name]
        assert f"{PROJECT}/_specs" in commits[0].paths

    def test_the_fold_lands_in_one_commit(self, schema_work, commits):
        rc, text = _render(schema_work, apply=True)
        assert rc == 0
        assert len(commits) == 1
        assert "3 moves, 3 slug rewrites" in commits[0].message.splitlines()[0]

    def test_compat_link_never_links_inside_runs(self, schema_work, commits):
        rc, text = _render(schema_work, compat_link=True)
        assert rc == 0
        assert "ln -s" not in text
        rc, _text = _render(schema_work, apply=True, compat_link=True)
        assert rc == 0
        assert not (schema_work / PROJECT / "_runs" / "chat-.").exists()
        for path in (schema_work / PROJECT / "_runs").rglob("*"):
            assert not path.is_symlink(), path

    def test_a_state_yml_record_folds(self, schema_work, commits):
        record_dir = schema_work / PROJECT / "_runs" / "chat-." / "20260901-100000-1-0"
        (record_dir / "state.yaml").rename(record_dir / "state.yml")
        _commit_all(schema_work, "legacy state.yml spelling")
        rc, text = _render(schema_work, apply=True)
        assert rc == 0, text
        new_state = schema_work / PROJECT / "_runs" / "chat" / "20260901-100000-1-0" / "state.yml"
        assert "slug: chat\n" in new_state.read_text(encoding="utf-8")

    def test_an_existing_destination_is_reported_and_skipped(self, schema_work, commits):
        taken = schema_work / PROJECT / "_runs" / "chat" / "20260901-100000-1-0"
        taken.mkdir(parents=True)
        (taken / "state.yaml").write_text(
            "schema: 1\ntaskdef: chat\ntarget: .\nslug: chat\nstatus: complete\n",
            encoding="utf-8",
        )
        _commit_all(schema_work, "destination taken")
        rc, text = _render(schema_work)
        assert rc == 0
        assert "already exists (skipped, never overwritten)" in text
        rc, text = _render(schema_work, apply=True)
        assert rc == 0, text
        # The skipped record stays where it was; the other groups fold.
        assert (schema_work / PROJECT / "_runs" / "chat-." / "20260901-100000-1-0").is_dir()
        assert (taken / "state.yaml").read_text(encoding="utf-8").endswith("status: complete\n")
        assert not (schema_work / PROJECT / "_runs" / "chat-widgets").exists()
        # With only the skipped record left, the no-op says so honestly.
        rc, text = _render(schema_work)
        assert "nothing this tool can move: 1 plain-session record" in text
        assert "already in schema form" not in text

    def test_an_interrupted_fold_gets_its_slug_rewrite_alone(self, schema_work, commits):
        runs = schema_work / PROJECT / "_runs"
        (runs / "chat").mkdir()
        (runs / "chat-." / "20260901-100000-1-0").rename(runs / "chat" / "20260901-100000-1-0")
        (runs / "chat-.").rmdir()
        _commit_all(schema_work, "moved but not rewritten")
        rc, text = _render(schema_work, apply=True)
        assert rc == 0, text
        state = run_state._read_sibling_state(runs / "chat" / "20260901-100000-1-0")
        assert state["slug"] == "chat"
        assert _plain_session_findings(schema_work) == []

    @pytest.mark.parametrize("mangle", [
        lambda text: text + "slug: chat-.\n",  # a duplicate key
        lambda text: "anchor: &a chat-.\n" + text.replace("slug: chat-.", "slug: *a"),
    ])
    def test_a_state_file_the_surgery_cannot_handle_refuses(self, schema_work, commits, mangle):
        state_file = schema_work / PROJECT / "_runs" / "chat-." / "20260901-100000-1-0" / "state.yaml"
        state_file.write_text(mangle(state_file.read_text(encoding="utf-8")), encoding="utf-8")
        _commit_all(schema_work, "awkward state")
        before = _snapshot(schema_work)
        rc, text = _render(schema_work, apply=True)
        assert rc == 1
        assert "slug" in text
        assert _snapshot(schema_work) == before
        assert commits == []

    def test_an_unsafe_taskdef_refuses(self, schema_work, commits):
        group = schema_work / PROJECT / "_runs" / "x-." / "20260901-100000-9-9"
        group.mkdir(parents=True)
        state = {"taskdef": "../x", "target": ".", "slug": run_state.legacy_derive_slug("../x", ".")}
        assert run_state.is_legacy_plain_session_slug(state)
        (group / "state.yaml").write_text(
            f"schema: 1\ntaskdef: ../x\ntarget: .\nslug: {state['slug']}\nstatus: complete\n",
            encoding="utf-8",
        )
        (group / "events.jsonl").write_text(
            json.dumps({"ts": "2026-09-01T10:05:00Z", "type": "session_end"}) + "\n",
            encoding="utf-8",
        )
        _commit_all(schema_work, "unsafe taskdef")
        before = _snapshot(schema_work)
        rc, text = _render(schema_work, apply=True)
        assert rc == 1
        assert "not a single safe directory name" in text
        assert _snapshot(schema_work) == before

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

    def test_the_legacy_fold_never_calls_a_record_writer(self, legacy_work, commits, monkeypatch):
        def boom(*_args, **_kwargs):
            raise AssertionError("the fold must not call a record writer")

        monkeypatch.setattr(run_state, "write_state", boom)
        monkeypatch.setattr(run_state, "append_event", boom)
        monkeypatch.setattr(run_state, "freeze_run_dir", boom)
        assert _render(legacy_work, apply=True)[0] == 0

    def test_the_legacy_release_records_keep_their_bytes(self, legacy_work, commits):
        before = {
            release["slug"]: (legacy_work / PROJECT / "runs" / release["slug"] / "state.yaml").read_bytes()
            for release in pf.RELEASE_RECORDS
        }
        assert _render(legacy_work, apply=True)[0] == 0
        for slug, raw in before.items():
            moved = list((legacy_work / PROJECT / "_runs" / slug).iterdir())
            assert len(moved) == 1, "nested under its own slug group, never folded"
            assert (moved[0] / "state.yaml").read_bytes() == raw


class TestPublication:
    def test_the_one_commit_carries_the_regenerated_readme(self, schema_work, tmp_path):
        remote = tmp_path / "remote.git"
        _git(tmp_path, "init", "-q", "--bare", str(remote))
        _git(schema_work, "remote", "add", "origin", str(remote))
        _git(schema_work, "push", "-q", "origin", "main")
        _git(schema_work, "branch", "--set-upstream-to", "origin/main")
        head = _git(schema_work, "rev-parse", "HEAD").strip()
        rc, text = _render(schema_work, apply=True)
        assert rc == 0, text
        assert _git(schema_work, "rev-list", "--count", f"{head}..HEAD").strip() == "1"
        changed = _git(schema_work, "show", "--name-only", "--format=", "HEAD").split()
        assert f"{PROJECT}/README.md" in changed
        readme = (schema_work / PROJECT / "README.md").read_text(encoding="utf-8")
        assert "_runs/chat/20260901-100000-1-0/" in readme
        assert "_runs/chat-./" not in readme


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
        assert migrate.LIVE_REMEDY in text
        assert "work housekeep --archive-stale" in text
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
        rc, text = _render(root, apply=True)
        assert rc == 1
        assert migrate.LIVE_REMEDY in text
        assert _snapshot(root) == before

    def test_a_live_record_refuses_its_slug_only_rewrite(self, tmp_path, monkeypatch):
        # An interrupted fold: the record already sits in its taskdef group
        # and only `slug:` is left to rewrite. A live writer there would put
        # the old slug back, so the preflight must see it.
        root = tmp_path / "work"
        tree = pf.build_schema_plexus(root)
        pf.plant_plain_session_records(tree.project_dir, signed_out=False, status="in-progress")
        runs = tree.project_dir / "_runs"
        (runs / "chat").mkdir(exist_ok=True)
        (runs / "chat-." / "20260901-100000-1-0").rename(runs / "chat" / "20260901-100000-1-0")
        (runs / "chat-.").rmdir()
        _git(root, "init", "-q", "-b", "main")
        _git(root, "config", "user.email", "tests@example.test")
        _git(root, "config", "user.name", "tests")
        _commit_all(root)
        _configure(monkeypatch, root, tmp_path)
        plan = migrate.plan_project(PROJECT, root=root)
        assert runs / "chat" / "20260901-100000-1-0" in plan.record_dirs()
        before = _snapshot(root)
        rc, text = _render(root, apply=True)
        assert rc == 1
        assert "_runs/chat/20260901-100000-1-0: live" in text
        assert migrate.LIVE_REMEDY in text
        assert _snapshot(root) == before

    def test_an_unreadable_sibling_in_a_folded_group_refuses(self, tmp_path, monkeypatch):
        root = tmp_path / "work"
        tree = pf.build_schema_plexus(root)
        pf.plant_plain_session_records(tree.project_dir)
        # A second, unreadable record in a group that IS being folded: spec
        # §7 refuses while any record in the group may be live, and unknown
        # is not stopped.
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
        assert migrate.LIVE_REMEDY in text
        assert _snapshot(root) == before


class TestSpecsRepointFromAnOperatorShell:
    def test_an_entry_that_cannot_be_reregistered_is_kept(self, schema_work, capsys):
        # No session env (stripped): the explicit areas are all it has. A
        # target that does not exist cannot be re-registered, so the old
        # entry must survive rather than be dropped silently.
        project = schema_work / PROJECT
        specs = project / "_specs"
        entry = specs / "2026-09-01-chat-.--spec.md"
        entry.symlink_to("../_runs/chat-./20260901-100000-1-0/spec.md")
        specs_index.repoint_run_dir_entries(
            "chat-./20260901-100000-1-0", "chat/20260901-100000-1-0", "chat",
            project_dir=project, specs_directory=specs, run_area=project / "_runs",
        )
        assert entry.is_symlink()
        assert "not re-pointed" in capsys.readouterr().out

    def test_the_explicit_areas_reregister_without_a_session(self, schema_work):
        project = schema_work / PROJECT
        source = project / "_runs" / "chat-." / "20260901-100000-1-0"
        (source / "spec.md").write_text("# spec\n", encoding="utf-8")
        specs = project / "_specs"
        entry = specs / "2026-09-01-chat-.--spec.md"
        entry.symlink_to("../_runs/chat-./20260901-100000-1-0/spec.md")
        moved = project / "_runs" / "chat" / "20260901-100000-1-0"
        moved.parent.mkdir()
        source.rename(moved)
        specs_index.repoint_run_dir_entries(
            "chat-./20260901-100000-1-0", "chat/20260901-100000-1-0", "chat-20260901-100000-1-0",
            project_dir=project,
        )
        entries = [e for e in specs.iterdir() if e.name.endswith("--spec.md")]
        assert [e.name for e in entries] == ["2026-09-01-chat-20260901-100000-1-0--spec.md"]
        assert entries[0].resolve() == (moved / "spec.md").resolve()
