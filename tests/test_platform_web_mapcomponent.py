"""Guards on the constellation map component (#141 fleet map).

Behaviour is exercised by ``web/tests/FleetMap.test.js``, which mounts the
component in jsdom — but that suite is manual (web/docs/TESTING.md), so what is
pinned here is the handful of properties that must not regress unnoticed in
``gate-check``:

- the map themes, because a hardcoded colour would look right in whichever
  scheme it was written in and wrong in the other;
- the map is reachable without a mouse, which is the property that chose SVG
  over a canvas in the first place;
- the simulation can stop, and does not animate for an operator who asked it
  not to.

These are read rather than executed on purpose: each one is a *absence* —
"no literal colour", "no missing tabindex" — and an absence is what a source
guard is good at.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
MAP = WEB / "src" / "components" / "FleetMap.vue"
APP = WEB / "src" / "App.vue"


def _read(path):
    return path.read_text(encoding="utf-8")


def _without_comments(source):
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    source = re.sub(r"<!--.*?-->", "", source, flags=re.S)
    return "\n".join(re.sub(r"//.*$", "", line) for line in source.splitlines())


def _script(source):
    """Just the ``<script setup>`` half of a single-file component.

    Load-bearing rather than tidiness. The reduced-motion guard below used to
    match against the whole file, which cannot fail: ``<style>`` carries
    ``@media (prefers-reduced-motion: reduce)`` for the halo animation, so the
    string is present no matter what the script does. Deleting the JS guard and
    all three of its call sites left the assertion green.
    """
    match = re.search(r"<script[^>]*>(.*?)</script>", source, flags=re.S)
    assert match, "the map component has no <script> block"
    return _without_comments(match.group(1))


def test_the_map_exists_and_is_reachable_from_the_fleet():
    assert MAP.is_file()
    app = _read(APP)
    assert "FleetMap" in app, "the map is not wired into the app at all"


def test_every_colour_the_map_draws_is_a_theme_token():
    """No literal colours: the map has to be right in both schemes.

    This is the concrete form of the reason the renderer is SVG rather than a
    canvas — a canvas would have needed the palette mirrored into it by hand,
    and this test could not have been written.
    """
    source = _without_comments(_read(MAP))
    literals = re.findall(r"#[0-9a-fA-F]{3,8}\b", source)
    assert not literals, f"the map hardcodes colours: {literals}"
    assert "toneColor" in source and "stateMeta" in source, (
        "the map must take its colours from format.js, like the run cards"
    )


def test_a_node_is_something_a_keyboard_can_reach_and_a_reader_can_name():
    source = _read(MAP)
    assert 'tabindex="0"' in source, "map nodes are not focusable"
    assert "aria-label" in source, "map nodes are not labelled"
    assert "keydown.enter" in source, "a focused node cannot be opened from the keyboard"
    assert 'role="button"' in source, "a node does not announce itself as activatable"


def test_the_clusters_are_hidden_from_a_screen_reader():
    """Every run in a cluster is named on its own node; the ring is decoration.

    Without this the map reads each project name twice — once as a label and
    once in every node under it.
    """
    assert 'aria-hidden="true"' in _read(MAP)


def test_the_simulation_can_stop_and_honours_reduced_motion():
    """The map settles and holds still; a node drifting under the cursor is a
    node the operator misses when they click it."""
    script = _script(_read(MAP))
    assert "alphaMin" in script, "the simulation has no stopping condition"
    assert "matchMedia" in script and "prefers-reduced-motion" in script, (
        "the map animates regardless of whether the operator asked it not to — "
        "the guard has to be in the script; the <style> media query only stops "
        "the halo keyframes"
    )
    assert "visibilitychange" in script, (
        "the map keeps simulating in a tab nobody is looking at"
    )


def test_the_reduced_motion_path_re_heats_like_the_animated_one():
    """An instant solve is the same solve, run now — not a smaller one.

    Every d3 force scales by alpha, so ticking a simulation that has decayed to
    ``alphaMin`` moves nothing: a run arriving on a later poll would keep
    whatever position d3 seeded it with, which for an unseen cluster is off the
    top of the viewBox. The component must reach the layout core's ``reheat``
    rather than ticking by hand, which is where that is decided and tested.
    """
    script = _script(_read(MAP))
    assert re.search(r"\breheat\(simulation\b", script), (
        "the map solves without re-heating; under reduced motion that solves "
        "nothing at all. Matched against the call rather than the bare word, "
        "which `FORCE.reheatAlpha` satisfies on its own."
    )
    assert not re.search(r"\bsimulation\.tick\(", script), (
        "the component ticks the simulation directly again — the re-heat and "
        "the ticks belong together in fleetMap.js's settle/reheat"
    )


def test_the_map_does_not_draw_the_orchestrator_at_all():
    """It is one tap away in the app bar, not a star on the canvas.

    The map answers "what is my fleet doing"; the session asking that question is
    not one of the answers. Excluded in the layout core so it is absent from the
    nodes, the clusters and the forces alike — a component-level `v-if` would
    still have it in the simulation, pushing real runs around.
    """
    core = _read(WEB / "src" / "fleetMap.js")
    graph = re.search(r"export function buildGraph\(runs, relations\) \{(.*?)\n\}", core, re.S)
    assert graph, "buildGraph is no longer a readable function"
    assert "isOrchestrator(run)" in graph.group(1), (
        "the orchestrator is not excluded when the graph is built, so it is still "
        "a node in the simulation even if nothing draws it"
    )

    script = _script(_read(MAP))
    assert "mdiRobot" not in script, (
        "the map still carries the uber lmer glyph; that mark belongs to the app "
        "bar control now"
    )


def test_the_bar_control_carries_the_orchestrators_state_and_what_it_needs():
    """What the removed node used to say, said where the operator can still see it.

    Dropping the node without this loses a fact the fleet view used to carry: the
    supervisor's own state. The bar control is the only place left that can say
    whether it is running, stuck, or waiting on somebody.
    """
    app = _script(_read(APP))
    assert "orchestratorOf" in app, (
        "the shell no longer finds the orchestrator row, so the bar control "
        "cannot be painted from its state"
    )
    assert "orchestratorTone" in app and "orchestratorHint" in app, (
        "the bar control has no tone or hover text of its own"
    )
    assert "toneColor" in app, (
        "the bar control's colour is not coming from the app's tone table, so it "
        "can disagree with the map about what a state looks like"
    )
    markup = _read(APP)
    button = re.search(r'<v-btn\b[^>]*:icon="mdiRobot"[^>]*/>', markup, re.S)
    assert button, "the uber lmer bar control is no longer recognisable"
    assert ':aria-label="orchestratorHint"' in button.group(0)
    assert ':color="orchestratorTone"' in button.group(0), (
        "the bar control is not painted from the orchestrator's state"
    )
    tooltip = next(block for block in re.findall(r"<v-tooltip\b.*?</v-tooltip>", markup, re.S) if button.group(0) in block)
    assert ':text="orchestratorHint"' in tooltip and 'open-on-focus' in tooltip, (
        "the bar control says nothing on hover/focus about what the orchestrator is doing"
    )


def _legend_tones():
    block = re.search(r"const LEGEND = \[(.*?)\n\]", _script(_read(MAP)), re.S)
    assert block, "the legend is no longer a declared table"
    return set(re.findall(r"\['([a-z]+)'", block.group(1)))


def test_the_map_explains_its_colours_with_a_legend():
    """A tone with no line naming it is a private language."""
    source = _read(MAP)
    assert "fleet-map__legend" in source, "the map has no legend"
    assert _legend_tones(), "the legend names no tones at all"
    assert "toneColor" in _script(source), (
        "the legend's swatches are not painted from the app's tone table, so the "
        "legend can show a tone in a colour the nodes do not use"
    )


def test_the_legend_names_every_tone_format_js_can_produce():
    """The legend is a second list, so it is compared against the first.

    Pinned against ``format.js``'s own table rather than against a literal set
    copied out of it — a literal passes just as happily when a sixth tone is
    added and the legend falls behind, which is the whole failure. The same shape
    as ``test_the_map_ranks_attention_exactly_as_the_daemon_does``, for the same
    reason: a transcription is a thing that rots, so compare the two tables.
    """
    block = re.search(
        r"const TONE_COLORS = \{(.*?)\n\}",
        _read(WEB / "src" / "format.js"),
        re.S,
    )
    assert block, "TONE_COLORS is no longer a literal in format.js"
    produced = set(re.findall(r"^\s*([a-z]+):", block.group(1), re.M))
    assert produced, f"could not read the tone table: {block.group(1)!r}"
    missing = produced - _legend_tones()
    assert not missing, (
        f"format.js can paint {sorted(missing)} and the map's legend does not "
        "explain them — a node would carry a colour with nothing naming it"
    )


def test_the_relations_fallback_is_marked_as_back_compat_and_is_not_per_poll():
    """The fallback is a demo affordance, not the design — and it must say so.

    The forward path is the `relations` key on the fleet payload: one read for the
    whole fleet. The per-run route is one request per visible run, so a ten-second
    poll over a real fleet would be a request storm for data that changes when
    somebody relates two runs. Refetching is keyed on the *set of runs* instead.
    """
    app = _read(APP)
    block = re.search(r"back-compat: relations(.*?)const relations = computed", app, re.S)
    assert block, "the relations fallback no longer explains itself"
    note = block.group(1)
    assert "dead code" in note or "temporary" in note, (
        "the fallback does not say it is temporary, so nobody will remove it when "
        "the daemon grows the key"
    )
    assert "per visible run" in note or "one request per" in note, (
        "the fallback does not state its per-poll cost, which is the reason it is "
        "a fallback and not the design"
    )
    script = _script(app)
    assert "relationsFetchedFor" in script, (
        "the fallback refetches without remembering what it fetched for — that is "
        "a request per run per poll"
    )
    assert re.search(r"if \(carried\)", script), (
        "the fallback does not stand down when the daemon sends the key"
    )


def test_the_map_does_not_own_the_selection():
    """A node hands the run back; it does not decide what opening one means.

    The same contract RunCard has — which is what keeps one way of opening a run
    in this app rather than two.
    """
    source = _read(MAP)
    assert "defineEmits(['open'])" in source
    assert "view.value" not in source, "the map is reaching into the app's routing"


def test_the_app_remembers_which_view_the_operator_reads_the_fleet_in():
    """And defaults to the list, so nobody's view changes without them asking."""
    app = _read(APP)
    assert "FLEET_VIEW_STORAGE_KEY" in app
    assert "lmer.fleet.view" in app, "the storage key is not spelled at its call site"
    modes = re.search(r"FLEET_VIEW_MODES = \[([^\]]+)\]", app)
    assert modes, "the fleet view modes are no longer an allowlist"
    first = modes.group(1).split(",")[0].strip().strip("'\"")
    assert first == "list", (
        "the default fleet view is no longer the list — every operator's view "
        "would change under them on upgrade"
    )
