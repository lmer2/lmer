"""Leased gate pushes exercise real Git against disposable bare remotes."""
from contextlib import contextmanager
import os
from pathlib import Path
import shlex
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock
import runpy
import sys

import pytest

import lmer_cli.gate_lock as gate_lock
from lmer_cli import push_lease, push_transport
from lmer_cli.gates import GateSystem
import work_repo.run_state as run_state
from lmer_cli.push_lease import LeaseError, PushLease
from tests.conftest import strip_lmer_env
from tests.test_gate_push_tag_ref import _load_gate_push_module
from tests.test_gate_push_scrub import CRED_URL, CRED_VALUE
from tests.timing_budgets import CLI, SHUTDOWN


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def git(repo, *args, check=True):
    # CLI, not 30 s: a loaded gate timed out this fixture's `git commit` (#636).
    return subprocess.run(["git", "-C", str(repo), *args], text=True,
                          capture_output=True, check=check, timeout=CLI)


def commit(repo, name):
    (repo / name).write_text(name + "\n")
    git(repo, "add", name)
    git(repo, "commit", "-m", name)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def configure(repo):
    git(repo, "config", "user.name", "Lease fixture")
    git(repo, "config", "user.email", "lease@example.com")
    git(repo, "config", "core.hooksPath", os.devnull)


def is_gate_push(command):
    return command[0] == "git" and command[1] in {"push", "-c"} and "push" in command


@pytest.fixture
def repo(tmp_path, monkeypatch, request):
    source = tmp_path / "source"
    remote = tmp_path / "remote.git"
    source.mkdir()
    remote.mkdir()
    object_format = getattr(request, "param", "sha1")
    git(source, "init", "-b", "feature", f"--object-format={object_format}")
    git(remote, "init", "--bare", f"--object-format={object_format}")
    configure(source)
    base = commit(source, "base")
    old = commit(source, "feature")
    url = "file://localhost" + str(remote)
    git(source, "remote", "add", "origin", url)
    git(source, "push", "origin", "HEAD:refs/heads/feature")
    git(source, "checkout", "-b", "upstream", base)
    upstream = commit(source, "upstream")
    git(source, "checkout", "feature")
    git(source, "rebase", "upstream")
    head = git(source, "rev-parse", "HEAD").stdout.strip()
    assert head != old
    assert git(source, "merge-base", "--is-ancestor", old, head,
               check=False).returncode == 1
    monkeypatch.chdir(source)
    monkeypatch.setenv("LMER_PUSH_ALLOW_LIST", url + "|refs/heads/feature")
    return SimpleNamespace(source=source, remote=remote, url=url, old=old,
                           head=head, upstream=upstream, tmp=tmp_path)


@pytest.fixture
def runner(repo, monkeypatch):
    monkeypatch.setenv("LMER_FORCE_PUSH_EXPLICITLY_APPROVED_BY_USER", "1")
    module = _load_gate_push_module()
    receipts = []
    pushes = []
    push_configs = []
    real_run = subprocess.run

    def run(command, **kwargs):
        if is_gate_push(command):
            kwargs.setdefault("timeout", CLI)
            pushes.append(command)
            push_configs.append(real_run(
                ["git", "config", "--null", "--list", "--includes"],
                env=kwargs.get("env"), capture_output=True, text=True, timeout=CLI,
                check=True).stdout)
        return real_run(command, **kwargs)

    checks = Mock(return_value=True)
    monkeypatch.setattr(GateSystem, "run_commit_gate", checks)
    monkeypatch.setattr(module, "emit_gate_event",
                        lambda gate, outcome, **kw: receipts.append((gate, outcome, kw)))
    monkeypatch.setattr(subprocess, "run", run)
    return SimpleNamespace(module=module, checks=checks, pushes=pushes,
                           receipts=receipts, real_run=real_run, push_configs=push_configs)


def leased(repo, runner, **kwargs):
    return runner.module.gate_push(
        force_with_lease=("refs/heads/feature", repo.old), **kwargs)


def assert_receipt(runner, outcome, rc):
    assert len(runner.receipts) == 1
    name, actual, fields = runner.receipts[0]
    assert (name, actual, fields["exit_code"]) == ("gate-push", outcome, rc)


@pytest.mark.parametrize("approval", [None, "", " ", "\t", "\n", "0", "2", "01",
                                      "1.0", "true", "TRUE", "yes", "on", "1 ",
                                      " 1", "1\n", "approved"])
def test_cli_refuses_unapproved_lease_before_any_checks_or_transport(
        repo, runner, monkeypatch, capsys, approval):
    variable = "LMER_FORCE_PUSH_EXPLICITLY_APPROVED_BY_USER"
    if approval is None:
        monkeypatch.delenv(variable)
    else:
        monkeypatch.setenv(variable, approval)
    stages = []

    @contextmanager
    def marker(label):
        stages.append(label)
        yield
        stages.append("released")

    def receipt(gate, outcome, **fields):
        assert stages == ["gate-push"]
        runner.receipts.append((gate, outcome, fields))
        stages.append("receipt")

    forbidden = Mock(side_effect=AssertionError("approval must precede this operation"))
    with monkeypatch.context() as scoped:
        scoped.setattr(GateSystem, "run_push_gate", forbidden)
        scoped.setattr(PushLease, "pin_source", forbidden)
        scoped.setattr(push_lease, "frozen_transport", forbidden)
        scoped.setattr(subprocess, "run", forbidden)
        scoped.setattr(subprocess, "Popen", forbidden)
        scoped.setattr(gate_lock, "hold_gate_lock", marker)
        scoped.setattr(run_state, "emit_gate_event", receipt)
        scoped.setattr(sys, "argv", [runner.module.__file__, "--force-with-lease",
                                    "refs/heads/feature", repo.old])
        with pytest.raises(SystemExit) as result:
            runpy.run_path(runner.module.__file__, run_name="__main__")
    assert result.value.code == 1
    forbidden.assert_not_called()
    runner.checks.assert_not_called()
    assert runner.pushes == []
    assert stages == ["gate-push", "receipt", "released"]
    assert_receipt(runner, "fail", 1)
    fields = runner.receipts[0][2]
    assert fields["summary"] is None
    assert "commit_sha" not in fields
    assert "test_scope" not in fields
    assert "test_cache_verdict" not in fields
    output = capsys.readouterr().out
    assert ("Force push blocked. Obtain explicit operator permission first; "
            "only then set LMER_FORCE_PUSH_EXPLICITLY_APPROVED_BY_USER=1 "
            "for this command.") in output
    assert "Push successful" not in output
    assert "You may proceed" not in output
    assert os.environ.get(variable) == approval
    assert git(repo.remote, "rev-parse", "refs/heads/feature").stdout.strip() == repo.old
    assert git(repo.source, "rev-parse", "HEAD").stdout.strip() == repo.head


@pytest.mark.parametrize("repo", ["sha1", "sha256"], indirect=True)
def test_rebased_update_exact_lease_and_source(repo, runner):
    assert leased(repo, runner) == 0
    runner.checks.assert_called_once_with()
    assert runner.pushes == [[
        "git", "push", "--no-follow-tags", "--recurse-submodules=no",
        f"--force-with-lease=refs/heads/feature:{repo.old}", "--", "origin",
        f"{repo.head}:refs/heads/feature",
    ]]
    assert git(repo.remote, "rev-parse", "refs/heads/feature").stdout.strip() == repo.head
    assert_receipt(runner, "pass", 0)
    assert runner.receipts[0][2]["commit_sha"] == repo.head


def test_remote_advance_during_gates_keeps_concurrent_work(repo, runner):
    other = repo.tmp / "other"
    other.mkdir()
    git(other, "clone", "--branch", "feature", repo.url, ".")
    configure(other)
    concurrent = commit(other, "remote-work")

    def advance():
        git(other, "push", "origin", "HEAD:refs/heads/feature")
        # Even an incidental fetch must not refresh the caller's expectation.
        git(repo.source, "fetch", "origin")
        return True

    runner.checks.side_effect = advance
    rc = leased(repo, runner)
    assert rc != 0
    assert len(runner.pushes) == 1
    assert f"--force-with-lease=refs/heads/feature:{repo.old}" in runner.pushes[0]
    assert git(repo.remote, "rev-parse", "refs/heads/feature").stdout.strip() == concurrent
    assert git(repo.remote, "show", "refs/heads/feature:remote-work").stdout == "remote-work\n"
    assert_receipt(runner, "pass", rc)


def test_failed_gate_never_pushes(repo, runner):
    runner.checks.return_value = False
    rc = leased(repo, runner)
    assert rc != 0
    runner.checks.assert_called_once_with()
    assert runner.pushes == []
    assert git(repo.remote, "rev-parse", "refs/heads/feature").stdout.strip() == repo.old
    assert_receipt(runner, "fail", rc)


@pytest.mark.parametrize("grant", ["refs/heads/upstream", "refs/tags/*"])
def test_destination_not_source_is_authorized(repo, runner, monkeypatch, grant):
    monkeypatch.setenv("LMER_PUSH_ALLOW_LIST", repo.url + "|" + grant)
    assert leased(repo, runner) != 0
    runner.checks.assert_not_called()
    assert runner.pushes == []


def test_authorized_different_destination_uses_checked_source(repo, runner, monkeypatch):
    git(repo.remote, "update-ref", "refs/heads/review", repo.old)
    monkeypatch.setenv("LMER_PUSH_ALLOW_LIST", repo.url + "|refs/heads/review")
    assert runner.module.gate_push(force_with_lease=("refs/heads/review", repo.old)) == 0
    assert git(repo.remote, "rev-parse", "refs/heads/review").stdout.strip() == repo.head
    assert git(repo.remote, "rev-parse", "refs/heads/feature").stdout.strip() == repo.old


def test_source_locks_cover_gate_push_and_receipt(repo, runner, monkeypatch):
    observed = []

    def try_move(stage):
        assert git(repo.source, "update-ref", "refs/heads/feature", repo.upstream,
                   check=False).returncode != 0
        assert git(repo.source, "symbolic-ref", "HEAD", "refs/heads/upstream",
                   check=False).returncode != 0
        assert git(repo.source, "rev-parse", "HEAD").stdout.strip() == repo.head
        observed.append(stage)

    def checks():
        try_move("gate")
        return True

    def run(command, **kw):
        if is_gate_push(command):
            kw.setdefault("timeout", CLI)
            try_move("push")
        return runner.real_run(command, **kw)

    runner.checks.side_effect = checks
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(runner.module, "emit_gate_event",
                        lambda *a, **kw: try_move("receipt"))
    assert leased(repo, runner) == 0
    assert observed == ["gate", "push", "receipt"]
    # Cleanup releases both locks even though no ref transaction was committed.
    git(repo.source, "update-ref", "refs/heads/feature", repo.head)
    git(repo.source, "symbolic-ref", "HEAD", "refs/heads/feature")


@pytest.mark.parametrize("kind", ["staged", "unstaged", "untracked", "detached"])
def test_unchecked_source_is_refused(repo, runner, kind):
    if kind == "detached":
        git(repo.source, "checkout", "--detach")
    else:
        name = "new" if kind == "untracked" else "feature"
        (repo.source / name).write_text("unchecked\n")
        if kind == "staged":
            git(repo.source, "add", name)
    assert leased(repo, runner) != 0
    runner.checks.assert_not_called()
    assert runner.pushes == []


def test_gate_edit_is_refused_before_push(repo, runner):
    def edit():
        (repo.source / "feature").write_text("changed by a check\n")
        return True
    runner.checks.side_effect = edit
    assert leased(repo, runner) != 0
    assert runner.pushes == []


def test_lost_source_lock_is_refused(repo, runner, monkeypatch):
    original = PushLease.pin_source

    @contextmanager
    def pin(lease, remote):
        with original(lease, remote) as source:
            def lose_lock():
                source.process.terminate()
                source.process.wait(timeout=SHUTDOWN)
                return True
            runner.checks.side_effect = lose_lock
            yield source

    monkeypatch.setattr(PushLease, "pin_source", pin)
    assert leased(repo, runner) != 0
    assert runner.pushes == []


@pytest.mark.parametrize("mode", ["multiple", "mirror", "symbolic"])
def test_implicit_or_aliased_destinations_refused(repo, runner, mode):
    if mode == "multiple":
        git(repo.source, "remote", "set-url", "--add", "--push", "origin", repo.url)
        git(repo.source, "remote", "set-url", "--add", "--push", "origin", repo.url)
    elif mode == "mirror":
        git(repo.source, "config", "remote.origin.mirror", "true")
    else:
        git(repo.remote, "update-ref", "refs/heads/protected", repo.old)
        git(repo.remote, "symbolic-ref", "refs/heads/feature", "refs/heads/protected")
    assert leased(repo, runner) != 0
    assert runner.pushes == []
    assert git(repo.remote, "rev-parse", "refs/heads/feature").stdout.strip() == repo.old


def test_configured_push_refspec_and_follow_tags_cannot_add_updates(repo, runner):
    git(repo.source, "config", "remote.origin.push", "+refs/heads/*:refs/heads/*")
    git(repo.source, "config", "push.followTags", "true")
    git(repo.source, "tag", "-a", "v-fixture", "-m", "fixture")
    assert leased(repo, runner) == 0
    assert git(repo.remote, "for-each-ref", "--format=%(refname)").stdout.splitlines() == ["refs/heads/feature"]


def test_push_url_not_fetch_url_is_authorized(repo, runner):
    git(repo.source, "remote", "set-url", "--push", "origin", "https://denied.example.com/group/repo.git")
    assert leased(repo, runner) != 0
    runner.checks.assert_not_called()
    assert runner.pushes == []


@pytest.mark.parametrize("approval", [None, "invalid"])
def test_ordinary_push_still_uses_current_branch_and_tracking(
        repo, runner, monkeypatch, approval):
    if approval is None:
        monkeypatch.delenv("LMER_FORCE_PUSH_EXPLICITLY_APPROVED_BY_USER")
    else:
        monkeypatch.setenv("LMER_FORCE_PUSH_EXPLICITLY_APPROVED_BY_USER", approval)
    git(repo.source, "reset", "--hard", repo.old)
    assert runner.module.gate_push() == 0
    assert runner.pushes == [["git", "push", "-u", "origin", "feature"]]
    assert git(repo.source, "config", "branch.feature.remote").stdout.strip() == "origin"


@pytest.mark.parametrize("destination", [
    "feature", "HEAD", "refs/tags/v1", "refs/remotes/origin/feature",
    "+refs/heads/feature", ":refs/heads/feature", "HEAD:refs/heads/feature",
    "refs/heads/*", "refs/heads/a:refs/heads/b", "refs/heads/../main",
    "refs/heads/a refs/heads/b", "refs/heads/a\nrefs/heads/b", "refs/heads/",
])
def test_invalid_destinations(repo, destination):
    with pytest.raises(LeaseError):
        PushLease(destination, repo.old)


@pytest.mark.parametrize("expected", ["", "HEAD", "origin/feature", "a" * 7, "0" * 40,
                                    "A" * 40, "f" * 41, "f" * 40 + ":refs/heads/main"])
def test_invalid_expectations(expected):
    with pytest.raises(LeaseError):
        PushLease("refs/heads/feature", expected)


@pytest.mark.parametrize("extra", [
    ["--tag", "v1"], ["--ref", "refs/heads/feature"],
    ["--force"], ["-f"], ["--force-with-leas", "refs/heads/feature", "f" * 40],
    ["--force-with-lease", "refs/heads/feature", "f" * 40],
    ["--remote", "origin", "--remote", "other"], ["refs/heads/other"],
])
def test_cli_rejects_conflicting_repeated_and_ambiguous_options(repo, extra):
    module = _load_gate_push_module()
    with pytest.raises(SystemExit) as error:
        module.parse_args(["--force-with-lease", "refs/heads/feature", repo.old, *extra])
    assert error.value.code == 2


def test_cli_parses_explicit_lease_and_requires_both_values(repo):
    module = _load_gate_push_module()
    args = module.parse_args(["--force-with-lease", "refs/heads/feature", repo.old,
                              "--remote", "mirror"])
    assert args.force_with_lease == ["refs/heads/feature", repo.old]
    assert args.remote == "mirror"
    for argv in (["--force-with-lease"], ["--force-with-lease", "refs/heads/feature"]):
        with pytest.raises(SystemExit) as error:
            module.parse_args(argv)
        assert error.value.code == 2


def test_main_forwards_the_lease_under_the_existing_gate_marker(repo, runner, monkeypatch):
    events = []

    @contextmanager
    def marker(label):
        events.append(label)
        yield
        events.append("released")

    def receipt(*a, **kw):
        assert events == ["gate-push"]
        assert kw["commit_sha"] == repo.head
        events.append("receipt")

    monkeypatch.setattr(gate_lock, "hold_gate_lock", marker)
    monkeypatch.setattr(run_state, "emit_gate_event", receipt)
    monkeypatch.setattr(sys, "argv", [runner.module.__file__, "--force-with-lease",
                                      "refs/heads/feature", repo.old])
    with pytest.raises(SystemExit) as result:
        runpy.run_path(runner.module.__file__, run_name="__main__")
    assert result.value.code == 0
    assert events == ["gate-push", "receipt", "released"]
    assert len(runner.pushes) == 1


def test_named_remote_cannot_redirect_after_authorization(repo, runner):
    def redirect():
        git(repo.source, "remote", "set-url", "--push", "origin",
            "https://denied.example.com/group/project.git")
        return True
    runner.checks.side_effect = redirect
    assert leased(repo, runner) == 0
    assert f"remote.origin.pushurl\n{repo.url}\0" in runner.push_configs[0]
    assert git(repo.remote, "rev-parse", "refs/heads/feature").stdout.strip() == repo.head


def test_last_boundary_remote_and_mirror_changes_cannot_expand_push(repo, runner, monkeypatch):
    def run(command, **kwargs):
        if is_gate_push(command):
            kwargs.setdefault("timeout", CLI)
            git(repo.source, "remote", "set-url", "--push", "origin",
                "https://denied.example.com/group/project.git")
            git(repo.source, "config", "remote.origin.mirror", "true")
        return runner.real_run(command, **kwargs)
    monkeypatch.setattr(subprocess, "run", run)
    assert leased(repo, runner) == 0
    assert git(repo.remote, "for-each-ref", "--format=%(refname)").stdout.splitlines() == ["refs/heads/feature"]


@pytest.mark.parametrize("by_url", [False, True])
def test_push_failure_preserves_raw_credentials_and_records_failure(repo, runner, monkeypatch, capsys, by_url):
    monkeypatch.setenv("LMER_PUSH_ALLOW_LIST", "git.example.com/group/project")
    pushes = []
    inspections = []
    remote = CRED_URL if by_url else "origin"
    if not by_url:
        git(repo.source, "remote", "set-url", "origin", CRED_URL)
        git(repo.source, "config", "remote.origin.proxy", "http://proxy.example.com:3128")

    def run(command, **kwargs):
        if "ls-remote" in command and "--symref" in command:
            inspections.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")
        if is_gate_push(command):
            kwargs.setdefault("timeout", CLI)
            pushes.append(command)
            return subprocess.CompletedProcess(command, 19)
        return runner.real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    assert leased(repo, runner, remote=remote) == 19
    assert pushes == [["git", "push", "--no-follow-tags", "--recurse-submodules=no",
                       f"--force-with-lease=refs/heads/feature:{repo.old}",
                       "--", remote, f"{repo.head}:refs/heads/feature"]]
    assert inspections[0][-2:] == [remote, "refs/heads/feature"]
    assert CRED_VALUE not in capsys.readouterr().out
    assert_receipt(runner, "pass", 19)


def test_existing_source_lock_refuses_without_removing_it(repo, runner):
    lock = repo.source / ".git" / "HEAD.lock"
    lock.write_text("owned by another process\n")
    assert leased(repo, runner) != 0
    runner.checks.assert_not_called()
    assert runner.pushes == []
    assert lock.read_text() == "owned by another process\n"


@pytest.mark.parametrize("remote", ["--force", "-f", "origin other", "origin\nother"])
def test_option_like_remote_is_refused(repo, runner, remote):
    assert leased(repo, runner, remote=remote) != 0
    runner.checks.assert_not_called()
    assert runner.pushes == []


@pytest.mark.parametrize("target", [{"ref": "refs/heads/upstream"}, {"tag": "v1"}])
def test_direct_entry_cannot_supply_an_unchecked_source(repo, runner, target):
    assert leased(repo, runner, **target) != 0
    runner.checks.assert_not_called()
    assert runner.pushes == []


@pytest.mark.parametrize("failure", ["url-list-reset", "url-rewrite"])
def test_inability_to_pin_destination_refuses(repo, runner, monkeypatch, failure):
    original = push_transport.git

    def read(*args, **kwargs):
        if failure == "url-list-reset" and kwargs.get("env") and "get-url" in args:
            return repo.url + "\nhttps://denied.example.com/repo.git"
        if failure == "url-rewrite" and "--get-url" in args:
            return "https://denied.example.com/repo.git"
        return original(*args, **kwargs)

    monkeypatch.setattr(push_transport, "git", read)
    assert leased(repo, runner) != 0
    assert runner.pushes == []


def test_source_lock_timeout_refuses_and_cleans_up(repo, runner, monkeypatch):
    ticks = iter([0, 31])
    monkeypatch.setattr(push_lease, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    assert leased(repo, runner) != 0
    runner.checks.assert_not_called()
    assert runner.pushes == []
    git(repo.source, "update-ref", "refs/heads/feature", repo.head)
    git(repo.source, "symbolic-ref", "HEAD", "refs/heads/feature")


def test_wrong_object_format_refuses_before_gates(repo, runner):
    assert runner.module.gate_push(force_with_lease=("refs/heads/feature", "f" * 64)) != 0
    runner.checks.assert_not_called()
    assert runner.pushes == []


def test_linked_worktree_source_is_locked_against_another_checkout(repo, runner, monkeypatch):
    checkout = repo.tmp / "linked"
    git(repo.source, "worktree", "add", "-b", "lease-source", str(checkout), repo.head)
    monkeypatch.chdir(checkout)

    def try_other_checkout():
        assert git(repo.source, "update-ref", "refs/heads/lease-source", repo.upstream,
                   check=False).returncode != 0
        assert git(checkout, "symbolic-ref", "HEAD", "refs/heads/upstream",
                   check=False).returncode != 0
        return True

    runner.checks.side_effect = try_other_checkout
    assert leased(repo, runner) == 0
    assert git(repo.remote, "rev-parse", "refs/heads/feature").stdout.strip() == repo.head


@pytest.mark.parametrize("layer", ["local", "global", "system", "include", "worktree", "command"])
def test_last_boundary_rewrite_cannot_redirect_to_denied_remote(repo, runner, monkeypatch, layer):
    denied = repo.tmp / "denied.git"
    git(repo.source, "clone", "--bare", repo.url, str(denied))
    denied_url = "file://localhost" + str(denied)
    config_file = repo.tmp / "late-config"
    config_file.write_text("")
    if layer == "global":
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config_file))
    elif layer == "system":
        monkeypatch.delenv("GIT_CONFIG_NOSYSTEM")
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(config_file))
    elif layer == "include":
        git(repo.source, "config", "include.path", str(config_file))
    elif layer == "worktree":
        git(repo.source, "config", "extensions.worktreeConfig", "true")

    def run(command, **kwargs):
        if is_gate_push(command):
            kwargs.setdefault("timeout", CLI)
            key = f"url.{denied_url}.insteadOf"
            if layer == "command":
                monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
                monkeypatch.setenv("GIT_CONFIG_KEY_0", key)
                monkeypatch.setenv("GIT_CONFIG_VALUE_0", repo.url)
            else:
                scope = (["--local"] if layer == "local" else
                         ["--worktree"] if layer == "worktree" else
                         ["--file", str(config_file)])
                git(repo.source, "config", *scope, key, repo.url)
        return runner.real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    rc = leased(repo, runner)
    assert git(denied, "rev-parse", "refs/heads/feature").stdout.strip() == repo.old
    assert rc == 0
    assert git(repo.remote, "rev-parse", "refs/heads/feature").stdout.strip() == repo.head


def test_raw_url_push_rewrite_cannot_redirect_to_denied_remote(repo, runner):
    denied = repo.tmp / "denied.git"
    git(repo.source, "clone", "--bare", repo.url, str(denied))
    git(repo.source, "config", f"url.file://localhost{denied}.pushInsteadOf", repo.url)
    rc = leased(repo, runner, remote=repo.url)
    assert git(denied, "rev-parse", "refs/heads/feature").stdout.strip() == repo.old
    assert rc == 0
    assert git(repo.remote, "rev-parse", "refs/heads/feature").stdout.strip() == repo.head


def test_receivepack_cannot_redirect_to_denied_repository(repo, runner, capsys):
    denied = repo.tmp / "denied.git"
    git(repo.source, "clone", "--bare", repo.url, str(denied))
    marker = repo.tmp / "receivepack-ran"
    wrapper = repo.tmp / "redirect-receivepack"
    wrapper.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n"
                       f"exec git receive-pack {shlex.quote(str(denied))}\n")
    wrapper.chmod(0o755)
    git(repo.source, "config", "remote.origin.receivepack", str(wrapper))
    rc = leased(repo, runner)
    assert git(denied, "rev-parse", "refs/heads/feature").stdout.strip() == repo.old
    assert git(repo.remote, "rev-parse", "refs/heads/feature").stdout.strip() == repo.old
    assert not marker.exists()
    assert rc != 0
    runner.checks.assert_called_once_with()
    assert runner.pushes == []
    assert_receipt(runner, "pass", rc)
    assert runner.receipts[0][2]["commit_sha"] == repo.head
    output = capsys.readouterr()
    assert "receivepack" in output.out
    assert "Push successful" not in output.out


@pytest.mark.parametrize("selector", ["receivepack", "uploadpack", "vcs", "serveroption"])
@pytest.mark.parametrize("inspect", [False, True])
def test_named_service_selectors_refuse_before_transport(repo, runner, monkeypatch, selector, inspect):
    marker = repo.tmp / "service-ran"
    wrapper = repo.tmp / "service"
    wrapper.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 17\n")
    wrapper.chmod(0o755)
    git(repo.source, "remote", "rename", "origin", "CaseSensitive")
    git(repo.source, "config", f"remote.CaseSensitive.{selector}", str(wrapper))
    transports = []

    def run(command, **kwargs):
        if "ls-remote" in command and "--symref" in command:
            transports.append(command)
        return runner.real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    rc = (runner.module.inspect_lease("CaseSensitive", "refs/heads/feature") if inspect
          else leased(repo, runner, remote="CaseSensitive"))
    assert rc != 0
    assert not marker.exists()
    assert transports == []
    assert git(repo.remote, "rev-parse", "refs/heads/feature").stdout.strip() == repo.old
    if inspect:
        runner.checks.assert_not_called()
        assert runner.receipts == []
    else:
        assert_receipt(runner, "pass", rc)


@pytest.mark.parametrize("layer", ["global", "system", "include", "worktree", "command"])
def test_service_selector_refusal_covers_captured_config_layers(repo, runner, monkeypatch, layer):
    config = repo.tmp / "selector-config"
    config.write_text('[remote "origin"]\n receivepack = git-receive-pack\n')
    if layer == "global":
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    elif layer == "system":
        monkeypatch.delenv("GIT_CONFIG_NOSYSTEM")
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(config))
    elif layer == "include":
        git(repo.source, "config", "include.path", str(config))
    elif layer == "worktree":
        git(repo.source, "config", "extensions.worktreeConfig", "true")
        git(repo.source, "config", "--worktree", "remote.origin.receivepack", "git-receive-pack")
    else:
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "remote.origin.receivepack")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "git-receive-pack")
    assert leased(repo, runner) != 0
    assert runner.pushes == []
    assert git(repo.remote, "rev-parse", "refs/heads/feature").stdout.strip() == repo.old


@pytest.mark.parametrize("by_url", [False, True])
def test_irrelevant_remote_selectors_do_not_block_or_redirect(repo, runner, by_url):
    remote = "origin" if by_url else "unused"
    for selector in ("receivepack", "uploadpack", "vcs", "serveroption"):
        git(repo.source, "config", f"remote.{remote}.{selector}", "/no-such-service")
    assert leased(repo, runner, remote=repo.url if by_url else "origin") == 0
    assert git(repo.remote, "rev-parse", "refs/heads/feature").stdout.strip() == repo.head


def test_last_boundary_service_selectors_cannot_redirect(repo, runner, monkeypatch):
    denied = repo.tmp / "denied.git"
    git(repo.source, "clone", "--bare", repo.url, str(denied))
    wrapper = repo.tmp / "late-receivepack"
    wrapper.write_text(f"#!/bin/sh\nexec git receive-pack {shlex.quote(str(denied))}\n")
    wrapper.chmod(0o755)

    def run(command, **kwargs):
        if is_gate_push(command):
            kwargs.setdefault("timeout", CLI)
            git(repo.source, "config", "remote.origin.receivepack", str(wrapper))
            git(repo.source, "config", "remote.origin.vcs", "/no-such-service")
            monkeypatch.setenv("GIT_OVERRIDE_VIRTUAL_HOST", "denied.example.com")
            assert "GIT_OVERRIDE_VIRTUAL_HOST" not in kwargs["env"]
        return runner.real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    assert leased(repo, runner) == 0
    assert git(denied, "rev-parse", "refs/heads/feature").stdout.strip() == repo.old
    assert git(repo.remote, "rev-parse", "refs/heads/feature").stdout.strip() == repo.head
    assert_receipt(runner, "pass", 0)


@pytest.mark.parametrize("url", ["ext::git receive-pack denied", "custom::https://example.com/repo",
                                  "custom://example.com/repo"])
def test_custom_url_helpers_are_unsupported(repo, url):
    with pytest.raises(LeaseError, match="custom URL helpers"):
        with push_transport.frozen_transport("origin", url, False):
            pytest.fail("Unsupported transport must not become usable")


@pytest.mark.parametrize("inspect", [False, True])
def test_virtual_host_override_is_unsupported(repo, runner, monkeypatch, inspect):
    monkeypatch.setenv("GIT_OVERRIDE_VIRTUAL_HOST", "denied.example.com")
    rc = (runner.module.inspect_lease("origin", "refs/heads/feature") if inspect
          else leased(repo, runner))
    assert rc != 0
    assert runner.pushes == []
    assert git(repo.remote, "rev-parse", "refs/heads/feature").stdout.strip() == repo.old


@pytest.mark.parametrize("inspect", [False, True])
def test_namespace_cannot_select_an_unauthorized_ref(repo, runner, monkeypatch, inspect):
    denied = "refs/namespaces/denied/refs/heads/feature"
    git(repo.remote, "update-ref", denied, repo.old)
    monkeypatch.setenv("GIT_NAMESPACE", "denied")
    rc = (runner.module.inspect_lease("origin", "refs/heads/feature") if inspect
          else leased(repo, runner))
    assert git(repo.remote, "rev-parse", denied).stdout.strip() == repo.old
    assert git(repo.remote, "rev-parse", "refs/heads/feature").stdout.strip() == repo.old
    assert rc != 0
    assert runner.pushes == []
    if not inspect:
        assert_receipt(runner, "pass", rc)


@pytest.mark.parametrize("failure", [False, True])
def test_inspection_never_prints_fake_url_userinfo(repo, runner, monkeypatch, capsys, failure):
    git(repo.source, "remote", "set-url", "origin", CRED_URL)
    assert CRED_VALUE in git(repo.source, "remote", "get-url", "--push", "--all", "origin").stdout
    inspections = []

    def run(command, **kwargs):
        if "ls-remote" in command and "--symref" in command:
            inspections.append(command)
            assert kwargs["capture_output"]
            assert push_transport.git("remote", "get-url", "--push", "origin", env=kwargs["env"]) == CRED_URL
            if failure:
                return subprocess.CompletedProcess(command, 128, "", "refused " + CRED_URL)
            return subprocess.CompletedProcess(command, 0, repo.old + "\trefs/heads/feature\n", "")
        return runner.real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", [runner.module.__file__, "--inspect-lease",
                                      "refs/heads/feature", "--remote", "origin"])
    with pytest.raises(SystemExit) as result:
        runpy.run_path(runner.module.__file__, run_name="__main__")
    assert result.value.code == (1 if failure else 0)
    output = capsys.readouterr()
    assert CRED_VALUE not in output.out + output.err
    assert "oauth2" not in output.out + output.err
    assert "/group/project.git" not in output.out
    if not failure:
        assert "Push endpoint: https://git.example.com\n" in output.out
        assert repo.old + " refs/heads/feature" in output.out
    assert len(inspections) == 1
    runner.checks.assert_not_called()
    assert runner.pushes == []
    assert runner.receipts == []


@pytest.mark.parametrize("failure", [None, "config", "remote", "ls-remote"])
def test_inspection_cli_omits_url_secrets_and_captured_errors(repo, runner, failure):
    secrets = ["FAKE_USER_508", "FAKE_PASSWORD_508", "FAKE_PATH_508",
               "FAKE_QUERY_508", "FAKE_FRAGMENT_508"]
    url = (f"https://{secrets[0]}:{secrets[1]}@git.example.com/{secrets[2]}"
           f"?access_token={secrets[3]}#{secrets[4]}")
    git(repo.source, "remote", "set-url", "origin", url)
    real_git = shutil.which("git")
    fake_bin = repo.tmp / "fake-bin"
    fake_bin.mkdir()
    marker = repo.tmp / "advertised"
    shim = fake_bin / "git"
    shim.write_text(f"#!{sys.executable}\nimport os, sys\n"
                    f"if sys.argv[1] == {failure!r} or sys.argv[1:3] == ['ls-remote', '--symref']:\n"
                    f"    open({str(marker)!r}, 'w').close()\n"
                    f"    if {failure!r}:\n"
                    f"        print({url!r})\n"
                    f"        print({' '.join(secrets)!r}, file=sys.stderr)\n"
                    "        sys.exit(128)\n"
                    f"    print({(repo.old + chr(9) + 'refs/heads/feature')!r})\n"
                    "    sys.exit(0)\n"
                    f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n")
    shim.chmod(0o755)
    env = dict(os.environ, PATH=str(fake_bin) + os.pathsep + os.environ["PATH"])
    result = subprocess.run(
        [sys.executable, runner.module.__file__, "--inspect-lease", "refs/heads/feature"],
        env=env, capture_output=True, text=True, check=False, timeout=CLI)
    assert marker.exists()
    assert result.returncode == (1 if failure else 0)
    for secret in secrets:
        assert secret not in result.stdout + result.stderr
    if failure:
        assert f"git {failure} failed (128)" in result.stdout
    else:
        assert "Push endpoint: https://git.example.com\n" in result.stdout
        assert repo.old + " refs/heads/feature" in result.stdout
    assert git(repo.remote, "rev-parse", "refs/heads/feature").stdout.strip() == repo.old


@pytest.fixture(params=["environment", "global", "system", "include"])
def traced_inspection(repo, request):
    secrets = ["FAKE_TRACE_USER_508", "FAKE_TRACE_PASSWORD_508", "FAKE_TRACE_PATH_508",
               "FAKE_TRACE_QUERY_508", "FAKE_TRACE_FRAGMENT_508"]
    path = repo.tmp / f"{secrets[2]}?access_token={secrets[3]}#{secrets[4]}"
    path.symlink_to(repo.remote, target_is_directory=True)
    url = f"file://{secrets[0]}:{secrets[1]}@localhost{path}"
    git(repo.source, "remote", "set-url", "origin", url)
    sinks = repo.tmp / "traces"
    sinks.mkdir()
    event = sinks / "events"
    event.mkdir()
    targets = {"GIT_TRACE2": sinks / "normal", "GIT_TRACE2_PERF": sinks / "perf",
               "GIT_TRACE2_EVENT": event}
    env = dict(os.environ, FAKE_TRACE_PAYLOAD=url, GIT_CURL_VERBOSE="1",
               GIT_TRACE_REDACT="0")
    for key in ("GIT_TRACE", "GIT_TRACE_SETUP", "GIT_TRACE_PERFORMANCE", "GIT_TRACE_PACKET",
                "GIT_TRACE_PACKFILE", "GIT_TRACE_CURL"):
        env[key] = str(sinks / key)
    if request.param == "environment":
        env.update({key: str(value) for key, value in targets.items()})
        env.update(GIT_TRACE2_CONFIG_PARAMS="remote.*.url", GIT_TRACE2_ENV_VARS="FAKE_TRACE_PAYLOAD")
    else:
        config = repo.tmp / "trace-config"
        config.write_text(f"[trace2]\nnormalTarget = {targets['GIT_TRACE2']}\n"
                          f"perfTarget = {targets['GIT_TRACE2_PERF']}\n"
                          f"eventTarget = {event}\nconfigParams = remote.*.url\n"
                          "envVars = FAKE_TRACE_PAYLOAD\n")
        if request.param == "system":
            env.pop("GIT_CONFIG_NOSYSTEM")
            env["GIT_CONFIG_SYSTEM"] = str(config)
        else:
            if request.param == "include":
                included = repo.tmp / "trace-include"
                config.rename(included)
                config.write_text(f"[include]\npath = {included}\n")
            env["GIT_CONFIG_GLOBAL"] = str(config)
    control = subprocess.run(["git", "check-ref-format", "refs/heads/feature"],
                             env=env, capture_output=True, text=True, timeout=CLI, check=True)
    assert control.stdout == ""
    traces = "".join(p.read_text() for p in sinks.rglob("*") if p.is_file())
    assert all(secret in traces for secret in secrets)
    assert all(p.exists() and (any(p.iterdir()) if p.is_dir() else p.stat().st_size)
               for p in targets.values())
    for path in sinks.rglob("*"):
        if path.is_file():
            path.unlink()
    return SimpleNamespace(env=env, sinks=sinks, secrets=secrets)


@pytest.mark.parametrize("stage", ["success", "first-call", "remote", "transport"])
def test_inspection_cli_disables_git_trace_sinks(repo, traced_inspection, stage):
    trace = traced_inspection
    destination = "refs/heads/bad..ref" if stage == "first-call" else "refs/heads/feature"
    remote = "absent" if stage == "remote" else "origin"
    if stage == "transport":
        repo.remote.rename(repo.tmp / "unavailable.git")
    command = Path(__file__).resolve().parents[1] / "bin" / "gate-push"
    result = subprocess.run([sys.executable, str(command), "--inspect-lease", destination,
                             "--remote", remote], env=trace.env, capture_output=True,
                            text=True, check=False, timeout=CLI)
    assert result.returncode == (0 if stage == "success" else 1)
    if stage == "success":
        assert result.stdout == f"Push endpoint: file://localhost\n{repo.old} {destination}\n"
    traces = "".join(p.read_text() for p in trace.sinks.rglob("*") if p.is_file())
    for secret in trace.secrets:
        assert secret not in result.stdout + result.stderr
        assert secret not in traces
    assert traces == ""
    remote_path = repo.tmp / "unavailable.git" if stage == "transport" else repo.remote
    assert git(remote_path, "rev-parse", "refs/heads/feature").stdout.strip() == repo.old


@pytest.mark.parametrize("with_lease", [False, True])
def test_inspection_does_not_change_push_tracing(repo, runner, monkeypatch, with_lease):
    if not with_lease:
        git(repo.source, "reset", "--hard", repo.old)
    trace = repo.tmp / "push-events"
    monkeypatch.setenv("GIT_TRACE2_EVENT", str(trace))
    monkeypatch.setenv("GIT_TRACE2_CONFIG_PARAMS", "remote.*.url")
    monkeypatch.setenv("GIT_TRACE_SETUP", str(repo.tmp / "setup-trace"))
    monkeypatch.setenv("GIT_CURL_VERBOSE", "1")
    before = dict(os.environ)
    calls = []
    original_run = subprocess.run

    def run(command, **kwargs):
        calls.append((command, kwargs.get("env")))
        return original_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    assert runner.module.inspect_lease("origin", "refs/heads/feature") == 0
    assert calls[0][0][1] == "check-ref-format"
    assert {command[1] for command, _ in calls} >= {"config", "remote", "rev-parse", "ls-remote"}
    for _, env in calls:
        assert {key: value for key, value in env.items() if key.startswith("GIT_TRACE")} == {
            "GIT_TRACE2": "0", "GIT_TRACE2_EVENT": "0", "GIT_TRACE2_PERF": "0"}
        assert "GIT_CURL_VERBOSE" not in env
    assert not trace.exists()
    assert not (repo.tmp / "setup-trace").exists()
    assert dict(os.environ) == before
    assert (leased(repo, runner) if with_lease else runner.module.gate_push()) == 0
    assert '"push"' in trace.read_text()
    assert dict(os.environ) == before


@pytest.mark.parametrize("error", [OSError("FAKE_URL_SECRET_508"),
                                   subprocess.TimeoutExpired("FAKE_URL_SECRET_508", 30)])
@pytest.mark.parametrize("stage", ["git", "filesystem"])
def test_inspection_exception_details_are_withheld(repo, runner, monkeypatch, capsys, error, stage):
    if stage == "git":
        monkeypatch.setattr(subprocess, "run", Mock(side_effect=error))
    else:
        monkeypatch.setattr(push_transport.tempfile, "TemporaryDirectory", Mock(side_effect=error))
    assert runner.module.inspect_lease("origin", "refs/heads/feature") == 1
    output = capsys.readouterr()
    assert "FAKE_URL_SECRET_508" not in output.out + output.err
    assert "diagnostic output withheld" in output.out


@pytest.mark.parametrize("url,label", [
    ("https://fake-user:fake-pass@example.com:8443/fake-path?fake-query#fake-fragment",
     "https://example.com:8443"),
    ("ssh://fake-user@[2001:db8::1]:2222/fake-path?fake-query#fake-fragment", "ssh://[2001:db8::1]:2222"),
    ("fake-user@example.com:fake-path?fake-query#fake-fragment", "ssh://example.com"),
    ("fake-user@[2001:db8::1]:fake-path", "ssh://[2001:db8::1]"),
    ("file:///fake-path", "endpoint details withheld"),
    ("/fake-path", "endpoint details withheld"),
    ("https://[invalid/fake-path?fake-query", "endpoint details withheld"),
])
def test_endpoint_label_discloses_only_transport_host_and_port(url, label):
    assert push_transport._endpoint_label(url) == label


@pytest.mark.parametrize("approval", [None, "invalid"])
def test_inspection_uses_push_endpoint_and_does_not_refresh_a_lease(
        repo, runner, monkeypatch, capsys, approval):
    if approval is None:
        monkeypatch.delenv("LMER_FORCE_PUSH_EXPLICITLY_APPROVED_BY_USER")
    else:
        monkeypatch.setenv("LMER_FORCE_PUSH_EXPLICITLY_APPROVED_BY_USER", approval)
    git(repo.source, "remote", "set-url", "--push", "origin", repo.url)
    git(repo.source, "remote", "set-url", "origin", "https://unused.example.com/fetch.git")
    assert runner.module.inspect_lease("origin", "refs/heads/feature") == 0
    assert repo.old + " refs/heads/feature" in capsys.readouterr().out
    assert git(repo.remote, "rev-parse", "refs/heads/feature").stdout.strip() == repo.old
    assert runner.pushes == []
    runner.checks.assert_not_called()
    assert runner.receipts == []


@pytest.mark.parametrize("destination", ["feature", "refs/tags/v1", "refs/heads/missing"])
def test_inspection_refuses_unsupported_or_missing_branch(repo, runner, destination):
    assert runner.module.inspect_lease("origin", destination) != 0
    assert runner.pushes == []


def test_inspection_cannot_be_combined_with_push(repo, runner):
    with pytest.raises(SystemExit) as error:
        runner.module.parse_args(["--inspect-lease", "refs/heads/feature",
                                  "--force-with-lease", "refs/heads/feature", repo.old])
    assert error.value.code == 2


def test_pre_push_hook_is_preserved_and_can_refuse(repo, runner):
    hooks = repo.tmp / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-push"
    hook.write_text("#!/bin/sh\nexit 23\n")
    hook.chmod(0o755)
    git(repo.source, "config", "core.hooksPath", str(hooks))
    rc = leased(repo, runner)
    assert rc != 0
    assert len(runner.pushes) == 1
    assert git(repo.remote, "rev-parse", "refs/heads/feature").stdout.strip() == repo.old
    assert_receipt(runner, "pass", rc)


def test_snapshot_keeps_credentials_proxy_precedence_and_cleans_up(repo, runner, monkeypatch):
    included = repo.tmp / "transport-config"
    included.write_text('[credential]\n helper = first\n helper =\n helper = second\n'
                        '[http "https://example.com"]\n proxy = http://proxy.example.com:3128\n'
                        '[core]\n sshCommand = ssh -o BatchMode=yes\n')
    global_config = repo.tmp / "global-config"
    global_config.write_text('[test]\n scope = global\n')
    git(repo.source, "config", "include.path", str(included))
    git(repo.source, "config", "remote.origin.proxy", "http://remote-proxy.example.com:3128")
    git(repo.source, "config", "test.scope", "local")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "test.scope")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "command")
    captured = []

    def run(command, **kwargs):
        if is_gate_push(command):
            kwargs.setdefault("timeout", CLI)
            env = kwargs["env"]
            captured.append(Path(env["GIT_DIR"]))
            included.write_text('[credential]\n helper = denied\n')
            def read(*args):
                return push_transport.git("config", *args, env=env)
            assert read("--get-all", "credential.helper").splitlines() == ["first", "", "second"]
            assert read("--get-urlmatch", "http.proxy", "https://example.com/path") == "http://proxy.example.com:3128"
            assert read("--get", "remote.origin.proxy") == "http://remote-proxy.example.com:3128"
            assert read("--get", "core.sshCommand") == "ssh -o BatchMode=yes"
            assert read("--get-all", "test.scope").splitlines() == ["global", "local", "command"]
            assert read("--get", "test.scope") == "command"
        return runner.real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    assert leased(repo, runner) == 0
    assert len(captured) == 1
    assert not captured[0].exists()


def test_credential_helper_is_used_from_the_snapshot(repo, runner, monkeypatch, capsys):
    helper = repo.tmp / "fake-credential-helper"
    helper.write_text("#!/bin/sh\nprintf 'username=fake-user\npassword=fake-pass\n'\n")
    helper.chmod(0o755)
    git(repo.source, "config", "credential.helper", str(helper))
    checked = []

    def run(command, **kwargs):
        if is_gate_push(command):
            kwargs.setdefault("timeout", CLI)
            git(repo.source, "config", "credential.helper", "/no-such-helper")
            result = runner.real_run(
                ["git", "credential", "fill"], env=kwargs["env"],
                input="protocol=https\nhost=git.example.com\n\n", capture_output=True,
                text=True, timeout=CLI, check=True)
            assert "username=fake-user\npassword=fake-pass" in result.stdout
            checked.append(True)
        return runner.real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    assert leased(repo, runner) == 0
    assert checked == [True]
    output = capsys.readouterr()
    assert "fake-pass" not in output.out + output.err


@pytest.mark.parametrize("mode", ["multiple", "symbolic"])
def test_inspection_refuses_multiple_or_symbolic_destinations(repo, runner, mode):
    if mode == "multiple":
        git(repo.source, "remote", "set-url", "--add", "--push", "origin", repo.url)
        git(repo.source, "remote", "set-url", "--add", "--push", "origin", repo.url)
    else:
        git(repo.remote, "symbolic-ref", "refs/heads/alias", "refs/heads/feature")
    destination = "refs/heads/alias" if mode == "symbolic" else "refs/heads/feature"
    assert runner.module.inspect_lease("origin", destination) != 0
    assert runner.pushes == []


def test_reftable_backend_is_explicitly_unsupported(repo, runner, capsys):
    # Git 2.47 cannot migrate reflogs; this fixture only needs the live refs.
    shutil.rmtree(repo.source / ".git" / "logs")
    git(repo.source, "refs", "migrate", "--ref-format=reftable")
    assert git(repo.source, "rev-parse", "--show-ref-format").stdout.strip() == "reftable"
    assert git(repo.source, "rev-parse", "HEAD").stdout.strip() == repo.head
    assert leased(repo, runner) != 0
    assert "Leased pushes require the files reference backend" in capsys.readouterr().out
    assert runner.pushes == []
    assert git(repo.remote, "rev-parse", "refs/heads/feature").stdout.strip() == repo.old


def test_snapshot_preserves_config_escapes_and_ignores_config_only_override(repo, runner, monkeypatch):
    literal = 'quote" backslash\\ and\nnewline\ttab'
    git(repo.source, "config", "credential.fixture", literal)
    unused = repo.tmp / "config-command-only"
    unused.write_text('[credential]\n fixture = incorrect\n')
    monkeypatch.setenv("GIT_CONFIG", str(unused))
    assert leased(repo, runner) == 0
    assert "credential.fixture\n" + literal + "\0" in runner.push_configs[0]
