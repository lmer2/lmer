"""`work migrate <project>` — the project-form engine (``work_repo.migrate``,
issue #368; run-identity spec §13.B, §1.6, §10).

What these tests pin:

- the dry run is a plan rendered as ``git mv`` lines and changes nothing;
- a legacy ``runs/<slug>[--<name>]/`` nests under the id of its FIRST
  ``session_start`` event, the creation timestamp (in ``LMER_SESSION_ID``
  form) being the fallback; a dir holding several sessions' history stays
  ONE session dir and the plan says so (v1 never splits history);
- ``runs/archive/<slug>/`` relocates to the ONE archive home,
  ``_archive/_runs/<slug>/<session-id>/``; the bare areas rename to their
  ``_`` names; ``--compat-link`` leaves a relative symlink at each old path;
- a moved record keeps ``name: null``, gets no uid and its ``state.yaml`` /
  ``events.jsonl`` bytes are unchanged: the engine renames directories and
  never calls ``write_state`` / ``append_event`` / ``freeze_run_dir`` on a
  moved record (monkeypatched to raise);
- the apply is ONE commit through ``git_ops.commit_work_path`` with history
  preserved, one ``migrate_move`` event per move in the CALLER's own record
  when run inside a session, and a re-run on a migrated project is a no-op
  that says so;
- the preconditions: no manifest → ``work adopt``; the version floor; the
  writer's ``schema:`` refusal; a dirty tree; and the shared read-only
  lifecycle preflight :func:`work_repo.migrate.preflight_no_live_records`,
  which refuses a clean-but-live work-item record, an unreadable one and
  the caller's own record — each with the tree byte-identical and the index
  untouched — and passes a signed-out record and a never-started seed.
"""

import ast
import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from lmer_cli import gate_lock
from tests.conftest import strip_lmer_env
from work_repo import git_ops, migrate, run_state

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATE_PATH = REPO_ROOT / "src" / "work_repo" / "migrate.py"

PROJECT = "lmer"
MANIFEST = (
    "schema: 1\n"
    "name: example\n"
    "projects:\n"
    f"  {PROJECT}:\n"
    "    repo: gitlab.example.com/group/project\n"
)
SID_ONE = "20260824-101500-1-11111"
SID_TWO = "20260824-113000-1-22222"
SID_ARCHIVED = "20260801-090000-1-33333"
CREATED = "2026-08-24T10:15:00Z"
SEEDED_UID = "0123456789abcdef0123456789abcdef"
SEEDER_UID = "fedcba9876543210fedcba9876543210"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


# ---------------------------------------------------------------------------
# Fixture tree: an ADOPTED project (manifest present, flat short name) whose
# inside is still legacy-shaped — exactly what `work adopt` leaves behind and
# `work migrate <project>` consumes.
# ---------------------------------------------------------------------------


def _git(cwd, *args) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )
    return result.stdout


def _event(ts, session, kind, **extra):
    event = {"ts": ts, "session": session, "type": kind}
    event.update(extra)
    return event


def _state_text(slug, *, name=None, status="complete", created=CREATED, session=None):
    """A ``docs/RUN-STATE.md`` state file as explicit text, so the bytes a
    test compares are the bytes it wrote."""
    lines = [
        "schema: 1",
        f"slug: {slug}",
        f"name: {name if name is not None else 'null'}",
        "taskdef: develop",
        "target: https://gitlab.example.com/group/project/-/issues/12",
        f"status: {status}",
        "phase: implement",
        "goal: Fixture record",
        "artifacts: {}",
        "owner: null",
        f"created: '{created}'",
        f"updated: '{created}'",
    ]
    if session is not None:
        lines.append("session:")
        lines.extend(f"  {key}: {value}" for key, value in session.items())
    return "\n".join(lines) + "\n"


def _write_record(rdir, slug, events, **state_kwargs):
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "state.yaml").write_text(_state_text(slug, **state_kwargs), encoding="utf-8")
    (rdir / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )


def _signed_out_events(session, start="2026-08-24T10:15:00Z", end="2026-08-24T10:41:00Z"):
    return [
        _event(start, session, "run_seeded"),
        _event(start, session, "session_start"),
        _event(end, session, "session_end"),
    ]


def _build_adopted_project(root):
    """The adopted-but-unmigrated tree (one registered project, bare areas)."""
    root.mkdir(parents=True)
    (root / "_lmer.yaml").write_text(MANIFEST, encoding="utf-8")
    (root / "README.md").write_text("# work repo\n", encoding="utf-8")
    project = root / PROJECT
    runs = project / "runs"

    # One session, signed out: the ordinary legacy record.
    one = runs / "develop-issue-12"
    _write_record(one, "develop-issue-12", _signed_out_events(SID_ONE))
    (one / "spec.md").write_text("# Spec\n", encoding="utf-8")

    # Two sessions' history interleaved in ONE shared dir, named at the freeze.
    two = runs / "develop-issue-13--second-look"
    _write_record(
        two, "develop-issue-13",
        _signed_out_events(SID_ONE)[:2]
        + _signed_out_events(SID_TWO, "2026-08-24T11:30:00Z", "2026-08-24T11:50:00Z"),
        name="second-look",
    )

    # The legacy archive home.
    archived = runs / "archive" / "develop-issue-7"
    _write_record(
        archived, "develop-issue-7",
        _signed_out_events(SID_ARCHIVED, "2026-08-01T09:00:00Z", "2026-08-01T09:30:00Z"),
        created="2026-08-01T09:00:00Z",
    )

    specs = project / "specs"
    specs.mkdir()
    (specs / "2026-08-24-develop-issue-12--spec.md").symlink_to(
        "../runs/develop-issue-12/spec.md"
    )
    (project / "info").mkdir()
    (project / "info" / "important.md").write_text("# important\n", encoding="utf-8")
    (project / "taskdef").mkdir()
    (project / "taskdef" / "review.md").write_text("# review\n", encoding="utf-8")
    (project / "plans").mkdir()
    (project / "plans" / "PLANS.md").write_text("# Plans\n", encoding="utf-8")
    (project / "memory").mkdir()
    (project / "memory" / "MEMORY.md").write_text("- nothing\n", encoding="utf-8")
    return project


def _commit_all(root, message="fixture"):
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", message)


@pytest.fixture
def work(tmp_path, monkeypatch):
    """An adopted project in a committed git checkout that IS the configured
    work repo (the commit helper is bound to ``LMER_WORK_REPO_PATH``)."""
    root = tmp_path / "work"
    _build_adopted_project(root)
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
    _build_adopted_project_into(root)
    _commit_all(root, "fixture")
    _git(root, "push", "-q", "origin", "main")
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(root))
    monkeypatch.setenv(gate_lock.LOCK_DIR_ENV, str(tmp_path / "gate-inflight"))
    return origin, root


def _build_adopted_project_into(root):
    """`_build_adopted_project` for a root that already exists (a clone)."""
    staging = root.parent / "staging"
    _build_adopted_project(staging)
    for child in staging.iterdir():
        target = root / child.name
        child.rename(target)
    staging.rmdir()


@pytest.fixture
def commits(monkeypatch):
    """Stand in for the commit → rebase → push helper: records every call,
    reports success, performs no git write. The engine must reach it through
    ``git_ops.commit_work_path`` (the existing helper) and nothing else."""
    calls = []

    def record(paths, message=None, allow_during_gate=False):
        calls.append(SimpleNamespace(paths=list(paths), message=message))
        return 0

    monkeypatch.setattr(git_ops, "commit_work_path", record)
    return calls


def _snapshot(root):
    """Every file and symlink under *root* (``.git`` excluded) by relative
    POSIX path: file bytes, or the link target for a symlink."""
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


def _render(root, project=PROJECT, **kwargs):
    """`migrate_project` with stdout and stderr lines collected into one text."""
    lines = []
    rc = migrate.migrate_project(
        project, root=root, out=lines.append, err=lines.append, **kwargs
    )
    return rc, "\n".join(lines)


# ---------------------------------------------------------------------------
# The plan: dry run by default, rendered as `git mv` lines
# ---------------------------------------------------------------------------


class TestPlan:
    def test_dry_run_renders_git_mv_lines_and_changes_nothing(self, work):
        before = _snapshot(work)
        head = _git(work, "rev-parse", "HEAD")
        rc, text = _render(work)
        assert rc == 0
        assert (
            f"git mv {PROJECT}/runs/develop-issue-12 {PROJECT}/_runs/develop-issue-12/{SID_ONE}"
            in text
        )
        assert (
            f"git mv {PROJECT}/runs/develop-issue-13--second-look "
            f"{PROJECT}/_runs/develop-issue-13/{SID_ONE}--second-look" in text
        )
        assert (
            f"git mv {PROJECT}/runs/archive/develop-issue-7 "
            f"{PROJECT}/_archive/_runs/develop-issue-7/{SID_ARCHIVED}" in text
        )
        for old, new in (("specs", "_specs"), ("plans", "_plans"),
                         ("info", "_info"), ("taskdef", "_taskdef")):
            assert f"git mv {PROJECT}/{old} {PROJECT}/{new}" in text
        assert "--apply" in text
        assert _snapshot(work) == before
        assert _git(work, "rev-parse", "HEAD") == head
        assert _git(work, "status", "--porcelain") == ""

    def test_the_plan_is_data_an_ordered_move_list_plus_notes(self, work):
        plan = migrate.plan_project(PROJECT, root=work)
        assert isinstance(plan, migrate.Plan)
        kinds = [move.kind for move in plan.moves]
        # Records first, then the archive, then the areas — the order the
        # dry run prints and the apply replays.
        assert kinds == (
            [migrate.RECORD, migrate.RECORD, migrate.ARCHIVE] + [migrate.AREA] * 4 + [migrate.MEMORY]
        )
        assert all(isinstance(move, migrate.Move) for move in plan.moves)
        assert plan.moves[0].src == "runs/develop-issue-12"
        assert plan.moves[0].dst == f"_runs/develop-issue-12/{SID_ONE}"
        assert plan.links == ()
        assert not plan.blockers
        assert plan.record_dirs() == [
            work / PROJECT / "runs" / "develop-issue-12",
            work / PROJECT / "runs" / "develop-issue-13--second-look",
            work / PROJECT / "runs" / "archive" / "develop-issue-7",
        ]

    def test_session_id_comes_from_the_first_session_start_event(self, work):
        rdir = work / PROJECT / "runs" / "develop-issue-12"
        assert migrate.legacy_session_id(rdir) == (SID_ONE, "session_start", 1)

    def test_session_id_falls_back_to_the_creation_timestamp(self, work):
        rdir = work / PROJECT / "runs" / "develop-issue-99"
        _write_record(
            rdir, "develop-issue-99",
            [_event(CREATED, "unknown", "run_seeded")],
            created="2026-08-24T10:15:00Z",
        )
        _commit_all(work, "a record that never logged a session_start")
        sid, source, starts = migrate.legacy_session_id(rdir)
        assert (sid, source, starts) == ("20260824-101500-0-0", "created", 0)
        assert run_state.SESSION_ID_RE.fullmatch(sid), "the fallback is in session-id form"
        _rc, text = _render(work)
        assert f"git mv {PROJECT}/runs/develop-issue-99 {PROJECT}/_runs/develop-issue-99/{sid}" in text
        assert "no usable session_start" in text

    def test_a_session_start_without_a_usable_id_also_falls_back(self, work):
        rdir = work / PROJECT / "runs" / "develop-issue-98"
        _write_record(
            rdir, "develop-issue-98",
            [_event(CREATED, "unknown", "session_start")],
        )
        sid, source, _starts = migrate.legacy_session_id(rdir)
        assert source == "created" and sid == "20260824-101500-0-0"

    def test_interleaved_history_stays_one_session_dir_and_is_noted(self, work):
        rc, text = _render(work)
        assert rc == 0
        notes = [line for line in text.splitlines() if "interleaved" in line]
        assert len(notes) == 1
        assert "2 sessions" in notes[0] and "one session dir" in notes[0]
        # Exactly one move for the shared dir: it is never split.
        plan = migrate.plan_project(PROJECT, root=work)
        assert [m for m in plan.moves if "develop-issue-13" in m.src] == [
            migrate.Move(
                migrate.RECORD,
                "runs/develop-issue-13--second-look",
                f"_runs/develop-issue-13/{SID_ONE}--second-look",
                note=plan.moves[1].note,
                tracked=True,
            )
        ]

    def test_archive_relocates_to_the_one_archive_home(self, work):
        plan = migrate.plan_project(PROJECT, root=work)
        archive = [m for m in plan.moves if m.kind == migrate.ARCHIVE]
        assert archive == [migrate.Move(
            migrate.ARCHIVE, "runs/archive/develop-issue-7",
            f"_archive/_runs/develop-issue-7/{SID_ARCHIVED}", note=None, tracked=True,
        )]

    def test_area_renames(self, work):
        plan = migrate.plan_project(PROJECT, root=work)
        areas = [(m.src, m.dst) for m in plan.moves if m.kind == migrate.AREA]
        assert areas == [("specs", "_specs"), ("plans", "_plans"),
                         ("info", "_info"), ("taskdef", "_taskdef")]

    def test_an_empty_untracked_area_is_a_plain_rename(self, work):
        (work / PROJECT / "plans" / "PLANS.md").unlink()
        _commit_all(work, "drop the plan index")
        assert (work / PROJECT / "plans").is_dir()  # left empty on disk
        plan = migrate.plan_project(PROJECT, root=work)
        plans = next(m for m in plan.moves if m.src == "plans")
        assert plans.tracked is False
        assert plans.render(PROJECT) == f"mv {PROJECT}/plans {PROJECT}/_plans"

    def test_the_memory_lift_and_the_specs_repoint_are_part_of_the_plan(self, work):
        plan = migrate.plan_project(PROJECT, root=work)
        assert migrate.Move(migrate.MEMORY, "memory", "_agents/memory", tracked=True) in plan.moves
        assert plan.repoints == (
            migrate.Move(
                migrate.REPOINT, "_specs/2026-08-24-develop-issue-12--spec.md",
                f"../_runs/develop-issue-12/{SID_ONE}/spec.md",
                was="../runs/develop-issue-12/spec.md",
            ),
        )

    def test_compat_links_are_planned_at_each_old_path(self, work):
        plan = migrate.plan_project(PROJECT, root=work, compat_link=True)
        links = {(m.src, m.dst) for m in plan.links}
        assert links == {
            ("runs/develop-issue-12", f"../_runs/develop-issue-12/{SID_ONE}"),
            ("runs/develop-issue-13--second-look",
             f"../_runs/develop-issue-13/{SID_ONE}--second-look"),
            ("runs/archive/develop-issue-7",
             f"../../_archive/_runs/develop-issue-7/{SID_ARCHIVED}"),
            ("specs", "_specs"), ("plans", "_plans"), ("info", "_info"), ("taskdef", "_taskdef"),
            ("memory", "_agents/memory"),
        }
        _rc, text = _render(work, compat_link=True)
        assert f"ln -s _specs {PROJECT}/specs" in text

    def test_a_destination_already_taken_is_a_blocker(self, work):
        taken = work / PROJECT / "_runs" / "develop-issue-12" / SID_ONE
        _write_record(taken, "develop-issue-12", _signed_out_events(SID_ONE))
        _commit_all(work, "a session dir already at the target")
        plan = migrate.plan_project(PROJECT, root=work)
        assert any("already exists" in b for b in plan.blockers)
        rc, _text = _render(work, apply=True)
        assert rc == 1
        assert _git(work, "status", "--porcelain") == ""

    def test_the_dry_run_takes_no_lock(self, work, monkeypatch):
        """Read-only means lock-free: a dry run never serializes the checkout."""
        def refuse(tree=None):
            raise AssertionError(f"the dry run took work_mutation({tree})")

        monkeypatch.setattr(migrate, "work_mutation", refuse)
        rc, text = _render(work)
        assert rc == 0, text
        assert "dry run" in text


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------


class TestPreconditions:
    def test_a_missing_manifest_points_at_work_adopt(self, work):
        (work / "_lmer.yaml").unlink()
        with pytest.raises(migrate.MigrateError) as excinfo:
            migrate.plan_project(PROJECT, root=work)
        assert "work adopt" in str(excinfo.value)

    def test_the_version_floor_refuses_a_newer_schema(self, work):
        (work / "_lmer.yaml").write_text(MANIFEST.replace("schema: 1", "schema: 99"))
        with pytest.raises(migrate.MigrateError) as excinfo:
            migrate.plan_project(PROJECT, root=work)
        assert "version floor" in str(excinfo.value)
        assert "99" in str(excinfo.value)

    def test_the_writer_refusal_is_the_schema_writable_check(self, work):
        real = work / "manifest-elsewhere.yaml"
        real.write_text(MANIFEST, encoding="utf-8")
        (work / "_lmer.yaml").unlink()
        (work / "_lmer.yaml").symlink_to("manifest-elsewhere.yaml")
        with pytest.raises(migrate.MigrateError) as excinfo:
            migrate.plan_project(PROJECT, root=work)
        assert "not writable" in str(excinfo.value)
        assert "symlink" in str(excinfo.value)

    def test_an_unregistered_project_is_refused(self, work):
        with pytest.raises(migrate.MigrateError) as excinfo:
            migrate.plan_project("gadgets", root=work)
        assert "gadgets" in str(excinfo.value) and "registered" in str(excinfo.value)

    def test_a_dirty_target_path_refuses(self, work, commits):
        stray = work / PROJECT / "runs" / "develop-issue-12" / "notes.txt"
        stray.write_text("uncommitted\n", encoding="utf-8")
        before = _snapshot(work)
        rc, text = _render(work)
        assert rc == 0 and "dirty" in text  # the dry run reports it
        rc, text = _render(work, apply=True)
        assert rc == 1
        assert "dirty" in text and "nothing was changed" in text
        assert _snapshot(work) == before
        assert commits == []
        assert "notes.txt" in _git(work, "status", "--porcelain")

    def test_the_commit_helper_binds_apply_to_the_configured_checkout(self, work, monkeypatch, tmp_path):
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path / "elsewhere"))
        rc, text = _render(work, apply=True)
        assert rc == 1 and "LMER_WORK_REPO_PATH" in text
        assert _git(work, "status", "--porcelain") == ""


# ---------------------------------------------------------------------------
# The shared read-only lifecycle preflight (§1.6, astra PR3)
# ---------------------------------------------------------------------------


def _preflight_record(base, name, *, state=None, events=None):
    rdir = base / name
    rdir.mkdir(parents=True)
    if state is not None:
        (rdir / "state.yaml").write_text(state, encoding="utf-8")
    if events is not None:
        (rdir / "events.jsonl").write_text(events, encoding="utf-8")
    return rdir


class TestPreflightNoLiveRecords:
    def test_a_signed_out_record_passes(self, tmp_path):
        rdir = _preflight_record(
            tmp_path, "signed-out",
            state=_state_text("x", status="in-progress"),
            events="".join(json.dumps(e) + "\n" for e in _signed_out_events(SID_ONE)),
        )
        assert migrate.preflight_no_live_records([rdir]) == []

    def test_a_never_started_seed_passes(self, tmp_path):
        rdir = _preflight_record(
            tmp_path, SEEDED_UID,
            state=_state_text(
                "chat", status="in-progress",
                session={"uid": SEEDED_UID, "seeded_by": SEEDER_UID, "started_at": CREATED},
            ),
            events=json.dumps(_event(CREATED, SEEDER_UID, "run_seeded")) + "\n",
        )
        assert migrate.preflight_no_live_records([rdir]) == []

    def test_a_clean_but_live_record_refuses(self, tmp_path):
        # No final session_end, not a seed: live by lifecycle facts, whatever
        # git says about its files.
        rdir = _preflight_record(
            tmp_path, "live",
            state=_state_text("x", status="in-progress"),
            events="".join(json.dumps(e) + "\n" for e in _signed_out_events(SID_ONE)[:2]),
        )
        problems = migrate.preflight_no_live_records([rdir])
        assert len(problems) == 1 and "live" in problems[0]

    def test_a_record_with_status_complete_but_no_sign_out_refuses(self, tmp_path):
        rdir = _preflight_record(
            tmp_path, "complete-not-signed-out",
            state=_state_text("x", status="complete"),
            events="".join(json.dumps(e) + "\n" for e in _signed_out_events(SID_ONE)[:2]),
        )
        assert migrate.preflight_no_live_records([rdir]) != []

    def test_unreadable_state_refuses(self, tmp_path):
        rdir = _preflight_record(
            tmp_path, "bad-state", state="status: [unclosed\n",
            events="".join(json.dumps(e) + "\n" for e in _signed_out_events(SID_ONE)),
        )
        problems = migrate.preflight_no_live_records([rdir])
        assert len(problems) == 1 and "state" in problems[0]

    def test_missing_state_refuses(self, tmp_path):
        rdir = _preflight_record(tmp_path, "no-state", events="")
        assert migrate.preflight_no_live_records([rdir]) != []

    def test_unreadable_events_refuse(self, tmp_path):
        rdir = _preflight_record(
            tmp_path, "bad-events",
            state=_state_text("x"),
            events=json.dumps(_event(CREATED, SID_ONE, "session_end")) + "\n{torn",
        )
        problems = migrate.preflight_no_live_records([rdir])
        assert len(problems) == 1 and "events" in problems[0]

    def test_a_seeded_record_something_started_in_refuses(self, tmp_path):
        rdir = _preflight_record(
            tmp_path, "seeded-then-started",
            state=_state_text(
                "chat", status="in-progress",
                session={"uid": SEEDED_UID, "seeded_by": SEEDER_UID},
            ),
            events="".join(json.dumps(e) + "\n" for e in [
                _event(CREATED, SEEDER_UID, "run_seeded"),
                _event(CREATED, SEEDED_UID, "session_start"),
            ]),
        )
        assert migrate.preflight_no_live_records([rdir]) != []

    def test_the_callers_own_record_refuses(self, tmp_path, monkeypatch):
        own = _preflight_record(
            tmp_path, "mine",
            state=_state_text("x"),
            events="".join(json.dumps(e) + "\n" for e in _signed_out_events(SID_ONE)),
        )
        monkeypatch.setattr(run_state, "run_dir", lambda *a, **k: own)
        problems = migrate.preflight_no_live_records([own])
        assert len(problems) == 1
        assert "run from an operator shell or against another project" in problems[0]

    def test_it_reads_and_never_writes(self, tmp_path, monkeypatch):
        for writer in ("write_state", "append_event", "freeze_run_dir"):
            monkeypatch.setattr(run_state, writer, _raise_writer(writer))
        rdir = _preflight_record(
            tmp_path, "live",
            state=_state_text("x", status="in-progress"),
            events="".join(json.dumps(e) + "\n" for e in _signed_out_events(SID_ONE)[:2]),
        )
        before = _snapshot(tmp_path)
        migrate.preflight_no_live_records([rdir])
        assert _snapshot(tmp_path) == before


def _raise_writer(name):
    def refuse(*args, **kwargs):
        raise AssertionError(f"migrate must never call run_state.{name} ({args[:1]})")
    return refuse


class TestPreflightThroughApply:
    """The preflight as the apply runs it: inside the lock, before any move,
    and a refusal makes no filesystem, index or manifest change."""

    def _refusal_leaves_everything(self, work, commits, expect):
        before = _snapshot(work)
        head = _git(work, "rev-parse", "HEAD")
        rc, text = _render(work, apply=True)
        assert rc == 1
        assert expect in text and "nothing was changed" in text
        assert _snapshot(work) == before
        assert _git(work, "rev-parse", "HEAD") == head
        assert _git(work, "status", "--porcelain") == ""
        assert commits == []

    def test_a_clean_but_live_work_item_record_refuses(self, work, commits):
        live = work / PROJECT / "runs" / "develop-issue-20"
        _write_record(live, "develop-issue-20", _signed_out_events(SID_TWO)[:2], status="in-progress")
        _commit_all(work, "a live record, clean in git")
        self._refusal_leaves_everything(work, commits, "live")

    def test_an_unreadable_record_refuses(self, work, commits):
        broken = work / PROJECT / "runs" / "develop-issue-12" / "events.jsonl"
        broken.write_text(broken.read_text(encoding="utf-8") + "{torn", encoding="utf-8")
        _commit_all(work, "torn events, committed")
        self._refusal_leaves_everything(work, commits, "events")

    def test_the_callers_own_record_refuses(self, work, commits, monkeypatch):
        own = work / PROJECT / "runs" / "develop-issue-12"
        monkeypatch.setattr(run_state, "run_dir", lambda *a, **k: own)
        self._refusal_leaves_everything(
            work, commits, "run from an operator shell or against another project"
        )

    def test_a_record_committed_while_waiting_for_the_lock_refuses(self, work, commits, monkeypatch):
        """The interleaving astra's probe found: a session creates and
        commits a live record under `runs/` between the plan and the lock.
        The tree is clean in git, so only a plan built INSIDE the lock lists
        it — a plan from before would move the area out from under it."""
        real_mutation = migrate.work_mutation
        live = work / PROJECT / "runs" / "develop-issue-20"

        @contextmanager
        def mutation(tree=None):
            with real_mutation(tree):
                # Another process got in first: by the time the apply holds
                # the lock, `runs/` has a live record, committed.
                _write_record(live, "develop-issue-20", _signed_out_events(SID_TWO)[:2], status="in-progress")
                _commit_all(work, "a session started while the migration planned")
                yield

        monkeypatch.setattr(migrate, "work_mutation", mutation)
        rc, text = _render(work, apply=True)
        assert rc == 1, text
        assert "live" in text and "develop-issue-20" in text and "nothing was changed" in text
        assert live.is_dir() and (work / PROJECT / "runs" / "develop-issue-12").is_dir()  # nothing moved
        assert not os.path.lexists(work / PROJECT / "_runs")
        assert _git(work, "status", "--porcelain") == ""
        assert commits == []

    def test_the_plan_and_the_checks_run_inside_the_lock_before_the_first_move(self, work, commits, monkeypatch):
        """Lock → plan → clean-tree check → preflight → the first `git mv`:
        every snapshot the apply acts on is taken under the one lock."""
        order = []
        real_plan = migrate.plan_project
        real_dirty = migrate._dirty_paths
        real_preflight = migrate.preflight_no_live_records
        real_git = git_ops.run_git_command

        class _Lock:
            def __enter__(self):
                order.append("lock")

            def __exit__(self, *exc):
                order.append("unlock")

        def mutation(tree=None):
            return _Lock()

        def plan(*args, **kwargs):
            order.append("plan")
            return real_plan(*args, **kwargs)

        def dirty(root, rels):
            order.append("dirty")
            return real_dirty(root, rels)

        def preflight(records):
            order.append("preflight")
            assert (work / PROJECT / "runs" / "develop-issue-12").is_dir()
            assert not os.path.lexists(work / PROJECT / "_runs")
            return real_preflight(records)

        def git(cmd, cwd, **kwargs):
            if cmd and cmd[0] == "mv":
                order.append("git mv")
            return real_git(cmd, cwd, **kwargs)

        monkeypatch.setattr(migrate, "work_mutation", mutation)
        monkeypatch.setattr(migrate, "plan_project", plan)
        monkeypatch.setattr(migrate, "_dirty_paths", dirty)
        monkeypatch.setattr(migrate, "preflight_no_live_records", preflight)
        monkeypatch.setattr(git_ops, "run_git_command", git)
        rc, text = _render(work, apply=True)
        assert rc == 0, text
        assert order[:5] == ["lock", "plan", "dirty", "preflight", "git mv"]
        assert order[-1] == "unlock"
        assert order.count("git mv") == 8 and order.count("plan") == 1


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


class TestApply:
    def test_moves_with_git_mv_and_leaves_record_bytes_unchanged(self, work, commits, monkeypatch):
        for writer in ("write_state", "append_event", "freeze_run_dir"):
            monkeypatch.setattr(run_state, writer, _raise_writer(writer))
        before = _snapshot(work)
        rc, text = _render(work, apply=True)
        assert rc == 0, text
        project = work / PROJECT
        moved = {
            "runs/develop-issue-12": f"_runs/develop-issue-12/{SID_ONE}",
            "runs/develop-issue-13--second-look": f"_runs/develop-issue-13/{SID_ONE}--second-look",
            "runs/archive/develop-issue-7": f"_archive/_runs/develop-issue-7/{SID_ARCHIVED}",
            "specs": "_specs", "plans": "_plans", "info": "_info", "taskdef": "_taskdef",
        }
        for old, new in moved.items():
            assert not os.path.lexists(project / old), old
            assert (project / new).is_dir(), new
        for old, new in moved.items():
            if not old.startswith("runs"):
                continue
            for name in ("state.yaml", "events.jsonl"):
                assert (project / new / name).read_bytes() == before[f"{PROJECT}/{old}/{name}"][1]
            state = (project / new / "state.yaml").read_text(encoding="utf-8")
            assert "session:" not in state and "uid" not in state  # no identity minted
        assert "name: null\n" in (project / f"_runs/develop-issue-12/{SID_ONE}/state.yaml").read_text()
        # The emptied legacy run area is gone; memory lifted to its schema home.
        assert not (project / "runs").exists()
        assert not os.path.lexists(project / "memory")
        assert (project / "_agents" / "memory" / "MEMORY.md").is_file()
        # The specs index entry travelled with its area and was re-pointed, in
        # place, at the moved record.
        entry = project / "_specs" / "2026-08-24-develop-issue-12--spec.md"
        assert entry.is_symlink() and os.readlink(entry) == f"../_runs/develop-issue-12/{SID_ONE}/spec.md"
        assert entry.exists()
        # Everything is staged as renames — history preserved — and nothing is
        # left unstaged for the ONE commit.
        status = _git(work, "status", "--porcelain")
        assert all(line[1] == " " for line in status.splitlines()), status
        assert f"R  {PROJECT}/runs/develop-issue-12/state.yaml -> {PROJECT}/_runs/develop-issue-12/{SID_ONE}/state.yaml" in status
        assert (work / "_lmer.yaml").read_text(encoding="utf-8") == MANIFEST

    def test_one_commit_through_the_existing_helper(self, work, commits):
        rc, _text = _render(work, apply=True)
        assert rc == 0
        assert len(commits) == 1
        call = commits[0]
        assert call.message.startswith(f"work migrate {PROJECT}: 8 moves, 1 re-point to schema form")
        assert f"git mv {PROJECT}/runs/develop-issue-12 {PROJECT}/_runs/develop-issue-12/{SID_ONE}" in call.message
        assert f"{PROJECT}/_runs/develop-issue-12/{SID_ONE}" in call.paths
        assert f"{PROJECT}/runs/develop-issue-12" in call.paths
        assert f"{PROJECT}/_specs" in call.paths

    def test_end_to_end_commit_and_push_preserve_history(self, pushable_work):
        origin, root = pushable_work
        head_before = _git(root, "rev-parse", "HEAD").strip()
        rc, text = _render(root, apply=True)
        assert rc == 0, text
        head_after = _git(root, "rev-parse", "HEAD").strip()
        assert head_after != head_before
        assert _git(root, "rev-parse", f"{head_after}^").strip() == head_before  # ONE commit
        assert _git(origin, "rev-parse", "main").strip() == head_after  # pushed
        assert _git(root, "status", "--porcelain") == ""
        subjects = _git(
            root, "log", "--follow", "--format=%s", "--",
            f"{PROJECT}/_runs/develop-issue-12/{SID_ONE}/state.yaml",
        ).splitlines()
        assert subjects[-1] == "fixture"  # `git mv` kept the file's history
        assert subjects[0].startswith(f"work migrate {PROJECT}")

    def test_compat_links_resolve_and_a_second_run_is_a_noop(self, work, commits):
        rc, _text = _render(work, apply=True, compat_link=True)
        assert rc == 0
        project = work / PROJECT
        for old, target in {
            "runs/develop-issue-12": f"../_runs/develop-issue-12/{SID_ONE}",
            "runs/develop-issue-13--second-look": f"../_runs/develop-issue-13/{SID_ONE}--second-look",
            "runs/archive/develop-issue-7": f"../../_archive/_runs/develop-issue-7/{SID_ARCHIVED}",
            "specs": "_specs", "plans": "_plans", "info": "_info", "taskdef": "_taskdef",
        }.items():
            link = project / old
            assert link.is_symlink() and os.readlink(link) == target, old
            assert (link / "state.yaml").is_file() or link.resolve().is_dir()
        assert (project / "runs" / "develop-issue-12" / "state.yaml").is_file()
        assert f"A  {PROJECT}/runs/develop-issue-12\n" in _git(work, "status", "--porcelain")
        assert commits[0].paths.count(f"{PROJECT}/runs/develop-issue-12") == 1
        rc, text = _render(work, apply=True, compat_link=True)
        assert rc == 0 and "nothing to migrate" in text
        assert len(commits) == 1

    def test_a_second_run_on_a_migrated_project_is_a_noop_that_says_so(self, work, commits):
        assert _render(work, apply=True)[0] == 0
        _commit_all(work, "migrated")
        rc, text = _render(work)
        assert rc == 0 and "nothing to migrate" in text and PROJECT in text
        rc, text = _render(work, apply=True)
        assert rc == 0 and "nothing to migrate" in text
        assert len(commits) == 1
        assert _git(work, "status", "--porcelain") == ""

    def test_in_a_session_each_move_is_one_event_in_the_callers_record(self, work, commits, monkeypatch):
        caller = work / "other" / "_runs" / "chat" / SID_TWO
        _write_record(caller, "chat", _signed_out_events(SID_TWO)[:2], status="in-progress")
        _commit_all(work, "the caller's own record, in another project")
        monkeypatch.setattr(run_state, "run_dir", lambda *a, **k: caller)
        monkeypatch.setattr(run_state, "run_rel_path_candidates", lambda *a, **k: ["other/_runs/chat"])
        rc, _text = _render(work, apply=True)
        assert rc == 0
        events = run_state.read_events(caller, last_n=0)
        moves = [e for e in events if e.get("type") == migrate.MOVE_EVENT]
        assert len(moves) == 9  # 8 renames (the memory lift included) + 1 re-point
        assert moves[0]["data"]["from"] == f"{PROJECT}/runs/develop-issue-12"
        assert moves[0]["data"]["to"] == f"{PROJECT}/_runs/develop-issue-12/{SID_ONE}"
        assert moves[0]["data"]["project"] == PROJECT
        log = (caller / "log.yaml").read_text(encoding="utf-8")
        assert f"work migrate {PROJECT}" in log and "8 moves, 1 re-point" in log
        # The caller's record rides along in the same commit.
        assert "other/_runs/chat" in commits[0].paths

    def test_outside_a_session_nothing_but_the_commit_records_the_moves(self, work, commits, monkeypatch):
        monkeypatch.setattr(run_state, "append_event", _raise_writer("append_event"))
        rc, text = _render(work, apply=True)
        assert rc == 0
        assert "commit message lists the moves" in text
        assert commits[0].message.count("git mv ") == 8

    def test_compat_links_leave_their_own_event_in_the_callers_record(self, work, commits, monkeypatch):
        """A compat link is an applied change like any other: one event, with
        the link's path and its relative target."""
        caller = work / "other" / "_runs" / "chat" / SID_TWO
        _write_record(caller, "chat", _signed_out_events(SID_TWO)[:2], status="in-progress")
        _commit_all(work, "the caller's own record, in another project")
        monkeypatch.setattr(run_state, "run_dir", lambda *a, **k: caller)
        monkeypatch.setattr(run_state, "run_rel_path_candidates", lambda *a, **k: ["other/_runs/chat"])
        rc, _text = _render(work, apply=True, compat_link=True)
        assert rc == 0
        events = [e for e in run_state.read_events(caller, last_n=0) if e.get("type") == migrate.MOVE_EVENT]
        links = [e for e in events if e["data"]["kind"] == migrate.LINK]
        assert len(events) == 9 + 8  # 8 renames + 1 re-point, then one per link
        assert {"project": PROJECT, "kind": migrate.LINK, "path": f"{PROJECT}/runs/develop-issue-12",
                "to": f"../_runs/develop-issue-12/{SID_ONE}"} in [e["data"] for e in links]
        assert {"project": PROJECT, "kind": migrate.LINK, "path": f"{PROJECT}/specs",
                "to": "_specs"} in [e["data"] for e in links]
        assert all(f"{PROJECT}/" in e["note"] and " -> " in e["note"] for e in links)


# ---------------------------------------------------------------------------
# The CLI seam (registration is B.migrate's cli.py task)
# ---------------------------------------------------------------------------


class TestCliSeam:
    def test_write_rel_paths_is_none_for_a_dry_run(self, work):
        assert migrate.write_rel_paths(SimpleNamespace(project=PROJECT, apply=False)) is None

    def test_write_rel_paths_names_the_project_dir_for_apply(self, work):
        assert migrate.write_rel_paths(SimpleNamespace(project=PROJECT, apply=True)) == [PROJECT]
        assert migrate.write_rel_paths(SimpleNamespace(project="gadgets", apply=True)) == []
        assert migrate.write_rel_paths(SimpleNamespace(project="../etc", apply=True)) == []

    def test_cmd_migrate_reports_a_refusal_on_stderr(self, work, capsys):
        rc = migrate.cmd_migrate(SimpleNamespace(project="gadgets", apply=False, compat_link=False))
        assert rc == 1
        assert "gadgets" in capsys.readouterr().err

    def test_cmd_migrate_dry_run_prints_the_plan(self, work, capsys):
        rc = migrate.cmd_migrate(SimpleNamespace(project=PROJECT, apply=False, compat_link=False))
        assert rc == 0
        assert "git mv " in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The thin-consumer guard (§13.B): no YAML dump of its own, the writer is the
# one write path to the manifest, and nothing is imported from the tests.
# ---------------------------------------------------------------------------


class TestThinConsumer:
    @pytest.fixture
    def tree(self):
        return ast.parse(MIGRATE_PATH.read_text(encoding="utf-8"))

    def test_no_yaml_dump_and_no_ruamel(self, tree):
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"dump", "safe_dump", "dump_all", "safe_dump_all"}, ast.dump(node)
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith("ruamel") for a in node.names), ast.dump(node)
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(("ruamel", "tests")), ast.dump(node)

    def test_it_writes_no_file_of_its_own(self, tree):
        """Directories are renamed and linked; every FILE write goes through
        an owner (the caller's record via run_state/loggers, the manifest via
        the writer — which this command never even mutates) — with ONE named
        exception: ``_write_index``, the memory lift's write of the two
        ``MEMORY.md`` indexes. memory.py owns that file but exposes no index
        writer without a session's restore baseline behind it, and routing
        the split through persist would hand it to D.index's reconcile, which
        generates lines the migration must not. The SECOND named exception is
        ``_rewrite_slug``: the fold's positional scalar surgery on a moved
        state file, which writes exactly one scalar and nothing else (§1.6).
        So the writes are two functions, named here, and nothing else in the
        module may write a file."""
        allowed = {"_write_index", "_rewrite_slug"}
        functions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        assert allowed <= functions, "the one index write must stay a named top-level function"
        for top in tree.body:
            if isinstance(top, ast.FunctionDef) and top.name in allowed:
                continue
            for node in ast.walk(top):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    assert node.func.attr not in {"write_text", "write_bytes"}, ast.dump(node)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    assert node.func.id != "open", ast.dump(node)
