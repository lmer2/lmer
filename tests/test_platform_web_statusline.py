"""The status strip, on the three surfaces that draw it (issue #248).

Most of what is kept here is *rendered* rather than read, because the two things
that can go wrong are both invisible to a source-level guard: a run with nothing
published must draw **nothing** (every dormant run and every harness but claude
is in that case, so an empty frame or a `0%` meter would be on most rows and
wrong on all of them), and a reading must never present itself as current, which
is a computed on a clock.

So the component is mounted through Vue's **server** renderer, as
``tests.test_platform_web_sketch`` mounts the sketch pad: no browser, but
``setup()`` runs and the markup is real. The pure helpers run under Node beside
it, and only what no single component can show — that the three call sites pass
the right field, at the right density — is pinned by reading the sources.
"""

import json
import re
import subprocess
from pathlib import Path

from tests.conftest import node_binary, require_node_toolchain

WEB = Path(__file__).resolve().parent.parent / "web"
COMPONENTS = WEB / "src" / "components"

#: The three surfaces that draw the strip. Spelled as literals rather than
#: composed from a loop variable so the gate's text-diff scanner can classify
#: what this file reads (.lmer/gate-check.yaml explains what it can model).
RUN_CARD = COMPONENTS / "RunCard.vue"
RUN_DETAIL = COMPONENTS / "RunDetail.vue"
CHAT_PANE = COMPONENTS / "AssistantChat.vue"
CALL_SITES = (RUN_CARD, RUN_DETAIL, CHAT_PANE)

#: A document the daemon would serve for a claude session with everything to
#: say: context, two windows (one with a reset), and an effort level.
STATUS = {
    "harness": "claude",
    "updated": "2026-09-03T14:31:07Z",
    "age_seconds": 3,
    "context": {"percent": 9},
    "limits": [
        {"key": "five_hour", "label": "5h", "percent": 24,
         "resets_at": "2026-09-03T16:00:00Z"},
        {"key": "seven_day", "label": "7d", "percent": 61},
    ],
    "effort": {"level": "high"},
}

NOW = "Date.parse('2026-09-03T14:31:10Z')"

_RENDER_PROBE = """
import { readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { parse, compileScript } from '@vue/compiler-sfc'

const path = 'src/components/StatusStrip.vue'
const { descriptor } = parse(readFileSync(path, 'utf8'), { filename: path })
const compiled = compileScript(descriptor, { id: 'probe', inlineTemplate: true })
// A data: URL module cannot resolve a bare or relative specifier, so the
// compiled component's imports are rewritten to what this rig resolved them to.
const source = compiled.content
  .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
  .replace(/from ['"]\\.\\.\\/format\\.js['"]/g,
    `from '${pathToFileURL('src/format.js').href}'`)
  .replace(/from ['"]\\.\\.\\/preferences\\.js['"]/g,
    `from '${pathToFileURL('src/preferences.js').href}'`)
const component = (await import(
  `data:text/javascript;base64,${Buffer.from(source, 'utf8').toString('base64')}`
)).default

const app = createSSRApp({ render: () => h(component, %s) })
// Vuetify's own components are not registered in this rig; left undeclared they
// warn and render as unknown tags, which would make the meter unassertable.
app.config.compilerOptions.isCustomElement = (tag) => tag.startsWith('v-')
app.config.warnHandler = () => {}
const html = await renderToString(app)
// The comments the SFC carries are rendered too; they would match every
// assertion about the strip's own text.
console.log(JSON.stringify({ html: html.replace(/<!--(?!\\]|\\[|v-if)[\\s\\S]*?-->/g, '') }))
"""


def _render(props, *, details=False):
    """*props* rendered by the real component, comments stripped."""
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")
    browser = "globalThis.window = {localStorage: {getItem: () => 'on'}};\n" if details else ""
    result = subprocess.run(
        [node, "--input-type=module", "-e", browser + _RENDER_PROBE % props],
        capture_output=True, text=True, timeout=180, cwd=str(WEB),
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout)["html"]


def _props(status=STATUS, *, compact=False):
    body = "null" if status is None else json.dumps(status)
    return f"{{ status: {body}, now: {NOW}, compact: {str(compact).lower()} }}"


def _text(html):
    """The rendered text, with markup and Vue's anchors taken out."""
    stripped = re.sub(r"<[^>]+>", " ", re.sub(r"<!--.*?-->", "", html, flags=re.S))
    return " ".join(stripped.split())


# --- rendered: what the operator actually sees -------------------------------

class TestTheCompactLine:
    """The fleet row's density: one dimmed run of text in the row's facts line,
    which is the treatment `idle` already has there."""

    def test_every_segment_in_the_documents_own_order(self):
        """The harness orders its windows because only it knows which matters
        most, and the row prints them in that order."""
        assert _text(_render(_props(compact=True))) == (
            "ctx 9% · 5h 24% · 7d 61% · eff high"
        )

    def test_it_is_one_element_carrying_no_styling_of_its_own(self):
        """It inherits the facts line's size and emphasis; a class here is how
        it starts drifting away from the facts beside it, and a chip group is
        how it starts competing with the attention signal."""
        html = _render(_props(compact=True))
        assert html.count("<span") == 1
        assert "class=" not in html
        assert "v-chip" not in html and "v-progress" not in html

    def test_the_reading_is_dated_in_the_tooltip(self):
        """A percentage with no date behind it is the one thing an operator
        cannot check."""
        assert 'title="2026-09-03T14:31:07Z"' in _render(_props(compact=True))


class TestTheMeters:
    """The run drawer's and the chat pane's density: read, not scanned."""

    def test_a_meter_for_every_proportion_and_none_for_effort(self):
        """A full bar under `eff high` would read as an effort budget spent."""
        html = _render(_props())
        assert html.count("<v-progress-linear") == 3
        assert 'model-value="9"' in html
        assert 'model-value="24"' in html
        assert 'model-value="61"' in html

    def test_the_meters_are_the_neutral_tone_never_the_urgency_ramp(self):
        """Colour in this app is urgency plus one accent, and no threshold here
        has been decided to be urgent (#248 shows the numbers; the spawn-time
        warning the issue originally asked for is not in its scope). A red bar
        at 90% would be this component inventing that policy."""
        html = _render(_props())
        assert html.count('color="tone-idle"') == 3
        for painted in ("error", "warning", "success", "primary"):
            assert f'color="{painted}"' not in html

    def test_every_segment_is_labelled_and_valued(self):
        assert _text(_render(_props())) == "ctx 9% 5h 24% 7d 61% eff high"

    def test_a_windows_reset_is_a_tooltip_on_that_window(self):
        """Phrased forwards, and only on the window it belongs to — inline it
        would double the width of the one segment scanned for a number."""
        html = _render(_props())
        assert 'title="resets in 1h — 2026-09-03T14:31:07Z"' in html
        assert html.count("resets in") == 1


class TestNothingToShow:
    def test_no_status_renders_nothing(self):
        """Every dormant run, every older image and every harness but claude is
        in this case, so an empty frame here would be wrong on most rows of the
        fleet."""
        for compact in (True, False):
            html = _render(_props(None, compact=compact))
            assert _text(html) == ""
            assert "<span" not in html and "<div" not in html

    def test_a_document_with_no_facts_renders_nothing(self):
        """A session that publishes before its first API response says who it
        is and nothing else; that is not a strip."""
        published = {"harness": "claude", "updated": "2026-09-03T14:31:07Z",
                     "age_seconds": 3}
        assert _text(_render(_props(published))) == ""

    def test_a_window_with_no_percentage_is_not_drawn(self):
        published = {**STATUS, "limits": [{"key": "five_hour", "label": "5h"}]}
        assert _text(_render(_props(published, compact=True))) == "ctx 9% · eff high"


class TestAnOldReading:
    """The property a source guard cannot see: a session stops publishing when
    it stops rendering, so the strip has to date an old reading rather than
    presenting it as current."""

    STALE = {**STATUS, "age_seconds": 600, "updated": "2026-09-03T14:21:07Z"}

    def test_the_compact_line_says_how_old_it_is(self):
        assert _text(_render(_props(self.STALE, compact=True))) == (
            "ctx 9% · 5h 24% · 7d 61% · eff high (as of 10m ago)"
        )

    def test_the_meters_say_it_on_a_line_of_their_own(self):
        """It qualifies all of them, so it sits under all of them."""
        assert "as of 10m ago" in _text(_render(_props(self.STALE)))

    def test_a_fresh_reading_says_nothing(self):
        """Under a minute is "now" for a status line — claude re-renders on
        every change, so dating a fresh reading is noise."""
        assert "as of" not in _text(_render(_props()))

    def test_a_reading_with_no_age_says_so_rather_than_nothing(self):
        """The daemon refuses to serve an undateable document, so this is the
        belt to that braces: "no caption" is the one answer it must not give,
        because that is what a three-second-old reading looks like.
        """
        published = {**STATUS, "age_seconds": None}
        assert _text(_render(_props(published, compact=True))) == (
            "ctx 9% · 5h 24% · 7d 61% · eff high (age unknown)"
        )

    def test_the_caption_is_measured_by_one_clock_and_it_is_the_daemons(self):
        """Gating on the daemon's ``age_seconds`` and labelling from the
        browser's clock printed "as of 0s ago" from a rule that only fires past
        60s. Pinned by moving the viewer's clock alone.
        """
        drawn = {
            _text(_render(f"{{ status: {json.dumps(self.STALE)}, "
                          f"now: Date.parse('{stamp}') }}"))
            for stamp in ("2026-09-03T14:31:10Z",   # the daemon's own moment
                          "2026-09-03T09:00:00Z",   # hours slow
                          "2026-09-04T09:00:00Z")   # a day fast
        }
        assert len(drawn) == 1, drawn
        assert "as of 10m ago" in drawn.pop()


# --- executed: the helpers the three surfaces share --------------------------

_HELPER_PROBE = """
import { resetsLabel, statusAge, statusSegments } from '%s/src/format.js'
// `resetsLabel` is the only helper here that still takes a clock — `statusAge`
// is measured entirely by the daemon (see format.js).
const now = Date.parse('2026-09-03T14:31:10Z')
const status = %s
console.log(JSON.stringify({
  segments: statusSegments(status),
  age: statusAge(status),
  resets: [
    resetsLabel('2026-09-03T16:00:00Z', now),
    resetsLabel('2026-09-03T13:00:00Z', now),
    resetsLabel('not a date', now),
    resetsLabel(null, now),
  ],
}))
"""


def _helpers(status=STATUS):
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")
    script = _HELPER_PROBE % (WEB.as_uri(), json.dumps(status))
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=120, cwd=str(WEB),
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout)


class TestTheHelpers:
    def test_a_window_without_a_label_is_named_by_its_key(self):
        """The label is the harness's vocabulary and may simply be absent; the
        key is the fallback, never a blank segment."""
        published = {**STATUS, "limits": [{"key": "monthly_quota", "percent": 12}]}
        labels = [s["label"] for s in _helpers(published)["segments"]]
        assert labels == ["ctx", "monthly_quota", "eff"]

    def test_a_harness_with_its_own_window_vocabulary_renders(self):
        """The whole point of the document's shape: nothing in the UI knows
        claude's window names either."""
        published = {
            "harness": "opencode", "updated": "2026-09-03T14:31:07Z",
            "age_seconds": 3,
            "limits": [{"key": "monthly_quota", "label": "mo", "percent": 12}],
            "effort": {"level": "thorough"},
        }
        assert [s["text"] for s in _helpers(published)["segments"]] == [
            "mo 12%", "eff thorough",
        ]

    def test_an_unusable_percentage_drops_its_segment(self):
        """Typed, not coerced: `Number(null)` is 0, so a coercing check renders
        a session that reported nothing as `ctx 0%` — the one answer the
        document's "absent means not reported" shape exists to avoid. Found by
        this test, on the first cut of the helper."""
        published = {**STATUS, "context": {"percent": None},
                     "limits": [{"key": "five_hour", "label": "5h", "percent": "24"},
                                {"key": "seven_day", "label": "7d", "percent": False}]}
        assert [s["key"] for s in _helpers(published)["segments"]] == ["effort"]

    def test_a_reset_that_has_passed_is_not_a_countdown(self):
        """The harness drops a window once its reset goes by, and "resets in
        0s" would be a countdown to something that already happened."""
        assert _helpers()["resets"] == ["resets in 1h", None, None, None]

    def test_nothing_published_is_no_segments_and_no_age(self):
        answer = _helpers(None)
        assert answer["segments"] == [] and answer["age"] is None


# --- read: what one component cannot show ------------------------------------

class TestTheCallSites:
    """Which field each surface passes, and at which density. A component that
    renders correctly for props nobody hands it is the failure this catches."""

    def test_the_fleet_row_passes_the_rows_own_field_compactly(self):
        tag = _status_strip_tag(RUN_CARD.read_text())
        assert re.search(r':status="run\.statusline"', tag)
        assert "compact" in tag, "the row must not draw meters"

    def test_the_run_drawer_passes_the_same_field_with_meters(self):
        source = RUN_DETAIL.read_text()
        assert re.search(
            r'<StatusStrip\s+:status="run\.statusline"\s+:now="now"', source
        ), "the drawer must draw the run's own status, at the meter density"
        assert "compact" not in _status_strip_tag(source)

    def test_the_chat_pane_passes_the_assistants_own_status(self):
        """uber lmer's session is not one of the runs, so its strip comes off
        the route that pane already polls rather than off a fleet row."""
        source = CHAT_PANE.read_text()
        assert ':status="status.statusline"' in source
        assert "compact" not in _status_strip_tag(source)

    def test_every_call_site_hands_it_the_shells_clock(self):
        """A component reading Date.now() itself drifts from the ages beside
        it, which is why every dated thing in this app takes `now`."""
        for path in CALL_SITES:
            tag = _status_strip_tag(path.read_text())
            assert ':now="now"' in tag, path.name

    def test_the_component_is_imported_statically_everywhere(self):
        """It is a few spans on the landing screen's own list, so deferring it
        would fetch a chunk to draw one line — the opposite of the trade
        ``Markdown.vue`` and ``Terminal.vue`` are deferred for."""
        for path in CALL_SITES:
            source = path.read_text()
            assert "import StatusStrip from './StatusStrip.vue'" in source, path.name
            assert "defineAsyncComponent(() => import('./StatusStrip.vue')" \
                not in source, path.name


def _status_strip_tag(source):
    """The ``<StatusStrip …>`` tag as written, for asserting on its props."""
    match = re.search(r"<StatusStrip\b[^>]*>", source, re.S)
    assert match, "no StatusStrip in this source"
    return match.group(0)


def test_claude_markup_matches_the_pre_codex_toggle_render():
    baseline = json.loads((WEB.parent / 'tests/fixtures/codex-usage/claude-render.json').read_text())
    for compact in (False, True):
        without_vue_comments = lambda value: re.sub(r"<!--.*?-->", "", value, flags=re.S)
        assert without_vue_comments(_render(_props(compact=compact))) == without_vue_comments(baseline[str(compact)])
