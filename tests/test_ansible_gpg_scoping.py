"""Negative-guarantee tests for gpg-agent forwarding scoping (#355).

Contract under test
-------------------
The forwarded gpg-agent extra socket and the public keyring that makes it
usable reach ``ansible``-taskdef sessions ONLY, keyed on the resolved task id
(``ANSIBLE_TASK_ID``). A session for ANY other taskdef receives neither: both
``LMER_ANSIBLE_GPG_AGENT`` and ``LMER_ANSIBLE_GPG_PUBRING`` are seeded
``None`` in the container env dict and no mount is assembled.

Every distinct leak path in cli.py gets its own test, because each is a
separate code path: the variables exported in the host environment, present
only in a cwd ``.env``, only in ``~/.lmer/.env``, only in an explicit
``--env-file``, and supplied by a selected preset's ``env``. Each asserts on
BOTH halves of the surface — the container env dict AND the assembled ``-v``
arguments — so a refactor cannot satisfy the suite by moving the socket from
one channel to the other. This mirrors
``tests/test_release_credential_scoping.py``, whose gate this one reuses the
mechanism of.

The asymmetry that carries the whole security story is pinned explicitly: an
``ansible`` session whose taskdef does NOT resolve host-side (it ships from
the work repo) still receives the mounts, while a ``develop`` session with
the same variables set receives nothing. The gate is the id, not taskdef
resolution.

The mechanics — how the socket and keyring are found, guarded and consumed —
live in ``tests/test_gpg_agent_forwarding.py``.
"""

import json
import os
import socket
from unittest.mock import patch

import pytest

from lmer_cli.cli import (
    ANSIBLE_GPG_AGENT_ENV,
    ANSIBLE_GPG_PUBRING_ENV,
    ANSIBLE_TASK_ID,
    _discover_all_tasks,
    _get_taskdef_paths,
    repo_root_path,
)
from lmer_cli.mounts import (
    CONTAINER_GPG_AGENT_SOCKET_PATH,
    CONTAINER_GPG_PUBRING_PREFIX,
)
from lmer_cli.runtime import _is_selinux_enforcing
from tests.test_lmer_cli_slack_target import (
    _BASE_ENV,
    _make_main_mocks,
    REPO_URL,
)


def _run_main(argv, env_in=None, captured_env=None, captured_cmd=None, home=None):
    """Run main() with the standard mock stack and a clean os.environ.

    Same harness as the release-credential scoping suite: ``home`` isolates
    the run from the developer's real ``~/.lmer/.env``, and ``captured_cmd``
    receives the full run command so the assembled ``-v`` arguments can be
    inspected.
    """
    env = {**_BASE_ENV, **(env_in or {})}
    if home is not None:
        env["HOME"] = str(home)
    with patch.dict(os.environ, env, clear=True):
        with _make_main_mocks(captured_env=captured_env) as stack:
            stack.enter_context(
                patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False)
            )
            _is_selinux_enforcing.cache_clear()
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
def gpg_host(tmp_path):
    """An isolated HOME carrying a genuinely usable gpg setup.

    The socket really is a socket and the keyring really exists and passes
    every guard, so when a negative test sees no mount the ONLY possible
    reason is the task-id gate — not an incidental guard refusal.
    """
    home = tmp_path / "home"
    gnupg = home / ".gnupg"
    gnupg.mkdir(parents=True)
    sock_path = gnupg / "S.gpg-agent.extra"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(sock_path))
    sock.listen(1)
    (gnupg / "pubring.kbx").write_bytes(b"\x00" * 8 + b"KBXf")
    yield home
    sock.close()


def _socket_path(home):
    return str(home / ".gnupg" / "S.gpg-agent.extra")


def _pubring_path(home):
    return str(home / ".gnupg" / "pubring.kbx")


def _configured(home):
    return {
        ANSIBLE_GPG_AGENT_ENV: _socket_path(home),
        ANSIBLE_GPG_PUBRING_ENV: _pubring_path(home),
    }


def _assert_no_gpg_forwarding(captured_env, captured_cmd, home):
    """Both halves of the negative guarantee, in one place."""
    assert ANSIBLE_GPG_AGENT_ENV in captured_env, (
        "The socket key must be seeded (None) in the env dict — a missing key "
        "reopens the .env-merge and preset leak paths"
    )
    assert ANSIBLE_GPG_PUBRING_ENV in captured_env, (
        "The keyring key must be seeded (None) in the env dict"
    )
    assert captured_env[ANSIBLE_GPG_AGENT_ENV] is None, (
        f"gpg socket leaked into a non-ansible session: "
        f"{captured_env[ANSIBLE_GPG_AGENT_ENV]!r}"
    )
    assert captured_env[ANSIBLE_GPG_PUBRING_ENV] is None, (
        f"gpg keyring leaked into a non-ansible session: "
        f"{captured_env[ANSIBLE_GPG_PUBRING_ENV]!r}"
    )
    for key, value in captured_env.items():
        assert value != _socket_path(home), f"Socket path found under env key {key}"
        assert value != _pubring_path(home), f"Keyring path found under env key {key}"
    assert "-v" not in captured_cmd, (
        f"No -v mount may be assembled for a non-ansible session; cmd: {captured_cmd}"
    )
    joined = "\x00".join(captured_cmd)
    assert CONTAINER_GPG_AGENT_SOCKET_PATH not in joined
    assert CONTAINER_GPG_PUBRING_PREFIX not in joined


class TestNonAnsibleSessionLeakPaths:
    """A non-ansible session receives NO gpg forwarding, whichever host-side
    channel carries the configuration — one test per code path in cli.py."""

    def test_host_environment_no_task(self, gpg_host):
        captured: dict = {}
        cmd: list = []
        rc = _run_main(
            ["--no-task", "--exec", "true", REPO_URL],
            env_in=_configured(gpg_host),
            captured_env=captured,
            captured_cmd=cmd,
            home=gpg_host,
        )
        assert rc == 0
        _assert_no_gpg_forwarding(captured, cmd, gpg_host)

    def test_host_environment_named_non_ansible_task(self, gpg_host):
        """The gate keys on the resolved task id, not on no-task mode."""
        captured: dict = {}
        cmd: list = []
        rc = _run_main(
            ["develop", REPO_URL],
            env_in=_configured(gpg_host),
            captured_env=captured,
            captured_cmd=cmd,
            home=gpg_host,
        )
        assert rc == 0
        _assert_no_gpg_forwarding(captured, cmd, gpg_host)

    def test_cwd_dotenv(self, gpg_host, tmp_path, monkeypatch):
        workdir = tmp_path / "cwd"
        workdir.mkdir()
        monkeypatch.chdir(workdir)
        (workdir / ".env").write_text(
            f"{ANSIBLE_GPG_AGENT_ENV}={_socket_path(gpg_host)}\n"
            f"{ANSIBLE_GPG_PUBRING_ENV}={_pubring_path(gpg_host)}\n"
        )
        captured: dict = {}
        cmd: list = []
        rc = _run_main(
            ["--no-task", "--exec", "true", REPO_URL],
            captured_env=captured,
            captured_cmd=cmd,
            home=gpg_host,
        )
        assert rc == 0
        _assert_no_gpg_forwarding(captured, cmd, gpg_host)

    def test_state_dir_dotenv(self, gpg_host):
        state_dir = gpg_host / ".lmer"
        state_dir.mkdir()
        (state_dir / ".env").write_text(
            f"{ANSIBLE_GPG_AGENT_ENV}={_socket_path(gpg_host)}\n"
            f"{ANSIBLE_GPG_PUBRING_ENV}={_pubring_path(gpg_host)}\n"
        )
        captured: dict = {}
        cmd: list = []
        rc = _run_main(
            ["--no-task", "--exec", "true", REPO_URL],
            captured_env=captured,
            captured_cmd=cmd,
            home=gpg_host,
        )
        assert rc == 0
        _assert_no_gpg_forwarding(captured, cmd, gpg_host)

    def test_explicit_env_file(self, gpg_host, tmp_path):
        env_file = tmp_path / "deploy.env"
        env_file.write_text(
            f"{ANSIBLE_GPG_AGENT_ENV}={_socket_path(gpg_host)}\n"
            f"{ANSIBLE_GPG_PUBRING_ENV}={_pubring_path(gpg_host)}\n"
        )
        captured: dict = {}
        cmd: list = []
        rc = _run_main(
            ["--env-file", str(env_file), "--no-task", "--exec", "true", REPO_URL],
            captured_env=captured,
            captured_cmd=cmd,
            home=gpg_host,
        )
        assert rc == 0
        _assert_no_gpg_forwarding(captured, cmd, gpg_host)

    def test_preset_env(self, gpg_host, tmp_path):
        presets_file = tmp_path / "presets.json"
        presets_file.write_text(
            json.dumps(
                {
                    "leaky": {
                        "env": {
                            ANSIBLE_GPG_AGENT_ENV: _socket_path(gpg_host),
                            ANSIBLE_GPG_PUBRING_ENV: _pubring_path(gpg_host),
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
            home=gpg_host,
        )
        assert rc == 0, f"main() must succeed with the leaky preset; rc={rc}"
        _assert_no_gpg_forwarding(captured, cmd, gpg_host)


class TestAnsibleSessionProvisioning:
    """The positive half: an ansible session receives both binds, and only
    the remapped container paths in its environment."""

    def test_ansible_session_receives_socket_and_keyring(self, gpg_host):
        captured: dict = {}
        cmd: list = []
        rc = _run_main(
            [ANSIBLE_TASK_ID, REPO_URL],
            env_in=_configured(gpg_host),
            captured_env=captured,
            captured_cmd=cmd,
            home=gpg_host,
        )
        assert rc == 0, f"ansible session must launch; rc={rc}"
        assert captured.get(ANSIBLE_GPG_AGENT_ENV) == CONTAINER_GPG_AGENT_SOCKET_PATH
        assert captured.get(ANSIBLE_GPG_PUBRING_ENV) == f"{CONTAINER_GPG_PUBRING_PREFIX}.kbx"
        # The host locations must never enter the container env.
        assert _socket_path(gpg_host) not in captured.values()
        assert _pubring_path(gpg_host) not in captured.values()
        assert cmd.count("-v") == 2, f"Expected exactly the two gpg binds; cmd: {cmd}"
        assert (
            f"{_socket_path(gpg_host)}:{CONTAINER_GPG_AGENT_SOCKET_PATH}:ro" in cmd
        )
        assert (
            f"{_pubring_path(gpg_host)}:{CONTAINER_GPG_PUBRING_PREFIX}.kbx:ro" in cmd
        )

    def test_keyring_is_derived_when_only_the_socket_is_configured(self, gpg_host):
        """The override is optional: the keyring comes from the host gpg home."""
        captured: dict = {}
        cmd: list = []
        rc = _run_main(
            [ANSIBLE_TASK_ID, REPO_URL],
            env_in={
                ANSIBLE_GPG_AGENT_ENV: _socket_path(gpg_host),
                "GNUPGHOME": str(gpg_host / ".gnupg"),
            },
            captured_env=captured,
            captured_cmd=cmd,
            home=gpg_host,
        )
        assert rc == 0
        assert captured.get(ANSIBLE_GPG_PUBRING_ENV) == f"{CONTAINER_GPG_PUBRING_PREFIX}.kbx"
        assert cmd.count("-v") == 2

    def test_ansible_session_without_the_variable_gets_nothing(self, gpg_host):
        """Unset is not an error — the taskdef simply launches without gpg."""
        captured: dict = {}
        cmd: list = []
        rc = _run_main(
            [ANSIBLE_TASK_ID, REPO_URL],
            captured_env=captured,
            captured_cmd=cmd,
            home=gpg_host,
        )
        assert rc == 0
        assert captured.get(ANSIBLE_GPG_AGENT_ENV) is None
        assert captured.get(ANSIBLE_GPG_PUBRING_ENV) is None
        assert "-v" not in cmd

    def test_blank_variable_is_treated_as_unset(self, gpg_host):
        captured: dict = {}
        cmd: list = []
        rc = _run_main(
            [ANSIBLE_TASK_ID, REPO_URL],
            env_in={ANSIBLE_GPG_AGENT_ENV: "   "},
            captured_env=captured,
            captured_cmd=cmd,
            home=gpg_host,
        )
        assert rc == 0
        assert "-v" not in cmd


class TestGateKeysOnTaskIdNotTaskdefResolution:
    """The asymmetry that carries the security story."""

    def test_the_ansible_taskdef_does_not_resolve_host_side(self):
        """Premise check: the `ansible` taskdef ships from the work repo, so
        the id the gate keys on is NOT backed by a host-side taskdef here.
        Asked of the CLI's own discovery — the same set whose miss produces
        the unknown-task warning the next test relies on. If that ever
        changes, this suite's next test stops meaning anything."""
        discovered = _discover_all_tasks(_get_taskdef_paths(repo_root_path()))
        assert discovered, "taskdef discovery found nothing — premise unverifiable"
        assert ANSIBLE_TASK_ID not in discovered

    def test_unresolved_ansible_taskdef_still_receives_the_mounts(self, gpg_host, capsys):
        captured: dict = {}
        cmd: list = []
        rc = _run_main(
            [ANSIBLE_TASK_ID, REPO_URL],
            env_in=_configured(gpg_host),
            captured_env=captured,
            captured_cmd=cmd,
            home=gpg_host,
        )
        out = capsys.readouterr()
        assert rc == 0
        assert "not found in host-side taskdef directories" in (out.out + out.err), (
            "Premise: this launch really did go down the unresolved-taskdef path"
        )
        assert cmd.count("-v") == 2, (
            "An ansible session must get gpg even when its taskdef resolves "
            "only inside the container — the gate is the resolved task id"
        )

    def test_the_same_launch_shape_for_develop_gets_nothing(self, gpg_host):
        captured: dict = {}
        cmd: list = []
        rc = _run_main(
            ["develop", REPO_URL],
            env_in=_configured(gpg_host),
            captured_env=captured,
            captured_cmd=cmd,
            home=gpg_host,
        )
        assert rc == 0
        _assert_no_gpg_forwarding(captured, cmd, gpg_host)


class TestConfiguredButUnusableIsFatal:
    """A vault session that cannot decrypt must not start — and a broken gpg
    setup may fail nothing else."""

    def test_missing_socket_aborts_the_ansible_launch(self, gpg_host, tmp_path, capsys):
        cmd: list = []
        rc = _run_main(
            [ANSIBLE_TASK_ID, REPO_URL],
            env_in={ANSIBLE_GPG_AGENT_ENV: str(tmp_path / "absent.sock")},
            captured_cmd=cmd,
            home=gpg_host,
        )
        out = capsys.readouterr()
        assert rc == 1, "A configured-but-unusable socket must abort the launch"
        assert "does not exist" in (out.out + out.err)
        assert cmd == [], "The container must never be started"

    def test_socket_that_is_not_a_socket_aborts(self, gpg_host, tmp_path, capsys):
        plain = tmp_path / "plain"
        plain.write_text("")
        rc = _run_main(
            [ANSIBLE_TASK_ID, REPO_URL],
            env_in={ANSIBLE_GPG_AGENT_ENV: str(plain)},
            home=gpg_host,
        )
        out = capsys.readouterr()
        assert rc == 1
        assert "is not a socket" in (out.out + out.err)

    def test_missing_keyring_aborts_and_names_the_override(self, gpg_host, capsys):
        (gpg_host / ".gnupg" / "pubring.kbx").unlink()
        rc = _run_main(
            [ANSIBLE_TASK_ID, REPO_URL],
            env_in={
                ANSIBLE_GPG_AGENT_ENV: _socket_path(gpg_host),
                "GNUPGHOME": str(gpg_host / ".gnupg"),
            },
            home=gpg_host,
        )
        out = capsys.readouterr()
        assert rc == 1
        assert ANSIBLE_GPG_PUBRING_ENV in (out.out + out.err)

    def test_the_same_broken_configuration_does_not_fail_another_taskdef(
        self, gpg_host, tmp_path
    ):
        """The refusal is scoped to the session that asked for gpg."""
        captured: dict = {}
        cmd: list = []
        rc = _run_main(
            ["develop", REPO_URL],
            env_in={ANSIBLE_GPG_AGENT_ENV: str(tmp_path / "absent.sock")},
            captured_env=captured,
            captured_cmd=cmd,
            home=gpg_host,
        )
        assert rc == 0, "A broken gpg setup may only fail the taskdef that asked for it"
        assert captured.get(ANSIBLE_GPG_AGENT_ENV) is None
