"""`work adopt <host>/<project> <shortname>` — name a host-bucket tree and
register it (``work_repo.adopt``, issue #367; run-identity spec §13.B, §1.6,
§10).

What these tests pin, on the schema-form fixture's own host bucket:

- the dry run prints every planned action — the registry entry, the
  ``git mv``, the optional link, the ``rmdir`` of emptied namespace levels,
  the commit message — and the bucket's SCHEMA-tier fix list, exits 0 and
  changes nothing;
- the fix list is the validator's own findings under the bucket, each
  re-tiered to the schema severity (D14: the tree is leaving the lenient
  tier the runner pins a bucket to); an ERROR refuses the apply unless
  ``--force``; the bucket's own "awaiting adoption" listing is not in it;
- a missing manifest is bootstrapped through ``writer.create()`` and nothing
  else, with the one-line notice in the dry run too (D20);
- ``--apply`` lands ONE commit through ``git_ops.commit_work_path`` with
  both paths staged (the move lands as a move) and a tree that validates
  with zero errors; the old path is gone, or with ``--compat-link`` is a
  relative, depth-matched symlink the validator reports as INFO;
- every refusal leaves the tree byte-identical, the index and the manifest
  untouched: a live or unreadable record in the bucket and the caller's own
  record (the shared §1.6 preflight, inside the lock), a bad shortname, a
  taken name, a remote already bound, the bucket's own remote registered
  elsewhere (a split tree), a newer schema int, a symlinked manifest, dirty
  paths, a symlinked bucket, a path that is not a bucket;
- inside a session one ``project_adopted`` event (name + repo) plus a
  run-log line in the CALLER's record, staged with the commit; outside, only
  the commit. A record that moves keeps its bytes; adopt never calls a
  record writer on it, mints nothing, and dumps no YAML of its own.
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
from lmer_schema import writer as schema_writer
from lmer_schema.checks.runner import validate
from lmer_schema.manifest import load_manifest
from lmer_schema.registry import REGISTRY_HIT, resolve_project_dir
from tests import plexus_fixtures as pf
from tests.conftest import strip_lmer_env
from work_repo import adopt, git_ops, run_state

REPO_ROOT = Path(__file__).resolve().parent.parent
ADOPT_PATH = REPO_ROOT / "src" / "work_repo" / "adopt.py"

#: The schema fixture's host bucket and the name it is adopted under.
BUCKET = f"{pf.BUCKET_HOST}/{pf.BUCKET_PROJECT}"
NAME = "widget"
#: The legacy fixture's `{host}/{project}` home (the bootstrap case).
LEGACY_BUCKET = pf.PRIMARY_REPO
SID = "20260901-100000-1-44444"
SID_TWO = "20260901-113000-1-55555"
CREATED = "2026-09-01T10:00:00Z"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


# ---------------------------------------------------------------------------
# Fixture trees: the schema-form plexus (its bucket is what adopt names) and
# the legacy twin (no manifest: the bootstrap case), each a committed git
# checkout that IS the configured work repo.
# ---------------------------------------------------------------------------


def _git(cwd, *args) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )
    return result.stdout


def _commit_all(root, message="fixture"):
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", message)


def _init_checkout(root):
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "tests@example.test")
    _git(root, "config", "user.name", "tests")
    _commit_all(root)


def _configure(monkeypatch, root, tmp_path):
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(root))
    monkeypatch.setenv(gate_lock.LOCK_DIR_ENV, str(tmp_path / "gate-inflight"))


@pytest.fixture
def work(tmp_path, monkeypatch):
    """The schema-form plexus with its host bucket, committed."""
    tree = pf.build_schema_plexus(tmp_path / "work")
    _init_checkout(tree.root)
    _configure(monkeypatch, tree.root, tmp_path)
    return tree.root


@pytest.fixture
def legacy_work(tmp_path, monkeypatch):
    """The legacy twin: no manifest, the project at `{host}/{project}/`. The
    fixture's shared run dir leaves its second session open; it is signed
    out here so the §1.6 preflight has nothing to refuse."""
    tree = pf.build_legacy_plexus(tmp_path / "work")
    events = tree.session_dirs[0] / "events.jsonl"
    events.write_text(
        events.read_text(encoding="utf-8")
        + json.dumps(_event("2026-08-24T11:50:00Z", pf.SESSION_IDS[1], "session_end")) + "\n",
        encoding="utf-8",
    )
    _init_checkout(tree.root)
    _configure(monkeypatch, tree.root, tmp_path)
    return tree.root


@pytest.fixture
def pushable_work(tmp_path, monkeypatch):
    """The schema tree as a clone of a bare origin, so the real commit →
    rebase → push helper runs end to end."""
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
    pf.build_schema_plexus(staging)
    for child in staging.iterdir():
        child.rename(root / child.name)
    staging.rmdir()
    _commit_all(root, "fixture")
    _git(root, "push", "-q", "origin", "main")
    _configure(monkeypatch, root, tmp_path)
    return origin, root


@pytest.fixture
def commits(monkeypatch):
    """Stand in for the commit → rebase → push helper: records every call,
    reports success, performs no git write. Adopt must reach it through
    ``git_ops.commit_work_path`` (the existing helper) and nothing else."""
    calls = []

    def record(paths, message=None, allow_during_gate=False):
        calls.append(SimpleNamespace(paths=list(paths), message=message))
        return 0

    monkeypatch.setattr(git_ops, "commit_work_path", record)
    return calls


def _event(ts, session, kind, **extra):
    event = {"ts": ts, "session": session, "type": kind}
    event.update(extra)
    return event


def _signed_out_events(session, start=CREATED, end="2026-09-01T10:41:00Z"):
    return [
        _event(start, session, "run_seeded"),
        _event(start, session, "session_start"),
        _event(end, session, "session_end"),
    ]


def _state_text(slug, *, status="complete", created=CREATED):
    """A ``docs/RUN-STATE.md`` state file as explicit text, so the bytes a
    test compares are the bytes it wrote."""
    stop = "complete" if status == "complete" else "null"
    return "\n".join([
        "schema: 1",
        f"slug: {slug}",
        "name: null",
        "taskdef: develop",
        f"target: https://{BUCKET}/-/issues/1",
        f"status: {status}",
        "phase: implement",
        f"stop_reason: {stop}",
        "critical_error: null",
        "open_question: null",
        "goal: Fixture record",
        "estimate: null",
        "artifacts: {}",
        "owner: null",
        f"frozen: '{created}'",
        f"created: '{created}'",
        f"updated: '{created}'",
    ]) + "\n"


def _write_record(rdir, slug, events, **state_kwargs):
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "state.yaml").write_text(_state_text(slug, **state_kwargs), encoding="utf-8")
    (rdir / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    return rdir


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


def _render(root, bucket=BUCKET, name=NAME, **kwargs):
    """`adopt_project` with stdout and stderr lines collected into one text."""
    lines = []
    rc = adopt.adopt_project(
        bucket, name, root=root, out=lines.append, err=lines.append, **kwargs
    )
    return rc, "\n".join(lines)


def _dangling_specs_link(root):
    """A `_specs/` entry in the bucket pointing at a record that is not
    there: WARN at the legacy tier, ERROR at the schema tier."""
    specs = root / BUCKET / "_specs"
    specs.mkdir()
    (specs / "2026-09-01-develop-issue-9-spec.md").symlink_to(
        "../_runs/develop-issue-9/20260901-000000-1-9/spec.md"
    )
    _commit_all(root, "a dangling specs entry in the bucket")


# ---------------------------------------------------------------------------
# The plan: dry run by default, every action printed, nothing changed
# ---------------------------------------------------------------------------


class TestPlan:
    def test_dry_run_prints_every_action_and_changes_nothing(self, work):
        before = _snapshot(work)
        head = _git(work, "rev-parse", "HEAD")
        rc, text = _render(work)
        assert rc == 0, text
        assert f"_plexus.yaml: add projects.{NAME}" in text
        assert f"repo: {BUCKET}" in text
        assert f"git mv {BUCKET} {NAME}" in text
        assert "commit message:" in text
        assert f"work adopt {NAME}: {BUCKET} -> {NAME}/" in text
        assert "schema-tier fix list" in text
        assert "--apply" in text
        assert _snapshot(work) == before
        assert _git(work, "rev-parse", "HEAD") == head
        assert _git(work, "status", "--porcelain") == ""

    def test_the_plan_is_data(self, work):
        plan = adopt.plan_adoption(BUCKET, NAME, root=work)
        assert isinstance(plan, adopt.Plan)
        assert plan.bucket == BUCKET and plan.name == NAME
        assert plan.repo == BUCKET  # the bucket path IS the canonical remote
        assert plan.remotes == () and plan.description is None
        assert plan.bootstrap is False and plan.schema == 1
        assert plan.tracked is True and plan.link is None
        assert plan.fix_list == () and plan.blockers() == ()
        assert plan.records == ()
        assert plan.git_paths() == ["_plexus.yaml", BUCKET, NAME]

    def test_remotes_and_description_land_in_the_entry_canonically(self, work):
        plan = adopt.plan_adoption(
            BUCKET, NAME, root=work,
            remotes=("https://github.com/example/widget.git", "git@gitlab.example.com:mirrors/widget.git"),
            description="the widget",
        )
        assert plan.remotes == ("github.com/example/widget", "gitlab.example.com/mirrors/widget")
        assert plan.description == "the widget"
        text = plan.render()
        assert "remotes:" in text and "  - github.com/example/widget" in text
        assert "description: the widget" in text
        assert "projects.widget.remotes: github.com/example/widget, gitlab.example.com/mirrors/widget" in plan.commit_message()

    def test_compat_link_is_relative_and_depth_matched(self, work):
        plan = adopt.plan_adoption(BUCKET, NAME, root=work, compat_link=True)
        assert plan.link == "../../widget"
        assert f"ln -s ../../widget {BUCKET}" in plan.render()
        assert f"ln -s ../../widget {BUCKET}" in plan.commit_message()
        assert plan.cleanups() == []  # the link keeps its parents

    def test_emptied_namespace_levels_are_planned_for_rmdir(self, work):
        plan = adopt.plan_adoption(BUCKET, NAME, root=work)
        assert plan.cleanups() == [f"{pf.BUCKET_HOST}/unnamed", pf.BUCKET_HOST]
        text = plan.render()
        assert f"rmdir {pf.BUCKET_HOST}/unnamed" in text and f"rmdir {pf.BUCKET_HOST}" in text

    def test_a_sibling_keeps_the_namespace_level(self, work):
        other = work / pf.BUCKET_HOST / "unnamed" / "gadget" / "_runs"
        other.mkdir(parents=True)
        (other.parent / "_info").mkdir()
        (other.parent / "_info" / "important.md").write_text("# gadget\n", encoding="utf-8")
        _commit_all(work, "a second bucket under the same namespace")
        plan = adopt.plan_adoption(BUCKET, NAME, root=work)
        assert plan.cleanups() == []

    def test_records_under_the_bucket_are_counted(self, work):
        rdir = _write_record(
            work / BUCKET / "_runs" / "develop-issue-1" / SID, "develop-issue-1",
            _signed_out_events(SID),
        )
        _commit_all(work, "a signed-out record in the bucket")
        plan = adopt.plan_adoption(BUCKET, NAME, root=work)
        assert plan.records == (rdir,)
        assert "records under the bucket: 1" in plan.render()

    def test_the_dry_run_takes_no_lock(self, work, monkeypatch):
        """Read-only means lock-free: a dry run never serializes the checkout."""
        def refuse(tree=None):
            raise AssertionError(f"the dry run took work_mutation({tree})")

        monkeypatch.setattr(adopt, "work_mutation", refuse)
        rc, text = _render(work)
        assert rc == 0, text
        assert "dry run" in text


# ---------------------------------------------------------------------------
# The schema-tier fix list (D14)
# ---------------------------------------------------------------------------


class TestFixList:
    def test_findings_under_the_bucket_are_retiered_to_schema_severities(self, work):
        _dangling_specs_link(work)
        # The runner's own verdict on the bucket is the legacy tier's ...
        runner = [f for f in validate(work).findings if f.code == "index.dangling-link"]
        assert len(runner) == 1 and runner[0].severity.value == "WARN"
        # ... and the fix list is the same finding at the tier it is moving to.
        plan = adopt.plan_adoption(BUCKET, NAME, root=work)
        listed = [f for f in plan.fix_list if f.code == "index.dangling-link"]
        assert len(listed) == 1
        assert listed[0].severity.value == "ERROR"
        assert listed[0].path == runner[0].path and listed[0].message == runner[0].message
        rc, text = _render(work)
        assert rc == 0
        assert "ERROR  index.dangling-link" in text
        assert "1 error" in text

    def test_the_buckets_own_listing_is_not_in_the_fix_list(self, work):
        assert any(f.code == "tree.host-bucket" and f.path == BUCKET for f in validate(work).findings)
        plan = adopt.plan_adoption(BUCKET, NAME, root=work)
        assert not any(f.code == "tree.host-bucket" for f in plan.fix_list)
        assert "clean" in plan.render()

    def test_findings_outside_the_bucket_are_not_in_the_fix_list(self, work):
        report = validate(work)
        assert any(f.path.startswith(f"{pf.PROJECT}/") for f in report.findings)
        plan = adopt.plan_adoption(BUCKET, NAME, root=work)
        assert all(f.path.startswith(f"{BUCKET}/") for f in plan.fix_list)

    def test_an_error_in_the_fix_list_refuses_the_apply_unless_forced(self, work, commits):
        _dangling_specs_link(work)
        before = _snapshot(work)
        rc, text = _render(work, apply=True)
        assert rc == 1
        assert "schema-tier error" in text and "--force" in text and "nothing was changed" in text
        assert _snapshot(work) == before
        assert _git(work, "status", "--porcelain") == ""
        assert commits == []
        rc, text = _render(work, apply=True, force=True)
        assert rc == 0, text
        assert len(commits) == 1
        assert (work / NAME / "_specs").is_dir()

    def test_the_fix_list_is_computed_from_the_runner(self, work, monkeypatch):
        """ONE source of findings: the fix list is the runner's report,
        never a second walk."""
        import lmer_schema.checks.runner as runner

        calls = []
        real = runner.validate

        def spy(target, **kwargs):
            calls.append(Path(target))
            return real(target, **kwargs)

        monkeypatch.setattr(runner, "validate", spy)
        adopt.plan_adoption(BUCKET, NAME, root=work)
        assert calls == [work]


# ---------------------------------------------------------------------------
# Bootstrap (D20): a missing manifest, created through the writer only
# ---------------------------------------------------------------------------


class TestBootstrap:
    def test_the_dry_run_carries_the_notice_and_creates_nothing(self, legacy_work):
        before = _snapshot(legacy_work)
        rc, text = _render(legacy_work, bucket=LEGACY_BUCKET, name="lmer")
        assert rc == 0, text
        notice = [line for line in text.splitlines() if "BOOTSTRAP" in line]
        assert len(notice) == 1, text
        assert "_plexus.yaml" in notice[0] and "schema form" in notice[0] and "D20" in notice[0]
        assert "name: work" in notice[0]  # the instance name create() will write
        assert not (legacy_work / "_plexus.yaml").exists()
        assert _snapshot(legacy_work) == before

    def test_the_apply_bootstraps_through_writer_create(self, legacy_work, commits, monkeypatch):
        created = []
        real = schema_writer.create

        def spy(path, name):
            created.append((Path(path), name))
            return real(path, name)

        monkeypatch.setattr(schema_writer, "create", spy)
        rc, text = _render(legacy_work, bucket=LEGACY_BUCKET, name="lmer", apply=True)
        assert rc == 0, text
        assert created == [(legacy_work / "_plexus.yaml", "work")]
        assert "BOOTSTRAP" in text
        manifest = load_manifest(legacy_work)
        assert manifest is not None and manifest.schema == 1 and manifest.name == "work"
        assert manifest.projects == {"lmer": {"repo": LEGACY_BUCKET}}
        assert (legacy_work / "lmer" / "runs" / pf.SLUG / "state.yaml").is_file()
        assert not os.path.lexists(legacy_work / LEGACY_BUCKET)
        # Flat-named and registered, legacy-shaped inside: the validator's
        # output is now `work migrate lmer`'s to-do list, with no error.
        report = validate(legacy_work)
        assert report.form.value == "schema"
        assert report.errors == (), [f.to_dict() for f in report.errors]
        # A launch against the repo URL resolves the flat dir by registry.
        hit = resolve_project_dir(legacy_work, f"https://{LEGACY_BUCKET}.git")
        assert hit.how == REGISTRY_HIT and hit.path == legacy_work / "lmer"
        assert "_plexus.yaml: created" in commits[0].message

    def test_a_refused_registration_after_create_rolls_the_bootstrap_back(self, legacy_work, commits, monkeypatch):
        def refuse(self, *args, **kwargs):
            raise schema_writer.WriterError(self.path, "refused on purpose")

        monkeypatch.setattr(schema_writer.ManifestWriter, "add_project", refuse)
        before = _snapshot(legacy_work)
        rc, text = _render(legacy_work, bucket=LEGACY_BUCKET, name="lmer", apply=True)
        assert rc == 1 and "refused on purpose" in text and "nothing was changed" in text
        assert not (legacy_work / "_plexus.yaml").exists()
        assert _snapshot(legacy_work) == before
        assert commits == []


# ---------------------------------------------------------------------------
# Apply: ONE commit, the registry through the writer, the move as a move
# ---------------------------------------------------------------------------


class TestApply:
    def test_one_commit_registers_moves_and_validates_clean(self, work, commits):
        rc, text = _render(work, apply=True)
        assert rc == 0, text
        manifest = load_manifest(work)
        assert manifest.projects[NAME] == {"repo": BUCKET}
        assert manifest.projects[pf.PROJECT]["repo"] == pf.PRIMARY_REPO  # untouched
        assert (work / NAME / "_info" / "important.md").is_file()
        assert (work / NAME / "_runs").is_dir()
        assert not os.path.lexists(work / BUCKET)
        assert not os.path.lexists(work / pf.BUCKET_HOST)  # emptied levels removed
        report = validate(work)
        assert report.errors == (), [f.to_dict() for f in report.errors]
        assert not any(f.code == "tree.host-bucket" for f in report.findings)
        # Everything is staged — the rename as a rename, the manifest edit —
        # and nothing is left unstaged for the ONE commit.
        status = _git(work, "status", "--porcelain")
        assert all(line[1] == " " for line in status.splitlines()), status
        assert f"R  {BUCKET}/_info/important.md -> {NAME}/_info/important.md" in status
        assert "M  _plexus.yaml" in status
        assert len(commits) == 1
        call = commits[0]
        assert call.message.startswith(f"work adopt {NAME}: {BUCKET} -> {NAME}/ (repo {BUCKET})")
        assert f"git mv {BUCKET} {NAME}" in call.message
        assert f"projects.{NAME}.repo: {BUCKET}" in call.message
        assert call.paths[:3] == ["_plexus.yaml", BUCKET, NAME]

    def test_the_manifest_is_written_only_through_the_writer(self, work, commits, monkeypatch):
        calls = []
        real = schema_writer.ManifestWriter.add_project

        def spy(self, name, **kwargs):
            calls.append((name, kwargs))
            return real(self, name, **kwargs)

        monkeypatch.setattr(schema_writer.ManifestWriter, "add_project", spy)
        rc, _text = _render(
            work, apply=True, remotes=("https://github.com/example/widget.git",), description="d",
        )
        assert rc == 0
        assert calls == [(NAME, {"repo": BUCKET, "remotes": ("github.com/example/widget",), "description": "d"})]
        assert load_manifest(work).projects[NAME] == {
            "repo": BUCKET, "remotes": ["github.com/example/widget"], "description": "d",
        }

    def test_compat_link_is_a_symlink_the_validator_reports_as_info(self, work, commits):
        rc, text = _render(work, apply=True, compat_link=True)
        assert rc == 0, text
        old = work / BUCKET
        assert old.is_symlink() and os.readlink(old) == "../../widget"
        assert (old / "_info" / "important.md").is_file()  # resolves
        assert (work / pf.BUCKET_HOST / "unnamed").is_dir()  # the link's parents stay
        report = validate(work)
        assert report.errors == (), [f.to_dict() for f in report.errors]
        link = [f for f in report.findings if f.code == "tree.bucket-compat-link"]
        assert len(link) == 1 and link[0].path == BUCKET and link[0].severity.value == "INFO"
        assert f"A  {BUCKET}\n" in _git(work, "status", "--porcelain")
        assert f"ln -s ../../widget {BUCKET}" in commits[0].message

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
            root, "log", "--follow", "--format=%s", "--", f"{NAME}/_info/important.md",
        ).splitlines()
        assert subjects[-1] == "fixture"  # `git mv` kept the file's history
        assert subjects[0].startswith(f"work adopt {NAME}")
        shown = _git(root, "show", "--name-status", "--format=", "HEAD")
        assert "M\t_plexus.yaml" in shown
        assert f"R100\t{BUCKET}/_info/important.md\t{NAME}/_info/important.md" in shown
        assert validate(root).errors == ()

    def test_a_bucket_git_tracks_nothing_of_is_a_plain_rename(self, work, commits):
        (work / BUCKET / "_info" / "important.md").unlink()
        _commit_all(work, "the bucket holds only an empty run area")
        assert (work / BUCKET / "_runs").is_dir()
        plan = adopt.plan_adoption(BUCKET, NAME, root=work)
        assert plan.tracked is False
        assert f"mv {BUCKET} {NAME}" in plan.render() and "git mv" not in plan.render()
        rc, _text = _render(work, apply=True)
        assert rc == 0
        assert (work / NAME / "_runs").is_dir() and not os.path.lexists(work / BUCKET)

    def test_a_record_moves_with_its_bytes_unchanged_and_no_identity_minted(self, work, commits, monkeypatch):
        for writer in ("write_state", "append_event", "freeze_run_dir"):
            monkeypatch.setattr(run_state, writer, _raise_writer(writer))
        rdir = _write_record(
            work / BUCKET / "_runs" / "develop-issue-1" / SID, "develop-issue-1",
            _signed_out_events(SID),
        )
        _commit_all(work, "a signed-out record in the bucket")
        before = _snapshot(work)
        rc, text = _render(work, apply=True)
        assert rc == 0, text
        moved = work / NAME / "_runs" / "develop-issue-1" / SID
        for name in ("state.yaml", "events.jsonl"):
            assert (moved / name).read_bytes() == before[f"{BUCKET}/_runs/develop-issue-1/{SID}/{name}"][1]
        state = (moved / "state.yaml").read_text(encoding="utf-8")
        assert "name: null\n" in state and "session:" not in state and "uid" not in state
        assert not rdir.exists()

    def test_in_a_session_one_event_and_a_log_line_in_the_callers_record(self, work, commits, monkeypatch):
        caller = work / pf.PROJECT / "_runs" / "chat" / SID_TWO
        _write_record(caller, "chat", _signed_out_events(SID_TWO)[:2], status="in-progress")
        _commit_all(work, "the caller's own record, in another project")
        monkeypatch.setattr(run_state, "run_dir", lambda *a, **k: caller)
        monkeypatch.setattr(
            run_state, "run_rel_path_candidates", lambda *a, **k: [f"{pf.PROJECT}/_runs/chat"]
        )
        rc, _text = _render(work, apply=True)
        assert rc == 0
        events = run_state.read_events(caller, last_n=0)
        adopted = [e for e in events if e.get("type") == adopt.ADOPT_EVENT]
        assert len(adopted) == 1
        assert adopted[0]["data"]["name"] == NAME and adopted[0]["data"]["repo"] == BUCKET
        assert adopted[0]["data"]["from"] == BUCKET and adopted[0]["data"]["to"] == NAME
        log = (caller / "log.yaml").read_text(encoding="utf-8")
        assert f"work adopt {NAME}" in log and BUCKET in log
        # The caller's record rides along in the same commit.
        assert f"{pf.PROJECT}/_runs/chat" in commits[0].paths

    def test_outside_a_session_only_the_commit_records_the_adoption(self, work, commits, monkeypatch):
        monkeypatch.setattr(run_state, "append_event", _raise_writer("append_event"))
        rc, text = _render(work, apply=True)
        assert rc == 0
        assert "no session record here" in text
        assert f"git mv {BUCKET} {NAME}" in commits[0].message
        assert commits[0].paths == ["_plexus.yaml", BUCKET, NAME]


def _raise_writer(name):
    def refuse(*args, **kwargs):
        raise AssertionError(f"adopt must never call run_state.{name} ({args[:1]})")
    return refuse


# ---------------------------------------------------------------------------
# Refusals — each with the tree byte-identical
# ---------------------------------------------------------------------------


class TestRefusals:
    def _refusal_leaves_everything(self, work, commits, expect, **kwargs):
        before = _snapshot(work)
        head = _git(work, "rev-parse", "HEAD")
        rc, text = _render(work, apply=True, **kwargs)
        assert rc == 1, text
        assert expect in text, text
        assert _snapshot(work) == before
        assert _git(work, "rev-parse", "HEAD") == head
        assert _git(work, "status", "--porcelain") == ""
        assert commits == []

    # -- the §1.6 preflight, inside the lock --------------------------------

    def test_a_clean_but_live_record_in_the_bucket_refuses(self, work, commits):
        _write_record(
            work / BUCKET / "_runs" / "develop-issue-2" / SID, "develop-issue-2",
            _signed_out_events(SID)[:2], status="in-progress",
        )
        _commit_all(work, "a live record, clean in git")
        self._refusal_leaves_everything(work, commits, "live")
        assert "nothing was changed" in _render(work, apply=True)[1]

    def test_an_unreadable_record_in_the_bucket_refuses(self, work, commits):
        rdir = _write_record(
            work / BUCKET / "_runs" / "develop-issue-2" / SID, "develop-issue-2",
            _signed_out_events(SID),
        )
        (rdir / "events.jsonl").write_text(
            (rdir / "events.jsonl").read_text(encoding="utf-8") + "{torn", encoding="utf-8"
        )
        _commit_all(work, "torn events, committed")
        self._refusal_leaves_everything(work, commits, "events")

    def test_the_callers_own_record_in_the_bucket_refuses(self, work, commits, monkeypatch):
        own = _write_record(
            work / BUCKET / "_runs" / "develop-issue-2" / SID, "develop-issue-2",
            _signed_out_events(SID),
        )
        _commit_all(work, "the caller's own record, inside the bucket")
        monkeypatch.setattr(run_state, "run_dir", lambda *a, **k: own)
        self._refusal_leaves_everything(
            work, commits, "run from an operator shell or against another project"
        )

    def test_a_record_committed_while_waiting_for_the_lock_refuses(self, work, commits, monkeypatch):
        """The interleaving astra's probe found: a session creates and
        commits a live record in the bucket between the plan and the lock.
        The tree is clean in git, so only a plan built INSIDE the lock lists
        it — a plan from before would move it out from under its writer."""
        real_mutation = adopt.work_mutation
        rdir = work / BUCKET / "_runs" / "develop-issue-3" / SID

        @contextmanager
        def mutation(tree=None):
            with real_mutation(tree):
                # Another process got in first: by the time the apply holds
                # the lock, the bucket has a live record, committed.
                _write_record(
                    rdir, "develop-issue-3", _signed_out_events(SID)[:2], status="in-progress",
                )
                _commit_all(work, "a session started in the bucket while the adoption planned")
                yield

        monkeypatch.setattr(adopt, "work_mutation", mutation)
        rc, text = _render(work, apply=True)
        assert rc == 1, text
        assert "live" in text and "develop-issue-3" in text and "nothing was changed" in text
        assert rdir.is_dir() and (work / BUCKET).is_dir()  # nothing moved
        assert not os.path.lexists(work / NAME)
        assert load_manifest(work).projects.get(NAME) is None
        assert _git(work, "status", "--porcelain") == ""
        assert commits == []

    def test_the_plan_and_the_preflight_run_inside_the_lock_before_the_first_change(self, work, commits, monkeypatch):
        from work_repo import migrate

        order = []
        real_plan = adopt.plan_adoption
        real_preflight = migrate.preflight_no_live_records

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

        def preflight(records):
            order.append("preflight")
            assert (work / BUCKET).is_dir() and not os.path.lexists(work / NAME)
            assert load_manifest(work).projects.get(NAME) is None
            return real_preflight(records)

        monkeypatch.setattr(adopt, "work_mutation", mutation)
        monkeypatch.setattr(adopt, "plan_adoption", plan)
        monkeypatch.setattr(adopt, "preflight_no_live_records", preflight)
        rc, _text = _render(work, apply=True)
        assert rc == 0
        assert order == ["lock", "plan", "preflight", "unlock"]

    # -- the checks before anything moves -----------------------------------

    @pytest.mark.parametrize("name", ["Widget", "_widget", "a.b", "-x", "a/b", ""])
    def test_a_bad_shortname(self, work, commits, name):
        with pytest.raises(adopt.AdoptError) as excinfo:
            adopt.plan_adoption(BUCKET, name, root=work)
        assert "short name" in str(excinfo.value)
        self._refusal_leaves_everything(work, commits, "short name", name=name)

    def test_a_name_already_in_the_registry(self, work, commits):
        with pytest.raises(adopt.AdoptError) as excinfo:
            adopt.plan_adoption(BUCKET, pf.PROJECT, root=work)
        assert f"projects.{pf.PROJECT}" in str(excinfo.value) and "already" in str(excinfo.value)
        self._refusal_leaves_everything(work, commits, "already", name=pf.PROJECT)

    def test_a_name_already_used_by_a_top_level_dir(self, work, commits):
        (work / NAME).mkdir()
        (work / NAME / "README.md").write_text("taken\n", encoding="utf-8")
        _commit_all(work, "an unregistered dir with the name")
        with pytest.raises(adopt.AdoptError) as excinfo:
            adopt.plan_adoption(BUCKET, NAME, root=work)
        assert f"`{NAME}/` already exists" in str(excinfo.value)
        self._refusal_leaves_everything(work, commits, "already exists")

    def test_a_remote_already_bound_to_another_project(self, work, commits):
        bound = f"https://{pf.REMOTES[0]}.git"
        with pytest.raises(adopt.AdoptError) as excinfo:
            adopt.plan_adoption(BUCKET, NAME, root=work, remotes=(bound,))
        assert f"projects.{pf.PROJECT}" in str(excinfo.value) and pf.REMOTES[0] in str(excinfo.value)
        self._refusal_leaves_everything(work, commits, "bound", remotes=(bound,))

    def test_the_buckets_own_remote_registered_elsewhere_is_a_split_tree(self, work, commits):
        manifest = work / "_plexus.yaml"
        # The fixture's `projects:` section ends where `sources:` begins.
        head, sep, tail = manifest.read_text(encoding="utf-8").partition("sources:")
        manifest.write_text(head + f"  gadgets:\n    repo: {BUCKET}\n" + sep + tail, encoding="utf-8")
        _commit_all(work, "the bucket's remote registered under another name")
        assert load_manifest(work).projects["gadgets"] == {"repo": BUCKET}
        with pytest.raises(adopt.AdoptError) as excinfo:
            adopt.plan_adoption(BUCKET, NAME, root=work)
        assert "split tree" in str(excinfo.value) and "projects.gadgets" in str(excinfo.value)
        self._refusal_leaves_everything(work, commits, "split tree")

    def test_two_remotes_spelling_one_repo_are_the_registrys_refusal(self, work, commits):
        pair = ("https://github.com/example/widget.git", "git@github.com:example/widget.git")
        with pytest.raises(adopt.AdoptError) as excinfo:
            adopt.plan_adoption(BUCKET, NAME, root=work, remotes=pair)
        assert "registry refuses" in str(excinfo.value)
        self._refusal_leaves_everything(work, commits, "registry refuses", remotes=pair)

    def test_a_remote_that_is_not_a_url(self, work, commits):
        with pytest.raises(adopt.AdoptError) as excinfo:
            adopt.plan_adoption(BUCKET, NAME, root=work, remotes=("origin",))
        assert "origin" in str(excinfo.value) and "not a repository URL" in str(excinfo.value)

    def test_a_newer_schema_int_is_not_writable(self, work, commits):
        manifest = work / "_plexus.yaml"
        manifest.write_text(manifest.read_text(encoding="utf-8").replace("schema: 1 ", "schema: 99"), encoding="utf-8")
        _commit_all(work, "a newer lmer wrote this manifest")
        with pytest.raises(adopt.AdoptError) as excinfo:
            adopt.plan_adoption(BUCKET, NAME, root=work)
        assert "not writable" in str(excinfo.value) and "99" in str(excinfo.value)
        self._refusal_leaves_everything(work, commits, "not writable")

    def test_a_symlinked_manifest_is_not_writable(self, work, commits):
        manifest = work / "_plexus.yaml"
        real = work / "manifest-elsewhere.yaml"
        real.write_text(manifest.read_text(encoding="utf-8"), encoding="utf-8")
        manifest.unlink()
        manifest.symlink_to("manifest-elsewhere.yaml")
        _commit_all(work, "a symlinked manifest")
        with pytest.raises(adopt.AdoptError) as excinfo:
            adopt.plan_adoption(BUCKET, NAME, root=work)
        assert "not writable" in str(excinfo.value) and "symlink" in str(excinfo.value)
        self._refusal_leaves_everything(work, commits, "not writable")

    def test_dirty_paths_are_reported_by_the_dry_run_and_refuse_the_apply(self, work, commits):
        stray = work / BUCKET / "_info" / "notes.txt"
        stray.write_text("uncommitted\n", encoding="utf-8")
        before = _snapshot(work)
        rc, text = _render(work)
        assert rc == 0 and "dirty" in text and "notes.txt" in text
        rc, text = _render(work, apply=True)
        assert rc == 1
        assert "dirty" in text and "nothing was changed" in text
        assert _snapshot(work) == before
        assert commits == []
        assert "notes.txt" in _git(work, "status", "--porcelain")

    def test_a_dirty_manifest_refuses_the_apply(self, work, commits):
        manifest = work / "_plexus.yaml"
        manifest.write_text(manifest.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
        before = _snapshot(work)
        rc, text = _render(work, apply=True)
        assert rc == 1 and "dirty" in text and "_plexus.yaml" in text
        assert _snapshot(work) == before
        assert commits == []

    def test_a_symlinked_bucket_path_is_refused(self, work, commits):
        real = work / "elsewhere"
        (work / BUCKET).rename(real)
        (work / BUCKET).symlink_to("../../elsewhere")
        _commit_all(work, "the bucket path is a symlink")
        with pytest.raises(adopt.AdoptError) as excinfo:
            adopt.plan_adoption(BUCKET, NAME, root=work)
        assert "symlink" in str(excinfo.value)
        self._refusal_leaves_everything(work, commits, "symlink")

    @pytest.mark.parametrize("path", [
        f"{pf.BUCKET_HOST}/unnamed",      # a namespace level above the project
        f"{pf.BUCKET_HOST}/unnamed/widget/_runs",  # below it
        f"{pf.BUCKET_HOST}/nothing/here",  # not there
        pf.PROJECT,                        # a registered short name, not a bucket
        f"../{pf.BUCKET_HOST}/unnamed/widget",
    ])
    def test_a_path_that_is_not_a_bucket(self, work, path):
        with pytest.raises(adopt.AdoptError):
            adopt.plan_adoption(path, NAME, root=work)

    def test_the_commit_helper_binds_apply_to_the_configured_checkout(self, work, commits, monkeypatch, tmp_path):
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path / "elsewhere"))
        rc, text = _render(work, apply=True)
        assert rc == 1 and "LMER_WORK_REPO_PATH" in text
        assert _git(work, "status", "--porcelain") == ""
        assert commits == []


# ---------------------------------------------------------------------------
# The CLI seam (registration is B.adopt's cli.py task)
# ---------------------------------------------------------------------------


class TestCliSeam:
    def test_write_rel_paths_is_none_for_a_dry_run(self, work):
        assert adopt.write_rel_paths(SimpleNamespace(bucket=BUCKET, name=NAME, apply=False)) is None

    def test_write_rel_paths_names_the_manifest_both_paths_and_the_callers_run(self, work, monkeypatch):
        assert adopt.write_rel_paths(SimpleNamespace(bucket=BUCKET, name=NAME, apply=True)) == [
            "_plexus.yaml", BUCKET, NAME,
        ]
        monkeypatch.setattr(run_state, "run_rel_path_candidates", lambda *a, **k: ["lmer/_runs/chat"])
        assert adopt.write_rel_paths(SimpleNamespace(bucket=BUCKET, name=NAME, apply=True)) == [
            "_plexus.yaml", BUCKET, NAME, "lmer/_runs/chat",
        ]

    @pytest.mark.parametrize("bucket", ["../etc", "/abs/path", "lmer", "gitlab.example.com", "", None])
    def test_write_rel_paths_drops_a_malformed_bucket_and_name(self, work, bucket):
        assert adopt.write_rel_paths(SimpleNamespace(bucket=bucket, name="Bad.Name", apply=True)) == [
            "_plexus.yaml",
        ]

    def test_cmd_adopt_dry_run_prints_the_plan(self, work, capsys):
        rc = adopt.cmd_adopt(SimpleNamespace(
            bucket=BUCKET, name=NAME, apply=False, compat_link=False,
            description=None, remote=None, force=False,
        ))
        assert rc == 0
        assert f"git mv {BUCKET} {NAME}" in capsys.readouterr().out

    def test_cmd_adopt_reports_a_refusal_on_stderr(self, work, capsys):
        rc = adopt.cmd_adopt(SimpleNamespace(
            bucket=BUCKET, name="Widget", apply=False, compat_link=False,
            description=None, remote=[], force=False,
        ))
        assert rc == 1
        assert "short name" in capsys.readouterr().err

    def test_cmd_adopt_passes_every_flag_through(self, work, monkeypatch):
        seen = {}

        def fake(bucket, name, **kwargs):
            seen.update(bucket=bucket, name=name, **kwargs)
            return 0

        monkeypatch.setattr(adopt, "adopt_project", fake)
        adopt.cmd_adopt(SimpleNamespace(
            bucket=BUCKET, name=NAME, apply=True, compat_link=True,
            description="d", remote=["https://github.com/example/widget.git"], force=True,
        ))
        assert seen == {
            "bucket": BUCKET, "name": NAME, "apply": True, "compat_link": True,
            "description": "d", "remotes": ("https://github.com/example/widget.git",), "force": True,
        }


# ---------------------------------------------------------------------------
# The thin-consumer guard (§13.B): no YAML dump of its own, the writer is the
# one write path to the manifest, nothing imported from the tests.
# ---------------------------------------------------------------------------


class TestThinConsumer:
    @pytest.fixture
    def tree(self):
        return ast.parse(ADOPT_PATH.read_text(encoding="utf-8"))

    def test_no_yaml_dump_and_no_ruamel(self, tree):
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"dump", "safe_dump", "dump_all", "safe_dump_all"}, ast.dump(node)
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith("ruamel") for a in node.names), ast.dump(node)
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(("ruamel", "tests")), ast.dump(node)

    def test_it_writes_no_file_of_its_own(self, tree):
        """The manifest goes through the writer, the caller's record through
        run_state/loggers; adopt itself renames, links and removes
        directories and opens nothing for writing."""
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"write_text", "write_bytes"}, ast.dump(node)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "open", ast.dump(node)

    def test_the_preflight_is_migrates(self, tree):
        """ONE lifecycle rule (§1.6): the preflight is imported from migrate,
        never restated here."""
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "migrate"
            for alias in node.names
        }
        assert "preflight_no_live_records" in imported
        defined = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        assert "preflight_no_live_records" not in defined
        assert not any(name.startswith("is_stopped") for name in defined)
