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


def test_the_group_rules_are_a_declared_seam_rather_than_a_buried_condition():
    """Grouping is a list of rules, editable later without touching the layout.

    The operator's next ask for this is editing the rules from the UI, so what is
    pinned here is the shape that makes that a change of *source* rather than a
    rewrite: a named constant of ``{label, pattern}`` entries, and grouping
    functions that take their rules as an argument. A hardcoded ``if project
    starts with`` would satisfy today's demo and cost that slice.
    """
    source = _code(FLEET_MAP)
    block = re.search(r"export const GROUP_RULES = \[(.*?)\]", source, re.S)
    assert block, "the group rules are no longer a declared, replaceable list"

    rules = re.findall(r"label:\s*'([^']+)'", block.group(1))
    assert "fullctl" in rules, (
        "the default rules no longer carry the operator's own example"
    )
    assert re.search(r"pattern:\s*(/|')", block.group(1)), (
        "a rule no longer carries a pattern"
    )
    assert re.search(r"rules\s*=\s*GROUP_RULES", source), (
        "the rules are read from the constant directly instead of being passed "
        "in — operator-supplied rules would have nowhere to enter"
    )


def test_the_seam_records_that_typed_rules_must_be_escaped_not_compiled():
    """The one decision the editable-rules slice must not re-open.

    ``groupRuns`` runs on every poll and every render, and a regex is not a safe
    thing to take from a text field: ``(a+)+$`` against a short path backtracks
    for minutes, and the ``try``/``catch`` in ``groupRuleFor`` cannot catch it
    because catastrophic backtracking does not throw — it spins, and the fleet
    view is gone for the session. Unreachable today, which is why it is written
    down now rather than found by the slice that makes it live.

    Pinned as a *comment* deliberately: there is no behaviour to assert yet, and
    a decision recorded nowhere is a decision the next session re-makes.
    """
    source = _read(FLEET_MAP)
    seam = re.search(r"\*\*This constant is the seam\.\*\*(.*?)export const GROUP_RULES", source, re.S)
    assert seam, "the group-rules seam no longer explains itself"
    note = seam.group(1)
    assert "escaped" in note and "literal" in note, (
        "the seam no longer says an operator-typed pattern must be escaped into "
        "a literal substring rather than compiled as a regex"
    )
    assert "backtrack" in note, (
        "the seam no longer says why — a typed regex can hang the fleet view, "
        "and that is the reason a future reader needs, not the rule alone"
    )


def test_the_shipped_group_rules_agree_about_what_a_boundary_is():
    """One boundary shape across the set, or the list cannot be read.

    An operator looking at three rules cannot tell which of two behaviours they
    will get if one is segment-anchored and the others are bare substrings.
    """
    source = _code(FLEET_MAP)
    block = re.search(r"export const GROUP_RULES = \[(.*?)\n\]", source, re.S)
    assert block, "the group rules are no longer a declared list"
    rules = re.findall(r"label: '([^']+)', pattern: /(.+)/[a-z]* \}", block.group(1))
    assert rules, f"could not read the shipped rules: {block.group(1)!r}"
    for label, pattern in rules:
        assert pattern == f"(^|[/-]){label}([/-]|$)", (
            f"rule {label!r} uses a boundary of its own ({pattern!r}); the "
            "shipped set must agree about what ends a name"
        )


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
