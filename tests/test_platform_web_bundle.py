"""Guards on what a phone downloads before it can show anything (T42).

The landing screen is the fleet view: a list of runs, a poll, and no markdown
anywhere on it. Until this slice the first load carried markdown-it and DOMPurify
anyway — a renderer for a screen that renders none — because every view that shows
an agent's words imported ``Markdown.vue`` statically, and one static import from a
component the entry reaches is enough to put a library in the entry chunk.

The terminal solved this shape of problem already: ``RunDetail.vue`` imports it
with ``defineAsyncComponent`` and xterm lives in a chunk of its own, fetched when
that tab is first opened. T44 made the same move possible for the renderer by
putting both packages *and* the CSS behind one module boundary, so each consumer
now imports that boundary lazily and the packages leave the first load with it.

Why these guards read the built output and not the source
--------------------------------------------------------
Because the source-level version of this test passes while the property is broken.
"every consumer uses ``defineAsyncComponent``" stays true right up until a fifth
view imports the component statically — at which point rollup pulls the renderer
back into the entry chunk *for all of them*, and every ``defineAsyncComponent`` the
test was reading is still there, still spelled correctly. Chunking is a property of
the whole module graph, so the only honest question is what came out of the build.

So this module builds the bundle, with two consequences worth being plain about:

- it needs the toolchain, and says so the way the executed markdown probes do
  (:func:`tests.conftest.require_node_toolchain`) rather than skipping quietly;
- a build is the better part of a minute on one core, so a ``dist/`` that is newer
  than everything it is built from is reused. Touching any web source invalidates
  it, which is exactly when the answer can change.

What each test pins:

- the first load is the entry chunk and its stylesheet, which is the premise the
  rest of the file rests on. index.html is where it would quietly stop being true:
  a modulepreload link for a deferred chunk fetches it on the landing screen
- neither markdown-it nor DOMPurify is in that chunk. This is the load-bearing one
- the renderer is *deferred*, not deleted: the chunk exists, carries every marker,
  and the entry names it in a dynamic import
- the markdown CSS travelled with it. Half of what a component costs the first
  load is stylesheet, and the rule that keeps a fence from reflowing is in there
- the terminal chunk is still split. A second async component must not disturb it
- every chunk lands under ``dist/assets``, the only directory the daemon serves
  from, so a deferred chunk is one the browser can actually fetch
- whoever imports the renderer defers it — found by scanning, not from a list,
  because a *new* consumer importing it statically is the failure mode
- the deferral is the loader form, whose loading state draws nothing. A
  ``loadingComponent`` for untrusted text would be a second render path for it
- the conversation still follows the end when the renderer lands a round trip after
  the turns did

Rendering, the flash of a turn with no body, and how any of it feels one-handed are
verified by building the bundle and by live test LT3 on a real phone.
"""

import inspect
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

# node_binary is the two-root Node lookup (T47): the pinned toolchain is
# invisible from inside the suite through the isolated platform dir, and a copy
# that forgot the second root would not fail, it would skip everywhere. In
# conftest because five modules want it, this one included.
from tests.conftest import node_binary, require_node_toolchain

WEB = Path(__file__).resolve().parent.parent / "web"
COMPONENTS = WEB / "src" / "components"
DIST = WEB / "dist"
ASSETS = DIST / "assets"
INDEX = DIST / "index.html"
CHAT = COMPONENTS / "Chat.vue"

#: The names vite.config.js pins for the entry, and the two deliberate splits. Read
#: as literals here on purpose: these are the paths index.html points a browser at
#: and the daemon serves, so a rename is a deployment question, not a detail.
ENTRY_JS = "app.js"
ENTRY_CSS = "index.css"
RENDERER_JS = "Markdown.js"
RENDERER_CSS = "Markdown.css"
TERMINAL_JS = "Terminal.js"
#: The sketch pad (issue 246), the third and smallest deliberate split.
SKETCH_JS = "SketchPad.js"

#: Strings that are in a chunk only if the renderer is: two of markdown-it's rule
#: names, DOMPurify's trusted-types policy name, and two config keys that both
#: DOMPurify and the component's own allowlist spell out. None of them is a local
#: identifier a minifier may rename — they are string literals and property names,
#: which esbuild leaves alone — and there are several because this assertion has to
#: survive a version bump of either package. The mutation that proves they work is
#: restoring a static import in one consumer.
RENDERER_MARKERS = (
    "html_block",
    "smartquotes",
    "dompurify",
    "ALLOWED_TAGS",
    "ALLOWED_URI_REGEXP",
)

#: Same idea for the emulator, whose split this one must not disturb. xterm names
#: itself in its own class names, so one marker is enough.
TERMINAL_MARKER = "xterm"
#: A string only the sketch pad has, and the second attempt at one: a media type
#: matches the composer's own ``accept`` list, and ``-marked`` matches Vuetify's
#: ``mdi-checkbox-marked`` in the entry chunk — both would have failed against a
#: correct build. This is the dialog's own title, which nothing else says.
SKETCH_MARKER = "mark up the image"

#: The selector every markdown rule is scoped to. Its absence from the entry
#: stylesheet is how the CSS half of the split is checked.
RENDERER_CSS_MARKER = ".markdown"

#: A static import of the renderer, which is what defeats the split — from any
#: file, including one that never renders it.
STATIC_IMPORT = re.compile(r"^\s*import\s+.*\bfrom\s+'\./Markdown\.vue'", re.M)

#: The lazy one, whose module specifier is what puts the packages in a chunk.
LAZY_IMPORT = "import('./Markdown.vue')"

#: Ceiling on one build, not a target: 35s on an idle core here and three minutes
#: on a busy one. Long enough that a loaded machine does not fail this file, short
#: enough that a hung build is a failure rather than a hung suite.
BUILD_TIMEOUT_SECONDS = 900


def _read(path):
    return path.read_text(encoding="utf-8")


def _call_source(text, opening):
    """One whole call expression starting at *opening*, or ``None``.

    Balanced on parentheses rather than matched with a pattern, so an assertion
    about what a call contains fails on *that* — a missing option reads as a
    missing option, not as a watcher nobody can find.
    """
    if opening not in text:
        return None
    start = text.index(opening)
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def _web_sources():
    """Every JavaScript and component source the bundle is built from."""
    return [
        path for path in sorted((WEB / "src").rglob("*"))
        if path.is_file() and path.suffix in {".js", ".vue"}
    ]


def _build_inputs():
    """Everything a change to which can change the chunking."""
    return [
        *_web_sources(),
        *(WEB / "src").rglob("*.css"),
        WEB / "index.html",
        WEB / "package.json",
        WEB / "package-lock.json",
        WEB / "vite.config.js",
    ]


def _needs_building():
    """Whether ``dist/`` is missing, or older than something it is built from.

    Compared against the *oldest* artifact in dist, because one build writes them
    all: a half-fresh dist is a rebuild, not a reuse. A source that is merely
    touched counts as changed, which is the safe direction to be wrong in. The
    unsafe one is a source that was *deleted* — nothing left gets a new mtime from
    that — so `rm -rf web/dist` is the answer if a result ever looks impossible.
    """
    if not INDEX.is_file() or not (ASSETS / ENTRY_JS).is_file():
        return True
    built = min(path.stat().st_mtime for path in [INDEX, *ASSETS.iterdir()])
    return any(
        path.stat().st_mtime > built for path in _build_inputs() if path.is_file()
    )


def _build(node):
    """Run the production build, and say what happened when it fails.

    ``vite build`` is invoked through Node directly rather than through npm: it is
    the same command ``npm run build`` runs (pinned by
    :func:`test_the_bundle_is_built_the_way_the_operator_builds_it`) without
    needing npm on PATH or a writable npm cache, neither of which this has anything
    to do with.
    """
    vite = WEB / "node_modules" / "vite" / "bin" / "vite.js"
    result = subprocess.run(
        [node, str(vite), "build"],
        cwd=str(WEB), capture_output=True, text=True,
        timeout=BUILD_TIMEOUT_SECONDS,
        env={**os.environ, "NO_COLOR": "1"},
    )
    assert result.returncode == 0, (
        "the UI build failed, so nothing below is about this working tree:\n"
        + ((result.stderr or "") + (result.stdout or ""))[-2000:]
    )


class Bundle:
    """What one build emitted, as text, keyed by the name a browser asks for."""

    def __init__(self):
        self.index = _read(INDEX)
        self.files = {
            path.name: _read(path)
            for path in sorted(ASSETS.iterdir())
            # Sourcemaps are not served and are not always written; a build with
            # them on would otherwise answer every question twice.
            if path.suffix in {".js", ".css"}
        }

    def carriers(self, marker):
        """Which emitted files contain *marker*."""
        return sorted(name for name, text in self.files.items() if marker in text)


@pytest.fixture(scope="module")
def bundle():
    if _needs_building():
        node = node_binary()
        if not node:
            require_node_toolchain("no Node available (run `lmer platform setup-ui`)")
        if not (WEB / "node_modules" / "vite").is_dir():
            require_node_toolchain(
                "web dependencies are not installed (run `npm ci` in web/)"
            )
        _build(node)
    return Bundle()


# --- what the first load is --------------------------------------------------

def test_the_first_load_is_the_entry_chunk_and_its_stylesheet(bundle):
    """The premise every assertion below rests on, checked rather than assumed.

    "Not in the initial bundle" means nothing unless the initial bundle is one
    known file. Vite can also make a *deferred* chunk part of the first load
    without any of the source changing: a ``modulepreload`` link in index.html
    fetches it eagerly, which would hand the fleet view the renderer back while
    every other test here still passed.
    """
    scripts = re.findall(r'<script[^>]*\bsrc="([^"]+)"', bundle.index)
    assert scripts == [f"./assets/{ENTRY_JS}"], (
        f"the first load is no longer one entry script: {scripts}"
    )
    sheets = re.findall(r'<link[^>]+rel="stylesheet"[^>]+href="([^"]+)"', bundle.index)
    assert sheets == [f"./assets/{ENTRY_CSS}"], (
        f"the first load is no longer one stylesheet: {sheets}"
    )
    eager = re.findall(r'rel="(modulepreload|preload|prefetch)"[^>]*', bundle.index)
    assert not eager, (
        f"index.html fetches a deferred chunk on the landing screen: {eager}"
    )


# --- the renderer is not in it -----------------------------------------------

def test_the_markdown_renderer_is_not_in_the_first_load(bundle):
    """The load-bearing one: the fleet view renders no markdown and pays for none.

    A list of runs is what a phone opens on every glance, and it has no agent prose
    on it at all — the renderer is for the views inside a run. Reading the
    build rather than the sources is the whole point: any one component importing
    ``Markdown.vue`` statically puts both packages back here, wherever it lives in
    the graph, and there is no symptom short of the byte count.
    """
    entry = bundle.files[ENTRY_JS]
    found = [marker for marker in RENDERER_MARKERS if marker in entry]
    assert not found, (
        f"assets/{ENTRY_JS} carries the markdown renderer again ({found}). Some "
        "component imports Markdown.vue statically — one is enough, and it puts "
        "markdown-it and DOMPurify in the chunk the fleet view loads."
    )


def test_the_renderer_is_deferred_and_not_deleted(bundle):
    """The other half of the same property, and not the same test.

    A renderer nobody imports would satisfy the assertion above perfectly. So the
    chunk has to exist, carry the whole renderer, and be named by the entry in a
    dynamic import — which is what makes it arrive when a view first renders one.
    """
    for marker in RENDERER_MARKERS:
        assert bundle.carriers(marker) == [RENDERER_JS], (
            f"{marker!r} is in {bundle.carriers(marker)}, not in the renderer's "
            f"own chunk alone"
        )
    assert f'import("./{RENDERER_JS}")' in bundle.files[ENTRY_JS], (
        f"nothing in the entry chunk fetches assets/{RENDERER_JS}, so the views "
        "that need the renderer will never get one"
    )


def test_the_markdown_stylesheet_travels_with_the_chunk(bundle):
    """A component's styles follow its module, and these are not decoration.

    Half of what makes rendered output safe to read on a phone is CSS — above all
    that a fence scrolls sideways and never reflows, because the operator's use for
    one is to paste it into a shell. Those rules belong to the chunk: in the entry
    stylesheet they would be bytes the fleet view downloads to style markup it
    never shows.
    """
    assert RENDERER_CSS_MARKER not in bundle.files[ENTRY_CSS], (
        f"the markdown rules are back in assets/{ENTRY_CSS}, which means the "
        "component is in the entry chunk with them"
    )
    assert RENDERER_CSS in bundle.files, (
        f"no assets/{RENDERER_CSS} was emitted, so the rules are either nowhere or "
        "somewhere this cannot see"
    )
    styles = bundle.files[RENDERER_CSS]
    assert RENDERER_CSS_MARKER in styles
    assert re.search(r"\.markdown\[[^]]+\] pre\{[^}]*overflow-x:auto", styles), (
        "the fence rule did not travel with the chunk, so a shell line in agent "
        "output reflows and is pasted half"
    )


# --- what must not have moved ------------------------------------------------

def test_the_terminal_chunk_is_still_split(bundle):
    """xterm is the older and larger split, and this slice adds a second one.

    Two async components in one app is where a bundler's chunking gets a say, and
    the way this regresses is not a broken build: it is the emulator quietly
    sharing the entry chunk with the app again.
    """
    assert TERMINAL_MARKER not in bundle.files[ENTRY_JS], (
        f"assets/{ENTRY_JS} carries the emulator again"
    )
    assert bundle.carriers(TERMINAL_MARKER) == ["Terminal.css", TERMINAL_JS], (
        f"the emulator is in {bundle.carriers(TERMINAL_MARKER)}"
    )


def test_the_only_deliberate_splits_are_the_named_ones(bundle):
    """Every chunk is a round trip somebody has to wait for.

    All three are argued for — the terminal saves 90 kB gzipped, the renderer 59
    kB, and the sketch pad is a canvas editor that opens only when an operator
    taps "mark up" on an image they already attached (issue 246) — and the vite
    config says as much. A fourth that nobody named is either an accident or a
    decision that was never written down, and both are worth a failing test.
    """
    entry = bundle.files[ENTRY_JS]
    deferred = sorted(set(re.findall(r'import\("(\./[^"]+)"\)', entry)))
    assert deferred == [f"./{RENDERER_JS}", f"./{SKETCH_JS}", f"./{TERMINAL_JS}"], (
        f"the entry chunk defers {deferred}"
    )


def test_the_sketch_pad_stays_out_of_the_entry_chunk(bundle):
    """The split is only worth having if the editor is actually in the chunk: one
    static import of the component anywhere pulls the whole thing back into the
    entry, and nothing about the build would look wrong."""
    assert SKETCH_MARKER not in bundle.files[ENTRY_JS], (
        f"assets/{ENTRY_JS} carries the sketch pad again"
    )
    assert SKETCH_JS in bundle.files, f"no assets/{SKETCH_JS} was emitted"
    assert SKETCH_MARKER in bundle.files[SKETCH_JS]


def test_every_chunk_lands_where_the_daemon_can_serve_it(bundle):
    """A chunk the daemon will not serve is a blank pane, not a build error.

    ``/assets/{path}`` is the only route to a built file and it resolves inside
    ``dist/assets`` and refuses anything outside it (api.py). index.html is the one
    thing served from the root of dist.
    """
    stray = sorted(
        path.name for path in DIST.iterdir()
        if path.is_file() and path.name != "index.html"
    )
    assert not stray, (
        f"{stray} sit outside dist/assets, where the daemon has no route to them"
    )
    assert bundle.files, "nothing was emitted into dist/assets at all"


# --- the source decisions the build above is the consequence of --------------

def test_every_view_that_renders_agent_words_defers_the_renderer():
    """Scanned, not listed, because the failure mode is a view nobody thought of.

    The build tests are the ones that would catch it either way; this one is here
    to *name the file*, because "markdown-it is in app.js" does not say which of a
    dozen components put it there.
    """
    statics = [
        path.name for path in _web_sources() if STATIC_IMPORT.search(_read(path))
    ]
    assert not statics, (
        f"{statics} import Markdown.vue statically, which puts markdown-it and "
        "DOMPurify back in the entry chunk for every view at once"
    )

    renders = {path.name for path in _web_sources() if "<Markdown" in _read(path)}
    lazy = {path.name for path in _web_sources() if LAZY_IMPORT in _read(path)}
    assert renders, "nothing renders agent words any more, which is not a saving"
    assert renders == lazy, (
        f"these render the shared renderer without deferring it: {renders - lazy}; "
        f"these defer one they never render: {lazy - renders}"
    )


def test_nothing_stands_in_for_the_renderer_while_it_loads():
    """There is one render path for untrusted text, and waiting must not add one.

    ``defineAsyncComponent(loader)`` renders nothing until the chunk lands, which
    is the correct placeholder for this text: the option form is the one that takes
    a ``loadingComponent``, and a stand-in for an agent's words is either plain
    text (which the consumer can do itself) or a second sanitiser nobody reviewed.
    The app-wide count of ``v-html`` is kept by
    :mod:`tests.test_platform_web_markdown`; this checks the seam the wait created.
    """
    for path in _web_sources():
        text = _read(path)
        if LAZY_IMPORT not in text:
            continue
        for match in re.finditer(r"defineAsyncComponent\(", text):
            tail = text[match.end():match.end() + 24].lstrip()
            assert tail.startswith("()") or tail.startswith("async ()"), (
                f"{path.name} defers a component with the option form, which is "
                f"how a fallback renderer gets in: {tail!r}"
            )
        assert "v-html" not in text, (
            f"{path.name} injects markup of its own while waiting for the one "
            "component that is allowed to"
        )


def test_the_conversation_follows_the_end_when_the_renderer_lands():
    """The cost of deferring it, paid where it lands.

    ``absorb`` scrolls to the end of the conversation on the first page, and with
    the renderer still on its way that pane is a column of headers with no bodies —
    so the end it reaches is not the end a moment later. The ResizeObserver in
    ``onMounted`` does not cover it either: a conversation long enough for this to
    matter has already filled the pane to its bound, so the box it observes never
    changes size. ``post`` is load-bearing (the DOM measured has to be the one with
    the words in it), and so is the ``following`` check — a reader who scrolled back
    must not be yanked to the bottom by a chunk arriving.
    """
    text = _read(CHAT)
    watcher = _call_source(text, "watch(rendererLoaded,")
    assert watcher, "nothing re-follows the end when the renderer arrives"
    assert "stickToBottom()" in watcher, (
        "the watcher no longer follows the end of the conversation"
    )
    assert "following.value" in watcher, (
        "the conversation is yanked to the end whether or not it is being read"
    )
    assert "flush: 'post'" in watcher, (
        "the scroll is measured before the words are in the DOM, so it stops at "
        "the height the turns had while they were still empty"
    )
    assert "rendererLoaded.value = true" in text, "the ref is never set"


def test_the_bundle_is_built_the_way_the_operator_builds_it():
    """Parity between this file's build and the one that ships.

    These tests would be worth nothing if they measured a different build from the
    one ``lmer platform setup-ui`` runs, and the seam is one line in package.json.
    """
    payload = json.loads(_read(WEB / "package.json"))
    assert payload["scripts"]["build"] == "vite build", (
        "the build script is no longer plain `vite build`, so calling vite "
        "directly is no longer what the operator gets"
    )
    from lmer_platform import ui_build

    assert '"run", "build"' in inspect.getsource(ui_build.build_ui), (
        "setup-ui no longer runs the build script this file stands in for"
    )
