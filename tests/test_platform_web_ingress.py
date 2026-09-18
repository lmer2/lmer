"""Mount the ingress UI against health responses and verify the fleet placement."""
import subprocess
from pathlib import Path

from tests.conftest import node_binary, require_node_toolchain

WEB = Path(__file__).resolve().parent.parent / "web"


def test_ingress_component_health_and_recovery():
    node = node_binary()
    if not node:
        require_node_toolchain("Node is required for ingress UI tests")
    result = subprocess.run(
        [node, "node_modules/vitest/vitest.mjs", "run", "tests/IngressStatus.test.js"],
        cwd=WEB, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_ingress_is_visible_in_an_empty_fleet():
    app = (WEB / "src" / "App.vue").read_text()
    position = app.index('<IngressStatus :now="now" />')
    assert position < app.index('v-if="runs.length"', position)
