"""Tests for the central specs index (issue #101).

`{host}/{project}/specs/` (sibling of info/ and runs/) holds one dated
relative symlink per spec-class artifact — `YYYY-MM-DD-<run>--<basename>`,
with `--` the reserved label/basename separator (labels never contain one)
— pointing at the spec's canonical location. Maintained by `work artifact`
registration and the masterplan bundle sync (which links the BUNDLE file,
never the run-root symlink); `work specs-index --rebuild` is the backfill
path. Symlinks-only by design (a dir listing IS the index), and always
fail-soft — index trouble never fails a registration.
"""
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from work_repo import cli as work_cli
from work_repo import run_state, specs_index
from tests.conftest import strip_lmer_env


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


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
    def test_later_day_reregistration_replaces_older_entry(
        self, run_env, tmp_path
    ):
        run_env.mkdir(parents=True)
        (run_env / "spec.md").write_text("# spec\n")
        old = specs_index.upsert_spec_link(
            run_env / "spec.md", "issue-101",
            when=datetime(2026, 7, 1, tzinfo=timezone.utc),
        )
        assert old is not None and old.name == "2026-07-01-issue-101--spec.md"
        new = specs_index.upsert_spec_link(run_env / "spec.md", "issue-101")
        assert new.name == f"{_today()}-issue-101--spec.md"
        assert not old.exists()  # one entry per (run, spec file) — no duplicates
        assert [p.name for p in specs_index.list_entries()] == [new.name]

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
        assert new.name == f"{_today()}-issue-101--spec.md"
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
