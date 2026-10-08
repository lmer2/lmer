"""Persistence and publication failures against real, isolated Git repositories."""

import subprocess

import pytest

from tests.conftest import strip_lmer_env
from work_repo import git_ops, memory


def git(repo, *args, check=True):
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=15
    )
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


@pytest.fixture(params=["memory", "work"])
def publication(request, tmp_path, monkeypatch):
    strip_lmer_env(monkeypatch)
    monkeypatch.setenv("LMER_GATE_INFLIGHT_GUARD", "0")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test User")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test User")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    templates = tmp_path / "templates"
    templates.mkdir()
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(templates))
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    repo = tmp_path / "work"
    git(tmp_path, "clone", str(origin), str(repo))
    rel = "git.example.com/group/project/" + (
        "memory" if request.param == "memory" else "develop/issue-404"
    )
    target = repo / rel / "fact.md"
    target.parent.mkdir(parents=True)
    target.write_text("base\n")
    git(repo, "add", "--", str(target))
    git(repo, "commit", "-m", "seed")
    git(repo, "push", "-u", "origin", "main")
    peer = tmp_path / "peer"
    git(tmp_path, "clone", str(origin), str(peer))
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "fact.md").write_text("base\n")
    for key, value in {
        "LMER_WORK_REPO_PATH": repo,
        "LMER_REPO_HOST": "git.example.com",
        "LMER_REPO_PROJECT": "group/project",
        "LMER_TASK": "develop",
        "LMER_TASK_TARGET": "https://git.example.com/group/project/-/issues/404",
        "LMER_PERSIST_AGENT_MEMORY": "1",
        "LMER_AGENT_MEMORY_DIR": agent,
    }.items():
        monkeypatch.setenv(key, str(value))

    class Publication:
        def write(self, text):
            self.source.write_text(text)

        def publish(self):
            if request.param == "memory":
                return memory.persist_memory("save")
            return git_ops.commit_work_changes("save")

        def peer_commit(self, path, text):
            file = peer / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(text)
            git(peer, "add", "--", path)
            git(peer, "commit", "-m", "peer update")
            git(peer, "push")

        def conflict_at_the_rebase(self, path, text):
            """Land a peer commit that the post-commit rebase must meet.

            Memory persist fast-forwards to upstream before it merges (#369
            gap (b)), so a peer commit made beforehand is integrated, not
            conflicted; it has to land after that pull, just before the
            local commit, to reach the rebase."""
            if request.param != "memory":
                self.peer_commit(path, text)
                return
            real_commit = memory.commit_work_path
            raced = []

            def commit_after_the_peer(*args, **kwargs):
                if not raced:
                    raced.append(True)
                    self.peer_commit(path, text)
                return real_commit(*args, **kwargs)

            monkeypatch.setattr(memory, "commit_work_path", commit_after_the_peer)

        def conflict_copies(self):
            return sorted(p.name for p in agent.iterdir() if ".conflict-" in p.name)

    case = Publication()
    case.kind = request.param
    case.repo, case.origin, case.peer = repo, origin, peer
    case.rel = f"{rel}/fact.md"
    case.target = target
    case.source = agent / "fact.md" if request.param == "memory" else target
    return case


def record_commands(monkeypatch, intercept=None):
    original = git_ops.run_git_command
    commands = []

    def run(cmd, cwd, check=True):
        commands.append(cmd)
        if intercept is not None:
            result = intercept(cmd, cwd)
            if result is not None:
                return result
        return original(cmd, cwd, check)

    monkeypatch.setattr(git_ops, "run_git_command", run)
    return commands


@pytest.mark.parametrize("state", [
    "detached", "rebase", "merge-staged", "unmerged",
    "cherry-pick", "revert", "sequencer",
])
def test_unsafe_entry_cannot_write_or_commit(publication, monkeypatch, capsys, state):
    case = publication
    if state == "detached":
        git(case.repo, "checkout", "--detach")
    else:
        case.target.write_text("local conflict\n")
        git(case.repo, "add", "--", case.rel)
        git(case.repo, "commit", "-m", "local conflict")
        case.peer_commit(case.rel, "remote conflict\n")
        git(case.repo, "fetch")
        if state == "sequencer":
            case.peer_commit("second.txt", "second change\n")
            git(case.repo, "fetch")
        args = {
            "rebase": ("rebase", "origin/main"),
            "cherry-pick": ("cherry-pick", "origin/main"),
            "revert": ("revert", "--no-edit", "origin/main"),
            "sequencer": ("cherry-pick", "origin/main^", "origin/main"),
        }.get(state, ("merge", "--no-edit", "origin/main"))
        assert git(case.repo, *args, check=False).returncode != 0
        if state == "merge-staged":
            git(case.repo, "add", "--", case.rel)
        elif state == "unmerged":
            (case.repo / ".git/MERGE_HEAD").unlink()
        elif state in {"cherry-pick", "revert", "sequencer"}:
            case.target.write_text("staged resolution\n")
            git(case.repo, "add", "--", case.rel)
            if state == "sequencer":
                (case.repo / ".git/CHERRY_PICK_HEAD").unlink()
    case.write("new local memory\n")
    before = git(case.repo, "rev-parse", "HEAD").stdout
    index = (case.repo / ".git/index").read_bytes()
    tree = case.target.read_bytes()
    commands = record_commands(monkeypatch)

    assert case.publish() != 0

    assert git(case.repo, "rev-parse", "HEAD").stdout == before
    assert (case.repo / ".git/index").read_bytes() == index
    assert case.target.read_bytes() == tree
    assert not any(cmd[0] in {"add", "commit", "push"} for cmd in commands)
    captured = capsys.readouterr()
    assert "Successfully" not in captured.out
    if state in {"cherry-pick", "revert", "sequencer"}:
        marker = {"cherry-pick": "CHERRY_PICK_HEAD", "revert": "REVERT_HEAD"}.get(state, state)
        assert marker in captured.err


@pytest.mark.parametrize("marker", ["<<<<<<< local", "=======", ">>>>>>> remote", "||||||| base"])
def test_staged_conflict_markers_block_commit(publication, capsys, marker):
    case = publication
    before = git(case.repo, "rev-parse", "HEAD").stdout
    case.write(f"fact\n{marker}\nunfinished\n")

    assert case.publish() != 0

    assert git(case.repo, "rev-parse", "HEAD").stdout == before
    assert git(case.origin, "show", f"main:{case.rel}").stdout == "base\n"
    captured = capsys.readouterr()
    assert "conflict marker" in captured.err.lower()
    assert "Successfully" not in captured.out


def test_marker_gate_checks_the_index_including_other_paths(publication):
    case = publication
    other = case.repo / "already-staged.md"
    other.write_text("<<<<<<< local\n=======\n>>>>>>> remote\n")
    git(case.repo, "add", "--", other.name)
    other.write_text("resolved only in worktree\n")
    before = git(case.repo, "rev-parse", "HEAD").stdout
    case.write("legitimate new fact\n")

    assert case.publish() != 0
    assert git(case.repo, "rev-parse", "HEAD").stdout == before


def test_rebase_conflict_aborts_without_push_and_retries_safely(publication, monkeypatch, capsys):
    case = publication
    case.conflict_at_the_rebase(case.rel, "remote conflict\n")
    case.write("local conflict\n")
    commands = record_commands(monkeypatch)

    assert case.publish() != 0

    assert not any(cmd[0] == "push" for cmd in commands)
    assert git(case.repo, "symbolic-ref", "--short", "HEAD").stdout.strip() == "main"
    assert not (case.repo / ".git/rebase-merge").exists()
    assert not (case.repo / ".git/rebase-apply").exists()
    if case.kind == "memory":
        # The memory contract (#369 gap (b)): the session's version survives
        # as a conflict copy, and its commit is dropped so the shared
        # checkout stays publishable; the retry is pinned in
        # tests/test_work_repo_memory_merge.py.
        assert case.target.read_text() == "base\n"
        assert git(case.repo, "rev-list", "@{upstream}..HEAD").stdout == ""
        assert case.conflict_copies(), "no .conflict- copy was written"
        assert git(case.origin, "show", f"main:{case.rel}").stdout == "remote conflict\n"
        assert "Successfully" not in capsys.readouterr().out
        return
    assert case.target.read_text() == "local conflict\n"
    assert git(case.repo, "log", "-1", "--format=%s").stdout.strip() == "save"
    assert git(case.origin, "show", f"main:{case.rel}").stdout == "remote conflict\n"
    before = git(case.repo, "rev-parse", "HEAD").stdout
    assert case.publish() != 0
    assert git(case.repo, "rev-parse", "HEAD").stdout == before
    assert "Successfully" not in capsys.readouterr().out


def test_push_race_rebases_and_publishes_both_writers(publication, monkeypatch):
    case = publication
    case.write("local fact\n")
    raced = False

    def race(cmd, cwd):
        nonlocal raced
        if cmd[0] == "push" and not raced:
            raced = True
            case.peer_commit("peer.md", "concurrent fact\n")

    commands = record_commands(monkeypatch, race)
    assert case.publish() == 0
    assert raced
    pushes = [i for i, cmd in enumerate(commands) if cmd[0] == "push"]
    assert len(pushes) == 2
    assert any(cmd[:2] == ["pull", "--rebase"] for cmd in commands[pushes[0] + 1:pushes[1]])
    assert git(case.origin, "show", f"main:{case.rel}").stdout == "local fact\n"
    assert git(case.origin, "show", "main:peer.md").stdout == "concurrent fact\n"
    assert git(case.repo, "merge-base", "--is-ancestor", "HEAD", "origin/main").returncode == 0


def test_successful_push_status_is_not_proof(publication, monkeypatch, capsys):
    case = publication
    case.write("not published\n")

    def dishonest_push(cmd, cwd):
        if cmd[0] == "push":
            return 0, ""

    commands = record_commands(monkeypatch, dishonest_push)
    assert case.publish() != 0
    assert any(cmd[0] == "push" for cmd in commands)
    assert git(case.origin, "show", f"main:{case.rel}").stdout == "base\n"
    assert "Successfully" not in capsys.readouterr().out


def test_retry_publishes_existing_commit_without_new_file_changes(publication, monkeypatch):
    case = publication
    case.write("saved locally\n")
    original = git_ops.run_git_command

    def fail_push(cmd, cwd, check=True):
        if cmd[0] == "push":
            return 1, "push unavailable"
        return original(cmd, cwd, check)

    monkeypatch.setattr(git_ops, "run_git_command", fail_push)
    assert case.publish() != 0
    saved = git(case.repo, "rev-parse", "HEAD").stdout.strip()
    assert git(case.repo, "status", "--porcelain").stdout == ""
    monkeypatch.setattr(git_ops, "run_git_command", original)

    assert case.publish() == 0

    assert git(case.repo, "rev-parse", "HEAD").stdout.strip() == saved
    assert git(case.origin, "show", f"main:{case.rel}").stdout == "saved locally\n"


def test_remote_successor_is_valid_publication(publication, monkeypatch):
    case = publication
    case.write("published fact\n")
    original = git_ops.run_git_command
    pushed = []

    def successor(cmd, cwd, check=True):
        result = original(cmd, cwd, check)
        if cmd[0] == "push" and result[0] == 0:
            pushed.append(git(case.repo, "rev-parse", "HEAD").stdout.strip())
            git(case.peer, "pull", "--rebase")
            case.peer_commit("successor.md", "later fact\n")
        return result

    monkeypatch.setattr(git_ops, "run_git_command", successor)
    assert case.publish() == 0
    assert len(pushed) == 1
    assert git(case.origin, "rev-parse", "main").stdout.strip() != pushed[0]
    assert git(case.origin, "merge-base", "--is-ancestor", pushed[0], "main").returncode == 0


@pytest.mark.parametrize("failure", ["fetch", "deleted-remote", "moved-head"])
def test_post_push_verification_cannot_use_stale_evidence(
    publication, monkeypatch, capsys, failure
):
    case = publication
    case.write("new fact\n")
    original = git_ops.run_git_command
    pushed = False

    def intervene(cmd, cwd, check=True):
        nonlocal pushed
        if pushed and cmd[0] == "fetch" and failure == "fetch":
            return 1, "verification transport unavailable"
        if cmd[0] == "push" and failure == "moved-head":
            pushed = True
            git(case.repo, "reset", "--hard", "HEAD^")
            return 0, ""
        result = original(cmd, cwd, check)
        if cmd[0] == "push" and result[0] == 0:
            pushed = True
            if failure == "deleted-remote":
                git(case.origin, "update-ref", "-d", "refs/heads/main")
        return result

    monkeypatch.setattr(git_ops, "run_git_command", intervene)
    assert case.publish() != 0
    assert pushed
    assert "Successfully" not in capsys.readouterr().out


def test_race_that_conflicts_stops_before_a_second_push(publication, monkeypatch):
    case = publication
    case.write("local fact\n")
    raced = False

    def race(cmd, cwd):
        nonlocal raced
        if cmd[0] == "push" and not raced:
            raced = True
            case.peer_commit(case.rel, "conflicting peer fact\n")

    commands = record_commands(monkeypatch, race)
    assert case.publish() != 0
    assert sum(cmd[0] == "push" for cmd in commands) == 1
    assert git(case.repo, "symbolic-ref", "--short", "HEAD").stdout.strip() == "main"
    if case.kind == "memory":
        # Memory drops its unpublishable commit and keeps a conflict copy.
        assert case.target.read_text() == "base\n"
        assert case.conflict_copies(), "no .conflict- copy was written"
        return
    assert case.target.read_text() == "local fact\n"


def test_races_exhaust_the_bounded_push_loop(publication, monkeypatch, capsys):
    case = publication
    case.write("local fact\n")
    races = 0

    def race(cmd, cwd):
        nonlocal races
        if cmd[0] == "push":
            races += 1
            case.peer_commit(f"peer-{races}.md", "concurrent fact\n")

    record_commands(monkeypatch, race)
    assert case.publish() != 0
    assert races == git_ops.PUSH_RETRIES
    assert "Successfully" not in capsys.readouterr().out
    assert git(case.origin, "show", f"main:{case.rel}").stdout == "base\n"


def test_retry_of_last_file_deletion_publishes_existing_commit(publication, monkeypatch):
    case = publication
    if case.source != case.target:
        assert memory.restore_memory() == 0
    case.source.unlink()
    original = git_ops.run_git_command

    def fail_push(cmd, cwd, check=True):
        return (1, "push unavailable") if cmd[0] == "push" else original(cmd, cwd, check)

    monkeypatch.setattr(git_ops, "run_git_command", fail_push)
    assert case.publish() != 0
    assert not case.target.exists()
    assert git(case.origin, "cat-file", "-e", f"main:{case.rel}").returncode == 0
    monkeypatch.setattr(git_ops, "run_git_command", original)

    assert case.publish() == 0
    assert git(case.origin, "cat-file", "-e", f"main:{case.rel}", check=False).returncode != 0


def test_clean_published_target_leaves_unrelated_dirty_files_alone(publication, monkeypatch):
    case = publication
    dirty = case.repo / "unrelated.txt"
    dirty.write_text("unsaved work\n")
    before = git(case.repo, "rev-parse", "HEAD").stdout
    commands = record_commands(monkeypatch)
    assert case.publish() == 0
    assert git(case.repo, "rev-parse", "HEAD").stdout == before
    assert dirty.read_text() == "unsaved work\n"
    assert not any(cmd[0] in {"commit", "pull", "push"} for cmd in commands)
    assert any(cmd[0] == "fetch" for cmd in commands)


def test_staged_gate_does_not_impose_whitespace_policy(publication):
    publication.write("fact with trailing space \n\n")
    assert publication.publish() == 0


def test_detached_linked_worktree_is_refused(publication, monkeypatch):
    case = publication
    linked = case.repo.parent / "linked"
    git(case.repo, "worktree", "add", "--detach", str(linked))
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(linked))
    if case.source == case.target:
        case.source = linked / case.rel
    case.write("local change\n")
    before = git(linked, "rev-parse", "HEAD").stdout
    commands = record_commands(monkeypatch)
    assert case.publish() != 0
    assert git(linked, "rev-parse", "HEAD").stdout == before
    assert not any(cmd[0] in {"add", "commit", "push"} for cmd in commands)


def test_publish_preserves_unrelated_tracked_edits(publication):
    case = publication
    case.peer_commit("run/log.txt", "recorded\n")
    git(case.repo, "pull", "--rebase")
    dirty = case.repo / "run/log.txt"
    dirty.write_text("uncommitted log\n")
    case.write("new memory\n")

    assert case.publish() == 0
    assert dirty.read_text() == "uncommitted log\n"
    assert git(case.repo, "status", "--porcelain").stdout == " M run/log.txt\n"
    assert git(case.origin, "show", f"main:{case.rel}").stdout == "new memory\n"
    assert git(case.origin, "show", "main:run/log.txt").stdout == "recorded\n"
    assert git(case.repo, "stash", "list").stdout == ""


def test_marker_refusal_restores_prior_index_and_allows_other_publication(publication):
    case = publication
    staged = case.repo / "previously-staged.txt"
    staged.write_text("staged version\n")
    git(case.repo, "add", "--", staged.name)
    staged.write_text("unstaged version\n")
    before = git(case.repo, "diff", "--cached", "--binary").stdout
    case.write("Heading\n=======\n")

    assert case.publish() != 0
    assert git(case.repo, "diff", "--cached", "--binary").stdout == before
    assert staged.read_text() == "unstaged version\n"
    safe = case.repo / "safe.txt"
    safe.write_text("publishable\n")
    assert git_ops.commit_work_path([safe.name], "safe") == 0
    assert git(case.origin, "show", "main:safe.txt").stdout == "publishable\n"
    assert git(case.origin, "show", f"main:{case.rel}").stdout == "base\n"


def test_push_and_verification_use_same_upstream_despite_push_config(publication):
    case = publication
    publish = case.repo.parent / "publish.git"
    git(case.repo.parent, "clone", "--bare", str(case.origin), str(publish))
    git(case.repo, "remote", "add", "publish", str(publish))
    git(case.repo, "config", "remote.pushDefault", "publish")
    git(case.repo, "config", "branch.main.pushRemote", "publish")
    git(case.repo, "config", "remote.origin.push", "HEAD:refs/heads/other")
    case.write("new memory\n")

    assert case.publish() == 0
    assert git(case.origin, "show", f"main:{case.rel}").stdout == "new memory\n"
    assert git(publish, "show", f"main:{case.rel}").stdout == "base\n"
    assert git(case.origin, "show-ref", "--verify", "refs/heads/other", check=False).returncode != 0


def test_status_failure_restores_index(publication, monkeypatch, capsys):
    case = publication
    before = (case.repo / ".git/index").read_bytes()
    case.write("new memory\n")
    record_commands(monkeypatch, lambda cmd, cwd: (128, "status unavailable") if cmd[0] == "status" else None)

    assert case.publish() != 0
    assert (case.repo / ".git/index").read_bytes() == before
    assert "status unavailable" in capsys.readouterr().err


def test_rebase_conflict_restores_unrelated_tracked_edits(publication, monkeypatch):
    case = publication
    case.peer_commit("run/log.txt", "recorded\n")
    git(case.repo, "pull", "--rebase")
    dirty = case.repo / "run/log.txt"
    dirty.write_text("uncommitted log\n")
    case.conflict_at_the_rebase(case.rel, "remote conflict\n")
    case.write("local conflict\n")
    commands = record_commands(monkeypatch)

    assert case.publish() != 0
    assert ["pull", "--rebase", "--autostash", "origin", "refs/heads/main"] in commands
    assert ["rebase", "--abort"] in commands
    assert dirty.read_text() == "uncommitted log\n"
    # Memory drops its unpublishable commit; work keeps it.
    expected = "base\n" if case.kind == "memory" else "local conflict\n"
    assert case.target.read_text() == expected
    assert git(case.repo, "stash", "list").stdout == ""
    assert git(case.repo, "ls-files", "--unmerged").stdout == ""
    assert not (case.repo / ".git/rebase-merge").exists()


def test_autostash_restore_conflict_stops_publication(publication, monkeypatch, capsys):
    case = publication
    case.peer_commit("run/log.txt", "recorded\n")
    git(case.repo, "pull", "--rebase")
    (case.repo / "run/log.txt").write_text("uncommitted log\n")
    case.peer_commit("run/log.txt", "peer log\n")
    case.write("new memory\n")
    commands = record_commands(monkeypatch)

    assert case.publish() != 0
    assert not any(cmd[0] == "push" for cmd in commands)
    assert "run/log.txt" in git(case.repo, "ls-files", "--unmerged").stdout
    assert "autostash" in git(case.repo, "stash", "list").stdout
    assert git(case.origin, "show", f"main:{case.rel}").stdout == "base\n"
    captured = capsys.readouterr()
    assert "autostash restoration" in captured.err
    assert git(case.repo, "rev-parse", "refs/stash").stdout.strip() in captured.err
    assert "Successfully" not in captured.out


@pytest.mark.parametrize("existing_stash", [False, True])
def test_unapplied_autostash_without_conflicts_stops_publication(
    publication, monkeypatch, capsys, existing_stash,
):
    case = publication
    case.peer_commit("run/log.txt", "recorded\n")
    git(case.repo, "pull", "--rebase")
    dirty = case.repo / "run/log.txt"
    if existing_stash:
        dirty.write_text("older saved edit\n")
        git(case.repo, "stash", "push", "-m", "existing stash")
    dirty.write_text("uncommitted log\n")
    case.peer_commit("run/log.txt", "peer log\n")
    case.write("new memory\n")
    hook = case.repo / ".git/hooks/post-rewrite"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text('#!/bin/sh\n: > "$(git rev-parse --git-path index.lock)"\n')
    hook.chmod(0o755)
    commands = record_commands(monkeypatch)

    assert case.publish() != 0
    assert not any(cmd[0] == "push" for cmd in commands)
    assert git(case.repo, "ls-files", "--unmerged").stdout == ""
    assert dirty.read_text() == "peer log\n"
    stash = git(case.repo, "rev-parse", "refs/stash").stdout.strip()
    assert git(case.repo, "show", f"{stash}:run/log.txt").stdout == "uncommitted log\n"
    assert len(git(case.repo, "stash", "list").stdout.splitlines()) == 1 + existing_stash
    assert git(case.origin, "show", f"main:{case.rel}").stdout == "base\n"
    captured = capsys.readouterr()
    assert "autostash restoration" in captured.err
    assert stash in captured.err
    assert "Successfully" not in captured.out


def test_successful_autostash_preserves_existing_stash(publication):
    case = publication
    case.peer_commit("run/log.txt", "recorded\n")
    git(case.repo, "pull", "--rebase")
    dirty = case.repo / "run/log.txt"
    dirty.write_text("older saved edit\n")
    git(case.repo, "stash", "push", "-m", "existing stash")
    stash = git(case.repo, "rev-parse", "refs/stash").stdout
    dirty.write_text("uncommitted log\n")
    case.peer_commit("peer.txt", "peer update\n")
    case.write("new memory\n")

    assert case.publish() == 0
    assert dirty.read_text() == "uncommitted log\n"
    assert git(case.repo, "rev-parse", "refs/stash").stdout == stash
    assert git(case.origin, "show", f"main:{case.rel}").stdout == "new memory\n"


@pytest.mark.parametrize("failed_probe", [1, 2])
def test_stash_inspection_failure_stops_publication(publication, monkeypatch, capsys, failed_probe):
    case = publication
    case.write("new memory\n")
    probes = 0

    def fail_probe(cmd, cwd):
        nonlocal probes
        if cmd == ["rev-parse", "--verify", "--quiet", "refs/stash"]:
            probes += 1
            if probes == failed_probe:
                return 128, "stash ref unavailable"

    commands = record_commands(monkeypatch, fail_probe)
    assert case.publish() != 0
    assert not any(cmd[0] == "push" for cmd in commands)
    assert sum(cmd[0] == "pull" for cmd in commands) == failed_probe - 1
    assert git(case.origin, "show", f"main:{case.rel}").stdout == "base\n"
    assert "stash ref unavailable" in capsys.readouterr().err


def test_abort_failure_is_reported_and_next_call_refuses_state(publication, monkeypatch, capsys):
    case = publication
    case.conflict_at_the_rebase(case.rel, "remote conflict\n")
    case.write("local conflict\n")
    commands = record_commands(monkeypatch, lambda cmd, cwd: (128, "abort unavailable") if cmd == ["rebase", "--abort"] else None)

    assert case.publish() != 0
    assert "git rebase --abort failed" in capsys.readouterr().err
    assert (case.repo / ".git/rebase-merge").exists()
    commands.clear()
    before = git(case.repo, "rev-parse", "HEAD").stdout
    assert case.publish() != 0
    assert git(case.repo, "rev-parse", "HEAD").stdout == before
    assert not any(cmd[0] in {"add", "commit", "push"} for cmd in commands)


def test_unborn_marker_refusal_leaves_no_index(publication):
    case = publication
    git(case.repo, "checkout", "--orphan", "unstarted")
    git(case.repo, "rm", "--cached", "-r", "--", ".")
    index = case.repo / ".git/index"
    index.unlink()
    case.write("=======\n")

    assert case.publish() != 0
    assert not index.exists()
    assert git(case.repo, "rev-parse", "--verify", "HEAD", check=False).returncode != 0
    assert git(case.origin, "show", f"main:{case.rel}").stdout == "base\n"


def test_unreadable_index_stops_before_staging(publication, monkeypatch, capsys):
    case = publication
    case.write("new memory\n")

    def unreadable(cmd, cwd):
        if cmd == ["rev-parse", "--git-path", "index"]:
            return 128, "index unavailable"

    commands = record_commands(monkeypatch, unreadable)
    assert case.publish() != 0
    assert "index snapshot failed" in capsys.readouterr().err
    assert not any(cmd[0] in {"add", "commit", "push"} for cmd in commands)


def test_restore_failure_reports_and_preserves_existing_lock(publication, monkeypatch, capsys):
    case = publication
    case.write("=======\n")
    lock = case.repo / ".git/index.lock"

    def locked(cmd, cwd):
        if "--check" in cmd:
            lock.write_text("another Git operation\n")

    commands = record_commands(monkeypatch, locked)
    assert case.publish() != 0
    assert "index restoration failed" in capsys.readouterr().err
    assert lock.read_text() == "another Git operation\n"
    assert not any(cmd[0] in {"commit", "push"} for cmd in commands)
