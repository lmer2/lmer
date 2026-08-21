"""Exactness and safety tests for opt-in full pre-commit pass reuse."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from lmer_cli import precommit_cache


@pytest.fixture
def cache_inputs(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    config = root / ".pre-commit-config.yaml"
    config.write_text("repos:\n- repo: example\n  rev: v1\n")
    executable = tmp_path / "pre-commit"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    cache = tmp_path / "cache"
    monkeypatch.setenv(precommit_cache.CACHE_DIR_ENV, str(cache))
    monkeypatch.setattr(precommit_cache.shutil, "which", lambda _name: str(executable))

    state = {
        "content": "content-a",
        "git_state": "git-state-a",
        "version": "pre-commit 3.7.1",
    }
    monkeypatch.setattr(
        precommit_cache, "checked_content_digest",
        lambda _run, _root: state["content"],
    )
    monkeypatch.setattr(
        precommit_cache, "_git_state_digest",
        lambda _run, _root, _environment: state["git_state"],
    )

    def run(command, check=True):
        assert command == ["pre-commit", "--version"]
        return 0, state["version"] + "\n", ""

    return root, executable, state, run


def _fingerprint(cache_inputs, argv=None, environment=None):
    root, _executable, _state, run = cache_inputs
    return precommit_cache.compute_fingerprint(
        run,
        root,
        ["pre-commit"],
        argv or ["pre-commit", "run", "--all-files"],
        environment if environment is not None else {"PATH": "/bin", "LANG": "C"},
    )


def test_every_declared_identity_dimension_changes_the_key(cache_inputs):
    root, executable, state, _run = cache_inputs
    baseline = _fingerprint(cache_inputs)
    assert baseline is not None

    variants = []
    state["content"] = "content-b"
    variants.append(_fingerprint(cache_inputs))
    state["content"] = "content-a"
    state["git_state"] = "git-state-b"
    variants.append(_fingerprint(cache_inputs))
    state["git_state"] = "git-state-a"
    root.joinpath(".pre-commit-config.yaml").write_text(
        "repos:\n- repo: example\n  rev: v2\n"
    )
    variants.append(_fingerprint(cache_inputs))
    root.joinpath(".pre-commit-config.yaml").write_text(
        "repos:\n- repo: example\n  rev: v1\n"
    )
    executable.write_text("#!/bin/sh\n# changed executable\n")
    variants.append(_fingerprint(cache_inputs))
    executable.write_text("#!/bin/sh\n")
    state["version"] = "pre-commit 3.7.2"
    variants.append(_fingerprint(cache_inputs))
    state["version"] = "pre-commit 3.7.1"
    variants.append(
        _fingerprint(cache_inputs, argv=["pre-commit", "run", "--all-files", "--verbose"])
    )
    variants.append(_fingerprint(cache_inputs, environment={"PATH": "/other"}))

    assert all(item is not None and item.key != baseline.key for item in variants)


def test_environment_is_exact_even_for_test_cache_volatile_names(cache_inputs):
    first = _fingerprint(cache_inputs, environment={"PWD": "/one"})
    second = _fingerprint(cache_inputs, environment={"PWD": "/two"})

    assert first is not None and second is not None
    assert first.key != second.key


def test_only_current_exact_pass_is_reused(cache_inputs):
    fingerprint = _fingerprint(cache_inputs)
    path = precommit_cache.record_pass(fingerprint, now=1_000)

    assert path is not None
    assert stat_mode(path.parent) == 0o700
    assert stat_mode(path) == 0o600
    assert precommit_cache.read_pass(fingerprint, now=1_001) is not None
    assert precommit_cache.read_pass(
        fingerprint, now=1_000 + precommit_cache.MAX_ENTRY_AGE_SECONDS + 1
    ) is None
    assert precommit_cache.read_pass(fingerprint, now=999) is None


def test_tampered_or_non_pass_entry_is_a_miss(cache_inputs):
    fingerprint = _fingerprint(cache_inputs)
    path = precommit_cache.record_pass(fingerprint, now=1_000)
    assert path is not None
    entry = json.loads(path.read_text())
    entry["outcome"] = "fail"
    path.write_text(json.dumps(entry))

    assert precommit_cache.read_pass(fingerprint, now=1_001) is None


def test_unknown_input_fails_soft_without_an_entry(cache_inputs, monkeypatch):
    monkeypatch.setattr(
        precommit_cache, "checked_content_digest", lambda _run, _root: None
    )
    fingerprint = _fingerprint(cache_inputs)

    assert fingerprint is None
    assert precommit_cache.record_pass(fingerprint) is None
    assert precommit_cache.read_pass(fingerprint) is None


def test_cache_refuses_a_symlink_directory(cache_inputs, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    cache = Path(os.environ[precommit_cache.CACHE_DIR_ENV])
    cache.symlink_to(target, target_is_directory=True)

    fingerprint = _fingerprint(cache_inputs)
    assert precommit_cache.record_pass(fingerprint) is None
    assert precommit_cache.read_pass(fingerprint) is None


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, text=True, capture_output=True
    )


def _runner(repo: Path):
    def run(command, check=True):
        result = subprocess.run(
            command, cwd=repo, check=False, text=True, capture_output=True
        )
        if check:
            result.check_returncode()
        return result.returncode, result.stdout, result.stderr
    return run


def test_checked_content_survives_a_landing_train_commit(tmp_path):
    """Committing one staged slice must not move bytes ``--all-files`` reads."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    repo.joinpath("one.txt").write_text("old one\n")
    repo.joinpath("two.txt").write_text("old two\n")
    _git(repo, "add", "one.txt", "two.txt")
    _git(repo, "commit", "-m", "base")

    repo.joinpath("one.txt").write_text("new one\n")
    repo.joinpath("two.txt").write_text("new two\n")
    _git(repo, "add", "one.txt")
    before = precommit_cache.checked_content_digest(_runner(repo), repo)
    _git(repo, "commit", "-m", "land one")
    after = precommit_cache.checked_content_digest(_runner(repo), repo)

    assert before == after


def test_staged_addition_moves_hook_state_when_checked_content_does_not(tmp_path):
    """A soft reset must invalidate the check-added-large-files verdict."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "commit", "--allow-empty", "-m", "base")
    repo.joinpath("big.bin").write_bytes(b"x" * 600_000)
    _git(repo, "add", "big.bin")
    _git(repo, "commit", "-m", "add binary")
    run = _runner(repo)
    environment = {"HOME": str(tmp_path / "home")}

    committed_content = precommit_cache.checked_content_digest(run, repo)
    committed_state = precommit_cache._git_state_digest(run, repo, environment)
    _git(repo, "reset", "--soft", "HEAD~1")
    staged_content = precommit_cache.checked_content_digest(run, repo)
    staged_state = precommit_cache._git_state_digest(run, repo, environment)

    assert committed_content == staged_content
    assert committed_state is not None and staged_state is not None
    assert committed_state != staged_state


def test_audited_git_hook_state_moves_its_own_digest(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    run = _runner(repo)
    environment = {"HOME": str(tmp_path / "home")}
    before = precommit_cache._git_state_digest(run, repo, environment)

    attributes = Path(
        run(["git", "rev-parse", "--git-path", "info/attributes"], False)[1].strip()
    )
    if not attributes.is_absolute():
        attributes = repo / attributes
    attributes.parent.mkdir(parents=True, exist_ok=True)
    attributes.write_text("*.bin filter=lfs\n")

    after = precommit_cache._git_state_digest(run, repo, environment)
    assert before is not None and after is not None and before != after


def test_attributes_symlink_is_keyed_by_the_bytes_git_reads(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    run = _runner(repo)
    environment = {"HOME": str(tmp_path / "home")}
    target = tmp_path / "attributes"
    target.write_text("*.bin filter=lfs\n")
    attributes = Path(
        run(["git", "rev-parse", "--git-path", "info/attributes"], False)[1].strip()
    )
    if not attributes.is_absolute():
        attributes = repo / attributes
    attributes.parent.mkdir(parents=True, exist_ok=True)
    attributes.symlink_to(target)
    before = precommit_cache._git_state_digest(run, repo, environment)

    target.write_text("*.bin -filter\n")

    after = precommit_cache._git_state_digest(run, repo, environment)
    assert before is not None and after is not None and before != after
