"""Guards on the control UI's terminal (issue #141, slice M2 / T17).

Source-level invariants, like :mod:`tests.test_platform_web_app`, because there is
no JS runner and no browser in this image — and because the ways this component
breaks are all quiet. A terminal that attaches without replaying the log, or
reconnects with a spent ticket, or reports no geometry, renders perfectly and is
simply useless; the discovery happens on a phone, in front of a session that is
waiting for an answer.

So each test here pins one decision whose loss has no visible symptom in a build:

- scrollback is replayed *before* the socket, and the socket resumes from the
  offset the server resolved — not from a byte count the client added up
- every connection attempt mints its own ticket, because a ticket is single-use
  and a reconnect with the old one is refused
- the replay is a bounded tail, within the server's own limit on one read
- the conversation and the terminal are a tab pair, which is also what defers
  xterm's chunk to the moment the terminal is first opened
- the session **is** fitted to this screen by default, and the switch that turns
  that off still exists. The old default was off, and off renders a TUI drawn for
  80 columns into a phone's 45; a regression either way is invisible until it is
  in someone's hand
- a session that has exited is read-only, and says so
- the composer (a line to submit, Enter, arrows, Esc, Ctrl-C) exists because an
  on-screen keyboard reaches none of those keys — and is shown only where the
  pointer is a finger, because a real keyboard reaches all of them
- every Ctrl-C is confirmed: twice in a row is the quit chord
- Ctrl+V and Shift+Insert paste, which means xterm must *not* encode them — and
  the pasted text comes back through the same handler a keystroke does
- the copy chords copy and Ctrl+C still interrupts: the browser's own chords are
  handed back to it, Ctrl+Shift+C is performed here because no browser performs
  it, and the chord that must never become a copy is the interrupt
- clicking the terminal gives it the keyboard, and it says so when it has not got
  it: every chord in the two lines above goes to whatever is focused
- "earlier output" stops at the origin of the log the server calls the record,
  and the launch — which for a session that records itself is in the other file —
  is a separate read of that file, shown on its own and never spliced in front
- xterm's stylesheet comes from the package and nothing loads a font

Rendering, input routing, and how any of it feels one-handed are verified by
building the bundle and by live test LT3 on a real phone.
"""

import json
import math
import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
TERMINAL = WEB / "src" / "components" / "Terminal.vue"
RUN_DETAIL = WEB / "src" / "components" / "RunDetail.vue"
API = WEB / "src" / "api.js"


def _read(path):
    return path.read_text(encoding="utf-8")


def _function_body(text, signature):
    """Source of one top-level function in a ``<script setup>`` block.

    Every function in the component is top-level, so a ``}`` in column zero ends
    one. Used to assert *ordering* and locality — which of two calls happens
    first, and which function a call lives in — since that is exactly what these
    bugs are made of.
    """
    start = text.index(signature)
    return text[start:text.index("\n}\n", start)]


def _status_case(text, event):
    """One ``case`` of the status-frame switch, up to its ``return``."""
    body = _function_body(text, "function handleStatus")
    start = body.index(f"case '{event}':")
    return body[start:body.index("return", start)]


def _prose(text):
    """*text* with runs of whitespace collapsed.

    For asserting a sentence the operator reads: in the markup it is wrapped to
    the column limit, and where the wrap falls is not a decision anybody wants a
    test to have an opinion about.
    """
    return re.sub(r"\s+", " ", text)


def _code(text):
    """*text* with its whole-line ``//`` comments removed.

    For the guards that assert a browser API is *not* reached. This component
    explains at length why it does not read the clipboard itself, and a substring
    check over the whole file cannot tell that sentence from a call — it would fail
    on the comment that documents the decision it is checking for.
    """
    return re.sub(r"^[ \t]*//.*$", "", text, flags=re.M)


def _element(text, opening, tag="div"):
    """One element's markup, from *opening* to the ``</tag>`` that closes it.

    Depth-counted rather than matched with a regex: the assertions below are
    about what sits *inside* a wrapper, and a non-greedy match would stop at the
    first nested close and quietly pass.
    """
    start = text.index(opening)
    depth = 0
    cursor = start
    while True:
        following = text.find(f"<{tag}", cursor + 1)
        closing = text.index(f"</{tag}>", cursor + 1)
        if following != -1 and following < closing:
            depth += 1
            cursor = following
        elif depth:
            depth -= 1
            cursor = closing
        else:
            return text[start:closing + len(f"</{tag}>")]


def _tab_panels(detail):
    """The run-detail view's tab panels, keyed by the value that selects them."""
    # Anchored on the inner window specifically. T49 nested these inside
    # top-level tabs (overview / lmer / exit), and a non-greedy match from the
    # first `<v-tabs-window` would take the OUTER one and flatten both levels
    # into one dict — which reads as "the session views are top-level tabs".
    window = re.search(
        r'<v-tabs-window\s+v-model="pane".*?</v-tabs-window>', detail, re.S
    )
    assert window, "RunDetail.vue no longer puts the session views in a window"
    panels = {}
    for chunk in window.group(0).split("<v-tabs-window-item")[1:]:
        value = re.search(r'value="(\w+)"', chunk)
        assert value, "a panel with no value cannot be selected by a tab"
        panels[value.group(1)] = chunk.split("</v-tabs-window-item>")[0]
    return panels


def _tail_steps(text):
    """The TAIL_STEPS literal, evaluated. Written as products for readability."""
    block = re.search(r"const TAIL_STEPS = \[([^\]]+)\]", text)
    assert block, "Terminal.vue no longer bounds its replay with TAIL_STEPS"
    steps = []
    for step in block.group(1).split(","):
        if step.strip():
            steps.append(math.prod(int(part) for part in step.split("*")))
    return steps


# --- it exists and it is mounted --------------------------------------------

def test_the_terminal_is_mounted_in_the_run_detail_view():
    """A component nothing renders is the same as no component.

    The detail view used to say terminal and chat were not in the build; that
    sentence has to go with the feature, or the UI contradicts itself.
    """
    assert TERMINAL.is_file()
    detail = _read(RUN_DETAIL)
    assert "<Terminal" in detail
    assert ':session-id="terminalSession"' in detail
    assert "not in this build yet" not in detail


def test_the_conversation_and_the_terminal_are_tabbed_not_stacked():
    """Stacked, the conversation pushed the terminal off the bottom of the page.

    A transcript is hundreds of turns, and the terminal, the events list and the
    forget button all sat underneath it. Nobody reads both views at once, so they
    are a tab pair — which also hands each of them a bounded box, which is what
    the emulator sizes itself from.
    """
    detail = _read(RUN_DETAIL)
    tabs = re.search(r'<v-tabs\s+v-model="(pane)"', detail)
    window = re.search(r'<v-tabs-window\s+v-model="(pane)"', detail)
    assert tabs and window, "the two session views are not a tab pair"
    assert tabs.group(1) == window.group(1), (
        "the tab bar and the panels read different models, so the selected tab "
        f"need not be the visible one: {tabs.group(1)} vs {window.group(1)}"
    )

    panels = _tab_panels(detail)
    # Three since T49: the operator channel joined them, because it is the third
    # thing you read a session through and it was previously stacked below both.
    # The pane holds the channel's *record* since T40 — what is waiting on a reply
    # is docked above the tabs, where a remembered tab cannot hide it.
    assert set(panels) == {"conversation", "terminal", "chat"}
    assert "<Chat" in panels["conversation"]
    assert "<Terminal" in panels["terminal"]
    assert "<AskHistory" in panels["chat"]
    for value in panels:
        assert f'<v-tab value="{value}"' in detail, f"no tab selects {value}"

    # Neither view is rendered a second time outside the window. That is what
    # stacked looked like, and it would undo both the bounded box and the chunk.
    outside = re.sub(r"<v-tabs-window[\s>].*?</v-tabs-window>", "", detail, flags=re.S)
    assert "<Chat" not in outside and "<Terminal" not in outside, "stacked again"


def test_the_terminal_chunk_arrives_when_its_tab_is_first_opened():
    """xterm is more than half the JS, and only this view opens one.

    The fleet view is what a phone loads on every glance, so it must not carry an
    emulator it may never show — and now that the two views are tabbed, opening a
    run must not either. Both halves are source-level decisions with no symptom in
    a build: a static import puts the emulator back in the entry bundle, and an
    ``eager`` panel renders it (and so fetches its chunk) before it is selected.
    """
    detail = _read(RUN_DETAIL)
    assert "defineAsyncComponent(() => import('./Terminal.vue'))" in detail
    assert "import Terminal from" not in detail, "back to one bundle"
    config = _read(WEB / "vite.config.js")
    assert "chunkFileNames" in config
    assert "defineAsyncComponent" in config, (
        "the split is deliberate and the config should say so"
    )
    assert "eager" not in _tab_panels(detail)["terminal"], (
        "an eager panel renders before it is selected, so the chunk is fetched "
        "by opening the run again"
    )


# --- scrollback first, then live (spec D16) ----------------------------------

def test_scrollback_is_replayed_before_the_socket_is_opened():
    """Attaching with no history is unusable.

    The first question on opening a session is "what is it doing and how did it
    get here", and a live socket alone answers neither until the session next
    prints something — which, for a session waiting on a human, is never.
    """
    text = _read(TERMINAL)
    start = _function_body(text, "async function start()")
    for call in ("fetchSessionLog", "render(chunk)", "connect(mine)"):
        assert call in start, f"start() no longer does {call}"
    assert (
        start.index("fetchSessionLog")
        < start.index("render(chunk)")
        < start.index("connect(mine)")
    ), "the log is not fetched, rendered, and only then followed"
    render = _function_body(text, "function render(frame)")
    assert "term.write(decodeLogData(frame.data))" in render, (
        "the replayed bytes never reach the emulator"
    )


def test_the_socket_resumes_from_the_offset_the_server_resolved():
    """The seam where a mistake duplicates or loses output silently.

    A byte count added up on the client is wrong the moment the server clamps a
    read or resolves a negative offset, and both failures look like a session
    misbehaving: repeated output, or a gap nobody can point at. So the cursor is
    only ever assigned from the server's ``next_offset``, and that cursor is what
    the socket is told to start from.
    """
    text = _read(TERMINAL)
    assignments = set(re.findall(r"cursor = ([^\n]+)", text))
    assert assignments == {"frame.next_offset", "0"}, (
        f"the cursor is computed rather than taken from the server: {assignments}"
    )
    assert "offset: cursor" in text, "the socket does not resume from the cursor"
    assert "ttySocketUrl" in text


def test_the_replay_is_a_bounded_tail_within_the_servers_read_limit():
    """Fetching the whole log of a long run would hang the phone that asked.

    A negative offset is how a tail is asked for, and the largest step must stay
    inside what one read can answer — past that the server clamps, the client
    renders less than it thinks it did, and "earlier output" quietly stops
    working.
    """
    from lmer_platform.session_io import MAX_LOG_LIMIT

    text = _read(TERMINAL)
    steps = _tail_steps(text)
    assert steps, "no tail steps"
    assert steps == sorted(steps), "tail steps must grow"
    assert max(steps) <= MAX_LOG_LIMIT, (
        f"a {max(steps)}-byte read exceeds the server's {MAX_LOG_LIMIT} limit"
    )
    assert "offset: -tailBytes" in text, "the first read is not a tail"


def test_earlier_output_is_reachable_and_finite():
    """More history is an explicit act, and the end of it has to be admitted.

    xterm cannot prepend to its buffer, so this restarts the attach from further
    back; when the largest read is showing, the view says so rather than offering
    a button that does nothing.
    """
    text = _read(TERMINAL)
    assert "earlier output" in text
    assert "canLoadEarlier" in text and "atOldestChunk" in text
    assert "function loadEarlier" in text


# --- the ticket is single-use ------------------------------------------------

def test_every_connection_attempt_mints_its_own_ticket():
    """A ticket is consumed by the handshake it authorizes.

    Minting once and reusing it on reconnect is the natural shape and it fails
    only on the second attempt: the socket is refused, the terminal freezes, and
    nothing in a build reveals it. So the mint lives inside the function the
    reconnect timer calls.
    """
    text = _read(TERMINAL)
    connect = _function_body(text, "async function connect(mine)")
    assert "mintTtyTicket" in connect
    assert text.count("mintTtyTicket(") == 1, "a second mint path can go stale"
    reconnect = _function_body(text, "function scheduleReconnect")
    assert "connect(mine)" in reconnect, "a dropped socket never reconnects"


def test_a_dropped_socket_reconnects_with_backoff_and_says_so():
    """Requirement 5: a frozen terminal must not be indistinguishable from a
    quiet session."""
    text = _read(TERMINAL)
    delays = re.search(r"const RECONNECT_DELAYS = \[([^\]]+)\]", text)
    assert delays, "no backoff schedule"
    values = [int(value) for value in delays.group(1).split(",")]
    assert values == sorted(values) and len(values) > 1, "backoff does not back off"
    reconnect = _function_body(text, "function scheduleReconnect")
    assert "phase.value = 'reconnecting'" in reconnect
    assert "notice.value" in reconnect, "the user is not told what is happening"


def test_the_socket_is_authenticated_only_by_a_ticket():
    """A WebSocket handshake carries no Authorization header and the browser does
    not apply its Basic credentials to an upgrade; the shared secret must never
    reach a query string, where the access log and the browser's history keep it."""
    api = _read(API)
    ticket_url = _function_body(api, "export function ttySocketUrl")
    assert "searchParams.set('ticket'" in ticket_url
    parameters = set(re.findall(r"searchParams\.set\('([a-z_]+)'", ticket_url))
    assert parameters == {"ticket", "offset"}, (
        f"the socket URL carries more than a ticket and an offset: {parameters}"
    )
    mint = _function_body(api, "export function mintTtyTicket")
    assert "method: 'POST'" in mint, (
        "the ticket must come from a request the browser authenticates"
    )


# --- geometry ----------------------------------------------------------------

def test_the_session_is_fitted_to_this_screen_by_default():
    """This default shipped off once, and off was the wrong answer.

    The reasoning was that a resize writes to a PTY other people are watching. For
    a run this platform spawned there are no other people: the daemon creates the
    PTY, nothing interactive is ever attached to it, and the drain thread only
    tees the output to a log. So off just draws an 80-column TUI into a phone's
    45, and nothing on screen says that is a setting — it reads as the terminal
    being broken.

    Invisible from a build either way, which is why the default is pinned rather
    than looked at.
    """
    text = _read(TERMINAL)
    # The switch is remembered now (T49), so the default is no longer a literal
    # `ref(true)` — it is the fallback handed to the stored-flag reader. That is
    # the value an operator with an empty store gets, which is what "by default"
    # means here.
    assert "const resizeOptIn = ref(storedResizeOptIn())" in text, (
        "the fit switch no longer reads its remembered value"
    )
    assert "storedFlag(() => window.localStorage.getItem(FIT_STORAGE_KEY), true)" in text, (
        "the fit is off by default again, which is an unreadable phone terminal"
    )


def test_fitting_is_still_a_switch_and_says_when_to_turn_it_off():
    """The old default was written for a real case — watching a session somebody
    else is driving from a terminal on the host — and that case did not go away
    with the default. It is the switch's whole reason to exist, so the control
    stays and the text has to name the situation rather than describe a mechanism.
    """
    text = _read(TERMINAL)
    assert "setResizeOptIn" in text
    assert '@update:model-value="setResizeOptIn"' in text, "nothing turns it off"
    assert ':model-value="resizeOptIn"' in text
    fit = _function_body(text, "function setResizeOptIn")
    assert "applyGeometry()" in fit, "turning it back on does not apply it"
    # The markup, not the whole file: this is about what the operator reads, and a
    # comment explaining it to the next maintainer is not the same thing.
    prose = _prose(text[text.index("<template>"):])
    assert "everyone attached" in prose, "the shared consequence is not stated"
    assert "looking over someone's shoulder" in prose, (
        "nothing tells the operator which situation the default is wrong for"
    )


def test_a_resize_frame_has_one_gate_and_one_sender():
    """With the fit on, that switch is the only thing between a shoulder-surfer
    and reflowing somebody else's terminal — so it has to be impossible to get
    around. A second sender, or a gate that does not precede the send, is
    invisible from the client that caused it."""
    text = _read(TERMINAL)
    report = _function_body(text, "function reportGeometry")
    assert "if (!resizeOptIn.value) return" in report, "the opt-out gate is gone"
    assert report.index("if (!resizeOptIn.value) return") < report.index("socket.send"), (
        "the gate does not precede the send"
    )

    senders = re.findall(r"type: 'resize'", text)
    assert len(senders) == 1, f"a resize frame is built in {len(senders)} places"
    assert "socket.send(JSON.stringify({ type: 'resize', rows, cols }))" in report


def test_a_resize_the_operator_asked_for_is_debounced():
    """Kept for the case the opt-in exists for: they fitted, then rotated the
    phone. Uncoalesced, that burst is a round trip and a reflow per event."""
    text = _read(TERMINAL)
    assert "RESIZE_DEBOUNCE_MS" in text
    assert "setTimeout(applyGeometry, RESIZE_DEBOUNCE_MS)" in text
    assert "window.addEventListener('resize'" in text
    assert "window.removeEventListener('resize'" in text, "listener outlives the view"
    opened = _function_body(text, "function handleFrame")
    assert "reportGeometry()" in opened, (
        "a reconnect does not re-apply a fit the operator asked for"
    )


def test_reported_dimensions_stay_inside_the_supervisors_bounds():
    """Out-of-range geometry comes back as ``resize_failed``, whose message says
    the PTY is probably gone — a diagnosis that would be a lie. The UPPER bound
    belongs to the control plane, so it is read from there rather than guessed.
    The LOWER bound is deliberately NOT the control plane's: legality-minimum (1)
    is what once let a sliver-layout fit reading reflow a live PTY to one column,
    so the client's floors must sit strictly above it (the daemon's
    ``session_io.MIN_RESIZE_*`` floor is the shared backstop)."""
    from lmer_cli.supervisor import MAX_WINSIZE_DIMENSION, MIN_WINSIZE_DIMENSION
    from lmer_platform.session_io import MIN_RESIZE_COLS, MIN_RESIZE_ROWS

    text = _read(TERMINAL)
    assert f"MAX_DIMENSION = {MAX_WINSIZE_DIMENSION}" in text
    assert f"MIN_COLS = {MIN_RESIZE_COLS}" in text, (
        "the client floor and the daemon floor are the same policy, spelled "
        "twice (the two-spellings pattern) — they must not drift"
    )
    assert f"MIN_ROWS = {MIN_RESIZE_ROWS}" in text
    assert MIN_RESIZE_COLS > MIN_WINSIZE_DIMENSION, (
        "a floor equal to the control plane's legality bound is the bug, not "
        "a guard"
    )
    clamp = _function_body(text, "function clampDimension")
    assert "MAX_DIMENSION" in clamp and "floor" in clamp
    report = _function_body(text, "function reportGeometry")
    assert "clampDimension" in report
    assert "if (!rows || !cols) return" in report, (
        "a below-floor dimension is dropped, never sent"
    )


def test_a_refused_resize_answers_the_operator_and_is_not_retried():
    """The backend's loud/quiet split, adjusted for who asked.

    Quiet was right while resizing was automatic. It is now something the operator
    pressed, so silence would be a broken control: 404/503 (this session cannot be
    resized — an older image, or a supervisor with no PTY hook) gets a line of text
    but not an error, while 500 (the ioctl failed, so the PTY is going away) is an
    error. Both disarm, so neither is asked again on the next rotation.
    """
    text = _read(TERMINAL)
    unsupported = _status_case(text, "resize_unsupported")
    assert "resizeSupported.value = false" in unsupported
    assert "notice.value" in unsupported, "a control that did nothing said nothing"
    assert "problem.value" not in unsupported, "a deployment fact is not an error"

    failed = _status_case(text, "resize_failed")
    assert "problem.value" in failed, "the session ending is not surfaced"
    for case in (unsupported, failed):
        assert "resizeOptIn.value = false" in case, "a refused resize is asked again"
    assert "if (!term || !resizeSupported.value) return" in text
    assert ':disabled="!canType || !resizeSupported"' in text, (
        "the control stays offered after the session said it cannot"
    )


def test_every_status_event_the_backend_emits_is_handled():
    """The UI must not drop an event the platform can send.

    Scraped from the two modules that emit them rather than restated, so a new
    event arrives as a failing test and someone decides how it should read.
    """
    source = Path(__file__).resolve().parent.parent / "src" / "lmer_platform"
    api_py = (source / "api.py").read_text(encoding="utf-8")
    io_py = (source / "session_io.py").read_text(encoding="utf-8")
    events = set(re.findall(r'send_status\(\s*websocket,\s*"([a-z_]+)"', api_py))
    events |= set(re.findall(r'event="([a-z_]+)"', io_py))
    assert {"ended", "input_failed", "resize_unsupported"} <= events, (
        f"the scrape stopped finding events: {sorted(events)}"
    )

    text = _read(TERMINAL)
    for event in sorted(events):
        assert f"case '{event}':" in text, f"Terminal.vue ignores a {event!r} frame"


# --- a dead session is read-only history ------------------------------------

def test_a_session_that_has_exited_renders_read_only_and_explains_itself():
    """The log outlives the container, which is the whole point of it.

    So a gone session still has a terminal — with no input box, and a sentence
    saying why, instead of a prompt that looks alive and swallows an answer the
    operator believes was delivered.
    """
    text = _read(TERMINAL)
    assert "disableStdin: true" in text, "input is live before liveness is known"
    assert "!chunk.live" in text, "liveness is not taken from the log route"
    assert "phase.value = 'history'" in text
    # Spanning lines since T79 gave the launch view its own phase — which is the
    # third thing there is nothing to type into.
    showinput = re.search(r"const showInput = computed\((.+?)\n\)", text, re.S)
    assert showinput and "'history'" in showinput.group(1), (
        "a dead session still shows an input box"
    )
    assert "has exited" in text, "nothing explains why there is no prompt"


def test_history_is_offered_for_a_run_whose_session_is_gone():
    """The log outlives the container, so the terminal must outlive the entry.

    A clean exit removes the registry entry while the PTY log stays on disk, and a
    crashed one loses its entry the moment the crash is acknowledged. Keying the
    terminal on ``run.session`` would throw the history away in both cases —
    exactly when someone wants to read it.
    """
    detail = _read(RUN_DETAIL)
    assert "run.last_session_id" in detail
    assert 'v-if="terminalSession"' in detail
    assert "terminalSession" in detail
    assert ':session-id="session.id"' not in detail, (
        "the terminal is keyed on the live entry again"
    )


def test_the_history_id_is_the_one_the_backend_publishes():
    """The seam between the fleet view's payload and what this component asks for.

    Renaming the field on either side leaves a run detail with no terminal and no
    error, which is the failure this test exists to make loud.
    """
    from lmer_platform.inventory import RunView

    remembered = RunView(
        host="h", project="p", slug="s", state="dormant", last_session_id="sess-old",
    )
    assert remembered.to_dict()["last_session_id"] == "sess-old"

    running = RunView(
        host="h", project="p", slug="s", state="running",
        session={"id": "sess-new", "live": True}, last_session_id="sess-old",
    )
    assert running.to_dict()["last_session_id"] == "sess-new", (
        "a live session's own log is what a live terminal has to follow"
    )


def test_liveness_is_not_taken_from_the_fleet_poll():
    """The fleet view is up to a poll interval old; whether a session can be typed
    into is decided by the log route's freshly computed answer."""
    text = _read(TERMINAL)
    props = re.search(r"const props = defineProps\((.*?)\n\}\)", text, re.S)
    assert props, "props changed shape"
    assert "live" not in props.group(1).split("sessionId")[1], (
        "liveness is back to being a prop"
    )


# --- mobile ------------------------------------------------------------------

def test_the_keys_a_phone_keyboard_does_not_have_are_buttons():
    r"""One-handed use is the requirement, not a nicety (spec §10.1).

    An on-screen keyboard has no Ctrl, no Tab, no Esc and no arrows, and xterm's
    hidden textarea is unreliable on iOS and Android — so the affordance that has
    to work is the visible one. CR rather than LF, because these land on a PTY in
    raw mode where the TUI reads CR as "submit" and LF as a literal newline.

    Esc is the one that earns its place: it is what interrupts a turn in the
    harness TUIs this drives, which was for a while wrongly attributed to Ctrl-C.
    """
    text = _read(TERMINAL)
    assert r"enter: '\r'" in text, "Enter must send CR, not LF"
    assert r"up: '\x1b[A'" in text, "no arrow up, so no command history"
    assert r"escape: '\x1b'" in text, "no Esc, and Esc is what interrupts a turn"
    assert r"interrupt: '\x03'" in text, "no Ctrl-C, so nothing can be stopped"
    for label in ("Enter", "Tab", "Esc", "Ctrl-C"):
        assert re.search(rf">\s*{re.escape(label)}\s*<", text), f"no {label} button"
    assert "aria-label" in text, "icon-only buttons need names"


def test_the_composer_is_shown_only_where_the_pointer_is_a_finger():
    """The keys and the field are one affordance and they share one reason.

    Both exist because of the device: an on-screen keyboard has none of those
    keys, and xterm's hidden textarea is unreliable on the phones that have it. A
    machine with a real keyboard has neither problem — it types into the emulator
    directly — so there the block is chrome between the terminal and everything
    under it, and it is hidden as a unit.

    A pointer query, not a width one: a tablet in landscape is wide and still has
    no keyboard, and a narrow desktop window is narrow and has one.
    """
    text = _read(TERMINAL)
    composer = _element(text, '<div class="composer">')
    assert "<v-text-field" in composer, "the line field is outside the gate"
    assert text.count("<v-text-field") == 1, "a second field would not be gated"
    for key in ("KEYS.enter", "KEYS.up", "KEYS.down", "KEYS.tab", "KEYS.escape"):
        assert key in composer, f"the {key} button is outside the gate"
    assert "confirmingInterrupt = true" in composer, "the Ctrl-C button is outside"

    style = text[text.index("<style"):]
    assert "@media (pointer: coarse)" in style, "the composer is not gated at all"
    default, coarse = style.split("@media (pointer: coarse)", 1)
    hidden = default[default.index(".composer {"):]
    assert "display: none" in hidden[:hidden.index("}")], (
        "the composer's default display is not none, so every desktop shows it"
    )
    assert ".composer" in coarse and "display: block" in coarse, (
        "nothing brings the composer back for a touch device"
    )
    for query in ("@media (max-width", "@media (min-width"):
        assert query not in style, (
            "a width breakpoint hides these on a tablet in landscape and shows "
            "them in a narrow desktop window"
        )


def test_every_ctrl_c_is_confirmed():
    """Two of them in a row is the quit chord, and a phone is one mis-tap.

    Ctrl-C stays because it is the only stop this UI has — the wind-down and exit
    verbs are not built — but its real function in that TUI is heading toward
    quitting, so no press reaches the socket without a dialog in between. That is
    also what makes an accidental double impossible, which is why there is no
    separate second-press guard to look for.
    """
    text = _read(TERMINAL)
    assert "v-dialog" in text and "confirmingInterrupt" in text
    assert '@click="confirmingInterrupt = true"' in text, "the button sends directly"

    senders = re.findall(r"sendInput\(KEYS\.interrupt\)", text)
    assert len(senders) == 1, f"Ctrl-C is sent from {len(senders)} places"
    interrupt = _function_body(text, "function interrupt()")
    assert "sendInput(KEYS.interrupt)" in interrupt, "confirming sends nothing"
    assert "confirmingInterrupt.value = false" in interrupt, "the dialog never closes"

    keys = _element(text, '<div class="key-pad mb-3">')
    assert "sendInput(KEYS.interrupt)" not in keys, (
        "the button sends the byte itself, so the confirmation is decorative"
    )


def test_a_line_can_be_typed_and_submitted_without_a_keyboard():
    """The one input path that is known to work on a phone.

    ``append_newline`` is the control plane's "and press Enter", so an empty field
    sends a bare Enter — which is often exactly what a waiting prompt needs.
    """
    text = _read(TERMINAL)
    assert "v-text-field" in text
    assert '@keydown.enter="submitLine"' in text
    assert '@click:append-inner="submitLine"' in text, "no tappable send"
    submit = _function_body(text, "function submitLine")
    assert "sendInput(line.value, true)" in submit
    assert "append_newline" in text
    assert 'autocapitalize="off"' in text, "a phone would capitalise the command"


def test_the_emulator_still_takes_a_real_keyboard():
    """On the machine that has one, this is now the *only* input path.

    The composer is hidden for a fine pointer, so nothing else on that screen
    accepts a keystroke: clicking the terminal focuses xterm, and onData is what
    carries what it produces — arrows, Ctrl chords, pasted text, already encoded
    for a PTY — on to the session. Losing that handler leaves a desktop with a
    terminal it cannot type into and no visible sign of why.
    """
    build = _function_body(_read(TERMINAL), "function buildTerminal")
    assert re.search(r"term\.onData\(\((\w+)\) => sendInput\(\1\)\)", build), (
        "xterm's keystrokes do not reach the session unchanged"
    )


def test_input_that_went_nowhere_is_loud():
    """The backend's rule, kept on this side: a session sitting there still
    waiting, while the operator believes it was answered, is worse than an
    error."""
    send = _function_body(_read(TERMINAL), "function sendInput")
    assert "problem.value" in send
    assert "readyState !== WebSocket.OPEN" in send


def test_the_terminal_box_has_a_definite_height():
    """The fit addon reads this box's computed height to decide how many rows the
    session is told it has; an ``auto`` height leaves it at xterm's 24x80 default,
    which on a phone means a session laid out for a screen twice as wide."""
    text = _read(TERMINAL)
    style = text[text.index("<style"):]
    assert ".terminal-host" in style
    assert "height:" in style
    assert "dvh" in style, "vh does not notice a phone's browser chrome"


# --- self-containedness ------------------------------------------------------

def test_xterms_stylesheet_comes_from_the_package():
    """Without it the viewport is unpositioned and the terminal is a wall of
    wrapped text; from a CDN it is a blank screen on the LAN this UI is for."""
    text = _read(TERMINAL)
    assert "import '@xterm/xterm/css/xterm.css'" in text
    assert not list((WEB / "src").rglob("xterm*.css")), (
        "a vendored copy of the stylesheet drifts from the package it belongs to"
    )


def test_the_terminal_loads_no_font():
    """System stacks only — an icon font's empty boxes are survivable, a missing
    monospace font is a terminal that does not line up."""
    text = _read(TERMINAL)
    assert "@font-face" not in text
    assert "url(" not in text
    stack = re.search(r"fontFamily: '([^']+)'", text)
    assert stack, "no font stack is named, so xterm picks its own"
    assert stack.group(1).strip().endswith("monospace"), (
        f"the stack has no generic fallback: {stack.group(1)}"
    )


def test_the_terminal_colours_come_from_the_theme():
    """Spec §10.1: both schemes are first-class and the OS picks. xterm's own
    default is a black rectangle that ignores both."""
    text = _read(TERMINAL)
    assert "useTheme" in text
    assert "theme.current.value.colors" in text
    assert not re.search(r"#[0-9a-fA-F]{6}", text), "a hardcoded colour"


def test_the_emulator_has_a_ground_of_its_own_and_the_box_shares_it():
    """The operator: "i think the terminal background could be darker".

    It was the card's ``surface``, which is white in the light scheme — a ground the
    ANSI colours a harness paints with were never chosen for. So the emulator reads a
    colour of its own, dark in both schemes (main.js argues the value;
    :mod:`tests.test_platform_web_theme` pins that it is darker than what surrounds
    it, in both).

    Two names, and the second is the one that is easy to lose: the ink has to come
    from the same pair. ``on-surface`` in a light scheme is near-black, so an emulator
    given a dark ground and the card's ink is a terminal of invisible text — which
    renders perfectly and is discovered by someone trying to read a session.

    And the box has to be painted with it too. xterm fills its own viewport, not the
    6px of padding around it, so a host div left on ``surface`` frames a dark terminal
    in a white ring on the one scheme where it is most obvious.
    """
    text = _read(TERMINAL)
    theme = _function_body(text, "function terminalTheme()")

    assert "background: pick('terminal')" in theme, (
        "the emulator is drawn on something other than its own theme colour"
    )
    assert "pick('on-terminal')" in theme, (
        "the emulator's ink is not the one that belongs to its ground; on a light "
        "scheme that is near-black text on a near-black terminal"
    )
    for borrowed in ("pick('surface')", "pick('on-surface')"):
        assert borrowed not in theme, (
            f"the emulator still takes the card's {borrowed}, so it is a dark box "
            "with the light scheme's ink in it, or a white one again"
        )

    style = text[text.index("<style"):]
    host = style[style.index(".terminal-host {"):]
    host = host[:host.index("}")]
    assert "background: rgb(var(--v-theme-terminal))" in host, (
        "the box around the emulator is painted with a different colour, so the "
        "padding shows the card through it"
    )


def test_the_socket_and_the_emulator_are_released_with_the_view():
    """A socket left open keeps a server-side follower polling a log nobody is
    reading, and the leak is invisible from the phone that caused it."""
    text = _read(TERMINAL)
    unmount = _function_body(text, "onBeforeUnmount(()")
    assert "teardownSocket()" in unmount
    assert "term.dispose()" in unmount
    assert "removeEventListener" in unmount
    assert "disposed = true" in unmount
    teardown = _function_body(text, "function teardownSocket")
    assert "onmessage = null" in teardown, (
        "a frame after a restart would be written into a replayed terminal"
    )


# --- dependencies -----------------------------------------------------------

def test_xterm_is_declared_and_locked():
    """The two packages this feature adds, named so the addition stays reviewed.

    ``@xterm/xterm`` is the current package (the old ``xterm`` name is
    deprecated); ``@xterm/addon-fit`` is what turns the container's size into rows
    and columns. Both widen the allowlist in
    ``test_platform_web_app.test_dependency_set_is_minimal``, which has to be
    updated to match — this test is the statement of intent, that one is the gate.
    """
    payload = json.loads(_read(WEB / "package.json"))
    for name in ("@xterm/xterm", "@xterm/addon-fit"):
        assert name in payload["dependencies"], f"{name} is not declared"
    lock = json.loads(_read(WEB / "package-lock.json"))
    for name in ("@xterm/xterm", "@xterm/addon-fit"):
        entry = lock["packages"].get(f"node_modules/{name}")
        assert entry and entry.get("resolved"), f"{name} is not locked"
    assert "xterm" not in {*payload["dependencies"]}, "the deprecated package name"


# --- a session that is still starting is not a broken one --------------------
#
# The operator, live testing: switching to the terminal before the harness was
# running gave "cannot reach the session's control plane at 127.0.0.1:8745
# (Connection reset by peer)", and the only cure was reloading the platform. Two
# things combined to make a transient state permanent: the server reported an
# unreachable control plane as `resize_failed` (whose handler assumes the PTY is
# gone), and fit-to-screen is now on by default, so every terminal open sends a
# resize immediately.

def test_a_still_starting_session_does_not_switch_fitting_off():
    """`resize_failed` means stop asking; `resize_deferred` means ask again.

    Handling the second like the first is what cost an operator a working terminal:
    both flags latched off and could only be restored by reloading the page.
    """
    text = _read(TERMINAL)
    case = _status_case(text, "resize_deferred")

    assert "resizeSupported.value = false" not in case, (
        "a session that is still starting supports resizing; it just is not "
        "listening yet"
    )
    assert "resizeOptIn.value = false" not in case, (
        "turning the operator's own preference off leaves them to notice and undo "
        "it, for a condition that clears itself in a second"
    )


def test_a_still_starting_session_is_a_notice_not_a_problem():
    """`problem` is the sticky red state; this is a wait, so it reads as one."""
    case = _status_case(_read(TERMINAL), "resize_deferred")

    assert "notice.value" in case
    assert "problem.value" not in case


def test_the_geometry_is_retried_and_the_retries_are_bounded():
    """Retried, because the harness comes up seconds later — and bounded, because a
    control plane that is still silent after that is a real failure the follower
    will report, not a race to keep hammering."""
    text = _read(TERMINAL)
    case = _status_case(text, "resize_deferred")

    assert "GEOMETRY_RETRY_DELAYS" in case
    assert "setTimeout" in case
    assert "reportGeometry()" in case
    assert "geometryRetries >= GEOMETRY_RETRY_DELAYS.length" in case, (
        "an unbounded retry on a plane that never answers is a loop, not a fix"
    )

    delays = re.search(r"const GEOMETRY_RETRY_DELAYS = \[([^\]]+)\]", text)
    assert delays, "the retry backoff is not a named table"
    values = [int(v.strip()) for v in delays.group(1).split(",")]
    assert values == sorted(values), f"the backoff does not back off: {values}"
    assert values[0] >= 500, "a sub-500ms first retry is a busy-wait on a starting harness"


def test_a_retry_never_fires_into_a_terminal_that_is_gone():
    """A resize is a write to a PTY the session owns, so a timer that outlives its
    socket would reflow a session on behalf of a view nobody is looking at."""
    case = _status_case(_read(TERMINAL), "resize_deferred")

    assert "resizeOptIn.value" in case, "a retry must still respect the switch"
    assert "WebSocket.OPEN" in case, (
        "the retry must check the socket it belongs to is still open"
    )


def test_the_pending_retry_is_dropped_when_the_socket_goes():
    """And the budget resets, so a reconnect gets a fresh chance rather than
    spending what the previous attempt already used."""
    body = _function_body(_read(TERMINAL), "function teardownSocket")

    assert "clearTimeout(geometryRetryTimer)" in body
    assert "geometryRetries = 0" in body


def test_the_failed_case_still_says_the_session_is_ending():
    """The distinction only helps if the genuine failure keeps its own meaning."""
    case = _status_case(_read(TERMINAL), "resize_failed")

    assert "resizeSupported.value = false" in case
    assert "problem.value" in case


def test_the_terminal_controls_are_one_line_not_three_stacked_rows():
    """Reported by the operator: state, "earlier output" and the fit switch were
    three separate rows.

    On a phone that spent most of a screenful saying very little, above the one
    thing the view exists to show. They share a single flex row now — and it must
    stay `flex-wrap`, because "one line" is a desktop outcome, not a promise: at
    phone width these have to wrap rather than overflow the card.
    """
    text = _read(TERMINAL)
    markup = text[text.index("<template>"):]

    row_start = markup.index('class="d-flex flex-wrap align-center ga-2 mb-2"')
    row = markup[row_start:markup.index("<v-alert", row_start)]

    assert "earlier output" in row, "the way back to older output left the status row"
    assert 'label="fit to my screen"' in row, "the fit control left the status row"
    assert "flex-wrap" in markup[row_start - 80:row_start + 60], (
        "the row must wrap on a narrow screen instead of overflowing"
    )


def test_the_fit_explanation_is_reachable_without_a_mouse():
    """It moved into a tooltip, and this UI is mostly driven from a phone.

    A tooltip that only opens on hover is not an explanation on a touch device —
    it is text nobody can read. The switch itself stays labelled either way, so
    the tooltip carries the *why*, never the *what*.
    """
    text = _read(TERMINAL)
    markup = text[text.index("<template>"):]

    assert "<v-tooltip" in markup
    tooltip = markup[markup.index("<v-tooltip"):markup.index("</v-tooltip>")]
    assert "open-on-click" in tooltip, (
        "hover-only means the explanation is unreachable on the device this is for"
    )
    assert "looking over someone's shoulder" in _prose(tooltip), (
        "the situation the default is wrong for has to be what the tooltip says"
    )


# --- terminal height presets -------------------------------------------------

def test_the_height_presets_scale_every_bound_not_just_the_height():
    """The operator wanted more height, offered as x1/x1.5/x2.

    The trap: `max-height` was a flat 560px while the height is 55dvh. On any
    desktop where 55dvh already exceeds 560px, scaling only the height changes
    nothing — the cap eats it, and the control looks broken rather than absent. So
    all three bounds carry the multiplier.
    """
    css = _read(TERMINAL)
    css = css[css.index(".terminal-host {"):]
    block = css[:css.index("}")]

    for bound in ("height:", "min-height:", "max-height:"):
        line = [ln for ln in block.splitlines() if ln.strip().startswith(bound)]
        assert line, f"{bound} vanished from .terminal-host"
        assert all("var(--term-scale)" in ln for ln in line), (
            f"{bound} is not scaled, so a preset cannot move it: {line}"
        )


def test_x1_is_the_default_and_is_what_it_always_was():
    """A preset set that changes the default is a silent change to every session."""
    text = _read(TERMINAL)

    scales = re.search(r"const HEIGHT_SCALES = \[([^\]]+)\]", text)
    assert scales, "the preset set is not a named list"
    values = [float(v) for v in scales.group(1).split(",")]
    assert values[0] == 1, f"the first preset must be the current size, got {values[0]}"
    assert values == sorted(values), f"presets should ascend: {values}"

    assert "--term-scale: 1;" in text, "the unscaled default is not the CSS fallback"
    assert "ref(storedHeightScale())" in text


def test_an_unusable_stored_height_falls_back_instead_of_collapsing_the_box():
    """A stale or hand-edited key must not reach `calc()`.

    A bad multiplier there does not fail loudly — it collapses the emulator to
    nothing, which reads as a broken terminal rather than a bad preference. Same
    rule as any other stored UI value: validate, never trust.
    """
    # The policy moved into web/src/preferences.js when the third remembered
    # preference arrived (T49) — validate, fall back, tolerate a throwing store.
    # So this asserts on both halves: that the terminal delegates rather than
    # hand-rolling, and that the shared policy actually does those three things.
    # Asserting only the call site would pass against a helper that trusts its
    # input, which is the failure this test exists to prevent.
    body = _function_body(_read(TERMINAL), "function storedHeightScale")
    assert "HEIGHT_SCALES" in body, "the allowed set is not handed to the reader"
    assert "storedChoice(" in body, (
        "the terminal validates its own stored value again instead of using the "
        "shared policy; a second copy is a second chance to omit the fallback"
    )

    policy = (WEB / "src" / "preferences.js").read_text(encoding="utf-8")
    chosen = _function_body(policy, "export function storedChoice")
    assert "allowed.includes" in chosen, "the stored value is not validated"
    assert "allowed[0]" in chosen, "there is no fallback to the default"
    assert "catch" in policy, (
        "localStorage throws rather than returning null in private mode and some "
        "webviews; a preference is never worth failing to render over"
    )


def test_setting_the_height_validates_before_it_stores():
    """Otherwise the next page load reads back something no preset offers."""
    body = _function_body(_read(TERMINAL), "function setHeightScale")

    assert "HEIGHT_SCALES.includes(scale)" in body
    assert body.index("includes(scale)") < body.index("setItem"), (
        "it stores before validating, so a bad value survives the reload"
    )


def test_growing_the_terminal_does_not_write_to_the_session_by_itself():
    """Height is this screen's business. The fit switch is the only thing that
    writes geometry to a PTY, and it is still gated on the opt-in — a preset must
    not become a second, ungated path to resizing somebody else's session."""
    body = _function_body(_read(TERMINAL), "function setHeightScale")

    assert "socket.send" not in body, "the height control writes to the session"
    assert "reportGeometry" not in body, (
        "the refit must come from the ResizeObserver, which is debounced and "
        "already respects the opt-in — not from a direct call here"
    )


def test_a_recovered_fit_stops_saying_the_session_is_still_starting():
    """Reported by the operator: the message stayed on screen after the retry had
    worked.

    A *successful* resize is answered with silence by design — the server only
    speaks up to refuse one — so there is no success event to clear the notice on.
    Clearing it as the next attempt goes out is the only signal available, and it
    is self-correcting: a refusal comes back within a round trip and writes its own
    line again.
    """
    body = _function_body(_read(TERMINAL), "function reportGeometry")

    assert "geometryDeferred" in body, (
        "nothing clears the still-starting line, so it outlives the condition"
    )
    assert "notice.value = null" in body
    assert body.index("geometryDeferred") < body.index("socket.send"), (
        "the clear must happen as the attempt goes out, not after a reply that "
        "never comes for a successful resize"
    )


def test_giving_up_on_the_fit_is_not_a_latch():
    """Exhausting the budget must not be permanent either.

    The ResizeObserver keeps calling reportGeometry on a rotation, a tab switch or
    a height change, and any of those clears the line if the plane has come up
    since. The operator hit exactly this: the terminal resized fine while the screen
    still said fitting had been given up on.
    """
    text = _read(TERMINAL)
    case = _status_case(text, "resize_deferred")

    assert "resizeSupported.value = false" not in case
    assert "resizeOptIn.value = false" not in case
    # The flag is set on both branches, so the give-up line is clearable too.
    assert case.index("geometryDeferred = true") < case.index("GEOMETRY_RETRY_DELAYS.length"), (
        "the give-up branch does not mark the line as owed a clear, so only the "
        "retrying branch recovers"
    )


def test_the_fit_retry_budget_outlasts_a_container_start():
    """1s+2s+4s gave up after seven seconds, on sessions that came up later.

    The operator saw 'giving up on fitting for now' on a terminal that then resized
    fine, which means the budget expired before the harness was listening rather
    than because anything was wrong.
    """
    text = _read(TERMINAL)
    delays = re.search(r"const GEOMETRY_RETRY_DELAYS = \[([^\]]+)\]", text)
    assert delays, "the retry backoff is not a named table"
    values = [int(v.strip()) for v in delays.group(1).split(",")]

    assert values == sorted(values), f"the backoff does not back off: {values}"
    assert sum(values) >= 20000, (
        f"the budget is only {sum(values)}ms; a container start can exceed that, "
        "and giving up early reports a failure on a session that is merely slow"
    )


def test_the_state_chip_shares_its_row_with_the_controls():
    """The operator asked: "the live badge ... still is on its own row".

    The markup was already one flex row; two things split it in practice. A
    `v-spacer` right-aligned the controls, so the moment the row wrapped the chip
    was alone on the first line. And the notice sat second — it is the only element
    here with unbounded width, so a long one filled line one and pushed every
    fixed-size control below it.

    So: no spacer, and the prose goes last. `flex-wrap` stays, because a narrow
    screen must wrap rather than overflow — what wraps now is the sentence, which
    is the part that can afford to.
    """
    detail = _read(TERMINAL)
    start = detail.index('class="d-flex flex-wrap align-center ga-2 mb-2"')
    row = detail[start:detail.index("<v-alert", start)]

    assert "<v-spacer" not in row, (
        "a spacer right-aligns the controls, which strands the state chip on its "
        "own line as soon as the row wraps"
    )
    assert "flex-wrap" in detail[start - 60:start + 60], "the row must still wrap"

    # The chip is first and the unbounded prose is last; everything fixed-width
    # sits between them.
    assert row.index("toneColor(meta.tone)") < row.index('label="fit to my screen"')
    assert row.index('label="fit to my screen"') < row.index("{{ notice }}"), (
        "the notice is back in front of the controls, so a long one pushes them "
        "onto a second row again"
    )


# --- pasting into the terminal -----------------------------------------------
#
# Operator, live testing: pasting only worked through the context menu's "paste as
# plain text"; Ctrl+V did nothing. Nothing in this component was eating it —
# xterm's own keydown listener resolves Ctrl+V the way a terminal does (keyCode 86
# with Ctrl is the control byte 0x16) and then cancels the event, and a cancelled
# keydown has no default action, so the browser never fires `paste`. The context
# menu worked because that path dispatches a ClipboardEvent with no keydown in
# front of it.
#
# Every guard below is invisible from a build: the component compiles, renders and
# types fine with the paste chord going to the PTY as 0x16.

def test_the_paste_chord_is_handed_back_to_the_browser():
    """The fix, pinned at the seam it lives in.

    Declining the chord is what leaves the keydown's default action alone, so the
    browser pastes into xterm's textarea and xterm's own paste listener picks it
    up. Anything that "handles" paste here instead — encoding it, or reading the
    clipboard — is a second input path, and the reason there is not one is in
    ``isBrowserPaste``.
    """
    text = _read(TERMINAL)
    build = _function_body(text, "function buildTerminal")
    assert "if (isBrowserPaste(event) || isBrowserCopy(event)) return false" in build, (
        "xterm is encoding Ctrl+V as 0x16 again, which cancels the keydown and "
        "leaves the browser with no paste to perform"
    )
    # In buildTerminal, not onMounted: the emulator is rebuilt for "earlier output"
    # and for a new session, and an attach that happened once would be lost there —
    # paste working until you press a button is worse than paste never working.
    assert text.count("attachCustomKeyEventHandler") == 1, (
        "a second attach replaces the first; xterm keeps only the last handler"
    )

    body = _function_body(text, "function isBrowserPaste")
    assert "ctrlKey" in body and "metaKey" in body, (
        "the decline is not modifier-gated, so plain letters stop reaching the PTY"
    )
    assert "sendInput" not in body and "socket.send" not in body, (
        "this predicate answers a question; sending from it would deliver the "
        "chord as well as the paste"
    )


def test_nothing_here_reads_the_clipboard_itself():
    """navigator.clipboard.readText needs a secure context.

    This platform is served over plain http on a LAN as often as not, where the
    API is simply absent — a fix that depended on it would work on localhost and
    fail in the deployment the platform exists for. The browser does the read; the
    text arrives as a paste event xterm already handles.
    """
    text = _read(TERMINAL)
    code = _code(text)
    assert "navigator.clipboard" not in code, (
        "the clipboard API is unavailable over plain http, so this is a paste that "
        "works only on localhost"
    )
    assert "term.paste(" not in code, (
        "pushing text into the emulator by hand is a second input path, and it "
        "doubles whatever the browser's own paste already delivered"
    )
    # The pasted bytes reach the session the way typed ones do: xterm hands them to
    # onData, which is sendInput, which is the only place an input frame is built.
    assert text.count("type: 'input'") == 1, "a second input path can drift"


def test_shift_insert_pastes_too():
    """The other paste chord, and the one a terminal user reaches for.

    xterm leaves it alone today (its insert case checks for the modifier and
    declines to encode), so this is a statement of intent rather than a fix — which
    is exactly why it needs a test: the day that changes upstream, nothing about
    this component looks different.
    """
    body = _function_body(_read(TERMINAL), "function isBrowserPaste")
    assert "'Insert'" in body and "shiftKey" in body, (
        "Shift+Insert is not recognised as a paste, so an xterm that starts "
        "encoding it takes the chord away with no visible symptom"
    )


def test_only_a_keydown_is_declined():
    """xterm asks this handler about keyup and keypress as well.

    A declined keyup skips the focus and cursor-style bookkeeping xterm does
    there, which is a terminal that stops taking input for reasons no part of this
    component would appear to be responsible for.
    """
    body = _function_body(_read(TERMINAL), "function isBrowserPaste")
    assert "if (event.type !== 'keydown') return false" in body
    assert body.index("event.type !== 'keydown'") < body.index("ctrlKey"), (
        "the chord is matched before the event type, so a keyup for the same keys "
        "is declined as well"
    )


# --- copying out of the terminal ----------------------------------------------
#
# The other half of the clipboard, and a different shape of problem from paste.
# Select-and-Ctrl+C cannot work here at all: that chord is the interrupt byte, and
# a terminal that copied instead would have no way to send it. What is left splits
# in two.
#
# Ctrl+Insert and Cmd+C are the browser's own copy chords, xterm declines to
# encode either, and xterm registers a `copy` listener on the terminal element
# that answers with the *terminal's* selection text — so those already work and
# are named here so that an upstream change cannot take them away quietly.
#
# Ctrl+Shift+C is nobody's: no browser binds it to copy (in Chrome and Firefox it
# opens the inspector), so unlike Ctrl+V there was nothing to hand back — xterm
# already leaves the chord alone. It is therefore the one chord this component
# performs itself, through the copy the browser would have run.

def test_ctrl_c_is_not_a_copy_chord_and_never_becomes_one():
    """The one that would be catastrophic and silent.

    Ctrl+C is how a session is interrupted, and it is the first half of the quit
    chord these TUIs use — a copy binding that swallowed it would leave the
    keyboard with no way to stop a run, while the button in the composer went on
    working. So the letter chords are matched on Cmd (and on Ctrl *with* Shift),
    never on Ctrl alone.
    """
    text = _read(TERMINAL)
    browser_copy = _function_body(text, "function isBrowserCopy")
    assert "event.metaKey && !event.ctrlKey" in browser_copy, (
        "the plain copy chord matches Ctrl, which is the interrupt byte"
    )
    terminal_copy = _function_body(text, "function isTerminalCopy")
    assert "event.ctrlKey && event.shiftKey" in terminal_copy, (
        "the terminal copy chord no longer requires Shift, so Ctrl+C is a copy "
        "and nothing can interrupt the session from a keyboard"
    )
    # And the interrupt still exists as a byte the component sends deliberately.
    assert "interrupt: '\\x03'" in text, "the interrupt key left the key table"


def test_the_copy_chords_the_browser_performs_are_handed_back_to_it():
    """Ctrl+Insert and Cmd+C, declined for the reason Shift+Insert is.

    Both work today because xterm resolves them to no key and returns without
    cancelling, which leaves the browser's copy to run — and that copy is answered
    by xterm's own listener with the terminal's selection rather than the DOM's.
    Naming them is what makes the day upstream changes visible here.
    """
    text = _read(TERMINAL)
    body = _function_body(text, "function isBrowserCopy")
    assert "'Insert'" in body and "event.ctrlKey && !event.shiftKey" in body, (
        "Ctrl+Insert is no longer recognised as a copy"
    )
    assert "'KeyC'" in body, (
        "only the letter is matched, so a non-Latin layout cannot copy"
    )
    assert "execCommand" not in body and "notice" not in body, (
        "this predicate answers a question; a copy performed from it would run "
        "twice for chords the browser copies on its own"
    )


def test_the_chord_no_browser_copies_on_is_performed_here():
    """Ctrl+Shift+C, and the reason it is the exception.

    There is nothing to hand back — xterm already leaves it alone and the
    browser's default action for it is the inspector — so declining it on its own
    would change nothing at all. The handler therefore performs the copy and only
    then takes the chord, which is also what leaves it alone when there is no
    selection to copy.
    """
    build = _function_body(_read(TERMINAL), "function buildTerminal")
    assert "if (isTerminalCopy(event) && copySelection()) {" in build, (
        "the chord is taken unconditionally, so pressing it with nothing selected "
        "swallows a chord that would otherwise have done something"
    )
    assert "event.preventDefault()" in build, (
        "the chord was copied and then also left to its other meaning"
    )


def test_the_copy_is_the_browsers_and_uses_no_secure_context_api():
    """Same constraint as the paste path: plain http on a LAN.

    navigator.clipboard.writeText needs a secure context, so a copy built on it
    would work on localhost and fail in the deployment this platform is for. The
    deprecated command is the one that still runs there — and it dispatches the
    event xterm's own copy listener answers, so the text this component moves is
    the text the terminal already had selected.
    """
    text = _read(TERMINAL)
    body = _function_body(text, "function copySelection")
    assert "document.execCommand('copy')" in body, (
        "the copy no longer goes through the browser's own copy"
    )
    assert "term.getSelection()" in body, "the copy takes text from somewhere else"
    # The selection is parked in xterm's textarea first, which is the trick xterm
    # itself uses for the right-click menu: a browser asked to copy with nothing
    # selected anywhere may decline to fire the event at all.
    assert "textarea.select()" in body
    assert "textarea.value = ''" in body, (
        "the selection is left sitting in the textarea xterm types out of"
    )


def test_a_copy_the_browser_refused_is_not_silent():
    """A clipboard that did not change is discovered at the paste, too late.

    By then whatever was on it before is what lands, which reads as this UI having
    copied the wrong thing rather than as its not having copied at all — so a
    refusal names the chords that do work instead.
    """
    body = _function_body(_read(TERMINAL), "function copySelection")
    assert "if (!copied) {" in body and "notice.value =" in body, (
        "a refused copy leaves no trace on screen"
    )
    assert "Ctrl+Insert" in body, "the refusal does not say what to try instead"


# --- the emulator has to have the keyboard ------------------------------------
#
# Reported from live testing as flaky paste: Ctrl+V worked sometimes. It was not
# flaky — the chord goes to whatever has the focus, and after clicking a tab, a
# switch, or anywhere but the emulator itself, that is not the terminal. xterm
# focuses itself only from a click that lands on its own element.

def test_clicking_the_terminal_box_focuses_the_emulator():
    """Including the padding, which is what "click the terminal" means to a person."""
    text = _read(TERMINAL)
    assert '@click="focusTerminal"' in text, (
        "the terminal box no longer takes the keyboard when it is clicked"
    )
    body = _function_body(text, "function focusTerminal")
    assert "term.focus()" in body


def test_a_sliver_fit_is_dropped_never_clamped():
    """A 1-2 column fit reading is a layout artifact, not a window size.

    fit() against a mid-animation sliver (a tab panel between states) proposes a
    couple of columns, and an earlier clamp-to-legal turned exactly that into a
    real write that reflowed a live session's TUI to one character per line for
    every watcher. Below-floor readings must be dropped — the next real layout
    reports the real size — and the floor must be a real-terminal floor, not the
    control plane's legality bound of 1.
    """
    text = _read(TERMINAL)
    assert "const MIN_COLS = 20" in text and "const MIN_ROWS = 5" in text, (
        "the floors are real-terminal floors; MIN_DIMENSION = 1 was the bug"
    )
    assert "MIN_DIMENSION" not in text, (
        "a single legality floor of 1 is what let a sliver layout resize a "
        "shared PTY to one column"
    )
    assert ("clampDimension(term.rows, MIN_ROWS)" in text
            and "clampDimension(term.cols, MIN_COLS)" in text), (
        "each axis is checked against its own floor before a resize is sent"
    )


def test_an_unfocused_terminal_says_so_where_the_keyboard_is_real():
    """The hint exists because the failure is invisible: nothing happens.

    Shown only while there is something to type into and the emulator has not got
    the keyboard, and hidden where the pointer is a finger — there the composer is
    the way in, the emulator is never focused, and this would be a permanent line
    of wrong advice.
    """
    text = _read(TERMINAL)
    tracked = ('@focusin="focused = true"' in text
               and '@focusout="focused = false"' in text)
    assert tracked, (
        "focus is tracked with events that do not bubble, so the box never learns "
        "that xterm's hidden textarea took the keyboard"
    )
    assert 'v-if="canType && !focused"' in text, (
        "the hint is shown for a session there is nothing to type into, or while "
        "the terminal already has the keyboard"
    )
    assert _prose("click the terminal to type or paste into it") in _prose(text)
    style = text[text.index("<style scoped>"):]
    coarse = style[style.index("@media (pointer: coarse)"):]
    assert ".focus-hint" in coarse, (
        "the hint is not hidden for a coarse pointer, where it is both permanent "
        "and untrue"
    )


# --- the launch is in the other log (#141 T79) --------------------------------
#
# The server serves whichever of a session's two logs is the record: the one the
# session writes from inside its container wherever that exists, the host-side tee
# otherwise. For a session that records itself that leaves the launch — the image
# pull, the clone, lmer's own lines, all printed before the container had a log to
# write into — behind the first byte of the file this terminal is paging through.
#
# "earlier output" cannot reach it and must not pretend to: it stops at the origin
# of the canonical log, which for those sessions is the harness's beginning and
# not the session's. So the other file is a second, deliberate read, shown on its
# own — never stitched in front, because nothing here or on the server can tell
# where one stream ends inside the other.

def test_earlier_output_stops_at_the_origin_of_the_canonical_log():
    """Zero is the beginning of the record, whichever file the record is in.

    Taken from the offset the server resolved for the replay and from nothing else:
    a client that decided this from the size, or from which source answered, would
    either offer a step that re-reads the same bytes or hide one that works.
    """
    text = _read(TERMINAL)
    assert "earlierExists.value = chunk.offset > 0" in text, (
        "the end of this stream's history is decided by something other than the "
        "resolved offset of the replay"
    )
    earlier = re.search(r"const canLoadEarlier = computed\((.+?)\n\)", text, re.S)
    assert earlier and "earlierExists.value" in earlier.group(1)


def test_the_launch_view_is_offered_only_where_there_is_a_second_log():
    """Mixed fleet: an older image writes no log of its own, and nothing changes.

    The affordance hangs on the source the server reported, which is a probe of a
    file on the host — never on a version, and never on an assumption made here.
    A session served from the tee already has its launch in the scrollback.
    """
    text = _read(TERMINAL)
    launch = re.search(r"const canShowLaunch = computed\((.+?)\n\)", text, re.S)
    assert launch, "the launch affordance is not gated at all"
    assert "logSource.value === CONTAINER_LOG" in launch.group(1), (
        "the launch view is offered for a session whose record is the host tee, "
        "where it would show the same bytes a second time"
    )
    render = _function_body(text, "function render")
    assert "logSource.value = frame.source" in render, (
        "the source is taken once and believed forever, so a session that starts "
        "recording itself while it is being watched never grows the affordance"
    )
    assert 'v-if="canShowLaunch"' in text and "launch output" in text


def test_the_launch_read_asks_for_the_head_of_the_host_log_by_name():
    """The read-only parameter T79 added to the route, used for exactly one thing."""
    from lmer_platform.session_io import LOG_SOURCE_HOST, MAX_LOG_LIMIT

    text = _read(TERMINAL)
    assert f"const HOST_LOG = '{LOG_SOURCE_HOST}'" in text, (
        "the client and the server disagree about what the host tee is called"
    )
    body = _function_body(text, "async function fetchLaunchLog")
    assert "source: HOST_LOG" in body, "the launch read takes whatever is canonical"
    assert "offset: '0'" in body, (
        "the launch read does not start at the beginning of the file, which is "
        "the only part of it that is not the session's own output again"
    )
    bound = re.search(r"const LAUNCH_BYTES = (\d+) \* (\d+)", text)
    assert bound, "the launch read is unbounded"
    assert int(bound.group(1)) * int(bound.group(2)) <= MAX_LOG_LIMIT


def test_the_launch_read_never_moves_the_terminals_cursor():
    """Two files, two offset spaces, and one cursor that belongs to the other one.

    The socket resumes from this component's cursor, so a number taken from the
    launch read would resume the live stream at a byte position that means
    something else there — the exact failure every offset in this component is
    arranged to prevent. It is written to the emulator and nothing else.
    """
    # Comments stripped: this function explains at length which cursor it must not
    # touch, and a substring check cannot tell that sentence from an assignment.
    body = _code(_function_body(_read(TERMINAL), "async function showLaunchOutput"))
    assert "term.write(decodeLogData(chunk.data))" in body
    assert "cursor" not in body, (
        "the launch read assigns the cursor the live stream resumes from"
    )
    assert "connect(" not in body, (
        "the launch view attaches a socket, which would stream the canonical log "
        "into a terminal showing the other one"
    )
    assert "earlierExists" not in body, (
        "the launch view drives the paging control of the stream it is not showing"
    )


def test_the_two_logs_are_shown_one_at_a_time_and_never_spliced():
    """Not a merge, and the view says which one it is.

    Stitching them would mean guessing where the host-side record of this session
    ends inside the file that also holds everything the container forwarded — the
    guess would show a stretch of output twice, as history that happened twice.
    """
    text = _read(TERMINAL)
    start = _function_body(text, "async function start")
    assert "if (showingLaunch.value) {" in start and "buildTerminal()" in start, (
        "the launch output is written into the terminal that is already showing "
        "the session, which splices two offset spaces into one screen"
    )
    assert "await showLaunchOutput(mine)" in start
    assert "launch: { label: 'launch output'" in text, (
        "the launch view borrows another phase, so the chip claims the session is "
        "history or live while a recording of its launch is on screen"
    )
    assert _prose("A separate recording, not earlier scrollback") in _prose(text)


def test_a_launch_read_that_found_nothing_says_so():
    """No host log is an ordinary answer, not a failure.

    The tee is written by the daemon that spawned the session, and a session
    adopted from elsewhere — or one whose logs were pruned unevenly — has none. An
    empty screen with no explanation reads as the view being broken.
    """
    text = _read(TERMINAL)
    body = _function_body(text, "async function showLaunchOutput")
    assert "launchEmpty.value = !chunk.size" in body
    assert _prose("Nothing was recorded on the host for this session") in _prose(text)
    assert "launchTruncated" in body, (
        "a head that is only the first part of a much longer file is presented as "
        "the whole of it"
    )


def test_the_launch_view_is_left_behind_when_the_session_changes():
    """A respawn is a different session and a different pair of files.

    Staying in this view would show one session's launch under another's heading,
    and the new one may be from an image that writes no log of its own — in which
    case the affordance that got here does not exist for it.
    """
    text = _read(TERMINAL)
    watcher = text[text.index("watch(() => props.sessionId"):]
    watcher = watcher[:watcher.index("})")]
    assert "showingLaunch.value = false" in watcher, (
        "the launch view survives a respawn"
    )
    assert "logSource.value = null" in watcher, (
        "the previous session's source decides whether the new one is offered a "
        "launch view"
    )
