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
RUN_CARD = WEB / "src" / "components" / "RunCard.vue"
RUN_DETAIL = WEB / "src" / "components" / "RunDetail.vue"
FORMAT = WEB / "src" / "format.js"


def _read(path):
    return path.read_text(encoding="utf-8")


def _function_body(text, signature):
    """Source of one top-level declaration in a ``<script setup>`` block.

    Same idea as :mod:`tests.test_platform_web_terminal`'s copy: everything in
    these components is top-level, so the first line that *starts* at column zero
    with a ``}`` ends the thing that began at ``signature``. Used for *ordering*
    assertions — which of two lookups happens first — and for scoping, which is
    what several of these bugs are made of.

    That closing line is matched rather than a bare ``\n}\n``, because a
    top-level ``watch(x, (on) => {…})`` ends ``})`` and one carrying options ends
    ``}, { immediate: true })``. Against the narrower form the search ran past
    those and returned the next few declarations as well, so two guards below were
    reading a 24-line blob while naming one watcher — and passing on text from
    outside it (found in review of !236). Everything up to and including the
    closing line is returned, so an option passed alongside the callback is inside
    the scope that names it.

    :mod:`tests.test_platform_web_terminal`'s copy is the same rule, and had to be:
    review round 2 measured it returning 346 lines for an 18-line
    ``onBeforeUnmount(()`` handler, which left the guard on releasing the socket and
    the emulator asserting against most of the file. The third copy
    (``_chat_function`` in :mod:`tests.test_platform_web_app`) is deliberately left
    narrower — every one of its ten call sites names a ``function``, so no callback
    form reaches it, and ``_chat_body`` there depends on the closing brace being
    *excluded*.
    """
    start = text.index(signature)
    end = re.search(r"^\}.*$", text[start:], re.M)
    assert end, f"nothing at column zero closes {signature!r}"
    return text[start:start + end.end()]


def _drawer(location="left"):
    """One of the shell's drawers — its opening tag, attributes and all.

    Left by default, because this file is mostly about the navigator; the
    supervisor's right-hand drawer has its own guards in
    :mod:`tests.test_platform_web_assistant` and one here, about a state it may not
    be rendered in. Taking a side rather than being copied inline is the point: the
    first cut of that guard walked the tags itself and kept the *last* match, which
    is right by accident while there is one drawer per side.
    """
    match = None
    for candidate in re.finditer(r"<v-navigation-drawer\b[^>]*>", _read(APP), re.S):
        if f'location="{location}"' in candidate.group(0):
            match = candidate
            break
    assert match, f"App.vue has no {location}-hand navigation drawer"
    return match.group(0)


def _css_rule(style, selector):
    """One declaration block out of a scoped stylesheet, by exact selector.

    Same shape as the chat module's ``_chat_rule``; here because the focused layout
    is three rules that have to agree with each other, and reading them as text
    would match the prose above them.
    """
    start = style.index(f"{selector} {{")
    return style[start:style.index("}", start)]


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


# --- the narrow band (#286) ---------------------------------------------------
#
# Operator, after living with the first cut of focused mode: the layout was the wrong
# answer to the right complaint. What they wanted was not a special place for uber
# lmer — it was the whole app held together in the middle of a wide screen, "the
# effect of narrowing the browser window", as a platform option because the window has
# other tabs in it. So the teleported panel, the fixed-width run column and the pinned
# pane are all gone, and what is left is one class and a width.

def test_the_whole_app_can_be_held_in_a_band_in_the_middle_of_the_screen():
    """The mode, and the two things that make it work at all.

    It is a class on the app's own root, because what sits against the window's edges
    is the app bar and the two drawers — and all three are ``position: fixed``, which
    no ``max-width`` on an ancestor can reach. The measured band at 2560: bar and
    navigator both start at 480 instead of 0, the conversation's drawer opens inside
    it, and nothing overflows sideways; below the band's own width the rules are inert,
    which is why a phone needs no special case.

    ``!important`` is load-bearing and is the reason this guard exists. Vuetify's
    layout composable writes ``position``, ``left``/``right`` and ``width`` as *inline*
    styles on every item it places, and an inline declaration outranks any stylesheet:
    without it the class applies, the toggle works, the preference persists — and
    nothing moves, which is exactly what the first cut of this measured.
    """
    app = _read(APP)
    assert 'class="{ \'app-narrow\': narrow }"' in app.replace(':class', 'class'), (
        "the band is not a class on the app root, so it cannot reach the fixed "
        "elements that sit against the window's edges"
    )
    style = app[app.index("<style scoped>"):]

    band = _css_rule(style, ".app-narrow")
    assert "--app-band" in band and "--app-inset" in band, (
        "the band's width is not a named property, so changing it is a hunt"
    )
    assert "max(0px" in band, (
        "a viewport narrower than the band gets a negative inset, which pushes the "
        "app off the screen instead of leaving it alone"
    )

    # The three fixed items, each with the framework's inline value overridden.
    for selector, edges in (
        (".app-narrow .v-app-bar", ("left", "right", "width")),
        (".app-narrow .v-navigation-drawer--left", ("left",)),
        (".app-narrow .v-navigation-drawer--right", ("right",)),
    ):
        rule = _css_rule(style, selector)
        for edge in edges:
            assert f"{edge}:" in rule, f"{selector} does not move its {edge}"
        assert rule.count("!important") == len(edges), (
            f"{selector} leaves an edge to a stylesheet, which the framework's "
            "inline layout style outranks — the class then does nothing at all"
        )

    # And the drawer *closed*. The framework parks a right drawer by translating it its
    # own width plus a pixel, which lands it exactly on whatever ``right`` says — and
    # ``right`` is now the inset from the window's edge, so the park stopped clearing
    # the screen: a closed drawer sat in the band's right-hand gutter showing the
    # inset's worth of itself (measured: 469px at 2560), empty on a fresh load because
    # the conversation is not mounted until it is first opened. The park carries the
    # same inset. It has to be the transform rather than putting ``right`` back, since
    # the transform is what the framework transitions — moving the edge would jump.
    parked = _css_rule(
        style, ".app-narrow .v-navigation-drawer--right:not(.v-navigation-drawer--active)",
    )
    for part in ("transform:", "var(--app-inset)", "!important"):
        assert part in parked, (
            f"the closed conversation drawer's park drops {part}, so it shows itself "
            "in the band's empty gutter"
        )

    # And the content between them, which is style.css's rule plus the inset rather
    # than instead of it: dropping either half puts the content under a drawer or
    # under a phone's rounded corner.
    main = _css_rule(style, ".app-narrow .v-main")
    for part in ("--v-layout-left", "--v-layout-right", "safe-area-inset-left",
                 "safe-area-inset-right", "var(--app-inset)"):
        assert part in main, f"the content's padding drops {part}"


def test_the_band_is_a_remembered_preference_with_a_control_that_does_something():
    """Kept from the first cut, because these two parts were never the problem.

    Offered on a desktop only: on a phone the window is already narrower than any band
    this could hold the app in, so the control would be a button that does nothing —
    and the rules being inert there is what makes that safe rather than a special case.

    Remembered through the same rules as every other stored preference (validate, fall
    back, survive a storage that throws). The key is ``.narrow`` and not the ``.focus``
    the feature was first called: what it names changed, and a key whose name means
    something else is one that gets read as the wrong answer.
    """
    app = _read(APP)

    toggle = None
    for match in re.finditer(r"<v-btn\b[^>]*>", app, re.S):
        if "narrow = !narrow" in match.group(0):
            toggle = match.group(0)
            break
    assert toggle, "nothing in the shell turns the band on"
    assert 'v-if="!mobile"' in toggle, (
        "the toggle is offered on a phone, where there is no width to give back"
    )
    assert "aria-label" in toggle, "an icon-only button needs a name"
    assert "mdiArrowCollapseHorizontal" in toggle and "mdiArrowExpandHorizontal" in toggle, (
        "the toggle draws the same icon in both states, or one pasted as path data "
        "rather than taken from @mdi/js"
    )
    cluster = app.index('<div class="bar-cluster"')
    assert cluster < app.index('@click="narrow = !narrow"') < app.index("</v-app-bar>")

    assert "const NARROW_STORAGE_KEY = 'lmer.app.narrow'" in app
    assert re.search(
        r"const narrow = ref\(storedFlag\(\s*\(\) => window\.localStorage"
        r"\.getItem\(NARROW_STORAGE_KEY\), false,\s*\)\)", app,
    ), (
        "the band is not read back through preferences.js, or it defaults to on — "
        "which would hold every first load in a band nobody asked for"
    )
    stored = _function_body(app, "watch(narrow,")
    assert "rememberFlag(" in stored and "NARROW_STORAGE_KEY" in stored, (
        f"the band is never written back — {stored}"
    )


def test_nothing_is_left_of_the_layout_the_band_replaced():
    """The first cut of this feature moved the conversation into the main view with a
    teleport, gave the run view a fixed-width column, and pinned the pane against the
    viewport. All three are gone (operator: "the focused-mode concept was misaligned in
    the original ask, simplify it"), and this is the guard that says so — a leftover
    from any of them is a second layout nobody is looking at.

    The two things the earlier rounds *did* fix are gone with the state they were
    about: a drawer that could open holding nothing, and a fleet card offering to
    reveal a conversation already on screen. Neither can happen when the conversation
    is only ever in the drawer, so the derived model and the write gate went with them
    — what is left is the drawer's own `v-model`, exactly as it was before this MR.
    """
    app = _read(APP)
    for gone in ("Teleport", "uber-stage", "uber-dock", "stage-uber", "stage-runs",
                 "stage-focused", "RUN_COLUMN_PX", "setUberOpen", "toggleUberFocus",
                 "closeUber", "offer-chat"):
        assert gone not in app, (
            f"{gone} is still in the shell, so part of the layout the band replaced "
            "is still shipping"
        )
    assert 'v-model="uberOpen"' in _drawer("right"), (
        "the drawer's open state is still derived from a mode that no longer exists"
    )
    assert "offerChat" not in _read(RUN_CARD), (
        "the run card still takes the prop that hid its chat button in a mode that "
        "no longer exists"
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
