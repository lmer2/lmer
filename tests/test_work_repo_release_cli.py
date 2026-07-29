"""Tests for the `work release record` / `work release status` verbs
(RUN-STATE.md §7 frozen verb table; masterplan release-flow §3).

The release record wired into the single-writer work CLI: every mutation
goes through the release.yaml kernel (release_run recorders), appends a
`release` audit event, and pushes the run dir via commit_work_path.
Identity fields are write-once — re-recording the identical value is an
idempotent no-op, a contradicting value is refused with the kernel's
hard-stop message (exit 1, never overwritten). `status` is read-only and
prints the derived leg plus the SINGLE next step (human and --json) so a
relaunched or scheduled session can decide whether there is anything to
advance.
"""
import json
from unittest.mock import patch

import pytest
import yaml

from work_repo import cli as work_cli
from work_repo import release_run, run_state
from tests.conftest import strip_lmer_env

SHA_BUMP = "a" * 40
SHA_MERGE = "b" * 40
RUN_REL = "git.example.com/org/repo/runs/release-main"
# The version-bearing address the run moves to at `record version`.
RUN_REL_V = "git.example.com/org/repo/runs/release-main-v0.5.0"
# Staged paths after the move: the resolved dir, then the vacated one.
RUN_RELS = [RUN_REL_V, RUN_REL]


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def run_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
    monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
    monkeypatch.setenv("LMER_REPO_PROJECT", "org/repo")
    monkeypatch.setenv("LMER_TASK", "release")
    monkeypatch.setenv("LMER_TASK_TARGET", "main")
    monkeypatch.setenv("LMER_SESSION_ID", "s-rel-1")
    return tmp_path / "git.example.com" / "org/repo" / "runs" / "release-main"


def _main(argv):
    with patch.object(work_cli.sys, "argv", ["work"] + argv):
        return work_cli.main()


def _record(argv):
    """A record verb with the durability push stubbed out."""
    with patch("work_repo.cli.commit_work_path", return_value=0):
        return _main(["release", "record"] + argv)


def _record_leg1():
    assert _record(["version", "0.5.0"]) == 0
    assert _record(["bump-sha", SHA_BUMP]) == 0


def _record_through_tag():
    _record_leg1()
    assert _record(["merge-sha", SHA_MERGE, "--version", "0.5.0"]) == 0
    assert _record(["tag", "v0.5.0", "--sha", SHA_MERGE]) == 0


def _rdir():
    """The run dir wherever the re-slug left it: recording the version moves
    the run off the seed address to `release-main-v0.5.0`."""
    return run_state.run_dir()


def _release_events(rdir):
    return [e for e in run_state.read_events(rdir, last_n=0)
            if e["type"] == "release"]


class TestRecordVersion:
    def test_no_context_exits_one(self, capsys):
        # Mutating-verb contract: no run context is an error, exit 1.
        assert _main(["release", "record", "version", "0.5.0"]) == 1
        assert "no run context" in capsys.readouterr().err.lower()

    def test_records_seeds_appends_and_pushes(self, run_env, capsys):
        with patch("work_repo.cli.commit_work_path", return_value=0) as push:
            assert _main(["release", "record", "version", "0.5.0"]) == 0
        release = release_run.load_release(_rdir())
        assert release["version"] == "0.5.0"
        events = run_state.read_events(_rdir(), last_n=0)
        assert events[0]["type"] == "run_seeded"  # auto-seeded, mutating-verb style
        assert _release_events(_rdir())[-1]["data"] == {
            "field": "version", "version": "0.5.0",
        }
        # Recording the version re-slugs the run: the push stages the new
        # address AND the vacated one, so the old path's deletion lands too.
        push.assert_called_once_with(
            RUN_RELS,
            "run-state: release-main-v0.5.0 release record version 0.5.0",
        )
        out = capsys.readouterr().out
        assert "✅ Release record: version 0.5.0" in out
        assert "next: leg1-record-bump-merge" in out

    def test_idempotent_re_record_no_new_event(self, run_env):
        assert _record(["version", "0.5.0"]) == 0
        n_events = len(_release_events(_rdir()))
        assert _record(["version", "0.5.0"]) == 0
        assert len(_release_events(_rdir())) == n_events  # no-op, no audit noise
        assert release_run.load_release(_rdir())["version"] == "0.5.0"

    def test_contradiction_refused_with_kernel_message(self, run_env, capsys):
        assert _record(["version", "0.5.0"]) == 0
        with patch("work_repo.cli.commit_work_path", return_value=0) as push:
            assert _main(["release", "record", "version", "0.6.0"]) == 1
        err = capsys.readouterr().err
        assert "already recorded as '0.5.0'" in err
        assert "refusing to change it to '0.6.0'" in err
        assert release_run.load_release(_rdir())["version"] == "0.5.0"  # untouched
        push.assert_not_called()  # a refused write pushes nothing

    def test_tag_prefixed_version_refused(self, run_env, capsys):
        assert _record(["version", "v0.5.0"]) == 1
        assert "tag prefix" in capsys.readouterr().err


class TestRecordBumpSha:
    def test_requires_version_first(self, run_env, capsys):
        assert _record(["bump-sha", SHA_BUMP]) == 1
        assert "record the version first" in capsys.readouterr().err

    def test_records_and_derives_gate(self, run_env, capsys):
        _record_leg1()
        release = release_run.load_release(_rdir())
        assert release["bump_mr_merge_sha"] == SHA_BUMP
        assert "next: gate-await-release-merge" in capsys.readouterr().out

    def test_short_sha_refused(self, run_env, capsys):
        assert _record(["version", "0.5.0"]) == 0
        assert _record(["bump-sha", "abc123"]) == 1
        assert "full 40-hex" in capsys.readouterr().err

    def test_push_message_uses_short_sha(self, run_env):
        assert _record(["version", "0.5.0"]) == 0
        with patch("work_repo.cli.commit_work_path", return_value=0) as push:
            assert _main(["release", "record", "bump-sha", SHA_BUMP]) == 0
        push.assert_called_once_with(
            RUN_RELS,
            f"run-state: release-main-v0.5.0 release record bump-sha "
            f"{SHA_BUMP[:12]}",
        )


class TestRecordMergeSha:
    def test_version_flag_is_required(self, run_env):
        # §7 frozen surface: --version is required, argparse enforces it.
        with pytest.raises(SystemExit) as exc:
            _main(["release", "record", "merge-sha", SHA_MERGE])
        assert exc.value.code == 2

    def test_version_mismatch_is_hard_stop(self, run_env, capsys):
        _record_leg1()
        with patch("work_repo.cli.commit_work_path", return_value=0) as push:
            assert _main(["release", "record", "merge-sha", SHA_MERGE,
                          "--version", "0.6.0"]) == 1
        err = capsys.readouterr().err
        assert "HARD STOP" in err
        assert "human decision required" in err
        assert release_run.load_release(_rdir())["release_mr_merge_sha"] is None
        push.assert_not_called()

    def test_match_records_and_derives_leg2(self, run_env, capsys):
        _record_leg1()
        assert _record(["merge-sha", SHA_MERGE, "--version", "0.5.0"]) == 0
        release = release_run.load_release(_rdir())
        assert release["release_mr_merge_sha"] == SHA_MERGE
        assert _release_events(_rdir())[-1]["data"]["version_at_sha"] == "0.5.0"
        assert "next: leg2-create-tag" in capsys.readouterr().out

    def test_mismatch_checked_even_on_re_record(self, run_env, capsys):
        # A re-entered leg 2 must re-prove the version binding, not coast.
        _record_leg1()
        assert _record(["merge-sha", SHA_MERGE, "--version", "0.5.0"]) == 0
        assert _record(["merge-sha", SHA_MERGE, "--version", "0.6.0"]) == 1
        assert "HARD STOP" in capsys.readouterr().err


class TestRecordTag:
    def test_sha_flag_is_required(self, run_env):
        with pytest.raises(SystemExit) as exc:
            _main(["release", "record", "tag", "v0.5.0"])
        assert exc.value.code == 2

    def test_requires_merge_sha_first(self, run_env, capsys):
        _record_leg1()
        assert _record(["tag", "v0.5.0", "--sha", SHA_MERGE]) == 1
        assert "no release-MR merge SHA recorded" in capsys.readouterr().err

    def test_wrong_name_is_hard_stop(self, run_env, capsys):
        # The spec §1 `0.2.0`-without-prefix damage, refused at record time.
        _record_leg1()
        assert _record(["merge-sha", SHA_MERGE, "--version", "0.5.0"]) == 0
        assert _record(["tag", "0.5.0", "--sha", SHA_MERGE]) == 1
        assert "HARD STOP" in capsys.readouterr().err
        assert release_run.load_release(_rdir())["tag"] is None

    def test_wrong_sha_is_hard_stop(self, run_env, capsys):
        _record_leg1()
        assert _record(["merge-sha", SHA_MERGE, "--version", "0.5.0"]) == 0
        assert _record(["tag", "v0.5.0", "--sha", "c" * 40]) == 1
        err = capsys.readouterr().err
        assert "HARD STOP" in err
        assert "never re-point, never re-sign" in err

    def test_records_and_re_record_is_idempotent(self, run_env, capsys):
        _record_through_tag()
        n_events = len(_release_events(_rdir()))
        assert _record(["tag", "v0.5.0", "--sha", SHA_MERGE]) == 0
        assert len(_release_events(_rdir())) == n_events
        tag = release_run.load_release(_rdir())["tag"]
        assert tag["name"] == "v0.5.0"
        assert tag["sha"] == SHA_MERGE
        assert "next: leg2-push-github-main" in capsys.readouterr().out


class TestRecordReceipt:
    def test_unknown_name_refused(self, run_env, capsys):
        _record_through_tag()
        assert _record(["receipt", "carrier-pigeon"]) == 1
        assert "unknown receipt" in capsys.readouterr().err

    def test_receipt_before_tag_refused(self, run_env, capsys):
        _record_leg1()
        assert _record(["receipt", "github-main-push"]) == 1
        assert "no tag recorded" in capsys.readouterr().err

    def test_actions_run_requires_url(self, run_env, capsys):
        _record_through_tag()
        assert _record(["receipt", "actions-run"]) == 1
        assert "requires --url" in capsys.readouterr().err
        assert _record(["receipt", "pypi"]) == 1

    def test_records_url_and_note(self, run_env):
        _record_through_tag()
        assert _record(["receipt", "github-main-push", "--note", "reconciled"]) == 0
        url = "https://github.com/org/repo/actions/runs/42"
        assert _record(["receipt", "github-tag-push"]) == 0
        assert _record(["receipt", "actions-run", "--url", url]) == 0
        receipts = release_run.load_release(_rdir())["receipts"]
        assert receipts["github-main-push"]["note"] == "reconciled"
        assert receipts["actions-run"]["url"] == url
        event = _release_events(_rdir())[-1]
        assert event["data"] == {"field": "receipt", "receipt": "actions-run",
                                 "url": url}

    def test_re_record_replaces_url_with_audit_trail(self, run_env):
        # Re-dispatched Actions run (spec §7 artifact drift): the receipt
        # must name the run that ACTUALLY uploaded; the prior value stays
        # in the event trail.
        _record_through_tag()
        assert _record(["receipt", "github-main-push"]) == 0
        assert _record(["receipt", "github-tag-push"]) == 0
        first = "https://github.com/org/repo/actions/runs/1"
        second = "https://github.com/org/repo/actions/runs/2"
        assert _record(["receipt", "actions-run", "--url", first]) == 0
        assert _record(["receipt", "actions-run", "--url", second]) == 0
        receipts = release_run.load_release(_rdir())["receipts"]
        assert receipts["actions-run"]["url"] == second
        urls = [e["data"]["url"] for e in _release_events(_rdir())
                if e["data"].get("receipt") == "actions-run"]
        assert urls == [first, second]

    def test_push_failure_is_nonfatal(self, run_env, capsys):
        _record_through_tag()
        with patch("work_repo.cli.commit_work_path", return_value=1):
            assert _main(["release", "record", "receipt", "github-main-push"]) == 0
        assert "push failed" in capsys.readouterr().out


class TestRecordDispatch:
    def test_bare_record_errors(self, run_env, capsys):
        assert _main(["release", "record"]) == 1
        err = capsys.readouterr().err
        assert "version | bump-sha | merge-sha | tag | receipt" in err


class TestStatus:
    def test_no_context_exits_zero(self, capsys):
        assert _main(["release", "status"]) == 0
        assert "no run context" in capsys.readouterr().out.lower()

    def test_no_context_json(self, capsys):
        assert _main(["release", "status", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["next_step"] is None
        assert payload["detail"] == "no run context"

    def test_no_release_recorded(self, run_env, capsys):
        assert _main(["release", "status"]) == 0
        assert "No release recorded" in capsys.readouterr().out

    def test_no_release_json_still_derives_position(self, run_env, capsys):
        # A fresh release run has no release.yaml yet — the scheduled
        # relaunch still gets a single next step (leg 1 from the top).
        assert _main(["release", "status", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["leg"] == "leg1"
        assert payload["next_step"] == "leg1-bump"
        assert payload["recorded"] is False

    def test_mid_ladder_human_view(self, run_env, capsys):
        _record_through_tag()
        capsys.readouterr()
        assert _main(["release", "status"]) == 0
        out = capsys.readouterr().out
        assert "Release: 0.5.0 — leg2 (next: leg2-push-github-main)" in out
        assert f"v0.5.0 @ {SHA_MERGE}" in out

    def test_mid_ladder_json(self, run_env, capsys):
        _record_through_tag()
        assert _record(["receipt", "github-main-push"]) == 0
        capsys.readouterr()
        assert _main(["release", "status", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["leg"] == "leg2"
        assert payload["next_step"] == "leg2-push-github-tag"
        assert payload["version"] == "0.5.0"
        assert payload["merge_sha"] == SHA_MERGE
        assert payload["pushed"] == {"github_main": True, "github_tag": False,
                                     "gitlab_tag": False}
        assert payload["recorded"] is True

    def test_full_ladder_completes(self, run_env, capsys):
        _record_through_tag()
        assert _record(["receipt", "github-main-push"]) == 0
        assert _record(["receipt", "github-tag-push"]) == 0
        assert _record(["receipt", "actions-run",
                        "--url", "https://github.com/org/repo/actions/runs/9"]) == 0
        assert _record(["receipt", "pypi",
                        "--url", "https://pypi.org/project/x/0.5.0/"]) == 0
        assert _record(["receipt", "gitlab-tag-push"]) == 0
        capsys.readouterr()
        assert _main(["release", "status", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["leg"] == "complete"
        assert payload["next_step"] == "complete"

    def test_hand_edited_inconsistency_is_hard_stop(self, run_env, capsys):
        # The recorders refuse to write a tag that disagrees with the merge
        # SHA — seeing one means release.yaml was hand-edited: exit 1 with
        # the kernel's message, never converge over it.
        _record_through_tag()
        release = release_run.load_release(_rdir())
        release["tag"]["sha"] = "d" * 40
        release_run.write_release(_rdir(), release)
        capsys.readouterr()
        assert _main(["release", "status"]) == 1
        assert "HARD STOP" in capsys.readouterr().err

    def test_corrupt_release_file_errors(self, run_env, capsys):
        run_env.mkdir(parents=True)
        (run_env / "release.yaml").write_text("{ not: valid: yaml [")
        assert _main(["release", "status"]) == 1
        assert "release.yaml" in capsys.readouterr().err

    def test_read_only_no_events_no_push(self, run_env, capsys):
        _record_through_tag()
        n_events = len(run_state.read_events(_rdir(), last_n=0))
        with patch("work_repo.cli.commit_work_path", return_value=0) as push:
            assert _main(["release", "status"]) == 0
        assert len(run_state.read_events(_rdir(), last_n=0)) == n_events
        push.assert_not_called()


class TestSingleWriterFile:
    def test_record_lands_in_release_yaml_not_state(self, run_env):
        # Storage decision: a dedicated release.yaml beside state.yaml —
        # never additive keys in the universal run contract.
        _record_leg1()
        rdir = _rdir()
        assert (rdir / "release.yaml").exists()
        state = yaml.safe_load((rdir / "state.yaml").read_text())
        assert "version" not in state
        assert "bump_mr_merge_sha" not in state
