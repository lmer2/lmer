"""The platform hands ``lmer`` a supplied ``repo_url`` when the target cannot name one (issue #545)."""
import pytest

from lmer_cli.cli import parse_args
from lmer_platform import config as cfg, spawn
from work_repo.run_state import derive_slug

REPO = "https://gitlab.example.com/group/project.git"
THREAD = "https://example.slack.com/archives/C0123ABCD/p1700000000000001"
OTHER = "https://example.slack.com/archives/C0123ABCD/p1700000000000002"
MATRIX = "https://matrix.to/#/!room:example.org/$event?via=example.org"


@pytest.fixture
def config(tmp_path):
    return cfg.load({"lmer_bin": "/bin/true"})


def _argv(config, **fields):
    request = spawn.SpawnRequest(**{"taskdef": "triage", "repo_url": REPO, **fields}).validate()
    return request, spawn._build_command(config, request)


def test_a_label_target_carries_the_repo_url_and_stays_the_target(config):
    _request, command = _argv(config, target="request-123")
    namespace, rest = parse_args(command[1:])
    assert rest == []
    assert namespace.target == ["request-123"]
    assert namespace.repo_url == REPO


@pytest.mark.parametrize("thread", [THREAD, MATRIX])
def test_a_thread_target_is_connected_once_and_carries_the_repo_url(config, thread):
    request, command = _argv(config, target=thread)
    assert request.targets == [thread]
    namespace, _rest = parse_args(command[1:])
    assert namespace.target == [thread]
    assert namespace.repo_url == REPO
    request, command = _argv(config, target=thread, targets=[thread])
    assert request.targets == [thread]
    assert command.count(thread) == 1


def test_two_threads_are_two_runs(config):
    identities = {spawn.derive_run_identity(spawn.SpawnRequest(taskdef="triage", target=thread, repo_url=REPO), REPO)
                  for thread in (THREAD, OTHER)}
    assert len(identities) == 2
    assert {slug for _host, _project, slug in identities} == {derive_slug("triage", THREAD),
                                                               derive_slug("triage", OTHER)}


@pytest.mark.parametrize("target", [
    "https://gitlab.example.com/group/project/-/issues/7",
    "https://gitlab.example.com/group/project",
])
def test_a_target_that_names_its_own_repository_keeps_its_argv(config, target):
    request, command = _argv(config, target=target)
    assert request.targets is None
    assert not any(arg.startswith(spawn.REPO_URL_FLAG) for arg in command)


def test_a_thread_without_a_repo_url_is_left_to_lmer_as_before(config):
    request = spawn.SpawnRequest(taskdef="chat", target=THREAD).validate()
    assert request.targets is None
    assert not any(arg.startswith(spawn.REPO_URL_FLAG) for arg in spawn._build_command(config, request))


def test_the_emitted_repo_url_never_carries_a_credential(config):
    _request, command = _argv(config, target="label", repo_url="https://oauth2:secret-value@gitlab.example.com/a/b.git")
    assert "secret-value" not in " ".join(command)
    assert any(arg.startswith(f"{spawn.REPO_URL_FLAG}=https://") for arg in command)


def test_an_uppercase_scheme_is_scrubbed_as_a_target_and_refused_as_a_repo_url():
    url = "HTTPS://oauth2:secret-value@gitlab.example.com/group/project.git"
    assert "secret-value" not in spawn._plain_repo_url_from_target(url)
    with pytest.raises(spawn.SpawnError) as refused:
        spawn.SpawnRequest(taskdef="triage", target="label", repo_url=url).validate()
    assert "secret-value" not in str(refused.value)


def test_the_flag_is_reserved_in_extra_args(config):
    with pytest.raises(spawn.SpawnError):
        spawn._build_command(config, spawn.SpawnRequest(taskdef="triage", target="label",
                                                        extra_args=("--repo-url", REPO)))


@pytest.mark.parametrize("taskdef,target", [
    ("chat", THREAD), ("chat", MATRIX), ("triage", "checkout"), ("triage", "./checkout"), ("triage", "{abs}"),
    ("triage", "gone/.."), ("triage", "./gone"), ("triage", "../gone"), ("triage", "~/checkout"), ("triage", "/srv/gone"),
])
def test_a_recorded_repo_url_leaves_chat_threads_and_paths_as_they_were(
        config, tmp_path, monkeypatch, taskdef, target):
    """Existing paths, and missing ones spelled as paths, keep their argv: lmer still fails on the latter."""
    (tmp_path / "checkout").mkdir()
    monkeypatch.chdir(tmp_path)
    request, command = _argv(config, taskdef=taskdef, target=target.format(abs=tmp_path / "checkout"))
    assert request.targets is None
    assert not any(arg.startswith(spawn.REPO_URL_FLAG) for arg in command)


@pytest.mark.parametrize("target", ["request-123", "2026-10/request-123", "checkout/gone"])
def test_a_target_lmer_would_miss_as_a_path_carries_the_repo_url(config, tmp_path, monkeypatch, target):
    monkeypatch.chdir(tmp_path)
    _request, command = _argv(config, target=target)
    namespace, _rest = parse_args(command[1:])
    assert namespace.target == [target]
    assert namespace.repo_url == REPO


@pytest.mark.parametrize("url", ["ssh://git@gitlab.example.com/a/b.git", "git@gitlab.example.com:a/b.git"])
def test_an_ssh_user_is_kept_in_the_emitted_repo_url(config, url):
    _request, command = _argv(config, target="label", repo_url=url)
    assert f"{spawn.REPO_URL_FLAG}={url}" in command


@pytest.mark.parametrize("url", [
    "ssh://deploy:s3cret@gitlab.example.com/a/b.git", "ssh://oauth2:s3cret@gitlab.example.com:2222/a/b",
    "https://fixture-user:fixture/s3cret@gitlab.example.com/group/project.git",
])
def test_a_non_http_repo_url_with_a_secret_is_refused_without_echoing_it(url):
    with pytest.raises(spawn.SpawnError, match="embeds a credential") as refused:
        spawn.SpawnRequest(taskdef="triage", target="label", repo_url=url).validate()
    assert "s3cret" not in str(refused.value)


def test_a_repo_url_that_is_not_a_url_is_refused_before_the_spawn(config):
    with pytest.raises(spawn.SpawnError, match="repository URL"):
        spawn.SpawnRequest(taskdef="triage", target="label", repo_url="request-123").validate()
