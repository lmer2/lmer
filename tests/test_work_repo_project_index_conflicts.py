"""Rebase conflicts confined to generated README blocks continue; everything else aborts.

Two sessions pushing in one project almost always touch adjacent lines of the
project README's index block — two new rows under the header, a new row
beside the previously newest row's edit — so `pull --rebase` stops on the
README on the normal concurrent path, not the exceptional one.
`git_ops._continue_past_index_conflicts` runs inside that paused rebase: when
every unmerged path is a project README whose text outside the markers is
identical on both sides and whose upstream block is not of a newer format, it
renders the block again from the merged tree, stages it and continues; any
other stop falls into the existing abort. These tests race real clones of one
bare origin through the real `work` composers and check origin against a
fresh clone's render.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from tests import timing_budgets as timing
from tests.schema2_fixtures import (
    DEFAULT_PROJECT,
    RecordSpec,
    git,
    memory_env,
    publication2,
    rebase_state,
    record_git_commands,
    resume_session,
    row_ids,
    session_identity,
    stamp,
    start_session,
    state_args,
    uid_for,
)
from work_repo import cli, git_ops, memory
from work_repo import project_index as pi

PROJECT = DEFAULT_PROJECT
README = f"{PROJECT}/README.md"
NOTES = f"{PROJECT}/notes.txt"
SEEDED = RecordSpec(uid_for(1), "chat", stamp(1), stamp(1, 1), name="seeded")
RESOLVED = "↻ regenerated project index on the merged head"
PULL_FAILED = "❌ git pull --rebase failed"
#: One round only — the bound exhausted by the second conflicting local commit (a count, not a wait).
ONE_ROUND = 1


def _stash(repo: Path) -> str:
    return git(repo, "rev-parse", "--verify", "--quiet", "refs/stash", check=False).stdout.strip()


def _unmerged(repo: Path) -> str:
    return git(repo, "ls-files", "--unmerged").stdout


def _assert_clean_resolution(repo: Path) -> None:
    assert not rebase_state(repo)
    assert _unmerged(repo) == ""
    assert _stash(repo) == ""
    assert git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip() == "main"


def _share_file(case, rel: str, text: str) -> None:
    """Commit *rel* from the primary checkout and bring every clone up to date."""
    path = case.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    git(case.root, "add", "--", rel)
    git(case.root, "commit", "-q", "-m", f"share {rel}")
    git(case.root, "push", "-q")
    for clone in case.clones:
        git(clone, "pull", "-q", "--rebase")


def _push_by_hand(repo: Path, message: str) -> None:
    """A human's (or another tool's) plain git commit and push of the project."""
    git(repo, "add", "-A", "--", PROJECT)
    git(repo, "commit", "-q", "-m", message)
    git(repo, "push", "-q")


def _with_hand_text(text: str, prose: str) -> str:
    """The README with its hand text replaced and its block kept."""
    return f"# {PROJECT}\n\n{prose}\n\n{pi.extract_block(text)}\n"


def _newer_format_readme(text: str) -> str:
    """The README as a newer runtime would write it: a format-2 block of another shape."""
    newer = (
        f"{pi.START_MARKER}\n\n## Runs\n\nRendered by a newer lmer (lmer index format "
        f"{pi.FORMAT + 1}). Edit this file outside the markers only.\n\n"
        "| started | run | state | activity | record | extra |\n|---|---|---|---|---|---|\n"
        "| — | a newer runtime's row | open | — | `chat` · `newer` | yes |\n\n"
        f"{pi.END_MARKER}"
    )
    return text.replace(pi.extract_block(text), newer)


def _peer_inserts_a_row(repo: Path, label: str) -> None:
    """Another writer lands a row right under the table header — the adjacent-line shape."""
    readme = repo / README
    lines = readme.read_text(encoding="utf-8").split("\n")
    separator = lines.index("|---|---|---|---|---|")
    lines.insert(separator + 1, f"| 2026-09-30 00:00 | [{label}](_runs/chat/{label}/) | open "
                                f"| 2026-09-30 00:00 | `chat` · `{label}` |")
    readme.write_text("\n".join(lines), encoding="utf-8")
    _push_by_hand(repo, f"peer row {label}")


@pytest.fixture
def race(tmp_path, monkeypatch):
    """One bare origin, the primary checkout and three peers, all at one committed record."""
    return publication2(tmp_path, monkeypatch, clones=3, projects={PROJECT: [SEEDED]})


def _first_publication(case, monkeypatch, repo=None) -> Path:
    """Session A publishes from *repo* (the primary checkout by default): the README is born."""
    rdir, _ = start_session(monkeypatch, repo or case.root)
    assert cli.cmd_commit("A") == 0
    return rdir


# ---------------------------------------------------------------------------
# README-only races continue
# ---------------------------------------------------------------------------


class TestReadmeOnlyRacesPublishBothWriters:
    def test_the_second_writer_regenerates_inside_the_paused_rebase_and_pushes(
        self, race, monkeypatch, capsys, tmp_path
    ):
        case = race
        a_rdir = _first_publication(case, monkeypatch)
        b_repo = case.clones[0]
        b_rdir, _ = start_session(monkeypatch, b_repo)
        capsys.readouterr()

        assert cli.cmd_commit("B") == 0

        captured = capsys.readouterr()
        assert RESOLVED in captured.out
        assert PULL_FAILED not in captured.err
        assert "Successfully committed and pushed" in captured.out
        published, rendered = case.origin_render(PROJECT, tmp_path / "oracle")
        assert published == rendered
        assert row_ids(published) == {uid_for(1)[:8], a_rdir.name[:8], b_rdir.name[:8]}
        _assert_clean_resolution(b_repo)
        assert (b_repo / README).read_text(encoding="utf-8") == published

    def test_the_adjacent_row_race_resolves(self, race, monkeypatch, capsys, tmp_path):
        """A running session's row changes beside a new top row: adjacent hunks, a conflict."""
        case = race
        a_rdir = _first_publication(case, monkeypatch)
        a_identity = session_identity()
        b_repo = case.clones[0]
        git(b_repo, "pull", "-q", "--rebase")
        b_rdir, _ = start_session(monkeypatch, b_repo)
        assert cli.cmd_commit("B") == 0
        resume_session(monkeypatch, case.root, a_identity)
        capsys.readouterr()

        assert cli.cmd_state(state_args(phase="spec")) == 0

        captured = capsys.readouterr()
        assert RESOLVED in captured.out and PULL_FAILED not in captured.err
        assert "run-state push failed" not in captured.out
        published, rendered = case.origin_render(PROJECT, tmp_path / "oracle")
        assert published == rendered
        assert row_ids(published) == {uid_for(1)[:8], a_rdir.name[:8], b_rdir.name[:8]}
        own_row = next(line for line in published.splitlines() if a_rdir.name[:8] in line)
        assert "phase spec" in own_row
        _assert_clean_resolution(case.root)

    def test_three_writers_in_one_project_each_publish(self, race, monkeypatch, capsys, tmp_path):
        case = race
        rdirs = [_first_publication(case, monkeypatch)]
        capsys.readouterr()
        for repo in case.clones[:2]:
            rdir, _ = start_session(monkeypatch, repo)
            rdirs.append(rdir)
            assert cli.cmd_commit("writer") == 0
            _assert_clean_resolution(repo)

        captured = capsys.readouterr()
        assert captured.out.count(RESOLVED) == 2
        assert PULL_FAILED not in captured.err
        published, rendered = case.origin_render(PROJECT, tmp_path / "oracle")
        assert published == rendered
        assert row_ids(published) == {uid_for(1)[:8], *(rdir.name[:8] for rdir in rdirs)}

    def test_a_readme_only_replay_that_regenerates_to_upstreams_bytes_is_dropped(
        self, race, monkeypatch, capsys, tmp_path
    ):
        """A swept leftover's commit emptied by the resolution: `--continue` drops it (git ≥ 2.26)."""
        case = race
        x_repo, y_repo = case.clones[0], case.clones[1]
        changed, _ = pi.write_project_readme(x_repo / PROJECT, heading=PROJECT)
        assert changed, "the leftover must differ from the (absent) committed README"
        y_rdir, _ = start_session(monkeypatch, y_repo)
        assert cli.cmd_commit("Y") == 0
        upstream = case.origin_file(README)
        start_session(monkeypatch, x_repo)
        memory_env(monkeypatch, tmp_path, kind=None)
        capsys.readouterr()

        assert memory.persist_memory() == 0

        captured = capsys.readouterr()
        assert RESOLVED in captured.out and PULL_FAILED not in captured.err
        _assert_clean_resolution(x_repo)
        assert case.head(x_repo) == git(case.origin, "rev-parse", "main").stdout.strip()
        assert "Update work repo: " not in git(x_repo, "log", "--format=%s").stdout
        assert case.origin_file(README) == upstream
        published, rendered = case.origin_render(PROJECT, tmp_path / "oracle")
        assert published == rendered and y_rdir.name[:8] in row_ids(published)

    def test_three_unpushed_local_commits_each_conflicting_are_all_resolved(
        self, race, monkeypatch, capsys, tmp_path
    ):
        case = race
        x_repo, y_repo = case.clones[0], case.clones[1]
        x_rdir, _ = start_session(monkeypatch, x_repo)
        x_identity = session_identity()
        original = git_ops.run_git_command

        def push_unavailable(cmd, cwd, check=True):
            return (1, "push unavailable") if cmd[0] == "push" else original(cmd, cwd, check)

        # Three durability pushes whose push leg fails leave three local
        # commits, each rewriting the own row; only then does the other
        # session land, so every one of them replays onto a changed README.
        with monkeypatch.context() as offline:
            offline.setattr(git_ops, "run_git_command", push_unavailable)
            for phase in ("spec", "plan", "design"):
                assert cli.cmd_state(state_args(phase=phase)) == 0
        assert git(x_repo, "rev-list", "--count", "origin/main..HEAD").stdout.strip() == "3"
        y_rdir, _ = start_session(monkeypatch, y_repo)
        assert cli.cmd_commit("Y") == 0
        resume_session(monkeypatch, x_repo, x_identity)
        capsys.readouterr()

        assert cli.cmd_commit("retry") == 0

        captured = capsys.readouterr()
        assert captured.out.count(RESOLVED) == 1 and PULL_FAILED not in captured.err
        _assert_clean_resolution(x_repo)
        assert git(x_repo, "rev-list", "--count", "origin/main..HEAD").stdout.strip() == "0"
        subjects = git(case.origin, "log", "--format=%s", "main").stdout.splitlines()
        assert [s for s in subjects if s.startswith("run-state: chat phase=")] == [
            "run-state: chat phase=design", "run-state: chat phase=plan", "run-state: chat phase=spec"]
        published, rendered = case.origin_render(PROJECT, tmp_path / "oracle")
        assert published == rendered
        assert row_ids(published) == {uid_for(1)[:8], y_rdir.name[:8], x_rdir.name[:8]}
        assert "phase design" in next(line for line in published.splitlines() if x_rdir.name[:8] in line)

    def test_a_dirty_tracked_sibling_file_survives_the_resolved_round(
        self, race, monkeypatch, capsys, tmp_path
    ):
        case = race
        _share_file(case, NOTES, "recorded\n")
        _first_publication(case, monkeypatch)
        x_repo, y_repo = case.clones[0], case.clones[1]
        start_session(monkeypatch, y_repo)
        assert cli.cmd_commit("Y") == 0
        dirty = x_repo / NOTES
        dirty.write_text("uncommitted log\n", encoding="utf-8")
        start_session(monkeypatch, x_repo)
        capsys.readouterr()

        assert cli.cmd_commit("X") == 0

        assert RESOLVED in capsys.readouterr().out
        assert dirty.read_text(encoding="utf-8") == "uncommitted log\n"
        assert git(x_repo, "status", "--porcelain").stdout == f" M {NOTES}\n"
        _assert_clean_resolution(x_repo)
        published, rendered = case.origin_render(PROJECT, tmp_path / "oracle")
        assert published == rendered

    def test_an_exported_failing_editor_does_not_stop_the_round(self, race, monkeypatch, capsys, tmp_path):
        case = race
        _first_publication(case, monkeypatch)
        b_repo = case.clones[0]
        b_rdir, _ = start_session(monkeypatch, b_repo)
        monkeypatch.setenv("GIT_EDITOR", "false")
        monkeypatch.setenv("GIT_SEQUENCE_EDITOR", "false")
        capsys.readouterr()

        assert cli.cmd_commit("B") == 0

        assert RESOLVED in capsys.readouterr().out
        _assert_clean_resolution(b_repo)
        published, rendered = case.origin_render(PROJECT, tmp_path / "oracle")
        assert published == rendered and b_rdir.name[:8] in row_ids(published)

    def test_an_untracked_sibling_record_is_not_rendered_during_the_paused_rebase(
        self, race, monkeypatch, capsys, tmp_path
    ):
        case = race
        a_rdir = _first_publication(case, monkeypatch)
        b_repo = case.clones[0]
        sibling = RecordSpec(uid_for(9), "review", stamp(5), stamp(5, 1), name="sibling")
        case.clone_tree(b_repo).add(PROJECT, sibling)
        b_rdir, _ = start_session(monkeypatch, b_repo)
        capsys.readouterr()

        assert cli.cmd_commit("B") == 0

        assert RESOLVED in capsys.readouterr().out
        published, rendered = case.origin_render(PROJECT, tmp_path / "oracle")
        assert published == rendered
        assert row_ids(published) == {uid_for(1)[:8], a_rdir.name[:8], b_rdir.name[:8]}
        assert "sibling" not in (b_repo / README).read_text(encoding="utf-8")
        assert git(b_repo, "status", "--porcelain", "--", f"{PROJECT}/_runs/review").stdout.startswith("??")
        _assert_clean_resolution(b_repo)


# ---------------------------------------------------------------------------
# Everything else aborts exactly as before
# ---------------------------------------------------------------------------


def _assert_aborted_like_before(case, repo: Path, captured, *, subject: str) -> None:
    """The push-safety contract for a conflict this command cannot resolve."""
    assert PULL_FAILED in captured.err
    assert "Rebase aborted; local commits retained" in captured.err
    assert RESOLVED not in captured.out
    assert "Successfully" not in captured.out
    assert not rebase_state(repo)
    assert _unmerged(repo) == ""
    assert git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip() == "main"
    assert git(repo, "log", "-1", "--format=%s").stdout.strip() == subject


class TestOtherConflictsAbort:
    def test_hand_text_edited_on_both_sides_aborts_and_retains_the_local_commit(
        self, race, monkeypatch, capsys
    ):
        case = race
        _first_publication(case, monkeypatch)
        x_repo, y_repo = case.clones[0], case.clones[1]
        for repo in (x_repo, y_repo):
            git(repo, "pull", "-q", "--rebase")
        y_readme = y_repo / README
        y_readme.write_text(_with_hand_text(y_readme.read_text(encoding="utf-8"), "Upstream prose."))
        _push_by_hand(y_repo, "upstream prose")
        upstream = case.origin_file(README)
        x_readme = x_repo / README
        x_readme.write_text(_with_hand_text(x_readme.read_text(encoding="utf-8"), "Local prose."))
        start_session(monkeypatch, x_repo)
        capsys.readouterr()

        assert cli.cmd_commit("save") != 0

        _assert_aborted_like_before(case, x_repo, capsys.readouterr(), subject="save")
        local = x_readme.read_text(encoding="utf-8")
        assert local.startswith(f"# {PROJECT}\n\nLocal prose.\n\n")
        assert git(x_repo, "show", f"HEAD:{README}").stdout == local
        assert case.origin_file(README) == upstream
        before = case.head(x_repo)
        assert cli.cmd_commit("save") != 0
        assert case.head(x_repo) == before
        assert "Successfully" not in capsys.readouterr().out

    def test_a_newer_format_upstream_block_aborts_and_stands(self, race, monkeypatch, capsys):
        case = race
        _first_publication(case, monkeypatch)
        x_repo, y_repo = case.clones[0], case.clones[1]
        for repo in (x_repo, y_repo):
            git(repo, "pull", "-q", "--rebase")
        y_readme = y_repo / README
        y_readme.write_text(_newer_format_readme(y_readme.read_text(encoding="utf-8")), encoding="utf-8")
        _push_by_hand(y_repo, "a newer runtime's block")
        upstream = case.origin_file(README)
        assert pi.block_format(pi.extract_block(upstream)) == pi.FORMAT + 1
        start_session(monkeypatch, x_repo)
        capsys.readouterr()

        assert cli.cmd_commit("save") != 0

        _assert_aborted_like_before(case, x_repo, capsys.readouterr(), subject="save")
        assert case.origin_file(README) == upstream

    def test_a_readme_plus_record_conflict_aborts(self, race, monkeypatch, capsys):
        case = race
        _first_publication(case, monkeypatch)
        x_repo, y_repo = case.clones[0], case.clones[1]
        for repo in (x_repo, y_repo):
            git(repo, "pull", "-q", "--rebase")
        seeded_rel = f"{PROJECT}/_runs/chat/{uid_for(1)}/state.yaml"
        for repo, goal in ((y_repo, "edited upstream"), (x_repo, "edited locally")):
            state = yaml.safe_load((repo / seeded_rel).read_text(encoding="utf-8"))
            state["goal"] = goal
            (repo / seeded_rel).write_text(yaml.safe_dump(state, sort_keys=False), encoding="utf-8")
        start_session(monkeypatch, y_repo)
        assert cli.cmd_commit("Y") == 0
        upstream = case.origin_file(README)
        start_session(monkeypatch, x_repo)
        capsys.readouterr()

        assert cli.cmd_commit("save") != 0

        _assert_aborted_like_before(case, x_repo, capsys.readouterr(), subject="save")
        assert case.origin_file(README) == upstream
        assert case.origin_file(seeded_rel) == (y_repo / seeded_rel).read_text(encoding="utf-8")

    def test_an_exhausted_bound_aborts(self, race, monkeypatch, capsys):
        case = race
        x_repo, y_repo = case.clones[0], case.clones[1]
        start_session(monkeypatch, x_repo)
        x_identity = session_identity()
        original = git_ops.run_git_command

        def push_unavailable(cmd, cwd, check=True):
            return (1, "push unavailable") if cmd[0] == "push" else original(cmd, cwd, check)

        with monkeypatch.context() as offline:
            offline.setattr(git_ops, "run_git_command", push_unavailable)
            for phase in ("spec", "plan", "design"):
                assert cli.cmd_state(state_args(phase=phase)) == 0
        assert git(x_repo, "rev-list", "--count", "origin/main..HEAD").stdout.strip() == "3"
        start_session(monkeypatch, y_repo)
        assert cli.cmd_commit("Y") == 0
        upstream = case.origin_file(README)
        resume_session(monkeypatch, x_repo, x_identity)
        monkeypatch.setattr(git_ops, "INDEX_CONFLICT_ROUNDS", ONE_ROUND)
        capsys.readouterr()

        assert cli.cmd_commit("retry") != 0

        _assert_aborted_like_before(case, x_repo, capsys.readouterr(), subject="run-state: chat phase=design")
        assert git(x_repo, "rev-list", "--count", "origin/main..HEAD").stdout.strip() == "3"
        assert case.origin_file(README) == upstream

    def test_a_clean_checkout_has_nothing_to_continue(self, race):
        assert git_ops._continue_past_index_conflicts(race.root, "work repo") is False
        assert not rebase_state(race.root)


# ---------------------------------------------------------------------------
# Around the resolved round: autostash and the bounded push loop
# ---------------------------------------------------------------------------


class TestAroundTheResolvedRound:
    def test_an_autostash_restore_failure_after_a_resolved_round_still_halts_with_the_stash(
        self, race, monkeypatch, capsys
    ):
        case = race
        _share_file(case, NOTES, "recorded\n")
        _first_publication(case, monkeypatch)
        x_repo, y_repo = case.clones[0], case.clones[1]
        for repo in (x_repo, y_repo):
            git(repo, "pull", "-q", "--rebase")
        (x_repo / NOTES).write_text("uncommitted log\n", encoding="utf-8")
        start_session(monkeypatch, y_repo)
        assert cli.cmd_commit("Y") == 0
        (y_repo / NOTES).write_text("peer log\n", encoding="utf-8")
        _push_by_hand(y_repo, "peer log")
        upstream = case.origin_file(README)
        start_session(monkeypatch, x_repo)
        commands = record_git_commands(monkeypatch)
        capsys.readouterr()

        assert cli.cmd_commit("X") != 0

        captured = capsys.readouterr()
        assert RESOLVED in captured.out and PULL_FAILED not in captured.err
        assert not any(cmd[0] == "push" for cmd in commands)
        assert NOTES in _unmerged(x_repo)
        assert "autostash" in git(x_repo, "stash", "list").stdout
        assert "autostash restoration" in captured.err
        assert _stash(x_repo) in captured.err
        assert "Successfully" not in captured.out
        assert not rebase_state(x_repo)
        assert case.origin_file(README) == upstream

    def test_a_rejected_push_after_a_resolved_round_stays_inside_the_bounded_loop(
        self, race, monkeypatch, capsys
    ):
        case = race
        _first_publication(case, monkeypatch)
        x_repo, y_repo = case.clones[0], case.clones[1]
        for repo in (x_repo, y_repo):
            git(repo, "pull", "-q", "--rebase")
        start_session(monkeypatch, x_repo)
        races = 0

        def another_writer_lands_first(cmd, cwd):
            nonlocal races
            if cmd[0] == "push":
                races += 1
                _peer_inserts_a_row(y_repo, f"peer{races}")

        commands = record_git_commands(monkeypatch, another_writer_lands_first)
        capsys.readouterr()

        assert cli.cmd_commit("X") != 0

        captured = capsys.readouterr()
        assert races == git_ops.PUSH_RETRIES
        assert sum(cmd[0] == "push" for cmd in commands) == git_ops.PUSH_RETRIES
        assert captured.out.count(RESOLVED) == git_ops.PUSH_RETRIES - 1
        assert PULL_FAILED not in captured.err
        assert f"git push failed after {git_ops.PUSH_RETRIES} attempts" in captured.err
        assert "Successfully" not in captured.out
        assert not rebase_state(x_repo) and _unmerged(x_repo) == ""
        assert git(x_repo, "log", "-1", "--format=%s").stdout.strip() == "X"
        assert case.origin_file(README) == (y_repo / README).read_text(encoding="utf-8")

    def test_a_race_resolved_once_then_publishes(self, race, monkeypatch, capsys, tmp_path):
        case = race
        _first_publication(case, monkeypatch)
        x_repo, y_repo = case.clones[0], case.clones[1]
        for repo in (x_repo, y_repo):
            git(repo, "pull", "-q", "--rebase")
        x_rdir, _ = start_session(monkeypatch, x_repo)
        raced = False

        def one_peer_row(cmd, cwd):
            nonlocal raced
            if cmd[0] == "push" and not raced:
                raced = True
                _peer_inserts_a_row(y_repo, "peer")

        commands = record_git_commands(monkeypatch, one_peer_row)
        capsys.readouterr()

        assert cli.cmd_commit("X") == 0

        captured = capsys.readouterr()
        assert raced and sum(cmd[0] == "push" for cmd in commands) == 2
        assert captured.out.count(RESOLVED) == 1
        _assert_clean_resolution(x_repo)
        published, rendered = case.origin_render(PROJECT, tmp_path / "oracle")
        assert published == rendered
        assert x_rdir.name[:8] in row_ids(published) and "peer" not in published


# ---------------------------------------------------------------------------
# The index-path predicate
# ---------------------------------------------------------------------------


class TestIsIndexRel:
    def test_only_a_registered_projects_readme_counts(self, race):
        assert git_ops._is_index_rel(race.root, README)
        assert not git_ops._is_index_rel(race.root, "README.md")
        assert not git_ops._is_index_rel(race.root, f"{PROJECT}/_runs/chat/{uid_for(1)}/state.yaml")
        assert not git_ops._is_index_rel(race.root, "elsewhere/README.md")

    def test_a_legacy_tree_has_no_index_paths(self, tmp_path):
        (tmp_path / "git.example.com" / "group" / "project").mkdir(parents=True)
        assert not git_ops._is_index_rel(tmp_path, "git.example.com/group/project/README.md")


class TestTheRaceTestsRunSubprocessesWithinTheBudget:
    def test_git_helper_carries_the_cli_budget(self, monkeypatch, tmp_path):
        seen = {}
        real_run = subprocess.run

        def spy(cmd, **kwargs):
            seen.update(kwargs)
            return real_run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", spy)
        git(tmp_path, "--version")
        assert seen["timeout"] == timing.CLI
