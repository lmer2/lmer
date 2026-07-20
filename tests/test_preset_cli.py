"""Tests for invoking presets from the lmer CLI (issue #127).

Contract under test
-------------------
A named startup preset (from ``LMER_PRESETS_FILE``) can be selected for a
direct CLI invocation via ``--preset <name>`` or ``LMER_PRESET=<name>`` (the
flag wins, matching the ``--harness``/``LMER_HARNESS`` convention; the env var
is also honored from ``.env`` files). The explicit invocation always wins over
the preset:

- preset args (plus ``--checkout``/``--service`` derived from the preset
  fields) are prepended to argv, so explicit flags override them;
- preset ``env`` is applied only over keys that are unset or ``.env``-sourced,
  never over exported environment variables (but it does beat ``.env`` files);
- the merged argument set is re-validated, so a preset can also fail the
  normal mutual-exclusion checks.

``--list-presets`` prints the configured presets and exits 0 without needing a
task; an unknown preset name fails fast (exit 2) listing the available names.

The behavioral tests run the real ``main()`` with the container runtime mocked
out (reusing the harness from ``test_lmer_cli_slack_target``) and inspect the
env dict the CLI hands to ``env_args``.
"""

import json
import os
from unittest.mock import patch

import pytest

from tests.test_lmer_cli_slack_target import (
    _BASE_ENV,
    _make_main_mocks,
    REPO_URL,
)


@pytest.fixture
def presets_file(tmp_path):
    """A presets file with one preset per feature axis under test."""
    path = tmp_path / "presets.json"
    path.write_text(
        json.dumps(
            {
                "demo": {
                    "env": {"LMER_LLM_NAME": "opus"},
                    "args": ["--branch", "preset-branch"],
                },
                "svc": {"checkout": str(tmp_path), "service": "mysvc"},
                "other": {"args": ["--branch", "other-branch"]},
                "clashing": {"args": ["--ref", "v1.0"]},
                "sneaky": {"args": ["--env-file", str(tmp_path / "smuggled.env")]},
                "custom-env": {"env": {"LMER_MY_CUSTOM_VAR": "from-preset"}},
                "positional": {"args": ["stray-token"]},
                "dashdash": {"args": ["--", "echo", "hi"]},
                "typo": {"args": ["--portz", "2"]},
            }
        )
    )
    return path


def _run_main(argv, env_in=None, captured_env=None, home=None):
    """Run main() with the standard mock stack and a clean os.environ.

    ``home`` — when given, HOME points there so the run never picks up the
    developer's real ~/.lmer/.env (these tests are all about env precedence).
    """
    env = {**_BASE_ENV, **(env_in or {})}
    if home is not None:
        env["HOME"] = str(home)
    with patch.dict(os.environ, env, clear=True):
        with _make_main_mocks(captured_env=captured_env):
            from lmer_cli.cli import main

            return main(argv)


_EXEC_ARGS = ["--no-task", "--exec", "true", REPO_URL]


class TestPresetSelection:
    """--preset / LMER_PRESET select a preset; the flag wins."""

    def test_flag_applies_preset_env_and_args(self, presets_file, tmp_path):
        """--preset demo applies the preset's env (LMER_LLM_NAME) and args
        (--branch), both observable in the container env dict."""
        captured: dict = {}
        rc = _run_main(
            ["--preset", "demo", *_EXEC_ARGS],
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            captured_env=captured,
            home=tmp_path,
        )
        assert rc == 0, f"main() must succeed with a valid preset; rc={rc}"
        assert captured.get("LMER_LLM_NAME") == "opus", (
            f"Preset env must reach the container env; captured "
            f"LMER_LLM_NAME={captured.get('LMER_LLM_NAME')!r}"
        )
        assert captured.get("LMER_CHECKOUT_BRANCH") == "preset-branch", (
            "Preset args (--branch) must apply to the invocation"
        )

    def test_env_var_selects_preset(self, presets_file, tmp_path):
        """LMER_PRESET=demo works without the flag."""
        captured: dict = {}
        rc = _run_main(
            _EXEC_ARGS,
            env_in={
                "LMER_PRESETS_FILE": str(presets_file),
                "LMER_PRESET": "demo",
            },
            captured_env=captured,
            home=tmp_path,
        )
        assert rc == 0
        assert captured.get("LMER_CHECKOUT_BRANCH") == "preset-branch"

    def test_flag_wins_over_env_var(self, presets_file, tmp_path):
        """--preset other beats LMER_PRESET=demo (flag > env, like --harness)."""
        captured: dict = {}
        rc = _run_main(
            ["--preset", "other", *_EXEC_ARGS],
            env_in={
                "LMER_PRESETS_FILE": str(presets_file),
                "LMER_PRESET": "demo",
            },
            captured_env=captured,
            home=tmp_path,
        )
        assert rc == 0
        assert captured.get("LMER_CHECKOUT_BRANCH") == "other-branch", (
            "The preset named by --preset must apply, not the LMER_PRESET one; "
            f"got {captured.get('LMER_CHECKOUT_BRANCH')!r}"
        )
        assert captured.get("LMER_LLM_NAME") is None, (
            "The demo preset (selected only via env var) must NOT apply when "
            "--preset names another preset"
        )

    def test_env_var_honored_from_cwd_dotenv(
        self, presets_file, tmp_path, monkeypatch
    ):
        """LMER_PRESET in the working directory's .env selects the preset,
        enabling per-directory default presets."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text(
            f"LMER_PRESET=demo\nLMER_PRESETS_FILE={presets_file}\n"
        )
        captured: dict = {}
        rc = _run_main(_EXEC_ARGS, captured_env=captured, home=tmp_path)
        assert rc == 0
        assert captured.get("LMER_CHECKOUT_BRANCH") == "preset-branch", (
            "A .env-sourced LMER_PRESET (and LMER_PRESETS_FILE) must be honored"
        )

    def test_no_preset_selected_changes_nothing(self, presets_file, tmp_path):
        """With presets configured but none selected, the invocation is
        untouched."""
        captured: dict = {}
        rc = _run_main(
            _EXEC_ARGS,
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            captured_env=captured,
            home=tmp_path,
        )
        assert rc == 0
        assert captured.get("LMER_LLM_NAME") is None
        assert captured.get("LMER_CHECKOUT_BRANCH") is None


class TestPresetPrecedence:
    """The explicit invocation wins over the preset."""

    def test_explicit_flag_beats_preset_arg(self, presets_file, tmp_path):
        """--branch on the command line overrides the preset's --branch."""
        captured: dict = {}
        rc = _run_main(
            ["--preset", "demo", "--branch", "my-branch", *_EXEC_ARGS],
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            captured_env=captured,
            home=tmp_path,
        )
        assert rc == 0
        assert captured.get("LMER_CHECKOUT_BRANCH") == "my-branch", (
            "An explicit flag must win over the same flag from preset args; "
            f"got {captured.get('LMER_CHECKOUT_BRANCH')!r}"
        )

    def test_exported_env_beats_preset_env(self, presets_file, tmp_path):
        """An exported LMER_LLM_NAME wins over the preset's env entry."""
        captured: dict = {}
        rc = _run_main(
            ["--preset", "demo", *_EXEC_ARGS],
            env_in={
                "LMER_PRESETS_FILE": str(presets_file),
                "LMER_LLM_NAME": "sonnet",
            },
            captured_env=captured,
            home=tmp_path,
        )
        assert rc == 0
        assert captured.get("LMER_LLM_NAME") == "sonnet", (
            "Exported environment must win over preset env; "
            f"got {captured.get('LMER_LLM_NAME')!r}"
        )

    def test_preset_env_beats_dotenv_value(
        self, presets_file, tmp_path, monkeypatch
    ):
        """A preset env entry overrides the same key sourced from a .env file
        (preset > .env, but < exported env)."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("LMER_LLM_NAME=from-dotenv\n")
        captured: dict = {}
        rc = _run_main(
            ["--preset", "demo", *_EXEC_ARGS],
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            captured_env=captured,
            home=tmp_path,
        )
        assert rc == 0
        assert captured.get("LMER_LLM_NAME") == "opus", (
            "Preset env must win over a .env-file value; "
            f"got {captured.get('LMER_LLM_NAME')!r}"
        )

    def test_preset_args_are_revalidated(self, presets_file, tmp_path, capsys):
        """A preset arg that collides with an explicit flag fails the normal
        validation (--branch + preset --ref)."""
        rc = _run_main(
            ["--preset", "clashing", "--branch", "b", *_EXEC_ARGS],
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            home=tmp_path,
        )
        assert rc == 2, f"Colliding preset args must fail validation; rc={rc}"
        out = capsys.readouterr()
        assert "--branch and --ref" in (out.out + out.err)

    def test_env_file_in_preset_args_is_ignored(
        self, presets_file, tmp_path, capsys
    ):
        """--env-file smuggled in via preset args is rejected with a warning
        (its precedence would be unresolvable after the early .env load)."""
        (tmp_path / "smuggled.env").write_text("LMER_LLM_NAME=smuggled\n")
        captured: dict = {}
        rc = _run_main(
            ["--preset", "sneaky", *_EXEC_ARGS],
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            captured_env=captured,
            home=tmp_path,
        )
        assert rc == 0
        assert captured.get("LMER_LLM_NAME") is None, (
            "Variables from a preset-args --env-file must NOT load"
        )
        out = capsys.readouterr()
        assert "--env-file inside preset args" in (out.out + out.err)


class TestPresetCheckoutService:
    """The preset's checkout/service fields become --checkout/--service."""

    def test_preset_checkout_is_applied(self, tmp_path):
        """A checkout-only preset mounts the preset's path as the workspace."""
        presets = tmp_path / "presets.json"
        presets.write_text(json.dumps({"co": {"checkout": str(tmp_path)}}))
        captured_paths: list = []

        def _capture_checkout(runtime, path):
            captured_paths.append(path)
            return []

        with patch.dict(
            os.environ,
            {**_BASE_ENV, "HOME": str(tmp_path), "LMER_PRESETS_FILE": str(presets)},
            clear=True,
        ):
            with _make_main_mocks():
                with patch(
                    "lmer_cli.cli.build_checkout_mount",
                    side_effect=_capture_checkout,
                ):
                    from lmer_cli.cli import main

                    rc = main(["--preset", "co", *_EXEC_ARGS])
        assert rc == 0
        assert captured_paths == [tmp_path.resolve()], (
            f"Preset checkout must be mounted; got {captured_paths!r}"
        )

    def test_explicit_checkout_beats_preset(self, tmp_path):
        """--checkout on the command line overrides the preset's checkout."""
        preset_dir = tmp_path / "preset-co"
        preset_dir.mkdir()
        explicit_dir = tmp_path / "explicit-co"
        explicit_dir.mkdir()
        presets = tmp_path / "presets.json"
        presets.write_text(json.dumps({"co": {"checkout": str(preset_dir)}}))
        captured_paths: list = []

        def _capture_checkout(runtime, path):
            captured_paths.append(path)
            return []

        with patch.dict(
            os.environ,
            {**_BASE_ENV, "HOME": str(tmp_path), "LMER_PRESETS_FILE": str(presets)},
            clear=True,
        ):
            with _make_main_mocks():
                with patch(
                    "lmer_cli.cli.build_checkout_mount",
                    side_effect=_capture_checkout,
                ):
                    from lmer_cli.cli import main

                    rc = main(
                        [
                            "--preset",
                            "co",
                            "--checkout",
                            str(explicit_dir),
                            *_EXEC_ARGS,
                        ]
                    )
        assert rc == 0
        assert captured_paths == [explicit_dir.resolve()], (
            f"An explicit --checkout must beat the preset's; got {captured_paths!r}"
        )

    def test_preset_service_enters_service_mode(self, presets_file, tmp_path):
        """A service preset resolves the named container and exports
        LMER_SERVICE_NAME into the container env."""
        captured: dict = {}
        with patch.dict(
            os.environ,
            {
                **_BASE_ENV,
                "HOME": str(tmp_path),
                "LMER_PRESETS_FILE": str(presets_file),
            },
            clear=True,
        ):
            with _make_main_mocks(captured_env=captured):
                with (
                    patch(
                        "lmer_cli.cli.resolve_container", return_value="cid123"
                    ) as resolve_mock,
                    patch(
                        "lmer_cli.cli.inspect_container_workdir",
                        return_value="/srv/app",
                    ),
                    patch(
                        "lmer_cli.cli.build_service_mode_mounts", return_value=[]
                    ),
                ):
                    from lmer_cli.cli import main

                    rc = main(["--preset", "svc", *_EXEC_ARGS])
        assert rc == 0
        assert resolve_mock.call_args[0][1] == "mysvc", (
            "The preset's service name must be resolved"
        )
        assert captured.get("LMER_SERVICE_NAME") == "mysvc", (
            "Service mode from a preset must export LMER_SERVICE_NAME"
        )


class TestPresetContainerEnvForwarding:
    """Applied preset env reaches the container even without a hardcoded
    passthrough, and keeps its precedence over .env files there."""

    def test_non_passthrough_preset_env_reaches_container(
        self, presets_file, tmp_path
    ):
        captured: dict = {}
        rc = _run_main(
            ["--preset", "custom-env", *_EXEC_ARGS],
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            captured_env=captured,
            home=tmp_path,
        )
        assert rc == 0
        assert captured.get("LMER_MY_CUSTOM_VAR") == "from-preset", (
            "A preset env key with no hardcoded container passthrough must "
            f"still be forwarded; captured: {captured.get('LMER_MY_CUSTOM_VAR')!r}"
        )

    def test_preset_env_beats_dotenv_in_container(
        self, presets_file, tmp_path, monkeypatch
    ):
        """The container-side .env merge must not override an applied preset
        entry (the key here has no hardcoded passthrough, so only the
        seeding keeps the documented precedence)."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("LMER_MY_CUSTOM_VAR=from-dotenv\n")
        captured: dict = {}
        rc = _run_main(
            ["--preset", "custom-env", *_EXEC_ARGS],
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            captured_env=captured,
            home=tmp_path,
        )
        assert rc == 0
        assert captured.get("LMER_MY_CUSTOM_VAR") == "from-preset", (
            "Preset env must beat a .env value in the container env too; "
            f"got {captured.get('LMER_MY_CUSTOM_VAR')!r}"
        )


class TestPresetArgsGuards:
    """Preset args must be flag tokens on a CLI invocation."""

    def test_positional_in_preset_args_fails(self, presets_file, tmp_path, capsys):
        rc = _run_main(
            ["--preset", "positional", *_EXEC_ARGS],
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            home=tmp_path,
        )
        assert rc == 2, f"Positional preset args must fail fast; rc={rc}"
        text = "".join(capsys.readouterr())
        assert "stray-token" in text and "positional" in text

    def test_dashdash_in_preset_args_fails(self, presets_file, tmp_path, capsys):
        rc = _run_main(
            ["--preset", "dashdash", *_EXEC_ARGS],
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            home=tmp_path,
        )
        assert rc == 2, f"'--' in preset args must fail fast; rc={rc}"
        assert "'--'" in "".join(capsys.readouterr())

    def test_unknown_flag_in_preset_args_fails(
        self, presets_file, tmp_path, capsys
    ):
        """A typo'd/unknown flag in preset args fails fast instead of being
        silently dropped (or prepended to the exec command under --exec)."""
        rc = _run_main(
            ["--preset", "typo", *_EXEC_ARGS],
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            home=tmp_path,
        )
        assert rc == 2, f"Unknown flags in preset args must fail fast; rc={rc}"
        text = "".join(capsys.readouterr())
        assert "--portz" in text and "does not" in text


class TestContainerUserFromDotenv:
    """LMER_CONTAINER_USER resolution (regression for the #127 reordering:
    the container-user decision now runs after the early .env load, so a
    .env-sourced value is honored; exported env and --user still win)."""

    def _run_capturing_user(self, argv, env_in=None, home=None):
        captured_users: list = []

        def _capture_base_run_args(runtime, exec_mode, container_user):
            captured_users.append(container_user)
            return []

        env = {**_BASE_ENV, **(env_in or {})}
        if home is not None:
            env["HOME"] = str(home)
        with patch.dict(os.environ, env, clear=True):
            with _make_main_mocks():
                with patch(
                    "lmer_cli.cli.base_run_args",
                    side_effect=_capture_base_run_args,
                ):
                    from lmer_cli.cli import main

                    rc = main(argv)
        return rc, captured_users

    def test_dotenv_container_user_is_honored(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("LMER_CONTAINER_USER=dotenv-user\n")
        rc, users = self._run_capturing_user(_EXEC_ARGS, home=tmp_path)
        assert rc == 0
        assert users == ["dotenv-user"], (
            f"LMER_CONTAINER_USER from cwd/.env must be honored; got {users!r}"
        )

    def test_exported_container_user_beats_dotenv(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("LMER_CONTAINER_USER=dotenv-user\n")
        rc, users = self._run_capturing_user(
            _EXEC_ARGS,
            env_in={"LMER_CONTAINER_USER": "exported-user"},
            home=tmp_path,
        )
        assert rc == 0
        assert users == ["exported-user"], (
            f"Exported LMER_CONTAINER_USER must beat the .env value; got {users!r}"
        )

    def test_user_flag_beats_env(self, tmp_path):
        rc, users = self._run_capturing_user(
            ["--user", "0:0", *_EXEC_ARGS],
            env_in={"LMER_CONTAINER_USER": "exported-user"},
            home=tmp_path,
        )
        assert rc == 0
        assert users == ["0:0"], (
            f"--user must beat LMER_CONTAINER_USER; got {users!r}"
        )

    def test_default_container_user(self, tmp_path):
        rc, users = self._run_capturing_user(_EXEC_ARGS, home=tmp_path)
        assert rc == 0
        assert users == ["developer"], (
            f"Default container user must be 'developer'; got {users!r}"
        )


class TestPresetErrors:
    """Unknown / unconfigured preset names fail fast."""

    def test_unknown_preset_lists_available(self, presets_file, tmp_path, capsys):
        rc = _run_main(
            ["--preset", "nope", *_EXEC_ARGS],
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            home=tmp_path,
        )
        assert rc == 2, f"Unknown preset must exit 2; rc={rc}"
        combined = capsys.readouterr()
        text = combined.out + combined.err
        assert "nope" in text
        assert "demo" in text and "svc" in text, (
            f"The error must list the available preset names; got: {text!r}"
        )

    def test_preset_without_presets_file_fails(self, tmp_path, capsys):
        """Selecting a preset with LMER_PRESETS_FILE unset points at the
        missing configuration."""
        rc = _run_main(["--preset", "demo", *_EXEC_ARGS], home=tmp_path)
        assert rc == 2
        text = "".join(capsys.readouterr())
        assert "LMER_PRESETS_FILE" in text


class TestListPresets:
    """--list-presets prints the configured presets and exits 0."""

    def test_lists_presets_without_task(self, presets_file, tmp_path, capsys):
        rc = _run_main(
            ["--list-presets"],
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            home=tmp_path,
        )
        assert rc == 0, f"--list-presets must exit 0; rc={rc}"
        out = capsys.readouterr().out
        for name in ("demo", "svc", "clashing", "sneaky"):
            assert name in out, f"Preset {name!r} missing from listing: {out!r}"
        assert "service=mysvc" in out
        # Env is listed by key only — a preset env value may carry credentials.
        assert "LMER_LLM_NAME" in out
        assert "opus" not in out, "Preset env VALUES must not be printed"

    def test_lists_nothing_when_unconfigured(self, tmp_path, capsys):
        rc = _run_main(["--list-presets"], home=tmp_path)
        assert rc == 0
        assert "No presets configured" in capsys.readouterr().out

    def test_show_env_labels_preset_sourced_vars(
        self, presets_file, tmp_path, capsys
    ):
        """--show-env attributes preset-applied vars to the preset."""
        rc = _run_main(
            ["--show-env", "--preset", "demo"],
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            home=tmp_path,
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "preset (demo)" in out, (
            f"--show-env must label preset-sourced vars; got: {out!r}"
        )
