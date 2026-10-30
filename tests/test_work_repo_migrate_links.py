"""`work migrate <project>` — the index re-point and the memory lift
(``work_repo.migrate``, issue #368's link and memory half; run-identity spec
§13.B, §13.D).

What these tests pin:

- every relative symlink in ``specs/``/``_specs/`` and ``plans/``/``_plans/``
  whose target moves is re-pointed IN PLACE at the moved path — the entry
  keeps its name and its registration date — and a link that was ALREADY
  dangling is reported and left untouched: the tool is not the hygiene fix
  and never drops an entry the way ``work specs-index --rebuild`` does;
- ``memory/`` lifts to ``_agents/memory/``, each file routed by its
  ``metadata.type`` through :func:`work_repo.memory.scope_for_memory`
  unchanged: ``project`` stays in project scope, ``user``/``feedback``/
  ``reference`` go to the instance scope at the plexus root, and a file
  with no type or an unknown one stays in project scope and is reported;
- the EXISTING ``MEMORY.md`` lines are routed between the two indexes by
  parsing ``](name.md)`` — a line carrying two entries (a lost newline) is
  split so each entry reaches its scope — and no line is generated: a file
  without an entry is reported (D.index's reconcile adds its line on the
  next persist), an entry without a file stays in the project index, and a
  tombstone stays a tombstone in the scope that wrote it;
- all of it works from an operator shell with no session env, the dry run
  shows the re-point table and the routing tables and changes nothing, the
  apply stages everything for the ONE commit with one event per change in
  the caller's record, and a migrated project is a no-op.
"""

import os
import subprocess
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from lmer_cli import gate_lock
from tests.conftest import strip_lmer_env
from tests.test_work_repo_migrate import (
    MANIFEST,
    PROJECT,
    SID_ONE,
    _commit_all,
    _git,
    _raise_writer,
    _render,
    _signed_out_events,
    _snapshot,
    _write_record,
)
from tests.test_work_repo_memory_areas import _index_findings
from work_repo import git_ops, memory, migrate, run_state

MOVED_ENTRY = "2026-08-24-develop-issue-12--spec.md"
DANGLING_ENTRY = "2026-08-20-develop-issue-5--spec.md"
OUTSIDE_ENTRY = "2026-08-21-readme--spec.md"
PLAN_ENTRY = "2026-08-24-develop-issue-12--plan.md"

OLD_TARGET = "../runs/develop-issue-12/spec.md"
NEW_TARGET = f"../_runs/develop-issue-12/{SID_ONE}/spec.md"

#: Every memory file in the legacy ``memory/`` and its ``metadata.type``
#: (``None``: no type at all; ``banana``: a type nobody routes).
MEMORY_FILES = {
    "user_terse.md": "user",
    "feedback_show_output.md": "feedback",
    "reference_dashboard.md": "reference",
    "project_pythonpath.md": "project",
    "untyped_note.md": None,
    "odd_kind.md": "banana",
    "orphan_fact.md": "project",  # no MEMORY.md line
}
INSTANCE_FILES = {"user_terse.md", "feedback_show_output.md", "reference_dashboard.md"}
PROJECT_FILES = set(MEMORY_FILES) - INSTANCE_FILES

#: The legacy index: a heading, a blank, one line per memory — except line 4,
#: which lost its newline and carries a project entry AND a feedback entry —
#: an entry whose file is gone, and a tombstone already re-pointed at the
#: archive mirror.
INDEX = (
    "# lmer memory\n"
    "\n"
    "- [Terse](user_terse.md) — short answers.\n"
    "- [Pythonpath](project_pythonpath.md) — prefix PYTHONPATH."
    "- [Show output](feedback_show_output.md) — paste output.\n"
    "- [Dashboard](reference_dashboard.md) — grafana.\n"
    "- [Untyped](untyped_note.md) — no type.\n"
    "- [Odd](odd_kind.md) — banana.\n"
    "- [Gone](gone.md) — entry without a file.\n"
    "- [Old](../_archive/memory/old.md) — forgotten 2026-01-01.\n"
)
PROJECT_INDEX = (
    "# lmer memory\n"
    "\n"
    "- [Pythonpath](project_pythonpath.md) — prefix PYTHONPATH.\n"
    "- [Untyped](untyped_note.md) — no type.\n"
    "- [Odd](odd_kind.md) — banana.\n"
    "- [Gone](gone.md) — entry without a file.\n"
    "- [Old](../_archive/memory/old.md) — forgotten 2026-01-01.\n"
)
INSTANCE_INDEX = (
    "# lmer memory\n"
    "\n"
    "- [Terse](user_terse.md) — short answers.\n"
    "- [Show output](feedback_show_output.md) — paste output.\n"
    "- [Dashboard](reference_dashboard.md) — grafana.\n"
)


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


def _memory_text(name, kind):
    """A memory file with the frontmatter SCHEMA.md requires; *kind* is the
    ``metadata.type`` (``None`` writes the block without one)."""
    lines = ["---", f"name: {name}", 'description: "a fact"', "metadata:", "  node_type: memory"]
    if kind is not None:
        lines.append(f"  type: {kind}")
    lines += ["---", "", "The fact.", ""]
    return "\n".join(lines)


def _build_project(root):
    """An adopted project (manifest, flat short name) whose inside is legacy:
    one record with a spec and a plan, index links to both, one link that
    already dangles, one that points outside the project, and a ``memory/``
    holding every routing case."""
    root.mkdir(parents=True)
    (root / "_lmer.yaml").write_text(MANIFEST, encoding="utf-8")
    (root / "README.md").write_text("# work repo\n", encoding="utf-8")
    project = root / PROJECT
    record = project / "runs" / "develop-issue-12"
    _write_record(record, "develop-issue-12", _signed_out_events(SID_ONE))
    (record / "spec.md").write_text("# Spec\n", encoding="utf-8")
    (record / "plan.md").write_text("# Plan\n", encoding="utf-8")
    specs = project / "specs"
    specs.mkdir()
    (specs / MOVED_ENTRY).symlink_to(OLD_TARGET)
    (specs / DANGLING_ENTRY).symlink_to("../runs/develop-issue-5/spec.md")
    (specs / OUTSIDE_ENTRY).symlink_to("../../README.md")
    plans = project / "plans"
    plans.mkdir()
    (plans / "PLANS.md").write_text("# Plans\n", encoding="utf-8")
    (plans / PLAN_ENTRY).symlink_to("../runs/develop-issue-12/plan.md")
    memory = project / "memory"
    memory.mkdir()
    for filename, kind in MEMORY_FILES.items():
        (memory / filename).write_text(_memory_text(filename[:-3], kind), encoding="utf-8")
    (memory / "MEMORY.md").write_text(INDEX, encoding="utf-8")
    return project


@pytest.fixture
def work(tmp_path, monkeypatch):
    """The project in a committed git checkout that IS the configured work
    repo, with no session env at all (an operator shell)."""
    root = tmp_path / "work"
    _build_project(root)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "tests@example.test")
    _git(root, "config", "user.name", "tests")
    _commit_all(root)
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(root))
    monkeypatch.setenv(gate_lock.LOCK_DIR_ENV, str(tmp_path / "gate-inflight"))
    return root


@pytest.fixture
def pushable_work(tmp_path, monkeypatch):
    """The same tree as a clone of a bare origin, so the real commit → rebase
    → push helper can run end to end."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)],
        check=True, capture_output=True,
    )
    root = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(root)], check=True, capture_output=True)
    _git(root, "config", "user.email", "tests@example.test")
    _git(root, "config", "user.name", "tests")
    (root / ".keep").write_text("", encoding="utf-8")
    _commit_all(root, "seed")
    _git(root, "push", "-q", "-u", "origin", "main")
    staging = tmp_path / "staging"
    _build_project(staging)
    for child in staging.iterdir():
        child.rename(root / child.name)
    staging.rmdir()
    _commit_all(root, "fixture")
    _git(root, "push", "-q", "origin", "main")
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(root))
    monkeypatch.setenv(gate_lock.LOCK_DIR_ENV, str(tmp_path / "gate-inflight"))
    return origin, root


@pytest.fixture
def commits(monkeypatch):
    """Stand in for the commit → rebase → push helper: records every call,
    performs no git write."""
    calls = []

    def record(paths, message=None, allow_during_gate=False):
        calls.append(SimpleNamespace(paths=list(paths), message=message))
        return 0

    monkeypatch.setattr(git_ops, "commit_work_path", record)
    return calls


def _staged_clean(root):
    """Every change is staged and nothing is left unstaged for the ONE commit."""
    status = _git(root, "status", "--porcelain")
    return all(line[1] == " " for line in status.splitlines()), status


# ---------------------------------------------------------------------------
# Index links: re-pointed in place, dangling ones reported and left
# ---------------------------------------------------------------------------


class TestRepoint:
    def test_a_link_whose_target_moved_is_repointed_in_place(self, work, commits):
        assert "LMER_REPO_HOST" not in os.environ  # no session env needed
        plan = migrate.plan_project(PROJECT, root=work)
        assert migrate.Move(
            migrate.REPOINT, f"_specs/{MOVED_ENTRY}", NEW_TARGET, was=OLD_TARGET
        ) in plan.repoints
        rc, text = _render(work)
        assert rc == 0
        assert f"ln -sfn {NEW_TARGET} {PROJECT}/_specs/{MOVED_ENTRY}" in text
        assert f"# was {OLD_TARGET}" in text

        rc, text = _render(work, apply=True)
        assert rc == 0, text
        entry = work / PROJECT / "_specs" / MOVED_ENTRY
        assert entry.is_symlink() and os.readlink(entry) == NEW_TARGET
        assert entry.exists() and entry.resolve().read_text() == "# Spec\n"
        # The entry kept its name (and so its registration date): nothing was
        # dropped or renamed, only the target changed.
        assert {p.name for p in (work / PROJECT / "_specs").iterdir()} == {
            MOVED_ENTRY, DANGLING_ENTRY, OUTSIDE_ENTRY
        }
        clean, status = _staged_clean(work)
        assert clean, status

    def test_plans_entries_are_repointed_too(self, work, commits):
        plan = migrate.plan_project(PROJECT, root=work)
        assert migrate.Move(
            migrate.REPOINT, f"_plans/{PLAN_ENTRY}",
            f"../_runs/develop-issue-12/{SID_ONE}/plan.md",
            was="../runs/develop-issue-12/plan.md",
        ) in plan.repoints
        assert _render(work, apply=True)[0] == 0
        entry = work / PROJECT / "_plans" / PLAN_ENTRY
        assert entry.exists() and entry.resolve().read_text() == "# Plan\n"

    def test_an_already_dangling_link_is_reported_and_left_untouched(self, work, commits):
        plan = migrate.plan_project(PROJECT, root=work)
        assert not any(DANGLING_ENTRY in m.src for m in plan.repoints)
        notes = [n for n in plan.notes if DANGLING_ENTRY in n]
        assert len(notes) == 1
        assert "already dangling" in notes[0] and "left untouched" in notes[0]
        assert "../runs/develop-issue-5/spec.md" in notes[0]
        assert _render(work, apply=True)[0] == 0
        entry = work / PROJECT / "_specs" / DANGLING_ENTRY
        assert entry.is_symlink()  # never dropped
        assert os.readlink(entry) == "../runs/develop-issue-5/spec.md"

    def test_a_link_whose_target_does_not_move_is_left_alone(self, work, commits):
        plan = migrate.plan_project(PROJECT, root=work)
        assert not any(OUTSIDE_ENTRY in m.src for m in plan.repoints)
        assert not any(OUTSIDE_ENTRY in n for n in plan.notes)
        assert _render(work, apply=True)[0] == 0
        entry = work / PROJECT / "_specs" / OUTSIDE_ENTRY
        assert os.readlink(entry) == "../../README.md" and entry.exists()

    def test_an_entry_already_under_the_schema_area_is_repointed_without_an_area_move(self, work, commits):
        _git(work, "mv", f"{PROJECT}/specs", f"{PROJECT}/_specs")
        _commit_all(work, "specs renamed by hand, runs still legacy")
        plan = migrate.plan_project(PROJECT, root=work)
        assert not any(m.src == "specs" for m in plan.moves)
        assert migrate.Move(
            migrate.REPOINT, f"_specs/{MOVED_ENTRY}", NEW_TARGET, was=OLD_TARGET
        ) in plan.repoints
        assert f"{PROJECT}/_specs/{MOVED_ENTRY}" in plan.git_paths()
        assert _render(work, apply=True)[0] == 0
        entry = work / PROJECT / "_specs" / MOVED_ENTRY
        assert os.readlink(entry) == NEW_TARGET and entry.exists()

    def test_the_dry_run_changes_nothing(self, work):
        before = _snapshot(work)
        rc, text = _render(work)
        assert rc == 0 and "ln -sfn" in text
        assert _snapshot(work) == before
        assert _git(work, "status", "--porcelain") == ""


# ---------------------------------------------------------------------------
# Memory: files routed by type, both indexes rebuilt from the existing lines
# ---------------------------------------------------------------------------


class TestMemoryLift:
    def test_files_are_planned_by_type_and_the_dry_run_shows_the_routing(self, work):
        plan = migrate.plan_project(PROJECT, root=work)
        lifts = [m for m in plan.moves if m.kind == migrate.MEMORY]
        assert [(m.src, m.dst) for m in lifts] == [
            ("memory/feedback_show_output.md", "../_agents/memory/feedback_show_output.md"),
            ("memory/reference_dashboard.md", "../_agents/memory/reference_dashboard.md"),
            ("memory/user_terse.md", "../_agents/memory/user_terse.md"),
            ("memory", "_agents/memory"),
        ]
        assert plan.moves[-1] == lifts[-1]  # the lift comes after the areas
        rc, text = _render(work)
        assert rc == 0
        assert f"git mv {PROJECT}/memory/user_terse.md _agents/memory/user_terse.md" in text
        assert f"git mv {PROJECT}/memory {PROJECT}/_agents/memory" in text
        # The routing tables: one row per file, one row per index line.
        files = {rel: scope for rel, scope, _remark in plan.memory_lift.files}
        assert files == {
            name: ("instance" if name in INSTANCE_FILES else "project")
            for name in MEMORY_FILES
        }
        assert "memory routing" in text and "memory index routing" in text
        assert "  instance  user_terse.md" in text
        assert "  project   untyped_note.md" in text
        assert "  instance  - [Terse](user_terse.md) — short answers." in text
        assert "  project   - [Gone](gone.md) — entry without a file." in text
        assert "  both      # lmer memory" in text
        assert f"write {PROJECT}/_agents/memory/MEMORY.md" in text
        assert "write _agents/memory/MEMORY.md" in text

    def test_apply_lifts_files_and_rebuilds_both_indexes(self, work, commits, monkeypatch):
        for writer in ("write_state", "append_event", "freeze_run_dir"):
            monkeypatch.setattr(run_state, writer, _raise_writer(writer))
        before = _snapshot(work)
        rc, text = _render(work, apply=True)
        assert rc == 0, text
        project = work / PROJECT
        assert not os.path.lexists(project / "memory")
        for name in INSTANCE_FILES:
            assert (work / "_agents" / "memory" / name).read_bytes() == before[f"{PROJECT}/memory/{name}"][1]
            assert not os.path.lexists(project / "_agents" / "memory" / name)
        for name in PROJECT_FILES:
            assert (project / "_agents" / "memory" / name).read_bytes() == before[f"{PROJECT}/memory/{name}"][1]
            assert not os.path.lexists(work / "_agents" / "memory" / name)
        assert (project / "_agents" / "memory" / "MEMORY.md").read_text(encoding="utf-8") == PROJECT_INDEX
        assert (work / "_agents" / "memory" / "MEMORY.md").read_text(encoding="utf-8") == INSTANCE_INDEX
        clean, status = _staged_clean(work)
        assert clean, status
        assert f"R  {PROJECT}/memory/project_pythonpath.md -> {PROJECT}/_agents/memory/project_pythonpath.md" in status
        assert f"R  {PROJECT}/memory/user_terse.md -> _agents/memory/user_terse.md" in status
        assert len(commits) == 1
        assert "_agents/memory/MEMORY.md" in commits[0].paths
        assert f"write {PROJECT}/_agents/memory/MEMORY.md" in commits[0].message

    def test_two_entries_on_one_line_route_each_to_its_scope(self, work, commits):
        plan = migrate.plan_project(PROJECT, root=work)
        notes = [n for n in plan.notes if "entries on one line" in n]
        assert len(notes) == 1 and "line 4" in notes[0]
        assert _render(work, apply=True)[0] == 0
        project_index = (work / PROJECT / "_agents" / "memory" / "MEMORY.md").read_text()
        instance_index = (work / "_agents" / "memory" / "MEMORY.md").read_text()
        assert "- [Pythonpath](project_pythonpath.md) — prefix PYTHONPATH.\n" in project_index
        assert "feedback_show_output.md" not in project_index
        assert "- [Show output](feedback_show_output.md) — paste output.\n" in instance_index
        assert "project_pythonpath.md" not in instance_index

    def test_no_type_and_an_unknown_type_stay_in_project_scope_and_are_reported(self, work, commits):
        plan = migrate.plan_project(PROJECT, root=work)
        for name in ("untyped_note.md", "odd_kind.md"):
            notes = [n for n in plan.notes if f"memory/{name}" in n]
            assert len(notes) == 1, name
            assert "no usable `metadata.type`" in notes[0] and "project scope" in notes[0]
            assert not any(m.src == f"memory/{name}" for m in plan.moves)
        assert _render(work, apply=True)[0] == 0
        project_index = (work / PROJECT / "_agents" / "memory" / "MEMORY.md").read_text()
        for name in ("untyped_note.md", "odd_kind.md"):
            assert (work / PROJECT / "_agents" / "memory" / name).is_file()
            assert f"]({name})" in project_index

    def test_a_file_without_an_entry_is_reported_and_no_line_is_generated(self, work, commits):
        plan = migrate.plan_project(PROJECT, root=work)
        notes = [n for n in plan.notes if "memory/orphan_fact.md" in n]
        assert len(notes) == 1 and "no MEMORY.md line" in notes[0]
        assert _render(work, apply=True)[0] == 0
        assert (work / PROJECT / "_agents" / "memory" / "orphan_fact.md").is_file()
        for index in (
            work / PROJECT / "_agents" / "memory" / "MEMORY.md",
            work / "_agents" / "memory" / "MEMORY.md",
        ):
            assert "orphan_fact" not in index.read_text()

    def test_an_entry_without_a_file_stays_in_the_project_index_untouched(self, work, commits):
        plan = migrate.plan_project(PROJECT, root=work)
        notes = [n for n in plan.notes if "gone.md" in n]
        assert len(notes) == 1 and "entry without a file" in notes[0]
        assert _render(work, apply=True)[0] == 0
        assert "- [Gone](gone.md) — entry without a file.\n" in (
            work / PROJECT / "_agents" / "memory" / "MEMORY.md"
        ).read_text()
        assert "gone.md" not in (work / "_agents" / "memory" / "MEMORY.md").read_text()

    def test_a_tombstone_stays_a_tombstone_in_the_scope_that_wrote_it(self, work, commits):
        assert _render(work, apply=True)[0] == 0
        tombstone = "- [Old](../_archive/memory/old.md) — forgotten 2026-01-01.\n"
        assert tombstone in (work / PROJECT / "_agents" / "memory" / "MEMORY.md").read_text()
        assert "old.md" not in (work / "_agents" / "memory" / "MEMORY.md").read_text()
        assert "forgotten" not in INSTANCE_INDEX  # and nothing new was tombstoned

    def test_the_instance_index_merges_into_an_existing_one(self, work, commits):
        instance = work / "_agents" / "memory"
        instance.mkdir(parents=True)
        (instance / "other.md").write_text(_memory_text("other", "user"), encoding="utf-8")
        existing = "# instance memory\n\n- [Other](other.md) — from another project.\n"
        (instance / "MEMORY.md").write_text(existing, encoding="utf-8")
        _commit_all(work, "an instance scope another project's migration left")
        rc, text = _render(work)
        assert rc == 0 and "merged into the existing instance index" in text
        assert _render(work, apply=True)[0] == 0
        assert (instance / "MEMORY.md").read_text(encoding="utf-8") == (
            existing
            + "# lmer memory\n"
            + "- [Terse](user_terse.md) — short answers.\n"
            + "- [Show output](feedback_show_output.md) — paste output.\n"
            + "- [Dashboard](reference_dashboard.md) — grafana.\n"
        )
        assert (instance / "other.md").read_text(encoding="utf-8") == _memory_text("other", "user")

    def test_an_instance_index_updated_while_waiting_for_the_lock_is_kept(self, work, commits, monkeypatch):
        """The interleaving astra's probe found on the index: another
        session's persist commits a new instance entry between the plan and
        the lock. The tree is clean in git, so only an index text composed
        INSIDE the lock carries it — a text planned before would overwrite
        the entry away."""
        instance = work / "_agents" / "memory"
        instance.mkdir(parents=True)
        (instance / "before.md").write_text(_memory_text("before", "user"), encoding="utf-8")
        existing = "# instance memory\n\n- [Before](before.md) — from another project.\n"
        (instance / "MEMORY.md").write_text(existing, encoding="utf-8")
        _commit_all(work, "an instance scope another project's migration left")
        concurrent = "- [Concurrent](concurrent.md) — persisted while the migration planned.\n"
        real_mutation = migrate.work_mutation

        @contextmanager
        def mutation(tree=None):
            with real_mutation(tree):
                (instance / "concurrent.md").write_text(_memory_text("concurrent", "user"), encoding="utf-8")
                (instance / "MEMORY.md").write_text(existing + concurrent, encoding="utf-8")
                _commit_all(work, "another session persisted an instance memory")
                yield

        monkeypatch.setattr(migrate, "work_mutation", mutation)
        rc, text = _render(work, apply=True)
        assert rc == 0, text
        assert (instance / "MEMORY.md").read_text(encoding="utf-8") == (
            existing
            + concurrent
            + "# lmer memory\n"
            + "- [Terse](user_terse.md) — short answers.\n"
            + "- [Show output](feedback_show_output.md) — paste output.\n"
            + "- [Dashboard](reference_dashboard.md) — grafana.\n"
        )
        assert (work / PROJECT / "_agents" / "memory" / "MEMORY.md").read_text(encoding="utf-8") == PROJECT_INDEX
        assert (instance / "concurrent.md").is_file() and (instance / "before.md").is_file()
        clean, status = _staged_clean(work)
        assert clean, status

    def test_a_taken_instance_destination_is_a_blocker(self, work, commits):
        instance = work / "_agents" / "memory"
        instance.mkdir(parents=True)
        (instance / "user_terse.md").write_text(_memory_text("terse", "user"), encoding="utf-8")
        _commit_all(work, "the instance scope already holds that name")
        plan = migrate.plan_project(PROJECT, root=work)
        assert any("_agents/memory/user_terse.md already exists" in b for b in plan.blockers)
        before = _snapshot(work)
        rc, text = _render(work, apply=True)
        assert rc == 1 and "nothing was changed" in text
        assert _snapshot(work) == before and commits == []

    def test_memory_and_agents_memory_both_present_is_a_blocker(self, work):
        taken = work / PROJECT / "_agents" / "memory"
        taken.mkdir(parents=True)
        (taken / "MEMORY.md").write_text("", encoding="utf-8")
        _commit_all(work, "a project scope already in place")
        plan = migrate.plan_project(PROJECT, root=work)
        assert any("memory/ and _agents/memory/ both exist" in b for b in plan.blockers)
        assert not any(m.kind == migrate.MEMORY for m in plan.moves)
        assert plan.memory_lift is None

    def test_a_memory_dir_without_an_index_lifts_files_and_writes_no_index(self, work, commits):
        (work / PROJECT / "memory" / "MEMORY.md").unlink()
        _commit_all(work, "no index")
        plan = migrate.plan_project(PROJECT, root=work)
        assert plan.memory_lift.writes == () and plan.memory_lift.lines == ()
        unindexed = [n for n in plan.notes if "no MEMORY.md line" in n]
        assert len(unindexed) == len(MEMORY_FILES)
        assert _render(work, apply=True)[0] == 0
        assert not (work / "_agents" / "memory" / "MEMORY.md").exists()
        assert not (work / PROJECT / "_agents" / "memory" / "MEMORY.md").exists()
        assert (work / "_agents" / "memory" / "user_terse.md").is_file()

    def test_nothing_routed_to_the_instance_scope_writes_nothing_there(self, work, commits):
        memory = work / PROJECT / "memory"
        for name in INSTANCE_FILES:
            (memory / name).unlink()
        (memory / "MEMORY.md").write_text(PROJECT_INDEX, encoding="utf-8")
        _commit_all(work, "project-scope memory only")
        plan = migrate.plan_project(PROJECT, root=work)
        assert [m for m in plan.moves if m.kind == migrate.MEMORY] == [
            migrate.Move(migrate.MEMORY, "memory", "_agents/memory", tracked=True)
        ]
        assert plan.memory_lift.writes == ()  # the git mv carries the index as is
        assert _render(work, apply=True)[0] == 0
        assert not (work / "_agents").exists()
        assert (work / PROJECT / "_agents" / "memory" / "MEMORY.md").read_text() == PROJECT_INDEX

    def test_memory_already_a_symlink_is_left_in_place(self, work, commits):
        assert _render(work, apply=True, compat_link=True)[0] == 0
        link = work / PROJECT / "memory"
        assert link.is_symlink() and os.readlink(link) == "_agents/memory"
        assert (link / "MEMORY.md").read_text() == PROJECT_INDEX
        _commit_all(work, "migrated with compat links")
        plan = migrate.plan_project(PROJECT, root=work)
        assert plan.is_noop
        assert any("memory: already a symlink" in n for n in plan.notes)

    def test_write_rel_paths_names_the_instance_scope_only_when_a_file_lifts(self, work):
        args = SimpleNamespace(project=PROJECT, apply=True)
        assert migrate.write_rel_paths(args) == [PROJECT, "_agents/memory"]
        for name in INSTANCE_FILES:
            (work / PROJECT / "memory" / name).unlink()
        assert migrate.write_rel_paths(args) == [PROJECT]
        assert migrate.write_rel_paths(SimpleNamespace(project=PROJECT, apply=False)) is None


# ---------------------------------------------------------------------------
# The whole slice through the apply: one commit, events, idempotence
# ---------------------------------------------------------------------------


class TestApply:
    def test_the_dry_run_changes_nothing_and_the_apply_is_one_commit(self, work, commits):
        before = _snapshot(work)
        head = _git(work, "rev-parse", "HEAD")
        rc, text = _render(work)
        assert rc == 0 and "memory routing" in text and "ln -sfn" in text
        assert "dry run" in text
        assert _snapshot(work) == before and _git(work, "rev-parse", "HEAD") == head
        rc, text = _render(work, apply=True)
        assert rc == 0, text
        assert len(commits) == 1
        message = commits[0].message
        assert message.startswith(f"work migrate {PROJECT}: 7 moves, 2 re-points, 2 index writes to schema form")
        assert f"ln -sfn {NEW_TARGET} {PROJECT}/_specs/{MOVED_ENTRY}" in message
        assert f"git mv {PROJECT}/memory {PROJECT}/_agents/memory" in message
        assert "write _agents/memory/MEMORY.md" in message
        for rel in (f"{PROJECT}/memory", f"{PROJECT}/_agents/memory", "_agents/memory/user_terse.md",
                    "_agents/memory/MEMORY.md", f"{PROJECT}/_specs/{MOVED_ENTRY}"):
            assert rel in commits[0].paths, rel

    def test_end_to_end_with_real_git_preserves_history(self, pushable_work):
        origin, root = pushable_work
        head_before = _git(root, "rev-parse", "HEAD").strip()
        rc, text = _render(root, apply=True)
        assert rc == 0, text
        head_after = _git(root, "rev-parse", "HEAD").strip()
        assert _git(root, "rev-parse", f"{head_after}^").strip() == head_before  # ONE commit
        assert _git(origin, "rev-parse", "main").strip() == head_after  # pushed
        assert _git(root, "status", "--porcelain") == ""
        for path in ("_agents/memory/user_terse.md", f"{PROJECT}/_agents/memory/project_pythonpath.md"):
            subjects = _git(root, "log", "--follow", "--format=%s", "--", path).splitlines()
            assert subjects[-1] == "fixture", path  # `git mv` kept the file's history
        entry = root / PROJECT / "_specs" / MOVED_ENTRY
        assert os.readlink(entry) == NEW_TARGET and entry.exists()
        assert (root / "_agents" / "memory" / "MEMORY.md").read_text(encoding="utf-8") == INSTANCE_INDEX

    def test_in_a_session_every_change_is_one_event_in_the_callers_record(self, work, commits, monkeypatch):
        caller = work / "other" / "_runs" / "chat" / "20260824-113000-1-22222"
        _write_record(caller, "chat", _signed_out_events("20260824-113000-1-22222")[:2], status="in-progress")
        _commit_all(work, "the caller's own record, in another project")
        monkeypatch.setattr(run_state, "run_dir", lambda *a, **k: caller)
        monkeypatch.setattr(run_state, "run_rel_path_candidates", lambda *a, **k: ["other/_runs/chat"])
        assert _render(work, apply=True)[0] == 0
        events = [e for e in run_state.read_events(caller, last_n=0) if e.get("type") == migrate.MOVE_EVENT]
        by_kind = {}
        for event in events:
            by_kind.setdefault(event["data"]["kind"], []).append(event["data"])
        assert len(by_kind[migrate.MEMORY]) == 4
        assert {"project": PROJECT, "kind": migrate.MEMORY,
                "from": f"{PROJECT}/memory/user_terse.md", "to": "_agents/memory/user_terse.md"} in by_kind[migrate.MEMORY]
        assert by_kind[migrate.REPOINT] == [
            {"project": PROJECT, "kind": migrate.REPOINT, "from": OLD_TARGET, "to": NEW_TARGET,
             "path": f"{PROJECT}/_specs/{MOVED_ENTRY}"},
            {"project": PROJECT, "kind": migrate.REPOINT, "from": "../runs/develop-issue-12/plan.md",
             "to": f"../_runs/develop-issue-12/{SID_ONE}/plan.md", "path": f"{PROJECT}/_plans/{PLAN_ENTRY}"},
        ]
        assert [(e["from"], e["to"]) for e in by_kind[migrate.INDEX]] == [
            (f"{PROJECT}/memory/MEMORY.md", f"{PROJECT}/_agents/memory/MEMORY.md"),
            (f"{PROJECT}/memory/MEMORY.md", "_agents/memory/MEMORY.md"),
        ]

    def test_a_second_run_on_a_migrated_project_is_a_noop(self, work, commits):
        assert _render(work, apply=True)[0] == 0
        _commit_all(work, "migrated")
        plan = migrate.plan_project(PROJECT, root=work)
        assert plan.is_noop and plan.repoints == () and plan.memory_lift is None
        rc, text = _render(work, apply=True)
        assert rc == 0 and "nothing to migrate" in text
        assert len(commits) == 1

    def test_a_refusal_leaves_the_indexes_and_links_untouched(self, work, commits):
        live = work / PROJECT / "runs" / "develop-issue-20"
        _write_record(live, "develop-issue-20", _signed_out_events(SID_ONE)[:2], status="in-progress")
        _commit_all(work, "a live record")
        before = _snapshot(work)
        rc, text = _render(work, apply=True)
        assert rc == 1 and "live" in text and "nothing was changed" in text
        assert _snapshot(work) == before and commits == []


class TestSplitLegacyMemoryLift:
    """A split legacy `memory/` keeps its hand-written hooks (#601)."""

    def test_area_lines_lift_and_the_store_round_trips_clean(
        self, work, commits, monkeypatch, tmp_path
    ):
        legacy = work / PROJECT / "memory"
        (legacy / "MEMORY.md").write_text(
            "# lmer memory\n\n- [general](areas/general.md) — read when anything\n",
            encoding="utf-8",
        )
        (legacy / "areas").mkdir()
        (legacy / "areas" / "general.md").write_text(
            "# general\n\n"
            + "".join(
                f"- [{name[:-3]}](../{name}) — HAND HOOK {name[:-3]}\n"
                for name in MEMORY_FILES
                if name != "orphan_fact.md"
            ),
            encoding="utf-8",
        )
        _commit_all(work, "a split legacy memory dir")
        assert _render(work, apply=True)[0] == 0
        instance_index = work / "_agents" / "memory" / "MEMORY.md"
        project_area = work / PROJECT / "_agents" / "memory" / "areas" / "general.md"
        for name in INSTANCE_FILES:
            assert f"]({name}) — HAND HOOK {name[:-3]}\n" in instance_index.read_text()
            assert name not in project_area.read_text()

        host, project = "gitlab.example.com", "group/project"
        agent_dir = tmp_path / "agent-memory"
        agent_dir.mkdir()
        monkeypatch.setenv("LMER_REPO_HOST", host)
        monkeypatch.setenv("LMER_REPO_PROJECT", project)
        monkeypatch.setenv("LMER_REPO_URL", f"https://{host}/{project}")
        monkeypatch.setenv("LMER_AGENT_MEMORY_DIR", str(agent_dir))
        monkeypatch.setenv("LMER_PERSIST_AGENT_MEMORY", "1")
        monkeypatch.setattr(memory, "check_commit_ready", lambda *args: 0)
        monkeypatch.setattr(memory, "commit_work_path", lambda *args, **kwargs: 0)
        assert memory.restore_memory() == 0
        assert memory.persist_memory() == 0
        for name in INSTANCE_FILES:
            assert f"]({name}) — HAND HOOK {name[:-3]}\n" in instance_index.read_text()
        assert "archived" not in instance_index.read_text()
        # The persist reconciled the split project scope: it filed the orphan.
        assert "](../orphan_fact.md)" in (project_area.parent / "unsorted.md").read_text()
        assert _index_findings(SimpleNamespace(root=work)) == []


class TestStoreFormatMarker:
    """A store a newer lmer wrote is never lifted (#598)."""

    @pytest.mark.parametrize("scope", ["legacy", "instance"])
    def test_a_newer_marker_blocks_the_lift(self, work, commits, scope):
        newer = memory.MEMORY_FORMAT_MAX + 1
        marked = work / PROJECT / "memory" if scope == "legacy" else work / "_agents" / "memory"
        marked.mkdir(parents=True, exist_ok=True)
        (marked / memory.MEMORY_FORMAT_FILE).write_text(f"{newer}\n", encoding="utf-8")
        _commit_all(work, "a newer lmer's marker")
        plan = migrate.plan_project(PROJECT, root=work)
        assert [b for b in plan.blockers if f"store format {newer} is newer" in b]
        before = _snapshot(work)
        rc, _text = _render(work, apply=True)
        assert rc != 0
        assert _snapshot(work) == before and commits == []

    def test_a_known_marker_rides_along_with_the_project_scope(self, work, commits):
        legacy = work / PROJECT / "memory"
        (legacy / memory.MEMORY_FORMAT_FILE).write_text("1\n", encoding="utf-8")
        _commit_all(work, "a marker")
        plan = migrate.plan_project(PROJECT, root=work)
        assert memory.MEMORY_FORMAT_FILE not in {rel for rel, _s, _r in plan.memory_lift.files}
        assert _render(work, apply=True)[0] == 0
        assert (work / PROJECT / "_agents" / "memory" / memory.MEMORY_FORMAT_FILE).is_file()
        assert not (work / "_agents" / "memory" / memory.MEMORY_FORMAT_FILE).exists()
