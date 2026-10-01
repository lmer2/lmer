"""Real isolated Git publication and same-checkout transaction regressions."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
import yaml

from lmer_cli import gate_lock
from tests.test_schema_v2_release_claims import claim_clones, git  # noqa: F401
from tests.timing_budgets import BARRIER, CLI, QUIET, SETTLE, SHUTDOWN
from tests.test_schema_v2_sessions import session_tree, seed_launch  # noqa: F401
from tests.test_work_repo_memory_scopes import _memory_file, _index_line
from work_repo import cli, git_ops, memory, release_claims, run_state


def wait_for_file(path, timeout=SETTLE):
    """Poll for ``path``; a short ``timeout`` (QUIET) is an absence window."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.01)
    return False


def test_same_checkout_claims_serialize_read_replace_stage_publish(claim_clones, tmp_path):
    origin, root, _ = claim_clones
    script = tmp_path / "racer.py"
    script.write_text('''import json, os, sys, time
from pathlib import Path
from work_repo import cli, release_claims, session_record
name, barriers = sys.argv[1], Path(sys.argv[2])
session_record.start_launch()
real_read = cli.published_file
real_publish = cli.publish_overlay_commit
def wait(key):
    until = time.monotonic() + float(sys.argv[3])
    while not (barriers / key).exists():
        assert time.monotonic() < until, key
        time.sleep(.01)
def read(repo_path, commit, rel):
    present, text = real_read(repo_path, commit, rel)
    if rel.endswith("release.yaml") and not present:
        (barriers / (name + "-read")).touch()
        wait(name + "-replace")
    return present, text
def publish(repo_path, **kwargs):
    (barriers / (name + "-written")).touch()
    wait(name + "-commit")
    return real_publish(repo_path, **kwargs)
cli.published_file = read
cli.publish_overlay_commit = publish
rc = cli.cmd_release_claim()
(barriers / (name + "-result")).write_text(json.dumps({"rc": rc, "uid": os.environ["LMER_SESSION_ID"]}))
''')
    barriers = tmp_path / "barriers"
    barriers.mkdir()
    env = dict(os.environ, HOME=str(tmp_path / "home"))
    outputs = []
    children = []
    try:
        for name in ("a", "b"):
            handle = (tmp_path / f"{name}.txt").open("w")
            outputs.append(handle)
            children.append(subprocess.Popen([sys.executable, str(script), name, str(barriers), str(BARRIER)],
                                              env=env, stdout=handle, stderr=handle))
            if name == "a":
                assert wait_for_file(barriers / "a-read")
        raced = wait_for_file(barriers / "b-read", QUIET)
        (barriers / "a-replace").touch()
        assert wait_for_file(barriers / "a-written")
        if raced:
            (barriers / "b-replace").touch()
            assert wait_for_file(barriers / "b-written")
        (barriers / "a-commit").touch()
        assert wait_for_file(barriers / "a-result")
        (barriers / "b-replace").touch()
        (barriers / "b-commit").touch()
        for child in children:
            assert child.wait(timeout=CLI) == 0
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()
        for handle in outputs:
            handle.close()
    results = [json.loads((barriers / f"{name}-result").read_text()) for name in ("a", "b")]
    winners = [result["uid"] for result in results if result["rc"] == 0]
    assert len(winners) == 1, results
    assert yaml.safe_load(git(origin, "show", "main:widgets/_claims/release.yaml"))["holder"] == winners[0]


def test_failed_claim_preserves_unrelated_staged_unstaged_and_untracked_bytes(
    claim_clones, monkeypatch
):
    _, root, _ = claim_clones
    sentinel = root / "unrelated.md"
    sentinel.write_text("base\n")
    git(root, "add", "unrelated.md")
    git(root, "commit", "-m", "unrelated base")
    git(root, "push")
    sentinel.write_text("staged\n")
    git(root, "add", "unrelated.md")
    sentinel.write_text("unstaged\n")
    (root / "untracked.md").write_text("keep untracked\n")
    staged = git(root, "diff", "--cached", "--", "unrelated.md")
    unstaged = git(root, "diff", "--", "unrelated.md")
    seed_launch()
    monkeypatch.setattr(cli, "publish_overlay_commit",
                        lambda *a, **k: (cli.CLAIM_PUSH_ERROR, "simulated publication failure", ""))
    assert cli.cmd_release_claim() == 1
    assert git(root, "diff", "--cached", "--", "unrelated.md") == staged
    assert git(root, "diff", "--", "unrelated.md") == unstaged
    assert (root / "untracked.md").read_text() == "keep untracked\n"
    assert release_claims.read_claim()["holder"] is None
    assert not git(root, "diff", "--cached", "--", release_claims.claim_rel_path())


def _worktree_state(root):
    """Everything about the non-record worktree a transaction could disturb:
    bytes, symlink targets and the executable bit, per path."""
    state = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(".git/") or rel == ".git" or "/_runs/" in f"/{rel}":
            continue
        if path.is_symlink():
            state[rel] = ("symlink", os.readlink(path))
        elif path.is_dir():
            state[rel] = ("dir", None)
        else:
            state[rel] = ("exec" if os.access(path, os.X_OK) else "file", path.read_bytes())
    return state


def test_a_claim_cannot_disturb_anything_else_in_the_shared_checkout(claim_clones):
    """A work-repo checkout is shared: spawn-harness children run beside their
    parent against one /work, and a gate can leave a memory persist half-done.

    The transaction used to stash the whole checkout to get a clean tree for
    `pull --rebase`, which captured every sibling's uncommitted record and
    could strand them in the stash. It now publishes an object built in a
    temporary index, so the shapes below — a tracked file replaced by a
    directory, a local deletion, a symlink, an executable, a staged symlink,
    and a file both sides rewrote in the window — are simply none of its
    business. Each of them broke the stash restore in a different way.
    """
    origin, root, other = claim_clones
    (root / "shared.md").write_text("line one\nline two\n")
    (root / "collide.md").write_text("a file today\n")
    (root / "gone.md").write_text("here for now\n")
    (root / "run.sh").write_text("#!/bin/sh\necho hi\n")
    (root / "run.sh").chmod(0o755)
    (root / "link.md").symlink_to("shared.md")
    git(root, "add", "shared.md", "collide.md", "gone.md", "run.sh", "link.md")
    git(root, "commit", "-m", "unrelated base")
    git(root, "push")

    # Upstream rewrites the line this checkout also rewrote, during the window.
    git(other, "pull")
    (other / "shared.md").write_text("line one\nline two rewritten upstream\n")
    git(other, "add", "shared.md")
    git(other, "commit", "-m", "upstream rewrite")
    git(other, "push")

    (root / "shared.md").write_text("line one\nline two rewritten locally\n")
    git(root, "add", "shared.md")                      # staged, both sides changed
    (root / "collide.md").unlink()
    (root / "collide.md").mkdir()                      # file -> directory
    (root / "collide.md" / "inner.txt").write_text("now a directory\n")
    (root / "gone.md").unlink()                        # modify/delete
    (root / "run.sh").write_text("#!/bin/sh\necho changed\n")
    (root / "newlink").symlink_to("shared.md")
    git(root, "add", "newlink")                        # a staged symlink
    (root / "pre-untracked.txt").write_text("keep me\n")
    launched, own, _ = seed_launch()
    sibling = own.parent / ("b" * 32)
    sibling.mkdir()
    (sibling / "log.yaml").write_text("- message: b-before-claim-window\n")
    (sibling / "events.jsonl").write_text('{"type": "run_seeded"}\n')

    before = _worktree_state(root)
    staged_before = git(root, "diff", "--cached", "--name-status")
    unstaged_before = git(root, "diff", "--name-status")

    assert cli.cmd_release_claim() == 0

    assert yaml.safe_load(git(origin, "show", "main:widgets/_claims/release.yaml"))["holder"] == launched["uid"]
    assert _worktree_state(root) == before, "a claim moved bytes that were not its own"
    assert git(root, "diff", "--cached", "--name-status") == staged_before
    assert git(root, "diff", "--name-status") == unstaged_before
    assert not git(root, "diff", "--name-only", "--diff-filter=U"), "an unmerged index would block every session"
    # Nothing was stashed, so nothing can be stranded in a stash.
    stash = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", "refs/stash"],
                           capture_output=True, text=True)
    assert stash.returncode != 0 and not stash.stdout.strip()
    assert (sibling / "log.yaml").read_text() == "- message: b-before-claim-window\n"
    assert (sibling / "events.jsonl").read_text() == '{"type": "run_seeded"}\n'


def test_a_claim_publishes_this_sessions_record_and_no_one_elses(claim_clones):
    """What the claim commit carries: the claim, and the claiming session's
    own record as the local branch holds it. A sibling's uncommitted record is
    not published by someone else's transaction."""
    origin, root, _ = claim_clones
    launched, own, _ = seed_launch()
    sibling = own.parent / ("c" * 32)
    sibling.mkdir()
    (sibling / "log.yaml").write_text("- message: not mine to publish\n")

    assert cli.cmd_release_claim() == 0

    published = set(git(origin, "ls-tree", "-r", "--name-only", "main").splitlines())
    own_rel = own.relative_to(root).as_posix()
    assert "widgets/_claims/release.yaml" in published
    assert any(name.startswith(f"{own_rel}/") for name in published), "the claiming record must be published"
    sibling_rel = sibling.relative_to(root).as_posix()
    assert not any(name.startswith(f"{sibling_rel}/") for name in published)
    # The published record is the one the local branch holds, so the next
    # ordinary rebase has nothing of this session's to merge.
    for name in (n for n in published if n.startswith(f"{own_rel}/")):
        assert git(origin, "show", f"main:{name}") == git(root, "show", f"HEAD:{name}")


@pytest.mark.parametrize("deferred", [True, False], ids=["gate", "prestage-failure"])
@pytest.mark.parametrize("action", ["forget", "revive"])
def test_pending_memory_transition_is_published_whole_by_ordinary_commit(
    claim_clones, monkeypatch, tmp_path, deferred, action
):
    origin, root, other = claim_clones
    seed_launch()
    directory = root / "widgets" / "_agents" / "memory"
    directory.mkdir(parents=True)
    key = "widgets/_agents/memory/fact.md"
    archived = "widgets/_archive/_agents/memory/fact.md"
    original = _memory_file("fact", "project")
    (root / key).write_text(original)
    (directory / "MEMORY.md").write_text(_index_line("fact", "fact.md"))
    git(root, "add", "widgets")
    git(root, "commit", "-m", "seed memory and execution")
    git(root, "push")
    if action == "revive":
        assert cli.cmd_memory("forget", None, argparse.ArgumentParser(), key) == 0
    monkeypatch.setenv(gate_lock.LOCK_DIR_ENV, str(tmp_path / "gate-markers"))
    if deferred:
        gate = subprocess.Popen([
            sys.executable, "-c",
            "import sys; from lmer_cli.gate_lock import hold_gate_lock\n"
            "with hold_gate_lock('gate-check'):\n"
            " print('ready', flush=True)\n sys.stdin.read()\n",
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            assert gate.stdout.readline().strip() == "ready"
            assert gate_lock.active_gate() is not None
            assert cli.cmd_memory(action, None, argparse.ArgumentParser(), key) == 0
            assert not git(root, "diff", "--cached")
        finally:
            gate.communicate(timeout=SHUTDOWN)
    else:
        with monkeypatch.context() as failing:
            failing.setattr(git_ops, "check_commit_ready", lambda *_: 1)
            assert cli.cmd_memory(action, None, argparse.ArgumentParser(), key) == 1
        assert not git(root, "diff", "--cached")
    assert cli.cmd_commit("publish complete memory transition") == 0
    files = set(git(origin, "ls-tree", "-r", "--name-only", "main").splitlines())
    marker = json.loads(git(origin, "show", "main:_memory_retirements.json"))
    index = git(origin, "show", "main:widgets/_agents/memory/MEMORY.md")
    if action == "forget":
        assert key not in files
        assert archived in files
        assert git(origin, "show", f"main:{archived}") == original
        assert key in marker["retired"]
        assert "(fact.md)" not in index
        # Another clone, including a client with no restore baseline, cannot
        # revive stale bytes. An explicit revive can recover the real archive.
        git(other, "pull", "--ff-only")
        with monkeypatch.context() as clone:
            clone.setenv("LMER_WORK_REPO_PATH", str(other))
            clone.setenv("LMER_PERSIST_AGENT_MEMORY", "1")
            local = tmp_path / "stale-memory"
            local.mkdir()
            (local / "fact.md").write_text(original)
            (local / "MEMORY.md").write_text(_index_line("fact", "fact.md"))
            clone.setenv("LMER_AGENT_MEMORY_DIR", str(local))
            assert memory.persist_memory() == 0
            assert not (other / key).exists()
            memory.revive_memory(key)
            assert (other / key).read_text() == original
    else:
        assert key in files
        assert archived not in files
        assert git(origin, "show", f"main:{key}") == original
        assert key not in marker["retired"]
        assert "(fact.md)" in index


def test_ordinary_work_commit_waits_for_claim_transaction(claim_clones, tmp_path):
    origin, root, _ = claim_clones
    script = tmp_path / "claim-holder.py"
    ready, release, finished = [tmp_path / name for name in ("ready", "release", "finished")]
    script.write_text(f'''import time
from pathlib import Path
from work_repo import cli, session_record
session_record.start_launch()
real = cli.fetch_publication_head
def sync(*args, **kwargs):
    result = real(*args, **kwargs)
    Path({str(ready)!r}).touch()
    until = time.monotonic() + {BARRIER}
    while not Path({str(release)!r}).exists():
        assert time.monotonic() < until
        time.sleep(.01)
    return result
cli.fetch_publication_head = sync
raise SystemExit(cli.cmd_release_claim())
''')
    ordinary = tmp_path / "ordinary.py"
    ordinary.write_text(f'''from pathlib import Path
from work_repo import cli, run_state, session_record
session_record.start_launch()
directory, state = run_state.ensure_run()
(directory / "ordinary.md").write_text("ordinary work survives")
rc = cli.cmd_commit("ordinary concurrent work")
Path({str(finished)!r}).write_text(str(rc))
raise SystemExit(rc)
''')
    children = []
    with (tmp_path / "claim.txt").open("w") as first, (tmp_path / "ordinary.txt").open("w") as second:
        try:
            children.append(subprocess.Popen([sys.executable, str(script)], stdout=first, stderr=first))
            assert wait_for_file(ready)
            head = git(root, "rev-parse", "HEAD")
            children.append(subprocess.Popen([sys.executable, str(ordinary)], stdout=second, stderr=second))
            finished_early = wait_for_file(finished, QUIET)
            unchanged_head = git(root, "rev-parse", "HEAD") == head
            release.touch()
            assert all(child.wait(timeout=CLI) == 0 for child in children)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait()
    assert not finished_early, "ordinary commit interleaved inside the claim transaction"
    assert unchanged_head
    assert "ordinary concurrent work" in git(origin, "log", "--format=%s", "main")
    assert yaml.safe_load(git(origin, "show", "main:widgets/_claims/release.yaml"))["holder"]


def test_claim_checks_actual_published_holder(claim_clones, monkeypatch):
    """The verdict is read back from the published tree, never assumed from
    what the transaction meant to publish. The claim no longer passes through
    the worktree at all, so the substitution happens where the bytes now go:
    the blob this publication carries."""
    origin, root, _ = claim_clones
    metadata, _, _ = seed_launch()
    real = cli.publish_overlay_commit

    def publish_a_different_holder(repo_path, **kwargs):
        claim = yaml.safe_load(kwargs["blobs"]["widgets/_claims/release.yaml"])
        claim["holder"] = "f" * 32
        kwargs["blobs"] = {"widgets/_claims/release.yaml": yaml.safe_dump(claim)}
        return real(repo_path, **kwargs)

    monkeypatch.setattr(cli, "publish_overlay_commit", publish_a_different_holder)
    assert cli.cmd_release_claim() == 1
    assert yaml.safe_load(git(origin, "show", "main:widgets/_claims/release.yaml"))["holder"] != metadata["uid"]


def test_hook_commit_defers_without_deadlock_and_replays_complete_paths(claim_clones, tmp_path):
    origin, root, _ = claim_clones
    _, directory, _ = seed_launch()
    (directory / "initial.md").write_text("initial work\n")
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.write_text(f'''#!{sys.executable}
from pathlib import Path
from work_repo import cli, run_state
marker = Path({str(root / '.git' / 'hook-wrote')!r})
if not marker.exists():
    marker.touch()
    directory = run_state.run_dir()
    (directory / "hook-note.md").write_text("saved by hook\\n")
    raise SystemExit(cli.cmd_commit("nested hook durability"))
''')
    hook.chmod(0o755)
    # A child process bounds a possible parent/hook lock deadlock.
    process = subprocess.run(
        [sys.executable, "-c", "from work_repo.cli import cmd_commit; raise SystemExit(cmd_commit('initial publication'))"],
        env=dict(os.environ, HOME=str(tmp_path / "home")), capture_output=True,
        text=True, timeout=CLI,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert cli.cmd_commit("flush hook request") == 0
    rel = directory.relative_to(root).as_posix()
    assert git(origin, "show", f"main:{rel}/hook-note.md") == "saved by hook\n"
    events = [json.loads(line) for line in git(origin, "show", f"main:{rel}/events.jsonl").splitlines()]
    assert any(event["type"] == "commit_deferred" for event in events)
    assert not git(root, "status", "--porcelain")


def test_failed_retry_does_not_consume_pending_transition_paths(claim_clones, monkeypatch):
    _, root, _ = claim_clones
    seed_launch()
    directory = root / "widgets/_agents/memory"
    directory.mkdir(parents=True)
    key = "widgets/_agents/memory/fact.md"
    (root / key).write_text(_memory_file("fact", "project"))
    (directory / "MEMORY.md").write_text(_index_line("fact", "fact.md"))
    git(root, "add", "widgets")
    git(root, "commit", "-m", "memory fixture")
    git(root, "push")
    with monkeypatch.context() as failing:
        failing.setattr(git_ops, "check_commit_ready", lambda *_: 1)
        assert cli.cmd_memory("forget", None, argparse.ArgumentParser(), key) == 1
    with monkeypatch.context() as failing:
        failing.setattr(git_ops, "_stage_and_check", lambda *_: (1, "simulated stage refusal"))
        assert cli.cmd_commit("first retry refuses before stage") == 1
    assert cli.cmd_commit("second retry publishes everything") == 0
    assert (root / "_memory_retirements.json").is_file()
    assert not git(root, "status", "--porcelain")


def test_pending_transitions_from_two_sessions_share_complete_publication(claim_clones, monkeypatch):
    origin, root, _ = claim_clones
    directory = root / "widgets/_agents/memory"
    directory.mkdir(parents=True)
    for name in ("first", "second"):
        (directory / f"{name}.md").write_text(_memory_file(name, "project"))
    (directory / "MEMORY.md").write_text("".join(_index_line(name, f"{name}.md") for name in ("first", "second")))
    git(root, "add", "widgets")
    git(root, "commit", "-m", "two memories")
    git(root, "push")
    for name in ("first", "second"):
        seed_launch()
        with monkeypatch.context() as failing:
            failing.setattr(git_ops, "check_commit_ready", lambda *_: 1)
            assert cli.cmd_memory("forget", None, argparse.ArgumentParser(), f"widgets/_agents/memory/{name}.md") == 1
    assert cli.cmd_commit("publish both deferred retirements") == 0
    marker = json.loads(git(origin, "show", "main:_memory_retirements.json"))
    files = set(git(origin, "ls-tree", "-r", "--name-only", "main").splitlines())
    index = git(origin, "show", "main:widgets/_agents/memory/MEMORY.md")
    for name in ("first", "second"):
        assert f"widgets/_agents/memory/{name}.md" in marker["retired"]
        assert f"widgets/_agents/memory/{name}.md" not in files
        assert f"widgets/_archive/_agents/memory/{name}.md" in files
        assert f"({name}.md)" not in index


@pytest.mark.parametrize("between", [None, "log"], ids=["claim-then-end", "claim-log-then-end"])
def test_session_end_releases_the_claim_it_actually_holds(claim_clones, between):
    """A holder's own session end must release its claim.

    Session end decided `held` from the checkout's copy of the claim file,
    and the claim verb publishes an object without writing that copy — so the
    copy is absent or stale unless something pulled in between, session end
    read "not ours", skipped the release, and left a claim behind. Schema 2
    has no age expiry, so that claim then refuses every later release until a
    human runs `unclaim --force`. `work log` writes the record without
    pulling, which is why it changes nothing here; `work commit` would have
    hidden the defect by refreshing the copy.
    """
    origin, root, _ = claim_clones
    launched, _, _ = seed_launch()
    assert cli.cmd_release_claim() == 0
    assert yaml.safe_load(git(origin, "show", "main:widgets/_claims/release.yaml"))["holder"] == launched["uid"]
    if between == "log":
        assert cli.cmd_log("work between the claim and the session end", []) == 0

    assert cli.cmd_session_end() == 0

    published = yaml.safe_load(git(origin, "show", "main:widgets/_claims/release.yaml"))
    assert published["holder"] is None, "a cleanly ended holder stranded its claim"
    # The next session can claim without a forced unclaim.
    following, _, _ = seed_launch()
    assert cli.cmd_release_claim() == 0
    assert yaml.safe_load(git(origin, "show", "main:widgets/_claims/release.yaml"))["holder"] == following["uid"]


def test_release_abort_completes_with_sibling_edits_in_the_shared_checkout(claim_clones):
    """Abort was the last claim transition on the rebase path.

    `pull --rebase` refuses outright with an uncommitted tracked file, and a
    sibling session's modified `log.yaml` is the resting state of a shared
    checkout (`work log` writes the record; the commit comes later), so abort
    could not complete until every sibling had committed. It now publishes
    the same way claim and unclaim do.
    """
    origin, root, _ = claim_clones
    launched, own, _ = seed_launch()
    second = root / "second.md"
    third = root / "third.md"
    second.write_text("base\n")
    third.write_text("base\n")
    git(root, "add", "second.md", "third.md")
    git(root, "commit", "-m", "sibling base")
    git(root, "push")
    assert cli.cmd_release_claim() == 0
    second.write_text("staged by a sibling\n")
    git(root, "add", "second.md")
    third.write_text("unstaged by a sibling\n")
    staged = git(root, "diff", "--cached", "--", "second.md")
    unstaged = git(root, "diff", "--", "third.md")

    assert cli.cmd_release_abort(reason="the human declined the release MR") == 0

    published = yaml.safe_load(git(origin, "show", "main:widgets/_claims/release.yaml"))
    assert published["holder"] is None, "abort must clear the claim it held"
    rel = own.relative_to(root).as_posix()
    state = yaml.safe_load(git(origin, "show", f"main:{rel}/state.yaml"))
    assert state["stop_reason"] == "aborted" and state["status"] != "in-progress"
    release = yaml.safe_load(git(origin, "show", f"main:{rel}/release.yaml"))
    assert release["aborted"]["reason"] == "the human declined the release MR"
    # The sibling's edits are untouched, with their staging split intact.
    assert git(root, "diff", "--cached", "--", "second.md") == staged
    assert git(root, "diff", "--", "third.md") == unstaged
    assert second.read_text() == "staged by a sibling\n"
    assert third.read_text() == "unstaged by a sibling\n"
    stash = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", "refs/stash"],
                           capture_output=True, text=True)
    assert stash.returncode != 0


def test_aborted_record_retries_failed_publication_without_rewriting_history(
    claim_clones, monkeypatch
):
    origin, root, _ = claim_clones
    launched, own, _ = seed_launch()
    assert cli.cmd_release_claim() == 0
    with monkeypatch.context() as failed:
        failed.setattr(cli, "publish_overlay_commit", lambda *a, **k: (
            cli.CLAIM_PUSH_ERROR, "transient publication failure", ""))
        assert cli.cmd_release_abort(reason="original decline") == 1
    before = {p.name: p.read_bytes() for p in own.iterdir() if p.is_file()}
    assert run_state.load_state(own)["stop_reason"] == "aborted"
    claim_rel = "widgets/_claims/release.yaml"
    assert yaml.safe_load(git(origin, "show", f"main:{claim_rel}"))["holder"] == launched["uid"]

    assert cli.cmd_release_abort(reason="different retry reason") == 0

    assert yaml.safe_load(git(origin, "show", f"main:{claim_rel}"))["holder"] is None
    rel = own.relative_to(root).as_posix()
    assert git(origin, "rev-parse", f"main:{rel}") == git(root, "rev-parse", f"HEAD:{rel}")
    assert yaml.safe_load(git(origin, "show", f"main:{rel}/state.yaml"))["stop_reason"] == "aborted"
    assert yaml.safe_load(git(origin, "show", f"main:{rel}/release.yaml"))["aborted"]["reason"] == "original decline"
    assert {p.name: p.read_bytes() for p in own.iterdir() if p.is_file()} == before


@pytest.mark.parametrize("force", [False, True])
def test_abort_retry_rechecks_successor_claim(claim_clones, monkeypatch, force):
    origin, root, other = claim_clones
    _, own, _ = seed_launch()
    assert cli.cmd_release_claim() == 0
    with monkeypatch.context() as failed:
        failed.setattr(cli, "publish_overlay_commit", lambda *a, **k: (
            cli.CLAIM_PUSH_ERROR, "transient publication failure", ""))
        assert cli.cmd_release_abort(reason="declined") == 1
    before = {p.name: p.read_bytes() for p in own.iterdir() if p.is_file()}
    claim_rel = "widgets/_claims/release.yaml"
    successor = "f" * 32
    git(other, "pull")
    (other / claim_rel).write_text(yaml.safe_dump({"schema": 1, "holder": successor}))
    git(other, "add", claim_rel)
    git(other, "commit", "-m", "operator takeover")
    git(other, "push")

    assert cli.cmd_release_abort(reason="retry", force=force) == (0 if force else 1)

    assert yaml.safe_load(git(origin, "show", f"main:{claim_rel}"))["holder"] == (None if force else successor)
    assert {p.name: p.read_bytes() for p in own.iterdir() if p.is_file()} == before


@pytest.mark.parametrize("changed", ["holder", "record"])
def test_abort_verifies_published_claim_and_record(claim_clones, monkeypatch, changed):
    """A successful push alone does not establish the abort's postconditions."""
    origin, root, _ = claim_clones
    seed_launch()
    assert cli.cmd_release_claim() == 0
    real = cli.publish_overlay_commit

    def publish_different_content(repo_path, **kwargs):
        if changed == "holder":
            claim_rel = "widgets/_claims/release.yaml"
            claim = yaml.safe_load(kwargs["blobs"][claim_rel])
            claim["holder"] = "f" * 32
            kwargs["blobs"] = {claim_rel: yaml.safe_dump(claim)}
        else:
            kwargs["subtrees"] = {}
        return real(repo_path, **kwargs)

    monkeypatch.setattr(cli, "publish_overlay_commit", publish_different_content)
    assert cli.cmd_release_abort(reason="declined") == 1
