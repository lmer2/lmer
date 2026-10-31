"""Run the mounted-UI vitest files the gate would otherwise never execute.

The gate and CI run pytest only, so a ``web/tests/*.test.js`` file runs there
only through a wrapper like this one (issue #507: its regressions for the chat
cursor, digest rows, the question badge, the status strip and the settings
dialog live in these files; issue #617 added the answer box and the per-launch
resume rows, whose disabled-send reasons are only observable here; issue #510
the Send Privately editor; issue #465 the operator channel dock's retention;
issue #646 the rotate-every-assistant modal).
"""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.conftest import node_binary, require_node_toolchain

WEB = Path(__file__).resolve().parents[1] / "web"

SUITES = [
    "tests/AnswerBox.test.js",
    "tests/AskChannel.test.js",
    "tests/AssistantHistory.test.js",
    "tests/AssistantSettings.test.js",
    "tests/Chat.test.js",
    "tests/ForgetCompleted.test.js",
    "tests/OperatorChat.test.js",
    "tests/PrivateSend.test.js",
    "tests/Projects.test.js",
    "tests/Ratchet.test.js",
    "tests/RequestRecovery.test.js",
    "tests/RotateAll.test.js",
    "tests/SessionsPerTarget.test.js",
    "tests/StatusStrip.test.js",
    "tests/UnassignedRuns.test.js",
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
