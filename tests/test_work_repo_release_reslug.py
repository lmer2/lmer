"""A repository can release more than once (MR !171 iteration-5 review).

The defect, stated directly: `derive_slug()` is deterministic per
`(taskdef, target)`, so every release of a repository resolved to ONE run
dir. Once that run went terminal — `complete` after a successful release,
`aborted` after a declined one — `work release claim` refused it and
`taskdef/release/instructions.txt` read the non-zero exit as "never proceed
unlocked; end the session". A repository could release exactly once, and
`release.yaml`'s write-once version blocked reopening the run by hand.

`docs/RUN-STATE.md` §7 and `taskdef/release-resume.jinja2` promise the next
release is *a NEW run*. These tests are that promise, end to end: a second
release claims after the first COMPLETED and after one was ABORTED, and the
run driving a live release is still found by its own sessions after it has
moved to its version-bearing address.
"""
import json
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from work_repo import cli as work_cli
from work_repo import release_run, run_state
from work_repo.git_ops import CLAIM_PUSH_WON
from tests.conftest import strip_lmer_env

SHA_BUMP = "a" * 40
SHA_MERGE = "b" * 40
# The seed address every release of this fixture's repository derives.
BASE = "release-repo"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def runs(monkeypatch, tmp_path):
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
    monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
    monkeypatch.setenv("LMER_REPO_PROJECT", "org/repo")
    monkeypatch.setenv("LMER_TASK", "release")
    monkeypatch.setenv("LMER_TASK_TARGET", "https://git.example.com/org/repo")
    monkeypatch.setenv("LMER_SESSION_ID", "s-rel-1")
    return tmp_path / "git.example.com" / "org/repo" / "runs"


def _main(argv):
    with patch.object(work_cli.sys, "argv", ["work"] + argv):
        return work_cli.main()


def _record(argv):
    with patch("work_repo.cli.commit_work_path", return_value=0):
        return _main(["release", "record"] + argv)


def _claim_seams(stack):
    """Patch the git plumbing seams the claim's CAS loop rides on. Returns
    the local-commit mock, whose staged path set is the "one CAS commit"
    property the roll-over must not break."""
    stack.enter_context(
        patch("work_repo.cli._sync_remote_head", return_value=(True, "")))
    commit = stack.enter_context(
        patch("work_repo.cli._commit_claim_write", return_value=(True, "")))
    stack.enter_context(patch("work_repo.cli._git_head", return_value="head"))
    stack.enter_context(patch("work_repo.cli._drop_claim_commit"))
    stack.enter_context(
        patch("work_repo.cli.claim_push_once", return_value=(CLAIM_PUSH_WON, "")))
    return commit


def _claim(stack, as_json=False):
    """`work release claim` with the git plumbing seams patched out."""
    _claim_seams(stack)
    return _main(["release", "claim"] + (["--json"] if as_json else []))


def _ship(version):
    """Drive one release all the way through leg 2, as the taskdef does."""
    assert _record(["version", version]) == 0
    assert _record(["bump-sha", SHA_BUMP]) == 0
    assert _record(["merge-sha", SHA_MERGE, "--version", version]) == 0
    assert _record(["tag", f"v{version}", "--sha", SHA_MERGE]) == 0
    for receipt in release_run.RECEIPT_NAMES:
        url = "https://example.invalid/run/1"
        assert _record(["receipt", receipt, "--url", url]) == 0


def _name_run(rdir, name):
    """What the freeze gate's one-shot rename does: record the name and move
    the dir to `<slug>--<name>`. The ground rules tell every session to
    `work name`, so a named run is the norm rather than the exotic case."""
    state = run_state.load_state(rdir)
    state["name"] = name
    run_state.write_state(rdir, state)
    return rdir.rename(rdir.parent / f"{state['slug']}--{name}")


def _close_out(status="complete", stop_reason="complete"):
    rdir = run_state.run_dir()
    state = run_state.load_state(rdir)
    state["status"] = status
    state["stop_reason"] = stop_reason
    state["claim"] = None
    run_state.write_state(rdir, state)
    return rdir


class TestSecondRelease:
    def test_a_second_release_claims_after_the_first_completed(self, runs, capsys):
        with ExitStack() as stack:
            assert _claim(stack) == 0
        _ship("0.5.0")
        first = _close_out()
        assert first.name == f"{BASE}-v0.5.0"
        capsys.readouterr()

        # The next release session — a fresh launch of the same taskdef
        # against the same repository, deriving the same address.
        with ExitStack() as stack:
            assert _claim(stack) == 0, "a repository can release only once"
        second = run_state.run_dir()

        assert second == runs / BASE
        assert second != first
        assert run_state.load_state(second)["status"] == "in-progress"
        # A fresh release record: leg 1 starts over, write-once intact.
        assert release_run.load_release(second) is None
        assert _record(["version", "0.6.0"]) == 0
        assert release_run.load_release(run_state.run_dir())["version"] == "0.6.0"
        # ...and the first release's record is untouched where it was left.
        assert release_run.load_release(first)["version"] == "0.5.0"

    def test_a_second_release_claims_after_one_was_aborted(self, runs, capsys):
        """The abandoned release (spec §7): bump merged, human declines the
        release MR. The bump stays on prep-release and the next run's ctl
        dry-run skips it — but only if there IS a next run."""
        with ExitStack() as stack:
            assert _claim(stack) == 0
        assert _record(["version", "0.5.0"]) == 0
        assert _record(["bump-sha", SHA_BUMP]) == 0
        release_run.record_abort(run_state.run_dir(), reason="declined")
        aborted = _close_out(stop_reason="aborted")
        capsys.readouterr()

        with ExitStack() as stack:
            assert _claim(stack) == 0, "an aborted release wedged the repository"

        second = run_state.run_dir()
        assert second == runs / BASE
        assert second != aborted
        assert release_run.load_release(aborted)["aborted"]["reason"] == "declined"
        assert release_run.derive_leg(
            release_run.load_release(aborted))["leg"] == "aborted"

    def test_three_releases_each_get_their_own_run(self, runs, capsys):
        dirs = []
        for version in ("0.5.0", "0.6.0", "0.7.0"):
            with ExitStack() as stack:
                assert _claim(stack) == 0
            _ship(version)
            dirs.append(_close_out())
            capsys.readouterr()

        assert [d.name for d in dirs] == [
            f"{BASE}-v0.5.0", f"{BASE}-v0.6.0", f"{BASE}-v0.7.0",
        ]
        assert all(d.exists() for d in dirs)
        assert [release_run.load_release(d)["version"] for d in dirs] == [
            "0.5.0", "0.6.0", "0.7.0",
        ]

    def test_a_re_used_version_does_not_wedge_the_repository(self, runs, capsys):
        """An abort at 0.6.0 is followed by a REAL 0.6.0, and then by the
        release after that.

        `RELEASE-FLOW.md` §6: a declined release leaves its bump on
        prep-release, so the successor's ctl dry-run skips it and the
        successor records the SAME `X.Y.Z`. The decline happens at the
        release-MR gate — after leg 1 step 5 — so the declined run has
        already taken `release-repo-v0.6.0` and is parked there, terminal.

        If the successor's address is decided by whether that DIRECTORY is
        free, its re-slug is skipped, it completes on the seed address, and
        the release after it is refused forever: the exact wedge
        version-in-slug exists to close. The third claim is the assertion.
        """
        with ExitStack() as stack:
            assert _claim(stack) == 0
        assert _record(["version", "0.6.0"]) == 0
        release_run.record_abort(run_state.run_dir(), reason="declined")
        aborted = _close_out(stop_reason="aborted")
        capsys.readouterr()

        with ExitStack() as stack:
            assert _claim(stack) == 0
        _ship("0.6.0")
        shipped = _close_out()
        capsys.readouterr()

        assert aborted.name == f"{BASE}-v0.6.0"  # it HAD recorded the version
        assert shipped != aborted
        assert shipped.exists() and aborted.exists()
        assert release_run.load_release(shipped)["tag"]["name"] == "v0.6.0"
        # The canonical address was taken, so it took a variant — but it did
        # LEAVE the seed address, which is the property that matters.
        assert shipped.name.startswith(f"{BASE}-v0.6.0-")
        assert not (runs / BASE).exists()
        # ...and one slug still names exactly one run.
        assert run_state.find_run_dir(f"{BASE}-v0.6.0") == aborted
        assert run_state.find_run_dir(shipped.name) == shipped

        # The release AFTER the re-used version — one release further than
        # this test used to go, and where the wedge actually bites.
        with ExitStack() as stack:
            assert _claim(stack) == 0, "a re-used version wedged the repository"
        _ship("0.7.0")
        third = _close_out()

        assert third.name == f"{BASE}-v0.7.0"
        assert len({aborted, shipped, third}) == 3

    def test_named_runs_never_record_one_slug_twice(self, runs, capsys):
        """The same collision from the other side.

        A named run lives at `runs/<slug>--<name>`, so its SLUG is occupied
        while `runs/<slug>` is free. A guard that asks whether the DIRECTORY
        exists lets the move through and leaves two runs recording one
        `state.slug` — after which `find_run_dir` picks between them by
        directory sort order, and every receipt citing that address points
        at a run chosen by chance. Renaming the stale dir by hand, the
        obvious manual recovery from the wedge above, produces exactly this.
        """
        with ExitStack() as stack:
            assert _claim(stack) == 0
        assert _record(["version", "0.6.0"]) == 0
        _name_run(run_state.run_dir(), "aa-declined-cut")
        release_run.record_abort(run_state.run_dir(), reason="declined")
        aborted = _close_out(stop_reason="aborted")
        capsys.readouterr()

        with ExitStack() as stack:
            assert _claim(stack) == 0
        _name_run(run_state.run_dir(), "zz-real-cut")
        assert _record(["version", "0.6.0"]) == 0
        shipped = run_state.run_dir()

        recorded = {d.name: run_state.load_state(d)["slug"] for d in runs.iterdir()}
        assert len(set(recorded.values())) == len(recorded), (
            f"two runs recorded one slug: {recorded}")
        assert shipped != aborted
        assert shipped.name.endswith("--zz-real-cut")  # still name-bearing
        assert run_state.find_run_dir(f"{BASE}-v0.6.0") == aborted

    def test_a_roll_over_frees_the_seed_address_when_the_version_is_taken(
            self, runs, capsys, monkeypatch):
        """The claim-side half of the same defect (`cli.py:2482`).

        A run whose re-slug could not happen at record time — a read-only
        fs, an interrupted move — stays on the seed address and goes
        terminal there while its version-bearing address belongs to someone
        else. The roll-over must free the seed address anyway: recomputing
        the same taken aside and giving up leaves the refusal permanent,
        because every later claim recomputes it too.
        """
        with ExitStack() as stack:
            assert _claim(stack) == 0
        assert _record(["version", "0.6.0"]) == 0
        aborted = _close_out(stop_reason="aborted")
        assert aborted.name == f"{BASE}-v0.6.0"
        capsys.readouterr()

        with ExitStack() as stack:
            assert _claim(stack) == 0
        with patch.object(run_state.Path, "rename",
                          side_effect=OSError("read-only fs")):
            assert _record(["version", "0.6.0"]) == 0
        parked = _close_out()
        assert parked == runs / BASE, "the re-slug was supposed to fail here"
        capsys.readouterr()

        monkeypatch.setenv("LMER_SESSION_ID", "s-third")
        with ExitStack() as stack:
            assert _claim(stack) == 0, "the seed address could not be freed"

        fresh = run_state.run_dir()
        assert fresh == runs / BASE
        assert run_state.load_state(fresh)["status"] == "in-progress"
        assert release_run.load_release(fresh) is None
        # The parked run moved somewhere of its own, not on top of `aborted`.
        assert run_state.load_state(aborted)["stop_reason"] == "aborted"
        assert len(list(runs.iterdir())) == 3


class TestLiveReleaseStillResolves:
    def test_the_run_follows_its_version_bearing_address(self, runs, capsys):
        with ExitStack() as stack:
            assert _claim(stack) == 0
        assert run_state.run_dir() == runs / BASE

        assert _record(["version", "0.5.0"]) == 0

        moved = runs / f"{BASE}-v0.5.0"
        assert run_state.run_dir() == moved, "the live run lost its own session"
        assert not (runs / BASE).exists()
        assert run_state.run_rel_path_candidates() == [
            f"git.example.com/org/repo/runs/{BASE}-v0.5.0",
            f"git.example.com/org/repo/runs/{BASE}",
        ]

    def test_an_interrupted_reslug_heals_to_the_canonical_address(
            self, runs, capsys):
        """The crash window reslug_run is ordered around: the dir was
        renamed, `state.slug` was not yet written. The next record verb must
        FINISH that move — the run is already sitting at the address it
        wants, and reading its OWN dir as "taken" would drift it to a fresh
        stamped address instead of healing it.
        """
        with ExitStack() as stack:
            assert _claim(stack) == 0
        assert _record(["version", "0.6.0"]) == 0
        moved = run_state.run_dir()
        assert moved.name == f"{BASE}-v0.6.0"

        # Rewind the slug write only; the rename already landed.
        state = run_state.load_state(moved)
        state["slug"] = BASE
        state.pop("reslugged_from", None)
        run_state.write_state(moved, state)
        capsys.readouterr()

        assert _record(["bump-sha", SHA_BUMP]) == 0

        healed = run_state.run_dir()
        assert healed == moved, "the interrupted re-slug drifted to a new address"
        assert run_state.load_state(healed)["slug"] == f"{BASE}-v0.6.0"
        assert [d.name for d in runs.iterdir()] == [f"{BASE}-v0.6.0"]

    def test_no_free_address_leaves_the_run_put_and_heals_at_the_next_verb(
            self, runs, capsys):
        """`unique_release_slug` raises rather than return an address it has
        just found taken. The record verb takes the same fallback a failed
        rename takes — the record itself already landed, so it warns, leaves
        the run where it is, and the next verb retries the move."""
        with ExitStack() as stack:
            assert _claim(stack) == 0
        with patch("work_repo.release_run.unique_release_slug",
                   side_effect=release_run.ReleaseRunError(
                       "no free release address for 'x'")):
            assert _record(["version", "0.5.0"]) == 0
        err = capsys.readouterr().err
        assert "re-slug skipped: no free release address" in err
        assert run_state.run_dir() == runs / BASE  # still at the seed address
        assert release_run.load_release(runs / BASE)["version"] == "0.5.0"

        assert _record(["bump-sha", SHA_BUMP]) == 0
        assert run_state.run_dir() == runs / f"{BASE}-v0.5.0"

    def test_a_relaunch_resumes_the_live_run_not_a_fresh_one(self, runs, capsys):
        """The scheduled relaunch derives the bare address and must land on
        the in-flight release — no launch parameter carries the version."""
        with ExitStack() as stack:
            assert _claim(stack) == 0
        assert _record(["version", "0.5.0"]) == 0
        assert _record(["bump-sha", SHA_BUMP]) == 0
        capsys.readouterr()

        with ExitStack() as stack:
            assert _claim(stack, as_json=True) == 0
        payload = json.loads(capsys.readouterr().out)

        assert payload["slug"] == f"{BASE}-v0.5.0"
        assert payload["action"] == "refresh"   # the same run, not a new one
        assert "rolled_over" not in payload
        assert release_run.next_step(
            release_run.load_release(run_state.run_dir())
        ) == "gate-await-release-merge"

    def test_a_live_release_still_refuses_a_second_session(self, runs, monkeypatch,
                                                           capsys):
        """Single-flight is unchanged by the move: the refusal that matters
        is on a LIVE run, and it still fires after the re-slug."""
        with ExitStack() as stack:
            assert _claim(stack) == 0
        assert _record(["version", "0.5.0"]) == 0
        capsys.readouterr()

        monkeypatch.setenv("LMER_SESSION_ID", "s-other")
        with ExitStack() as stack:
            assert _claim(stack) == 1
        err = capsys.readouterr().err
        assert "s-rel-1" in err
        assert f"{BASE}-v0.5.0" in err


class TestRollOverIsOneCommit:
    """Archive-aside, seed and claim must land together: a commit that
    staged only the fresh run would push a successor whose predecessor's
    move is still local, and the next session would resolve two runs at
    once.

    A move has TWO ends, and the commit needs both. Staging only the vacated
    path records the previous release run's `release.yaml`, events, spec,
    retro and reports as a deletion with nothing adding them back — no
    resolver can name the aside dir once the successor owns the address, so
    `work commit` never stages it and `report_uncommitted_work_items` reports
    it as a stray."""

    def test_the_rolled_aside_dir_is_staged_with_the_fresh_run(
            self, runs, capsys, monkeypatch):
        # A run that never reached `record version` never re-slugged itself,
        # so it is still parked on the seed address — the case the claim-side
        # roll-over exists for (a release aborted in leg 1). NAMED, so the dir
        # it vacates is `release-repo--<name>`: a path the run-dir resolver
        # cannot name once the fresh run owns the address, and therefore the
        # one the roll-over has to hand the commit explicitly.
        with ExitStack() as stack:
            assert _claim(stack) == 0
        parked = _close_out(stop_reason="aborted")
        state = run_state.load_state(parked)
        state["name"] = "declined"
        run_state.write_state(parked, state)
        parked = parked.rename(runs / f"{BASE}--declined")
        capsys.readouterr()
        monkeypatch.setenv("LMER_SESSION_ID", "s-next")

        with ExitStack() as stack:
            commit = _claim_seams(stack)
            assert _main(["release", "claim"]) == 0

        # Once the fresh run owns the address, the resolver cannot name the
        # vacated dir at all — so it has to come from the roll-over itself.
        assert f"git.example.com/org/repo/runs/{BASE}--declined" not in \
            run_state.run_rel_path_candidates()
        staged = commit.call_args.args[1] or []
        assert f"git.example.com/org/repo/runs/{BASE}--declined" in staged, (
            f"the vacated run's move was left out of the claim commit: {staged}"
        )
        # ...and the aside dir it moved to is where the old run now lives —
        # `<aside>--<name>`, a second path the slug does not spell either.
        aside = [d for d in runs.iterdir() if d.name != BASE]
        assert len(aside) == 1
        assert aside[0].name.endswith("--declined")  # still name-bearing
        assert run_state.load_state(aside[0])["stop_reason"] == "aborted"
        assert f"git.example.com/org/repo/runs/{aside[0].name}" in staged, (
            "the dir the run moved TO was left out of the claim commit — the "
            f"previous release run lands as a pure deletion: {staged}"
        )

    def test_the_destination_of_the_move_is_staged_with_the_vacated_path(
            self, runs, capsys, monkeypatch):
        """The unnamed spelling of the same property: the aside dir is
        `runs/<aside>` and the vacated one is the seed address the successor
        now occupies. Both are in the one commit, or the commit removes a
        release record it never re-adds."""
        with ExitStack() as stack:
            assert _claim(stack) == 0
        _close_out(stop_reason="aborted")
        capsys.readouterr()
        monkeypatch.setenv("LMER_SESSION_ID", "s-next")

        with ExitStack() as stack:
            commit = _claim_seams(stack)
            assert _main(["release", "claim"]) == 0

        aside = [d for d in runs.iterdir() if d.name != BASE]
        assert len(aside) == 1
        staged = commit.call_args.args[1] or []
        assert set(staged) == {
            f"git.example.com/org/repo/runs/{BASE}",
            f"git.example.com/org/repo/runs/{aside[0].name}",
        }, f"the move did not land whole: {staged}"
        # The successor owns the seed address; the predecessor's record is
        # intact at the address the commit carried it to.
        assert run_state.load_state(runs / BASE)["status"] == "in-progress"
        assert run_state.load_state(aside[0])["stop_reason"] == "aborted"

    def test_a_roll_over_stages_nothing_extra_when_nothing_moved(
            self, runs, capsys):
        """The ordinary claim on a fresh address carries no extra path."""
        with ExitStack() as stack:
            commit = _claim_seams(stack)
            assert _main(["release", "claim"]) == 0

        assert commit.call_args.args[1] is None
