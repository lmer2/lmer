"""Which surfaces render an agent's words, and in which shape (issue #141, T46).

:mod:`tests.test_platform_web_markdown` keeps the renderer honest — two layers, one
``v-html``, no image, no scheme nobody allowed. This file keeps the *inventory*
honest: which of the many places this UI shows text produced inside a container
turn it into markup, which deliberately do not, and — the decision this slice
adds — which of them may only ever produce inline markup.

The rule
--------
Agent prose is rendered. On a surface that is a **line** — a card row, an
event-log entry, the attention note that says why a run needs a human, the clause
a session closed a question with — it is rendered in the compact mode, which is a
different *parse* rather than a filter: ``renderInline`` never builds a block
construct, so a ``#`` stays a ``#`` and a fence stays backticks. A heading, a list
or a ``<pre>`` in one of those rows is not a formatting nicety gone wrong; it is
the row becoming three rows tall, and the fleet and the run header being read at a
glance is the thing that pays for it.

Three decisions that are *not* the rule, and each is pinned here because each
looks like an oversight from the outside:

- **a title is not prose** (T65). It is a label, the daemon has already collapsed
  it to one line, and both listings and the detail header interpolate it;
- **the fleet card renders nothing at all**. The renderer is a chunk of its own so
  the landing screen does not pay for it (T42), and the landing screen *is* the
  fleet: thirty rows would fetch markdown-it and DOMPurify on first paint to
  italicise a clause in one dimmed sentence. The same note is rendered a tap
  later, in the detail header, where the chunk is already earning its place;
- **the terminal stays raw**. What is in it is PTY bytes — escape sequences and a
  TUI's own drawing — not prose, and an emulator is not a document.

And the pinned property from T38 holds unchanged: **operator-authored text is
never rendered**, anywhere. What you typed went to a session as bytes, and a line
that quietly ate a pair of asterisks would be misreporting what the run got.

The inventory below is the artefact this file exists for. A ``<Markdown>`` that is
not in it fails the sweep, which is the point: adding a render path is a decision
about a surface, and the decision is written down here.

Source-level for the placement, executed under Node for the inline parse — the
same split, and the same probe, :mod:`tests.test_platform_web_markdown` uses.
"""

import json
import re
from pathlib import Path

from tests.test_platform_web_markdown import HOSTILE, _probe

WEB = Path(__file__).resolve().parent.parent / "web"
COMPONENTS = WEB / "src" / "components"

#: Surfaces that render agent prose *inline*: one line of a card, a row or a
#: sentence, where a block element would break the layout it sits in. Keyed by
#: component, valued by the expression each ``<Markdown>`` is bound to.
INLINE_SURFACES = {
    "RunDetail.vue": {"run.attention.note", "run.goal", "event.note"},
    "AskChannel.vue": {"closureReason(entry)"},
    "AskHistory.vue": {"closureReason(entry)"},
}

#: Surfaces that render it whole: a bubble or a card, written to be read, where a
#: list of alternatives should arrive as a list.
BLOCK_SURFACES = {
    "Chat.vue": {"message.text"},
    "AskBox.vue": {"question.text"},
    "AskChannel.vue": {"entry.text"},
    "AskHistory.vue": {"entry.text"},
    "AnswerBox.vue": {"note"},
    "RunMeta.vue": {"record.description"},
}

#: Agent-written text that is deliberately shown as text, and why. Not a list of
#: everything left over — these are the ones somebody will reach for next.
VERBATIM_SURFACES = {
    # A label, collapsed to one line by the daemon and bounded at 120 characters
    # (T65). Both listings and the detail header show it as text.
    "run.title": ("RunCard.vue", "RunNav.vue", "RunDetail.vue", "RunMeta.vue"),
    # The fleet row's one piece of prose, and the argument is the bundle's: see
    # the module docstring and the comment beside it in the card.
    "run.attention.note": ("RunCard.vue",),
}

#: Every component allowed to hold no renderer at all *and* show agent text: the
#: fleet card, the drawer row and the emulator. Named, so that adding a fourth is
#: an edit to this list.
NO_RENDERER = ("RunCard.vue", "RunNav.vue", "Terminal.vue")


def _read(path):
    return path.read_text(encoding="utf-8")


def _component(name):
    return _read(COMPONENTS / name)


def _without_comments(text):
    """*text* with its markup and line comments removed.

    These components explain themselves at length and several of them name the
    renderer in prose — the class is "on the ``<Markdown>``", says the one that
    styles it — and a comment renders nothing.
    """
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    # The stylesheet's block comments too: the rule that styles a rendered turn is
    # documented next to the element it reaches, by name.
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)


def _elements(text):
    """Every ``<Markdown …>`` element in *text*, whole."""
    return [
        " ".join(match.split())
        for match in re.findall(r"<Markdown\b[^>]*/?>", _without_comments(text), re.S)
    ]


def _bound_text(element):
    """What one ``<Markdown>`` renders, from its ``:text`` binding."""
    match = re.search(r':text="([^"]+)"', element)
    assert match, f"a renderer with nothing bound to it: {element}"
    return match.group(1)


def _renderers():
    """The whole inventory, read out of the components: ``{file: {expr: inline}}``."""
    found = {}
    for path in sorted(COMPONENTS.glob("*.vue")):
        for element in _elements(_read(path)):
            found.setdefault(path.name, {})[_bound_text(element)] = bool(
                re.search(r"\binline\b", element)
            )
    return found


# --- the inventory ------------------------------------------------------------

def test_every_rendered_surface_is_in_the_inventory():
    """The sweep, and the reason this file is not a list of assertions.

    A new view that renders agent words is exactly the thing nobody remembers to
    write a test for — and the question it has to answer ("can this surface host a
    block element?") is not one the author of a fifth consumer will necessarily
    ask. So the components are scanned and the answer has to already be here.
    """
    declared = {}
    for table, inline in ((INLINE_SURFACES, True), (BLOCK_SURFACES, False)):
        for name, bindings in table.items():
            for binding in bindings:
                declared.setdefault(name, {})[binding] = inline

    found = _renderers()
    assert found == declared, (
        "the rendered surfaces have moved away from the inventory in this file.\n"
        f"  in the components: {json.dumps(found, indent=2, sort_keys=True)}\n"
        f"  written down here: {json.dumps(declared, indent=2, sort_keys=True)}\n"
        "Add the surface to INLINE_SURFACES if it is a line (a row, a log entry, "
        "an alert) or to BLOCK_SURFACES if it is a bubble or a card."
    )


def test_a_line_renders_no_block_element():
    """The compact mode, asserted where it is asked for.

    ``inline`` is a boolean prop, so its absence is silent and looks identical
    until an agent writes a list into a note — at which point the run header is
    three lines taller and the answer box is off the bottom of a phone.
    """
    for name, bindings in INLINE_SURFACES.items():
        text = _component(name)
        for binding in bindings:
            element = next(
                (one for one in _elements(text) if _bound_text(one) == binding), None,
            )
            assert element, f"{name} no longer renders {binding}"
            assert re.search(r"\binline\b", element), (
                f"{name} renders {binding} as a document: {element}"
            )


def test_a_card_renders_the_whole_thing():
    """The other half, and it is not the default either.

    A question that lays out three alternatives is *written* as a list, and the
    compact mode would show the operator the hyphens. Bubbles and cards have the
    room; inline there would be a loss with nothing bought for it.
    """
    for name, bindings in BLOCK_SURFACES.items():
        text = _component(name)
        for binding in bindings:
            element = next(
                (one for one in _elements(text) if _bound_text(one) == binding), None,
            )
            assert element, f"{name} no longer renders {binding}"
            assert not re.search(r"\binline\b", element), (
                f"{name} renders {binding} compactly, so a list of alternatives "
                f"arrives as one paragraph of hyphens: {element}"
            )


def test_a_run_title_is_never_rendered_anywhere():
    """T65's decision, kept while everything around it started rendering.

    The daemon collapses a title to one line and bounds it; what a listing or a
    heading needs from it is a name, not a document. This is the decision most
    likely to be undone by accident *now*, because the compact mode makes it look
    safe — and a bold half-sentence in a heading is exactly what it would be.
    """
    for name in VERBATIM_SURFACES["run.title"]:
        text = _component(name)
        for element in _elements(text):
            assert "title" not in _bound_text(element), (
                f"{name} renders a title as markup: {element}"
            )
    assert "{{ run.title || run.label }}" in _component("RunCard.vue")
    assert "{{ record.title }}" in _component("RunMeta.vue")


def test_the_fleet_row_holds_no_renderer_at_all():
    """The bundle decision, in the file it is a decision about.

    ``RunCard.vue`` is in the entry chunk: it is the landing screen. One
    ``<Markdown>`` in it — even lazily imported, even inline — fetches the
    renderer chunk on the first paint of a list of runs, which is precisely the
    59 kB gzipped T42 took off that screen. What is given up is small and stated
    beside the note: the row shows the asterisks, and the same note is rendered
    one tap later in the detail header.
    """
    for name in NO_RENDERER:
        text = _component(name)
        assert "<Markdown" not in text, (
            f"{name} renders markdown; the fleet screen would fetch the renderer "
            "chunk on first paint, and an emulator is not a document"
        )
        assert "import('./Markdown.vue')" not in text, (
            f"{name} pulls in the renderer chunk"
        )
        for injecting in ("v-html", "innerHTML"):
            assert injecting not in text, f"{name} injects markup of its own"
    # And the row still shows the note — unrendered is not the same as dropped.
    assert "{{ run.attention.note }}" in _component("RunCard.vue")
    # The decision is written where the next person will be standing when they
    # reach for it, not only in this file.
    assert "T46" in _component("RunCard.vue"), (
        "nothing beside the note says why it is the one piece of agent prose in "
        "this app that is not rendered"
    )


def test_the_note_the_fleet_row_shows_plainly_is_rendered_in_the_detail_view():
    """The other half of that trade, and it is what makes it a trade.

    If the note were plain in both places the decision would just be a feature
    nobody built. It is rendered the moment the operator enters the run, above the
    tabs, where the chunk is worth its round trip.
    """
    detail = _component("RunDetail.vue")
    assert 'run.attention.note' in detail
    element = next(
        one for one in _elements(detail) if _bound_text(one) == "run.attention.note"
    )
    assert "inline" in element
    assert detail.index("<Markdown") < detail.index('<v-tabs v-model="tab"'), (
        "the rendered note is behind a tab, so a remembered tab hides the reason "
        "the run needs a human"
    )


def test_what_the_operator_wrote_is_still_shown_as_they_wrote_it():
    """T38's pinned property, extended to the surfaces this slice added.

    A reply went to the session as bytes. Rendering it would mean a view claiming
    the operator typed something they did not — and in the record, which is the
    view that exists to say what was actually said.
    """
    for name in ("AskChannel.vue", "AskHistory.vue"):
        text = _component(name)
        assert "you: {{ entry.answer.text }}" in text, (
            f"{name} renders the operator's own reply"
        )
        for element in _elements(text):
            assert "answer" not in _bound_text(element), (
                f"{name} renders a reply as markup: {element}"
            )
    # An option is literally the text a tap puts in the box, so the record shows
    # the same characters the dock would have sent.
    history = _component("AskHistory.vue")
    assert "it offered: {{ options(entry) }}" in history
    assert "<v-chip" not in history, (
        "the record offers the options as chips, which reads as a menu on a view "
        "that sends nothing"
    )


def test_every_new_consumer_still_defers_the_chunk():
    """Source-level here, and against the built output in the bundle tests.

    This one names the file. "markdown-it is in app.js" does not say which of a
    dozen components put it there, and a static import from any of them puts it
    back for all of them.
    """
    for name in sorted(set(INLINE_SURFACES) | set(BLOCK_SURFACES)):
        text = _component(name)
        assert "import('./Markdown.vue')" in text, (
            f"{name} renders agent words without deferring the renderer"
        )
        assert not re.search(r"^\s*import\s+.*from\s+'\./Markdown\.vue'", text, re.M), (
            f"{name} imports the renderer statically, which puts markdown-it and "
            "DOMPurify back in the chunk the fleet view loads"
        )


# --- the compact parse, executed ---------------------------------------------

def test_the_compact_mode_cannot_produce_a_block_element():
    """The property the whole rule rests on, and it is a parse rather than a strip.

    Every block construct markdown has, put through the inline path: what comes
    back has to be free of block tags *and* still contain the text, because a
    renderer that dropped the heading would be worse than one that rendered it —
    an attention note would silently lose its first line.
    """
    block_sources = [
        "# heading",
        "## smaller\n\nand a paragraph",
        "- one\n- two",
        "1. first\n2. second",
        "> quoted",
        "---",
        "| a | b |\n| --- | --- |\n| 1 | 2 |",
        "```sh\nrm -rf -- /tmp/x\n```",
        "    indented code\n",
        "para one\n\npara two",
    ]
    body = "\n".join([
        f"const sources = {json.dumps(block_sources)}",
        "const block = /<\\/?(p|h[1-6]|ul|ol|li|pre|blockquote|hr|table|thead|"
        "tbody|tr|th|td)\\b/i",
        "for (const source of sources) {",
        "  const html = markdown.renderInline(source)",
        "  assert.ok(!block.test(html), `a block element survived: ${html}`)",
        "}",
        # The text is still there: the markers stay as characters rather than the
        # line being swallowed.
        "assert.match(markdown.renderInline('# heading'), /# heading/)",
        "assert.match(markdown.renderInline('- one\\n- two'), /- one/)",
        "assert.match(markdown.renderInline('- one\\n- two'), /- two/)",
        # And what inline markdown *is* for still works.
        "assert.match(markdown.renderInline('a **bold** word'), /<strong>bold<\\/strong>/)",
        "assert.match(markdown.renderInline('run `git push`'), /<code>git push<\\/code>/)",
        "assert.match(markdown.renderInline('[x](https://ok.invalid/p)'), /<a /)",
    ])
    _probe(body)


def test_the_compact_allowlist_covers_everything_that_parse_can_emit():
    """The seam between the two layers, in the direction that goes quiet.

    A tag the inline parse emits that the inline allowlist has not been told about
    does not fail — it disappears, and an emphasised word in an attention note
    becomes unmarked text with nothing saying when it stopped working. Run the
    other way too: the allowlist must not have grown a block tag, because that is
    the rule this mode exists to enforce arriving back as a possibility.
    """
    corpus = "\n\n".join([
        "**bold**, *em*, ~~struck~~, `code`",
        "[link](https://ok.invalid/x) and https://bare.invalid/y",
        "a line\nand the next one",
        "# not a heading here\n- not a list either",
    ] + HOSTILE)
    body = "\n".join([
        f"const html = markdown.renderInline({json.dumps(corpus)})",
        "const tags = new Set([...html.matchAll(/<\\/?([a-z][a-z0-9]*)/gi)]"
        ".map((m) => m[1].toLowerCase()))",
        "const allowed = new Set(SANITIZE_INLINE.ALLOWED_TAGS)",
        "for (const tag of tags) {",
        "  assert.ok(allowed.has(tag), `the sanitiser would silently drop <${tag}>`)",
        "}",
        "assert.ok(tags.size > 3, `the corpus stopped exercising the parse: "
        "${[...tags]}`)",
        # No block tag may be allowlisted here, whatever the parse does.
        "for (const tag of ['p', 'h1', 'ul', 'ol', 'li', 'pre', 'blockquote',",
        "                   'table', 'tr', 'td', 'hr']) {",
        "  assert.ok(!allowed.has(tag), `<${tag}> is allowed in the compact mode`)",
        "}",
        # The rest of the policy is shared rather than restated, which is the
        # reason this is a second parse and not a second renderer.
        "for (const key of ['ALLOWED_URI_REGEXP', 'ALLOW_DATA_ATTR',",
        "                   'ALLOW_ARIA_ATTR']) {",
        "  assert.deepEqual(SANITIZE_INLINE[key], SANITIZE[key],",
        "    `the compact mode has its own ${key}`)",
        "}",
        "assert.deepEqual(SANITIZE_INLINE.ALLOWED_ATTR, SANITIZE.ALLOWED_ATTR)",
    ])
    _probe(body)


def test_hostile_text_is_as_inert_on_a_card_row_as_it_is_in_a_bubble():
    """The same corpus, through the other parse.

    An attention note is agent output like any other — the daemon writes it around
    what the run said — so the compact path gets no weaker a test than the block
    one. Layer 1 is what is executed (``html: false`` means a tag in the text is
    never read as a tag); DOMPurify needs a DOM this image has not got, which is
    also why the fallback below has to be inert on its own.
    """
    body = "\n".join([
        f"const hostile = {json.dumps(HOSTILE)}",
        "const never = ['script', 'iframe', 'svg', 'img', 'style', 'base', 'object']",
        "for (const source of hostile) {",
        "  for (const html of [markdown.renderInline(source),",
        "                      renderMarkdownInline(source)]) {",
        "    const where = `${JSON.stringify(source)} rendered as "
        "${JSON.stringify(html)}`",
        "    for (const [, tag] of html.matchAll(/<\\/?([a-z0-9]+)/gi)) {",
        "      assert.ok(!never.includes(tag.toLowerCase()), `<${tag}> is live in "
        "${where}`)",
        "    }",
        "    for (const [, attr] of html.matchAll(/\\s([a-zA-Z:-]+)\\s*=\\s*\"/g)) {",
        "      assert.ok(!attr.toLowerCase().startsWith('on'), `${attr}= is live in "
        "${where}`)",
        "      assert.ok(!/^(src|srcset|formaction|action)$/i.test(attr),",
        "        `${attr}= is live in ${where}`)",
        "    }",
        "    for (const [, url] of html.matchAll(/href=\"([^\"]*)\"/gi)) {",
        "      assert.match(url, /^(?:https?:|mailto:)/i,",
        "        `a browser would follow ${url}: ${where}`)",
        "    }",
        "  }",
        "}",
        # Escaped, not swallowed, on this path too — and with no <p> wrapper, which
        # is what makes it safe to drop into the middle of somebody's sentence.
        "const fallback = renderMarkdownInline('<script>alert(1)</script>')",
        "assert.match(fallback, /&lt;script&gt;alert\\(1\\)&lt;\\/script&gt;/, fallback)",
        "assert.ok(!/<p>/.test(fallback), `the compact fallback is a block: "
        "${fallback}`)",
        "assert.equal(DOMPurify.isSupported, false, 'this probe has a DOM now — "
        "sanitising is no longer untested and the docstrings should say so')",
    ])
    _probe(body)


def test_the_two_shapes_do_not_share_a_cache_entry():
    """The same string renders differently in the two of them.

    One cache keyed on the text alone would hand a card row whatever a bubble
    rendered first — a heading inside an event-log line, arriving only when the
    same text happened to be shown in both places, which is exactly the kind of
    bug that is impossible to reproduce from a report.
    """
    body = "\n".join([
        "const source = '# heading'",
        "const block = markdown.render(source)",
        "const inline = markdown.renderInline(source)",
        "assert.notEqual(block, inline)",
        "assert.match(block, /<h1>/)",
        "assert.ok(!/<h1>/.test(inline), inline)",
        # The caches are separate objects, so neither can answer for the other.
        "assert.ok(renderCache !== inlineCache, 'one cache serves both shapes')",
    ])
    _probe(body)
