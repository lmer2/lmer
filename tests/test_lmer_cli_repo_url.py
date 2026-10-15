"""``lmer --repo-url``: the repository apart from the target (issue #545)."""
import os
from unittest.mock import patch

from lmer_cli import resolve
from lmer_cli.cli import main, parse_args
from tests.test_lmer_cli_slack_target import (
    _BASE_ENV, _EXPECTED_CHANNEL, _SLACK_URL, _make_main_mocks, _run_main_repoless,
)
from work_repo.run_state import derive_slug

REPO = "https://git.example.com/example/project"
OTHER_THREAD = "https://myworkspace.slack.com/archives/C0123ABCDEF/p1700000009000001"


_REAL_NORMALIZE = resolve.normalize_repo_url


def _launch(argv):
    """main() under the standard mocks, but with the real repo resolution."""
    captured: dict = {}
    with patch.dict(os.environ, {**_BASE_ENV, "SLACK_BOT_TOKEN": "xoxb-test-bot-token"}, clear=True):
        with _make_main_mocks(captured_env=captured), \
                patch("lmer_cli.cli.resolve.normalize_repo_url", side_effect=_REAL_NORMALIZE):
            return main(argv), captured


def test_a_label_target_with_a_repo_url_is_the_task_target_not_a_path():
    rc, env = _launch(["triage", "request-123", "--repo-url", REPO])
    assert rc == 0
    assert env["LMER_TASK_TARGET"] == "request-123"
    assert env["LMER_REPO_URL"] == REPO
    assert env.get("LMER_NO_REPO") is None


def test_a_thread_target_with_a_repo_url_runs_a_non_chat_task_per_thread():
    rc, env = _launch(["triage", _SLACK_URL, "--repo-url", REPO])
    assert rc == 0
    assert env["LMER_TASK_TARGET"] == _SLACK_URL
    assert env["LMER_REPO_URL"] == REPO
    assert env["LMER_SLACK_CHANNEL"] == _EXPECTED_CHANNEL
    assert env.get("LMER_NO_REPO") is None
    assert derive_slug("triage", _SLACK_URL) != derive_slug("triage", OTHER_THREAD)


def test_a_resource_target_keeps_its_own_identity_but_clones_the_named_repo():
    issue = "https://git.example.com/other/thing/-/issues/7"
    rc, env = _launch(["develop", issue, "--repo-url", REPO])
    assert rc == 0
    assert env["LMER_TASK_TARGET"] == issue
    assert env["LMER_REPO_URL"] == REPO


def test_a_repo_url_that_is_not_a_url_is_refused(capsys):
    rc, _env = _launch(["triage", "label", "--repo-url", "request-123"])
    assert rc == 2
    assert "--repo-url needs a repository URL" in capsys.readouterr().out


def test_without_a_repo_url_a_non_chat_thread_is_refused_with_the_flag_named(capsys):
    assert _run_main_repoless(["review", _SLACK_URL]) != 0
    assert "pass --repo-url URL" in capsys.readouterr().out


def test_without_a_repo_url_a_label_still_needs_a_repository(tmp_path, monkeypatch, capsys):
    # Schema 2 classifies a missing non-URL positional as a work item (spec §2),
    # so the refusal is the cwd-origin fallback's, not a missing local path.
    monkeypatch.chdir(tmp_path)
    assert _launch(["develop", "request-123"])[0] != 0
    assert "could not infer git origin" in capsys.readouterr().out


def test_a_thread_after_the_repo_url_is_refused_rather_than_dropped(capsys):
    rc, _env = _launch(["triage", "request-123", "--repo-url", REPO, _SLACK_URL])
    assert rc == 2
    assert "after an option would be ignored" in capsys.readouterr().out


def test_a_thread_before_the_repo_url_is_connected_beside_the_label():
    rc, env = _launch(["triage", "request-123", _SLACK_URL, "--repo-url", REPO])
    assert rc == 0
    assert env["LMER_TASK_TARGET"] == "request-123"
    assert env["LMER_SLACK_CHANNEL"] == _EXPECTED_CHANNEL


def test_only_a_bare_word_after_an_option_counts_as_a_stray_target():
    assert parse_args(["chat", REPO, "--exec", "ls", "-la"])[0].stray_positionals == ["ls"]
    assert parse_args(["chat", REPO, "--unknown", "value", "--other=x"])[0].stray_positionals == []


def test_repo_and_repo_url_together_name_two_repositories(capsys):
    rc, _env = _launch(["triage", "request-123", "--repo", REPO, "--repo-url", REPO])
    assert rc == 2
    assert "two repositories named: --repo" in capsys.readouterr().out
