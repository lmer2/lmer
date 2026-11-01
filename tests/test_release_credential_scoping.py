"""Negative-guarantee tests for release credential scoping (release-flow §4/§5).

Contract under test
--------------------
The production release credentials — the fine-grained GitHub PAT
(``LMER_RELEASE_GITHUB_TOKEN``, env passthrough) and the release SSH signing
key (``LMER_RELEASE_SIGNING_KEY``, host path remapped through a read-only
bind mount) — reach release-taskdef sessions ONLY, keyed on the resolved
task id (``RELEASE_TASK_ID``). A session for ANY other taskdef receives
neither: both keys are seeded ``None`` in the container env dict and no
signing-key mount is assembled.

Every distinct leak path in cli.py gets its own test, because each is a
separate code path: the credential exported in the host environment, present
only in a cwd ``.env``, only in ``~/.lmer/.env``, only in an explicit
``--env-file``, and supplied by a selected preset's ``env`` (the preset
seeding loop). Each test asserts on BOTH halves of the surface — the
container env dict handed to ``build_container_env`` AND the assembled ``-v`` mount
arguments — so a refactor cannot satisfy the suite by moving the credential
from one channel to the other.

The behavioral tests run the real ``main()`` with the container runtime
mocked out (reusing the harness from ``test_lmer_cli_slack_target``) and
inspect the env dict the CLI hands to ``build_container_env`` plus the full run command
handed to ``subprocess.call``.
"""

import json
import os
from unittest.mock import patch

import pytest

from lmer_cli.cli import (
    RELEASE_GITHUB_TOKEN_ENV,
    RELEASE_SIGNING_KEY_ENV,
    RELEASE_TASK_ID,
)
from lmer_cli.mounts import CONTAINER_RELEASE_SIGNING_KEY_PATH
from lmer_cli.runtime import _is_selinux_enforcing
from tests.test_lmer_cli_slack_target import (
    _BASE_ENV,
    _make_main_mocks,
    REPO_URL,
)

# Dummy secrets only — distinct per test so a leak names its channel.
_PAT_VALUE = "github_pat_dummy_scoping_fixture"


def _run_main(argv, env_in=None, captured_env=None, captured_cmd=None, home=None):
    """Run main() with the standard mock stack and a clean os.environ.

    ``home`` — HOME points there so the run never picks up the developer's
    real ``~/.lmer/.env`` and so a signing key placed under it passes the
    mount guard's under-home check (a rejected key would make the negative
    assertions vacuous).

    ``captured_cmd`` — if provided (a list), receives the full run command
    main() hands to ``subprocess.call`` so tests can inspect the assembled
    ``-v`` mount arguments.
    """
    env = {**_BASE_ENV, **(env_in or {})}
    if home is not None:
        env["HOME"] = str(home)
    with patch.dict(os.environ, env, clear=True):
        with _make_main_mocks(captured_env=captured_env) as stack:
            # Deterministic mount strings: no SELinux suffix regardless of host.
            stack.enter_context(
                patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False)
            )
            _is_selinux_enforcing.cache_clear()
            # runtime._LMER_STATE_DIR is frozen at import from the REAL home,
            # so redirect the state dir to the isolated HOME explicitly —
            # this is what makes the ~/.lmer/.env leak-path test drive the
            # real state-dir code path (and keeps every other test away from
            # the developer's actual ~/.lmer/.env).
            if home is not None:
                stack.enter_context(
                    patch("lmer_cli.cli.lmer_state_dir", return_value=home / ".lmer")
                )
            # Not mocked by the shared harness; its harnesses-dir bind would
            # add an unrelated -v and break the "no -v at all" assertions.
            stack.enter_context(
                patch(
                    "lmer_cli.cli.build_user_harness_mounts",
                    return_value=([], False),
                )
            )
            if captured_cmd is not None:

                def _capture_call(cmd, *args, **kwargs):
                    captured_cmd[:] = list(cmd)
                    return 0

                stack.enter_context(
                    patch("lmer_cli.cli.subprocess.call", side_effect=_capture_call)
                )
            from lmer_cli.cli import main

            return main(argv)


@pytest.fixture
def fake_home(tmp_path):
    """An isolated HOME with a real (dummy) signing key under it.

    The key file genuinely exists and passes every mount-guard check, so
    when a negative test sees no mount the ONLY possible reason is the
    task-id gate — not an incidental guard rejection.
    """
    home = tmp_path / "home"
    home.mkdir()
    key = home / ".ssh" / "lmer_release_key"
    key.parent.mkdir()
    key.write_text("dummy private key material\n")
    return home


def _key_path(home):
    return str(home / ".ssh" / "lmer_release_key")


def _assert_no_production_credentials(captured_env, captured_cmd, home):
    """Both halves of the negative guarantee, in one place.

    Env half: both production keys are PRESENT and None (the None seed is
    the leak blocker — the .env merge and preset seeding loop both skip keys
    already in the dict), and NO env value carries a secret.
    Mount half: the run command contains no ``-v`` argument at all (every
    other mount builder is mocked to []), so in particular no signing-key
    bind and no token smuggled into mount syntax.
    """
    key_path = _key_path(home)
    assert RELEASE_GITHUB_TOKEN_ENV in captured_env, (
        "The PAT key must be seeded (None) in the env dict — a missing key "
        "reopens the .env-merge and preset leak paths"
    )
    assert RELEASE_SIGNING_KEY_ENV in captured_env, (
        "The signing-key key must be seeded (None) in the env dict"
    )
    assert captured_env[RELEASE_GITHUB_TOKEN_ENV] is None, (
        f"PAT leaked into a non-release session: "
        f"{captured_env[RELEASE_GITHUB_TOKEN_ENV]!r}"
    )
    assert captured_env[RELEASE_SIGNING_KEY_ENV] is None, (
        f"Signing-key var leaked into a non-release session: "
        f"{captured_env[RELEASE_SIGNING_KEY_ENV]!r}"
    )
    # No other env key may carry the secrets either. There are no
    # exemptions: the rig-scoped rehearsal trio that used to be excused here
    # went with the rehearsal rig.
    for k, v in captured_env.items():
        assert v != _PAT_VALUE, f"PAT value found under unexpected env key {k}"
        assert v != key_path, f"Signing-key path found under unexpected env key {k}"
    # Mount half: nothing was bind-mounted at all.
    assert "-v" not in captured_cmd, (
        f"No -v mount may be assembled for a non-release session; "
        f"cmd: {captured_cmd}"
    )
    joined = "\x00".join(captured_cmd)
    assert CONTAINER_RELEASE_SIGNING_KEY_PATH not in joined, (
        "The container signing-key path must not appear anywhere in the run command"
    )
    assert _PAT_VALUE not in joined, (
        "The PAT value must not appear anywhere in the run command"
    )


class TestNonReleaseSessionLeakPaths:
    """A non-release session receives NEITHER production credential,
    regardless of which host-side channel carries it — one test per
    distinct code path in cli.py."""

    def test_host_environment_no_task(self, fake_home):
        """Leak path 1a: both credentials exported in the host environment;
        a --no-task session (resolved task id None) gets neither."""
        captured: dict = {}
        cmd: list = []
        rc = _run_main(
            ["--no-task", "--exec", "true", REPO_URL],
            env_in={
                RELEASE_GITHUB_TOKEN_ENV: _PAT_VALUE,
                RELEASE_SIGNING_KEY_ENV: _key_path(fake_home),
            },
            captured_env=captured,
            captured_cmd=cmd,
            home=fake_home,
        )
        assert rc == 0
        _assert_no_production_credentials(captured, cmd, fake_home)

    def test_host_environment_named_non_release_task(self, fake_home):
        """Leak path 1b: same host-environment channel, but with a NAMED
        taskdef other than release — the gate keys on the resolved task id,
        not on no-task mode."""
        captured: dict = {}
        cmd: list = []
        rc = _run_main(
            ["review", REPO_URL],
            env_in={
                RELEASE_GITHUB_TOKEN_ENV: _PAT_VALUE,
                RELEASE_SIGNING_KEY_ENV: _key_path(fake_home),
            },
            captured_env=captured,
            captured_cmd=cmd,
            home=fake_home,
        )
        assert rc == 0
        _assert_no_production_credentials(captured, cmd, fake_home)

    def test_cwd_dotenv(self, fake_home, tmp_path, monkeypatch):
        """Leak path 2: credentials present ONLY in the working-directory
        .env (loaded both by the early os.environ export and by the
        container-env merge — the None seeds must block both)."""
        workdir = tmp_path / "cwd"
        workdir.mkdir()
        monkeypatch.chdir(workdir)
        (workdir / ".env").write_text(
            f"{RELEASE_GITHUB_TOKEN_ENV}={_PAT_VALUE}\n"
            f"{RELEASE_SIGNING_KEY_ENV}={_key_path(fake_home)}\n"
        )
        captured: dict = {}
        cmd: list = []
        rc = _run_main(
            ["--no-task", "--exec", "true", REPO_URL],
            captured_env=captured,
            captured_cmd=cmd,
            home=fake_home,
        )
        assert rc == 0
        _assert_no_production_credentials(captured, cmd, fake_home)

    def test_state_dir_dotenv(self, fake_home):
        """Leak path 3: credentials present ONLY in ~/.lmer/.env (the lmer
        state dir under the isolated HOME)."""
        state_dir = fake_home / ".lmer"
        state_dir.mkdir()
        (state_dir / ".env").write_text(
            f"{RELEASE_GITHUB_TOKEN_ENV}={_PAT_VALUE}\n"
            f"{RELEASE_SIGNING_KEY_ENV}={_key_path(fake_home)}\n"
        )
        captured: dict = {}
        cmd: list = []
        rc = _run_main(
            ["--no-task", "--exec", "true", REPO_URL],
            captured_env=captured,
            captured_cmd=cmd,
            home=fake_home,
        )
        assert rc == 0
        _assert_no_production_credentials(captured, cmd, fake_home)

    def test_explicit_env_file(self, fake_home, tmp_path):
        """Leak path 4: credentials present ONLY in an explicit --env-file
        (the highest-precedence .env source)."""
        env_file = tmp_path / "deploy.env"
        env_file.write_text(
            f"{RELEASE_GITHUB_TOKEN_ENV}={_PAT_VALUE}\n"
            f"{RELEASE_SIGNING_KEY_ENV}={_key_path(fake_home)}\n"
        )
        captured: dict = {}
        cmd: list = []
        rc = _run_main(
            ["--env-file", str(env_file), "--no-task", "--exec", "true", REPO_URL],
            captured_env=captured,
            captured_cmd=cmd,
            home=fake_home,
        )
        assert rc == 0
        _assert_no_production_credentials(captured, cmd, fake_home)

    def test_preset_env(self, fake_home, tmp_path):
        """Leak path 5: credentials supplied by a selected preset's env —
        the preset seeding loop in main() must skip both keys because the
        gate already seeded them None."""
        presets_file = tmp_path / "presets.json"
        presets_file.write_text(
            json.dumps(
                {
                    "leaky": {
                        "env": {
                            RELEASE_GITHUB_TOKEN_ENV: _PAT_VALUE,
                            RELEASE_SIGNING_KEY_ENV: _key_path(fake_home),
                        }
                    }
                }
            )
        )
        captured: dict = {}
        cmd: list = []
        rc = _run_main(
            ["--preset", "leaky", "--no-task", "--exec", "true", REPO_URL],
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            captured_env=captured,
            captured_cmd=cmd,
            home=fake_home,
        )
        assert rc == 0, f"main() must succeed with the leaky preset; rc={rc}"
        _assert_no_production_credentials(captured, cmd, fake_home)


class TestReleaseSessionProvisioning:
    """The positive half: a release-taskdef session receives exactly both —
    the PAT in the env dict and the signing key as a read-only bind mount
    with the REMAPPED container path in the env."""

    def test_release_session_receives_pat_and_key_mount(self, fake_home):
        key_path = _key_path(fake_home)
        captured: dict = {}
        cmd: list = []
        rc = _run_main(
            [RELEASE_TASK_ID, REPO_URL],
            env_in={
                RELEASE_GITHUB_TOKEN_ENV: _PAT_VALUE,
                RELEASE_SIGNING_KEY_ENV: key_path,
            },
            captured_env=captured,
            captured_cmd=cmd,
            home=fake_home,
        )
        assert rc == 0, f"release session must launch; rc={rc}"
        # Env half: PAT verbatim, signing key remapped to the container path.
        assert captured.get(RELEASE_GITHUB_TOKEN_ENV) == _PAT_VALUE
        assert (
            captured.get(RELEASE_SIGNING_KEY_ENV)
            == CONTAINER_RELEASE_SIGNING_KEY_PATH
        ), (
            "The container env must carry the REMAPPED key path, never the "
            f"host path; got {captured.get(RELEASE_SIGNING_KEY_ENV)!r}"
        )
        # The host key location must not leak into the container env.
        assert key_path not in captured.values()
        # Mount half: exactly one -v, and it is the signing-key bind (every
        # other mount builder is mocked to [] in this harness).
        mount_token = f"{key_path}:{CONTAINER_RELEASE_SIGNING_KEY_PATH}:ro"
        assert cmd.count("-v") == 1, (
            f"Expected exactly the signing-key mount; cmd: {cmd}"
        )
        idx = cmd.index(mount_token)
        assert cmd[idx - 1] == "-v", f"Mount spec must follow -v; cmd: {cmd}"

    def test_release_session_without_configured_credentials_gets_none(
        self, fake_home
    ):
        """Unconfigured credentials are not fatal for a release session
        (leg-1 work needs neither) — the seeds stay None and no mount is
        assembled."""
        captured: dict = {}
        cmd: list = []
        rc = _run_main(
            [RELEASE_TASK_ID, REPO_URL],
            captured_env=captured,
            captured_cmd=cmd,
            home=fake_home,
        )
        assert rc == 0
        assert captured.get(RELEASE_GITHUB_TOKEN_ENV) is None
        assert captured.get(RELEASE_SIGNING_KEY_ENV) is None
        assert "-v" not in cmd


class TestReleaseCredentialsReloadedFromEnvFile:
    """A release credential the daemon baked from ``.env`` at startup must be
    re-read from the file at spawn (#467).

    ``lmer platform`` seeds its own environment from ``~/.lmer/.env`` once at
    startup and records which keys that supplied; a child inherits the snapshot
    and its first-wins ``.env`` seeding then keeps the launch-time value, so a
    rotated token had no effect until the daemon restarted. The child must drop
    the file-supplied defaults before its own early ``.env`` load so the file as
    it is now decides.
    """

    @staticmethod
    def _carrier(stale: dict) -> dict:
        """The provenance carrier the daemon's loader would have written."""
        from lmer_cli.env_sources import DEFAULT_SOURCES_ENV, remember_env_defaults

        with patch.dict(os.environ, stale, clear=True):
            remember_env_defaults(
                {name: ".env (lmer state dir)" for name in stale}
            )
            return {DEFAULT_SOURCES_ENV: os.environ[DEFAULT_SOURCES_ENV]}

    def test_release_session_uses_the_current_env_file(self, fake_home):
        state_dir = fake_home / ".lmer"
        state_dir.mkdir()
        fresh_pat = "github_pat_rotated_in_env_file"
        fresh_key = _key_path(fake_home)
        (state_dir / ".env").write_text(
            f"{RELEASE_GITHUB_TOKEN_ENV}={fresh_pat}\n"
            f"{RELEASE_SIGNING_KEY_ENV}={fresh_key}\n"
        )
        stale = {
            RELEASE_GITHUB_TOKEN_ENV: "github_pat_stale_daemon_snapshot",
            RELEASE_SIGNING_KEY_ENV: "/host/stale-signing-key",
        }
        captured: dict = {}
        cmd: list = []
        rc = _run_main(
            [RELEASE_TASK_ID, REPO_URL],
            env_in={**stale, **self._carrier(stale)},
            captured_env=captured,
            captured_cmd=cmd,
            home=fake_home,
        )
        assert rc == 0, f"release session must launch; rc={rc}"
        assert captured.get(RELEASE_GITHUB_TOKEN_ENV) == fresh_pat, (
            "the file as it is now must decide, not the daemon's snapshot"
        )
        assert (
            captured.get(RELEASE_SIGNING_KEY_ENV)
            == CONTAINER_RELEASE_SIGNING_KEY_PATH
        )
        assert f"{fresh_key}:{CONTAINER_RELEASE_SIGNING_KEY_PATH}:ro" in cmd, (
            "the mount must name the current file's key, not the stale snapshot"
        )

    def test_a_genuinely_exported_release_credential_still_wins(self, fake_home):
        """The fix is scoped to file-supplied defaults: an export was never
        recorded, so it keeps its documented precedence over the file."""
        state_dir = fake_home / ".lmer"
        state_dir.mkdir()
        (state_dir / ".env").write_text(
            f"{RELEASE_GITHUB_TOKEN_ENV}=github_pat_from_file\n"
        )
        exported = "github_pat_exported_in_daemon_shell"
        captured: dict = {}
        cmd: list = []
        rc = _run_main(
            [RELEASE_TASK_ID, REPO_URL],
            env_in={RELEASE_GITHUB_TOKEN_ENV: exported},
            captured_env=captured,
            captured_cmd=cmd,
            home=fake_home,
        )
        assert rc == 0
        assert captured.get(RELEASE_GITHUB_TOKEN_ENV) == exported

    def test_a_key_deleted_from_the_env_file_stops_being_forwarded(self, fake_home):
        """Removal is the other half of "the file as it is now decides": a key
        the daemon baked from ``.env`` and the operator has since deleted from
        the file must not keep reaching release sessions until a restart."""
        state_dir = fake_home / ".lmer"
        state_dir.mkdir()
        (state_dir / ".env").write_text("LMER_UNRELATED=1\n")
        # A stale key that still passes the mount guard, so a mount in the
        # command could only come from the daemon's snapshot.
        stale = {
            RELEASE_GITHUB_TOKEN_ENV: "github_pat_deleted_from_env_file",
            RELEASE_SIGNING_KEY_ENV: _key_path(fake_home),
        }
        captured: dict = {}
        cmd: list = []
        rc = _run_main(
            [RELEASE_TASK_ID, REPO_URL],
            env_in={**stale, **self._carrier(stale)},
            captured_env=captured,
            captured_cmd=cmd,
            home=fake_home,
        )
        assert rc == 0, f"release session must launch; rc={rc}"
        assert captured.get(RELEASE_GITHUB_TOKEN_ENV) is None, (
            "a PAT deleted from the file must stop being forwarded"
        )
        assert captured.get(RELEASE_SIGNING_KEY_ENV) is None
        assert not any(CONTAINER_RELEASE_SIGNING_KEY_PATH in arg for arg in cmd), (
            "a signing key deleted from the file must not be mounted"
        )


class TestReleaseCredentialDisagreementWarning:
    """An exported release credential that disagrees with ``.env`` is announced
    (#467, option 3; operator decision on the review of the fix).

    An export keeps its precedence over the file, so a daemon started from a
    shell that exports the PAT still hands every release session that value
    after the operator rotates it in ``~/.lmer/.env``. That precedence is
    documented, but silent: the launch must say which variable disagrees, with
    which file, and that the export won — without printing either value.
    """

    _MARKER = "the exported value wins"

    def _launch(self, fake_home, capsys, *, exported, env_file, task=RELEASE_TASK_ID):
        state_dir = fake_home / ".lmer"
        state_dir.mkdir()
        (state_dir / ".env").write_text(
            "".join(f"{name}={value}\n" for name, value in env_file.items())
        )
        captured: dict = {}
        cmd: list = []
        rc = _run_main(
            [task, REPO_URL],
            env_in=exported,
            captured_env=captured,
            captured_cmd=cmd,
            home=fake_home,
        )
        return rc, captured, capsys.readouterr().out, state_dir / ".env"

    def test_an_exported_pat_that_disagrees_with_the_env_file_warns(
        self, fake_home, capsys
    ):
        exported = "github_pat_exported_in_daemon_shell"
        in_file = "github_pat_rotated_in_env_file"
        rc, captured, out, env_file = self._launch(
            fake_home,
            capsys,
            exported={RELEASE_GITHUB_TOKEN_ENV: exported},
            env_file={RELEASE_GITHUB_TOKEN_ENV: in_file},
        )
        assert rc == 0
        assert captured.get(RELEASE_GITHUB_TOKEN_ENV) == exported, (
            "the export keeps its precedence; the warning changes nothing else"
        )
        warnings = [line for line in out.splitlines() if self._MARKER in line]
        assert len(warnings) == 1, out
        assert RELEASE_GITHUB_TOKEN_ENV in warnings[0]
        assert str(env_file) in warnings[0], "the warning must name the file"
        assert exported not in out and in_file not in out, (
            "the warning must never print a credential value"
        )

    def test_an_exported_signing_key_that_disagrees_with_the_env_file_warns(
        self, fake_home, capsys
    ):
        exported_key = _key_path(fake_home)
        rc, captured, out, env_file = self._launch(
            fake_home,
            capsys,
            exported={RELEASE_SIGNING_KEY_ENV: exported_key},
            env_file={RELEASE_SIGNING_KEY_ENV: str(fake_home / "rotated_key")},
        )
        assert rc == 0
        assert captured.get(RELEASE_SIGNING_KEY_ENV) == CONTAINER_RELEASE_SIGNING_KEY_PATH
        warnings = [line for line in out.splitlines() if self._MARKER in line]
        assert len(warnings) == 1, out
        assert RELEASE_SIGNING_KEY_ENV in warnings[0]
        assert str(env_file) in warnings[0]

    def test_the_warning_precedes_the_abort_on_an_exported_deleted_key(
        self, fake_home, capsys
    ):
        """The usual rotation deletes the old key: an export still naming it
        aborts the launch, and the abort alone does not say the path came from
        an export while ``.env`` names the new key — the warning must be
        printed before the launch gives up."""
        deleted_key = fake_home / ".ssh" / "old_key_deleted"
        rc, captured, out, env_file = self._launch(
            fake_home,
            capsys,
            exported={RELEASE_SIGNING_KEY_ENV: str(deleted_key)},
            env_file={RELEASE_SIGNING_KEY_ENV: _key_path(fake_home)},
        )
        assert rc == 1, "the export still wins, and its key is missing"
        assert captured == {}, "the aborted launch must not start a container"
        warnings = [line for line in out.splitlines() if self._MARKER in line]
        assert len(warnings) == 1, out
        assert RELEASE_SIGNING_KEY_ENV in warnings[0]
        assert str(env_file) in warnings[0]
        assert _key_path(fake_home) not in warnings[0], (
            "the warning names the file, never the value it holds"
        )

    def test_no_warning_when_the_export_and_the_file_agree(self, fake_home, capsys):
        same = "github_pat_same_in_both"
        rc, _, out, _ = self._launch(
            fake_home,
            capsys,
            exported={RELEASE_GITHUB_TOKEN_ENV: same},
            env_file={RELEASE_GITHUB_TOKEN_ENV: same},
        )
        assert rc == 0
        assert self._MARKER not in out

    def test_no_warning_for_a_daemon_file_default(self, fake_home, capsys):
        """A value the daemon took from the file is re-read, not exported: the
        rotation case #467 fixed must not be reported as a disagreement."""
        stale = {RELEASE_GITHUB_TOKEN_ENV: "github_pat_stale_daemon_snapshot"}
        rc, captured, out, _ = self._launch(
            fake_home,
            capsys,
            exported={
                **stale,
                **TestReleaseCredentialsReloadedFromEnvFile._carrier(stale),
            },
            env_file={RELEASE_GITHUB_TOKEN_ENV: "github_pat_rotated_in_env_file"},
        )
        assert rc == 0
        assert captured.get(RELEASE_GITHUB_TOKEN_ENV) == "github_pat_rotated_in_env_file"
        assert self._MARKER not in out

    def test_no_warning_for_a_non_release_session(self, fake_home, capsys):
        """A non-release session never forwards the credential, so which value
        would have won is moot there."""
        rc, captured, out, _ = self._launch(
            fake_home,
            capsys,
            exported={RELEASE_GITHUB_TOKEN_ENV: "github_pat_exported"},
            env_file={RELEASE_GITHUB_TOKEN_ENV: "github_pat_in_file"},
            task="review",
        )
        assert rc == 0
        assert captured.get(RELEASE_GITHUB_TOKEN_ENV) is None
        assert self._MARKER not in out
