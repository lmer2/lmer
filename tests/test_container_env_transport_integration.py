"""Cross-runtime verification of the container env transport (issue #158).

Why this file exists
--------------------
``test_container_env_transport.py`` asserts what lmer *writes*. It cannot
assert what docker and podman *do* with it: each has its own env-file parser,
nothing promises the two agree on quoting, ``$`` handling or comment
detection, and if they diverge the symptom is a value silently changing
inside a session — nobody finds out. So the assumption is discharged here,
against whichever runtimes are actually installed.

The check resolves the environment without executing anything in the
container: ``<runtime> create`` runs the client's env-file parser and
``<runtime> inspect`` reports the resolved ``Config.Env``. That keeps the
image's entrypoint out of the way and lets JSON carry values a shell round
trip could not (newlines included), so both transport legs are covered.

Gating: skipped unless a runtime is on ``PATH``, its daemon answers, and a
usable image is already present locally. Nothing is ever pulled. Set
``LMER_ENV_TRANSPORT_TEST_IMAGE`` to name an image explicitly; otherwise
``LMER_IMAGE``, the resolved lmer image, then ``busybox``/``alpine`` are
tried in that order.
"""

import functools
import json
import os
import shutil
import subprocess

import pytest

from lmer_cli.build import resolve_image_tag
from lmer_cli.runtime import build_container_env, repo_root_path
from tests.test_container_env_transport import _HAZARD_VALUES

# The gating probes run at COLLECTION time, in every full-suite run on every
# host — so they get a short leash and never propagate a failure, or a
# misconfigured daemon would take the whole collection down instead of
# skipping three tests.
_PROBE_TIMEOUT = 10
_TIMEOUT = 60

# Values that cannot ride an env-file line and must arrive via the bare
# ``-e NAME`` inheritance marker instead.
_UNREPRESENTABLE_VALUES = {
    "LMER_HAZARD_NEWLINE": "first line\nsecond line",
    "LMER_HAZARD_BLANK_LINE": "before\n\nafter",
    "LMER_HAZARD_CR": "yes\rno",
}


@functools.lru_cache(maxsize=1)
def _image_candidates():
    explicit = os.environ.get("LMER_ENV_TRANSPORT_TEST_IMAGE")
    if explicit:
        return (explicit,)
    candidates = []
    from_env = os.environ.get("LMER_IMAGE")
    if from_env:
        candidates.append(from_env)
    try:
        resolved = resolve_image_tag(repo_root_path())
    except Exception:
        resolved = None
    if resolved:
        candidates.append(resolved)
    candidates += ["busybox:latest", "alpine:latest"]
    return tuple(candidates)


def _probe(argv):
    """True when ``argv`` exits 0. Never raises — see _PROBE_TIMEOUT."""
    try:
        return (
            subprocess.run(
                argv, capture_output=True, text=True, timeout=_PROBE_TIMEOUT
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def _usable(runtime):
    """``(image, None)`` when this runtime can be exercised, else ``(None, why)``."""
    if shutil.which(runtime) is None:
        return None, f"{runtime} not on PATH"
    if not _probe([runtime, "info"]):
        return None, f"{runtime} daemon not reachable"
    candidates = _image_candidates()
    for image in candidates:
        if _probe([runtime, "image", "inspect", image]):
            return image, None
    return None, (
        f"no local image for {runtime} (tried {', '.join(candidates)}); set "
        f"LMER_ENV_TRANSPORT_TEST_IMAGE to one that is present"
    )


def _runtime_params():
    params = []
    for runtime in ("docker", "podman"):
        image, why = _usable(runtime)
        params.append(
            pytest.param(
                (runtime, image),
                id=runtime,
                marks=pytest.mark.skipif(image is None, reason=why or "unavailable"),
            )
        )
    return params


def _resolved_container_env(runtime, image, env):
    """The environment the runtime resolves for a container built from ``env``.

    Creates (never starts) a container so the client's own env-file parser and
    inheritance-marker lookup run for real, then reads back ``Config.Env``.
    """
    container_env = build_container_env(env)
    try:
        created = subprocess.run(
            [runtime, "create"] + container_env.args + [image, "true"],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            env=container_env.subprocess_env(),
        )
        assert created.returncode == 0, (
            f"{runtime} create rejected the transport args: {created.stderr.strip()}"
        )
        container_id = created.stdout.strip()
        try:
            inspected = subprocess.run(
                [runtime, "inspect", "-f", "{{json .Config.Env}}", container_id],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
            )
            assert inspected.returncode == 0, inspected.stderr.strip()
            entries = json.loads(inspected.stdout.strip())
        finally:
            subprocess.run(
                [runtime, "rm", "-f", container_id],
                capture_output=True,
                timeout=_TIMEOUT,
            )
    finally:
        container_env.cleanup()
    resolved = {}
    for entry in entries or []:
        name, _, value = entry.partition("=")
        resolved[name] = value
    return resolved


@pytest.mark.parametrize("runtime_image", _runtime_params())
class TestCrossRuntimeTransport:
    """Both legs, against every runtime present on this host."""

    def test_hazard_values_survive_the_env_file(self, runtime_image):
        """The prediction under test: this runtime's env-file parser does not
        strip quotes, expand ``$`` or treat an embedded ``#`` as a comment."""
        runtime, image = runtime_image
        resolved = _resolved_container_env(runtime, image, dict(_HAZARD_VALUES))
        mismatched = {
            name: (value, resolved.get(name))
            for name, value in _HAZARD_VALUES.items()
            if resolved.get(name) != value
        }
        assert not mismatched, (
            f"{runtime} altered values in transit (expected, got): {mismatched!r}"
        )

    def test_unrepresentable_values_survive_the_inheritance_leg(self, runtime_image):
        """Newline-bearing values reach the container through the client env."""
        runtime, image = runtime_image
        resolved = _resolved_container_env(
            runtime, image, dict(_UNREPRESENTABLE_VALUES)
        )
        mismatched = {
            name: (value, resolved.get(name))
            for name, value in _UNREPRESENTABLE_VALUES.items()
            if resolved.get(name) != value
        }
        assert not mismatched, (
            f"{runtime} lost multi-line values (expected, got): {mismatched!r}"
        )

    def test_both_legs_together(self, runtime_image):
        """A realistic session mixes the two; neither leg may drop the other's
        variables."""
        runtime, image = runtime_image
        combined = {**_HAZARD_VALUES, **_UNREPRESENTABLE_VALUES}
        resolved = _resolved_container_env(runtime, image, combined)
        missing = [name for name in combined if name not in resolved]
        assert not missing, f"{runtime} dropped: {missing!r}"
