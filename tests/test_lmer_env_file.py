"""Tests for the ``lmer --env-file`` flag (issue #75).

Contract under test
--------------------
``lmer --env-file PATH`` loads ``PATH`` as an additional .env source whose
variables are forwarded into the container env dict. It is the highest
precedence among .env files (above cwd/.env and ~/.lmer/.env) but still below
already-exported environment variables. The flag is purely opt-in: when it is
absent, container env construction is unchanged. A missing explicitly-named
file is non-fatal (warned, then skipped).

The behavioral tests run the real ``main()`` with the container runtime mocked
out (reusing the harness from ``test_lmer_cli_slack_target``) and inspect the
env dict the CLI hands to ``env_args``.
"""

import os
from unittest.mock import patch

from tests.test_lmer_cli_slack_target import (
    _BASE_ENV,
    _make_main_mocks,
    REPO_URL,
)


def _run_main(argv, env_in=None, captured_env=None):
    """Run main() with the standard mock stack and a clean os.environ."""
    env = {**_BASE_ENV, **(env_in or {})}
    with patch.dict(os.environ, env, clear=True):
        with _make_main_mocks(captured_env=captured_env):
            from lmer_cli.cli import main

            return main(argv)


class TestEnvFileForwarding:
    """An explicit --env-file's variables reach the container env dict."""

    def test_env_file_vars_forwarded_into_container(self, tmp_path):
        """A var present only in --env-file (and not hardcoded, not in cwd
        .env, not exported) must appear in the container env dict."""
        env_file = tmp_path / "deploy.env"
        env_file.write_text(
            "GITLAB_TOKEN_example_com=glpat-fixturetoken\n"
            "LMER_PUSH_ALLOW_LIST=owner/*\n"
        )
        captured: dict = {}
        _run_main(
            ["--env-file", str(env_file), "--no-task", "--exec", "true", REPO_URL],
            captured_env=captured,
        )
        assert captured.get("GITLAB_TOKEN_example_com") == "glpat-fixturetoken", (
            "A git token present only in --env-file must be forwarded into the "
            f"container env; captured keys: {sorted(captured)}"
        )
        assert captured.get("LMER_PUSH_ALLOW_LIST") == "owner/*", (
            "A non-hardcoded LMER_* var from --env-file must be forwarded too"
        )

    def test_without_env_file_var_is_absent(self, tmp_path):
        """The same var is absent from the container env when --env-file is not
        passed — the flag is opt-in and changes nothing when unused."""
        captured: dict = {}
        _run_main(
            ["--no-task", "--exec", "true", REPO_URL],
            captured_env=captured,
        )
        assert captured.get("GITLAB_TOKEN_example_com") is None
        assert captured.get("LMER_PUSH_ALLOW_LIST") is None

    def test_env_file_does_not_override_literal_container_keys(self, tmp_path):
        """--env-file must not clobber a container key whose value is a literal
        in the env dict (the .env merge skips keys already present there).

        Keys the env dict derives via ``os.environ.get(...)`` (e.g.
        LMER_WORK_REPO_PATH) remain intentionally overridable by any .env file,
        exactly as cwd/.env and ~/.lmer/.env already are — this guards only the
        truly fixed ones, e.g. GITLAB_REVIEW_FILE."""
        env_file = tmp_path / "deploy.env"
        env_file.write_text("GITLAB_REVIEW_FILE=/hacked\n")
        captured: dict = {}
        _run_main(
            ["--env-file", str(env_file), "--no-task", "--exec", "true", REPO_URL],
            captured_env=captured,
        )
        assert captured.get("GITLAB_REVIEW_FILE") == "review.json", (
            "A literal hardcoded container key must win over --env-file; "
            f"got {captured.get('GITLAB_REVIEW_FILE')!r}"
        )


    def test_env_file_overrides_conflicting_cwd_dotenv(self, tmp_path, monkeypatch):
        """The headline precedence claim: a key set in BOTH the working-dir
        `.env` and `--env-file` resolves to the `--env-file` value in the
        container env. This locks the ordering, which relies on opposite
        conventions in the two load paths (first-wins with --env-file placed
        first in the early load; last-wins with it appended last in the
        container merge)."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("GITLAB_TOKEN_example_com=from-cwd-dotenv\n")
        explicit = tmp_path / "explicit.env"
        explicit.write_text("GITLAB_TOKEN_example_com=from-env-file\n")
        captured: dict = {}
        _run_main(
            ["--env-file", str(explicit), "--no-task", "--exec", "true", REPO_URL],
            captured_env=captured,
        )
        assert captured.get("GITLAB_TOKEN_example_com") == "from-env-file", (
            "--env-file must win over a conflicting cwd .env key; "
            f"got {captured.get('GITLAB_TOKEN_example_com')!r}"
        )


class TestEnvFileMissing:
    """A missing explicitly-named --env-file is non-fatal."""

    def test_missing_env_file_is_not_fatal(self, tmp_path, capsys):
        """main() must still succeed and warn (not crash) when --env-file
        points at a path that does not exist."""
        missing = tmp_path / "nope.env"
        rc = _run_main(
            ["--env-file", str(missing), "--no-task", "--exec", "true", REPO_URL],
        )
        assert rc == 0, f"Missing --env-file must be non-fatal; rc={rc}"
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert str(missing) in combined, (
            f"Expected a warning naming the missing --env-file; got: {combined!r}"
        )

    def test_directory_env_file_is_skipped(self, tmp_path, capsys):
        """A --env-file that exists but is a directory is treated like a missing
        file — warned and skipped — so all three .is_file() checks agree and the
        'skipping' warning stays accurate."""
        a_dir = tmp_path / "envdir"
        a_dir.mkdir()
        rc = _run_main(
            ["--env-file", str(a_dir), "--no-task", "--exec", "true", REPO_URL],
        )
        assert rc == 0, f"A directory --env-file must be non-fatal; rc={rc}"
        captured = capsys.readouterr()
        assert str(a_dir) in (captured.out + captured.err), (
            "Expected a warning naming the directory passed as --env-file"
        )
