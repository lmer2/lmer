"""What ``scripts/platform-container-run.sh`` actually assembles (#150).

The helper is spike-stage tooling and stays deliberately readable, but the run
line it prints is where the platform container's invariants live: the state dir
mounted at its own absolute path, ``HOME`` telling the truth about the host, the
uid that owns that state dir, the host network namespace, and credentials
forwarded by *name* so no value reaches an argument list. Every one of those is a
silent failure when it is wrong — a renamed mount makes the platform write
sessions' state where nothing reads it — so they are pinned here rather than left
to a walk of the runbook.

Everything runs through ``--print``, which stops before the runtime is invoked.
The stubbed ``docker`` is therefore never executed; it exists because the helper
refuses to build a command for a runtime that is not on ``PATH``. The environment
handed to each run is built from scratch rather than inherited, so an ambient
``LMER_*`` on the developer's machine cannot decide what these assertions see.
"""

import os
import shlex
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "scripts" / "platform-container-run.sh"

#: The helper is bash (``compgen -e``, ``${var,,}``, arrays), not POSIX sh.
pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="the helper is a bash script"
)


@pytest.fixture(scope="module")
def runtime_socket():
    """A real AF_UNIX socket, which is all ``--socket`` asks for.

    In its own short temporary directory rather than under ``tmp_path``: the path
    goes into a ``sockaddr_un``, which holds 108 bytes on Linux, and pytest names
    its temporary directories after the test.
    """
    directory = Path(tempfile.mkdtemp(prefix="lmer-platform-sock-"))
    path = directory / "runtime.sock"
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    try:
        yield path
    finally:
        server.close()
        shutil.rmtree(directory, ignore_errors=True)


class _Run:
    """One ``--print`` invocation: the argv it printed and the notes it wrote."""

    def __init__(self, completed):
        self.completed = completed
        # printf '%q ' quotes for a shell, and shlex reads that back. Fine for the
        # paths a tmp dir produces; a path needing bash's $'...' form would not
        # round-trip, and is not a case this helper has to serve.
        self.argv = shlex.split(completed.stdout)
        self.notes = completed.stderr

    @property
    def volumes(self):
        """The ``host:dest:mode`` specs, in order."""
        return [
            self.argv[index + 1]
            for index, arg in enumerate(self.argv)
            if arg == "--volume"
        ]

    @property
    def env_names(self):
        """Variables forwarded by name — i.e. ``--env NAME`` with no ``=``."""
        return [
            self.argv[index + 1]
            for index, arg in enumerate(self.argv)
            if arg == "--env" and "=" not in self.argv[index + 1]
        ]

    def flag_value(self, flag):
        return self.argv[self.argv.index(flag) + 1]


class _Host:
    """A fake host: a tmp ``HOME`` with a state dir, a stub runtime, a socket."""

    def __init__(self, tmp_path, runtime_socket):
        self.home = tmp_path / "home"
        self.state_dir = self.home / ".lmer"
        (self.state_dir / "platform").mkdir(parents=True)
        self.socket = runtime_socket
        self.bin_dir = tmp_path / "bin"
        self.bin_dir.mkdir()
        stub = self.bin_dir / "docker"
        stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        stub.chmod(0o755)

    @property
    def config_json(self):
        return self.state_dir / "platform" / "config.json"

    def write_config(self, body):
        self.config_json.write_text(body, encoding="utf-8")

    def run(self, *args, env=None, check=True):
        environ = {
            "HOME": str(self.home),
            # The stub first, then the system directories the helper's own tools
            # come from (id, stat, grep, sed, and python3 for the config parse).
            "PATH": f"{self.bin_dir}:/usr/bin:/bin",
            **(env or {}),
        }
        completed = subprocess.run(
            ["bash", str(HELPER), "--print", "--socket", str(self.socket), *args],
            capture_output=True, text=True, timeout=60, env=environ,
        )
        if check:
            assert completed.returncode == 0, (
                f"the helper failed:\n{completed.stdout}\n{completed.stderr}"
            )
        return _Run(completed)


@pytest.fixture
def host(tmp_path, runtime_socket):
    return _Host(tmp_path, runtime_socket)


# --- the path-identity invariant ---------------------------------------------

def test_the_state_dir_is_mounted_at_its_own_absolute_path(host):
    """THE invariant: the same path on both sides, read-write.

    A renamed mount does not fail. The platform hands the *host's* daemon ``-v``
    arguments it derived from its own ``Path.home()``, and the daemon creates a
    missing bind source rather than refusing — so sessions write their logs, asks
    and transcripts to one place while the platform reads another.
    """
    run = host.run()
    assert f"{host.state_dir}:{host.state_dir}:rw" in run.volumes, run.argv


def test_home_is_the_hosts_home(host):
    """``Path.home()`` in-container is what every derived host path starts from."""
    run = host.run()
    assert f"HOME={host.home}" in run.argv, run.argv


def test_the_user_is_the_one_that_owns_the_state_dir(host):
    """A mismatch writes root-owned state into the invoking user's home."""
    run = host.run()
    assert run.flag_value("--user") == f"{os.getuid()}:{os.getgid()}"


def test_the_container_shares_the_hosts_network_namespace(host):
    """Session control planes publish on host loopback; so must the prober."""
    assert "--network=host" in host.run().argv


def test_the_runtime_socket_is_mounted_where_the_docker_client_looks(host):
    """The image ships the docker client only; podman's socket answers it too."""
    run = host.run()
    assert f"{host.socket}:/var/run/docker.sock:rw" in run.volumes, run.argv


# --- environment forwarding --------------------------------------------------

def test_the_ui_dist_variable_is_refused_with_a_note(host):
    """It names a *host* directory, and would shadow the bundle baked in here."""
    run = host.run(env={"LMER_PLATFORM_UI_DIST": "/host/ui"})
    assert "LMER_PLATFORM_UI_DIST" not in run.env_names, run.argv
    assert "/host/ui" not in run.completed.stdout
    assert "not forwarding LMER_PLATFORM_UI_DIST" in run.notes


def test_the_shared_secret_is_forwarded_by_name_and_never_by_value(host):
    """``-e NAME`` has the runtime copy the value, so no ``ps`` output holds it."""
    secret = "sentinel-secret-value"
    run = host.run(env={"LMER_PLATFORM_SECRET": secret})
    assert "LMER_PLATFORM_SECRET" in run.env_names, run.argv
    assert secret not in run.completed.stdout
    assert secret not in run.notes


def test_work_repo_and_target_repo_tokens_are_forwarded_by_name(host):
    """The daemon's mirror pull and the ``lmer`` it spawns both authenticate.

    ``LMER_WORK_REPO_TOKEN`` is the work-repo mirror's (``workrepo``); the
    ``GITLAB_TOKEN``/``GITLAB_TOKEN_<host>`` pair is what a target-repo clone
    resolves through (``lmer_cli.tokens.get_token``). Forwarding
    ``LMER_WORK_REPO`` without them left every clone in a containerized platform
    unauthenticated.
    """
    values = {
        "LMER_WORK_REPO": "git@gitlab.example.com:team/work-repo.git",
        "LMER_WORK_REPO_TOKEN": "sentinel-work-token",
        "GITLAB_TOKEN": "sentinel-generic-token",
        "GITLAB_TOKEN_gitlab_example_com": "sentinel-host-token",
    }
    run = host.run(env=values)
    for name in values:
        assert name in run.env_names, f"{name} is not forwarded: {run.argv}"
    for value in values.values():
        assert value not in run.completed.stdout
        assert value not in run.notes


def test_an_env_request_for_an_unset_variable_is_noted(host):
    """Silently dropping it looks exactly like a variable that arrived."""
    run = host.run("--env", "SOME_UNSET_VARIABLE")
    assert "SOME_UNSET_VARIABLE" not in run.env_names
    assert "--env SOME_UNSET_VARIABLE is not set" in run.notes


def test_a_state_path_variable_is_forwarded_only_when_its_path_is_mounted(host):
    """``LMER_PLATFORM_SECRET_FILE`` outside the mounts relocates the secret.

    Forwarded, the daemon looks for the secret at a path this container does not
    have, finds nothing, and mints a new one — refusing every client holding the
    old one. Refused, the state-dir default takes over, which is a path that
    exists on both sides.
    """
    unmounted = host.run(env={"LMER_PLATFORM_SECRET_FILE": "/srv/secrets/platform"})
    assert "LMER_PLATFORM_SECRET_FILE" not in unmounted.env_names, unmounted.argv
    assert "not forwarding LMER_PLATFORM_SECRET_FILE" in unmounted.notes

    inside = host.run(
        env={"LMER_PLATFORM_SECRET_FILE": str(host.state_dir / "platform" / "secret")}
    )
    assert "LMER_PLATFORM_SECRET_FILE" in inside.env_names, inside.argv
    assert "not forwarding LMER_PLATFORM_SECRET_FILE" not in inside.notes


# --- the host paths a persisted config can name ------------------------------

def test_config_json_host_paths_outside_the_mounts_are_named_with_their_failure(host):
    """The three fields that escape the state dir, and what each one costs.

    A state dir that has served a bare-host platform is the one the runbook says
    to reuse, and it is the one likely to carry these. None of the three fails
    loudly by itself, so the helper naming them is the only warning there is.
    """
    host.write_config(
        '{\n'
        '  "lmer_bin": "/opt/checkout/.venv/bin/lmer",\n'
        '  "secret_file": "/srv/secrets/platform",\n'
        '  "work_repo_mirror": "/srv/mirrors/work"\n'
        '}\n'
    )
    run = host.run()

    assert "lmer_bin=/opt/checkout/.venv/bin/lmer" in run.notes, run.notes
    assert "ENOENT" in run.notes
    assert "secret_file=/srv/secrets/platform" in run.notes
    assert "mints a NEW one" in run.notes
    assert "work_repo_mirror=/srv/mirrors/work" in run.notes
    assert "re-cloned" in run.notes
    # A note, not a refusal: the operator may have mounted the paths some other
    # way, and a helper that will not print a command cannot be argued with.
    assert run.argv[:2] == ["docker", "run"]


def test_config_json_paths_under_the_state_dir_are_not_flagged(host):
    """They ride the state mount, which is the shape this topology is built for."""
    host.write_config(
        '{\n'
        f'  "secret_file": "{host.state_dir}/platform/secret",\n'
        f'  "work_repo_mirror": "{host.state_dir}/platform/work-repo"\n'
        '}\n'
    )
    run = host.run()
    assert "config.json sets" not in run.notes, run.notes


def test_a_config_json_that_is_not_json_does_not_stop_the_run(host):
    """The check is a courtesy; refusing to start the platform over it is not."""
    host.write_config("{ this is not json\n")
    run = host.run()
    assert run.argv[:2] == ["docker", "run"]
    assert "config.json sets" not in run.notes


def test_a_config_json_host_path_that_is_mounted_is_accepted_quietly(host):
    """``--mount-rw`` is the documented fix, so it has to actually silence it."""
    mirror = host.home / "mirrors" / "work"
    mirror.mkdir(parents=True)
    host.write_config(f'{{"work_repo_mirror": "{mirror}"}}\n')

    flagged = host.run()
    assert f"work_repo_mirror={mirror}" in flagged.notes

    mounted = host.run("--mount-rw", str(mirror))
    assert f"{mirror}:{mirror}:rw" in mounted.volumes
    assert "config.json sets" not in mounted.notes, mounted.notes


# --- what goes where in the command line -------------------------------------

def test_passthrough_arguments_land_after_the_image(host):
    """After the image is the ENTRYPOINT's argv: ``-- status`` is a CLI call."""
    run = host.run("--", "status")
    assert run.argv[-2:] == ["lmer-platform:dev", "status"]


def test_runtime_arguments_land_before_the_image(host):
    """Which is where docker and podman stop reading their own flags."""
    run = host.run("--runtime-arg", "--memory=2g", "--", "status")
    image = run.argv.index("lmer-platform:dev")
    assert run.argv.index("--memory=2g") < image, run.argv
