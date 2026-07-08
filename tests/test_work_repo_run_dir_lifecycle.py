"""Tests for the CLI-owned run-dir lifecycle (issue #87).

Covers the content-keyed resolver (D1), tmp-dir-then-rename seeding and the
pre-execution freeze rename (D2), the `work seed` verb (D3), and the
log/report unification into runs/<slug>/ (D4).
"""
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from work_repo import cli as work_cli
from work_repo import run_state


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("LMER_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def run_env(monkeypatch, tmp_path):
    """Full env trio + task context; returns the runs/ base dir."""
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
    monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
    monkeypatch.setenv("LMER_REPO_PROJECT", "org/repo")
    monkeypatch.setenv("LMER_TASK", "develop")
    monkeypatch.setenv("LMER_TASK_TARGET", "https://git.example.com/org/repo/-/issues/123")
    monkeypatch.setenv("LMER_SESSION_ID", "s-lc-1")
    return tmp_path / "git.example.com" / "org/repo" / "runs"


def _main(argv):
    with patch.object(work_cli.sys, "argv", ["work"] + argv):
        return work_cli.main()


def _seed_at(base: Path, dirname: str, slug: str, name=None) -> Path:
    """Hand-place a run dir (arbitrary dir name) for resolver tests."""
    rdir = base / dirname
    state = run_state.seed_state(slug, "develop", "t")
    if name:
        state["name"] = name
    run_state.write_state(rdir, state)
    return rdir


class TestFindRunDir:
    def test_hit_by_slug_ignores_dir_name(self, run_env):
        rdir = _seed_at(run_env, "totally-unrelated-dir", "develop-issue-123")
        assert run_state.find_run_dir("develop-issue-123") == rdir

    def test_hit_by_name_requires_opt_in(self, run_env):
        rdir = _seed_at(run_env, "develop-issue-123", "develop-issue-123", name="auth-refactor")
        assert run_state.find_run_dir("auth-refactor", match_names=True) == rdir
        assert run_state.find_run_dir("auth-refactor") is None

    def test_slug_match_beats_name_match(self, run_env):
        _seed_at(run_env, "a-dir", "other-slug", name="develop-issue-123")
        by_slug = _seed_at(run_env, "b-dir", "develop-issue-123")
        assert run_state.find_run_dir("develop-issue-123", match_names=True) == by_slug

    def test_session_resolution_ignores_names(self, run_env):
        # A foreign run NAMED like this session's slug must not be adopted:
        # run_dir() resolves by slug only, so the session gets its own
        # creation address, not the foreign run.
        _seed_at(run_env, "chat", "chat", name="develop-issue-123")
        assert run_state.run_dir() == run_env / "develop-issue-123"

    def test_scan_error_resolves_to_no_match(self, run_env, monkeypatch):
        _seed_at(run_env, "develop-issue-123", "develop-issue-123")

        def boom(self):
            raise PermissionError("runs/ unreadable")

        monkeypatch.setattr(type(run_env), "iterdir", boom)
        assert run_state.find_run_dir("develop-issue-123") is None
        # run_dir degrades to the creation address instead of raising.
        assert run_state.run_dir() == run_env / "develop-issue-123"

    def test_miss_returns_none(self, run_env):
        _seed_at(run_env, "develop-issue-123", "develop-issue-123")
        assert run_state.find_run_dir("develop-issue-999") is None

    def test_never_matches_by_directory_name_alone(self, run_env):
        # Dir named like the slug but whose state records another slug.
        _seed_at(run_env, "develop-issue-123", "some-other-slug")
        assert run_state.find_run_dir("develop-issue-123") is None

    def test_skips_archive_dotdirs_and_corrupt_siblings(self, run_env):
        _seed_at(run_env / "archive", "develop-issue-123"[:0] or "develop-issue-123", "develop-issue-123")
        _seed_at(run_env, ".new-s-lc-1-abc", "develop-issue-123")
        corrupt = run_env / "corrupt-run"
        corrupt.mkdir(parents=True)
        (corrupt / "state.yaml").write_text("{not yaml: [", encoding="utf-8")
        assert run_state.find_run_dir("develop-issue-123") is None

    def test_matches_legacy_state_yml_sibling(self, run_env):
        rdir = run_env / "legacy-run"
        rdir.mkdir(parents=True)
        (rdir / "state.yml").write_text(
            yaml.safe_dump({"schema": 1, "slug": "develop-issue-123"}), encoding="utf-8"
        )
        assert run_state.find_run_dir("develop-issue-123") == rdir


class TestRunDirResolution:
    def test_run_dir_follows_rename(self, run_env):
        rdir = _seed_at(run_env, "develop-issue-123", "develop-issue-123", name="lifecycle")
        renamed = rdir.rename(run_env / "develop-issue-123--lifecycle")
        assert run_state.run_dir() == renamed

    def test_run_dir_falls_back_to_creation_address(self, run_env):
        assert run_state.run_dir() == run_env / "develop-issue-123"

    def test_run_rel_path_follows_rename(self, run_env):
        rdir = _seed_at(run_env, "develop-issue-123", "develop-issue-123")
        rdir.rename(run_env / "develop-issue-123--lifecycle")
        assert (
            run_state.run_rel_path()
            == "git.example.com/org/repo/runs/develop-issue-123--lifecycle"
        )

    def test_rel_path_candidates_include_bare_slug(self, run_env):
        rdir = _seed_at(run_env, "develop-issue-123", "develop-issue-123")
        rdir.rename(run_env / "develop-issue-123--lifecycle")
        assert run_state.run_rel_path_candidates() == [
            "git.example.com/org/repo/runs/develop-issue-123--lifecycle",
            "git.example.com/org/repo/runs/develop-issue-123",
        ]

    def test_rel_path_candidates_deduped_when_unrenamed(self, run_env):
        _seed_at(run_env, "develop-issue-123", "develop-issue-123")
        assert run_state.run_rel_path_candidates() == [
            "git.example.com/org/repo/runs/develop-issue-123"
        ]


class TestSeedRunDir:
    def test_seeds_at_final_name_with_no_tmp_leftovers(self, run_env):
        rdir, state = run_state.seed_run_dir("develop-issue-123", "develop", "target-x")
        assert rdir == run_env / "develop-issue-123"
        assert state["slug"] == "develop-issue-123"
        assert state["taskdef"] == "develop"
        assert state["target"] == "target-x"
        assert state["owner"] is None
        assert state["frozen"] is None
        events = run_state.read_events(rdir, last_n=0)
        assert [e["type"] for e in events] == ["run_seeded"]
        leftovers = [p.name for p in run_env.iterdir() if p.name.startswith(".new-")]
        assert leftovers == []

    def test_seed_note_lands_on_run_seeded_event(self, run_env):
        rdir, _ = run_state.seed_run_dir("s1", "develop", "t", note="via work seed")
        events = run_state.read_events(rdir, last_n=0)
        assert events[0]["note"] == "via work seed"

    def test_lost_race_adopts_existing_run(self, run_env):
        existing = _seed_at(run_env, "develop-issue-123", "develop-issue-123")
        existing_state = run_state.load_state(existing)
        rdir, state = run_state.seed_run_dir("develop-issue-123", "develop", "t2")
        assert rdir == existing
        assert state["created"] == existing_state["created"]
        assert state["target"] == "t"  # the existing seed won
        leftovers = [p.name for p in run_env.iterdir() if p.name.startswith(".new-")]
        assert leftovers == []

    def test_no_context_raises(self):
        with pytest.raises(run_state.RunStateError):
            run_state.seed_run_dir("s", "develop", "t")

    def test_non_collision_rename_failure_reports_real_error(self, run_env, monkeypatch):
        # EACCES/ENOSPC/EXDEV must surface as themselves, not as a bogus
        # "already exists" that sends debugging the wrong way.
        def boom(self, target):
            raise PermissionError("read-only fs")

        monkeypatch.setattr(Path, "rename", boom)
        with pytest.raises(run_state.RunStateError, match="read-only fs"):
            run_state.seed_run_dir("s-err", "develop", "t", adopt_existing=False)


class TestPlanningPhaseClassifier:
    @pytest.mark.parametrize(
        "phase",
        [None, "", "spec", "Spec", "planning", "plan-approval", "brainstorm",
         "exploration", "explore", "design", "review", "review-response",
         "REVIEW",
         # shipped-taskdef pre-execution phases: these precede naming, and a
         # premature freeze on an unnamed run forfeits the rename forever
         # (develop records branch-setup/issue-analysis/interview, review
         # records retrieve, before the run is named).
         "branch-setup", "issue-analysis", "interview", "setup",
         "retrieve"],
    )
    def test_planning_family(self, phase):
        assert run_state.is_planning_phase(phase) is True

    @pytest.mark.parametrize(
        "phase",
        ["execution", "execute", "delivery", "implement", "retro", "phase 3",
         "post", "cleanup"],
    )
    def test_execution_family(self, phase):
        assert run_state.is_planning_phase(phase) is False


class TestFreezeRunDir:
    def test_named_run_renames_once(self, run_env):
        rdir = _seed_at(run_env, "develop-issue-123", "develop-issue-123", name="lifecycle")
        state = run_state.load_state(rdir)
        new_rdir, old_name = run_state.freeze_run_dir(rdir, state)
        assert new_rdir == run_env / "develop-issue-123--lifecycle"
        assert old_name == "develop-issue-123"
        assert state["frozen"] is not None
        assert not (run_env / "develop-issue-123").exists()
        events = run_state.read_events(new_rdir, last_n=0)
        assert any(e["type"] == "run_dir_renamed" for e in events)

    def test_unnamed_run_freezes_without_rename(self, run_env):
        rdir = _seed_at(run_env, "develop-issue-123", "develop-issue-123")
        state = run_state.load_state(rdir)
        new_rdir, old_name = run_state.freeze_run_dir(rdir, state)
        assert new_rdir == rdir
        assert old_name is None
        assert state["frozen"] is not None

    def test_already_name_bearing_dir_is_noop(self, run_env):
        rdir = _seed_at(
            run_env, "develop-issue-123--lifecycle", "develop-issue-123", name="lifecycle"
        )
        state = run_state.load_state(rdir)
        new_rdir, old_name = run_state.freeze_run_dir(rdir, state)
        assert new_rdir == rdir
        assert old_name is None

    def test_rename_target_taken_is_skipped(self, run_env, capsys):
        rdir = _seed_at(run_env, "develop-issue-123", "develop-issue-123", name="lifecycle")
        target = run_env / "develop-issue-123--lifecycle"
        target.mkdir(parents=True)
        (target / "occupied.txt").write_text("x", encoding="utf-8")
        state = run_state.load_state(rdir)
        new_rdir, old_name = run_state.freeze_run_dir(rdir, state)
        assert new_rdir == rdir
        assert old_name is None
        assert "rename skipped" in capsys.readouterr().out


class TestCorruptStateRecovery:
    def test_renamed_dir_with_corrupt_state_is_preferred_over_reseed(self, run_env):
        # A renamed run whose state.yaml corrupts can't be content-matched;
        # run_dir() must still prefer it so load_state's backup-and-recover
        # runs there instead of shadow-seeding a duplicate at the bare slug.
        rdir = run_env / "develop-issue-123--lifecycle"
        rdir.mkdir(parents=True)
        (rdir / "state.yaml").write_text("{broken: [", encoding="utf-8")
        assert run_state.run_dir() == rdir

    def test_ensure_run_recovers_corrupt_renamed_dir_in_place(self, run_env):
        # ensure_run must back up the corrupt file and reseed at the
        # already-resolved (renamed) dir WITHIN one call — a retry after the
        # backup would no longer resolve the dir and would fork a duplicate
        # run at the bare slug.
        rdir = run_env / "develop-issue-123--lifecycle"
        rdir.mkdir(parents=True)
        (rdir / "state.yaml").write_text("{broken: [", encoding="utf-8")
        got_rdir, state = run_state.ensure_run()
        assert got_rdir == rdir
        assert state["slug"] == "develop-issue-123"
        assert not (run_env / "develop-issue-123").exists()  # no fork
        backups = list(rdir.glob("state.yaml.bad-*"))
        assert len(backups) == 1  # corrupt bytes preserved for post-mortem

    def test_ensure_run_still_refuses_newer_schema(self, run_env):
        rdir = _seed_at(run_env, "develop-issue-123", "develop-issue-123")
        state = run_state.load_state(rdir)
        state["schema"] = run_state.SCHEMA_VERSION + 1
        (rdir / "state.yaml").write_text(
            yaml.safe_dump(state, sort_keys=False), encoding="utf-8"
        )
        with pytest.raises(run_state.RunStateError, match="read-only refusal"):
            run_state.ensure_run()
        # The newer-schema file is untouched — not reseeded over.
        assert run_state._read_sibling_state(rdir)["schema"] == run_state.SCHEMA_VERSION + 1

    def test_readable_foreign_state_is_not_a_recovery_candidate(self, run_env):
        _seed_at(run_env, "develop-issue-123--x", "some-other-slug")
        assert run_state.run_dir() == run_env / "develop-issue-123"


class TestDeriveSlugShaTruncation:
    def test_full_sha_slug_uses_short_form(self):
        sha = "2a418231d83e1b7f4ff952656724f4de18a15d6e"
        assert run_state.derive_slug("review", sha) == "review-2a418231d83e"

    def test_uppercase_sha_lowers(self):
        sha = "2A418231D83E1B7F4FF952656724F4DE18A15D6E"
        assert run_state.derive_slug("review", sha) == "review-2a418231d83e"

    @pytest.mark.parametrize(
        "value",
        [
            "2a418231d83e1b7f4ff952656724f4de18a15d6",  # 39 hex
            "2a418231d83e1b7f4ff952656724f4de18a15d6ez",  # 41 chars
            "ga418231d83e1b7f4ff952656724f4de18a15d6e",  # 40 chars, non-hex
        ],
    )
    def test_almost_shas_keep_full_form(self, value):
        assert run_state.derive_slug("review", value) == f"review-{value}"


class TestCmdSeed:
    def test_happy_path_writes_but_never_pushes(self, run_env, capsys):
        with patch("work_repo.cli.commit_work_path", return_value=0) as push:
            rc = _main(["seed", "develop", "gate-receipts"])
        assert rc == 0
        push.assert_not_called()
        rdir = run_env / "develop-gate-receipts"
        state = run_state.load_state(rdir)
        assert state["slug"] == "develop-gate-receipts"
        assert state["taskdef"] == "develop"
        assert state["target"] == "gate-receipts"
        assert state["owner"] is None  # seeding is not owning
        out = capsys.readouterr().out
        assert "Run seeded" in out
        assert "work commit" in out

    def test_goal_and_name_recorded_with_cli_event_shapes(self, run_env):
        rc = _main(["seed", "develop", "gate-receipts",
                    "--goal", "receipts for gates", "--name", "gate-receipts"])
        assert rc == 0
        rdir = run_env / "develop-gate-receipts"
        state = run_state.load_state(rdir)
        assert state["goal"] == "receipts for gates"
        assert state["name"] == "gate-receipts"
        events = run_state.read_events(rdir, last_n=0)
        assert [e["type"] for e in events] == ["run_seeded", "goal_set", "run_named"]

    def test_existing_run_is_an_error(self, run_env, capsys):
        assert _main(["seed", "develop", "gate-receipts"]) == 0
        assert _main(["seed", "develop", "gate-receipts"]) == 1
        assert "already exists" in capsys.readouterr().err

    def test_existing_renamed_run_is_still_detected(self, run_env, capsys):
        _seed_at(run_env, "develop-gate-receipts--gr", "develop-gate-receipts")
        assert _main(["seed", "develop", "gate-receipts"]) == 1
        assert "already exists" in capsys.readouterr().err

    def test_name_conflict_with_sibling_is_an_error(self, run_env, capsys):
        _seed_at(run_env, "other-run", "other-run", name="gate-receipts")
        rc = _main(["seed", "develop", "receipts", "--name", "gate-receipts"])
        assert rc == 1
        assert "already held" in capsys.readouterr().err
        assert not (run_env / "develop-receipts").exists()

    def test_reserved_name_is_an_error(self, run_env, capsys):
        assert _main(["seed", "develop", "receipts", "--name", "archive"]) == 1
        assert "reserved" in capsys.readouterr().err

    def test_name_is_normalized(self, run_env, capsys):
        assert _main(["seed", "develop", "receipts", "--name", "Gate Receipts"]) == 0
        assert "Normalized to: gate-receipts" in capsys.readouterr().out
        state = run_state.load_state(run_env / "develop-receipts")
        assert state["name"] == "gate-receipts"

    def test_full_sha_target_gets_short_slug(self, run_env):
        sha = "2a418231d83e1b7f4ff952656724f4de18a15d6e"
        assert _main(["seed", "review", sha]) == 0
        assert (run_env / "review-2a418231d83e").is_dir()

    def test_no_context_is_an_error(self, capsys):
        assert _main(["seed", "develop", "x"]) == 1
        assert "No run context" in capsys.readouterr().err


class TestNameSlugShadowGuard:
    def test_name_matching_a_renamed_runs_slug_is_rejected(self, run_env, capsys):
        # After the freeze rename the sibling's dir no longer carries its
        # slug, so the conflict check must read it from the sibling's state.
        _seed_at(run_env, "develop-issue-5--auth", "develop-issue-5", name="auth")
        with patch("work_repo.cli.commit_work_path", return_value=0):
            assert _main(["state", "set", "--phase=spec"]) == 0  # seed our run
        assert _main(["name", "develop-issue-5"]) == 1
        assert "already held" in capsys.readouterr().err

    def test_seed_refuses_slug_held_as_sibling_name(self, run_env, capsys):
        _seed_at(run_env, "chat", "chat", name="develop-gate-receipts")
        assert _main(["seed", "develop", "gate-receipts"]) == 1
        assert "already exists" in capsys.readouterr().err


class TestFreezeViaStateSet:
    def _seed_named(self, run_env):
        with patch("work_repo.cli.commit_work_path", return_value=0):
            assert _main(["state", "set", "--phase=spec"]) == 0
            assert _main(["name", "lifecycle"]) == 0

    def test_planning_phase_does_not_freeze(self, run_env):
        self._seed_named(run_env)
        state = run_state.load_state(run_env / "develop-issue-123")
        assert state["frozen"] is None

    def test_execution_phase_freezes_and_renames(self, run_env):
        self._seed_named(run_env)
        with patch("work_repo.cli.commit_work_path", return_value=0) as push:
            assert _main(["state", "set", "--phase=execution"]) == 0
        renamed = run_env / "develop-issue-123--lifecycle"
        assert renamed.is_dir()
        assert not (run_env / "develop-issue-123").exists()
        state = run_state.load_state(renamed)
        assert state["frozen"] is not None
        assert state["phase"] == "execution"
        # Push staged both the new dir and the old (deleted) one.
        staged = push.call_args.args[0]
        assert "git.example.com/org/repo/runs/develop-issue-123--lifecycle" in staged
        assert "git.example.com/org/repo/runs/develop-issue-123" in staged
        events = run_state.read_events(renamed, last_n=0)
        assert any(e["type"] == "run_dir_renamed" for e in events)

    def test_rename_happens_exactly_once(self, run_env):
        self._seed_named(run_env)
        with patch("work_repo.cli.commit_work_path", return_value=0):
            assert _main(["state", "set", "--phase=execution"]) == 0
            # Rename the run again by hand? No — renaming is one-shot; a
            # later name change must not move the dir a second time.
            assert _main(["name", "second-name"]) == 0
            assert _main(["state", "set", "--phase=delivery"]) == 0
        assert (run_env / "develop-issue-123--lifecycle").is_dir()
        assert not (run_env / "develop-issue-123--second-name").exists()

    def test_unnamed_at_freeze_has_no_second_chance(self, run_env):
        with patch("work_repo.cli.commit_work_path", return_value=0):
            assert _main(["state", "set", "--phase=execution"]) == 0
            assert _main(["name", "latecomer"]) == 0
            assert _main(["state", "set", "--phase=delivery"]) == 0
        assert (run_env / "develop-issue-123").is_dir()
        assert not (run_env / "develop-issue-123--latecomer").exists()
        state = run_state.load_state(run_env / "develop-issue-123")
        assert state["frozen"] is not None
        assert state["name"] == "latecomer"

    def test_verbs_keep_working_after_rename(self, run_env):
        self._seed_named(run_env)
        with patch("work_repo.cli.commit_work_path", return_value=0):
            assert _main(["state", "set", "--phase=execution"]) == 0
            assert _main(["event", "checkpoint", "--note", "post-rename event"]) == 0
            assert _main(["state", "set", "--stop-reason=question"]) == 0
        renamed = run_env / "develop-issue-123--lifecycle"
        state = run_state.load_state(renamed)
        assert state["stop_reason"] == "question"
        events = run_state.read_events(renamed, last_n=0)
        assert any(e["type"] == "checkpoint" for e in events)


class TestLogReportUnification:
    def test_log_writes_into_run_dir_and_seeds(self, run_env):
        assert _main(["log", "A sufficiently long log message"]) == 0
        rdir = run_env / "develop-issue-123"
        assert (rdir / "log.yaml").exists()
        assert (rdir / "state.yaml").exists()  # auto-seeded, not stateless
        logs = yaml.safe_load((rdir / "log.yaml").read_text())
        assert logs[0]["message"] == "A sufficiently long log message"

    def test_log_follows_renamed_run_dir(self, run_env):
        rdir = _seed_at(run_env, "develop-issue-123--lifecycle", "develop-issue-123")
        assert _main(["log", "A sufficiently long log message"]) == 0
        assert (rdir / "log.yaml").exists()
        assert not (run_env / "develop-issue-123").exists()

    def test_log_display_falls_back_to_legacy_location(
        self, run_env, tmp_path, capsys
    ):
        legacy = tmp_path / "git.example.com" / "org/repo" / "develop" / "issue-123"
        legacy.mkdir(parents=True)
        (legacy / "log.yaml").write_text("- message: legacy entry\n", encoding="utf-8")
        assert _main(["log"]) == 0
        out = capsys.readouterr().out
        assert str(legacy / "log.yaml") in out
        assert "legacy entry" in out

    def test_report_lands_in_run_dir_reports(self, run_env, tmp_path):
        source = tmp_path / "report-src.md"
        source.write_text("# findings\n", encoding="utf-8")
        assert _main(["report", "--file", str(source)]) == 0
        rdir = run_env / "develop-issue-123"
        reports = list((rdir / "reports").glob("*.md"))
        assert len(reports) == 1
        assert reports[0].read_text() == "# findings\n"
        assert (rdir / "state.yaml").exists()  # auto-seeded

    def test_resume_json_exposes_resolved_run_dir(self, run_env, capsys):
        import json

        rdir = _seed_at(run_env, "develop-issue-123--lifecycle", "develop-issue-123")
        assert _main(["resume", "--json"]) == 0
        decision = json.loads(capsys.readouterr().out)
        assert decision["run_dir"] == str(rdir)


class TestSessionStartLifecycle:
    def test_fresh_session_seeds_via_tmp_rename(self, run_env):
        with patch("work_repo.cli.commit_work_path", return_value=0):
            assert _main(["session-start"]) == 0
        rdir = run_env / "develop-issue-123"
        assert (rdir / "state.yaml").exists()
        leftovers = [p.name for p in run_env.iterdir() if p.name.startswith(".new-")]
        assert leftovers == []

    def test_resume_lands_on_renamed_run(self, run_env, capsys):
        rdir = _seed_at(
            run_env, "develop-issue-123--lifecycle", "develop-issue-123", name="lifecycle"
        )
        with patch("work_repo.cli.commit_work_path", return_value=0):
            assert _main(["session-start"]) == 0
        # The claim landed on the renamed dir — no duplicate run was seeded.
        assert not (run_env / "develop-issue-123").exists()
        state = run_state.load_state(rdir)
        assert state["owner"]["session_id"] == "s-lc-1"
