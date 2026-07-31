"""Guards on the control UI's shell — the run navigator (issue #141, T39).

Source-level invariants, like :mod:`tests.test_platform_web_app` and
:mod:`tests.test_platform_web_terminal`, because this image has no JS runner and
no browser. That constraint bites hardest here: a navigator is a list of rows,
and every way it goes wrong still renders a tidy list.

The drawer exists because the detail view was a dead end — reaching a second run
meant going back to the fleet first, which is three taps to answer the second of
two waiting questions. Making the list fit beside a run means cutting things, and
the cut that would quietly destroy the product is the signal: this whole app is
here to answer "which run needs me" at a glance. A compact row that dropped the
state tone would be a neat list of names that answers nothing, and it would look
completely fine in a screenshot.

So each test pins one decision whose loss has no visible symptom:

- the row's colour is the run's tone, taken from ``format.js``'s ``toneColor()``,
  and never a hex a component picked
- the tone is the *row's*, not the state's: a live question sits on a session
  that is ``running``, and a green play button at the top of "needs you"
  contradicts the only thing that row is there to say
- ``detached`` is not a shade of ``running`` — that state exists precisely to
  stop a UI implying liveness nobody verified, and a shrunken row is the easiest
  place to lose the distinction
- every state and every tone the backend can produce draws *something*, because
  a blank gutter reads as "nothing to see here"
- the drawer is permanent beside the content on a desktop and an overlay on a
  phone, decided by Vuetify's breakpoint rather than a media query of ours
- the run being shown is marked as the one being shown, and the shell and the
  navigator agree on what identifies a run — they compute that string separately
  and a divergence highlights nothing while every row still opens fine
- the navigator did not drag xterm into the first load

How any of it looks, and whether two icon buttons crowd a phone's app bar, are
verified by building the bundle and by live test LT3 on a real phone.
"""

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
APP = WEB / "src" / "App.vue"
RUN_NAV = WEB / "src" / "components" / "RunNav.vue"
RUN_DETAIL = WEB / "src" / "components" / "RunDetail.vue"
FORMAT = WEB / "src" / "format.js"


def _read(path):
    return path.read_text(encoding="utf-8")


def _function_body(text, signature):
    """Source of one top-level function in a ``<script setup>`` block.

    Same helper as :mod:`tests.test_platform_web_terminal`: every function in
    these components is top-level, so a ``}`` in column zero ends one. Used for
    *ordering* assertions — which of two lookups happens first — because that is
    what several of these bugs are made of.
    """
    start = text.index(signature)
    return text[start:text.index("\n}\n", start)]


def _drawer():
    """The run navigator's drawer — its opening tag, attributes and all.

    The first of the two the shell has since T31: this file is about the
    navigator, and the supervisor's right-hand drawer has its own guards in
    :mod:`tests.test_platform_web_assistant`.
    """
    match = re.search(r"<v-navigation-drawer\b[^>]*>", _read(APP), re.S)
    assert match, "App.vue has no navigation drawer, so the detail view is a dead end"
    assert 'location="left"' in match.group(0), (
        "the first drawer in App.vue is no longer the run navigator, so every "
        "assertion below is about the wrong panel"
    )
    return match.group(0)


def _icon_map(name):
    """One of RunNav's ``{ key: mdiIcon }`` maps, as a dict.

    The values are asserted to be ``mdi*`` identifiers rather than strings: path
    data pasted inline would drift from the package and defeat the tree-shaking
    that keeps the icon set to the handful this UI uses.
    """
    text = _read(RUN_NAV)
    block = re.search(rf"const {name} = \{{(.*?)\n\}}", text, re.S)
    assert block, f"RunNav.vue no longer has a {name} map"
    pairs = dict(re.findall(r"(\w+): (mdi\w+),", block.group(1)))
    assert pairs, f"{name} maps nothing to an @mdi/js icon"
    return pairs


def _state_tones():
    """``format.js``'s state → tone mapping, parsed out of STATE_META.

    Read rather than restated: the point of several assertions below is that the
    navigator agrees with the one place that decides what a state means.
    """
    text = _read(FORMAT)
    block = re.search(r"const STATE_META = \{(.*?)\n\}", text, re.S)
    assert block, "format.js no longer maps states to tones"
    return dict(re.findall(r"(\w+): \{[^}]*tone: '(\w+)'", block.group(1)))


def _tones():
    """The tone vocabulary, from ``format.js``'s TONE_COLORS."""
    text = _read(FORMAT)
    block = re.search(r"const TONE_COLORS = \{(.*?)\n\}", text, re.S)
    assert block, "format.js no longer maps tones to colours"
    return set(re.findall(r"^\s*'?([\w-]+)'?:", block.group(1), re.M))


# --- the dead end is gone ----------------------------------------------------

def test_the_run_list_is_reachable_without_going_back_to_the_fleet():
    """The whole point of T39: switch runs from the run you are looking at.

    A navigator that renders but is fed nothing, or is fed the calm runs only,
    looks like an empty or short list — which on a quiet fleet is exactly what a
    working one looks like. So the wiring is pinned, not just the component.
    """
    app = _read(APP)
    assert RUN_NAV.is_file()
    assert "<RunNav" in app, "the drawer holds no run list"
    assert ':runs="runs"' in app, "the navigator is not given the fleet"
    assert ':attention="attention"' in app, (
        "the navigator is not given the priority-ordered attention list, so it "
        "would have to invent an order the fleet view does not use"
    )
    assert "@open=\"open\"" in app, "picking a run in the drawer opens nothing"


def test_the_drawer_is_on_the_left_and_the_shell_has_no_third_one():
    """The operator asked for left, and the right-hand side went to T31 as reserved.

    The count was 1 until the supervisor's chat drawer landed on the right, which
    is what that reservation was for. It stays pinned as an exact number because
    the value of the rule never was "one drawer" — it is that each side has an
    owner: the run navigator left, uber lmer right (its own guards live in
    :mod:`tests.test_platform_web_assistant`). A third drawer is a layout the next
    feature quietly argues with, and on a phone it is a second full-screen overlay
    with no home.
    """
    assert 'location="left"' in _drawer()
    app = _read(APP)
    assert app.count("<v-navigation-drawer") == 2, (
        "the shell has grown a drawer beyond the navigator and uber lmer's chat"
    )
    assert app.count('location="left"') == 1 and app.count('location="right"') == 1, (
        "the two drawers no longer sit on one side each"
    )


# --- the shell on a phone ----------------------------------------------------

def test_the_drawer_is_permanent_on_a_desktop_and_an_overlay_on_a_phone():
    """A permanent drawer on a 390px screen eats the view it exists to reach.

    Vuetify already answers this — its ``mobile`` breakpoint is what the drawer
    itself consults to decide overlay-versus-inline — so the shell reads the same
    composable rather than a media query of its own. Two sources of "is this a
    phone" is how a drawer ends up permanent and 280px wide on a screen that is
    390px, with no symptom anywhere but a phone.
    """
    app = _read(APP)
    # The composable, not the exact import line: the shell imports `useTheme`
    # alongside it since T62 (the colour-scheme switcher lives in the app bar), and
    # what this pins is where "is this a phone" comes from.
    assert re.search(r"import \{[^}]*\buseDisplay\b[^}]*\} from 'vuetify'", app), (
        "the shell does not read Vuetify's own breakpoint"
    )
    assert "const { mobile } = useDisplay()" in app
    assert ':permanent="!mobile"' in _drawer(), (
        "the drawer is either always an overlay or always in the way"
    )
    assert "@media" not in app and "matchMedia" not in app, (
        "a hand-rolled breakpoint can disagree with the one the drawer uses"
    )


def test_the_drawer_starts_open_beside_the_content_and_closed_over_it():
    """The first frame has to be right, and Vuetify does not decide this one.

    It resolves the initial state itself only for a drawer with no model bound;
    with one bound it takes the value it is given. ``ref(false)`` therefore ships
    a desktop whose navigator is invisible until the window is resized, and
    ``ref(true)`` ships a phone that opens onto a full-screen menu instead of the
    fleet. Both are invisible from the build.
    """
    assert "const navOpen = ref(!mobile.value)" in _read(APP)


def test_a_phone_can_open_the_drawer_and_it_gets_out_of_the_way_after():
    """Two halves of one gesture, and each is useless without the other.

    Where the drawer is permanent there is nothing to toggle, so the button is
    offered only where it does something. And a temporary drawer that stays open
    after picking a run is an overlay covering the run it was just used to open —
    on the one screen where it covers everything.
    """
    app = _read(APP)
    nav_icon = re.search(r"<v-app-bar-nav-icon\b[^>]*>", app, re.S)
    assert nav_icon, "nothing opens the drawer on a phone"
    assert 'v-if="mobile"' in nav_icon.group(0), (
        "a toggle is offered where the drawer is permanent and cannot be closed"
    )
    assert "aria-label" in nav_icon.group(0), "an icon-only button needs a name"

    opened = _function_body(app, "function open(run)")
    assert "if (mobile.value) navOpen.value = false" in opened, (
        "the overlay stays over the run it just opened"
    )


# --- the signal survives the compaction --------------------------------------

def test_the_compact_row_still_carries_the_state_tone():
    """The one thing compaction must not cost.

    This app exists to answer "which run needs me" at a glance; the tone ramp in
    main.js is how it answers. A row that dropped it — or picked its own colour,
    which is the same thing once the theme moves — is a plain list of names that
    still looks like a working navigator.

    Both the icon and the state word are painted from ``toneColor()``, so there
    is one place that decides what urgency looks like and this is not it.
    """
    text = _read(RUN_NAV)
    assert ":color=\"toneColor(tone(run))\"" in text, (
        "the row's icon is not painted with the run's tone"
    )
    assert "`text-${toneColor(tone(run))}`" in text, (
        "the state word is not painted with the run's tone"
    )
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", text), (
        "a colour hardcoded in a component outlives the theme that matched it"
    )
    assert "--v-list-item-subtitle-opacity: 1" in text, (
        "the signal word is rendered at the subtitle's medium emphasis, which is "
        "the quiet half of dropping it"
    )


def test_the_rows_tone_is_the_runs_and_not_merely_its_states():
    """A live question sits on a session that is *running*.

    Take the tone from the state and that row draws a calm green play button
    while sitting at the top of "needs you" — the fleet card does not make that
    mistake (RunCard computes the same thing for its edge stripe) and neither may
    the navigator. A crash and a question are both urgent and are not the same
    news, so they do not share a colour either.
    """
    tone = _function_body(_read(RUN_NAV), "function tone(run)")
    assert "if (run.attention)" in tone
    assert "'bad' : 'attention'" in tone, "a crash and a question read the same"
    assert tone.index("run.attention") < tone.index("stateMeta"), (
        "the state decides the colour, so a waiting run can look calm"
    )


def test_a_detached_session_never_reads_as_a_healthy_running_one():
    """``detached`` exists to stop a UI implying liveness nobody verified.

    The process is up, but the platform lost its only view of it and nothing is
    being recorded. Rendering that as a healthy row is the exact lie the state was
    added to prevent, and a shrunken row — one icon, one word — is where the
    distinction is easiest to lose.

    Two channels, checked separately, because either alone is one refactor from
    gone: the colour (format.js separates the tones) and the shape (this map
    separates the icons).
    """
    tones = _state_tones()
    assert tones["detached"] != tones["running"], (
        "format.js gives a detached session a healthy run's tone"
    )

    icons = _icon_map("STATE_ICONS")
    assert icons["detached"] != icons["running"], (
        "a detached session and a running one draw the same shape, so the only "
        "thing keeping them apart is a colour a colour-blind operator cannot see"
    )


def test_every_state_and_tone_the_backend_can_produce_draws_something():
    """An iconless row is a blank gutter, which reads as "nothing to see here".

    That is the one thing a state this UI does not understand must not look like
    — the same argument ``toneColor()`` makes for its own fallback, one channel
    over. The state vocabulary is scraped from the inventory rather than
    restated, so a new state arrives as a failing test and someone decides what
    shape it is; the tone fallback then covers the window before they do.
    """
    from lmer_platform.inventory import RUN_STATES

    states = _icon_map("STATE_ICONS")
    for state in RUN_STATES:
        assert state in states, f"RunNav.vue draws no icon for state {state!r}"

    tone_icons = _icon_map("TONE_ICONS")
    for tone in _tones():
        assert tone in tone_icons, f"a {tone!r} row falls through to nothing"

    icon = _function_body(_read(RUN_NAV), "function icon(run)")
    assert "STATE_ICONS[run.state] || TONE_ICONS[" in icon, (
        "a state that reaches format.js before this map renders no icon at all"
    )


def test_a_run_that_needs_a_human_says_so_even_when_its_state_looks_calm():
    """The attention reason outranks the state, in the shape and in the words.

    Same reason as the tone: the run at the top of the list is there because
    something is waiting on a human, and "running · 4m ago" is not that news. The
    ordering is asserted inside the functions because a swapped lookup still
    renders a full, plausible row.
    """
    text = _read(RUN_NAV)
    reasons = _icon_map("REASON_ICONS")
    assert reasons["question"] != reasons["live_question"], (
        "answering a stopped run starts a container while replying to a live one "
        "does not; format.js keeps the words apart and the shapes follow"
    )

    icon = _function_body(text, "function icon(run)")
    assert icon.index("REASON_ICONS") < icon.index("STATE_ICONS"), (
        "the state's icon wins, so a live question draws a calm play button"
    )

    line = _function_body(text, "function line(run)")
    assert "run.attention ? attentionLabel(" in line, (
        "the row shows a state where the reason a human is needed belongs"
    )

    sections = _function_body(text, "const sections = computed")
    assert sections.index("'needs you'") < sections.index("'everything else'"), (
        "the fleet view's order is not kept, so urgent runs sort in anywhere"
    )


def test_the_navigator_shows_where_you_are():
    """A list you cannot locate yourself in is a list you re-read every time.

    The mark is the brand accent and nothing else in the row is: the ramp in
    main.js was re-separated around deep orange precisely so the accent can mean
    "you are here" without being read as "this needs you". And nothing is current
    on the fleet view — the highlight means "this is what the main area shows".
    """
    nav = _read(RUN_NAV)
    assert ':active="isCurrent(run)"' in nav, "no row is marked as the open one"
    assert 'color="primary"' in nav, "the mark is not the accent"
    assert "aria-current" in nav, "the mark is colour only, so it is not announced"

    app = _read(APP)
    assert ':current-key="view === \'detail\' ? selectedKey : null"' in app, (
        "a row stays marked as open on a view that is not showing it"
    )


def test_the_navigator_and_the_shell_agree_on_what_identifies_a_run():
    """They compute that string separately, and a divergence is silent.

    The shell decides which run is open by this key; the navigator decides which
    row to mark by rebuilding it. Change one — add a branch, drop the host — and
    the drawer marks nothing while every row still opens perfectly, which is the
    kind of bug that gets lived with rather than reported.
    """
    shape = "`${run.host}/${run.project}/${run.slug}`"
    for source in (APP, RUN_NAV):
        body = _function_body(_read(source), "function keyOf(run)")
        assert shape in body, f"{source.name} identifies a run differently"


def test_an_empty_fleet_leaves_the_navigator_saying_so():
    """Permanent furniture with nothing in it reads as a broken panel.

    The fleet view's empty state explains the scope rule at length; the drawer
    only has to admit that the emptiness is the truth rather than a failed load.
    """
    assert 'v-if="!runs.length"' in _read(RUN_NAV)
    assert "nothing tracked yet" in _read(RUN_NAV)


# --- what the drawer must not drag in ----------------------------------------

def test_the_navigator_does_not_pull_the_terminal_into_the_first_load():
    """xterm is more than half the JS and the shell is what a phone loads first.

    The tempting version of this feature preloads the run you are about to pick.
    That would put the emulator's chunk on the app's critical path for every
    glance at the fleet, undoing a split worth 90 kB gzipped — and the bundle
    still builds, so nothing says so.
    """
    for source in (APP, RUN_NAV):
        text = _read(source)
        assert "Terminal" not in text, f"{source.name} names the terminal"
    detail = _read(RUN_DETAIL)
    assert "defineAsyncComponent(() => import('./Terminal.vue'))" in detail, (
        "the terminal is no longer the only lazy thing in the detail view"
    )
    assert detail.count("import('./Terminal.vue')") == 1


# --- the bar's controls sit at the centre on a desktop -------------------------

def test_the_bar_cluster_is_centred_on_a_desktop_and_in_flow_on_a_phone():
    """The operator's call: the controls read as "tucked into the upper right".

    Centred absolutely against the bar, because a flex-spacer sandwich centres
    on whatever the title leaves over — a point that moves with the title's
    length. And only on a desktop: a 390px bar already carries the nav toggle
    and the title on the left, so a centred cluster would land on top of them.
    """
    text = _read(APP)
    cluster = re.search(r'<div class="bar-cluster"[^>]*>', text)
    assert cluster, "the bar's controls are not grouped, so nothing can place them"
    assert ":class=\"{ 'bar-cluster-centered': !mobile }\"" in cluster.group(0), (
        "centring is not keyed on the same `mobile` the drawers already read, "
        "so the bar and the drawers can disagree about which device this is"
    )
    for control in ("mdiRobot", "THEME_ICONS[themeMode]", "doRescan"):
        after_cluster = text[cluster.start():]
        assert control in after_cluster.split("</v-app-bar>")[0], (
            f"{control} is not inside the cluster, so centring moves only part "
            "of the controls"
        )
    style = text[text.index("<style scoped>"):]
    assert "position: absolute" in style and "translateX(-50%)" in style, (
        "the centred cluster is not absolute against the bar, so it centres on "
        "the title's leftovers rather than the viewport"
    )
