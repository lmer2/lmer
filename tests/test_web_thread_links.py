"""The thread icons (issue #412, T6).

``ThreadLinks.vue`` is one component on three surfaces — the fleet row, the run
view header and the uber lmer pane header — and what can go wrong with it is
mostly invisible to a source read: an icon that looks connected when nothing is,
a count that does not appear when a run holds two threads, a refusal swallowed
instead of shown, and a control offered on a run with no session to connect. So
the component is mounted through Vue's **server** renderer, as
:mod:`tests.test_platform_web_statusline` mounts the status strip: no browser,
but ``setup()`` runs and the markup is real.

Only what no single component can show — that the three call sites hand it the
same three fields, and that a write reloads the view rather than patching a local
copy — is pinned by reading the sources.
"""

import json
import re
import subprocess
from pathlib import Path

from tests.conftest import node_binary, require_node_toolchain

WEB = Path(__file__).resolve().parent.parent / "web"
COMPONENTS = WEB / "src" / "components"

#: The three surfaces. Spelled as literals rather than composed from a loop
#: variable so the gate's text-diff scanner can classify what this file reads
#: (.lmer/gate-check.yaml explains what it can model).
RUN_CARD = COMPONENTS / "RunCard.vue"
RUN_DETAIL = COMPONENTS / "RunDetail.vue"
CHAT_PANE = COMPONENTS / "AssistantChat.vue"
CALL_SITES = (RUN_CARD, RUN_DETAIL, CHAT_PANE)
API = WEB / "src" / "api.js"

SLACK = {
    "id": "slack:9f2c1d",
    "channel": "slack",
    "permalink": "https://example.slack.com/archives/C0123ABCDEF/p1712345678000900",
    "label": "example · C0123ABCDEF",
    "running": True,
}
SLACK_TWO = dict(SLACK, id="slack:aa11bb", label="example · C0999ZZZ")
REFERENCE = {
    "id": "6f1a2b3c4d5e6f70",
    "channel": "slack",
    "permalink": "https://example.slack.com/archives/C0777AAA/p1712000000000100",
    "title": "the design discussion",
    "announced": True,
}
UNTOLD = dict(REFERENCE, id="0011223344556677", title="a thread nobody has read out", announced=False)
MATRIX = {
    "id": "matrix:44ff88",
    "channel": "matrix",
    "permalink": "https://matrix.to/#/%21room%3Aexample.org/%24event",
    "label": "!room:example.org",
    "running": False,
    "detail": "SLACK_BOT_TOKEN is not set in the daemon's environment",
}

_RENDER_PROBE = """
import { readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { parse, compileScript } from '@vue/compiler-sfc'

const path = 'src/components/ThreadLinks.vue'
const { descriptor } = parse(readFileSync(path, 'utf8'), { filename: path })
const compiled = compileScript(descriptor, { id: 'probe', inlineTemplate: true })
// A data: URL module cannot resolve a bare or relative specifier, so the
// compiled component's imports are rewritten to what this rig resolved them to.
const source = compiled.content
  .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
  .replace(/from ['"]@mdi\\/js['"]/g, `from '${import.meta.resolve('@mdi/js')}'`)
  .replace(/from ['"]\\.\\.\\/api\\.js['"]/g,
    `from '${pathToFileURL('src/api.js').href}'`)
const component = (await import(
  `data:text/javascript;base64,${Buffer.from(source, 'utf8').toString('base64')}`
)).default

const app = createSSRApp({ render: () => h(component, %s) })
// Vuetify's own components are not registered in this rig; left undeclared they
// warn and render as unknown tags, which is what makes the icons assertable.
app.config.compilerOptions.isCustomElement = (tag) => tag.startsWith('v-')
app.config.warnHandler = () => {}
const html = await renderToString(app)
console.log(JSON.stringify({ html: html.replace(/<!--(?!\\]|\\[|v-if)[\\s\\S]*?-->/g, '') }))
"""


def _render(threads, *, session="s-1", live=True, references=None):
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")
    session_json = "null" if session is None else json.dumps(session)
    props = (
        f"{{ sessionId: {session_json}, threads: {json.dumps(threads)}, "
        f"references: {json.dumps(references)}, live: {str(live).lower()} }}"
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", _RENDER_PROBE % props],
        capture_output=True, text=True, timeout=180, cwd=str(WEB),
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout)["html"]


def _icon_buttons(html):
    """The two transport buttons, as their raw opening tags.

    The dialog is in the same markup and has buttons of its own; they belong to
    a different question, and a search over every ``v-btn`` here would be
    answering it by accident.
    """
    return re.findall(r'<v-btn icon\b[^>]*>', html)


# --- rendered: what the operator actually sees -------------------------------


class TestTheTwoIcons:
    def test_both_transports_are_always_offered(self):
        """A run with no Slack thread still shows the Slack icon: the control is
        how you connect one, so hiding it until there is one to show hides the
        feature from every run that has never used it."""
        html = _render([])
        labels = re.findall(r'aria-label="([^"]+)"', html)
        assert labels == ["connect a Slack thread", "connect a matrix thread"]

    def test_an_unconnected_transport_is_colourless(self):
        """Colour in this app is urgency plus one accent for go-and-see-this. An
        icon that is coloured while nothing is connected says the opposite of
        what is true."""
        assert all(
            'color="primary"' not in button for button in _icon_buttons(_render([]))
        )

    def test_a_connected_transport_wears_the_accent(self):
        slack, matrix = _icon_buttons(_render([SLACK]))
        assert 'color="primary"' in slack
        assert 'color="primary"' not in matrix

    def test_a_second_thread_on_one_transport_becomes_a_count(self):
        """Two threads are two rows in a menu, and a badge is the only thing that
        says so from a row scanned at arm's length."""
        one = _render([SLACK])
        two = _render([SLACK, SLACK_TWO])
        assert 'content="2"' in two
        assert 'content="2"' not in one
        # The badge is drawn either way — v-model-value is what turns it on — so
        # what is asserted is the switch, not the element.
        assert 'model-value="false"' in one and 'model-value="true"' in two

    def test_the_dialog_takes_a_channel_as_well_as_a_permalink(self):
        """Issue #429: the same one field, and what it holds decides which of the
        two things saving does."""
        html = _render([])
        assert "Thread permalink, or a channel to open a new thread in" in html

    def test_the_transports_do_not_share_a_count(self):
        html = _render([SLACK, SLACK_TWO, MATRIX])
        assert 'content="2"' in html and 'content="3"' not in html


class TestWhatHoverSays:
    def test_a_connected_icon_names_its_threads(self):
        assert "example · C0123ABCDEF" in _render([SLACK])

    def test_a_recorded_thread_whose_connector_is_not_running_says_why(self):
        """A thread that has gone quiet otherwise looks exactly like one that is
        working, which is the failure this whole feature can have silently."""
        title = _render([MATRIX])
        assert "not connected" in title
        assert "SLACK_BOT_TOKEN is not set" in title

    def test_an_empty_transport_invites_a_connection(self):
        assert "Connect a Slack thread" in _render([])


class TestARunWithNoLiveSession:
    def test_the_connect_control_is_disabled_and_says_so(self):
        """Connecting a run that has no live session is out of scope: there is no
        control plane for a connector to talk to."""
        html = _render([], live=False)
        assert all("disabled" in button for button in _icon_buttons(html))
        assert "no live session to connect" in html

    def test_a_dead_session_still_shows_what_it_was_connected_to(self):
        """The record outlives the session, and "this run was in that thread" is
        still true and still worth reading."""
        assert "example · C0123ABCDEF" in _render([SLACK], live=False)

    def test_a_row_with_no_session_id_offers_nothing_to_press(self):
        html = _render([], session=None, live=True)
        assert all("disabled" in button for button in _icon_buttons(html))


# --- read: what one component cannot show ------------------------------------


class TestReferenceThreads:
    """A thread the run was *pointed at* (#414): connected to nothing, listed
    under its own heading, and offered on a run rather than on a session."""

    def test_a_reference_is_listed_apart_from_a_connection(self):
        html = _render([SLACK], references=[REFERENCE])
        assert "referenced" in html
        assert "the design discussion" in html
        # And it did not become a connection: the accent and the count still
        # answer "is this run mirrored into a thread", which a reference is not.
        assert 'content="2"' not in html

    def test_a_reference_says_whether_the_run_has_been_told(self):
        assert "the run was told" in _render([], references=[REFERENCE])
        assert "the run has not been told yet" in _render([], references=[UNTOLD])

    def test_a_transport_with_only_references_still_opens_a_menu(self):
        """The menu is the only way to reach a reference's link or its remove,
        and a run may hold one on a transport it has never connected."""
        html = _render([], references=[REFERENCE])
        assert "Remove the design discussion" in html

    def test_the_entry_is_offered_on_a_run_and_withheld_on_a_session(self):
        """`references: null` is the uber lmer pane, which is a session with no
        run behind it — the route would 404, so the control is not offered."""
        assert "Add a thread as reference" in _render([], references=[])
        assert "Add a thread as reference" not in _render([], references=None)

    def test_a_run_between_sessions_can_still_be_pointed_at_a_thread(self):
        """Unlike connecting, which needs a control plane: a reference is a note
        on the run, and telling it is what waits for a session.

        Asserted on the **button**, not on the menu text: the menu renders inline
        under SSR whether or not it can ever open, so a disabled activator is a
        control that reads as present and cannot be clicked — which is what the
        first cut of this shipped."""
        html = _render([], references=[], live=False)
        assert all("disabled" not in button for button in _icon_buttons(html))
        assert "Add a thread as reference" in html

    def test_a_row_that_can_neither_connect_nor_reference_is_disabled(self):
        html = _render([], references=None, live=False)
        assert all("disabled" in button for button in _icon_buttons(html))


class TestTheThreeCallSites:
    def test_every_surface_hands_it_the_same_three_fields(self):
        """One component, three homes, one contract. A call site that passed a
        different shape would render an icon that is silently always inactive."""
        for path in CALL_SITES:
            text = path.read_text(encoding="utf-8")
            assert "<ThreadLinks" in text, path.name
            assert ":threads=" in text and ":live=" in text, path.name
            assert ":session-id=" in text, path.name
        # The exception, and the reason the prop defaults to null: references are
        # attached to a run, and the uber lmer pane is a session with none.
        for path in (RUN_CARD, RUN_DETAIL):
            assert ":references=" in path.read_text(encoding="utf-8"), path.name
        assert ":references=" not in CHAT_PANE.read_text(encoding="utf-8")

    def test_a_write_reloads_the_view_rather_than_patching_a_copy(self):
        """The icons render off the fleet payload (and the assistant status), so
        one poll feeds every copy of them on screen. A call site that patched its
        own copy would be the one place they disagree."""
        assert '@changed="load"' in CHAT_PANE.read_text(encoding="utf-8")
        assert '@changed="emit(\'changed\')"' in RUN_DETAIL.read_text(encoding="utf-8")
        # The row does not own the fleet state, so it emits and the shell reloads.
        assert "@changed=\"$emit('threads-changed')\"" in RUN_CARD.read_text(encoding="utf-8")

    def test_a_failed_disconnect_does_not_open_the_connect_dialog(self):
        """Review finding: the catch used to set `dialog = true`, so a refused
        *disconnect* surfaced as a form titled "Connect a …" whose primary action
        is the opposite of what was asked for."""
        text = COMPONENTS.joinpath("ThreadLinks.vue").read_text(encoding="utf-8")
        body = text[text.index("async function disconnect"):text.index("</script>")]
        assert "dialog.value = true" not in body
        assert "failure.value = error.message" in body
        # And it is still shown: these refusals are written for the operator.
        assert 'v-if="failure"' in text

    def test_a_channel_is_held_to_the_transport_its_dialog_belongs_to(self):
        """Review finding on !299: a channel carries no transport, and the
        daemon reads a bare `#name` as Slack — so a matrix alias typed without
        its `:server` half, into the matrix dialog, would open a Slack thread in
        a like-named channel. A root message cannot be unposted."""
        text = COMPONENTS.joinpath("ThreadLinks.vue").read_text(encoding="utf-8")
        body = text[text.index("async function connect"):text.index("async function disconnect")]
        # Both submit paths: the button is disabled, and Enter reaches connect().
        assert "if (wrongTransport.value) return" in body
        assert ':disabled="wrongTransport"' in text
        # And it says which spelling is wanted, rather than only refusing.
        assert 'v-if="wrongTransport"' in text
        assert "!room:example.org" in text

    def test_the_dialogs_reading_of_a_channel_matches_the_daemons(self):
        """The two readings that have to agree: the component decides which body
        to send, `parse_channel` decides what the daemon does with it. Each is
        asked the same table here — the component's own regex under Node, the
        parser in-process — so a drift in either shows up as a mismatch. The
        table is channel-shaped spellings only: the component answers "which
        transport", never "is this a channel at all", which stays the daemon's
        to refuse."""
        from lmer_cli.threads import ThreadError, parse_channel

        cases = [
            "#general", "C0123ABCDEF", "general",
            "!room:example.org", "#room:example.org", "!room:example.org:8448",
        ]
        node = node_binary()
        if not node:
            require_node_toolchain("no Node available (run `lmer platform setup-ui`)")
        source = COMPONENTS.joinpath("ThreadLinks.vue").read_text(encoding="utf-8")
        pattern = re.search(r"\(/\^\[!#\][^\n]*?/\.test", source)
        assert pattern, "the component's channel-transport regex moved"
        literal = pattern.group(0)[1:-len(".test")]
        probe = (
            f"const re = {literal};\n"
            f"console.log(JSON.stringify({json.dumps(cases)}"
            ".map((c) => (re.test(c.trim()) ? 'matrix' : 'slack'))))"
        )
        result = subprocess.run(
            [node, "--input-type=module", "-e", probe],
            capture_output=True, text=True, timeout=60, cwd=str(WEB),
        )
        assert result.returncode == 0, result.stderr
        by_component = json.loads(result.stdout)

        def by_daemon(spec):
            try:
                return parse_channel(spec).channel
            except ThreadError:
                return "refused"

        assert by_component == [by_daemon(case) for case in cases]

    def test_the_client_sends_the_permalink_and_nothing_else(self):
        """The transport is read out of the link. A body field naming it would be
        a second source for one fact, and the two could disagree."""
        text = API.read_text(encoding="utf-8")
        assert "JSON.stringify({ permalink })" in text
        # And opening one is the same route with the other half of the choice:
        # a channel to make a thread in, and the root's first line (#429).
        assert "JSON.stringify({ channel, title })" in text
        assert "encodeURIComponent(threadId)" in text
        # A reference carries a title beside the link, and nothing else: what
        # kind of thread it is comes out of the link there too.
        assert "JSON.stringify({ permalink, title })" in text
        assert "encodeURIComponent(referenceId)" in text
        # And there is no client for the GET: the icons render from the fleet
        # payload and the assistant status, so nothing would read one.
        assert "fetchSessionThreads" not in text
