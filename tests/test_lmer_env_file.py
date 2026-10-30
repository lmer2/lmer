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
env dict the CLI hands to ``build_container_env``.
"""

import hashlib
import json
import os
from unittest.mock import patch

import pytest

from lmer_cli.env_sources import DEFAULT_SOURCES_ENV
from lmer_cli.runtime import apply_env_file_defaults
from tests.test_lmer_cli_slack_target import (
    _BASE_ENV,
    _make_main_mocks,
    REPO_URL,
)


def _carrier(**values):
    """Build the ``LMER_PLATFORM_ENV_DEFAULTS`` carrier the platform daemon passes.

    Mirrors ``lmer_cli.env_sources.remember_env_defaults``: each key maps to
    its source label and the sha256 of the value, so ``consume_env_defaults``
    recognizes the values as daemon-loaded defaults.
    """
    return json.dumps(
        {
            name: [
                ".env (lmer state dir)",
                hashlib.sha256(value.encode()).hexdigest(),
            ]
            for name, value in values.items()
        }
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

    @pytest.mark.parametrize(
        "name",
        ["LMER_SLACK_DM_ALLOWED_USERS", "LMER_MATRIX_ALLOWED_USERS", "LMER_MATRIX_OPERATOR_USERS"],
    )
    def test_host_only_user_lists_never_reach_the_container(self, tmp_path, name):
        """Who may DM lmer or answer an operator-only question is decided on
        the host (#599); an env file must not carry the list into a run."""
        env_file = tmp_path / "deploy.env"
        env_file.write_text(f"{name}=@operator:example.org\n")
        captured: dict = {}
        _run_main(
            ["--env-file", str(env_file), "--no-task", "--exec", "true", REPO_URL],
            captured_env=captured,
        )
        assert captured.get(name) is None, f"{name} leaked into the container env"


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


class TestInFileReferenceResolution:
    """Both resolutions of a ``${VAR}`` reference inside a ``.env``.

    The CLI has always resolved these against the file, the Slack listener
    against the active environment; the helper takes the resolution as a
    parameter so neither caller moves (#67).
    """

    def _seeded(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "LMER_ENVREF_BASE=from_file\n"
            "LMER_ENVREF_DERIVED=${LMER_ENVREF_BASE}/repo.git\n"
            "LMER_ENVREF_FILEONLY=solo\n"
            "LMER_ENVREF_DERIVED2=${LMER_ENVREF_FILEONLY}/x\n"
        )
        return [("probe", env_file)]

    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        for name in ("BASE", "DERIVED", "FILEONLY", "DERIVED2"):
            monkeypatch.delenv(f"LMER_ENVREF_{name}", raising=False)

    def test_default_resolves_against_the_file(self, tmp_path, monkeypatch):
        from lmer_cli.runtime import apply_env_file_defaults

        monkeypatch.setenv("LMER_ENVREF_BASE", "from_active")
        sources = apply_env_file_defaults(self._seeded(tmp_path))

        assert os.environ["LMER_ENVREF_BASE"] == "from_active"  # first-wins
        assert os.environ["LMER_ENVREF_DERIVED"] == "from_file/repo.git"
        assert "LMER_ENVREF_DERIVED" in sources

    def test_environ_resolution_uses_the_exported_value(self, tmp_path, monkeypatch):
        """The listener's resolution: a value composed from an exported variable
        is built from what is exported, not from the file's copy of it."""
        from lmer_cli.runtime import apply_env_file_defaults

        monkeypatch.setenv("LMER_ENVREF_BASE", "from_active")
        sources = apply_env_file_defaults(
            self._seeded(tmp_path), references_from_environ=True
        )

        assert os.environ["LMER_ENVREF_BASE"] == "from_active"  # first-wins
        assert os.environ["LMER_ENVREF_DERIVED"] == "from_active/repo.git"
        assert "LMER_ENVREF_DERIVED" in sources
        assert "LMER_ENVREF_BASE" not in sources  # it came from the environment

    def test_only_limits_the_load_and_refuses_environ_resolution(self, tmp_path):
        sources = apply_env_file_defaults(
            self._seeded(tmp_path), only=("LMER_ENVREF_FILEONLY",)
        )
        assert sources == {"LMER_ENVREF_FILEONLY": ".env (probe)"}
        assert "LMER_ENVREF_BASE" not in os.environ
        with pytest.raises(ValueError):
            apply_env_file_defaults(
                self._seeded(tmp_path), references_from_environ=True, only=("X",)
            )

    def test_environ_resolution_still_falls_back_to_the_file(self, tmp_path):
        """A variable the environment does not carry resolves from the file, so
        the flag changes which value wins and never whether one is found."""
        from lmer_cli.runtime import apply_env_file_defaults

        apply_env_file_defaults(self._seeded(tmp_path), references_from_environ=True)

        assert os.environ["LMER_ENVREF_DERIVED2"] == "solo/x"


class TestEnvIsolation:
    """``--env-only`` / ``LMER_ENV_ISOLATED`` (issue #609).

    An isolated launch loads only the explicit ``--env-file`` (if any) and
    already-exported variables; the cwd and state-dir ``.env`` files are
    skipped on both the host seeding path and the container merge.
    """

    def _state_dir(self, monkeypatch, tmp_path):
        state = tmp_path / "state"
        state.mkdir()
        monkeypatch.setattr("lmer_cli.cli.lmer_state_dir", lambda: state)
        return state

    def test_env_only_skips_cwd_and_state_in_container(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        state = self._state_dir(monkeypatch, tmp_path)
        (tmp_path / ".env").write_text(
            "GITLAB_TOKEN_example_com=from-cwd\n"
            "LMER_PUSH_ALLOW_LIST=from-cwd\n"
        )
        (state / ".env").write_text(
            "GITLAB_TOKEN_example_com=from-state\n"
            "LMER_TRUST_MISE=from-state\n"
        )
        explicit = tmp_path / "explicit.env"
        explicit.write_text(
            "GITLAB_TOKEN_example_com=from-env-file\n"
            "LMER_PUSH_ALLOW_LIST=from-env-file\n"
        )
        captured: dict = {}
        _run_main(
            [
                "--env-only",
                "--env-file",
                str(explicit),
                "--no-task",
                "--exec",
                "true",
                REPO_URL,
            ],
            captured_env=captured,
        )
        assert captured.get("GITLAB_TOKEN_example_com") == "from-env-file"
        assert captured.get("LMER_PUSH_ALLOW_LIST") == "from-env-file"
        assert captured.get("LMER_TRUST_MISE") is None, (
            "A state-dir-only key must not reach an isolated container; "
            f"captured keys: {sorted(captured)}"
        )

    def test_env_only_skips_cwd_and_state_in_host_seeding(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.chdir(tmp_path)
        state = self._state_dir(monkeypatch, tmp_path)
        (tmp_path / ".env").write_text("LMER_TRUST_MISE=from-cwd\n")
        (state / ".env").write_text("LMER_TRUST_MISE=from-state\n")
        explicit = tmp_path / "explicit.env"
        explicit.write_text("LMER_PUSH_ALLOW_LIST=from-env-file\n")
        rc = _run_main(
            ["--show-env", "--env-only", "--env-file", str(explicit), REPO_URL]
        )
        assert rc == 0
        output = capsys.readouterr().out + capsys.readouterr().err
        assert "LMER_TRUST_MISE" not in output
        assert "LMER_PUSH_ALLOW_LIST" in output

    def test_lmer_env_isolated_env_var_is_equivalent(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        state = self._state_dir(monkeypatch, tmp_path)
        (state / ".env").write_text("LMER_TRUST_MISE=from-state\n")
        explicit = tmp_path / "explicit.env"
        explicit.write_text("LMER_PUSH_ALLOW_LIST=from-env-file\n")
        captured: dict = {}
        _run_main(
            [
                "--env-file",
                str(explicit),
                "--no-task",
                "--exec",
                "true",
                REPO_URL,
            ],
            env_in={"LMER_ENV_ISOLATED": "1"},
            captured_env=captured,
        )
        assert captured.get("LMER_TRUST_MISE") is None
        assert captured.get("LMER_PUSH_ALLOW_LIST") == "from-env-file"

    def test_lmer_env_isolated_without_env_file_loads_no_dotenv(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        state = self._state_dir(monkeypatch, tmp_path)
        (tmp_path / ".env").write_text("LMER_TRUST_MISE=from-cwd\n")
        (state / ".env").write_text("LMER_PUSH_ALLOW_LIST=from-state\n")
        captured: dict = {}
        _run_main(
            ["--no-task", "--exec", "true", REPO_URL],
            env_in={"LMER_ENV_ISOLATED": "1"},
            captured_env=captured,
        )
        assert captured.get("LMER_TRUST_MISE") is None
        assert captured.get("LMER_PUSH_ALLOW_LIST") is None

    def test_lmer_env_isolated_falsy_does_not_isolate(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        state = self._state_dir(monkeypatch, tmp_path)
        (state / ".env").write_text("LMER_TRUST_MISE=from-state\n")
        captured: dict = {}
        _run_main(
            ["--no-task", "--exec", "true", REPO_URL],
            env_in={"LMER_ENV_ISOLATED": "0"},
            captured_env=captured,
        )
        assert captured.get("LMER_TRUST_MISE") == "from-state"

    def test_env_only_requires_env_file(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        self._state_dir(monkeypatch, tmp_path)
        rc = _run_main(["--env-only", "--no-task", "--exec", "true", REPO_URL])
        assert rc == 2
        output = capsys.readouterr().out + capsys.readouterr().err
        assert "--env-only requires --env-file" in output

    def test_exported_variable_still_wins_when_isolated(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self._state_dir(monkeypatch, tmp_path)
        explicit = tmp_path / "explicit.env"
        explicit.write_text("LMER_HUMAN_IDENTITY=from-env-file\n")
        captured: dict = {}
        _run_main(
            [
                "--env-only",
                "--env-file",
                str(explicit),
                "--no-task",
                "--exec",
                "true",
                REPO_URL,
            ],
            env_in={"LMER_HUMAN_IDENTITY": "from-export"},
            captured_env=captured,
        )
        assert captured.get("LMER_HUMAN_IDENTITY") == "from-export"


class TestContainerEnvPrecedence:
    """The container merge must agree with the host's first-wins order.

    Host seeding reads ``--env-file``, then cwd, then state dir (first wins).
    The container merge is last-wins, so it must queue state dir, cwd, then
    ``--env-file`` to produce the same winner (issue #609).
    """

    def _state_dir(self, monkeypatch, tmp_path):
        state = tmp_path / "state"
        state.mkdir()
        monkeypatch.setattr("lmer_cli.cli.lmer_state_dir", lambda: state)
        return state

    def test_cwd_dotenv_beats_state_dotenv(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        state = self._state_dir(monkeypatch, tmp_path)
        (tmp_path / ".env").write_text("LMER_TRUST_MISE=from-cwd\n")
        (state / ".env").write_text("LMER_TRUST_MISE=from-state\n")
        captured: dict = {}
        _run_main(
            ["--no-task", "--exec", "true", REPO_URL],
            captured_env=captured,
        )
        assert captured.get("LMER_TRUST_MISE") == "from-cwd", (
            "The container merge must let cwd/.env override ~/.lmer/.env, "
            f"matching the host; got {captured.get('LMER_TRUST_MISE')!r}"
        )

    def test_env_file_beats_cwd_and_state(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        state = self._state_dir(monkeypatch, tmp_path)
        (tmp_path / ".env").write_text("LMER_TRUST_MISE=from-cwd\n")
        (state / ".env").write_text("LMER_TRUST_MISE=from-state\n")
        explicit = tmp_path / "explicit.env"
        explicit.write_text("LMER_TRUST_MISE=from-env-file\n")
        captured: dict = {}
        _run_main(
            ["--env-file", str(explicit), "--no-task", "--exec", "true", REPO_URL],
            captured_env=captured,
        )
        assert captured.get("LMER_TRUST_MISE") == "from-env-file"


class TestEnvFileBleedWarning:
    """A non-isolated ``--env-file`` warns when routing/credential keys leak
    from another ``.env`` file (operator decision: routing + token keys only).
    """

    def _state_dir(self, monkeypatch, tmp_path):
        state = tmp_path / "state"
        state.mkdir()
        monkeypatch.setattr("lmer_cli.cli.lmer_state_dir", lambda: state)
        return state

    def test_warns_for_routing_key_from_state_dir(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.chdir(tmp_path)
        state = self._state_dir(monkeypatch, tmp_path)
        (state / ".env").write_text(
            "LMER_NAPKIN_REPO=git@git.example.com:org/napkin.git\n"
        )
        explicit = tmp_path / "explicit.env"
        explicit.write_text("LMER_PUSH_ALLOW_LIST=owner/*\n")
        rc = _run_main(
            ["--env-file", str(explicit), "--no-task", "--exec", "true", REPO_URL]
        )
        assert rc == 0
        output = capsys.readouterr().out + capsys.readouterr().err
        assert "does not isolate this launch" in output
        assert "LMER_NAPKIN_REPO" in output
        assert ".env (lmer state dir)" in output
        assert "--env-only" in output

    def test_warns_for_token_key_from_cwd(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        self._state_dir(monkeypatch, tmp_path)
        (tmp_path / ".env").write_text(
            "GITLAB_TOKEN_example_com=glpat-leaked-value\n"
        )
        explicit = tmp_path / "explicit.env"
        explicit.write_text("LMER_PUSH_ALLOW_LIST=owner/*\n")
        rc = _run_main(
            ["--env-file", str(explicit), "--no-task", "--exec", "true", REPO_URL]
        )
        assert rc == 0
        output = capsys.readouterr().out + capsys.readouterr().err
        assert "GITLAB_TOKEN_example_com" in output
        assert "glpat-leaked-value" not in output, "warning must not print values"

    def test_does_not_warn_for_ordinary_lmer_key(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        state = self._state_dir(monkeypatch, tmp_path)
        (state / ".env").write_text("LMER_TRUST_MISE=from-state\n")
        explicit = tmp_path / "explicit.env"
        explicit.write_text("LMER_PUSH_ALLOW_LIST=owner/*\n")
        rc = _run_main(
            ["--env-file", str(explicit), "--no-task", "--exec", "true", REPO_URL]
        )
        assert rc == 0
        output = capsys.readouterr().out + capsys.readouterr().err
        assert "does not isolate this launch" not in output

    def test_no_warning_when_isolated(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        state = self._state_dir(monkeypatch, tmp_path)
        (state / ".env").write_text(
            "LMER_NAPKIN_REPO=git@git.example.com:org/napkin.git\n"
        )
        explicit = tmp_path / "explicit.env"
        explicit.write_text("LMER_PUSH_ALLOW_LIST=owner/*\n")
        rc = _run_main(
            [
                "--env-only",
                "--env-file",
                str(explicit),
                "--no-task",
                "--exec",
                "true",
                REPO_URL,
            ]
        )
        assert rc == 0
        output = capsys.readouterr().out + capsys.readouterr().err
        assert "does not isolate this launch" not in output

    def test_no_warning_for_key_in_the_explicit_file(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.chdir(tmp_path)
        state = self._state_dir(monkeypatch, tmp_path)
        (state / ".env").write_text(
            "LMER_NAPKIN_REPO=git@git.example.com:org/napkin.git\n"
        )
        explicit = tmp_path / "explicit.env"
        explicit.write_text(
            "LMER_NAPKIN_REPO=git@git.example.com:personal/napkin.git\n"
        )
        rc = _run_main(
            ["--env-file", str(explicit), "--no-task", "--exec", "true", REPO_URL]
        )
        assert rc == 0
        output = capsys.readouterr().out + capsys.readouterr().err
        assert "does not isolate this launch" not in output


class TestInheritedPlatformDefaults:
    """The platform daemon's inherited ``.env`` keys (review of !429).

    ``lmer_platform.daemon`` loads ``~/.lmer/.env`` and passes the values to
    spawned sessions through ``LMER_PLATFORM_ENV_DEFAULTS``. An isolated
    launch must drop those values too, not just skip the files this process
    would read.
    """

    def _state_dir(self, monkeypatch, tmp_path):
        state = tmp_path / "state"
        state.mkdir()
        monkeypatch.setattr("lmer_cli.cli.lmer_state_dir", lambda: state)
        return state

    def test_isolated_launch_drops_inherited_defaults(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        self._state_dir(monkeypatch, tmp_path)
        explicit = tmp_path / "personal.env"
        explicit.write_text("LMER_NAPKIN_REPO=git@git.example.com:me/napkin.git\n")
        inherited = {
            "LMER_NAPKIN_REPO": "git@git.example.com:org/napkin.git",
            "LMER_TRUST_MISE": "org-value",
        }
        captured: dict = {}
        _run_main(
            [
                "--env-only",
                "--env-file",
                str(explicit),
                "--no-task",
                "--exec",
                "true",
                REPO_URL,
            ],
            env_in={
                DEFAULT_SOURCES_ENV: _carrier(**inherited),
                **inherited,
            },
            captured_env=captured,
        )
        assert captured.get("LMER_NAPKIN_REPO") == (
            "git@git.example.com:me/napkin.git"
        ), "The explicit file must supply the isolated routing value"
        assert captured.get("LMER_TRUST_MISE") is None, (
            "A daemon-inherited default .env key must not survive isolation"
        )

    def test_inherited_defaults_do_not_trigger_bleed_warning(
        self, tmp_path, monkeypatch, capsys
    ):
        """A platform assistant spawn always passes --env-file on purpose; the
        daemon-owned inherited defaults must not produce the #609 warning."""
        monkeypatch.chdir(tmp_path)
        self._state_dir(monkeypatch, tmp_path)
        explicit = tmp_path / "platform.env"
        explicit.write_text("LMER_PUSH_ALLOW_LIST=owner/*\n")
        inherited = {
            "LMER_NAPKIN_REPO": "git@git.example.com:org/napkin.git",
            "GITLAB_TOKEN_example_com": "glpat-daemon-value",
        }
        rc = _run_main(
            ["--env-file", str(explicit), "--no-task", "--exec", "true", REPO_URL],
            env_in={
                DEFAULT_SOURCES_ENV: _carrier(**inherited),
                **inherited,
            },
        )
        assert rc == 0
        output = capsys.readouterr().out + capsys.readouterr().err
        assert "does not isolate this launch" not in output, (
            "Daemon-owned inherited defaults must not warn on every platform "
            f"assistant start; got: {output!r}"
        )


class TestIsolationPresetAndCarrierEdges:
    """The two new branches called out in the !429 review."""

    def _state_dir(self, monkeypatch, tmp_path):
        state = tmp_path / "state"
        state.mkdir()
        monkeypatch.setattr("lmer_cli.cli.lmer_state_dir", lambda: state)
        return state

    def test_preset_supplied_env_only_is_ignored_with_warning(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.chdir(tmp_path)
        state = self._state_dir(monkeypatch, tmp_path)
        (state / ".env").write_text("LMER_TRUST_MISE=from-state\n")
        explicit = tmp_path / "explicit.env"
        explicit.write_text("LMER_PUSH_ALLOW_LIST=owner/*\n")
        presets = tmp_path / "presets.json"
        presets.write_text(json.dumps({"sneaky": {"args": ["--env-only"]}}))
        captured: dict = {}
        rc = _run_main(
            [
                "--preset",
                "sneaky",
                "--env-file",
                str(explicit),
                "--no-task",
                "--exec",
                "true",
                REPO_URL,
            ],
            env_in={"LMER_PRESETS_FILE": str(presets)},
            captured_env=captured,
        )
        assert rc == 0
        output = capsys.readouterr().out + capsys.readouterr().err
        assert "--env-only inside preset args is ignored" in output
        assert captured.get("LMER_TRUST_MISE") == "from-state", (
            "A preset-supplied --env-only must not isolate the launch"
        )

    def test_inherited_lmer_env_isolated_is_ignored(self, tmp_path, monkeypatch):
        """LMER_ENV_ISOLATED counts only when the operator exported it, not
        when the platform daemon loaded it from a default .env file."""
        monkeypatch.chdir(tmp_path)
        state = self._state_dir(monkeypatch, tmp_path)
        (state / ".env").write_text("LMER_TRUST_MISE=from-state\n")
        captured: dict = {}
        _run_main(
            ["--no-task", "--exec", "true", REPO_URL],
            env_in={
                DEFAULT_SOURCES_ENV: _carrier(LMER_ENV_ISOLATED="1"),
                "LMER_ENV_ISOLATED": "1",
            },
            captured_env=captured,
        )
        assert captured.get("LMER_TRUST_MISE") == "from-state", (
            "An inherited LMER_ENV_ISOLATED must not enable isolation"
        )
