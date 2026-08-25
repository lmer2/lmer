"""Guards on the one renderer that turns a container's words into markup (T44).

Three views show text produced *inside a container*: the conversation
(``Chat.vue``, T38), one entry of the operator channel (``AskEntry.vue``, which is
what both views of that channel draw an entry with since #274) and the question a
live session is blocked on (``AskBox.vue``). Everything else the daemon
serves is written by us; this text is written by an agent, from a repository it was
pointed at, with tool output pasted into it. So the interesting question about
"render it as markdown" is not whether the asterisks disappear — that is visible
the moment anybody looks — but whether the render path can be talked into
producing something live, which is visible to nobody until it has already
happened.

The reason this file exists rather than three copies of it: the *number of
implementations* is itself the property being kept. A second hand-rolled renderer
is how one of the three consumers ends up without the sanitiser, and nothing on
screen would say so. ``Markdown.vue`` is the only one, it owns the single
``v-html`` in the app, and it owns the CSS too — half of what makes rendered
output safe to read on a phone is that a fence never reflows.

Two layers, and this file exists to keep both:

- the parser is configured so it cannot emit HTML at all (``html: false``), and
  so a link has to carry a scheme named in an allowlist to survive;
- what the parser does emit is passed through DOMPurify against an explicit tag
  and attribute allowlist before it is handed to ``v-html``.

The first layer is *executed* here: the render path in ``Markdown.vue`` sits
between two marker comments, this module lifts that block verbatim and runs
hostile input through it under Node. The second layer is pinned by reading the
source, because DOMPurify needs a DOM and this image has no browser and no jsdom —
a limitation each affected test states rather than papers over. In Node,
``DOMPurify.sanitize`` returns its input untouched, which is why the component's
own fallback (escape everything) exists and is asserted too.

The rest is the behaviour that markdown makes newly possible to break, all of it
invisible in a desktop window:

- a fence scrolls sideways and never reflows. A wrapped shell command reads as
  two lines and the half that gets pasted is a real hazard;
- nothing in agent output can make this browser fetch anything — the same
  reasoning main.js gives for compiling in SVG icon paths instead of the MDI
  webfont, applied to ``![](…)``;
- what *you* sent or replied is still shown verbatim, in every view, so the two
  sides of a conversation do not turn into one wall of formatted prose — and so a
  bubble never claims you typed something you did not.

Chat's own bounds (the pane, the cap on one turn, the stick-to-bottom flag) stay
in :mod:`tests.test_platform_web_chat`, because they belong to the conversation
rather than to the renderer.

Rendering, and how any of it feels one-handed, is verified by building the bundle
and by live test LT3 on a real phone.
"""

import inspect
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.conftest import REQUIRE_NODE_ENV, node_binary, require_node_toolchain

WEB = Path(__file__).resolve().parent.parent / "web"
COMPONENTS = WEB / "src" / "components"
MARKDOWN = COMPONENTS / "Markdown.vue"
CHAT = COMPONENTS / "Chat.vue"
ASK_CHANNEL = COMPONENTS / "AskChannel.vue"
# One channel entry, and both views of the channel draw their entries with it
# (#274). The rendering the guards below are about is here rather than in the two
# views, which is the whole of what that extraction did.
ASK_ENTRY = COMPONENTS / "AskEntry.vue"
ASK_BOX = COMPONENTS / "AskBox.vue"
APP = WEB / "src" / "App.vue"
STYLE = WEB / "src" / "style.css"

# Every view that renders words an agent wrote. Named rather than globbed: adding
# one is a decision, and the decision is "it goes through the shared component".
CONSUMERS = [CHAT, ASK_ENTRY, ASK_BOX]

# The fence around the part of Markdown.vue that is plain JavaScript with no Vue
# and no DOM in it, so it can be run as-is. Prefixes, because both lines are
# padded out to the column limit with dashes.
RENDER_PATH_START = (
    "// --- render path (extracted by tests/test_platform_web_markdown.py)"
)
RENDER_PATH_END = "// --- end of render path"


def _read(path):
    return path.read_text(encoding="utf-8")


def _rule(selector, path=MARKDOWN):
    """One declaration block from a component's scoped stylesheet.

    Same helper as :mod:`tests.test_platform_web_app`; the rules this file cares
    about are the ones that reach *inside* injected markup, which the other file
    knows nothing about.
    """
    style = _read(path)
    style = style[style.index("<style"):]
    block = style[style.index(f"{selector} {{"):]
    return block[:block.index("}")]


def _render_path():
    """The render path out of Markdown.vue, verbatim.

    Verbatim is the whole point: a copy of the configuration in this file would
    pass forever while the component's own copy drifted into something unsafe.
    """
    text = _read(MARKDOWN)
    start = text.index(RENDER_PATH_START)
    end = text.index(RENDER_PATH_END, start)
    return text[start:end]


def _probe(body):
    """Run *body* under Node with the component's real render path in scope.

    The two packages are imported by their bare names and the process runs from
    ``web/``, so Node resolves them out of the same ``node_modules`` the bundle is
    built from — not a copy that could be a different version.

    What happens when there is no toolchain is :func:`require_node_toolchain`'s
    decision, not this function's: on a host that says it has Node the absence is
    a failure, because a skip here takes every executed guard below with it and
    leaves the run green (T47).
    """
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")
    if not (WEB / "node_modules" / "markdown-it").is_dir():
        require_node_toolchain(
            "web dependencies are not installed (run `npm ci` in web/)"
        )

    script = "\n".join([
        "import MarkdownIt from 'markdown-it'",
        "import DOMPurify from 'dompurify'",
        "import assert from 'node:assert/strict'",
        # The render path imports the run-reference shape from the module that
        # owns it (issue #241), so the probe resolves it from the real file too.
        "import { RUN_REF_RE } from './src/runref.js'",
        _render_path(),
        body,
        "console.log('probe ok')",
    ])
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(WEB), capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "probe ok" in result.stdout
    return result.stdout


# Everything a hostile transcript would try. Kept in one place because every
# test that runs it should run all of it: the interesting failures are the ones
# where a fix for one shape opens another.
HOSTILE = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "<iframe src=x></iframe>",
    "<svg onload=alert(1)></svg>",
    "<a href=\"javascript:alert(1)\">click</a>",
    "[click](javascript:alert(1))",
    "[click](JaVaScRiPt:alert(1))",
    "[click](vbscript:alert(1))",
    "[click](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)",
    "[click](  javascript:alert(1))",
    "![beacon](http://elsewhere.invalid/pixel.png)",
    "<p onmouseover=\"alert(1)\">hover</p>",
    "`<script>alert(1)</script>`",
    "```\n<script>alert(1)</script>\n```",
    "> <script>alert(1)</script>",
    "[click](https://ok.invalid/x \"onload=alert(1)\")",
    "<style>body{display:none}</style>",
    "<base href=\"http://elsewhere.invalid/\">",
    # The one non-web scheme admitted is a run reference of exactly one shape
    # (issue #241); everything else wearing it is hostile input.
    "[click](lmer://spawn/gitlab.example.com/acme/widget/x)",
    "[click](lmer://run/h/p/s?spawn=1)",
    "[click](lmer://run/h/p/s/../../etc/passwd)",
    "[click](lmer:javascript:alert(1))",
    "[click](lmerx://run/h/p/s)",
]

# Tag names that must never appear as tags, whatever else changes. The tag
# allowlist below covers these already; they are named because they are the
# threat, and a test that only says "the set matched" reads like bookkeeping.
NEVER_A_TAG = ["script", "iframe", "svg", "img", "style", "base", "object"]

# Pulls apart what the renderer produced, so an assertion can tell markup from
# text. Three distinctions the naive "is this substring present" version gets
# wrong, each of which showed up while writing this:
#
#   - `&lt;img onerror=…&gt;` is the *correct* answer for hostile input, and it
#     contains both of those substrings;
#   - `[x](url "onload=alert(1)")` puts that string in a quoted `title=`, where it
#     is a tooltip and nothing else;
#   - so what matters is the tag names, the attribute *names*, and the values of
#     the attributes a browser dereferences.
#
# Splitting tags on `>` is exact for this input: markdown-it escapes `>` inside an
# attribute value, so the only `>` that ends a tag is the one that ends a tag.
INSPECT_JS = """
const inspect = (html) => {
  const tags = [], attrs = [], urls = []
  for (const tag of html.match(/<[^>]*>/g) || []) {
    const name = /^<\\/?([a-z0-9]+)/i.exec(tag)
    if (name) tags.push(name[1].toLowerCase())
    for (const [, key, value] of tag.matchAll(/\\s([a-zA-Z:-]+)\\s*=\\s*"([^"]*)"/g)) {
      attrs.push(key.toLowerCase())
      if (/^(href|src|srcset|action|formaction|xlink:href)$/i.test(key)) urls.push(value)
    }
  }
  return { tags, attrs, urls }
}
"""


# --- one renderer, and every view pointed at it ------------------------------

def test_there_is_exactly_one_renderer_and_one_v_html():
    """The property that made this a component instead of three copies.

    A shared *function* would still leave one ``v-html`` per view, each an edit
    away from being bound to the raw text — and the copy that skips the sanitiser
    renders identically for every message anybody tests with. So the count is the
    guard: one file imports the two packages, one file contains the binding, and a
    fourth consumer (T31's supervisor chat) inherits both by construction.
    """
    holders, bindings = [], []
    for source in sorted(WEB.glob("src/**/*")):
        if not source.is_file() or source.suffix not in {".js", ".vue"}:
            continue
        text = _read(source)
        # The quoted module specifier, so a comment that mentions either package by
        # name is prose and only an import counts.
        if "'markdown-it'" in text or "'dompurify'" in text:
            holders.append(source.name)
        # One entry per binding, not per file: a second v-html in the component
        # itself is the same hole as one in another view.
        bindings.extend([source.name] * len(re.findall(r'v-html="', text)))
    assert holders == ["Markdown.vue"], (
        f"the renderer exists in more than one place: {holders}"
    )
    assert bindings == ["Markdown.vue"], (
        f"markup is injected outside the one place that sanitises it: {bindings}"
    )


def test_every_view_that_shows_agent_words_renders_them():
    """The operator's ask, and the reason the extraction happened when it did.

    The operator channel showed its notes and questions raw while the chat beside
    it rendered — the same agent, the same markdown, two different answers a tab
    apart. Each of these is a *field written by an agent*: the channel entry, and
    the question a live session is blocked on.
    """
    for path in CONSUMERS:
        text = _read(path)
        # T42 gave the renderer a chunk of its own, so a consumer reaches it
        # through defineAsyncComponent rather than a static import — the property
        # tests/test_platform_web_bundle.py keeps, against the BUILT output. What
        # this guard is about is unchanged: the view renders agent words through the
        # one shared component instead of showing them raw or rolling its own.
        assert "defineAsyncComponent" in text and "import('./Markdown.vue')" in text, (
            f"{path.name} does not use the shared renderer"
        )
    entry = _read(ASK_ENTRY)
    assert re.search(r"<Markdown\s+:text=\"entry\.text\"", entry), (
        "the operator channel's own feed is still raw text"
    )
    # And the feed is drawn with it: the rendering moved into the entry component
    # (#274), so a view that stopped composing it would show raw text again with
    # every guard here still green.
    assert "<AskEntry" in _read(ASK_CHANNEL), (
        "the operator channel draws its entries with something other than the "
        "component this file just checked"
    )
    box = _read(ASK_BOX)
    assert re.search(r"<Markdown\s+:text=\"question\.text\"", box), (
        "a live session's question is still raw text"
    )
    assert re.search(r"<Markdown[^>]*:text=\"message\.text\"", _read(CHAT), re.S), (
        "the conversation is still raw text"
    )


def test_your_own_words_are_never_rendered_anywhere():
    """Three places, one rule, and each has its own reason on top of the shared one.

    The shared one: what you typed went to a session as bytes, and a line that
    quietly ate a pair of asterisks would be misreporting what the run received.
    On top of that — the conversation has to stay tellable apart at a glance, or a
    long scroll is one wall of formatted prose; and an option chip is *literally*
    the text a tap puts in the composer, so a rendered chip would promise to send
    something else.
    """
    chat = _read(CHAT)
    branch = re.search(
        r'<p\s+v-if="([^"]+)"\s+class="text-body-medium said plain"',
        chat,
        re.S,
    )
    assert branch and "message.role === 'user'" in branch.group(1), (
        "the user's own turns go through the renderer too"
    )
    entry = _read(ASK_ENTRY)
    assert "you: {{ entry.answer.text }}" in entry, (
        "your reply in the channel history is no longer shown as you wrote it"
    )
    assert "said plain reply" in entry, (
        "the verbatim half of an entry lost the class that keeps its newlines"
    )
    box = _read(ASK_BOX)
    assert ">{{ option }}</v-chip>" in box, (
        "an option chip renders markdown, so its label is no longer the text a "
        "tap sends"
    )


def test_the_renderer_makes_no_colour_of_its_own():
    """Same rule as everywhere else: the theme in main.js owns the palette.

    A fence needs a ground to sit on, which is the one thing in this component
    that wants a colour — and it takes it from the theme, so both schemes get it
    right and neither is restated here.
    """
    text = _read(MARKDOWN)
    assert not [
        line for line in text.splitlines()
        if "#" in line and ("color" in line.lower() or "background" in line.lower())
        and "rgb(var(--v-theme" not in line
    ], "colours come from the theme, never a hex literal"
    assert "rgb(var(--v-theme-code))" in text
    # text-caption and text-h6 do not exist in Vuetify 4.
    assert "text-caption" not in text
    assert "text-h6" not in text


# --- the render path, executed ----------------------------------------------

def test_hostile_transcript_text_does_not_survive_as_live_markup():
    """The test this feature is for.

    An agent's output is untrusted text: it quotes the web, it pastes tool output,
    and on a bad day it is repeating something a repository told it to repeat.
    Turning that into markup is the one genuinely dangerous thing this UI does,
    and a mistake in it is silent — the page renders, and the operator's browser
    is running somebody else's script against a daemon that can spawn containers.

    What is executed here is layer 1, the parser: with ``html: false`` a tag in the
    text is *never read as a tag*, so it comes back as the characters of one.
    DOMPurify (layer 2) cannot run in this image — it needs a DOM, and there is no
    browser and no jsdom here — so it is pinned by
    :func:`test_the_rendered_html_is_allowlisted_before_it_reaches_v_html`
    instead. That is also why the component falls back to escaping everything when
    ``DOMPurify.isSupported`` is false, which *is* exercised below: in Node that is
    the branch ``renderMarkdown`` takes, and its output has to be inert too.
    """
    body = "\n".join([
        INSPECT_JS,
        f"const hostile = {json.dumps(HOSTILE)}",
        f"const never = {json.dumps(NEVER_A_TAG)}",
        "const allowed = new Set(SANITIZE.ALLOWED_TAGS)",
        "for (const source of hostile) {",
        "  for (const rendered of [markdown.render(source), renderMarkdown(source)]) {",
        "    const seen = inspect(rendered)",
        "    const where = `${JSON.stringify(source)} rendered as "
        "${JSON.stringify(rendered)}`",
        "    for (const tag of seen.tags) {",
        "      assert.ok(!never.includes(tag), `<${tag}> is live in ${where}`)",
        "      assert.ok(allowed.has(tag), `<${tag}> is not on the allowlist: ${where}`)",
        "    }",
        "    for (const attr of seen.attrs) {",
        "      assert.ok(!attr.startsWith('on'), `${attr}= is live in ${where}`)",
        "      assert.ok(!/^(src|srcset|formaction|action)$/.test(attr),",
        "        `${attr}= is live in ${where}`)",
        "    }",
        # This file's own idea of a safe scheme, deliberately not the component's
        # SAFE_LINK: reading the constant under test would mean widening it also
        # widened the assertion, which is exactly the edit that has to be caught.
        "    for (const url of seen.urls) {",
        "      assert.match(url, /^(?:https?:|mailto:)/i,",
        "        `a browser would follow ${url}: ${where}`)",
        "    }",
        "  }",
        "}",
        # Escaped, not swallowed. A renderer that dropped the text would pass every
        # assertion above and hide half of what an agent said.
        "for (const rendered of [markdown.render('<script>alert(1)</script>'),",
        "                        renderMarkdown('<script>alert(1)</script>')]) {",
        "  assert.match(rendered, /&lt;script&gt;alert\\(1\\)&lt;\\/script&gt;/, rendered)",
        "}",
        # The fallback is only inert because there is nothing to sanitise in the
        # first place; say so, so nobody reads the loop above as covering layer 2.
        "assert.equal(DOMPurify.isSupported, false, 'this probe has a DOM now — "
        "sanitising is no longer untested and the docstrings should say so')",
    ])
    _probe(body)


def test_a_link_the_agent_wrote_can_only_be_http_https_or_mailto():
    """Deny by default, because the next dangerous scheme is not on anyone's list.

    markdown-it ships a denylist (``javascript:``, ``vbscript:``, ``file:``,
    ``data:``) and that is the shape this must not drift back into: it was written
    against the schemes that were interesting when it was written. An allowlist is
    wrong in the safe direction — an unknown scheme renders as text, which is
    ugly and harmless.
    """
    body = "\n".join([
        "const rejected = ['javascript:alert(1)', 'vbscript:x', 'file:///etc/passwd',",
        "  'data:text/html,x', 'weird-new-scheme:x', 'JAVASCRIPT:alert(1)']",
        "for (const url of rejected) {",
        "  assert.ok(!markdown.validateLink(url), `${url} was accepted as a link`)",
        "  const html = markdown.render(`[click](${url})`)",
        "  assert.ok(!html.includes('href'), `${url} still produced an href: ${html}`)",
        "}",
        "for (const url of ['https://ok.invalid/x', 'http://ok.invalid/x',",
        "                   'mailto:someone@ok.invalid']) {",
        "  assert.ok(markdown.validateLink(url), `${url} was refused`)",
        "}",
    ])
    _probe(body)


def test_an_external_link_cannot_reach_back_into_this_page():
    """``target=_blank`` without ``rel`` hands the other page a live handle.

    ``window.opener`` on the new tab can navigate this one — the tab that is
    authenticated to the daemon — and nothing on screen would change while it
    happened. ``noreferrer`` is the smaller half: it keeps the run's URL, which
    names host, project and slug, out of somebody else's access log.
    """
    body = "\n".join([
        "for (const source of ['[x](https://ok.invalid/p)',",
        "                      'bare https://ok.invalid/p link']) {",
        "  const html = markdown.render(source)",
        "  assert.match(html, /<a /, `no link at all in ${html}`)",
        "  assert.match(html, /rel=\"noopener noreferrer\"/, html)",
        "  assert.match(html, /target=\"_blank\"/, html)",
        "}",
    ])
    _probe(body)


def test_a_run_reference_is_a_link_and_nothing_wearing_its_scheme_is():
    """The one non-web scheme admitted here (issue #241), and only its shape.

    Executed, because both halves fail invisibly: too strict renders a reference
    as text, too loose puts a hole in an allowlist that denies by default.
    """
    body = "\n".join([
        "const ref = 'lmer://run/gitlab.example.com/acme/widget/develop-issue-381'",
        "assert.ok(markdown.validateLink(ref), 'a run reference was refused')",
        "const html = markdown.render(`[the run](${ref})`)",
        "assert.match(html, new RegExp(`href=\"${ref}\"`), html)",
        "assert.match(html, />the run</, 'the label is the visible text')",
        # A scheme the browser cannot open must not be sent to a new tab.
        "assert.ok(!html.includes('target='), `run reference got a target: ${html}`)",
        "assert.ok(!html.includes('rel='), `run reference got a rel: ${html}`)",
        "const rejected = ['lmer://run/h/s', 'lmer://run/h/p/s?spawn=1',",
        "  'lmer://run/h/p/s#frag', 'lmer://spawn/h/p/s', 'lmer:run/h/p/s',",
        "  'lmer://run/h/p/s/../../etc', 'lmerx://run/h/p/s', 'lmer://run//p/s']",
        "for (const url of rejected) {",
        "  assert.ok(!markdown.validateLink(url), `${url} was accepted as a link`)",
        "  const out = markdown.render(`[click](${url})`)",
        "  assert.ok(!out.includes('href'), `${url} still produced an href: ${out}`)",
        "}",
        # Layer 2 must admit what layer 1 built and nothing more — where a stray
        # `i` flag on the combined pattern was widening the scheme.
        "for (const url of [ref, 'https://ok.invalid/x', 'HTTPS://ok.invalid/x',",
        "                   'mailto:a@ok.invalid']) {",
        "  assert.ok(LINK_OK(url), `${url} was refused by the parser`)",
        "  assert.ok(SANITIZE.ALLOWED_URI_REGEXP.test(url), `${url} was refused by the sanitiser`)",
        "}",
        "for (const url of ['LMER://run/h/p/s', 'lmer://RUN/h/p/s', 'LmEr://Run/H/P/S',",
        "                   'lmer://run/h/p/s?spawn=1', 'javascript:alert(1)',",
        "                   'JAVASCRIPT:alert(1)', 'data:text/html,x']) {",
        "  assert.ok(!LINK_OK(url), `${url} was accepted by the parser`)",
        "  assert.ok(!SANITIZE.ALLOWED_URI_REGEXP.test(url),",
        "    `${url} was accepted by the sanitiser but not by the parser`)",
        "}",
    ])
    _probe(body)


def test_nothing_in_a_transcript_makes_this_browser_fetch_anything():
    """main.js's reasoning about the icon webfont, one level down.

    An ``![](…)`` in agent output is a request the operator's browser makes to a
    host chosen by a container: a beacon that says which run is being read and
    when, an empty box on the LAN with no route out that this UI is for, and — the
    quiet one — a message that changes height *after* the view has scrolled to the
    end of it, which is exactly how the stick-to-bottom flag gets caught out.

    Disabled at the parser rather than stripped after it, so there is no ordering
    of the two layers in which an ``<img>`` exists at all.
    """
    body = "\n".join([
        "for (const source of ['![alt](http://elsewhere.invalid/p.png)',",
        "                      '![alt](https://elsewhere.invalid/p.png)',",
        "                      '![alt][ref]\\n\\n[ref]: http://elsewhere.invalid/p.png']) {",
        "  const html = markdown.render(source)",
        "  assert.ok(!/<img/i.test(html), `an image survived: ${html}`)",
        "  assert.ok(!/src\\s*=/i.test(html), `something is fetched: ${html}`)",
        "}",
    ])
    _probe(body)
    assert "markdown.disable(['image'])" in _render_path(), (
        "images are stripped afterwards rather than never built"
    )


def test_the_allowlist_covers_everything_the_parser_can_emit():
    """The seam between the two layers, checked in the direction that goes quiet.

    A tag markdown-it emits that DOMPurify has not been told about does not fail —
    it disappears, so a table or a blockquote in an answer becomes a run of
    unlabelled text and nobody can say when it stopped working. Run in the other
    direction too: if the parser ever starts emitting ``src`` or an event handler,
    that is layer 2 being asked to be the only thing standing between agent output
    and the DOM.

    Two attributes are deliberately dropped rather than allowed, and both cost
    only appearance: ``class``, the language name on a fence, worth nothing
    without a highlighter and an invitation to name any class in the app; and
    ``style``, which markdown-it puts on an aligned table cell and which — allowed
    — would let a container's output position things over this page.
    """
    corpus = "\n\n".join([
        "# heading\n\n## smaller",
        "para with **bold**, *em*, ~~struck~~ and `code`",
        "- one\n- two\n\n1. first\n2. second",
        "> quoted\n\n---",
        "| a | b |\n| --- | ---: |\n| 1 | 2 |",
        "```sh\nrm -rf -- /tmp/x\n```",
        "[link](https://ok.invalid/x) and https://bare.invalid/y",
        "term\nwrapped by a single newline",
    ] + HOSTILE)
    body = "\n".join([
        f"const html = markdown.render({json.dumps(corpus)})",
        "const tags = new Set([...html.matchAll(/<\\/?([a-z][a-z0-9]*)/gi)]"
        ".map((m) => m[1].toLowerCase()))",
        "const attrs = new Set([...html.matchAll(/<[a-z][^>]*?\\s([a-z-]+)=/gi)]"
        ".map((m) => m[1].toLowerCase()))",
        "const allowedTags = new Set(SANITIZE.ALLOWED_TAGS)",
        "for (const tag of tags) {",
        "  assert.ok(allowedTags.has(tag), `the sanitiser would silently drop <${tag}>`)",
        "}",
        "const allowedAttrs = new Set([...SANITIZE.ALLOWED_ATTR, 'class', 'style'])",
        "for (const attr of attrs) {",
        "  assert.ok(allowedAttrs.has(attr), `the parser emitted an unreviewed ${attr}=`)",
        "}",
        # Named individually as well: these are the ones whose appearance would be
        # a change of threat model, not a formatting regression.
        "for (const attr of ['src', 'srcset', 'formaction']) {",
        "  assert.ok(!attrs.has(attr), `the parser now emits ${attr}=`)",
        "}",
        "assert.ok(![...attrs].some((a) => a.startsWith('on')), `${[...attrs]}`)",
        "assert.ok(tags.size > 12, `the corpus stopped exercising the parser: "
        "${[...tags]}`)",
    ])
    _probe(body)


def test_shell_text_survives_being_rendered():
    """This text is full of things that get read off a screen and typed back in.

    markdown-it's typographer turns ``--`` into an en dash and ``...`` into an
    ellipsis, which is charming in prose and silently destroys ``--force`` and a
    truncated path. It is off by default, so this is a guard against somebody
    turning it on for the quotes.
    """
    body = "\n".join([
        "const html = markdown.render('run `git push --force-with-lease` ... (c) done')",
        "assert.match(html, /--force-with-lease/, html)",
        "assert.match(html, /\\.\\.\\./, html)",
        "assert.match(html, /\\(c\\)/, html)",
    ])
    _probe(body)


def test_a_single_newline_is_still_a_line_break():
    """Rendering must not *lose* formatting the plain view already had.

    Every view this replaces was ``white-space: pre-wrap``: every newline the agent
    wrote was a newline on screen. CommonMark collapses a single one into a space,
    so without ``breaks`` an agent's line-per-item answer arrives as one paragraph
    — a regression that reads as the text being mangled. It is also why the two
    operator-channel views had to *drop* their own ``pre-wrap`` in the same edit
    (chat lost its in T38): on rendered markup it shows the newlines between block
    tags as blank lines instead.
    """
    body = "\n".join([
        "const html = markdown.render('first line\\nsecond line')",
        "assert.match(html, /<br>/, html)",
        "assert.ok(!/first line second line/.test(html), html)",
    ])
    _probe(body)
    # The operator channel's two halves share a class, so this is the one place
    # where a stray `pre-wrap` would land on rendered markup as well as on the
    # reply typed beside it. One stylesheet since #274, and both views of the
    # channel are drawn from it — so this reads the entry component rather than
    # one view's copy of the rule.
    assert "white-space: pre-wrap" not in _rule(".said", ASK_ENTRY), (
        "pre-wrap is back on the rendered half, which shows the newlines between "
        "block tags as blank lines"
    )
    assert "white-space: pre-wrap" in _rule(".said.plain", ASK_ENTRY), (
        "your own reply lost the rule that keeps the newlines you typed"
    )
    box = _read(ASK_BOX)
    assert "pre-wrap" not in box[box.index("<style"):], (
        "the question is rendered now, so nothing there needs pre-wrap"
    )


# --- and they cannot go missing quietly (T47) ---------------------------------
#
# Everything above this line is executed, which means everything above this line
# can be *skipped* — and a skip is the one outcome that looks like success from
# every angle worth checking. `pytest -q` prints it as an `s`, the run exits 0,
# and a mutation sweep that reads the exit code scores it as "not caught". So on a
# host that says it has a Node, not having one is a failure. These tests are the
# infrastructure's own coverage: the strict mode must fail, the default must still
# skip, and the strict mode must never resolve to a second skip.

def _outcome_of(call):
    """Run *call* and hand back the pytest outcome it raised, as an object.

    Deliberately not ``pytest.raises``. ``Failed`` and ``Skipped`` are siblings —
    neither catches the other — so ``pytest.raises(pytest.fail.Exception)`` around
    a call that skips does not fail, it lets the skip through, and the test that
    was checking for a failure is itself reported as *skipped*. Green, quiet, and
    checking nothing: the same trick, one level up, in the tests written to catch
    it. So the outcome is inspected instead of pattern-matched, and "no outcome at
    all" (``None``) is a third answer rather than an accident.
    """
    try:
        call()
    except (pytest.fail.Exception, pytest.skip.Exception) as outcome:
        return outcome
    return None


def _assert_failed(call):
    """Assert *call* failed the test, naming what it did instead."""
    outcome = _outcome_of(call)
    assert isinstance(outcome, pytest.fail.Exception), (
        f"a missing toolchain produced {type(outcome).__name__} where "
        f"{REQUIRE_NODE_ENV} demands a failure — and a skip is not a failure"
    )
    return str(outcome)


def _assert_skipped(call):
    """Assert *call* skipped the test, naming what it did instead."""
    outcome = _outcome_of(call)
    assert isinstance(outcome, pytest.skip.Exception), (
        f"a missing toolchain produced {type(outcome).__name__} with "
        f"{REQUIRE_NODE_ENV} unset, so a Node-less machine cannot run the suite"
    )
    return str(outcome)


def test_a_missing_toolchain_is_a_failure_where_node_is_expected(monkeypatch):
    """The point of the whole thing: the flag turns the `s` into an `F`.

    Which is only half of it. A hard failure that leaves the reader with nowhere
    to go is worse than the skip it replaced — they will reach for the shortest
    thing that makes it stop, and that is deleting the guard. So the message is
    part of the behaviour and is asserted like any other: the variable by name,
    and the command that produces a toolchain.
    """
    monkeypatch.setenv(REQUIRE_NODE_ENV, "1")
    message = _assert_failed(lambda: require_node_toolchain("no Node available"))
    assert "no Node available" in message, message
    # A hard failure that leaves the reader stuck is worse than the skip was, so
    # the message owes them both halves: which variable, and how to get a Node.
    assert REQUIRE_NODE_ENV in message, message
    assert "lmer platform setup-ui" in message, message
    assert "npm ci" in message, message


def test_a_missing_toolchain_is_still_only_a_skip_by_default(monkeypatch):
    """A laptop with no Node has to stay able to run this suite.

    The coverage is genuinely gone in that case, which is why the skip reason
    names the variable: the person reading it is the one who can decide their
    machine ought to have been holding itself to the executed tests.
    """
    monkeypatch.delenv(REQUIRE_NODE_ENV, raising=False)
    reason = _assert_skipped(lambda: require_node_toolchain("no Node available"))
    assert REQUIRE_NODE_ENV in reason, reason


def test_the_strict_mode_takes_a_deliberate_value_and_nothing_less(monkeypatch):
    """``LMER_TESTS_REQUIRE_NODE=`` in a .env file is not a request for it.

    Arming on mere presence would make an empty assignment — or a `0` somebody
    wrote to turn it *off* — fail a Node-less machine with no way to read why from
    the variable they set. Both directions are checked: the affirmative spellings
    arm it, and everything else leaves the default alone.
    """
    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv(REQUIRE_NODE_ENV, value)
        _assert_failed(lambda: require_node_toolchain("no Node available"))
    for value in ("", " ", "0", "false", "no", "off", "maybe"):
        monkeypatch.setenv(REQUIRE_NODE_ENV, value)
        _assert_skipped(lambda: require_node_toolchain("no Node available"))


def test_every_executed_test_here_goes_through_the_one_guard(monkeypatch, tmp_path):
    """Written *and* wired: the decision lives in `_probe`, and nowhere else does.

    Several halves, because none of them alone is worth anything. The wiring: with
    the flag set, *both* of the reasons ``_probe`` can decline — no Node, and no
    ``node_modules`` for it to resolve the two packages out of — have to end in a
    failure, because either one on its own takes every executed test with it. The
    location: the decision is inside ``_probe``, the funnel all of them go through.
    And the exclusivity: anything in this file that reaches for ``pytest.skip`` on
    its own is outside the guard entirely, and its absence from a run would be as
    quiet as it was before this existed.
    """
    monkeypatch.setenv(REQUIRE_NODE_ENV, "1")
    monkeypatch.setattr(f"{__name__}.node_binary", lambda: None)
    message = _assert_failed(lambda: _probe("assert.ok(true)"))
    # The *reason*, not the advice. `lmer platform setup-ui` and `npm ci` are in
    # the guard's standing message either way, so asserting those would pass on a
    # probe that had stopped saying which of its two bail-outs it hit.
    assert "no Node available" in message, message

    # A Node, and nothing for it to import: the half a host with a system Node and
    # no `npm ci` lands on, and the one it would be easiest to leave out.
    monkeypatch.setattr(f"{__name__}.node_binary", lambda: "/nonexistent/node")
    monkeypatch.setattr(f"{__name__}.WEB", tmp_path)
    message = _assert_failed(lambda: _probe("assert.ok(true)"))
    assert "web dependencies are not installed" in message, message

    assert "require_node_toolchain(" in inspect.getsource(_probe), (
        "the probe decides for itself what a missing toolchain means"
    )
    # Matched as source text rather than by name, so the `pytest.skip.Exception`
    # references in the helpers above are prose to this and only a call counts.
    assert not re.findall(r"pytest\.skip\(", _read(Path(__file__))), (
        "something in this file skips on its own, so it is not covered by the "
        f"{REQUIRE_NODE_ENV} guard"
    )


def test_the_strict_flag_outlives_the_suite_own_env_stripping(monkeypatch):
    """``strip_lmer_env`` removes LMER_* wholesale, and this is an LMER_* var.

    Modules that reach the `work` CLI strip the environment so they cannot touch
    the operational work repo (issue #93) — a fixture that would also have taken
    this flag with it, disarming the guard for any file that adopts both. Which
    would be a guard satisfied by silence, one level up.
    """
    from tests.conftest import strip_lmer_env

    monkeypatch.setenv(REQUIRE_NODE_ENV, "1")
    monkeypatch.setenv("LMER_REPO_HOST", "gitlab.example.com")
    strip_lmer_env(monkeypatch)
    assert os.environ.get(REQUIRE_NODE_ENV) == "1", (
        "isolating a module's environment disarms the Node guard"
    )
    assert "LMER_REPO_HOST" not in os.environ, (
        "the exemption widened to run context, which is what stripping is for"
    )


def test_the_pinned_node_is_visible_through_the_suite_own_isolation(
    tmp_path, monkeypatch
):
    """The bug that decided what "expected" can mean here.

    :func:`tests.conftest.node_binary` looks for ``setup-ui``'s Node first, via
    ``node_dir()`` —
    which resolves through ``store.platform_dir()``, which conftest repoints at a
    tmp dir for the whole session so nothing writes to the developer's real state
    dir. So inside pytest that branch never resolved: a host whose only Node was
    the pinned one skipped every executed test above, and the file that fetched
    the toolchain was the file that hid it.

    It matters beyond tidiness. "This machine can build the UI, so hold it to the
    executed tests" is only a coherent policy if the tests can see the Node that
    building the UI installed.
    """
    from lmer_platform import store
    from lmer_platform.ui_build import NODE_VERSION

    state = tmp_path / "state"
    pinned = state / store.PLATFORM_DIRNAME / "node" / NODE_VERSION / "bin" / "node"
    pinned.parent.mkdir(parents=True)
    pinned.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    # Exactly what the session fixture does, and what used to mask the above.
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "isolated"))
    monkeypatch.setattr("lmer_cli.runtime.lmer_state_dir", lambda: state)
    # No PATH Node either, so a hit can only have come from the pinned one.
    monkeypatch.setattr("shutil.which", lambda name: None)

    assert node_binary() == str(pinned)


# --- the same decisions, read from the source --------------------------------
#
# These run with no Node and no node_modules, so the guards above are never
# entirely absent from a run.

def test_the_parser_is_never_allowed_to_produce_html():
    """Layer 1, stated once, where turning it off is a one-word edit.

    ``html: true`` is the setting somebody reaches for when an agent's answer
    contains a ``<br>`` they wanted honoured, and it hands a container's output a
    direct line to the DOM.
    """
    path = _render_path()
    assert "html: false" in path, "the parser is allowed to emit raw HTML"
    assert "html: true" not in path


def test_the_rendered_html_is_allowlisted_before_it_reaches_v_html():
    """Layer 2, which this image cannot execute (no DOM, so no DOMPurify).

    So it is pinned by reading it: that the sanitiser wraps the parser rather than
    sitting beside it, that the allowlist is an allowlist, and that ``v-html`` is
    bound to nothing else. The last one is the one that actually goes wrong — a
    ``v-html`` bound to the text itself renders identically for every message
    anybody tests with, and the component is now the only place in the app where
    that edit is even possible (see the count in
    :func:`test_there_is_exactly_one_renderer_and_one_v_html`).
    """
    text = _read(MARKDOWN)
    path = _render_path()
    assert "DOMPurify.sanitize(markdown.render(text), SANITIZE)" in path, (
        "the sanitiser does not wrap the parser's output"
    )
    for key in ("ALLOWED_TAGS", "ALLOWED_ATTR", "ALLOWED_URI_REGEXP"):
        assert key in path, f"the sanitiser has no {key}"
    for opt in ("ALLOW_DATA_ATTR: false", "ALLOW_ARIA_ATTR: false"):
        assert opt in path, f"{opt} is not stated, so the default decides"

    allowed_tags = re.search(r"ALLOWED_TAGS: \[(.*?)\]", path, re.S)
    assert allowed_tags, "ALLOWED_TAGS is not a literal list"
    tags = set(re.findall(r"'([a-z0-9]+)'", allowed_tags.group(1)))
    for tag in ("script", "img", "iframe", "style", "form", "object", "embed"):
        assert tag not in tags, f"<{tag}> is on the allowlist"

    allowed_attrs = re.search(r"ALLOWED_ATTR: \[(.*?)\]", path)
    assert allowed_attrs, "ALLOWED_ATTR is not a literal list"
    attrs = set(re.findall(r"'([a-z-]+)'", allowed_attrs.group(1)))
    for attr in ("src", "style", "class", "onclick", "onerror", "srcset"):
        assert attr not in attrs, f"{attr}= is on the allowlist"

    bindings = set(re.findall(r'v-html="([^"]+)"', text))
    assert bindings == {"rendered"}, (
        f"v-html is bound to something that has not been sanitised: {bindings}"
    )
    # And `rendered` is the sanitised path, not a second name for the prop. Two
    # shapes since T46 — the compact one for a card row — so both arms are pinned
    # here: the binding is one, and each arm of it is a function that sanitises.
    # The inline arm's own guards (that it can produce no block element, and what
    # its allowlist covers) are in tests/test_platform_web_markdown_surfaces.py.
    assert re.search(
        r"const rendered = computed\(\(\) => \(props\.inline\s*"
        r"\?\s*renderMarkdownInline\(props\.text\)\s*"
        r":\s*renderMarkdown\(props\.text\)\)\)",
        text,
    ), "what v-html renders is not the output of one of the two render functions"
    assert "DOMPurify.sanitize(markdown.renderInline(text), SANITIZE_INLINE)" in path, (
        "the inline shape does not sanitise what its parse produced"
    )


def test_the_fallback_when_there_is_no_dom_is_not_the_raw_text():
    """DOMPurify hands its input back unchanged when it cannot sanitise.

    That is the failure mode that would turn layer 2 into a decoration without
    changing a single character on screen, so the component checks rather than
    assumes — and what it falls back to is escaped text, which is what these views
    showed before they rendered anything.
    """
    path = _render_path()
    assert "DOMPurify.isSupported" in path, (
        "a DOM-less DOMPurify would pass the parser's output straight through"
    )
    assert "markdown.utils.escapeHtml(text)" in path, (
        "the fallback is not escaped, so it is worse than not rendering at all"
    )


def test_the_render_path_is_the_one_the_tests_run():
    """The extraction this whole file leans on.

    If the markers move, or the block grows something that needs Vue or a DOM, the
    executed tests stop testing the component and start testing a fragment — or
    skip. Neither is visible from a passing run, so the fence is checked directly.
    """
    text = _read(MARKDOWN)
    assert text.count(RENDER_PATH_START) == 1
    assert text.count(RENDER_PATH_END) == 1
    path = _render_path()
    assert path.index("new MarkdownIt(") < path.index("function renderMarkdown"), (
        "the block no longer contains the whole path"
    )
    # Comments stripped: they are allowed to *talk* about window.opener, which is
    # in fact what the rel attribute is there for.
    code = re.sub(r"^\s*//.*$", "", path, flags=re.M)
    for forbidden in ("ref(", "computed(", "props.", "document.", "window."):
        assert forbidden not in code, (
            f"the extracted block reaches for {forbidden}, which Node has not got"
        )


# --- what markdown must not take away ----------------------------------------

def test_a_fence_scrolls_sideways_and_never_reflows():
    """A wrapped command is a command someone pastes half of.

    Fences in this text are shell lines and diffs, and the operator's use for them
    is to copy one into a terminal. Reflowed to the width of a phone, a ``git``
    invocation reads as two commands and the second one is nonsense.

    The ``white-space`` is restated on the inner ``<code>`` for a reason that is
    invisible until it happens: style.css sets ``nowrap`` on every ``<code>``,
    which inside a ``<pre>`` collapses the newlines out and leaves a whole script
    on one line.

    This is also the rule that made the shared unit a *component*: as a plain
    helper the renderer would have travelled to a new view and this would not.
    """
    pre = _rule(".markdown :deep(pre)")
    assert "white-space: pre" in pre, "a fence reflows to the width of the bubble"
    assert "overflow-x: auto" in pre, "a wide fence widens the bubble instead"
    assert "overflow-wrap: normal" in pre, (
        "the root sets `anywhere`, which would break a long path mid-token"
    )

    code = _rule(".markdown :deep(pre code)")
    assert "white-space: pre" in code, (
        "style.css's `nowrap` on <code> is inherited and eats the newlines"
    )
    assert "white-space: nowrap" in _read(STYLE), (
        "the rule this works around is gone; the restatement above is now only "
        "belt, and this test should say so"
    )


def test_injected_markup_is_actually_reachable_by_the_stylesheet():
    """Scoped CSS and ``v-html`` do not meet without ``:deep()``.

    Vue tags the elements it compiles with the scope attribute; markup handed to
    ``v-html`` has none, so every rule aimed at it matches nothing — silently, and
    the fence rule is not one to lose silently. Every rule that reaches *inside*
    the rendered block is therefore written through ``:deep()``, and one that
    forgets is undetectable short of looking at it in a browser.

    The root element is the exception and the reason the selectors are checked
    rather than counted: it *is* compiled from the template, so it needs no
    ``:deep()`` — and it is the only place a bare ``.markdown`` rule is right.
    """
    style = _read(MARKDOWN)
    style = style[style.index("<style"):]
    selectors = re.findall(r"^\.markdown[^{,]*", style, re.M)
    assert selectors, "nothing styles the rendered markdown at all"
    bare = [s.strip() for s in selectors if ":deep(" not in s]
    assert bare == [".markdown"], (
        f"these cannot reach injected markup: {bare}"
    )
    # The root rule is for the element the template compiles, so what it carries
    # has to be inheritable or its own; `anywhere` is both.
    assert "overflow-wrap: anywhere" in _rule(".markdown"), (
        "a long path or URL in agent output widens the page on a phone"
    )


def test_rendering_is_memoised_because_the_parent_redraws_every_second():
    """Otherwise this is a parse and a sanitise of the whole pane, once a second.

    ``now`` is a prop of every view here and App.vue ticks it so the relative times
    stay honest; every tick re-renders those components. A ``computed`` on the prop
    covers that much — the text has not changed, so nothing re-parses — and the
    module-level cache covers what it cannot: a pane that is destroyed and rebuilt
    (a run detail closed and reopened, older turns paged in) re-mounts every child
    with a fresh computed each. Forty turns of tool output, on a phone, per open.
    Nothing about it looks wrong; it just gets hot and slow on the device least
    able to say so.
    """
    text = _read(MARKDOWN)
    assert "renderCache" in text and "RENDER_CACHE_MAX" in text, (
        "every re-mount re-parses every message"
    )
    assert re.search(r"const rendered = computed\(\(\) => \(props\.inline", text), (
        "the render runs on every re-render of the parent, not on a change of text"
    )
    render = _render_path()
    assert "renderCache.get(text)" in render, "the cache is written but never read"
    assert "renderCache.clear()" in render, (
        "an unbounded cache of message bodies is a leak in a view that is open "
        "for hours"
    )
    # The compact shape has a cache of its own, because the same string renders to
    # different markup in the two of them — and a row is redrawn by the same tick.
    assert "inlineCache.get(text)" in render, "the inline shape re-parses every tick"
    assert "inlineCache.clear()" in render, "the inline cache is unbounded"
    # The reason, taken from the component that supplies it rather than restated.
    tick = re.search(
        r"setInterval\(.{0,80}?now\.value = Date\.now\(\).{0,40}?,\s*(\d+)\)",
        _read(APP), re.S,
    )
    assert tick and int(tick.group(1)) <= 1000, (
        "App.vue no longer ticks `now` at least once a second; if the tick is "
        "gone this cache is only an optimisation and this test should say so"
    )


# --- dependencies -------------------------------------------------------------

def test_the_renderer_is_bundled_and_brings_no_asset_with_it():
    """A CDN reference is a blank chat pane on the network this UI is for.

    xterm needed a stylesheet out of its package; these two need nothing but the
    JavaScript, and an asset import is how a font or an icon sheet would arrive in
    ``dist/`` — the thing main.js's first comment exists to prevent.
    """
    text = _read(MARKDOWN)
    assert "import DOMPurify from 'dompurify'" in text
    assert "import MarkdownIt from 'markdown-it'" in text
    for statement in re.findall(r"^import .*$", text, re.M):
        assert "://" not in statement, f"a module is fetched at runtime: {statement}"
        assert not re.search(r"\.(css|woff2?|ttf|eot|png|svg)'", statement), (
            f"an asset comes with the renderer: {statement}"
        )
