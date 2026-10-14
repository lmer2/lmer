"""Tests for the central specs index (issue #101, form-routed by #353).

The index is the project-scope specs area — `{host}/{project}/specs/` in a
legacy tree, `<project>/_specs/` under the registry-resolved project dir in a
schema-form one (D20) — holding one dated relative symlink per spec-class
artifact, `YYYY-MM-DD-<run>--<basename>`, with `--` the reserved
label/basename separator (labels never contain one). Targets point at the
spec's canonical location: `../runs/<dir>/…` in a legacy tree and
`../_runs/<slug>/<session-dir>/…` in schema form, where the record lives one
level deeper (D22 — the session is the unit of record). Maintained by `work
artifact` registration and the masterplan bundle sync (which links the BUNDLE
file, never the run-root symlink); `work specs-index --rebuild` is the
backfill path. Symlinks-only by design (a dir listing IS the index), and
always fail-soft — index trouble never fails a registration.

The legacy classes below are the pre-#353 suite, unchanged: a manifest-less
tree writes the same entries at the same names against the same targets. The
four `#353` classes at the end add the form routing, the deeper target, the
re-point across the session level and the one-level-deeper rebuild, each with
the pre-routing implementation as its oracle rather than a hand-written
expectation.
"""
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from lmer_schema.checks import Form, Report
from lmer_schema.checks.index_links import check_index_links
from work_repo import cli as work_cli
from work_repo import run_state, specs_index, utils
from tests.conftest import strip_lmer_env
from tests.plexus_fixtures import (
    BUCKET_HOST,
    BUCKET_PROJECT,
    PRIMARY_REPO,
    PROJECT,
    SLUG,
    build_legacy_plexus,
    build_schema_plexus,
)

#: Two launches against one target, in the `LMER_SESSION_ID` form.
SID_A = "20260824-101500-1-11111"
SID_B = "20260824-113000-1-22222"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    utils._ANNOUNCED_MANIFEST_PROBLEMS.clear()
    run_state._reset_synthesized_session_id()


@pytest.fixture
def run_env(monkeypatch, tmp_path):
    """Set the full env trio + task context; return the expected run dir."""
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
    monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
    monkeypatch.setenv("LMER_REPO_PROJECT", "org/repo")
    monkeypatch.setenv("LMER_TASK", "develop")
    monkeypatch.setenv("LMER_TASK_TARGET", "issue-101")
    monkeypatch.setenv("LMER_SESSION_ID", "s-specs-1")
    return tmp_path / "git.example.com" / "org/repo" / "runs" / "develop-issue-101"


def _specs_dir(tmp_path: Path) -> Path:
    return tmp_path / "git.example.com" / "org/repo" / "specs"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _main(argv):
    with patch.object(work_cli.sys, "argv", ["work"] + argv):
        return work_cli.main()


def _register(tmp_path, name, content="# spec\n"):
    src = tmp_path / "src.md"
    src.write_text(content)
    with patch("work_repo.cli.commit_work_path", return_value=0):
        return _main(["artifact", name, "--file", str(src)])


class TestIsSpecArtifact:
    @pytest.mark.parametrize("name", [
        "spec.md", "foo-spec.md", "masterplan-spec.md", "spec-auth.md",
        "my_spec.md", "SPEC.md", "spec.v2.md",
    ])
    def test_spec_class_names(self, name):
        assert specs_index.is_spec_artifact(name)

    @pytest.mark.parametrize("name", [
        "plan.md", "spec.py", "specifics.md", "inspect.md", "respect.md",
        "goals.md", "retro.md", "spec", "spec.md.bak",
    ])
    def test_non_spec_names(self, name):
        assert not specs_index.is_spec_artifact(name)


class TestArtifactRegistrationIndexes:
    def test_spec_artifact_creates_dated_relative_symlink(
        self, run_env, tmp_path, capsys
    ):
        assert _register(tmp_path, "spec.md") == 0
        entry = _specs_dir(tmp_path) / f"{_today()}-develop-issue-101--spec.md"
        assert entry.is_symlink()
        assert os.readlink(entry) == "../runs/develop-issue-101/spec.md"
        assert entry.read_text() == "# spec\n"
        assert f"Specs index: {entry}" in capsys.readouterr().out

    def test_entry_uses_run_name_over_slug(self, run_env, tmp_path):
        state = run_state.seed_state("develop-issue-101", "develop", "issue-101")
        state["name"] = "specs-index"
        run_state.write_state(run_env, state)
        assert _register(tmp_path, "spec.md") == 0
        entry = _specs_dir(tmp_path) / f"{_today()}-specs-index--spec.md"
        assert entry.is_symlink()

    def test_non_spec_artifact_not_indexed(self, run_env, tmp_path):
        assert _register(tmp_path, "plan.md") == 0
        assert not _specs_dir(tmp_path).exists()

    def test_reregistration_same_day_is_idempotent(self, run_env, tmp_path):
        assert _register(tmp_path, "spec.md", "v1\n") == 0
        assert _register(tmp_path, "spec.md", "v2\n") == 0
        entries = list(_specs_dir(tmp_path).iterdir())
        assert len(entries) == 1
        assert entries[0].read_text() == "v2\n"  # same link, canonical repointed content

    def test_specs_path_staged_with_the_push(self, run_env, tmp_path):
        with patch("work_repo.cli.commit_work_path", return_value=0) as commit:
            src = tmp_path / "src.md"
            src.write_text("# spec\n")
            assert _main(["artifact", "spec.md", "--file", str(src)]) == 0
        staged = commit.call_args[0][0]
        assert "git.example.com/org/repo/specs" in staged

    def test_linked_registration_indexes_canonical_file(
        self, run_env, tmp_path
    ):
        # #103: registering an in-run source makes runs/<slug>/spec.md a
        # symlink; the index entry must target the CANONICAL file, not it.
        run_state.write_state(
            run_env,
            run_state.seed_state("develop-issue-101", "develop", "issue-101"),
        )
        bundle = run_env / "masterplan" / "mp-a"
        bundle.mkdir(parents=True)
        (bundle / "spec.md").write_text("# canonical\n")
        with patch("work_repo.cli.commit_work_path", return_value=0):
            assert _main(
                ["artifact", "spec.md", "--file", str(bundle / "spec.md")]
            ) == 0
        assert (run_env / "spec.md").is_symlink()
        entry = _specs_dir(tmp_path) / f"{_today()}-develop-issue-101--spec.md"
        assert entry.is_symlink()
        assert os.readlink(entry) == (
            "../runs/develop-issue-101/masterplan/mp-a/spec.md"
        )

    def test_index_failure_never_fails_registration(
        self, run_env, tmp_path, capsys
    ):
        # A regular FILE squatting on the specs/ path makes mkdir raise —
        # the registration must still succeed, with a warning.
        _specs_dir(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        _specs_dir(tmp_path).write_text("not a directory\n")
        assert _register(tmp_path, "spec.md") == 0
        out = capsys.readouterr().out
        assert "✅ Artifact registered" in out
        assert "specs index skipped" in out


class TestUpsertSpecLink:
    def test_later_day_reregistration_keeps_the_original_date(
        self, run_env, tmp_path
    ):
        """#334: an existing entry's date always wins; `when` dates only a
        NEW entry, so a later-day re-registration re-points it in place."""
        run_env.mkdir(parents=True)
        (run_env / "spec.md").write_text("# spec\n")
        old = specs_index.upsert_spec_link(
            run_env / "spec.md", "issue-101",
            when=datetime(2026, 7, 1, tzinfo=timezone.utc),
        )
        assert old is not None and old.name == "2026-07-01-issue-101--spec.md"
        new = specs_index.upsert_spec_link(run_env / "spec.md", "issue-101")
        assert new.name == old.name  # the original date wins
        assert [p.name for p in specs_index.list_entries()] == [old.name]

    def test_suffix_label_collision_not_deleted(self, run_env, tmp_path):
        # `my-run` entries must never be treated as stale entries of `run`.
        run_env.mkdir(parents=True)
        (run_env / "spec.md").write_text("x")
        other = specs_index.upsert_spec_link(
            run_env / "spec.md", "my-run",
            when=datetime(2026, 7, 1, tzinfo=timezone.utc),
        )
        mine = specs_index.upsert_spec_link(run_env / "spec.md", "run")
        assert other.exists() and mine.exists()

    def test_reserved_separator_keeps_distinct_pairs_distinct(
        self, run_env, tmp_path
    ):
        # With a single `-`, (label `a`, `b-spec.md`) and (label `a-b`,
        # `spec.md`) would both name `…-a-b-spec.md` — colliding, and the
        # stale cleanup would clobber one pair's entry with the other's.
        # The reserved `--` separator keeps them apart in BOTH directions.
        run_env.mkdir(parents=True)
        (run_env / "b-spec.md").write_text("x")
        (run_env / "spec.md").write_text("y")
        one = specs_index.upsert_spec_link(
            run_env / "b-spec.md", "a",
            when=datetime(2026, 7, 1, tzinfo=timezone.utc),
        )
        two = specs_index.upsert_spec_link(run_env / "spec.md", "a-b")
        assert one.name == "2026-07-01-a--b-spec.md"
        assert two.name == f"{_today()}-a-b--spec.md"
        assert one.exists() and two.exists()  # neither clobbered the other

    def test_label_hyphen_runs_collapsed(self, run_env, tmp_path):
        # Dir-name fallback labels can be `slug--name` shaped; runs of
        # hyphens collapse so the label component never contains the
        # reserved `--` and the first post-date `--` splits unambiguously.
        run_env.mkdir(parents=True)
        (run_env / "spec.md").write_text("x")
        link = specs_index.upsert_spec_link(
            run_env / "spec.md", "develop-issue-101--nice-name"
        )
        assert link.name == f"{_today()}-develop-issue-101-nice-name--spec.md"

    def test_legacy_single_hyphen_entry_cleaned_up(self, run_env, tmp_path):
        # Entries written before the reserved `--` separator existed
        # (`<date>-<label>-<basename>`) count as stale for the same pair.
        run_env.mkdir(parents=True)
        (run_env / "spec.md").write_text("x")
        sdir = _specs_dir(tmp_path)
        sdir.mkdir(parents=True)
        legacy = sdir / "2026-07-01-issue-101-spec.md"
        legacy.symlink_to("../runs/develop-issue-101/spec.md")
        new = specs_index.upsert_spec_link(run_env / "spec.md", "issue-101")
        # The legacy entry's date is kept, in the current `--` format (#334).
        assert new.name == "2026-07-01-issue-101--spec.md"
        assert not legacy.exists()
        assert [p.name for p in specs_index.list_entries()] == [new.name]

    def test_legacy_cleanup_skipped_for_double_hyphen_label(
        self, run_env, tmp_path
    ):
        # A `--`-bearing label's legacy pattern (`a--b-spec.md`) would match
        # the CURRENT-format entry of a different pair (label `a`,
        # `b-spec.md`), so legacy cleanup is skipped for such labels —
        # `--rebuild` covers whatever this leaves behind.
        run_env.mkdir(parents=True)
        (run_env / "b-spec.md").write_text("x")
        (run_env / "spec.md").write_text("y")
        other = specs_index.upsert_spec_link(
            run_env / "b-spec.md", "a",
            when=datetime(2026, 7, 1, tzinfo=timezone.utc),
        )
        mine = specs_index.upsert_spec_link(run_env / "spec.md", "a--b")
        assert mine.name == f"{_today()}-a-b--spec.md"  # collapsed label
        assert other.exists() and mine.exists()

    def test_wrong_symlink_repointed(self, run_env, tmp_path):
        run_env.mkdir(parents=True)
        (run_env / "spec.md").write_text("x")
        sdir = _specs_dir(tmp_path)
        sdir.mkdir(parents=True)
        entry = sdir / f"{_today()}-issue-101--spec.md"
        entry.symlink_to("somewhere/else.md")
        specs_index.upsert_spec_link(run_env / "spec.md", "issue-101")
        assert os.readlink(entry) == "../runs/develop-issue-101/spec.md"

    def test_path_outside_work_repo_skipped(self, run_env, tmp_path):
        outside = tmp_path.parent / "elsewhere-spec.md"
        outside.write_text("x")
        assert specs_index.upsert_spec_link(outside, "issue-101") is None
        assert not _specs_dir(tmp_path).exists()

    def test_no_run_context_returns_none(self, tmp_path):
        spec = tmp_path / "spec.md"
        spec.write_text("x")
        assert specs_index.upsert_spec_link(spec, "label") is None


class TestMasterplanSyncIndexes:
    def _seed_run(self, run_env, name=None):
        state = run_state.seed_state("develop-issue-101", "develop", "issue-101")
        if name:
            state["name"] = name
        run_state.write_state(run_env, state)

    def _bundle(self, run_env, mp_slug, *names):
        bundle = run_env / "masterplan" / mp_slug
        bundle.mkdir(parents=True, exist_ok=True)
        for name in names:
            (bundle / name).write_text(f"{mp_slug}:{name}\n")
        return bundle

    def test_bundle_spec_linked_to_bundle_file(self, run_env, tmp_path):
        self._seed_run(run_env)
        self._bundle(run_env, "mp-a", "spec.md", "plan.md")
        run_state.sync_masterplan_artifacts(run_env)
        entry = _specs_dir(tmp_path) / f"{_today()}-develop-issue-101--spec.md"
        assert entry.is_symlink()
        # Canonical target is the BUNDLE file, not the run-root symlink.
        assert os.readlink(entry) == (
            "../runs/develop-issue-101/masterplan/mp-a/spec.md"
        )
        assert entry.read_text() == "mp-a:spec.md\n"
        # plan.md is masterplan-linked but not spec-class — not indexed.
        assert [p.name for p in specs_index.list_entries()] == [entry.name]

    def test_multi_bundle_specs_each_indexed(self, run_env, tmp_path):
        self._seed_run(run_env, name="specs-index")
        self._bundle(run_env, "mp-a", "spec.md")
        self._bundle(run_env, "mp-b", "spec.md")
        run_state.sync_masterplan_artifacts(run_env)
        # Entry basenames follow the prefixed run-root link names, so two
        # bundles' spec.md files stay distinct, each at its own bundle.
        entries = specs_index.list_entries()
        assert [p.name for p in entries] == [
            f"{_today()}-specs-index--mp-a-spec.md",
            f"{_today()}-specs-index--mp-b-spec.md",
        ]
        assert os.readlink(entries[0]) == (
            "../runs/develop-issue-101/masterplan/mp-a/spec.md"
        )

    def test_registration_date_survives_a_later_session_start(
        self, run_env, tmp_path
    ):
        """#334: the next session start's sync keeps the entry's original
        date instead of re-dating it under today."""
        self._seed_run(run_env)
        self._bundle(run_env, "mp-a", "spec.md")
        run_state.sync_masterplan_artifacts(run_env)
        original = specs_index.list_entries()[0].name
        # A later session start runs the sync again; the date is unchanged.
        run_state.sync_masterplan_artifacts(run_env)
        assert [p.name for p in specs_index.list_entries()] == [original]

    def test_sync_idempotent_in_index(self, run_env, tmp_path):
        self._seed_run(run_env)
        self._bundle(run_env, "mp-a", "spec.md")
        run_state.sync_masterplan_artifacts(run_env)
        before = specs_index.list_entries()
        run_state.sync_masterplan_artifacts(run_env)
        assert specs_index.list_entries() == before

    def test_index_failure_never_fails_sync(self, run_env, tmp_path, capsys):
        self._seed_run(run_env)
        self._bundle(run_env, "mp-a", "spec.md")
        _specs_dir(tmp_path).write_text("not a directory\n")
        assert run_state.sync_masterplan_artifacts(run_env) == ["spec.md"]
        assert "specs index skipped" in capsys.readouterr().out


class TestFreezeRenameRepointsIndex:
    """The freeze's `runs/<slug>/` → `runs/<slug>--<name>/` rename must not
    leave index entries dangling (review on !126): entries are relative
    symlinks into the old dir, and the label change (slug → name) defeats
    upsert's stale-cleanup, so freeze_run_dir re-points them itself."""

    def _seed_named_run(self, run_env, name="specs-index"):
        state = run_state.seed_state("develop-issue-101", "develop", "issue-101")
        state["name"] = name
        run_env.mkdir(parents=True, exist_ok=True)
        run_state.write_state(run_env, state)
        return state

    def test_entry_repointed_and_relabelled_on_freeze(self, run_env, tmp_path):
        state = self._seed_named_run(run_env)
        (run_env / "spec.md").write_text("# spec\n")
        # Registered during planning, before the freeze — under the slug
        # label and an earlier date, like a real `work artifact` call.
        old = specs_index.upsert_spec_link(
            run_env / "spec.md", "develop-issue-101",
            when=datetime(2026, 7, 1, tzinfo=timezone.utc),
        )
        assert os.readlink(old) == "../runs/develop-issue-101/spec.md"

        rdir, old_name = run_state.freeze_run_dir(run_env, state)

        assert rdir.name == "develop-issue-101--specs-index"
        assert old_name == "develop-issue-101"
        entries = specs_index.list_entries()
        assert [p.name for p in entries] == ["2026-07-01-specs-index--spec.md"]
        entry = entries[0]
        assert os.readlink(entry) == (
            "../runs/develop-issue-101--specs-index/spec.md"
        )
        assert entry.read_text() == "# spec\n"  # resolves — not dangling
        assert not old.exists()  # old slug-labelled entry is gone

    def test_aliased_bundle_entry_keeps_alias(self, run_env, tmp_path):
        state = self._seed_named_run(run_env)
        bundle = run_env / "masterplan" / "mp-a"
        bundle.mkdir(parents=True)
        (bundle / "spec.md").write_text("mp-a\n")
        specs_index.upsert_spec_link(
            bundle / "spec.md", "develop-issue-101",
            when=datetime(2026, 7, 2, tzinfo=timezone.utc),
            alias="mp-a-spec.md",
        )

        run_state.freeze_run_dir(run_env, state)

        entries = specs_index.list_entries()
        assert [p.name for p in entries] == ["2026-07-02-specs-index--mp-a-spec.md"]
        assert os.readlink(entries[0]) == (
            "../runs/develop-issue-101--specs-index/masterplan/mp-a/spec.md"
        )

    def test_prefix_label_does_not_missplit_new_format_entry(
        self, run_env, tmp_path
    ):
        # The old slug being a dash-prefix of the entry's created label must
        # not shear the label mid-way (review on !156 followup): a legacy
        # `<candidate>-` strip tried before the `--` split would turn
        # `…-develop-issue-101--spec.md` parsed with old slug
        # `develop-issue` into basename `101--spec.md`.
        # Post-rename layout: repoint_run_dir_entries runs AFTER the dir move.
        new_dir = run_env.parent / "develop-issue--specs-index"
        new_dir.mkdir(parents=True)
        state = run_state.seed_state("develop-issue", "develop", "issue")
        state["name"] = "specs-index"
        run_state.write_state(new_dir, state)
        (new_dir / "spec.md").write_text("# spec\n")
        # Entry created under a label the old slug prefixes, still pointing
        # into the OLD dir.
        sdir = _specs_dir(tmp_path)
        sdir.mkdir(parents=True)
        entry = sdir / "2026-07-01-develop-issue-101--spec.md"
        entry.symlink_to("../runs/develop-issue/spec.md")

        specs_index.repoint_run_dir_entries(
            "develop-issue", "develop-issue--specs-index",
            "develop-issue-101",
        )

        names = [p.name for p in specs_index.list_entries()]
        assert names == ["2026-07-01-develop-issue-101--spec.md"]
        assert os.readlink(sdir / names[0]) == (
            "../runs/develop-issue--specs-index/spec.md"
        )

    def test_legacy_single_dash_entry_repoints_to_new_format(
        self, run_env, tmp_path
    ):
        # Entries created before the `--` separator existed use
        # `<label>-<base>`; the re-point must still parse them and rewrite
        # under the new `--` naming.
        state = self._seed_named_run(run_env)
        (run_env / "spec.md").write_text("# spec\n")
        sdir = _specs_dir(tmp_path)
        sdir.mkdir(parents=True)
        legacy = sdir / "2026-07-01-develop-issue-101-spec.md"
        legacy.symlink_to("../runs/develop-issue-101/spec.md")

        run_state.freeze_run_dir(run_env, state)

        names = [p.name for p in specs_index.list_entries()]
        assert names == ["2026-07-01-specs-index--spec.md"]
        assert not legacy.exists()

    def test_other_runs_entries_untouched(self, run_env, tmp_path):
        state = self._seed_named_run(run_env)
        (run_env / "spec.md").write_text("x")
        specs_index.upsert_spec_link(run_env / "spec.md", "develop-issue-101")
        other_run = run_env.parent / "develop-issue-7"
        other_run.mkdir(parents=True)
        (other_run / "spec.md").write_text("other\n")
        other = specs_index.upsert_spec_link(other_run / "spec.md", "develop-issue-7")

        run_state.freeze_run_dir(run_env, state)

        assert other.is_symlink()
        assert os.readlink(other) == "../runs/develop-issue-7/spec.md"
        assert other.read_text() == "other\n"

    def test_index_trouble_never_fails_freeze(self, run_env, tmp_path, capsys):
        state = self._seed_named_run(run_env)
        (run_env / "spec.md").write_text("x")
        specs_index.upsert_spec_link(run_env / "spec.md", "develop-issue-101")
        with patch.object(
            specs_index, "repoint_run_dir_entries",
            side_effect=RuntimeError("boom"),
        ):
            rdir, old_name = run_state.freeze_run_dir(run_env, state)
        assert rdir.name == "develop-issue-101--specs-index"
        assert "specs index re-point failed" in capsys.readouterr().out

    def test_unnamed_run_no_rename_no_repoint(self, run_env, tmp_path):
        state = run_state.seed_state("develop-issue-101", "develop", "issue-101")
        run_env.mkdir(parents=True, exist_ok=True)
        run_state.write_state(run_env, state)
        (run_env / "spec.md").write_text("x")
        entry = specs_index.upsert_spec_link(run_env / "spec.md", "develop-issue-101")

        rdir, old_name = run_state.freeze_run_dir(run_env, state)

        assert rdir == run_env and old_name is None
        assert os.readlink(entry) == "../runs/develop-issue-101/spec.md"


class TestSpecsIndexVerb:
    def test_no_context_errors(self, capsys):
        assert _main(["specs-index"]) == 1
        assert "No run context" in capsys.readouterr().err

    def test_empty_index_lists_nothing(self, run_env, capsys):
        assert _main(["specs-index"]) == 0
        assert "empty" in capsys.readouterr().out

    def test_list_shows_entries_with_targets(self, run_env, tmp_path, capsys):
        assert _register(tmp_path, "spec.md") == 0
        capsys.readouterr()
        assert _main(["specs-index"]) == 0
        out = capsys.readouterr().out
        assert (
            f"{_today()}-develop-issue-101--spec.md -> "
            "../runs/develop-issue-101/spec.md" in out
        )

    def test_rebuild_backfills_registered_artifacts(
        self, run_env, tmp_path, capsys
    ):
        # A pre-index run: registered spec artifact, no specs/ entry yet.
        state = run_state.seed_state("develop-issue-101", "develop", "issue-101")
        state["artifacts"] = {"spec": "spec.md", "plan": "plan.md"}
        run_state.write_state(run_env, state)
        (run_env / "spec.md").write_text("# spec\n")
        (run_env / "plan.md").write_text("# plan\n")
        run_state.append_event(run_env, "artifact_written", note="spec.md")
        assert _main(["specs-index", "--rebuild"]) == 0
        out = capsys.readouterr().out
        assert "Specs index rebuilt: 1 entries" in out
        entry = _specs_dir(tmp_path) / f"{_today()}-develop-issue-101--spec.md"
        assert entry.is_symlink()
        assert os.readlink(entry) == "../runs/develop-issue-101/spec.md"

    def test_rebuild_dates_entry_from_artifact_event(self, run_env, tmp_path):
        state = run_state.seed_state("develop-issue-101", "develop", "issue-101")
        state["artifacts"] = {"spec": "spec.md"}
        run_state.write_state(run_env, state)
        (run_env / "spec.md").write_text("# spec\n")
        (run_env / "events.jsonl").write_text(
            '{"ts": "2026-07-01T10:00:00Z", "type": "artifact_written",'
            ' "note": "spec.md"}\n'
        )
        assert _main(["specs-index", "--rebuild"]) == 0
        entry = _specs_dir(tmp_path) / "2026-07-01-develop-issue-101--spec.md"
        assert entry.is_symlink()

    def test_rebuild_follows_run_root_symlink_to_bundle(
        self, run_env, tmp_path
    ):
        # A masterplan run: run-root spec.md is a convenience symlink; the
        # rebuilt entry must target the bundle file (one canonical target).
        run_state.write_state(
            run_env, run_state.seed_state("develop-issue-101", "develop", "t")
        )
        bundle = run_env / "masterplan" / "mp-a"
        bundle.mkdir(parents=True)
        (bundle / "spec.md").write_text("mp-a:spec.md\n")
        (run_env / "spec.md").symlink_to("masterplan/mp-a/spec.md")
        assert _main(["specs-index", "--rebuild"]) == 0
        entries = specs_index.list_entries()
        assert len(entries) == 1
        assert os.readlink(entries[0]) == (
            "../runs/develop-issue-101/masterplan/mp-a/spec.md"
        )

    def test_rebuild_drops_stale_entries(self, run_env, tmp_path):
        sdir = _specs_dir(tmp_path)
        sdir.mkdir(parents=True)
        (sdir / "2026-01-01-gone-run-spec.md").symlink_to("../runs/gone/spec.md")
        assert _main(["specs-index", "--rebuild"]) == 0
        assert specs_index.list_entries() == []

    def test_rebuild_skips_dot_and_archive_dirs(self, run_env, tmp_path):
        runs = run_env.parent
        for dirname in (".new-s1-x", "archive"):
            d = runs / dirname
            d.mkdir(parents=True)
            (d / "spec.md").write_text("hidden\n")
        assert _main(["specs-index", "--rebuild"]) == 0
        assert specs_index.list_entries() == []


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd),
         "-c", "user.name=test", "-c", "user.email=test@example.com",
         *args],
        check=True, capture_output=True,
    )


@pytest.fixture
def git_work_repo(run_env, monkeypatch, tmp_path):
    """A real work-repo clone with a bare origin, pointed at by the env —
    the staging-gap regression needs actual `git add` semantics."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)],
        check=True, capture_output=True,
    )
    clone = tmp_path / "workrepo"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(clone)],
        check=True, capture_output=True,
    )
    _git(clone, "config", "user.name", "test")
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "commit", "-q", "--allow-empty", "-m", "init")
    _git(clone, "push", "-q", "-u", "origin", "main")
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(clone))
    return clone


class TestIndexEntriesPushed:
    """The !140 staging gap: index entries written without a push of their
    own (`--rebuild`, the masterplan sync) sat untracked forever because
    `work commit` never staged specs/ — these prove they actually land."""

    def _origin_files(self, clone):
        out = subprocess.run(
            ["git", "-C", str(clone),
             "ls-tree", "-r", "--name-only", "origin/main"],
            check=True, capture_output=True, text=True,
        ).stdout
        return out.splitlines()

    def test_rebuild_then_commit_pushes_entries(self, git_work_repo):
        clone = git_work_repo
        rdir = clone / "git.example.com" / "org/repo" / "runs" / "develop-issue-101"
        state = run_state.seed_state("develop-issue-101", "develop", "issue-101")
        state["artifacts"] = {"spec": "spec.md"}
        run_state.write_state(rdir, state)
        (rdir / "spec.md").write_text("# spec\n")
        assert _main(["specs-index", "--rebuild"]) == 0
        assert _main(["commit"]) == 0
        assert (
            f"git.example.com/org/repo/specs/{_today()}-develop-issue-101--spec.md"
            in self._origin_files(clone)
        )

    def test_masterplan_sync_entries_pushed_by_work_commit(self, git_work_repo):
        clone = git_work_repo
        rdir = clone / "git.example.com" / "org/repo" / "runs" / "develop-issue-101"
        run_state.write_state(
            rdir, run_state.seed_state("develop-issue-101", "develop", "issue-101")
        )
        bundle = rdir / "masterplan" / "mp-a"
        bundle.mkdir(parents=True)
        (bundle / "spec.md").write_text("mp-a:spec.md\n")
        # `work commit` runs the masterplan sync itself and must then stage
        # the index entries the sync just wrote.
        assert _main(["commit"]) == 0
        assert (
            f"git.example.com/org/repo/specs/{_today()}-develop-issue-101--spec.md"
            in self._origin_files(clone)
        )


# ===========================================================================
# Issue #353: the specs area is form-routed and its links reach one deeper
# ===========================================================================
#
# Two things change and nothing else does. The AREA is `specs/` in a legacy
# tree and `_specs/` under the registry-resolved project dir in a schema-form
# one (D20), and the TARGET reaches into a session dir
# (`../_runs/<slug>/<session-id>/…`) rather than a run dir
# (`../runs/<dir>/…`), because in schema form the record lives one level
# deeper (D22). Entry naming, the reserved `--` separator, the spec-class
# predicate and the fail-soft contract are untouched — which is why the
# legacy classes above are unchanged and the oracles below are the verbatim
# pre-routing implementations.


def _reference_specs_dir():
    """`specs_dir()` BEFORE form routing (specs_index.py:75-80 at the time),
    with the pre-dual-read `_work_repo_base()` inlined so the oracle depends
    on nothing this task touched."""
    host = os.environ.get("LMER_REPO_HOST")
    project = os.environ.get("LMER_REPO_PROJECT")
    if not host or not project:
        return None
    root = Path(os.environ.get("LMER_WORK_REPO_PATH", "/work"))
    return root / host / project / "specs"


def _reference_specs_rel_path():
    """`specs_rel_path()` verbatim (specs_index.py:83-89 at the time) — the
    hand-built `{host}/{project}/specs` f-string this task removed."""
    host = os.environ.get("LMER_REPO_HOST")
    project = os.environ.get("LMER_REPO_PROJECT")
    if not host or not project:
        return None
    return f"{host}/{project}/specs"


def _reference_repoint_run_dir_entries(old_dirname, new_dirname, new_label):
    """`repoint_run_dir_entries()` verbatim (specs_index.py:186-268 at the
    time): the one-level `../runs/<dirname>/` prefix rewrite, with the base
    inlined. Entry naming helpers are the shipped ones — this task does not
    touch them, and the oracle would drift from the suite if it copied them."""
    try:
        base = _reference_specs_dir().parent
        if old_dirname == new_dirname:
            return
        sdir = base / "specs"
        if not sdir.is_dir():
            return
        old_prefix = f"../runs/{old_dirname}/"
        for entry in list(sdir.iterdir()):
            if not entry.is_symlink():
                continue
            target = os.readlink(entry).replace(os.sep, "/")
            if not target.startswith(old_prefix):
                continue
            new_target = base / "runs" / new_dirname / target[len(old_prefix):]
            m = re.match(r"^(\d{4}-\d{2}-\d{2})-(.+)$", entry.name)
            if not m:
                continue
            when = datetime.strptime(
                m.group(1), specs_index._DATE_PREFIX
            ).replace(tzinfo=timezone.utc)
            rest = m.group(2)
            basename = None
            safe_candidates = [
                specs_index._safe_label(str(c))
                for c in (old_dirname, new_label)
                if str(c)
            ]
            if "--" in rest:
                label_part, _, base_part = rest.partition("--")
                if label_part in safe_candidates and base_part:
                    basename = base_part
            if not basename:
                for safe in safe_candidates:
                    if rest.startswith(f"{safe}-") and not rest.startswith(f"{safe}--"):
                        basename = rest[len(safe) + 1:]
                        break
            if not basename:
                canonical_name = Path(target).name
                basename = (
                    canonical_name if rest.endswith(f"-{canonical_name}") else rest
                )
            entry.unlink()
            specs_index.upsert_spec_link(
                new_target, new_label, when=when, alias=basename
            )
    except Exception as exc:
        print(f"⚠️  specs index re-point skipped: {exc}")


def _reference_rebuild():
    """`rebuild()` verbatim (specs_index.py:302-345 at the time): its own
    one-level descent of `<base>/runs/`, with the base inlined."""
    base = _reference_specs_dir().parent
    sdir = base / "specs"
    if sdir.is_dir():
        for entry in specs_index.list_entries():
            try:
                entry.unlink()
            except OSError as exc:
                print(f"⚠️  specs index: could not remove {entry.name}: {exc}")
    created = []
    runs = base / "runs"
    try:
        run_dirs = (
            sorted(p for p in runs.iterdir() if p.is_dir()) if runs.is_dir() else []
        )
    except OSError:
        run_dirs = []
    for rdir in run_dirs:
        if rdir.name.startswith(".") or rdir.name == run_state.ARCHIVE_DIR:
            continue
        try:
            state = run_state._read_sibling_state(rdir)
            label = specs_index.run_label(rdir, state)
            names = {str(v) for v in ((state or {}).get("artifacts") or {}).values()}
            names |= {p.name for p in rdir.iterdir() if not p.is_dir()}
            for name in sorted(n for n in names if specs_index.is_spec_artifact(n)):
                path = rdir / name
                if not path.is_file():
                    continue
                canonical = path.resolve() if path.is_symlink() else path
                link = specs_index.upsert_spec_link(
                    canonical, label,
                    when=specs_index._registration_when(rdir, name, canonical),
                    alias=name,
                )
                if link is not None:
                    created.append(link)
        except Exception as exc:
            print(f"⚠️  specs index: skipped {rdir.name}: {exc}")
    return created


#: The launch contexts the legacy oracle has to keep answering identically —
#: the "no run context" pairs and the awkward spellings a launch produces
#: (the same matrix tests/test_work_repo_schema_paths.py uses).
_LEGACY_CONTEXTS = [
    (None, None),
    ("git.example.com", None),
    (None, "group/proj"),
    ("", "group/proj"),
    ("git.example.com", ""),
    ("git.example.com", "group/proj"),
    ("git.example.com", "group/sub/proj"),
    ("git.example.com:8443", "group/proj"),
    ("localhost", "proj"),
]


def _set_context(monkeypatch, root, *, host=None, project=None, session=None,
                 taskdef="develop", target="issue-101"):
    """Point the resolver at *root* with the launch context a session has."""
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(root))
    for name, value in (
        ("LMER_REPO_HOST", host),
        ("LMER_REPO_PROJECT", project),
        ("LMER_SESSION_ID", session),
        ("LMER_TASK", taskdef),
        ("LMER_TASK_TARGET", target),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def _launch(monkeypatch, tree, session=None):
    """The launch context for a fixture tree's registered project."""
    host, path = PRIMARY_REPO.split("/", 1)
    _set_context(
        monkeypatch, tree.root, host=host, project=path, session=session,
        target=f"https://{PRIMARY_REPO}/-/issues/12",
    )


def _empty_schema_tree(monkeypatch, tmp_path, session=SID_A, target="issue-101"):
    """A schema tree whose registered project has no records or index yet."""
    root = tmp_path / "plexus-empty"
    root.mkdir()
    (root / "_lmer.yaml").write_text(
        f"schema: 1\nname: example\nprojects:\n  {PROJECT}:\n"
        f"    repo: {PRIMARY_REPO}\n",
        encoding="utf-8",
    )
    host, path = PRIMARY_REPO.split("/", 1)
    _set_context(
        monkeypatch, root, host=host, project=path, session=session, target=target
    )
    return root


def _entry_map(sdir):
    """`{entry name: raw symlink target}` — what byte-identical means here."""
    if not sdir.is_dir():
        return {}
    return {
        p.name: os.readlink(p).replace(os.sep, "/")
        for p in sorted(sdir.iterdir())
        if p.is_symlink()
    }


def _record(rdir, slug, *, name=None, spec="# spec\n", **fields):
    """A minimal run record with a spec-class artifact registered."""
    rdir.mkdir(parents=True, exist_ok=True)
    state = run_state.seed_state(slug, "develop", "issue-101")
    state["name"] = name
    state["artifacts"] = {"spec": "spec.md"}
    state.update(fields)
    run_state.write_state(rdir, state)
    (rdir / "spec.md").write_text(spec)
    return state


class TestTheSpecsAreaRoutesByForm:
    """`specs/` legacy, `_specs/` schema — both off the ONE resolved base,
    and the staging path is that base made relative to the work-repo root
    rather than a hand-built `{host}/{project}/specs` f-string."""

    @pytest.mark.parametrize("host,project", _LEGACY_CONTEXTS)
    def test_specs_dir_matches_the_pre_routing_implementation(
        self, monkeypatch, tmp_path, host, project
    ):
        _set_context(monkeypatch, tmp_path, host=host, project=project)
        assert specs_index.specs_dir() == _reference_specs_dir()

    @pytest.mark.parametrize("host,project", _LEGACY_CONTEXTS)
    def test_specs_rel_path_matches_the_pre_routing_implementation(
        self, monkeypatch, tmp_path, host, project
    ):
        _set_context(monkeypatch, tmp_path, host=host, project=project)
        assert specs_index.specs_rel_path() == _reference_specs_rel_path()

    def test_the_legacy_fixture_tree_resolves_at_its_bare_names(
        self, monkeypatch, tmp_path
    ):
        tree = build_legacy_plexus(tmp_path / "plexus-legacy")
        _launch(monkeypatch, tree)
        host, path = PRIMARY_REPO.split("/", 1)
        assert specs_index.specs_dir() == tree.specs_dir
        assert specs_index.specs_rel_path() == f"{host}/{path}/specs"

    def test_a_registry_hit_resolves_the_short_name_tree(
        self, monkeypatch, tmp_path
    ):
        tree = build_schema_plexus(tmp_path / "plexus-schema")
        _launch(monkeypatch, tree)
        host, path = PRIMARY_REPO.split("/", 1)
        assert specs_index.specs_dir() == tree.specs_dir
        assert specs_index.specs_dir() == tree.root / PROJECT / "_specs"
        assert specs_index.specs_rel_path() == f"{PROJECT}/_specs"
        # The bypass this task removed: the f-string could only ever have
        # named the launch's {host}/{project}, which is not where the work is.
        assert specs_index.specs_rel_path() != f"{host}/{path}/specs"

    def test_it_is_the_area_helper_the_root_resolver_owns(
        self, monkeypatch, tmp_path
    ):
        """One mapping, in one module: `specs_dir()` IS `project_area()`."""
        tree = build_schema_plexus(tmp_path / "plexus-schema")
        _launch(monkeypatch, tree)
        assert specs_index.specs_dir() == utils.project_area("specs", "_specs")

    def test_an_unregistered_repo_gets_the_buckets_schema_areas(
        self, monkeypatch, tmp_path
    ):
        """D9 + SCHEMA.md "Host buckets": the form of the TREE decides the
        names, so a bucket in a schema tree carries `_specs/` too."""
        tree = build_schema_plexus(tmp_path / "plexus-schema")
        _set_context(
            monkeypatch, tree.root, host=BUCKET_HOST, project=BUCKET_PROJECT
        )
        assert specs_index.specs_dir() == tree.bucket_dir / "_specs"
        assert specs_index.specs_rel_path() == \
            f"{BUCKET_HOST}/{BUCKET_PROJECT}/_specs"

    @pytest.mark.parametrize("form", ["schema", "legacy"])
    def test_the_staging_path_addresses_the_dir_it_names(
        self, monkeypatch, tmp_path, form
    ):
        """The invariant that makes the string worth staging: root + rel is
        the directory `specs_dir()` resolves, in either form."""
        tree = (
            build_schema_plexus(tmp_path / "plexus-schema")
            if form == "schema"
            else build_legacy_plexus(tmp_path / "plexus-legacy")
        )
        _launch(monkeypatch, tree)
        rel = specs_index.specs_rel_path()
        assert rel is not None
        assert utils.work_repo_root() / rel == specs_index.specs_dir()

    def test_a_broken_manifest_refuses_rather_than_demoting(
        self, monkeypatch, tmp_path, capsys
    ):
        """D20: no fallback to `{host}/{project}/specs/` — an entry written
        at the legacy names inside a schema tree is the split tree the
        validator flags, and staging that path would commit it."""
        tree = build_schema_plexus(tmp_path / "plexus-schema")
        tree.manifest.write_text(
            "schema: 1\nname: example\nprojects: [oops\n", encoding="utf-8"
        )
        _launch(monkeypatch, tree)
        assert specs_index.specs_dir() is None
        assert specs_index.specs_rel_path() is None
        assert specs_index.list_entries() == []
        assert str(tree.manifest) in capsys.readouterr().err


class TestEntryTargetsReachIntoTheSessionDir:
    """A schema-form entry links `../_runs/<slug>/<session-dir>/…` — one
    level deeper than a legacy `../runs/<dir>/…` — and the link still has to
    satisfy the ERROR-tier shape rules the validator enforces on it."""

    def test_the_target_carries_the_group_and_the_session(
        self, monkeypatch, tmp_path
    ):
        root = _empty_schema_tree(monkeypatch, tmp_path)
        rdir, state = run_state.ensure_run("develop-issue-101")
        (rdir / "spec.md").write_text("# spec\n")
        link = specs_index.upsert_spec_link(rdir / "spec.md", "develop-issue-101")
        assert link.parent == root / PROJECT / "_specs"
        assert os.readlink(link) == (
            f"../_runs/develop-issue-101/{SID_A}/spec.md"
        )
        assert link.read_text() == "# spec\n"  # resolves — not dangling

    def test_a_named_session_dir_round_trips(self, monkeypatch, tmp_path):
        """The `--<name>` suffix a freeze leaves on a session dir is legal
        (docs/SCHEMA.md) and has to survive into the target verbatim."""
        _empty_schema_tree(monkeypatch, tmp_path)
        rdir, state = run_state.ensure_run("develop-issue-101")
        state["name"] = "auth-refactor"
        rdir, _ = run_state.freeze_run_dir(rdir, state)
        (rdir / "spec.md").write_text("# spec\n")
        link = specs_index.upsert_spec_link(rdir / "spec.md", "auth-refactor")
        assert os.readlink(link) == (
            f"../_runs/develop-issue-101/{SID_A}--auth-refactor/spec.md"
        )
        assert link.read_text() == "# spec\n"

    def test_a_bundle_spec_targets_the_bundle_file(self, monkeypatch, tmp_path):
        # The claim is that the entry points at the BUNDLE FILE rather than at
        # the record-root convenience symlink. The bundle is flat here because
        # D12/T26 retired the `<mp-slug>/` level in schema form: a session dir
        # IS the fragment, so it holds at most one bundle.
        root = _empty_schema_tree(monkeypatch, tmp_path)
        rdir, state = run_state.ensure_run("develop-issue-101")
        bundle = rdir / "masterplan"
        bundle.mkdir(parents=True)
        (bundle / "spec.md").write_text("mp-a\n")
        run_state.sync_masterplan_artifacts(rdir)
        entries = specs_index.list_entries()
        assert [p.parent for p in entries] == [root / PROJECT / "_specs"]
        assert [os.readlink(p) for p in entries] == [
            f"../_runs/develop-issue-101/{SID_A}/masterplan/spec.md"
        ]

    @pytest.mark.parametrize("named", [False, True])
    def test_the_validator_accepts_every_link_this_module_writes(
        self, monkeypatch, tmp_path, named
    ):
        """`index.link-not-relative`, `index.link-escapes` and
        `index.dangling-link` are ERROR-tier shape rules in every form, so a
        target written here is checked against the shipped checker rather
        than against this test's idea of a relative path."""
        root = _empty_schema_tree(monkeypatch, tmp_path)
        rdir, state = run_state.ensure_run("develop-issue-101")
        if named:
            state["name"] = "auth-refactor"
            rdir, _ = run_state.freeze_run_dir(rdir, state)
        (rdir / "spec.md").write_text("# spec\n")
        specs_index.upsert_spec_link(rdir / "spec.md", state.get("name") or "s")
        assert specs_index.list_entries()  # something was actually checked
        report = Report(Form.SCHEMA)
        check_index_links(root, report)
        assert [(f.code, f.path) for f in report.findings] == []


class TestTheRepointFollowsTheSessionLevel:
    """The freeze and the re-slug hand this module RUN-AREA-RELATIVE names
    (`<slug>/<sid>`), so the interface stays name-keyed and the name carries
    the group — the prefix it rewrites is `../<area>/<name>/`, derived from
    the resolved run and specs areas so the two can never drift."""

    def test_the_freeze_repoints_into_the_renamed_session_dir(
        self, monkeypatch, tmp_path
    ):
        _empty_schema_tree(monkeypatch, tmp_path)
        rdir, state = run_state.ensure_run("develop-issue-101")
        (rdir / "spec.md").write_text("# spec\n")
        old = specs_index.upsert_spec_link(
            rdir / "spec.md", "develop-issue-101",
            when=datetime(2026, 7, 1, tzinfo=timezone.utc),
        )
        assert os.readlink(old) == f"../_runs/develop-issue-101/{SID_A}/spec.md"

        state["name"] = "auth-refactor"
        moved, old_name = run_state.freeze_run_dir(rdir, state)

        assert old_name == f"develop-issue-101/{SID_A}"
        entries = specs_index.list_entries()
        # The label is the RECORD's: the name half changes with the freeze,
        # the session half is the same launch on both sides of the rename.
        assert [p.name for p in entries] == [
            f"2026-07-01-auth-refactor-{SID_A}--spec.md"
        ]
        assert os.readlink(entries[0]) == (
            f"../_runs/develop-issue-101/{SID_A}--auth-refactor/spec.md"
        )
        assert entries[0].read_text() == "# spec\n"  # resolves — not dangling

    def test_an_aliased_bundle_entry_keeps_its_alias(self, monkeypatch, tmp_path):
        _empty_schema_tree(monkeypatch, tmp_path)
        rdir, state = run_state.ensure_run("develop-issue-101")
        bundle = rdir / "masterplan" / "mp-a"
        bundle.mkdir(parents=True)
        (bundle / "spec.md").write_text("mp-a\n")
        specs_index.upsert_spec_link(
            bundle / "spec.md", "develop-issue-101",
            when=datetime(2026, 7, 2, tzinfo=timezone.utc),
            alias="mp-a-spec.md",
        )
        state["name"] = "auth-refactor"
        run_state.freeze_run_dir(rdir, state)
        entries = specs_index.list_entries()
        assert [p.name for p in entries] == [
            f"2026-07-02-auth-refactor-{SID_A}--mp-a-spec.md"
        ]
        assert os.readlink(entries[0]) == (
            f"../_runs/develop-issue-101/{SID_A}--auth-refactor"
            "/masterplan/mp-a/spec.md"
        )

    def test_the_slug_label_is_still_a_candidate_after_the_move(
        self, monkeypatch, tmp_path
    ):
        """The entry was created under the run's LABEL (the slug), while the
        old name is now `<slug>/<sid>` — so the label candidates come from
        the name's components, not from the whole string."""
        _empty_schema_tree(monkeypatch, tmp_path)
        rdir, state = run_state.ensure_run("develop-issue-101")
        (rdir / "b-spec.md").write_text("# spec\n")
        specs_index.upsert_spec_link(
            rdir / "b-spec.md", "develop-issue-101",
            when=datetime(2026, 7, 1, tzinfo=timezone.utc),
        )
        state["name"] = "auth-refactor"
        run_state.freeze_run_dir(rdir, state)
        names = [p.name for p in specs_index.list_entries()]
        assert names == [f"2026-07-01-auth-refactor-{SID_A}--b-spec.md"]

    def test_the_reslug_repoints_into_the_new_group(self, monkeypatch, tmp_path):
        """A re-slug moves the SESSION sideways into the group its new
        identity addresses, so the group level of the target changes and the
        session level does not."""
        _empty_schema_tree(monkeypatch, tmp_path, target="repo")
        rdir, state = run_state.ensure_run("release-repo")
        (rdir / "spec.md").write_text("# spec\n")
        specs_index.upsert_spec_link(
            rdir / "spec.md", "release-repo",
            when=datetime(2026, 7, 3, tzinfo=timezone.utc),
        )
        moved, state, old_name = run_state.reslug_run(
            rdir, state, "release-repo-v0.6.0"
        )
        assert old_name == f"release-repo/{SID_A}"
        entries = specs_index.list_entries()
        assert [p.name for p in entries] == [
            f"2026-07-03-release-repo-v0.6.0-{SID_A}--spec.md"
        ]
        assert os.readlink(entries[0]) == (
            f"../_runs/release-repo-v0.6.0/{SID_A}/spec.md"
        )
        assert entries[0].read_text() == "# spec\n"

    def test_a_sibling_sessions_entries_are_untouched(self, monkeypatch, tmp_path):
        """Two launches on one target are two records; re-pointing one must
        not touch the other's link, whose target shares the group prefix."""
        root = _empty_schema_tree(monkeypatch, tmp_path)
        group = root / PROJECT / "_runs" / "develop-issue-101"
        _record(group / SID_B, "develop-issue-101", name="first-look")
        other = specs_index.upsert_spec_link(
            group / SID_B / "spec.md", "first-look"
        )
        rdir, state = run_state.ensure_run("develop-issue-101")
        (rdir / "spec.md").write_text("mine\n")
        specs_index.upsert_spec_link(rdir / "spec.md", "develop-issue-101")

        state["name"] = "auth-refactor"
        run_state.freeze_run_dir(rdir, state)

        # Mine moved (so the re-point actually ran over this index)...
        assert f"{_today()}-auth-refactor-{SID_A}--spec.md" in _entry_map(
            specs_index.specs_dir()
        )
        # ...and the sibling's link, whose target shares the group prefix,
        # still addresses the session it was registered for.
        assert os.readlink(other) == (
            f"../_runs/develop-issue-101/{SID_B}/spec.md"
        )
        assert other.read_text() == "# spec\n"

    def test_index_trouble_never_fails_the_freeze(
        self, monkeypatch, tmp_path, capsys
    ):
        _empty_schema_tree(monkeypatch, tmp_path)
        rdir, state = run_state.ensure_run("develop-issue-101")
        (rdir / "spec.md").write_text("x")
        specs_index.upsert_spec_link(rdir / "spec.md", "develop-issue-101")
        state["name"] = "auth-refactor"
        with patch.object(
            specs_index, "repoint_run_dir_entries",
            side_effect=RuntimeError("boom"),
        ):
            moved, old_name = run_state.freeze_run_dir(rdir, state)
        assert moved.name == f"{SID_A}--auth-refactor"
        assert "specs index re-point failed" in capsys.readouterr().out

    #: (entry name, entry target, old name, new name, new label) — the legacy
    #: re-point shapes the oracle has to keep answering identically: the
    #: current format, an aliased bundle entry, a pre-`--` entry, a label the
    #: old dir name is a dash-prefix of, and a target under another run.
    _LEGACY_REPOINT_CASES = [
        ("2026-07-01-develop-issue-101--spec.md",
         "../runs/develop-issue-101/spec.md",
         "develop-issue-101", "develop-issue-101--named", "named"),
        ("2026-07-02-develop-issue-101--mp-a-spec.md",
         "../runs/develop-issue-101/masterplan/mp-a/spec.md",
         "develop-issue-101", "develop-issue-101--named", "named"),
        ("2026-07-01-develop-issue-101-spec.md",
         "../runs/develop-issue-101/spec.md",
         "develop-issue-101", "develop-issue-101--named", "named"),
        ("2026-07-01-develop-issue-101--spec.md",
         "../runs/develop-issue/spec.md",
         "develop-issue", "develop-issue--named", "develop-issue-101"),
        ("2026-07-01-other--spec.md", "../runs/other-run/spec.md",
         "develop-issue-101", "develop-issue-101--named", "named"),
    ]

    @pytest.mark.parametrize(
        "entry,target,old,new,label", _LEGACY_REPOINT_CASES
    )
    def test_legacy_repointing_matches_the_pre_routing_implementation(
        self, monkeypatch, tmp_path, entry, target, old, new, label
    ):
        """Byte-identical in a legacy tree, checked against the verbatim
        one-level implementation rather than against a hand-written map."""
        maps = {}
        for which, runner in (
            ("shipped", specs_index.repoint_run_dir_entries),
            ("reference", _reference_repoint_run_dir_entries),
        ):
            root = tmp_path / which
            project = root / "git.example.com" / "org/repo"
            # Post-rename layout: the re-point runs AFTER the dir move.
            _record(project / "runs" / new, "develop-issue-101", name=label)
            (project / "runs" / new / "masterplan" / "mp-a").mkdir(parents=True)
            (project / "runs" / new / "masterplan" / "mp-a" / "spec.md").write_text("b")
            _record(project / "runs" / "other-run", "other")
            sdir = project / "specs"
            sdir.mkdir(parents=True)
            (sdir / entry).symlink_to(target)
            _set_context(
                monkeypatch, root, host="git.example.com", project="org/repo"
            )
            runner(old, new, label)
            maps[which] = _entry_map(sdir)
        assert maps["shipped"] == maps["reference"]


class TestRebuildWalksTheSessionLevel:
    """`rebuild()` re-indexes every RECORD. In schema form that is each
    `_runs/<slug>/<session-id>/`, one level deeper, with the grouping level
    itself skipped — and it asks the kernel's shared walk for the set rather
    than growing a descent of its own."""

    def test_it_indexes_each_session_and_skips_the_group(
        self, monkeypatch, tmp_path
    ):
        root = _empty_schema_tree(monkeypatch, tmp_path)
        group = root / PROJECT / "_runs" / "develop-issue-101"
        _record(group / SID_A, "develop-issue-101", name="first-look",
                spec="# first\n")
        _record(group / f"{SID_B}--second-look", "develop-issue-101",
                name="second-look", spec="# second\n")
        assert specs_index.rebuild()
        assert _entry_map(specs_index.specs_dir()) == {
            f"{_today()}-first-look-{SID_A}--spec.md":
                f"../_runs/develop-issue-101/{SID_A}/spec.md",
            f"{_today()}-second-look-{SID_B}--spec.md":
                f"../_runs/develop-issue-101/{SID_B}--second-look/spec.md",
        }

    def test_a_spec_at_the_grouping_level_is_never_indexed(
        self, monkeypatch, tmp_path
    ):
        """Nothing is written at `_runs/<slug>/` (D22), so anything found
        there is not a record — the walk must not read it as one."""
        root = _empty_schema_tree(monkeypatch, tmp_path)
        group = root / PROJECT / "_runs" / "develop-issue-101"
        _record(group / SID_A, "develop-issue-101", name="first-look")
        (group / "spec.md").write_text("# stray\n")
        specs_index.rebuild()
        assert [os.readlink(p) for p in specs_index.list_entries()] == [
            f"../_runs/develop-issue-101/{SID_A}/spec.md"
        ]

    def test_it_uses_the_kernels_shared_record_walk(self, monkeypatch, tmp_path):
        """One walk, not a third one: the set rebuilt is exactly the set
        `run_state.iter_record_dirs()` yields (the resolvers' own)."""
        root = _empty_schema_tree(monkeypatch, tmp_path)
        group = root / PROJECT / "_runs" / "develop-issue-101"
        _record(group / SID_A, "develop-issue-101", name="first-look")
        _record(group / SID_B, "develop-issue-101", name="second-look")
        seen = []
        real = run_state.iter_record_dirs

        def _spy(base=None):
            records = real(base)
            seen.append(list(records))
            return records

        monkeypatch.setattr(run_state, "iter_record_dirs", _spy)
        specs_index.rebuild()
        assert seen and seen[0] == real()

    def test_a_bundle_spec_is_rebuilt_at_its_canonical_target(
        self, monkeypatch, tmp_path
    ):
        root = _empty_schema_tree(monkeypatch, tmp_path)
        rdir = root / PROJECT / "_runs" / "develop-issue-101" / SID_A
        _record(rdir, "develop-issue-101", name="first-look")
        (rdir / "spec.md").unlink()
        bundle = rdir / "masterplan" / "mp-a"
        bundle.mkdir(parents=True)
        (bundle / "spec.md").write_text("mp-a\n")
        (rdir / "spec.md").symlink_to("masterplan/mp-a/spec.md")
        specs_index.rebuild()
        assert [os.readlink(p) for p in specs_index.list_entries()] == [
            f"../_runs/develop-issue-101/{SID_A}/masterplan/mp-a/spec.md"
        ]

    def test_dot_dirs_and_the_archive_are_still_skipped(
        self, monkeypatch, tmp_path
    ):
        root = _empty_schema_tree(monkeypatch, tmp_path)
        runs = root / PROJECT / "_runs"
        _record(runs / f"{run_state.TMP_DIR_PREFIX}s-1-abc" / SID_A, "seed")
        _record(runs / run_state.ARCHIVE_DIR / "old" / SID_A, "archived")
        _record(runs / "develop-issue-101" / f".{SID_A}", "hidden")
        # A real record beside them, so the skips are proved against a walk
        # that reached the run area rather than against one that found nothing.
        _record(runs / "develop-issue-101" / SID_B, "develop-issue-101",
                name="first-look")
        assert len(specs_index.rebuild()) == 1
        assert [os.readlink(p) for p in specs_index.list_entries()] == [
            f"../_runs/develop-issue-101/{SID_B}/spec.md"
        ]

    def test_rebuild_drops_stale_entries(self, monkeypatch, tmp_path):
        root = _empty_schema_tree(monkeypatch, tmp_path)
        sdir = root / PROJECT / "_specs"
        sdir.mkdir(parents=True)
        (sdir / "2026-01-01-gone--spec.md").symlink_to("../_runs/gone/s/spec.md")
        assert specs_index.rebuild() == []
        assert specs_index.list_entries() == []

    #: Legacy run-area shapes rebuild has to keep indexing identically.
    def _populate_legacy_area(self, project):
        _record(project / "runs" / "develop-issue-101", "develop-issue-101")
        _record(project / "runs" / "develop-issue-102--named",
                "develop-issue-102", name="named")
        bundle = project / "runs" / "develop-issue-103" / "masterplan" / "mp-a"
        _record(project / "runs" / "develop-issue-103", "develop-issue-103")
        (project / "runs" / "develop-issue-103" / "spec.md").unlink()
        bundle.mkdir(parents=True)
        (bundle / "spec.md").write_text("mp-a\n")
        (project / "runs" / "develop-issue-103" / "spec.md").symlink_to(
            "masterplan/mp-a/spec.md"
        )
        _record(project / "runs" / f"{run_state.TMP_DIR_PREFIX}s-1-abc", "seed")
        _record(project / "runs" / run_state.ARCHIVE_DIR / "old", "archived")
        # A run whose spec.md is not registered in state.artifacts: found at
        # the record root instead. plan.md is not spec-class.
        plain = project / "runs" / "develop-issue-104"
        _record(plain, "develop-issue-104", artifacts={})
        (plain / "plan.md").write_text("# plan\n")

    def test_a_stateless_directory_under_runs_is_not_a_record(
        self, monkeypatch, tmp_path
    ):
        """THE one deliberate legacy behaviour change (reported, not hidden).

        The pre-#353 rebuild grew its own descent and indexed every directly
        nested directory under `runs/`, state file or not, labelling a
        stateless one by its directory name. The shared walk it now asks
        (`run_state._record_scan`) reads "no state file" as "not a record" —
        which is FORCED in schema form, where a stateless directory under the
        run area is exactly the `_runs/<slug>/` grouping level D22 says
        nothing is ever written at. Applying a different rule per form is the
        drift the shared walk exists to prevent, so a `runs/<dir>/` holding a
        spec and no state is no longer indexed in either form.
        """
        root = tmp_path / "legacy"
        project = root / "git.example.com" / "org/repo"
        _record(project / "runs" / "develop-issue-101", "develop-issue-101")
        husk = project / "runs" / "develop-issue-102"
        husk.mkdir(parents=True)
        (husk / "spec.md").write_text("# no state here\n")
        _set_context(monkeypatch, root, host="git.example.com", project="org/repo")

        specs_index.rebuild()
        shipped = _entry_map(project / "specs")
        _reference_rebuild()
        reference = _entry_map(project / "specs")

        assert shipped == {
            f"{_today()}-develop-issue-101--spec.md":
                "../runs/develop-issue-101/spec.md",
        }
        assert set(reference) - set(shipped) == {
            f"{_today()}-develop-issue-102--spec.md",
        }

    def test_legacy_rebuild_matches_the_pre_routing_implementation(
        self, monkeypatch, tmp_path
    ):
        maps = {}
        for which, runner in (
            ("shipped", specs_index.rebuild),
            ("reference", _reference_rebuild),
        ):
            root = tmp_path / which
            project = root / "git.example.com" / "org/repo"
            self._populate_legacy_area(project)
            _set_context(
                monkeypatch, root, host="git.example.com", project="org/repo"
            )
            runner()
            maps[which] = _entry_map(project / "specs")
        assert maps["shipped"] == maps["reference"]
        assert maps["shipped"]  # the oracle actually indexed something


class TestTheEntryKeyIsTheRecordNotTheRun:
    """One entry per RECORD, not per run (issue #353 must-fix D).

    The label an entry is named under used to be the run's identity alone —
    `state.name`, slug fallback — and in schema form two launches of one
    target share that exactly (D22: the SESSION is the unit of record, so one
    group holds several complete, independent records). The second launch's
    upsert therefore re-pointed the first launch's entry (same day) or
    unlinked it as stale (any other day), and that record's spec left the
    index with nothing raised and nothing logged. The session id is what is
    unique by construction, so a schema-form label carries it.

    The cost, deliberate and stated on the MR: entry names get long —
    `2026-08-25-develop-issue-101-20260824-101500-1-11111--spec.md` where the
    old name was `2026-08-25-develop-issue-101--spec.md`.
    """

    def _launch(self, root, session, *, name=None, when=None, alias=None,
                slug="develop-issue-101"):
        """One launch's record, indexed the way every caller indexes one:
        under `run_label`, which is the label a caller has to hand."""
        rdir = root / PROJECT / "_runs" / slug / session
        state = _record(rdir, slug, name=name, spec=f"# spec of {session}\n")
        spec = rdir / (alias or "spec.md")
        if alias:
            spec.write_text(f"# spec of {session}\n")
        link = specs_index.upsert_spec_link(
            spec, specs_index.run_label(rdir, state), when=when
        )
        return rdir, state, link

    def test_two_unnamed_launches_each_keep_their_own_entry(
        self, monkeypatch, tmp_path
    ):
        """The defect, at its smallest: two launches, no names, one target.
        Both specs exist on disk, so both have to be reachable from the flat
        directory that IS the index."""
        root = _empty_schema_tree(monkeypatch, tmp_path)
        _first, _s1, one = self._launch(root, SID_A)
        _second, _s2, two = self._launch(root, SID_B)

        assert _entry_map(specs_index.specs_dir()) == {
            f"{_today()}-develop-issue-101-{SID_A}--spec.md":
                f"../_runs/develop-issue-101/{SID_A}/spec.md",
            f"{_today()}-develop-issue-101-{SID_B}--spec.md":
                f"../_runs/develop-issue-101/{SID_B}/spec.md",
        }
        assert one.read_text() == f"# spec of {SID_A}\n"
        assert two.read_text() == f"# spec of {SID_B}\n"

    def test_a_later_launch_never_unlinks_an_earlier_launchs_entry(
        self, monkeypatch, tmp_path
    ):
        """The silent-deletion half: a same-pair entry under ANOTHER date is
        removed by the stale cleanup, so a launch that indexed its spec
        yesterday lost it to today's launch without a word."""
        root = _empty_schema_tree(monkeypatch, tmp_path)
        _first, _s1, one = self._launch(
            root, SID_A, when=datetime(2026, 7, 1, tzinfo=timezone.utc)
        )
        _second, _s2, two = self._launch(root, SID_B)

        assert one.is_symlink()
        assert one.read_text() == f"# spec of {SID_A}\n"
        assert sorted(p.name for p in specs_index.list_entries()) == sorted(
            [f"2026-07-01-develop-issue-101-{SID_A}--spec.md",
             f"{_today()}-develop-issue-101-{SID_B}--spec.md"]
        )
        assert two.read_text() == f"# spec of {SID_B}\n"

    def test_two_launches_given_the_same_name_stay_distinct(
        self, monkeypatch, tmp_path
    ):
        """Naming a launch does not make it unique: `work name` takes any
        string, and two launches can be handed the same one. The session id
        is the part nobody chooses."""
        root = _empty_schema_tree(monkeypatch, tmp_path)
        _first, _s1, one = self._launch(root, SID_A, name="auth-refactor")
        _second, _s2, two = self._launch(root, SID_B, name="auth-refactor")

        assert [p.name for p in specs_index.list_entries()] == [
            f"{_today()}-auth-refactor-{SID_A}--spec.md",
            f"{_today()}-auth-refactor-{SID_B}--spec.md",
        ]
        assert one.read_text() == f"# spec of {SID_A}\n"
        assert two.read_text() == f"# spec of {SID_B}\n"

    def test_the_session_id_survives_the_label_sanitiser(
        self, monkeypatch, tmp_path
    ):
        r"""`_safe_label` collapses hyphen runs so a label can never contain
        the reserved `--`; a session id is `\d{8}-\d{6}-\d+-\d+`, which it
        passes through unchanged — so the first post-date `--` is still the
        label/basename boundary."""
        root = _empty_schema_tree(monkeypatch, tmp_path)
        rdir, state, link = self._launch(root, SID_A)

        label = specs_index.run_label(rdir, state)
        assert label == f"develop-issue-101-{SID_A}"
        assert specs_index._safe_label(SID_A) == SID_A
        assert specs_index._safe_label(label) == label  # nothing squashed
        rest = link.name[len("YYYY-MM-DD-"):]
        assert rest.partition("--")[0] == label
        assert rest.partition("--")[2] == "spec.md"

    def test_a_longer_label_never_matches_a_neighbouring_records_entry(
        self, monkeypatch, tmp_path
    ):
        """A fix for silent deletion must not BE one: the stale-cleanup
        pattern is built from the label, so a label that is a dash-prefix of
        a sibling record's label must not sweep the sibling's entry away.
        Both alternatives are escaped and anchored, which is what makes the
        longer label match only its own record."""
        root = _empty_schema_tree(monkeypatch, tmp_path)
        short_sid, long_sid = "20260824-101500-1-1111", "20260824-101500-1-11111"
        _a, _sa, one = self._launch(
            root, short_sid, when=datetime(2026, 7, 1, tzinfo=timezone.utc)
        )
        _b, _sb, two = self._launch(root, long_sid)

        assert one.is_symlink() and two.is_symlink()
        assert one.read_text() == f"# spec of {short_sid}\n"
        assert two.read_text() == f"# spec of {long_sid}\n"
        assert sorted(p.name for p in specs_index.list_entries()) == sorted(
            [f"2026-07-01-develop-issue-101-{short_sid}--spec.md",
             f"{_today()}-develop-issue-101-{long_sid}--spec.md"]
        )

    def test_a_freeze_keeps_both_launches_in_the_index(
        self, monkeypatch, tmp_path
    ):
        """End to end over the freeze rename: an entry registered pre-freeze
        still resolves post-freeze, under the record's new label, with no
        dangling link, no orphan under the old label — and the sibling
        launch's entry untouched beside it."""
        root = _empty_schema_tree(monkeypatch, tmp_path)
        first, state, old = self._launch(
            root, SID_A, when=datetime(2026, 7, 1, tzinfo=timezone.utc)
        )
        _second, _s2, sibling = self._launch(root, SID_B)

        state["name"] = "auth-refactor"
        moved, old_name = run_state.freeze_run_dir(first, state)

        assert moved.name == f"{SID_A}--auth-refactor"
        assert _entry_map(specs_index.specs_dir()) == {
            f"2026-07-01-auth-refactor-{SID_A}--spec.md":
                f"../_runs/develop-issue-101/{SID_A}--auth-refactor/spec.md",
            f"{_today()}-develop-issue-101-{SID_B}--spec.md":
                f"../_runs/develop-issue-101/{SID_B}/spec.md",
        }
        for entry in specs_index.list_entries():
            assert entry.exists(), entry  # resolves — nothing left dangling
        assert not old.is_symlink()  # the pre-freeze label leaves no orphan

    def test_a_freeze_reparses_an_entry_written_under_the_record_label(
        self, monkeypatch, tmp_path
    ):
        """The re-point splits an entry at the first post-date `--` and
        verifies the label half against the labels the record can have been
        indexed under. `<run>-<sid>` has to be one of them: without it the
        legacy single-dash strip below would shear the name mid-session-id
        and re-register the alias as `20260824-101500-1-11111--mp-a-spec.md`.
        """
        root = _empty_schema_tree(monkeypatch, tmp_path)
        rdir, state, _link = self._launch(
            root, SID_A, alias="mp-a-spec.md",
            when=datetime(2026, 7, 2, tzinfo=timezone.utc),
        )

        state["name"] = "auth-refactor"
        run_state.freeze_run_dir(rdir, state)

        assert _entry_map(specs_index.specs_dir()) == {
            f"2026-07-02-auth-refactor-{SID_A}--mp-a-spec.md":
                f"../_runs/develop-issue-101/{SID_A}--auth-refactor/mp-a-spec.md",
        }

    def test_the_rebuild_reproduces_the_incremental_entry_names(
        self, monkeypatch, tmp_path
    ):
        """The two writers of entry names have to agree: `--rebuild` drops
        the lot and re-indexes from the run area, so a name it cannot
        reproduce is a name the next rebuild would silently rename."""
        root = _empty_schema_tree(monkeypatch, tmp_path)
        self._launch(root, SID_A)
        self._launch(root, SID_B)
        incremental = _entry_map(specs_index.specs_dir())
        assert len(incremental) == 2  # one per RECORD, not one per run

        specs_index.rebuild()

        assert _entry_map(specs_index.specs_dir()) == incremental

    def test_a_legacy_tree_keeps_the_run_label_it_always_had(
        self, monkeypatch, tmp_path
    ):
        """A legacy tree has one shared run dir per target — nothing to
        disambiguate — so its entry names stay byte-identical, from both
        writers."""
        root = tmp_path / "legacy"
        project = root / "git.example.com" / "org/repo"
        rdir = project / "runs" / "develop-issue-101"
        state = _record(rdir, "develop-issue-101")
        _set_context(monkeypatch, root, host="git.example.com", project="org/repo")

        assert specs_index.run_label(rdir, state) == "develop-issue-101"
        link = specs_index.upsert_spec_link(
            rdir / "spec.md", specs_index.run_label(rdir, state)
        )
        assert link.name == f"{_today()}-develop-issue-101--spec.md"

        specs_index.rebuild()
        assert _entry_map(project / "specs") == {
            f"{_today()}-develop-issue-101--spec.md":
                "../runs/develop-issue-101/spec.md",
        }

    def test_an_unmigrated_record_in_a_schema_tree_keeps_the_run_label(
        self, monkeypatch, tmp_path
    ):
        """The question asked is the RECORD's shape, not the tree's: a record
        sitting directly in the run area (a legacy tree, or a schema one
        adopted but not yet migrated) is the target's only record, and its
        directory name carries no session id to add."""
        root = _empty_schema_tree(monkeypatch, tmp_path)
        rdir = root / PROJECT / "_runs" / "develop-issue-101"
        state = _record(rdir, "develop-issue-101", name="auth-refactor")

        assert specs_index.run_label(rdir, state) == "auth-refactor"
        link = specs_index.upsert_spec_link(
            rdir / "spec.md", specs_index.run_label(rdir, state)
        )
        assert link.name == f"{_today()}-auth-refactor--spec.md"
