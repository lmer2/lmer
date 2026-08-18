"""Regression coverage for issue #310's container Git credential boundary."""

import os
import stat
import subprocess
from pathlib import Path

import pytest

from lmer_cli.container import clone_and_exec


TEST_CREDENTIAL = "issue310-test-credential"


def _git(*args, cwd=None, input_text=None):
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        input=input_text,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def session_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _upstream(tmp_path):
    repo = tmp_path / "upstream"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    (repo / "README.md").write_text("hello\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)
    return repo


def test_credential_moves_to_a_mode_0600_session_file(session_home):
    supplied = (
        f"https://oauth2:{TEST_CREDENTIAL}@git.example.com/org/project.git"
    )

    clean, credential = clone_and_exec._write_session_git_credential(supplied)

    assert clean == "https://git.example.com/org/project.git"
    assert credential is not None
    assert credential.path.parent == session_home
    assert stat.S_ISREG(credential.path.stat().st_mode)
    assert stat.S_IMODE(credential.path.stat().st_mode) == 0o600
    assert credential.path.read_text() == supplied + "\n"
    assert TEST_CREDENTIAL not in credential.helper
    assert TEST_CREDENTIAL not in credential.scope_url


def test_clone_command_contains_clean_url_and_only_a_helper_reference(session_home):
    supplied = (
        f"https://oauth2:{TEST_CREDENTIAL}@git.example.com/org/project.git"
    )
    clean, credential = clone_and_exec._write_session_git_credential(supplied)

    command = clone_and_exec._clone_cmd(
        clean, Path("/workspace"), credential=credential
    )
    rendered = " ".join(command)

    assert supplied not in rendered
    assert TEST_CREDENTIAL not in rendered
    assert clean in command
    assert credential is not None
    assert str(credential.path) in rendered


def test_username_only_https_credential_never_reaches_git_argv(session_home):
    supplied = f"https://{TEST_CREDENTIAL}@git.example.com/org/project.git"

    clean, credential = clone_and_exec._write_session_git_credential(supplied)
    command = clone_and_exec._clone_cmd(
        clean, Path("/workspace"), credential=credential
    )

    assert clean == "https://git.example.com/org/project.git"
    assert credential is not None
    assert credential.path.read_text() == (
        f"https://{TEST_CREDENTIAL}:@git.example.com/org/project.git\n"
    )
    assert TEST_CREDENTIAL not in " ".join(command)
    assert supplied not in " ".join(command)
    output = _git(
        "-c", f"credential.{clean}.useHttpPath=true",
        "-c", f"credential.{clean}.helper=",
        "-c", f"credential.{clean}.helper={credential.helper}",
        "credential", "fill",
        input_text=(
            "protocol=https\n"
            "host=git.example.com\n"
            "path=org/project.git\n\n"
        ),
    )
    assert f"username={TEST_CREDENTIAL}" in output
    assert "password=" in output


@pytest.mark.parametrize("supplied", [
    "https://@git.example.com/org/project.git",
    "https://:@git.example.com/org/project.git",
])
def test_empty_https_userinfo_is_scrubbed_without_a_helper(session_home, supplied):
    clean, credential = clone_and_exec._write_session_git_credential(supplied)
    command = clone_and_exec._clone_cmd(clean, Path("/workspace"))

    assert clean == "https://git.example.com/org/project.git"
    assert credential is None
    assert supplied not in " ".join(command)
    assert clean in command
    assert list(session_home.iterdir()) == []


def test_ssh_protocol_username_is_not_treated_as_a_credential(session_home):
    supplied = "ssh://git@git.example.com/org/project.git"

    clean, credential = clone_and_exec._write_session_git_credential(supplied)

    assert clean == supplied
    assert credential is None
    assert list(session_home.iterdir()) == []


def test_real_clone_keeps_remote_and_repository_config_credential_free(
    tmp_path, session_home
):
    upstream = _upstream(tmp_path)
    supplied = f"file://oauth2:{TEST_CREDENTIAL}@{upstream}"
    destination = tmp_path / "clone"

    clone_and_exec.ensure_clone(destination, supplied, None, None)

    remote = _git("remote", "get-url", "origin", cwd=destination)
    remote_listing = _git("remote", "-v", cwd=destination)
    config = (destination / ".git" / "config").read_text()
    credential_files = list(session_home.glob(".git-credentials-*-*"))
    assert remote == f"file://{upstream}"
    assert TEST_CREDENTIAL not in remote
    assert TEST_CREDENTIAL not in remote_listing
    assert TEST_CREDENTIAL not in config
    assert "credential" in config
    assert len(credential_files) == 1
    assert stat.S_IMODE(credential_files[0].stat().st_mode) == 0o600


def test_persisted_helper_supplies_the_credential_for_later_git_operations(
    tmp_path, session_home
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", cwd=repo)
    supplied = (
        f"https://oauth2:{TEST_CREDENTIAL}@git.example.com/org/project.git"
    )
    _, credential = clone_and_exec._write_session_git_credential(supplied)
    clone_and_exec._persist_git_credential_helper(repo, credential)

    output = _git(
        "credential",
        "fill",
        cwd=repo,
        input_text=(
            "protocol=https\n"
            "host=git.example.com\n"
            "path=org/project.git\n\n"
        ),
    )
    config = (repo / ".git" / "config").read_text()
    assert "username=oauth2" in output
    assert f"password={TEST_CREDENTIAL}" in output
    assert TEST_CREDENTIAL not in config


def test_doctor_helper_can_reuse_work_credential_for_a_same_host_source(
    tmp_path, session_home
):
    supplied = (
        f"https://oauth2:{TEST_CREDENTIAL}@git.example.com/org/work.git"
    )
    _, credential = clone_and_exec._write_session_git_credential(supplied)
    assert credential is not None
    source_url = "https://git.example.com/org/taskdefs.git"

    output = _git(
        "-c", f"credential.{source_url}.useHttpPath=false",
        "-c", f"credential.{source_url}.helper=",
        "-c", f"credential.{source_url}.helper={credential.helper}",
        "credential", "fill",
        input_text=(
            "protocol=https\n"
            "host=git.example.com\n"
            "path=org/taskdefs.git\n\n"
        ),
    )

    assert "username=oauth2" in output
    assert f"password={TEST_CREDENTIAL}" in output


def test_same_host_repositories_keep_distinct_session_credentials(
    tmp_path, session_home
):
    first_url = "https://oauth2:first-value@git.example.com/org/first.git"
    second_url = "https://oauth2:second-value@git.example.com/org/second.git"
    first_repo = tmp_path / "first"
    second_repo = tmp_path / "second"
    first_repo.mkdir()
    second_repo.mkdir()
    _git("init", cwd=first_repo)
    _git("init", cwd=second_repo)

    _, first = clone_and_exec._write_session_git_credential(first_url)
    _, second = clone_and_exec._write_session_git_credential(second_url)
    clone_and_exec._persist_git_credential_helper(first_repo, first)
    clone_and_exec._persist_git_credential_helper(second_repo, second)

    assert first is not None and second is not None
    assert first.path != second.path
    assert first.path.read_text() == first_url + "\n"
    assert second.path.read_text() == second_url + "\n"
    for repo, path, expected in (
        (first_repo, "org/first.git", "first-value"),
        (second_repo, "org/second.git", "second-value"),
    ):
        output = _git(
            "credential", "fill", cwd=repo,
            input_text=(
                "protocol=https\n"
                "host=git.example.com\n"
                f"path={path}\n\n"
            ),
        )
        assert f"password={expected}" in output
        assert "first-value" not in (repo / ".git" / "config").read_text()
        assert "second-value" not in (repo / ".git" / "config").read_text()


def test_operator_owned_existing_clone_is_never_modified(tmp_path, session_home):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", cwd=repo)
    supplied = (
        f"https://oauth2:{TEST_CREDENTIAL}@git.example.com/org/project.git"
    )
    _git("remote", "add", "origin", supplied, cwd=repo)
    config_before = (repo / ".git" / "config").read_text()

    clone_and_exec.ensure_clone(repo, supplied, None, None)

    assert _git("remote", "get-url", "origin", cwd=repo) == supplied
    assert (repo / ".git" / "config").read_text() == config_before
    assert list(session_home.iterdir()) == []


def test_service_mode_scrubs_env_without_touching_checkout_config(
    tmp_path, session_home, monkeypatch
):
    supplied = (
        f"https://oauth2:{TEST_CREDENTIAL}@git.example.com/org/project.git"
    )
    work_path = tmp_path / "work"
    commands = []
    secondary_calls = []
    monkeypatch.setenv("LMER_SERVICE_MODE", "1")
    monkeypatch.setenv("LMER_SERVICE_NAME", "test")
    monkeypatch.setenv("LMER_REPO_URL", supplied)
    monkeypatch.setenv("LMER_WORK_REPO", "https://git.example.com/org/work.git")
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work_path))
    monkeypatch.setenv(
        "LMER_SECONDARY_TARGETS",
        "https://git.example.com/org/other/-/merge_requests/9",
    )
    monkeypatch.delenv("LMER_NO_REPO", raising=False)
    monkeypatch.setattr(
        clone_and_exec, "check_call", lambda command: commands.append(command)
    )
    monkeypatch.setattr(clone_and_exec, "ensure_clone", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        clone_and_exec,
        "clone_secondary_mr",
        lambda *args, **kwargs: secondary_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(clone_and_exec, "ensure_work_repo_directory", lambda *args: None)
    monkeypatch.setattr(clone_and_exec, "provision_documentation", lambda *args: None)
    monkeypatch.setattr(clone_and_exec, "clone_aux_repos", lambda *args: None)
    monkeypatch.setattr(clone_and_exec, "setup_napkin_and_links", lambda *args, **kwargs: None)
    monkeypatch.setattr(clone_and_exec, "_trust_mise_config", lambda *args: None)
    monkeypatch.setattr(clone_and_exec, "find_runner", lambda *args: "/bin/true")
    monkeypatch.setattr(clone_and_exec, "dispatch_runner", lambda *args: 0)

    rc = clone_and_exec.main(["--", "claude-runner"])

    assert rc == 0
    assert os.environ["LMER_REPO_URL"] == (
        "https://git.example.com/org/project.git"
    )
    assert TEST_CREDENTIAL not in os.environ["LMER_REPO_URL"]
    assert not any("--local" in command for command in commands)
    assert not any("set-url" in command for command in commands)
    assert secondary_calls
    assert secondary_calls[0][1] == {"persist_local_config": False}


def test_existing_lmer_clone_is_migrated_to_a_clean_remote(tmp_path, session_home):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", cwd=repo)
    stale = f"https://oauth2:{TEST_CREDENTIAL}@git.example.com/org/project.git"
    clean = "https://git.example.com/org/project.git"
    _git("remote", "add", "origin", stale, cwd=repo)
    scope = f"credential.{clean}"
    _git("config", "--local", f"{scope}.useHttpPath", "true", cwd=repo)
    _git("config", "--local", f"{scope}.helper", "", cwd=repo)
    _git(
        "config", "--local", "--add", f"{scope}.helper",
        "store --file=/home/developer/.git-credentials-git_example_com-old",
        cwd=repo,
    )

    clone_and_exec.ensure_clone(
        repo, clean, None, None, manage_existing=True
    )

    assert _git("remote", "get-url", "origin", cwd=repo) == (
        "https://git.example.com/org/project.git"
    )
    config = (repo / ".git" / "config").read_text()
    assert TEST_CREDENTIAL not in config
    assert ".git-credentials-" not in config


def test_declared_source_helper_survives_clean_aux_pass(tmp_path, session_home):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", cwd=repo)
    supplied = (
        f"https://oauth2:{TEST_CREDENTIAL}@git.example.com/org/project.git"
    )
    clean, credential = clone_and_exec._write_session_git_credential(supplied)
    assert credential is not None
    _git("remote", "add", "origin", clean, cwd=repo)
    clone_and_exec._persist_git_credential_helper(repo, credential)

    result = clone_and_exec.ensure_clone(
        repo, clean, None, None, manage_existing=True
    )

    assert result is None
    config = (repo / ".git" / "config").read_text()
    assert str(credential.path) in config
    output = _git(
        "credential", "fill", cwd=repo,
        input_text=(
            "protocol=https\n"
            "host=git.example.com\n"
            "path=org/project.git\n\n"
        ),
    )
    assert f"password={TEST_CREDENTIAL}" in output


def test_managed_existing_clone_refreshes_credential_and_remote(
    tmp_path, session_home
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", cwd=repo)
    _git("remote", "add", "origin", "https://old.example.com/fork.git", cwd=repo)
    supplied = (
        f"https://oauth2:{TEST_CREDENTIAL}@git.example.com/org/project.git"
    )

    credential = clone_and_exec.ensure_clone(
        repo, supplied, None, None, manage_existing=True
    )

    assert credential is not None
    assert _git("remote", "get-url", "origin", cwd=repo) == (
        "https://git.example.com/org/project.git"
    )
    config = (repo / ".git" / "config").read_text()
    assert TEST_CREDENTIAL not in config
    assert str(credential.path) in config


def test_existing_secondary_clone_is_migrated(tmp_path, session_home, monkeypatch):
    target = "https://git.example.com/org/project/-/merge_requests/7"
    repo = tmp_path / "mr-7"
    repo.mkdir()
    _git("init", cwd=repo)
    _git(
        "remote", "add", "origin",
        "https://oauth2:stale-value@git.example.com/org/project.git",
        cwd=repo,
    )
    monkeypatch.setenv("GITLAB_TOKEN_git_example_com", TEST_CREDENTIAL)

    clone_and_exec.clone_secondary_mr(target, tmp_path)

    assert _git("remote", "get-url", "origin", cwd=repo) == (
        "https://git.example.com/org/project.git"
    )
    config = (repo / ".git" / "config").read_text()
    assert TEST_CREDENTIAL not in config
    assert "stale-value" not in config


def test_existing_secondary_clone_on_bind_mount_is_untouched(
    tmp_path, session_home, monkeypatch
):
    target = "https://git.example.com/org/project/-/merge_requests/7"
    repo = tmp_path / "mr-7"
    repo.mkdir()
    _git("init", cwd=repo)
    stale = "https://oauth2:operator-value@git.example.com/org/fork.git"
    _git("remote", "add", "origin", stale, cwd=repo)
    config_before = (repo / ".git" / "config").read_text()
    monkeypatch.setenv("GITLAB_TOKEN_git_example_com", TEST_CREDENTIAL)

    clone_and_exec.clone_secondary_mr(
        target, tmp_path, persist_local_config=False
    )

    assert _git("remote", "get-url", "origin", cwd=repo) == stale
    assert (repo / ".git" / "config").read_text() == config_before
    assert list(session_home.iterdir()) == []


def test_fresh_secondary_clone_on_bind_mount_persists_no_local_helper(
    tmp_path, session_home
):
    upstream = _upstream(tmp_path)
    supplied = f"file://oauth2:{TEST_CREDENTIAL}@{upstream}"
    destination = tmp_path / clone_and_exec.sanitize_task_target(supplied)

    clone_and_exec.clone_secondary_mr(
        supplied, tmp_path, persist_local_config=False
    )

    config = (destination / ".git" / "config").read_text()
    assert _git("remote", "get-url", "origin", cwd=destination) == (
        f"file://{upstream}"
    )
    assert TEST_CREDENTIAL not in config
    assert ".git-credentials-" not in config
    credential_files = list(session_home.glob(".git-credentials-*-*"))
    assert len(credential_files) == 1
