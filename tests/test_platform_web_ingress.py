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
    app = (WEB / "src/App.vue").read_text()
    settings = (WEB / "src/components/PlatformSettings.vue").read_text()
    header = app.split('<v-app-bar ', 1)[1].split('</v-app-bar>', 1)[0]
    assert 'aria-label="Platform settings"' in header
    assert '@click="showPlatformSettings"' in header
    handler = app.split('function showPlatformSettings(', 1)[1].split('{', 1)[1].split('\n}', 1)[0]
    assert "view.value = 'platform-settings'" in handler
    assert '<IngressStatus :now="now" />' in settings
    assert 'runs.length' not in settings
