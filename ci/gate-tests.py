#!/usr/bin/env python3
"""Describe lmer's parallel gate invocation (work-repo argv-v1)."""

import json
import os
from pathlib import Path
import subprocess
import sys

from lmer_cli.test_resources import SERIAL_ENV, read_limits


def test_command(argv: list[str]) -> list[str]:
    """Keep the gate's interpreter and targets; cap this project's workers at four."""
    if os.environ.get(SERIAL_ENV, "").strip().lower() in {"1", "true", "yes"}:
        return argv
    limits = read_limits()
    if limits.cpus is None or limits.memory_bytes is None:
        return argv
    workers = min(4, limits.cpus)
    if workers < 2:
        return argv
    try:
        probe = subprocess.run(
            [argv[0], "-c", "import xdist"], capture_output=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return argv
    return argv + ["-n", str(workers)] if probe.returncode == 0 else argv


def main() -> None:
    if sys.argv[1:2] == ["--run"]:
        # A gate under test must not rediscover the enclosing suite's override.
        os.environ[SERIAL_ENV] = "1"
        argv = sys.argv[2:]
        os.execvpe(argv[0], argv, os.environ)
    if sys.argv[1:] != ["--lmer-command"]:
        raise SystemExit("Use the work-repo argv-v1 parallel test runner contract")
    request = json.load(sys.stdin)
    argv = request["argv"]
    targets = request["targets"]
    expected = [argv[0], "-m", "pytest", *targets, "-x", "--tb=short", "-q",
                "--ignore=tests/test_container_build.py"]
    if argv != expected:
        raise SystemExit("Unrecognized lmer gate invocation")
    command = [argv[0], str(Path(__file__).resolve()), "--run", *test_command(argv)]
    sys.stdout.write(json.dumps({"argv": command, "targets": targets}) + "\n")


if __name__ == "__main__":
    main()
