"""Tests for the run re-slug primitive and successor resolution
(run_state.reslug_run / find_successor_run_dir / resolve_run_dir).

Run identity is deterministic per `(taskdef, target)`, so every release of a
repository resolved to ONE run dir and a terminal run was refused forever.
A release run now moves to a version-bearing slug when leg 1 records the
version; the address it vacates is what the next release seeds at. The
resolver has to follow the live run to its new address without ever
resolving a finished one — and without string-prefix guessing, which would
let `develop-mr-171` resolve `develop-mr-1712`.
"""
from unittest.mock import patch

import pytest

from work_repo import run_state
from tests.conftest import strip_lmer_env


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def runs(monkeypatch, tmp_path):
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
    monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
    monkeypatch.setenv("LMER_REPO_PROJECT", "org/repo")
    monkeypatch.setenv("LMER_TASK", "release")
    monkeypatch.setenv("LMER_TASK_TARGET", "main")
    monkeypatch.setenv("LMER_SESSION_ID", "s-1")
    base = tmp_path / "git.example.com" / "org/repo" / "runs"
    base.mkdir(parents=True)
    return base


def _seed(base, slug, taskdef="release", target="main", status="in-progress",
          name=None, dirname=None, reslugged_from=None):
    rdir = base / (dirname or slug)
    state = run_state.seed_state(slug, taskdef, target)
    state["status"] = status
    state["name"] = name
    if reslugged_from is not None:
        state["reslugged_from"] = reslugged_from
    run_state.write_state(rdir, state)
    return rdir, state


class TestReslugRun:
    def test_renames_the_dir_and_rewrites_the_slug(self, runs):
        rdir, state = _seed(runs, "release-main")
        new_dir, state, old_name = run_state.reslug_run(
            rdir, state, "release-main-v0.6.0")

        assert new_dir == runs / "release-main-v0.6.0"
        assert old_name == "release-main"
        assert state["slug"] == "release-main-v0.6.0"
        assert run_state.load_state(new_dir)["slug"] == "release-main-v0.6.0"
        assert not (runs / "release-main").exists()

    def test_the_vacated_address_is_free_for_the_next_run(self, runs):
        """The whole point: seed_run_dir can create at the bare address
        again without adopting the previous release's run."""
        rdir, state = _seed(runs, "release-main")
        run_state.reslug_run(rdir, state, "release-main-v0.6.0")

        fresh, fresh_state = run_state.seed_run_dir(
            "release-main", "release", "main")

        assert fresh == runs / "release-main"
        assert fresh_state["slug"] == "release-main"
        assert fresh_state["status"] == "in-progress"

    def test_appends_a_run_reslugged_event(self, runs):
        rdir, state = _seed(runs, "release-main")
        new_dir, _, _ = run_state.reslug_run(rdir, state, "release-main-v0.6.0")

        events = run_state.read_events(new_dir, last_n=0)
        reslugged = [e for e in events if e["type"] == "run_reslugged"]
        assert len(reslugged) == 1
        assert reslugged[0]["note"] == "release-main -> release-main-v0.6.0"

    def test_a_named_dir_stays_name_bearing(self, runs):
        rdir, state = _seed(runs, "release-main", name="ship-it",
                            dirname="release-main--ship-it")
        new_dir, state, old_name = run_state.reslug_run(
            rdir, state, "release-main-v0.6.0")

        assert new_dir == runs / "release-main-v0.6.0--ship-it"
        assert old_name == "release-main--ship-it"
        assert state["slug"] == "release-main-v0.6.0"

    def test_same_slug_is_a_no_op(self, runs):
        rdir, state = _seed(runs, "release-main")
        same, state, old_name = run_state.reslug_run(rdir, state, "release-main")

        assert same == rdir
        assert old_name is None
        assert not [e for e in run_state.read_events(rdir, last_n=0)
                    if e["type"] == "run_reslugged"]

    def test_a_taken_target_leaves_the_slug_untouched(self, runs, capsys):
        """Slug and dir name must never disagree in a way that lets the next
        seed adopt this run: refusing the move keeps them together."""
        rdir, state = _seed(runs, "release-main")
        _seed(runs, "release-main-v0.6.0")

        same, state, old_name = run_state.reslug_run(
            rdir, state, "release-main-v0.6.0")

        assert same == rdir
        assert old_name is None
        assert state["slug"] == "release-main"
        assert run_state.load_state(rdir)["slug"] == "release-main"
        assert "re-slug skipped" in capsys.readouterr().err

    def test_a_failed_rename_leaves_the_slug_untouched(self, runs, capsys):
        rdir, state = _seed(runs, "release-main")
        with patch.object(run_state.Path, "rename",
                          side_effect=OSError("read-only fs")):
            same, state, old_name = run_state.reslug_run(
                rdir, state, "release-main-v0.6.0")

        assert same == rdir
        assert old_name is None
        assert state["slug"] == "release-main"
        assert "re-slug rename failed" in capsys.readouterr().err

    def test_completes_a_crash_between_rename_and_slug_write(self, runs):
        """Dir already at the target name, slug not yet moved (the crash
        window): the re-slug finishes without a second rename."""
        rdir, state = _seed(runs, "release-main", dirname="release-main-v0.6.0")

        same, state, old_name = run_state.reslug_run(
            rdir, state, "release-main-v0.6.0")

        assert same == rdir
        assert old_name is None  # nothing moved — nothing extra to stage
        assert state["slug"] == "release-main-v0.6.0"
        assert run_state.load_state(rdir)["slug"] == "release-main-v0.6.0"


class TestSuccessorResolution:
    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            ({}, ()),
            ({"reslugged_from": "release-main"}, ()),
            ({"reslugged_from": ["release-main", 7, None]},
             ("release-main",)),
        ],
    )
    def test_vacated_slug_shape_is_defined_once(self, state, expected):
        assert run_state.vacated_slugs(state) == expected
        assert run_state.has_vacated(state, "release-main") == (
            "release-main" in expected
        )

    def test_a_live_reslugged_run_still_resolves_from_the_bare_slug(self, runs):
        rdir, state = _seed(runs, "release-main")
        new_dir, _, _ = run_state.reslug_run(rdir, state, "release-main-v0.6.0")

        assert run_state.find_run_dir("release-main") is None
        assert run_state.resolve_run_dir("release-main") == new_dir
        assert run_state.run_dir() == new_dir

    def test_a_terminal_reslugged_run_never_resolves_for_another_launch(
            self, runs, monkeypatch):
        """A finished release has given its address up — that is what lets
        the next release seed there instead of being refused forever."""
        rdir, state = _seed(runs, "release-main")
        new_dir, state, _ = run_state.reslug_run(rdir, state, "release-main-v0.6.0")
        state["status"] = "complete"
        run_state.write_state(new_dir, state)
        monkeypatch.setenv("LMER_SESSION_ID", "s-next-release")

        assert run_state.resolve_run_dir("release-main") is None
        assert run_state.run_dir() == runs / "release-main"  # creation address

    def test_a_terminal_reslugged_run_resolves_for_the_launch_that_wrote_it(
            self, runs):
        """#172: the completing session must still find the run it just
        finished, or its commit stages the vacated address and its next
        `work` verb seeds a phantom there. Its `run_reslugged` event is the
        record that it wrote here."""
        rdir, state = _seed(runs, "release-main")
        new_dir, state, _ = run_state.reslug_run(rdir, state, "release-main-v0.6.0")
        state["status"] = "complete"
        run_state.write_state(new_dir, state)

        assert run_state.resolve_run_dir("release-main") == new_dir
        assert run_state.run_dir() == new_dir

    def test_a_launch_with_no_session_id_wrote_nothing(self, runs, monkeypatch):
        """The `unknown` label events carry for a contextless writer is not an
        identity — two operator shells would otherwise share every run."""
        monkeypatch.delenv("LMER_SESSION_ID")
        rdir, state = _seed(runs, "release-main")
        new_dir, state, _ = run_state.reslug_run(rdir, state, "release-main-v0.6.0")
        state["status"] = "complete"
        run_state.write_state(new_dir, state)

        assert run_state.read_events(new_dir)[-1]["session"] == "unknown"
        assert run_state.resolve_run_dir("release-main") is None

    def test_a_torn_multibyte_event_in_a_finished_run_does_not_break_resolution(
            self, runs, monkeypatch):
        """Every later launch reads the events of a finished run that vacated
        its address, so one crash mid-append (notes are not ASCII-escaped)
        must not wedge resolution for all of them."""
        rdir, state = _seed(runs, "release-main")
        new_dir, state, _ = run_state.reslug_run(rdir, state, "release-main-v0.6.0")
        state["status"] = "complete"
        run_state.write_state(new_dir, state)
        with open(new_dir / run_state.EVENTS_FILE, "ab") as fh:
            fh.write(b'{"ts": "x", "session": "s-1", "note": "caf\xc3')
        monkeypatch.setenv("LMER_SESSION_ID", "s-next-release")

        assert run_state.resolve_run_dir("release-main") is None
        assert run_state.read_events(new_dir)[-1]["type"] == "run_reslugged"

    def _finished_by(self, runs, monkeypatch, writer):
        monkeypatch.setenv("LMER_SESSION_ID", writer)
        rdir, state = _seed(runs, "release-main")
        new_dir, state, _ = run_state.reslug_run(rdir, state, "release-main-v0.6.0")
        state["status"] = "complete"
        run_state.write_state(new_dir, state)
        return new_dir, state

    def test_a_launch_in_the_lineage_wrote_the_run(self, runs, monkeypatch):
        new_dir, state = self._finished_by(runs, monkeypatch, "s-old")
        state["sessions"] = ["s-old", "s-resumed"]
        run_state.write_state(new_dir, state)
        monkeypatch.setenv("LMER_SESSION_ID", "s-resumed")

        assert "s-resumed" not in {
            e["session"] for e in run_state.read_events(new_dir, last_n=0)}
        assert run_state.resolve_run_dir("release-main") == new_dir

    def test_a_lineage_that_is_not_a_list_records_no_launch(self, runs, monkeypatch):
        new_dir, state = self._finished_by(runs, monkeypatch, "s-old")
        state["sessions"] = "s-resumed"
        run_state.write_state(new_dir, state)
        monkeypatch.setenv("LMER_SESSION_ID", "s-resumed")

        assert run_state.resolve_run_dir("release-main") is None

    @pytest.mark.parametrize("launch", [None, "s-resumed"])
    def test_the_session_a_resume_names_wrote_the_run(self, runs, monkeypatch,
                                                      launch):
        """The resumed launch's own id is not on the run yet — `attach_session`
        chains it only after the record is found."""
        new_dir, _ = self._finished_by(runs, monkeypatch, "s-old")
        if launch is None:
            monkeypatch.delenv("LMER_SESSION_ID")
        else:
            monkeypatch.setenv("LMER_SESSION_ID", launch)

        assert run_state.resolve_run_dir("release-main") is None
        assert run_state.resolve_run_dir("release-main", session="s-old") == new_dir
        assert run_state.resolve_run_dir("release-main", session="s-other") is None

    def test_an_unhashable_event_stamp_is_skipped(self, runs, monkeypatch):
        new_dir, _ = self._finished_by(runs, monkeypatch, "s-old")
        with open(new_dir / run_state.EVENTS_FILE, "a", encoding="utf-8") as fh:
            fh.write('{"ts": "x", "session": []}\n')
        monkeypatch.setenv("LMER_SESSION_ID", "s-resumed")
        run_state.append_event(new_dir, "note")

        assert run_state.resolve_run_dir("release-main") == new_dir

    def test_the_run_this_launch_wrote_wins_over_a_foreign_live_one(
            self, runs, monkeypatch):
        mine, state = _seed(runs, "release-main")
        mine, state, _ = run_state.reslug_run(mine, state, "release-main-v0.6.0")
        state["status"] = "complete"
        state["created"] = "2026-07-01T00:00:00Z"
        run_state.write_state(mine, state)
        monkeypatch.setenv("LMER_SESSION_ID", "s-other")
        live, state = _seed(runs, "release-main", dirname="release-main-x")
        live, state, _ = run_state.reslug_run(live, state, "release-main-v0.7.0")
        monkeypatch.setenv("LMER_SESSION_ID", "s-1")

        assert run_state.resolve_run_dir("release-main") == mine

    def test_an_exact_slug_match_wins_over_a_successor(self, runs):
        live, state = _seed(runs, "release-main")
        run_state.reslug_run(live, state, "release-main-v0.6.0")
        fresh, _ = _seed(runs, "release-main", dirname="release-main-2")

        assert run_state.resolve_run_dir("release-main") == fresh

    def test_never_matches_a_foreign_run_by_string_prefix(self, runs):
        """The fallback keys on addresses a run RECORDS having vacated, so a
        longer slug that merely starts the same way is not a successor."""
        _seed(runs, "develop-mr-1712", taskdef="develop", target="mr-1712",
              reslugged_from=["develop-mr-1712-old"])

        assert run_state.find_successor_run_dir("develop-mr-171") is None

    def test_a_different_taskdef_is_not_a_successor(self, runs):
        _seed(runs, "chat-main", taskdef="chat", target="main")

        assert run_state.find_successor_run_dir("release-main") is None

    def test_a_run_that_never_reslugged_is_not_a_successor(self, runs):
        """Vacating an address is a fact reslug_run records. A run that has
        not moved has not vacated anything, whatever its identity derives
        to — the predicate cannot be inferred from taskdef/target."""
        _seed(runs, "release-main-v0.6.0")

        assert run_state.find_successor_run_dir("release-main") is None

    def test_a_legacy_full_sha_run_is_not_aliased_by_the_short_slug(self, runs,
                                                                    monkeypatch):
        """derive_slug truncates a 40-hex target to 12 chars but promises "no
        aliasing between the two forms". Matching on DERIVED identity would
        break that promise here: a `develop` session would silently adopt a
        pre-#87 run it had never touched."""
        sha = "c" * 40
        monkeypatch.setenv("LMER_TASK", "develop")
        monkeypatch.setenv("LMER_TASK_TARGET", sha)
        legacy, _ = _seed(runs, f"develop-{sha}", taskdef="develop", target=sha)

        derived = run_state.derive_slug()
        assert derived == f"develop-{sha[:12]}"
        assert legacy.name != derived
        assert run_state.find_successor_run_dir(derived) is None
        assert run_state.resolve_run_dir(derived) is None
        assert run_state.run_dir() == runs / derived  # creation address

    def test_ties_resolve_to_the_newest_created(self, runs):
        older, state = _seed(runs, "release-main-v0.6.0",
                             reslugged_from=["release-main"])
        state["created"] = "2026-07-01T00:00:00Z"
        run_state.write_state(older, state)
        newer, state = _seed(runs, "release-main-v0.7.0",
                             reslugged_from=["release-main"])
        state["created"] = "2026-07-20T00:00:00Z"
        run_state.write_state(newer, state)

        assert run_state.find_successor_run_dir("release-main") == newer

    def test_every_vacated_identity_survives_multiple_reslugs(self, runs):
        first, state = _seed(runs, "release-main")
        second, state, _ = run_state.reslug_run(
            first, state, "release-main-v0.6.0")
        third, state, _ = run_state.reslug_run(
            second, state, "release-main-v0.6.0-final")

        assert run_state.vacated_slugs(state) == (
            "release-main", "release-main-v0.6.0")
        assert run_state.find_successor_run_dir("release-main") == third
        assert run_state.find_successor_run_dir("release-main-v0.6.0") == third

    def test_run_rel_path_candidates_covers_both_addresses(self, runs):
        rdir, state = _seed(runs, "release-main")
        run_state.reslug_run(rdir, state, "release-main-v0.6.0")

        rels = run_state.run_rel_path_candidates()

        assert rels == [
            "git.example.com/org/repo/runs/release-main-v0.6.0",
            "git.example.com/org/repo/runs/release-main",
        ]
