"""Guards on the colour-scheme switcher (T62).

The operator asked: "vuetify seems to load a light theme depending on the browser
theme. if we have light and dark themes, then we should have a switcher for it".
Before this slice there was no override at all — ``defaultTheme: 'system'`` read
prefers-color-scheme and that was the end of it.

The switcher is three states and not a toggle, which is the one decision here
worth this file. Both schemes are first-class with the OS picking (spec §10.1), so
"follow the system" is not one end of a switch: it is the default, and a two-state
control quietly deletes it — after the first flip there is no way back, and an
operator who reads the fleet from a dark desk and a bright phone loses the
behaviour they started with. So the OS is a mode of its own, and the first one.

Source-level, like :mod:`tests.test_platform_web_app`, plus one *executed* probe
for the halves reading cannot settle: what a stored value that means nothing turns
into, and what the inline first-paint script does with each of the three words.

What each test pins:

- three states, in an order where the OS is the default, and every one of them
  reachable from every view (the scheme is the app's, not one run's)
- the preference goes through ``preferences.js``'s rules — validate, fall back,
  survive a storage that throws — with the key named at the access, which is what
  keeps the allowlist in ``test_platform_web_app`` able to read what this app
  stores
- the lever is ``theme.change()``, the only entry point the installed Vuetify
  offers that accepts ``'system'`` at all, and the one whose name ``theme.current``
  is derived from — which is what repaints the terminal, an emulator no stylesheet
  reaches
- the pre-mount frame is decided by an inline script in ``index.html`` and, for the
  page itself, *only* by ``color-scheme``: no colour comes back into ``style.css``,
  whose one job is not restating a palette that lives in ``main.js``
- the chrome the phone paints *around* that frame is the one exception, and a
  deliberate one: two background hexes are restated in ``index.html`` because the
  markup is read before any script exists, and pinned here to ``main.js``'s own —
  the same bargain the storage key gets below, and the reason the duplication is
  allowed to be exactly two values
- that script and ``App.vue`` agree on the key and on the three words. They cannot
  import each other, so nothing but a test holds them together
- every colour a component names is defined in *both* schemes, and the conversation's
  three grounds (T84) stay readable under the ink that actually lands on them. A
  tone defined in one scheme only is not a wrong colour: Vuetify emits no variable
  for it, so the declaration is invalid and the background silently disappears —
  which is a bug that ships to whichever half of the operators switched
- an inline-code chip is still a chip: it has to be a visible step off every
  surface it can be drawn on, and the light scheme's was a cool near-white on a
  white card, which is a background nobody can see
- the terminal's own ground, added by the operator's second live pass (2026-07-29),
  is held to the same bargain plus one constraint: it has to be darker than the
  surfaces around it in *both* schemes, which is what the operator asked for and is
  the reason the suggested grey-darken-4 is not the value (it is lighter than this
  app's dark background)
- the cyan that pass also added — an ink and a ground for the uber lmer row — is
  guarded by its *absence*, because the pass after it took the tinted row away in
  favour of a border and an icon. A palette key with no consumer is drift waiting to
  happen, and a component still naming one draws an invalid declaration, i.e. a
  background nobody sees

How the menu looks in a 390px app bar is verified by building the bundle and by
live test LT3 on a real phone.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

# node_binary is the two-root Node lookup (T47): the pinned toolchain is
# invisible from inside the suite through the isolated platform dir, and a copy
# that forgot the second root would not fail, it would skip everywhere. In
# conftest because five modules want it, this one included.
from tests.conftest import node_binary, require_node_toolchain
from tests.test_platform_web_app import ALLOWED_STORAGE_KEYS
# The conversation's three grounds, named where the markup that uses them is
# guarded. This module holds the other half: a colour defined in one scheme only is
# a background that vanishes the moment the switcher is touched.
from tests.test_platform_web_chat import CHAT_GROUNDS

WEB = Path(__file__).resolve().parent.parent / "web"
APP = WEB / "src" / "App.vue"
MAIN = WEB / "src" / "main.js"
STYLE = WEB / "src" / "style.css"
INDEX = WEB / "index.html"
TERMINAL = WEB / "src" / "components" / "Terminal.vue"
VUETIFY = WEB / "node_modules" / "vuetify"

#: The fence around the part of App.vue that is plain JavaScript — the vocabulary,
#: the key, and the two accessors. Lifted verbatim and executed below, because a
#: paraphrase in this file would keep passing while the component's own copy drifted
#: into trusting what it read. Prefixes, because both lines are padded out to the
#: column limit with dashes.
MODE_START = "// --- theme mode (extracted by tests/test_platform_web_theme.py)"
MODE_END = "// --- end of theme mode"

#: The same fence around main.js's other half of the browser-chrome decision: the
#: watcher that repaints the metas from the theme once the app exists. Lifted and
#: executed below for the reason the block above is — a paraphrase would keep
#: passing against a watcher that had stopped firing.
CHROME_START = "// --- browser chrome (extracted by tests/test_platform_web_theme.py)"
CHROME_END = "// --- end of browser chrome"

#: What an inline-code chip has to clear against the page and its cards, per
#: scheme. Two numbers because the two schemes are not in the same state: the light
#: chip was a cool near-white at 1.12:1 on a white card — a background nobody can
#: see, and the one cool value in a warm palette — and this is the floor the fix
#: put it above. The dark one is 1.11:1 on its own card surface, which is the same
#: papercut in the scheme where the chip at least reads inside a conversation; the
#: number here is today's, so it can only be improved, and picking its replacement
#: wants a screen rather than a ratio.
CODE_ON_PAGE = {"light": 1.45, "dark": 1.10}

#: And against the conversation's grounds, where rendered markdown puts the same
#: chip inside a turn. Lower than the page floor in the light scheme by
#: arithmetic rather than by choice: those grounds are themselves only 1.23–1.38:1
#: from white, so a chip a full 1.3 clear of *all* of them and of the card would
#: have to be dark enough to read as a button.
CODE_ON_GROUND = {"light": 1.15, "dark": 1.20}

#: Where a ``code`` element can actually be painted. The card is the common one — a
#: target, a path, a session id in a run's details — and the two conversation
#: grounds carry it inside rendered markdown. The operator's ground is deliberately
#: absent: what you sent is rendered verbatim (:mod:`tests.test_platform_web_chat`),
#: so no ``code`` element is ever drawn on it.
CODE_SURFACES = {"page": ("surface", "background"), "ground": ("chat-agent", "chat-action")}

#: Where a colour can be named in this app besides the theme: the components, their
#: scoped stylesheets and the one global one. Globbed rather than listed because the
#: guard below is about a *deleted* palette key, and a file added after the deletion
#: is exactly the one that would name it again.
SOURCE_GLOBS = ("*.js", "*.vue", "*.css")


def _read(path):
    return path.read_text(encoding="utf-8")


def _element(text, tag, start):
    """The markup of the *tag* element that opens at *start*, closing tag included.

    Same depth-counted helper as :mod:`tests.test_platform_web_details_tabs`, and
    the lookahead matters for the same reason: ``<v-app-bar-title`` begins with
    ``<v-app-bar``, so a plain substring scan never finds the close.
    """
    pattern = re.compile(rf"<{tag}(?=[\s/>])|</{tag}>")
    depth = 0
    for match in pattern.finditer(text, start):
        if match.group(0).startswith("</"):
            depth -= 1
            if not depth:
                return text[start:match.end()]
        else:
            depth += 1
    raise AssertionError(f"<{tag}> opened at {start} is never closed")


def _app_bar():
    text = _read(APP)
    return _element(text, "v-app-bar", text.index("<v-app-bar"))


def _string_constant(text, name):
    match = re.search(rf"const {name} = '([^']*)'", text)
    assert match, f"no {name} constant"
    return match.group(1)


def _string_list(text, name):
    """The words of a ``const NAME = ['a', 'b']`` array, in source order."""
    match = re.search(rf"const {name} = \[([^\]]*)\]", text)
    assert match, f"no {name} array"
    return re.findall(r"'([^']*)'", match.group(1))


def _object_keys(text, name):
    """The keys of a ``const NAME = {{ ... }}`` object literal, in source order."""
    match = re.search(rf"const {name} = \{{(.*?)\n\}}", text, re.S)
    assert match, f"no {name} object"
    return re.findall(r"^\s*([\w-]+):", match.group(1), re.M)


def _mode_block():
    """App.vue's theme-mode block, verbatim.

    Verbatim is the point: the interesting failure is the component trusting what
    it read back.
    """
    text = _read(APP)
    start = text.index(MODE_START)
    return text[start:text.index(MODE_END, start)]


def _chrome_block():
    """main.js's browser-chrome block, verbatim, for the same reason."""
    text = _read(MAIN)
    start = text.index(CHROME_START)
    return text[start:text.index(CHROME_END, start)]


def _chrome_metas():
    """index.html's ``theme-color`` metas, in source order: media and content."""
    metas = []
    for tag in re.findall(r"<meta[^>]*name=\"theme-color\"[^>]*>", _read(INDEX)):
        media = re.search(r"media=\"([^\"]*)\"", tag)
        content = re.search(r"content=\"([^\"]*)\"", tag)
        assert media and content, f"{tag} names no scheme or no colour"
        metas.append({"media": media.group(1), "content": content.group(1)})
    return metas


def _chrome_by_scheme():
    """The same metas keyed by the scheme their media query asks about."""
    found = {}
    for meta in _chrome_metas():
        scheme = re.search(r"prefers-color-scheme:\s*(\w+)", meta["media"])
        assert scheme, f"{meta['media']!r} is not a scheme this can read"
        found[scheme.group(1)] = meta["content"]
    return found


def _inline_script():
    """The one inline script in index.html — the first-paint decision, verbatim."""
    text = _read(INDEX)
    found = re.findall(r"<script>(.*?)</script>", text, re.S)
    assert len(found) == 1, (
        f"index.html has {len(found)} inline scripts; the first-paint scheme is "
        "the only thing that belongs in one"
    )
    return found[0]


# --- three states, and the OS is one of them ---------------------------------

def test_the_switcher_has_three_states_with_the_os_as_the_default():
    """The whole point of the slice, and the shape a toggle would lose.

    Order is asserted rather than membership: ``storedChoice`` falls back to the
    *first* entry, so "system" leading is what makes an unreadable or unrecognised
    preference land back on the OS rather than on a forced scheme.
    """
    app = _read(APP)

    assert _string_list(app, "THEME_MODES") == ["system", "light", "dark"], (
        f"the modes are {_string_list(app, 'THEME_MODES')} — three states with the "
        "OS first is the requirement (spec §10.1); a two-state toggle deletes "
        "'follow the system' the first time it is flipped"
    )
    # Vuetify offers both of these and both are exactly the mistake: they cycle a
    # list of *theme names*, and 'system' is not one — `theme.name` resolves it to
    # light or dark, so a cycle starting from it can never return to it.
    for shortcut in ("theme.toggle(", "theme.cycle("):
        assert shortcut not in app, (
            f"{shortcut} cycles theme names, and following the OS is not one of "
            "them — it is not reachable again once left"
        )


def test_every_state_says_what_it_is_and_can_be_picked():
    """A three-way control whose third state has no label is a two-way control
    plus a guess. Icons alone do not answer "which one is the OS"."""
    app = _read(APP)
    modes = _string_list(app, "THEME_MODES")

    assert _object_keys(app, "THEME_ICONS") == modes, "a mode has no icon"
    assert _object_keys(app, "THEME_LABELS") == modes, "a mode has no label"
    assert "follow the system" in app, "nothing on screen says the OS is picking"

    bar = _app_bar()
    assert 'v-for="mode in THEME_MODES"' in bar, (
        "the menu items are written out rather than driven from the vocabulary, so "
        "a fourth state (or a dropped one) leaves the control disagreeing with it"
    )
    assert '@click="themeMode = mode"' in bar, "the items pick nothing"

    # Icons stay bundled SVG paths. The webfont set is what a LAN with no route out
    # renders as three empty boxes — test_platform_web_app owns the rule; this is
    # the one control added since, and the menu is where it would be broken.
    assert "@mdi/font" not in app
    for icon in ("mdiThemeLightDark", "mdiWeatherSunny", "mdiWeatherNight"):
        assert icon in app, f"{icon} is not imported from @mdi/js"


def test_the_scheme_is_the_apps_and_reachable_from_every_view():
    """In the app bar, not in a run: the operator's screen does not change when
    they open a run, and a preference behind one view is one they cannot find from
    the other two."""
    bar = _app_bar()

    assert "<v-menu" in bar, "the switcher is not in the app bar"
    assert "THEME_ICONS[themeMode]" in bar, "the button does not show the state"
    # Before the fleet-only block, which is the difference between "always there"
    # and "there on the landing screen": the bar's other two controls are behind
    # `view === 'fleet'`, and a switcher inside that template is unreachable from a
    # run detail view and from the spawn form.
    assert bar.index("<v-menu") < bar.index("""<template v-if="view === 'fleet'">"""), (
        "the switcher sits inside the fleet-only block, so it disappears the "
        "moment a run is opened"
    )

    # And nowhere else: a second control writing the same key is two answers to one
    # question, and the one not on screen wins on the next reload.
    key = _string_constant(_read(APP), "THEME_STORAGE_KEY")
    others = [
        path for path in sorted((WEB / "src").glob("**/*"))
        if path.is_file() and path != APP and key in _read(path)
    ]
    assert not others, f"{[p.name for p in others]} also store the scheme"


# --- what is remembered ------------------------------------------------------

def test_the_scheme_goes_through_the_shared_preference_rules():
    """Three rules, one place: validate against what exists now, fall back to the
    default, and survive a storage that throws rather than answering. A fourth key
    with its own copy of them is the one that gets one of them wrong."""
    app = _read(APP)

    assert "from './preferences.js'" in app
    assert "storedChoice(" in app and "rememberChoice(" in app
    block = _mode_block()
    assert "try {" not in block, (
        "a second copy of the throw-tolerance, which is what the helper is for"
    )

    key = _string_constant(app, "THEME_STORAGE_KEY")
    assert key.startswith("lmer."), (
        f"THEME_STORAGE_KEY = {key!r} is not namespaced, so it can collide with "
        "anything else served from this origin"
    )
    assert "THEME_STORAGE_KEY" in ALLOWED_STORAGE_KEYS, (
        "the key is not in test_platform_web_app.ALLOWED_STORAGE_KEYS, which is "
        "the list saying what this app is allowed to put in a browser"
    )
    # Named at the access, not handed to a helper: `getItem(key)` inside
    # preferences.js would make that allowlist blind. Every key the shell reaches for
    # has to be one of the named constants and on the allowlist — which key set the
    # shell is allowed to have at all is pinned in
    # tests/test_platform_web_assistant (it is one line either side of the drawer's
    # deliberately-unremembered open state, so that is where it belongs).
    used = set(re.findall(r"localStorage\.\w+\((\w+)", app))
    assert "THEME_STORAGE_KEY" in used, (
        f"the scheme is not stored under its own named constant — {sorted(used)}"
    )
    assert used <= ALLOWED_STORAGE_KEYS, (
        f"App.vue stores under {sorted(used - ALLOWED_STORAGE_KEYS)}, which is not "
        "on the allowlist that says what this app may put in a browser"
    )


def test_the_choice_is_applied_at_startup_and_written_down_when_it_changes():
    """Either half missing is a preference that does not work: nothing applied is a
    switcher that forgets on every reload, nothing written is one that forgets when
    the tab closes."""
    app = _read(APP)

    assert "const themeMode = ref(storedThemeMode())" in app, (
        "the app does not start on the remembered scheme"
    )
    assert "theme.change(themeMode.value)" in app, (
        "the remembered scheme is never handed to Vuetify, so it is a ref nothing "
        "reads"
    )
    assert re.search(
        r"watch\(themeMode, \(mode\) => \{\n\s+theme\.change\(mode\)\n\s+"
        r"rememberThemeMode\(mode\)\n\s*\}\)", app,
    ), "picking a scheme does not both apply it and remember it"


def test_nothing_is_forced_on_an_operator_who_has_never_picked():
    """``defaultTheme: 'system'`` stays the default rather than being replaced by
    whatever the switcher's first state happens to be — an empty browser store must
    still mean "the OS decides", including on the frame before App.vue exists."""
    main = _read(MAIN)

    assert "defaultTheme: 'system'" in main, (
        "main.js no longer follows the OS by default, so an operator who has never "
        "touched the switcher gets whatever is hardcoded instead"
    )
    assert _string_list(_read(APP), "THEME_MODES")[0] == "system"


# --- the conversation's three grounds (T84) ----------------------------------

#: What the emphasis classes actually paint: Vuetify's own medium-emphasis opacity,
#: and the ink it is applied to — ``theme-on-light``/``theme-on-dark``, picked per
#: colour by ``hasLightForeground``. Confirmed against the installed 4.1.6 while
#: writing this; a bump that moved either would make the numbers below optimistic,
#: which is the one direction that matters for a background under prose.
EMPHASIS = {"light": ("#000000", 0.60), "dark": ("#ffffff", 0.70)}


def _theme_colors(scheme):
    """The ``colors`` map of one named theme in main.js, as ``{name: '#rrggbb'}``."""
    main = _read(MAIN)
    block = re.search(
        rf"const {scheme} = \{{.*?colors: \{{(.*?)\n  \}},", main, re.S,
    )
    assert block, f"main.js has no {scheme} theme with a colors map"
    return dict(re.findall(r"'?([\w-]+)'?:\s*'(#[0-9a-fA-F]{6})'", block.group(1)))


def _luminance(value):
    """WCAG relative luminance of one opaque colour.

    Its own function rather than a closure inside :func:`_contrast`, because "is this
    darker than that" is a question of its own — the terminal's ground is picked
    against the surfaces around it and not against the ink on it.
    """
    channels = [int(value[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92 if channel <= 0.03928
        else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(one, other):
    """WCAG 2.1 contrast ratio between two opaque colours."""
    darker, lighter = sorted((_luminance(one), _luminance(other)))
    return (lighter + 0.05) / (darker + 0.05)


def _over(ink, ground, alpha):
    """The colour a partly transparent ink resolves to over *ground*."""
    mixed = [
        round(alpha * int(ink[index:index + 2], 16)
              + (1 - alpha) * int(ground[index:index + 2], 16))
        for index in (1, 3, 5)
    ]
    return "#%02x%02x%02x" % tuple(mixed)


def test_the_conversation_grounds_are_defined_in_both_schemes():
    """Half a palette is worse than none: the background simply stops existing.

    ``rgb(var(--v-theme-chat-operator))`` is an invalid declaration when the active
    theme has no such colour, so the turn falls back to the card's surface and the
    coding quietly switches itself off — for the operator who forced the *other*
    scheme, which is nobody's screen while the change is being made.
    """
    for scheme in ("light", "dark"):
        colours = _theme_colors(scheme)
        missing = [name for name in CHAT_GROUNDS if name not in colours]
        assert not missing, f"the {scheme} theme defines no {missing}"

        tones = [colours[name] for name in CHAT_GROUNDS]
        assert len(set(tones)) == len(tones), (
            f"the {scheme} theme gives two classes of turn the same tone {tones} — "
            "the coding renders as complete and tells the reader nothing"
        )
        assert colours["surface"] not in tones, (
            f"a {scheme} ground is the card's own surface, so that class of turn is "
            "uncoded while the others are"
        )


def test_the_conversation_grounds_stay_readable_under_body_text():
    """These are backgrounds under prose, which is the whole risk in a tint.

    Checked against the *dimmest* ink the view puts on them rather than against
    full-strength text: every turn carries a medium-emphasis header (who, when,
    which channel), and that is 60% of the text colour in light and 70% in dark. AA
    is 4.5:1 for body text, and it is the floor here rather than the target — the
    tints are a few percent of `primary` or of the theme's own grey, so the real
    figures sit well above it and the assertion is what stops a later "nicer"
    colour from being picked on a calibrated desktop display alone.
    """
    for scheme, (ink, opacity) in EMPHASIS.items():
        colours = _theme_colors(scheme)
        for name in CHAT_GROUNDS:
            ground = colours[name]
            for description, drawn in (
                ("body text", ink),
                ("the turn header", _over(ink, ground, opacity)),
            ):
                ratio = _contrast(drawn, ground)
                assert ratio >= 4.5, (
                    f"{description} on {name} in the {scheme} scheme is "
                    f"{ratio:.2f}:1, under WCAG AA — the coding costs legibility, "
                    "which is the trade this change is not allowed to make"
                )


# --- the row that is not a run: the colour that went away (2026-07-29) --------

def test_the_orchestrator_row_has_no_colour_of_its_own():
    """The tint the operator asked for, and then asked to be rid of.

    One pass gave the uber lmer row a cyan of its own, off the tone ramp so it could
    not be read as a state: ``orchestrator`` for the badge and ``orchestrator-ground``
    for the wash under the whole row. The next pass ended it — "i don't think solid
    background for the uber lmer row is the way -- lets instead do the following -
    give it an orange border in the main list view and in the side list view instead
    of the play icon show the robot icon" — and a border in ``primary`` plus a glyph
    need no palette key at all.

    So this guards the *absence*, in both directions, because both halves fail
    quietly. A colour left in the theme with no consumer is drift waiting to happen:
    the next reader takes it for a decision and paints something with it, which is how
    a rejected treatment comes back. And a component still naming a colour the theme
    no longer defines emits an invalid declaration — a background that is simply not
    there, on whichever screen the reviewer did not open.

    :mod:`tests.test_platform_web_runcard` holds the other end: what the two listings
    mark that row with instead.
    """
    for scheme in ("light", "dark"):
        stale = sorted(
            name for name in _theme_colors(scheme) if name.startswith("orchestrator")
        )
        assert not stale, (
            f"the {scheme} scheme still defines {stale}; the tinted row is gone, and a "
            "palette key with no consumer is the next treatment nobody argued for"
        )

    # Every source but ``main.js``, whose two schemes the loop above reads properly:
    # its header names both deleted keys in prose on purpose, so the next iteration
    # reads why the tint died rather than re-deriving a cyan nobody wants.
    sources = sorted(
        path for glob in SOURCE_GLOBS
        for path in (WEB / "src").rglob(glob) if path != MAIN
    )
    assert sources, "no web sources found; this guard would pass on an empty tree"
    named = sorted(
        path.relative_to(WEB).as_posix()
        for path in sources
        for token in ("orchestrator-ground", "--v-theme-orchestrator",
                      'color="orchestrator"')
        if token in _read(path)
    )
    assert not named, (
        f"{named} still names the deleted orchestrator colour, which renders as an "
        "invalid declaration: the row is marked by nothing at all"
    )


# --- the terminal's own surface (2026-07-29) ----------------------------------

def test_the_terminal_has_a_dark_ground_in_both_schemes():
    """The operator: "i think the terminal background could be darker".

    It was the card's ``surface``, which in the light scheme is white — a ground the
    ANSI colours a harness paints with were never chosen for. So the emulator has a
    surface of its own, and it is dark in *both* schemes: the same bytes have to be
    readable on a phone in daylight and on a dark desk.

    Darker than the darkest thing the app paints around it, which is the point of the
    change and also the reason grey-darken-4 (#212121, the suggestion) is not the
    value: it is *lighter* than this app's own dark background, so taken literally it
    would have made the terminal a pale patch on a dark page.
    """
    for scheme in ("light", "dark"):
        colours = _theme_colors(scheme)
        for name in ("terminal", "on-terminal"):
            assert name in colours, (
                f"the {scheme} theme defines no {name!r}; Terminal.vue reads both "
                "names, and a missing one is an emulator xterm paints black with "
                "invisible text"
            )
        terminal = colours["terminal"]
        for around in ("surface", "background"):
            assert _luminance(terminal) < _luminance(colours[around]), (
                f"the {scheme} scheme's terminal {terminal} is no darker than the "
                f"{around} around it, which is what the operator asked it not to be"
            )
        ink = _contrast(colours["on-terminal"], terminal)
        assert ink >= 7, (
            f"the {scheme} scheme reads the terminal at {ink:.2f}:1; this is a whole "
            "screen of monospace text, so AA is the floor and this is the target"
        )


# --- the inline-code chip -----------------------------------------------------

def test_inline_code_is_a_chip_you_can_see_on_a_card():
    """The papercut, and the direction the fix had to go.

    ``style.css`` paints every ``code`` element on the theme's ``code`` colour, and
    most of them are on a card: a target, a path, a session id in a run's details.
    The light scheme's card is white, so a *paler* chip has nowhere left to go — the
    old one sat at 1.12:1 against it, which is a background that renders and says
    nothing. Deeper is the only direction, and the floors are per scheme because the
    dark one has not been through this yet (see ``CODE_ON_PAGE``).
    """
    for scheme, floor in CODE_ON_PAGE.items():
        colours = _theme_colors(scheme)
        for name in CODE_SURFACES["page"]:
            ratio = _contrast(colours["code"], colours[name])
            assert ratio >= floor, (
                f"an inline-code chip on the {scheme} scheme's {name} is "
                f"{ratio:.2f}:1, under {floor}:1 — a chip that faint is a "
                "background the operator cannot tell from the thing behind it"
            )


def test_inline_code_stays_a_step_off_the_conversations_own_grounds():
    """The other half: rendered markdown puts the same chip inside a turn.

    Which is the case a card-only check would miss entirely — the grounds are
    tints of their own, so a chip picked against white alone can land on one of
    them and disappear. And a warm palette is a decision that has to hold here too:
    a cool chip is the one patch of a different palette in a pane of warm greys,
    which is what the old light value was.
    """
    for scheme, floor in CODE_ON_GROUND.items():
        colours = _theme_colors(scheme)
        for name in CODE_SURFACES["ground"]:
            ratio = _contrast(colours["code"], colours[name])
            assert ratio >= floor, (
                f"an inline-code chip on the {scheme} scheme's {name} is "
                f"{ratio:.2f}:1, under {floor}:1 — inside a rendered turn it is "
                "the same colour as the turn"
            )

        red, green, blue = (int(colours["code"][at:at + 2], 16) for at in (1, 3, 5))
        assert red > blue and green >= blue, (
            f"the {scheme} scheme's code chip {colours['code']} is not warm; the "
            "greys in this palette are neutral-to-warm on purpose (main.js), and a "
            "cool chip reads as a piece of another app"
        )

        # And the ink on it still clears AA with room to spare: the chip moved, so
        # the pair it makes with its own text had to move with it.
        ink = _contrast(colours["on-code"], colours["code"])
        assert ink >= 7, (
            f"the {scheme} scheme reads code at {ink:.2f}:1, which is a chip that "
            "gained a background by losing its text"
        )


# --- the lever ---------------------------------------------------------------

def test_forcing_a_scheme_drives_vuetifys_own_theme():
    """Which is what repaints the terminal, and the reason a route of our own would
    be a rendering bug.

    xterm is not styled by CSS: ``Terminal.vue`` watches
    ``theme.current.value.dark`` and hands the emulator a new palette. Anything
    that changes the app's colours *without* going through Vuetify's theme — a
    class on the root, a ``color-scheme`` of our own, a second stylesheet — leaves
    that watcher silent and the terminal in the old scheme.
    """
    app = _read(APP)

    assert "useTheme" in app, "App.vue does not reach Vuetify's theme at all"
    assert "theme.change(" in app, "the switcher does not change Vuetify's theme"
    # Deprecated in Vuetify 4 (it warns), and unreadable: `theme.name` resolves
    # 'system' down to light or dark, so what was chosen cannot be read back out.
    # The assignment, not the words: the comment above the lever in App.vue names
    # this path in order to explain why it is not taken.
    assert not re.search(r"global\.name(\.value)?\s*=[^=]", app), (
        "assigning theme.global.name is deprecated in Vuetify 4 and cannot express "
        "'system' — theme.change() is the lever"
    )
    # The switcher must not paint anything itself. `color-scheme` for the pre-mount
    # frame is index.html's job precisely because the mounted app has a theme.
    assert "colorScheme" not in app and "documentElement" not in app, (
        "App.vue paints outside the theme, so the terminal is not told"
    )
    assert not re.search(r"#[0-9a-fA-F]{6}", app), "a colour outside the theme"

    # The other side of the seam, read and not touched (Terminal.vue is not this
    # slice's file): if the watcher moves off `theme.current`, the mechanism above
    # stops being the one that matters.
    terminal = _read(TERMINAL)
    assert "watch(() => theme.current.value.dark" in terminal, (
        "Terminal.vue no longer watches the theme's own dark flag, so forcing a "
        "scheme leaves the emulator in the previous palette"
    )


def test_the_lever_is_the_one_the_installed_vuetify_actually_offers():
    """Read out of ``node_modules``, because this is the assumption the feature
    rests on and a Vuetify bump is where it would stop holding.

    Two properties, both confirmed against 4.1.6 while writing this:
    ``change()`` is the only entry point that accepts ``'system'`` as a name, and
    ``current`` — the ref the terminal watches — is derived from the name that
    ``change()`` sets.
    """
    theme_js = VUETIFY / "lib" / "composables" / "theme.js"
    types = VUETIFY / "lib" / "composables" / "theme.d.ts"
    if not theme_js.is_file():
        pytest.skip("vuetify is not installed (run `lmer platform setup-ui`)")

    version = json.loads(_read(VUETIFY / "package.json"))["version"]
    declared = _read(types)
    source = _read(theme_js)

    assert "'system'" in declared, (
        f"vuetify {version} no longer declares 'system' as a theme name: the "
        "switcher's third state is not the framework's any more"
    )
    assert re.search(r"\bchange:", declared), (
        f"vuetify {version} has no theme.change(); re-confirm how a forced scheme "
        "is expressed before trusting App.vue"
    )
    assert "themeName !== 'system'" in source, (
        f"vuetify {version}'s change() no longer treats 'system' as a name it "
        "accepts, so theme.change('system') warns and does nothing"
    )
    derived = r"current = toRef\(\(\) => computedThemes\.value\[name\.value\]"
    assert re.search(derived, source), (
        f"in vuetify {version}, theme.current is no longer derived from the name "
        "change() sets — the terminal's repaint watcher is no longer driven by the "
        "switcher"
    )


# --- the frame before the app exists -----------------------------------------

def test_the_first_paint_is_decided_before_the_bundle_loads():
    """Vuetify's theme CSS is injected by JS, so the pre-mount frame is the
    browser's: ``color-scheme: light dark`` in style.css hands it the OS's choice.
    Right for an operator following the OS, and a dark flash before a forced light
    theme for one who is not — so the stored preference has to be read ahead of the
    bundle, which means inline and in index.html.
    """
    script = _inline_script()
    css = _read(STYLE)

    assert "localStorage.getItem(" in script, (
        "the inline script reads no preference, so the forced scheme still starts "
        "with a frame of the other one"
    )
    assert "colorScheme" in script, "nothing decides the pre-mount frame"
    assert _read(INDEX).index("<script>") < _read(INDEX).index('src="/src/main.js"'), (
        "the first-paint script runs after the bundle, which is the flash it "
        "exists to remove"
    )
    # An inline script is not an external one — test_platform_web_app forbids
    # `src="http"`, and this adds no src at all.
    assert "src=" not in script

    # And the script still writes no colour of its own. It moves one: the value it
    # copies onto the chrome metas is one of the two already in the markup, which
    # the test below pins to the theme. A literal here would be a third copy, and
    # one the theme cannot change.
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(", script), (
        "the first-paint script paints a colour of its own; the palette lives in "
        "src/main.js, `color-scheme` is what delegates the one frame, and the two "
        "chrome values are the metas'"
    )
    assert "color-scheme: light dark" in css, (
        "style.css no longer delegates the pre-mount frame to the browser, so an "
        "operator following the OS is back to a white flash on a dark phone"
    )
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", css), (
        "the switcher brought colour back into style.css — the theme in main.js "
        "owns it, and a literal here cannot be changed by editing the theme"
    )


def test_the_inline_script_and_the_app_agree_on_what_is_stored():
    """The one seam nothing else can hold. index.html runs before the bundle exists,
    so it cannot import App.vue's constants — a renamed key or a renamed state
    leaves a first-paint script reading a key nobody writes, which fails as a flash
    on somebody else's phone and nowhere else.
    """
    script = _inline_script()
    app = _read(APP)
    key = _string_constant(app, "THEME_STORAGE_KEY")
    modes = _string_list(app, "THEME_MODES")

    stored = re.search(r"localStorage\.getItem\('([^']*)'\)", script)
    assert stored, "the inline script's key is not a plain literal this can read"
    assert stored.group(1) == key, (
        f"index.html reads {stored.group(1)!r} while App.vue writes {key!r}"
    )

    words = set(re.findall(r"mode === '([^']*)'", script))
    assert words, "the inline script compares against no state at all"
    assert words <= set(modes), (
        f"index.html knows about {sorted(words - set(modes))}, which App.vue does "
        f"not offer (its states are {modes})"
    )
    # The forced ones, and only those: 'system' is the absence of a declaration,
    # which is what lets the browser keep following the OS.
    assert words == {"light", "dark"}, (
        f"the inline script forces {sorted(words)}; following the OS must leave "
        "`color-scheme` alone rather than pinning it to a scheme"
    )


# --- the chrome around the frame ----------------------------------------------

def test_the_chrome_metas_are_the_themes_own_backgrounds():
    """The two hexes index.html is allowed to hold, and what licenses them.

    A phone paints a strip of its own UI above and below the page, and it takes
    the colour from these metas — out of the markup, before a script has run, so
    there is nothing to import a palette from. That is the same bind the storage
    key is in and it gets the same answer: restate it, and pin the restatement.
    The pin is not a formality. Unpinned, these two drifted into a cool grey pair
    that belonged to no scheme this app has, under a comment claiming they were
    the theme's — which is exactly the failure a duplicated value fails as, since
    the only screen it shows up on is a phone's.
    """
    metas = _chrome_by_scheme()

    assert set(metas) == {"light", "dark"}, (
        f"index.html paints the browser's chrome for {sorted(metas)}; both schemes "
        "are first-class (spec §10.1), and a scheme with no meta is a strip of the "
        "other one's colour around the app"
    )
    for scheme, content in metas.items():
        background = _theme_colors(scheme)["background"]
        assert content == background, (
            f"the {scheme} chrome is {content} while the {scheme} theme's "
            f"background is {background} — the phone frames the app in a colour the "
            "app never paints"
        )

    # And only those two. Any other literal in this file is a colour with no pin on
    # it, in the one file that cannot import the palette.
    hexes = re.findall(r"#[0-9a-fA-F]{3,8}\b", _read(INDEX))
    assert sorted(hexes) == sorted(metas.values()), (
        f"index.html holds {sorted(hexes)}; the chrome pair is the whole of the "
        "duplication this file is allowed"
    )


def test_the_mounted_app_repaints_the_chrome_from_the_theme():
    """The metas answer prefers-color-scheme, which stops being the answer twice.

    Once for an operator who forced a scheme — covered before the app exists by the
    inline script, and executed below — and again every time the switcher is used,
    which no media query hears at all. So main.js follows the theme it is painting
    with: ``current`` is the resolved theme, 'system' included, and it re-resolves
    when the OS flips at sunset, which the metas would otherwise handle for a
    window that has been open since morning.

    And it has to stay out of the way until then. This module runs before App.vue's
    setup, so at mount the theme is still the default: painting from it there would
    lay the OS's colour back over what index.html had just decided, which is the
    flash the inline script exists to remove, one line further out.

    Executed, and against the *real* metas: a watcher that stopped firing, one that
    wrote to the wrong element, and one that paints too early all read the same in
    source as one that works.
    """
    seen = _chrome_probe()
    untouched = [meta["content"] for meta in _chrome_metas()]
    dark = _theme_colors("dark")["background"]

    assert seen["mounted"] == untouched, (
        f"mounting repainted the chrome to {seen['mounted']} from a theme that has "
        "not been told the operator's preference yet — index.html already decided "
        "that frame, and this is it being undone"
    )
    assert seen["switched"] == [dark] * len(untouched), (
        f"switching the theme left the chrome at {seen['switched']} — the app "
        "repaints and the phone's own UI stays in the previous scheme"
    )

    # The premise, read out of node_modules like the lever's: the instance main.js
    # holds is the one carrying that theme.
    framework = VUETIFY / "lib" / "framework.d.ts"
    if not framework.is_file():
        pytest.skip("vuetify is not installed (run `lmer platform setup-ui`)")
    version = json.loads(_read(VUETIFY / "package.json"))["version"]
    assert re.search(r"theme: ThemeInstance", _read(framework)), (
        f"createVuetify() in vuetify {version} no longer hands back the theme, so "
        "main.js is watching something else"
    )


# --- executed: a stored value that means nothing, and the frame it paints -----

#: One Node run over both halves — App.vue's own block and index.html's own script
#: — against a storage that is by turns empty, stale, hostile and broken. The
#: assertions are in Python; the JS only observes and reports.
_PROBE = """
// A localStorage that can be swapped, and can refuse the way real ones refuse.
let store = null
globalThis.window = {
  get localStorage() {
    if (!store) throw new Error('access denied (cookies blocked)')
    return store
  },
}
const plain = (entries, faults = {}) => ({
  entries,
  getItem: (key) => {
    if (faults.read) throw new Error('read denied')
    return key in entries ? entries[key] : null
  },
  setItem: (key, value) => {
    if (faults.write) throw new Error('quota exceeded')
    entries[key] = String(value)
  },
})

// What the pre-mount frame is allowed to touch, recorded rather than assumed —
// the root's style, and the chrome metas, which are index.html's own, copied
// fresh for each case so one run cannot inherit the previous one's answer.
let painted = {}
let metas = []
globalThis.document = {
  documentElement: {
    style: new Proxy({}, {
      set(target, prop, value) {
        painted[prop] = value
        return true
      },
    }),
  },
  querySelectorAll: (selector) => {
    if (selector !== 'meta[name="theme-color"]') {
      throw new Error(`the first-paint script looked for ${selector}`)
    }
    return metas
  },
}

%(BLOCK)s

const firstPaint = () => { %(SCRIPT)s }

const KEY = %(KEY)s
const CHROME = %(METAS)s
const seen = { paint: {}, chrome: {} }

// Nothing stored yet: the first glance from a browser this operator has not used.
store = plain({})
seen.fresh = storedThemeMode()

// Their choice, read back the way the next reload reads it.
rememberThemeMode('dark')
seen.written = { ...store.entries }
seen.reopened = storedThemeMode()

// A state this build does not have: renamed here, or written by another version of
// the app against the same origin.
store = plain({ [KEY]: 'sepia' })
seen.stale = storedThemeMode()
store = plain({ [KEY]: '' })
seen.blank = storedThemeMode()

// Storage that throws instead of answering: private browsing, some webviews.
store = plain({}, { read: true })
seen.readThrows = storedThemeMode()
store = null
seen.accessThrows = storedThemeMode()

// A value the app could not read back must not be stored at all, and a storage
// that refuses the write must not reach the operator as an error.
store = plain({})
rememberThemeMode('sepia')
seen.refused = { ...store.entries }
store = plain({}, { write: true })
try {
  rememberThemeMode('light')
  seen.writeThrew = false
} catch (exc) {
  seen.writeThrew = exc.message
}

// And the frame before any of that code exists.
for (const [name, entries] of Object.entries({
  fresh: {},
  light: { [KEY]: 'light' },
  dark: { [KEY]: 'dark' },
  system: { [KEY]: 'system' },
  stale: { [KEY]: 'sepia' },
})) {
  store = plain(entries)
  painted = {}
  metas = CHROME.map((meta) => ({ ...meta }))
  firstPaint()
  seen.paint[name] = { ...painted }
  seen.chrome[name] = metas.map((meta) => meta.content)
}
store = plain({}, { read: true })
painted = {}
metas = CHROME.map((meta) => ({ ...meta }))
try {
  firstPaint()
  seen.paint.readThrows = { ...painted }
  seen.chrome.readThrows = metas.map((meta) => meta.content)
} catch (exc) {
  seen.paint.readThrows = exc.message
}
store = null
painted = {}
metas = CHROME.map((meta) => ({ ...meta }))
try {
  firstPaint()
  seen.paint.accessThrows = { ...painted }
  seen.chrome.accessThrows = metas.map((meta) => meta.content)
} catch (exc) {
  seen.paint.accessThrows = exc.message
}

console.log(JSON.stringify(seen))
"""


#: The other executed half: main.js's watcher, handed a theme by hand and pointed
#: at index.html's real metas. Vuetify itself is not started — the framework's side
#: of that seam is read out of node_modules by the test — so what this observes is
#: the block's own behaviour: which elements it writes to, whether it writes before
#: anything changes, and whether it is still writing after something did.
_CHROME_PROBE = """
import { nextTick, ref, watch } from 'vue'

const metas = %(METAS)s.map((meta) => ({ ...meta }))
globalThis.document = {
  querySelectorAll: (selector) => {
    if (selector !== 'meta[name="theme-color"]') {
      throw new Error(`the chrome watcher looked for ${selector}`)
    }
    return metas
  },
}

// Vuetify's own shape: `current` is the resolved theme — 'system' already turned
// into one of the two — and the colours on it are the palette's own strings
// (composables/theme.js).
const current = ref({ colors: { background: %(LIGHT)s } })
const vuetify = { theme: { current } }

%(BLOCK)s

const seen = { mounted: metas.map((meta) => meta.content) }

// The switcher, from this side of the seam: theme.change() resolves to a different
// theme object, and that is the whole of what reaches this block.
current.value = { colors: { background: %(DARK)s } }
await nextTick()
seen.switched = metas.map((meta) => meta.content)

console.log(JSON.stringify(seen))
"""


def _node_report(script, what):
    """Run one probe under Node from the web directory and read back its report.

    Missing Node skips, unless the host says it has one
    (:func:`tests.conftest.require_node_toolchain`): a guard that can be satisfied
    by not running is the bug that helper exists to catch.
    """
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")

    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(WEB), capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"the {what} probe failed:\n{result.stdout}\n{result.stderr}"
    )
    return json.loads(result.stdout)


def _probe():
    """Run both real sources under Node and report what they did."""
    helpers = json.dumps(str(WEB / "src" / "preferences.js"))
    script = "\n".join([
        f"import {{ rememberChoice, storedChoice }} from {helpers}",
        _PROBE % {
            "BLOCK": _mode_block(),
            "SCRIPT": _inline_script(),
            # App.vue's key, so the probe *also* fails when index.html reads
            # another one: the first-paint half below would see nothing stored.
            "KEY": json.dumps(_string_constant(_read(APP), "THEME_STORAGE_KEY")),
            # index.html's own metas, so what the script copies is the real pair
            # rather than a fixture that cannot drift with them.
            "METAS": json.dumps(_chrome_metas()),
        },
    ])
    return _node_report(script, "theme")


def _chrome_probe():
    """Run main.js's chrome watcher under Node and report what it painted."""
    script = _CHROME_PROBE % {
        "BLOCK": _chrome_block(),
        "METAS": json.dumps(_chrome_metas()),
        "LIGHT": json.dumps(_theme_colors("light")["background"]),
        "DARK": json.dumps(_theme_colors("dark")["background"]),
    }
    return _node_report(script, "chrome")


def test_a_first_glance_follows_the_os():
    """Nothing stored is not a missing preference, it is the default — and the
    default is the behaviour this UI already had."""
    assert _probe()["fresh"] == "system"


def test_the_choice_survives_a_reload():
    seen = _probe()
    key = _string_constant(_read(APP), "THEME_STORAGE_KEY")

    assert seen["reopened"] == "dark", f"a second visit renders {seen['reopened']}"
    assert seen["written"] == {key: "dark"}, f"stored as {seen['written']}"


def test_a_scheme_that_no_longer_exists_falls_back_to_the_os():
    """The failure the validation exists for. A mode that names no theme is not a
    forgotten preference: ``theme.change()`` warns and keeps the previous scheme,
    so the app would render one thing while the switcher claimed another.
    """
    seen = _probe()

    assert seen["stale"] == "system", (
        f"a state this build does not have selected {seen['stale']}"
    )
    assert seen["blank"] == "system", f"an empty stored value selected {seen['blank']}"


def test_a_storage_that_refuses_is_not_a_broken_app_bar():
    """Private browsing and some embedded webviews throw on access rather than
    answering, and a full or disabled store throws on write. A scheme is never
    worth failing to render over, and never worth an error to read."""
    seen = _probe()

    assert seen["readThrows"] == "system"
    assert seen["accessThrows"] == "system"
    assert seen["writeThrew"] is False, (
        f"a refused write reached the operator: {seen['writeThrew']}"
    )
    assert seen["refused"] == {}, (
        f"a state this build does not have was stored anyway: {seen['refused']}"
    )


def test_the_pre_mount_frame_matches_the_scheme_that_is_about_to_render():
    """The flash, and the three answers it has.

    A forced scheme has to be on the very first frame, because that frame is
    painted before Vuetify's stylesheet exists. Following the OS has to leave it
    alone — pinning `color-scheme` to whatever the OS said at load would freeze it
    for a window that is still open when the OS flips at sunset.
    """
    paint = _probe()["paint"]

    assert paint["light"] == {"colorScheme": "light"}, (
        f"a forced light theme starts on {paint['light']}, so it flashes the "
        "other scheme on a dark phone"
    )
    assert paint["dark"] == {"colorScheme": "dark"}
    assert paint["system"] == {}, (
        f"following the OS pins the frame to {paint['system']} instead of leaving "
        "the browser to it"
    )
    assert paint["fresh"] == {}, "an operator who has never picked gets a forced frame"
    assert paint["stale"] == {}, (
        f"a stored state this build does not have painted {paint['stale']}, while "
        "the app itself falls back to the OS — the flash is the disagreement"
    )
    # Same tolerance as everything else that reads a browser's storage: an
    # exception here is thrown before the bundle loads, i.e. a blank page.
    assert paint["readThrows"] == {}, (
        f"a storage that refuses stopped the first-paint script: {paint['readThrows']}"
    )
    assert paint["accessThrows"] == {}, (
        f"a storage that cannot be reached stopped the first-paint script: "
        f"{paint['accessThrows']}"
    )


def test_the_chrome_on_that_frame_follows_the_forced_scheme_and_nothing_else():
    """One line further out than the page: the strip the phone paints itself.

    The metas answer prefers-color-scheme, so on a forced scheme they answer the
    wrong question — a dark phone frames a forced light app in black until the
    bundle lands. The script copies that scheme's own meta value onto both, which
    is why no colour is written here and why the pair has to be pinned to the theme
    (above): what it moves is one of those two values.

    And only for a forced scheme. Following the OS means the metas keep their media
    queries — a scheme resolved once at load is one that stops being right at
    sunset, which is exactly the mistake the ``color-scheme`` half avoids.
    """
    chrome = _probe()["chrome"]
    metas = _chrome_by_scheme()
    untouched = [meta["content"] for meta in _chrome_metas()]

    for scheme in ("light", "dark"):
        assert chrome[scheme] == [metas[scheme]] * len(untouched), (
            f"a forced {scheme} theme leaves the chrome at {chrome[scheme]}; both "
            f"metas have to read {metas[scheme]} or the phone paints the strip "
            "around the app in the scheme the OS asked for"
        )
    for name in ("system", "fresh", "stale"):
        assert chrome[name] == untouched, (
            f"the {name} case rewrote the chrome to {chrome[name]}; with no forced "
            "scheme the media queries are the right answer, and one resolved at "
            "load is frozen for a window that is still open at sunset"
        )
    for name in ("readThrows", "accessThrows"):
        assert chrome[name] == untouched, (
            f"a storage that refuses left the chrome at {chrome[name]}"
        )
