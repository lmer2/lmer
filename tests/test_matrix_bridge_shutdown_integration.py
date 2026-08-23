"""The one shutdown test that runs as PID 1 — a real container, really stopped.

None of the unit tests in ``test_matrix_bridge_cli.py`` can catch a PID-1
regression: the kernel does not deliver default-disposition signals to PID 1,
so "the handler exists" is only provable from inside a container whose
entrypoint is the bridge. This is the test that failed on every stop of the
first production deployment (issue #349): SIGTERM dropped, ten seconds of
grace, SIGKILL with the crypto store open.

**Skipped unless ``LMER_BRIDGE_IMAGE`` names a built bridge image** — nothing
else in this suite runs a container, and the gate must not grow a
podman-and-image prerequisite. The skip never hides a failure: when the
variable is set, the test genuinely runs, and on an image without the handler
it fails the way the deployment did.

The bridge only parks — the state a stop request has to interrupt — after a
successful startup, which needs its config, secrets and homeserver. Those are
real on the deploy host and nowhere else, so the runner supplies them through
``LMER_BRIDGE_RUN_ARGS`` (shlex-split, appended to the ``run`` command:
volumes, env files, network). To run it by hand on the deploy host — shown
with podman because that is the deploy host's runtime; the test itself
resolves the runtime via ``detect_runtime()``, overridable with
``LMER_BRIDGE_RUNTIME`` on a host where detection (docker-first) would pick
the runtime the image was not built with::

    export LMER_BRIDGE_RUNTIME=podman

    podman build -t lmer-matrix-bridge:test -f Dockerfile.matrix-bridge .
    export LMER_BRIDGE_IMAGE=lmer-matrix-bridge:test
    export LMER_BRIDGE_RUN_ARGS="--network=host -v bridge-state:/data --env-file /etc/lmer/bridge.env"
    pytest tests/test_matrix_bridge_shutdown_integration.py -rs

Acceptance, in the deploy agent's own words: a stop that produces exit 0
inside the grace window and no SIGKILL line in the journal.
"""

import os
import shlex
import subprocess
import time
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("LMER_BRIDGE_IMAGE"),
    reason=(
        "set LMER_BRIDGE_IMAGE to a built bridge image (and "
        "LMER_BRIDGE_RUN_ARGS to the deployment's mounts/env) to run the "
        "real-container shutdown test"
    ),
)

#: How long the bridge gets to reach its running state before the test gives
#: up. Startup is store + room + one baseline snapshot — seconds, not minutes.
STARTUP_TIMEOUT = 60

#: The stop must finish well inside the runtime's 10-second SIGTERM grace; a
#: stop that takes the whole window IS the SIGKILL escalation this test exists
#: to catch, so the bound is deliberately tighter than the grace.
STOP_DEADLINE = 8


def _run(*argv: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, check=check, capture_output=True, text=True, timeout=120,
    )


def _logs(runtime: str, name: str) -> str:
    proc = _run(runtime, "logs", name)
    return proc.stderr + proc.stdout


def test_a_container_stop_is_a_clean_exit_inside_the_grace():
    from lmer_cli.runtime import detect_runtime

    # Overridable because detection prefers docker: on a host carrying both
    # runtimes, an image built with podman is invisible to docker, and the
    # test would error against the wrong store.
    runtime = os.environ.get("LMER_BRIDGE_RUNTIME") or detect_runtime()
    image = os.environ["LMER_BRIDGE_IMAGE"]
    extra = shlex.split(os.environ.get("LMER_BRIDGE_RUN_ARGS", ""))
    name = f"lmer-bridge-shutdown-test-{uuid.uuid4().hex[:8]}"

    _run(runtime, "run", "-d", "--name", name, *extra, image, "run")
    try:
        # Parked, not merely started: `matrix_bridge_started` is the journal
        # key the bridge logs on entering the state a stop has to interrupt.
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while True:
            logs = _logs(runtime, name)
            if "matrix_bridge_started" in logs:
                break
            alive = _run(
                runtime, "inspect", "--format", "{{.State.Running}}", name,
            ).stdout.strip()
            if alive != "true":
                raise AssertionError(
                    "the bridge exited before reaching its running state — "
                    "the environment (config/secrets/homeserver) is not the "
                    f"deployment's; container logs:\n{logs}"
                )
            if time.monotonic() > deadline:
                raise AssertionError(
                    "the bridge never logged matrix_bridge_started within "
                    f"{STARTUP_TIMEOUT}s; container logs:\n{logs}"
                )
            time.sleep(1)

        started = time.monotonic()
        _run(runtime, "stop", name)
        elapsed = time.monotonic() - started

        exit_code = _run(
            runtime, "inspect", "--format", "{{.State.ExitCode}}", name,
        ).stdout.strip()
        logs = _logs(runtime, name)

        assert elapsed < STOP_DEADLINE, (
            f"stop took {elapsed:.1f}s — the SIGTERM grace ran out, which is "
            "the SIGKILL escalation this fix removes"
        )
        assert exit_code == "0", (
            f"a requested stop must exit 0, got {exit_code}; logs:\n{logs}"
        )
        assert "matrix_bridge_stopping" in logs, (
            "the receipt journal line is missing — the handler never saw the "
            f"signal; logs:\n{logs}"
        )
        assert "matrix_bridge_stopped" in logs, (
            "the drain never completed — the transport was not closed before "
            f"exit; logs:\n{logs}"
        )
    finally:
        _run(runtime, "rm", "-f", name, check=False)
