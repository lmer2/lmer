"""Guards on the control UI's source (issue #141, slice M2 / T9).

There is deliberately no JS test runner in this repo, so these are source-level
invariants rather than behavioral tests — the kind that would otherwise only fail
on a phone, at the worst moment:

- nothing is fetched from an external host (the daemon serves everything, and a
  CDN reference would make the UI fail exactly where it is most needed: a LAN with
  no route out)
- build output is never committed (spec D10)
- dependencies are pinned by a lockfile
- the mobile constraint and the scope rule are actually expressed in the markup
- the conversation is bounded and only follows the end when the end is what is
  being read — both of which look fine in a desktop window and neither of which
  is usable on a phone when they are wrong

The UI's *behavior* is verified by building it (`npm run build`, run by
`lmer platform setup-ui`) and by live test LT3 on a real phone.
"""

import inspect
import json
import re
from pathlib import Path

import pytest

from tests.conftest import node_binary, require_node_toolchain

WEB = Path(__file__).resolve().parent.parent / "web"
SOURCES = sorted(WEB.glob("src/**/*")) if WEB.is_dir() else []
APP = WEB / "src" / "App.vue"
RUN_CARD = WEB / "src" / "components" / "RunCard.vue"


def _text_sources():
    return [p for p in SOURCES if p.is_file() and p.suffix in {".js", ".vue", ".css"}]


# --- layout -----------------------------------------------------------------

def test_web_app_exists():
    assert (WEB / "package.json").is_file()
    assert (WEB / "vite.config.js").is_file()
    assert (WEB / "index.html").is_file()
    assert (WEB / "src" / "main.js").is_file()
    assert (WEB / "src" / "App.vue").is_file()


def test_entry_components_are_present():
    for name in ("RunCard.vue", "RunDetail.vue", "AddRun.vue"):
        assert (WEB / "src" / "components" / name).is_file(), f"missing {name}"


# --- self-containedness -----------------------------------------------------

@pytest.mark.parametrize("source", _text_sources(), ids=lambda p: p.name)
def test_no_external_hosts_referenced(source):
    """A CDN reference would break the UI on exactly the networks it is for."""
    text = source.read_text(encoding="utf-8")
    offenders = re.findall(r"https?://[^\s'\"`)]+", text)
    allowed = ("http://127.0.0.1", "http://localhost", "https://git.example.com",
               "https://gitlab.example.com")
    for url in offenders:
        # Interpolated hosts are built from window.location at runtime (a link to
        # a port on the operator's own machine), so they name no fixed host and
        # cannot be a CDN dependency.
        if "${" in url:
            continue
        # Placeholder URLs in form hints and the dev-proxy default are fine; a
        # real dependency host is not.
        assert url.startswith(allowed), f"{source.name} references {url}"


def test_index_html_has_no_external_scripts():
    text = (WEB / "index.html").read_text(encoding="utf-8")
    assert "src=\"http" not in text
    assert "href=\"http" not in text


def test_no_google_fonts_or_font_downloads():
    for source in _text_sources():
        text = source.read_text(encoding="utf-8").lower()
        assert "fonts.googleapis" not in text
        assert "@font-face" not in text, (
            f"{source.name} loads a font file; system fonts only"
        )


def test_icons_are_bundled_svg_paths_not_a_webfont():
    """The same rule one level up: a font *package* would embed the file instead.

    Vuetify's default icon set is the MDI webfont, so choosing ``mdi-svg`` is a
    decision that has to be made and kept — with the font set, every icon on a
    LAN with no route out renders as an empty box.
    """
    payload = json.loads((WEB / "package.json").read_text(encoding="utf-8"))
    declared = set(payload["dependencies"]) | set(payload["devDependencies"])
    assert not [name for name in declared if "font" in name], (
        f"a font package would ship a font file: {sorted(declared)}"
    )
    main = (WEB / "src" / "main.js").read_text(encoding="utf-8")
    assert "iconsets/mdi-svg" in main
    for source in _text_sources():
        for line in source.read_text(encoding="utf-8").splitlines():
            statement = line.strip()
            if not statement.startswith(("import ", "@import")):
                continue
            assert "font" not in statement, (
                f"{source.name} imports a font: {statement}"
            )


# --- dependency hygiene -----------------------------------------------------

def test_dependencies_are_locked():
    lock = WEB / "package-lock.json"
    assert lock.is_file(), "package-lock.json must be committed so npm ci is reproducible"
    payload = json.loads(lock.read_text(encoding="utf-8"))
    assert payload.get("lockfileVersion", 0) >= 2


def test_dependency_set_is_minimal():
    """Every dependency is a maintenance and supply-chain cost; justify each.

    An exact allowlist rather than a count, so adding one stays a deliberate,
    reviewed act instead of something that drifts in with a feature:

    - vue, vite, @vitejs/plugin-vue — the app and its build
    - vuetify — the component library the UI is built from
    - vite-plugin-vuetify — resolves the components each template uses, so the
      bundle carries those rather than the whole framework
    - @mdi/js — SVG icon path data, compiled in (see the webfont test above)
    - @xterm/xterm, @xterm/addon-fit — the session terminal. The current package
      names; the bare `xterm` package is deprecated. Lazy-loaded into their own
      chunk, so the fleet view does not pay for them (see the terminal tests).
    - markdown-it, dompurify — chat renders agent output as markdown (T38), which
      means turning text written inside a container into HTML in the operator's
      browser. Both are here for that one job and the pairing is the point:
      markdown-it parses with `html: false` so a tag never becomes a tag, and
      DOMPurify allowlists what the parser emits before it reaches `v-html`. This
      is the one feature where hand-rolling the dependency away would be the
      riskier choice, not the safer one — see tests/test_platform_web_markdown.py,
      which executes both against hostile input rather than reading the source.
      (T44 moved the renderer out of Chat.vue into one shared component, and the
      executed probes moved with it.)
    """
    payload = json.loads((WEB / "package.json").read_text(encoding="utf-8"))
    assert set(payload["dependencies"]) == {
        "vue", "vuetify", "@mdi/js", "@xterm/xterm", "@xterm/addon-fit",
        "markdown-it", "dompurify",
    }
    assert set(payload["devDependencies"]) == {
        "@vitejs/plugin-vue", "vite", "vite-plugin-vuetify",
    }


def test_rollup_is_the_wasm_build_so_an_older_host_can_still_build():
    """Rollup's native binary needs GLIBC_2.29; RHEL 8 has 2.28.

    The operator's host could fetch Node fine and then died loading
    ``@rollup/rollup-linux-x64-gnu`` — ``/lib64/libm.so.6: version GLIBC_2.29 not
    found``. npm's own advice on that error ("npm has a bug related to optional
    dependencies") is a red herring; rollup 4 has no env-var escape hatch and no
    automatic fallback, and its error text names the one answer: the WASM build.

    So the override is load-bearing infrastructure, not a preference, and it lives
    in *two* places that must agree — the declaration in package.json and the
    resolution in package-lock.json, because ``npm ci`` (which is what
    ``lmer platform setup-ui`` runs) reads only the lock. Regenerating the lock
    without the override silently restores the native binary and breaks that host
    again, with a build that passes everywhere the maintainer is likely to test.

    Cost of the WASM build: ~13s vs ~12s, byte-identical output.
    """
    payload = json.loads((WEB / "package.json").read_text(encoding="utf-8"))
    assert payload.get("overrides", {}).get("rollup") == "npm:@rollup/wasm-node@*", (
        "the rollup override is missing from package.json; hosts on glibc < 2.29 "
        "cannot build the UI without it"
    )

    lock = json.loads((WEB / "package-lock.json").read_text(encoding="utf-8"))
    entry = lock.get("packages", {}).get("node_modules/rollup", {})
    resolved = entry.get("resolved", "")
    assert "wasm-node" in resolved, (
        "package-lock.json resolves rollup to the native build "
        f"({resolved!r}) — npm ci reads the lock, not the override, so this is "
        "what a host actually installs. Regenerate with `npm install` after "
        "confirming the override is in package.json."
    )


def test_build_output_is_not_committed():
    """Spec D10: the host builds the UI; dist/ never enters git."""
    ignored = (WEB / ".gitignore").read_text(encoding="utf-8")
    assert "dist/" in ignored
    assert "node_modules/" in ignored


# --- the constraints the spec puts on this UI -------------------------------

def test_viewport_meta_is_present():
    """"Fully usable in mobile" is a constraint on every view (spec §10.1)."""
    text = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'name="viewport"' in text
    assert "width=device-width" in text


def test_both_color_schemes_follow_the_os():
    """Light and dark are both first-class and the OS picks (spec §10.1).

    Two mechanisms, because they cover different moments: Vuetify's
    ``defaultTheme: 'system'`` reads (and keeps watching) prefers-color-scheme for
    the running app, while ``color-scheme: light dark`` covers the frame before it
    — the theme stylesheet is injected by JS, so a dark phone would otherwise
    flash white before the app mounts.

    style.css used to restate the two background colours for that first frame,
    which meant the palette lived in two files that could drift. It does not any
    more (the operator asked: "the theme should control the colors, i dont like to
    have two places for it"): ``color-scheme`` delegates the one pre-mount frame to
    the browser's own dark canvas, so the guarantee survives with nothing restated.
    """
    main = (WEB / "src" / "main.js").read_text(encoding="utf-8")
    assert "defaultTheme: 'system'" in main
    assert "themes:" in main and "dark" in main

    css = (WEB / "src" / "style.css").read_text(encoding="utf-8")
    assert "color-scheme: light dark" in css


def test_the_stylesheet_restates_no_colour_the_theme_owns():
    """One place owns the palette, and this is what keeps it that way.

    A hex in style.css is a colour the theme cannot change — it survives a theme
    edit, drifts silently, and is exactly the split the operator asked to be rid of.
    The fix for anything that seems to need one here is a theme colour plus
    ``rgb(var(--v-theme-<name>))``, not a literal.
    """
    css = (WEB / "src" / "style.css").read_text(encoding="utf-8")
    hexes = re.findall(r"#[0-9a-fA-F]{3,8}\b", css)
    assert not hexes, (
        f"style.css hardcodes {hexes} — the theme in main.js owns colour, so "
        "these cannot be changed by editing the theme and will drift from it"
    )


def test_wide_content_scrolls_inside_its_own_box():
    """A long target or path must not push the page sideways on a phone."""
    css = (WEB / "src" / "style.css").read_text(encoding="utf-8")
    assert ".scroll-x" in css
    assert "overflow-x: auto" in css


def test_safe_area_insets_are_honored():
    """The notch is a layout constraint, and Vuetify's layout knows nothing of it.

    Its app bar is fixed at top 0 and v-main's offset comes from a CSS variable,
    so the insets have to be added to both — losing this rule puts the bar's
    controls under a phone's status bar.
    """
    css = (WEB / "src" / "style.css").read_text(encoding="utf-8")
    assert "env(safe-area-inset-top)" in css
    assert "env(safe-area-inset-bottom)" in css
    assert ".v-main" in css


def test_the_app_is_wrapped_in_the_vuetify_layout_root():
    """Without <v-app> and the plugin, components render unstyled and unpositioned."""
    main = (WEB / "src" / "main.js").read_text(encoding="utf-8")
    assert "createVuetify" in main
    assert ".use(vuetify)" in main
    app = (WEB / "src" / "App.vue").read_text(encoding="utf-8")
    # The tags, not their whole openings: the app root carries the narrow band's class
    # since #286, and what this is about is the layout root being there at all.
    assert "<v-app" in app
    assert "<v-main" in app


def test_empty_state_explains_the_scope_rule():
    """An empty fleet must not read as a broken one (spec D25)."""
    app = (WEB / "src" / "App.vue").read_text(encoding="utf-8")
    assert "Nothing tracked yet" in app
    assert "never the whole shared work repo" in app


def test_mirror_staleness_is_surfaced():
    """Spec R20: a stale mirror must be visible, not silently wrong."""
    app = (WEB / "src" / "App.vue").read_text(encoding="utf-8")
    assert "mirrorProblem" in app
    assert "stale" in app.lower()


def test_attention_reasons_cover_the_inventory_vocabulary():
    """The UI must not render a raw enum value for a reason the backend can emit."""
    from lmer_platform.inventory import ATTENTION_REASONS

    text = (WEB / "src" / "format.js").read_text(encoding="utf-8")
    for reason in ATTENTION_REASONS:
        assert f"{reason}:" in text, f"format.js has no label for {reason!r}"


def test_run_states_cover_the_inventory_vocabulary():
    from lmer_platform.inventory import RUN_STATES

    text = (WEB / "src" / "format.js").read_text(encoding="utf-8")
    for state in RUN_STATES:
        assert f"{state}:" in text, f"format.js has no label for state {state!r}"


def test_every_tone_maps_to_a_colour_the_theme_defines():
    """A tone pointing at a colour no theme defines renders as an unstyled chip.

    That is worse than a wrong colour: it reads as "nothing to see here" for a run
    that may be the one waiting on you. format.js names the colours, main.js's
    theme is what has to define them — this is the seam between the two.
    """
    text = (WEB / "src" / "format.js").read_text(encoding="utf-8")
    block = re.search(r"const TONE_COLORS = \{(.*?)\}", text, re.S)
    assert block, "format.js no longer maps tones to colours"
    colours = re.findall(r":\s*'([^']+)'", block.group(1))
    assert len(colours) >= 5, f"only {len(colours)} tones mapped"

    main = (WEB / "src" / "main.js").read_text(encoding="utf-8")
    for colour in colours:
        # Quoted or bare, depending on whether the name needs quoting in JS.
        assert f"{colour}:" in main or f"'{colour}':" in main, (
            f"theme in main.js defines no {colour!r} colour"
        )


# --- the conversation on a phone --------------------------------------------
#
# The chat view is a tab panel now rather than the bottom of the page, and
# everything in it is unbounded: a transcript runs to hundreds of turns and any
# one of them can be a pasted file or a wall of tool output. Each of these reads
# fine in a tall desktop window, which is the one place this is never used.

CHAT = WEB / "src" / "components" / "Chat.vue"


def _chat_source():
    return CHAT.read_text(encoding="utf-8")


def _chat_rule(selector):
    """One declaration block from Chat.vue's scoped stylesheet."""
    style = _chat_source()
    style = style[style.index("<style"):]
    block = style[style.index(f"{selector} {{"):]
    return block[:block.index("}")]


def _chat_function(signature):
    """Source of one top-level function in Chat.vue's ``<script setup>``.

    Same shape as the helper in :mod:`tests.test_platform_web_terminal`: a ``}``
    in column zero ends a top-level function, and locality is the thing being
    asserted — which function a decision is made in.
    """
    text = _chat_source()
    start = text.index(signature)
    return text[start:text.index("\n}\n", start)]


def test_the_conversation_scrolls_inside_its_own_box():
    """Unbounded, a long transcript pushes the composer and the rest of the page
    somewhere no thumb will reach.

    The bound is a share of the viewport rather than a pixel count — a pixel count
    is a letterbox on a desktop and still taller than a phone — and a maximum
    rather than a height, so a three-turn conversation stays a short card instead
    of a mostly empty one.
    """
    assert 'ref="scroller"' in _chat_source(), "nothing can be scrolled to"
    rule = _chat_rule(".chat")
    assert "overflow-y: auto" in rule, "the conversation grows the page instead"
    assert "max-height" in rule, "the conversation is unbounded"
    assert "dvh" in rule, (
        "a pixel bound wastes a desktop and still overflows a phone; dvh also "
        "notices the chrome a phone browser hides as you scroll"
    )


def test_one_huge_message_cannot_push_the_rest_out_of_reach():
    """Tool output and pasted files are routine in these transcripts, so a single
    turn taller than the pane it sits in is the common case, not the edge one."""
    rule = _chat_rule(".said")
    assert "max-height" in rule and "overflow-y: auto" in rule, (
        "one message stretches the conversation box instead of scrolling"
    )


def test_the_conversation_follows_the_end_only_while_the_end_is_being_read():
    """Otherwise reading back through history is impossible on a phone: every
    poll — one every five seconds — yanks the page to the newest turn.

    The condition is remembered rather than measured when it is needed, because
    the pane is a tab panel: hiding one destroys its scroll box, and showing it
    again hands back a fresh one at the top, which is indistinguishable from a
    reader who has scrolled up.
    """
    text = _chat_source()
    absorb = _chat_function("function absorb(page")
    assert "if (follow) nextTick(stickToBottom)" in absorb, (
        "new turns do not scroll into view at all"
    )
    decision = re.search(r"const follow = ([^\n]+)", absorb)
    assert decision, "the scroll is not a decision"
    assert "following.value" in decision.group(1), (
        "the poll scrolls to the bottom wherever the reader happens to be"
    )
    assert "'prepend'" not in decision.group(1), (
        "loading older turns scrolls away from the ones just loaded"
    )

    assert '@scroll="onScroll"' in text, "nothing notices the reader scrolling"
    assert "following.value = atBottom()" in _chat_function("function onScroll")
    bottom = _chat_function("function atBottom")
    assert "scrollHeight" in bottom and "clientHeight" in bottom, (
        "at-the-bottom is not measured from the box"
    )

    earlier = _chat_function("async function loadEarlier")
    assert "following.value = false" in earlier, (
        "asking for older turns and then being scrolled back to the newest is "
        "the same bug arriving from the other direction"
    )


def test_api_client_uses_relative_paths():
    """The app is served by the daemon it talks to; absolute URLs would break
    behind a reverse proxy on a subpath."""
    text = (WEB / "src" / "api.js").read_text(encoding="utf-8")
    assert "request('api/state')" in text
    assert "'/api/state'" not in text


#: Every browser-storage key this UI is allowed to write, and what it is for.
#: Adding one is meant to be a deliberate, reviewed act — the same reason the
#: dependency set is an exact allowlist rather than a count.
ALLOWED_STORAGE_KEYS = {
    # x1/x1.5/x2 terminal height: a property of the operator's screen, not of any
    # session, and re-picking it on every run is the annoyance it exists to remove.
    "HEIGHT_STORAGE_KEY",
    # Whether the terminal fits the session to this screen. Same reasoning, plus
    # one of its own: it is turned off to watch a session somebody else is driving,
    # and coming back on for the next one reflows that person's terminal.
    "FIT_STORAGE_KEY",
    # Which run-detail tab (overview / meta / lmer / exit) and which of the lmer
    # panes (conversation / terminal / operator chat) this operator reads runs in.
    # Global rather than per run: the preference is "I read the terminal", not
    # "this run opens on the terminal" (T49; the meta tab arrived with T52).
    "RUN_TAB_STORAGE_KEY",
    "RUN_PANE_STORAGE_KEY",
    # Which colour scheme this operator forces — or 'system', the default, meaning
    # the OS still picks. Global like the two above, because a scheme is a property
    # of the screen being read from and not of any run (T62). Its own guards, and
    # the agreement with the inline first-paint script in web/index.html, are in
    # tests/test_platform_web_theme.py.
    "THEME_STORAGE_KEY",
    # Whether the whole app is held in a band in the middle of the screen instead of
    # spread across it (#286) — the effect of narrowing the browser window, for a
    # window that has other tabs in it. A property of the screen being read from, like
    # the three above, and it costs nothing to remember: no panel is mounted and no
    # request is made by the answer being "on". The drawer's own open state is still
    # deliberately not stored (tests/test_platform_web_assistant.py).
    "NARROW_STORAGE_KEY",
}


def test_no_secret_is_stored_client_side():
    """Auth is the browser's (Basic); a token in JS is a token in the DOM.

    This used to ban ``localStorage`` outright, which was a cheap proxy for the
    real rule while nothing needed storage. A UI preference legitimately does now
    (terminal height, and the remembered tab after it), so the proxy would have
    had to be deleted — and deleting it would have left *nothing* checking that
    the shared secret stays out of the DOM, which is the property that matters:
    the secret can spawn containers, and anything in localStorage is readable by
    any script that gets injected.

    So the guard moved from "no storage" to "only these keys, and nothing
    credential-shaped". ``sessionStorage`` stays banned entirely — nothing needs
    it, and an empty allowlist is the easiest one to keep.
    """
    forbidden = ("secret", "token", "password", "credential", "authorization")

    for source in _text_sources():
        text = source.read_text(encoding="utf-8")
        assert "sessionStorage" not in text, (
            f"{source.name} uses sessionStorage; nothing needs it"
        )
        if "localStorage" not in text:
            continue

        # EVERY occurrence has to be a readable call, not just one of them.
        # Checking "at least one is readable" let `localStorage[KEY]` past while a
        # sibling `setItem` satisfied the assertion — found by mutation.
        call = re.compile(r"localStorage\.(?:get|set|remove)Item\(\s*([A-Za-z0-9_.]+)")
        keys = []
        for match in re.finditer(r"localStorage", text):
            readable = call.match(text, match.start())
            assert readable, (
                f"{source.name} touches localStorage in a shape this guard cannot "
                f"read ({text[match.start():match.start() + 40]!r}) — use "
                "getItem/setItem with a named key constant so what is stored "
                "stays reviewable"
            )
            keys.append(readable.group(1))
        for key in keys:
            assert key in ALLOWED_STORAGE_KEYS, (
                f"{source.name} stores under {key!r}, which is not in "
                f"ALLOWED_STORAGE_KEYS. If it is a preference, add it there with a "
                f"comment saying what it is for; if it is a credential, it must not "
                f"be stored at all."
            )

        # The key constants' own values, so an allowlisted name cannot smuggle a
        # credential-shaped key past the check above.
        for name in ALLOWED_STORAGE_KEYS:
            declared = re.search(rf"const {name} = '([^']*)'", text)
            if declared:
                value = declared.group(1).lower()
                assert not any(word in value for word in forbidden), (
                    f"{source.name}: {name} = {declared.group(1)!r} names something "
                    "credential-shaped"
                )


# --- the fleet's header area, its search and its verbs -----------------------
#
# Three things the operator asked for after a week of reading a live fleet, all on
# this one screen: the worker cap displayed (T97), a search over the list (T100),
# and a one-tap way to get a finished run out of it (T101). Source-level like the
# rest of this file, and the assertions are about the properties that would still
# look right in a screenshot with the behaviour gone: a numerator that counts the
# wrong sessions, a filter matching fields the row does not show, a forget that
# fires before the undo can be reached.


def _app():
    return APP.read_text(encoding="utf-8")


def _script(text):
    """A single-file component's ``<script setup>``, without its markup."""
    return text[:text.index("</script>")]


def _template(text):
    """The markup half, which is where order-of-appearance assertions belong."""
    return text[text.index("<template>"):]


def _binding(name, text=None):
    """Source of one top-level ``const`` in ``App.vue``'s script.

    Same helper as :mod:`tests.test_platform_web_runcard`'s: from the ``const`` to
    whatever starts at column zero after it, because these are arrow bodies that
    end in ``)`` rather than in a brace of their own.
    """
    script = _script(text or _app())
    rest = script[script.index(f"const {name} = "):]
    match = re.search(r"\n(?=(?:const|function|let|//|/\*)\s)", rest)
    return rest[:match.start()] if match else rest


def _function(name, text=None):
    """Source of one top-level function; a ``}`` in column zero ends it."""
    script = _script(text or _app())
    start = script.index(f"function {name}(")
    return script[start:script.index("\n}\n", start)]


def _counts_line(text=None):
    """The dimmed line of fleet counts above the list."""
    template = _template(text or _app())
    start = template.index("{{ totals.runs }} tracked")
    return template[template.rindex("<div", 0, start):template.index("</div>", start)]


def test_the_fleet_says_how_much_of_the_host_is_spent():
    """The cap, displayed (the operator asked: "also display the cap somewhere").

    A cap nobody can see is only met as a refusal, which is the worst place to
    learn a number: the spawn that hits it is the one the operator wanted. So the
    occupancy sits in the counts line the fleet already carries, as the fraction
    it is — a bare "3 workers" says nothing about how close the host is.
    """
    line = _counts_line()
    assert "{{ liveWorkers }}/{{ workerCap }} workers" in line, (
        "the header area no longer shows live workers against the cap, so the only "
        "way to learn the cap is to be refused by it"
    )
    cap = _binding("workerCap")
    assert "config?.max_concurrent_sessions" in cap, (
        "the cap is read from something other than the config block the daemon "
        "serves it in, so the number shown can differ from the number enforced"
    )


def test_the_displayed_cap_is_the_one_the_daemon_serves():
    """One key, spelled in two languages, and no test on either side alone."""
    from lmer_platform import api, config as cfg

    served = api._config_summary(cfg.PlatformConfig(), {"url": None})
    assert "max_concurrent_sessions" in served, (
        "the state payload no longer carries the cap the fleet view reads"
    )
    assert served["max_concurrent_sessions"] == cfg.DEFAULT_MAX_CONCURRENT_SESSIONS


def test_the_occupancy_numerator_is_the_count_the_daemon_would_refuse_from():
    """The one way this display can lie, and it lies by exactly one.

    ``max_concurrent_sessions`` bounds *workers*: the orchestrating session holds
    its own slot beside them, so the daemon's counter skips its kind and the
    refusal a full host answers with is derived from that same count. A client
    counting every live row instead would read 8/8 on a host with room for another
    run — and the operator would then be told there is no room, or be given room
    the display said was gone. Both readings are pinned here, on both sides of the
    language boundary.
    """
    from lmer_platform import inventory, registry, spawn

    counter = inspect.getsource(spawn._live_worker_count)
    assert "ASSISTANT_KIND" in counter, (
        "the daemon's own worker count no longer excludes the orchestrating "
        "session, so this whole test is about the wrong arithmetic"
    )
    assert registry.ASSISTANT_KIND in inspect.getsource(
        inventory.RunView.orchestrator.fget
    ), "the row flag the client counts by is no longer the registry kind"

    binding = _binding("liveWorkers")
    assert "run.live" in binding, (
        "the numerator is not counting live sessions at all"
    )
    assert "!run.orchestrator" in binding, (
        "the orchestrating session is counted against a cap it does not spend, so "
        "the fleet reports a host as full one run early"
    )
    assert "totals" not in binding, (
        "the numerator is the payload's live *rows*, which includes the "
        "orchestrator — the number the daemon enforces is one lower"
    )


def test_a_daemon_that_sends_no_cap_shows_no_occupancy():
    """Absent renders nothing: an older daemon serves a fleet with no cap in it,
    and a fraction over an invented denominator is worse than no fraction."""
    assert 'v-if="workerCap"' in _counts_line(), (
        "the occupancy is drawn by something other than whether there is a cap"
    )
    assert "|| null" in _binding("workerCap"), (
        "a missing cap arrives as undefined rather than null; the value the view "
        "tests is then not the value it renders"
    )


def test_the_list_can_be_searched_over_what_the_row_shows():
    """The operator's ask ("Let me search the runs list"), and its one real trap.

    A filter matching a field the card does not render answers with rows whose
    reason for matching is invisible; one that misses a rendered field is a filter
    an operator stops trusting after the first miss. So the vocabulary is derived
    from the card rather than listed here: every ``run.*`` the row reads has to be
    in the haystack or be named below as deliberately unsearchable.
    """
    text = _app()
    assert re.search(r'<v-text-field[^>]*v-model="query"', text, re.S), (
        "there is no search field over the fleet list"
    )
    template = _template(text)
    assert template.index('v-model="query"') < template.index("<RunCard"), (
        "the search field is below the list it filters"
    )

    #: Row facts a substring search cannot help with, and why. Times are rendered
    #: relative ("3m ago"), so a query never means one; the ledger is a count; and
    #: the session block and the liveness flag are read by the row to *derive* what
    #: it shows — the state word is what carries them into the search.
    unsearchable = {"updated", "session", "ledger", "live"}
    card = re.sub(r"<!--.*?-->", "", RUN_CARD.read_text(encoding="utf-8"), flags=re.S)
    card = re.sub(r"^\s*//.*$", "", card, flags=re.M)
    shown = {match for match in re.findall(r"\brun\.(\w+)", card)} - unsearchable
    haystack = _function("haystack")
    for field in sorted(shown):
        assert f"run.{field}" in haystack, (
            f"the row shows run.{field} and the search cannot see it; add it to the "
            "haystack, or to the unsearchable table above with the reason"
        )
    assert "driverLabel(run)" in haystack, (
        "the harness and model the row draws as a chip are not searchable"
    )
    assert "toLowerCase()" in haystack, "the search is case-sensitive"


def test_the_search_asks_the_daemon_for_nothing():
    """It is a client-side filter and has to stay one.

    The fleet is scoped to the runs this orchestrator tracks (spec D25), so the
    list is small and a pass over it costs nothing — while a search that refetched
    would put a request behind every keystroke, and would go blank the moment the
    daemon was unreachable, which is when the payload on screen is all there is.
    """
    text = _app()
    assert "watch(query" not in text, "typing in the search box triggers a watcher"
    assert not re.search(r"query[^\n]*fetchState|fetchState[^\n]*query", text), (
        "the search is wired to a fleet read"
    )


def test_an_empty_search_renders_the_whole_fleet_and_keeps_the_grouping():
    """Two properties that die together in the same rewrite.

    A filter that treats an empty query as "match nothing" empties the landing
    screen; one that flattens the two sections gives up the only thing this view
    exists to say, in exchange for a text box. So the *same* split is applied to
    the filtered lists, and the attention rows stay first.
    """
    text = _app()
    body = _function("visible", text)
    assert "words.every(" in body, (
        "the filter is not an every-word match, so a two-word query finds nothing"
    )
    assert "words.length ? haystack(run) : ''" in body, (
        "an empty query is not the identity: with no words to match, `every` is "
        "true and the row passes — spell that out rather than paying for a "
        "haystack per row on every render of an unfiltered list"
    )
    for name in ("attentionRows", "calmRows"):
        assert f"const {name} = computed(() => visible(" in text, (
            f"{name} is not the filtered list, so one of the two sections ignores "
            "the search"
        )
    template = _template(text)
    assert template.index("attentionRows") < template.index("calmRows"), (
        "the runs needing a human are no longer first in the results"
    )
    assert 'v-for="run in attention"' not in template, (
        "a section still renders the unfiltered payload list"
    )


def test_a_search_that_matches_nothing_says_so():
    """A tracked fleet with an empty list on screen reads as a lost fleet."""
    text = _app()
    assert 'v-if="nothingMatches"' in _template(text), (
        "nothing tells the operator the search matched nothing, so an empty list "
        "is indistinguishable from a fleet that vanished"
    )
    binding = _binding("nothingMatches", text)
    assert "queryWords.value.length" in binding, (
        "the no-match line can show for a reason other than the search — a run "
        "hidden by a pending forget would draw it too"
    )


# --- forgetting a finished run (T101) ----------------------------------------

def test_a_forget_is_one_tap_with_no_dialog():
    """"quickly" is the requirement, and a confirm is what it rules out.

    The operator asked to "quickly forget completed runs from the list": ten rows
    behind ten confirmations is a chore nobody starts. What replaces the confirm is
    an undo window — so the row goes at once and the dialog never existed.
    """
    text = _app()
    assert '@forget="forget"' in text, "the shell does not listen for the row's verb"
    assert text.count('@forget="forget"') == 2, (
        "only one of the two sections can forget a run"
    )
    for confirming in ("<v-dialog", "window.confirm", "confirm("):
        assert confirming not in text, (
            f"forgetting goes through {confirming}; the undo window is the confirm"
        )


def test_nothing_is_sent_until_the_undo_window_lapses():
    """The whole design: undo is not a rollback, it is the call never happening.

    Forget is cheap to undo only in that sense — the route drops the run from the
    index and takes this orchestrator's title for it with it — so the window has to
    be *before* the request, not a compensating one after it.
    """
    text = _app()
    assert text.count("forgetRun(") == 1, (
        "the forget route is called from more than one place in the shell"
    )
    commit = _function("commitForget", text)
    assert "forgetRun(" in commit, (
        "the request is not made by the timer's own callback, so it is made "
        "somewhere the undo cannot get in front of"
    )
    scheduled = _function("forget", text)
    assert "setTimeout(() => commitForget(key), FORGET_UNDO_MS)" in scheduled, (
        "the forget is not delayed at all, or not by the named window"
    )
    assert "forgetRun" not in scheduled, "the tap itself sends the request"
    undo = _function("undoForget", text)
    assert "clearTimeout" in undo and "forgetRun" not in undo, (
        "undo does not cancel the pending request"
    )
    assert "hiddenKeys.value.filter" in undo, "undo leaves the row hidden"


def test_the_row_goes_at_once_and_comes_back_if_the_forget_is_refused():
    """Optimistic, and therefore reversible in both directions.

    The row is *hidden* rather than removed from anything, which is what makes an
    undo — and a daemon that refuses the call — put it back with nothing to rebuild
    it from. The payload is still the payload; the next poll is what finally drops a
    run that really was forgotten.
    """
    text = _app()
    scheduled = _function("forget", text)
    assert "hiddenKeys.value = [...hiddenKeys.value, key]" in scheduled, (
        "the row does not disappear on the tap, so the one-tap verb reads as a "
        "no-op until the next poll"
    )
    body = _function("visible", text)
    assert "hiddenKeys.value.includes(keyOf(run))" in body, (
        "the hidden rows are not filtered out of the list"
    )
    commit = _function("commitForget", text)
    catch = commit[commit.index("} catch"):]
    assert "hiddenKeys.value.filter" in catch, (
        "a refused forget leaves the row hidden for the life of the tab, which "
        "reads as a run this orchestrator has lost"
    )
    assert "error.value" in catch, "a refused forget says nothing"


def test_each_of_several_rapid_forgets_still_happens():
    """The case this exists for: clearing four finished runs in four taps.

    One shared timer would let the fourth tap cancel the first three, and a single
    pending slot would drop the runs whose notice was overwritten — either way the
    rows come back on the next poll and the operator taps them again.
    """
    text = _app()
    assert "const forgetTimers = new Map()" in text, (
        "the pending forgets do not each have their own timer"
    )
    assert "const pendingForgets = ref([])" in text, (
        "only one forget can be pending at a time"
    )
    scheduled = _function("forget", text)
    assert "if (forgetTimers.has(key)) return" in scheduled, (
        "a second tap on the same row schedules a second forget for it"
    )
    assert "...pendingForgets.value" in scheduled, (
        "a new forget replaces the pending list rather than joining it"
    )
    commit = _function("commitForget", text)
    assert "forgetTimers.delete(key)" in commit, "the timer map grows forever"


def test_the_undo_bar_says_what_forgetting_costs():
    """It is the only place this is said, and it is said while it can still be
    undone.

    Forgetting is not deleting: tracking ends, the run dir and the work-repo state
    are untouched, and adopting the run brings it back. An operator who does not
    know that either never uses the verb or uses it expecting a cleanup it does not
    do.
    """
    text = _app()
    notice = _binding("forgetNotice", text)
    assert "untouched" in notice and "adopting it brings it back" in notice, (
        f"the bar does not say what forgetting leaves alone: {notice!r}"
    )
    bar = re.search(r"<v-snackbar\b.*?</v-snackbar>", _template(text), re.S)
    assert bar, "nothing offers the undo"
    assert ':timeout="-1"' in bar.group(0), (
        "the bar closes on Vuetify's own timeout, which is not the undo window — "
        "the operator would lose the undo while the request was still unsent"
    )
    assert "undoForget" in bar.group(0), "the bar has no undo"
    assert "{{ undoLabel }}" in bar.group(0), (
        "the button cannot say that it takes back more than one row"
    )
    assert "pendingForgets.value.length > 1" in _binding("undoLabel", text)


# --- coming back to the app --------------------------------------------------

def test_the_fleet_refetches_when_the_operator_comes_back():
    """The poll is not enough on a phone, which is where this view is read.

    A backgrounded tab has its timers throttled hard or stopped, so the first thing
    on screen after switching back can be minutes old — which is how a session that
    ended looks like one that is still running, with the interval working exactly as
    written. Both events, because neither implies the other: switching apps fires
    visibilitychange with no focus event, and a window raised over another fires
    focus without ever having been hidden.
    """
    text = _app()
    for event, target in (("visibilitychange", "document"), ("focus", "window")):
        assert f"{target}.addEventListener('{event}', refetchOnReturn)" in text, (
            f"nothing refetches the fleet on {event}"
        )
        assert f"{target}.removeEventListener('{event}', refetchOnReturn)" in text, (
            f"the {event} listener outlives the component that added it"
        )
    handler = _function("refetchOnReturn", text)
    assert "load()" in handler, "the handler does not read the fleet"
    assert "visibilityState === 'hidden'" in handler, (
        "the handler fires on the tab being *hidden* too, which is a fleet build "
        "nobody is looking at"
    )


def test_two_fleet_reads_never_overlap():
    """One gesture fires both listeners, and four things call load() now.

    Without a guard that is two builds of the whole fleet racing on the daemon —
    each of which pulls the work-repo mirror and reads every run dir — with the
    loser's payload deciding what stays on screen.
    """
    text = _app()
    body = _function("load", text)
    assert "if (inFlight) return inFlight" in body, (
        "load() has no in-flight guard, so a visibility change plus a focus event "
        "issues two overlapping fleet reads"
    )
    assert "inFlight = null" in body, (
        "the guard is never cleared, so the fleet is read exactly once per tab"
    )
    assert body.index("inFlight = null") > body.index("} finally {"), (
        "the guard is cleared somewhere a failed read can skip, which stops the "
        "fleet updating for the life of the tab"
    )


# --- executed checks --------------------------------------------------------

def test_format_helpers_behave(tmp_path):
    """Actually execute the presentation logic — labels and relative times.

    Missing Node skips, unless the host says it has one
    (:func:`tests.conftest.require_node_toolchain`) — which is the point of going
    through that helper rather than skipping here: on a machine that can build the
    UI, this test not running is a failure and not a shrug. Component rendering is
    not covered here: that needs a DOM, and live test LT3 on a real phone is what
    verifies it.
    """
    import subprocess

    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")

    script = Path(__file__).resolve().parent / "test_platform_web_format.js"
    result = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "all assertions passed" in result.stdout
