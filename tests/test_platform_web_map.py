"""Guards on the constellation map's layout core (#141 fleet map).

The decisions are *executed* by ``tests/test_platform_web_fleetmap.js`` — what a
cluster is, what survives a poll, what paints last. What is read here is what an
executed test cannot see: that the map takes its colour from the one table this
app already has for it, and that the two attention rankings (this module's and
the daemon's) have not drifted apart.

The second is the interesting one. The map and the list both order by attention;
if they ever disagree, the operator gets two different answers to "which of these
is most urgent" from one fleet.
"""

import re
import subprocess
from pathlib import Path

from tests.conftest import node_binary, require_node_toolchain

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
FLEET_MAP = WEB / "src" / "fleetMap.js"
INVENTORY = ROOT / "src" / "lmer_platform" / "inventory.py"


def _read(path):
    return path.read_text(encoding="utf-8")


def _code(path):
    """The source with its comments removed.

    These guards are about what the module *does*, and this file's own prose
    names the things it deliberately does not reach for ("colour comes from
    format.js", "the component configures d3-force"). Matching on the raw text
    would fail on the comment that explains the rule being checked.
    """
    source = re.sub(r"/\*.*?\*/", "", _read(path), flags=re.S)
    return "\n".join(
        re.sub(r"//.*$", "", line) for line in source.splitlines()
    )


def test_the_layout_core_behaves():
    """Execute the module — the poll diff is where this feature goes wrong."""
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")

    script = Path(__file__).resolve().parent / "test_platform_web_fleetmap.js"
    result = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "all assertions passed" in result.stdout


def test_the_map_ranks_attention_exactly_as_the_daemon_does():
    """One fleet must not have two opinions about which run is most urgent.

    ``ATTENTION_PRIORITY`` is transcribed into the browser because the map sorts
    by it client-side. A transcription is a thing that rots, so this compares the
    two tables rather than trusting the comment that says to keep them in step.
    """
    source = _read(INVENTORY)
    block = re.search(r"ATTENTION_PRIORITY = \{(.*?)\}", source, re.S)
    assert block, "ATTENTION_PRIORITY is no longer a literal in inventory.py"
    daemon = {
        name: int(rank)
        for name, rank in re.findall(r'"([a-z_]+)":\s*(\d+)', block.group(1))
    }

    browser_block = re.search(
        r"export const ATTENTION_PRIORITY = \{(.*?)\}", _read(FLEET_MAP), re.S,
    )
    assert browser_block, "the map no longer carries an attention ranking"
    browser = {
        name: int(rank)
        for name, rank in re.findall(r"([a-z_]+):\s*(\d+)", browser_block.group(1))
    }

    assert browser == daemon, (
        "the map's attention ranking has drifted from the daemon's — the map and "
        "the list would order the same fleet differently"
    )


def test_the_map_does_not_carry_a_second_colour_table():
    """Colour comes from format.js, or the map and the cards disagree.

    The specific failure this stops: a state the platform calls "detached" is
    deliberately not painted as healthy, and a private palette in the map would
    re-decide that without anyone noticing.
    """
    source = _code(FLEET_MAP)
    assert "stateMeta" not in source and "toneColor" not in source, (
        "the layout core should not resolve colour at all — the component does, "
        "through format.js"
    )
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", source), (
        "the map's layout core hardcodes a colour; tones belong to format.js"
    )


def test_operator_groups_are_literal_and_have_no_deployment_defaults():
    source = _code(FLEET_MAP)
    assert "export const GROUP_RULES = []" in source
    assert "new RegExp" not in source
    assert ".includes(" in source


def test_the_map_knows_the_orchestrator_by_its_flag_and_not_by_its_dashes():
    """``orchestrator`` is on the row; the dashes are shared with every worker.

    ``inventory`` writes the same em-dash into the identity fields of any session
    that has not committed a run identity yet — a worker in its first seconds
    carries them too. Keying the standalone node on the dash would badge that
    worker as the platform itself, which is the mistake the daemon's own
    docstring warns about.
    """
    source = _code(FLEET_MAP)
    assert re.search(r"function isOrchestrator\b", source), (
        "the layout core no longer decides what the orchestrator is"
    )
    guard = re.search(r"function isOrchestrator\(run\) \{(.*?)\n\}", source, re.S)
    assert guard and "orchestrator" in guard.group(1), (
        "isOrchestrator does not read the row's orchestrator flag"
    )
    assert "\u2014" not in guard.group(1), (
        "the orchestrator is identified by a dash rather than by its flag"
    )

    key = re.search(r"export function clusterKey\(run\) \{(.*?)\n\}", source, re.S)
    assert key and "isOrchestrator" in key.group(1), (
        "the orchestrator is still being put into a cluster, so it will be drawn "
        "inside a ring with a project label"
    )


def test_the_layout_core_is_dom_free():
    """It runs under node, which is what makes the poll diff testable at all."""
    source = _code(FLEET_MAP)
    for forbidden in ("document.", "window.", "d3-force", "from 'd3"):
        assert forbidden not in source, (
            f"the layout core reaches for {forbidden!r}; it is meant to be pure"
        )


def test_a_cross_project_relation_pulls_more_weakly_than_one_inside_a_project():
    """The constraint that keeps the clusters legible.

    Found by building the map: at equal strength, a handful of cross-project
    relations drag every project into one smear and the grouping — the thing the
    view exists for — is gone.
    """
    source = _code(FLEET_MAP)
    same = re.search(r"linkStrengthSame:\s*([0-9.]+)", source)
    cross = re.search(r"linkStrengthCross:\s*([0-9.]+)", source)
    assert same and cross, "the link strengths are no longer declared as data"
    assert float(cross.group(1)) < float(same.group(1))
