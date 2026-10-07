"""Run the mounted-UI vitest files the gate would otherwise never execute.

The gate and CI run pytest only, so a ``web/tests/*.test.js`` file runs there
only through a wrapper like this one (issue #507: its regressions for the chat
cursor, digest rows, the question badge, the status strip and the settings
dialog live in these files).
"""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.conftest import node_binary, require_node_toolchain

WEB = Path(__file__).resolve().parents[1] / "web"

SUITES = [
    "tests/AssistantHistory.test.js",
    "tests/AssistantSettings.test.js",
    "tests/OperatorChat.test.js",
    "tests/Projects.test.js",
    "tests/RequestRecovery.test.js",
    "tests/StatusStrip.test.js",
]


@pytest.mark.parametrize("suite", SUITES)
def test_the_mounted_suite_passes(suite):
    node = node_binary()
    if not node:
        require_node_toolchain("Node is required for mounted UI tests")
    result = subprocess.run(
        [node, "node_modules/vitest/vitest.mjs", "run", suite],
        cwd=WEB, capture_output=True, text=True, timeout=600,
        env={**os.environ, "PROJECT_TEST_PYTHON": sys.executable},
    )
    assert result.returncode == 0, result.stdout + result.stderr
