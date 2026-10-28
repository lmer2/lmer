"""Guards on the uber lmer drawer — the operator's chat with the supervisor (T31).

Source-level invariants, like every other web guard here: this image has no
browser, and the properties below are ones a screenshot cannot check anyway.

The feature is one drawer, and almost all of its design is a defence against a
single mistake: telling the supervisor to stop a run while actually typing at the
run. Nothing about that mistake is visible in a build, in a screenshot, or in a
desktop window — the message goes somewhere, a session answers, and the operator
finds out later. So what is pinned is what keeps the two conversations apart and
what keeps the drawer honest about a supervisor that is not there:

- the drawer is the RIGHT one. The run navigator owns the left edge, and two
  conversations one drawer-width apart on the same edge is the mistake set up
  rather than guarded (:mod:`tests.test_platform_web_shell` owns the other side);
- the words. A worker session is "lmer" and the supervising one is "uber lmer"
  (operator request, 2026-07-27); "assistant" is the code's spelling and reaches an
  operator nowhere. Two different names is the other half of the guard, and the
  check is on the strings an operator can actually read — not on identifiers, which
  keep the API's spelling on purpose;
- the conversation is ``Chat.vue`` pointed at the supervisor's session, not a
  parallel implementation of the same transcript-read and control-plane-write. A
  copy would be a second place for the delay between sending a message and seeing
  it to be got wrong, and a second consumer of the markdown renderer to forget the
  sanitiser in;
- a supervisor that is not running says so and can be started, because the drawer
  is useless in exactly the state an operator most needs it in;
- a status read that failed degrades instead of resetting. An emptied drawer reads
  as "there is no uber lmer" and offers to start a second one;
- the digest spool is never drained from here. That is the decision T69 left open,
  it is recorded in api.js, and this file is what keeps it: the only non-consuming
  read that exists is the count on the status, so the count is what is shown;
- the standing orders are shown and never edited here (T87). The operator asked for
  the chat to be the write path rather than a settings screen, so a textarea beside
  the document would be a second writer of it and would skip the confirm-the-wording
  step that makes the stored rule theirs;
- the fleet view pays nothing for the drawer until it is opened, and the drawer
  remembers nothing in a browser store.

How any of it looks, and whether the app bar now crowds a phone, are verified by
building the bundle and by live test LT3 on a real phone.
"""

import json
import re
import subprocess
from pathlib import Path

from tests.conftest import node_binary, require_node_toolchain

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
APP = WEB / "src" / "App.vue"
DRAWER = WEB / "src" / "components" / "AssistantChat.vue"
CHAT = WEB / "src" / "components" / "Chat.vue"
API_JS = WEB / "src" / "api.js"
API_PY = ROOT / "src" / "lmer_platform" / "api.py"

#: The attributes that put words on a screen without putting them in a text node.
#: Static ones only — a bound ``:label`` names an identifier, which is code.
#: ``composer-label`` and ``agent-label`` are ours rather than Vuetify's (Chat.vue's
#: props), and they are here for the same reason the rest are: whatever is passed
#: down them is read by the operator — the composer's send line and the name over
#: every agent turn.
VISIBLE_ATTRS = (
    "label", "composer-label", "agent-label", "aria-label", "hint", "title",
    "placeholder", "text",
)


def _read(path):
    return path.read_text(encoding="utf-8")


def _template(path):
    """The markup half of a single-file component."""
    text = _read(path)
    start = text.index("<template>")
    return text[start:text.rindex("</template>")]


def _drawer_tag(location):
    """One of App.vue's navigation drawers, by the side it is docked to."""
    for match in re.finditer(r"<v-navigation-drawer\b[^>]*>", _read(APP), re.S):
        if f'location="{location}"' in match.group(0):
            return match.group(0)
    raise AssertionError(f"App.vue has no {location}-hand navigation drawer")


def _operator_words(path):
    """Every word an operator can read in a component's markup.

    Text nodes plus the static label-ish attributes, with comments, tags and
    interpolations removed. The distinction is the whole point of this file: an
    identifier keeps the API's spelling (``fetchAssistant``, ``status.pending``)
    and a comment explains the code, while a *label* is what the operator is told —
    so only the third is checked for the words the UI is not allowed to use.
    """
    markup = _template(path)
    labels = []
    for attr in VISIBLE_ATTRS:
        labels += re.findall(rf'(?<![:\w-]){attr}="([^"]*)"', markup)
    prose = re.sub(r"<!--.*?-->", " ", markup, flags=re.S)
    prose = re.sub(r"\{\{.*?\}\}", " ", prose, flags=re.S)
    prose = re.sub(r"<[^>]*>", " ", prose, flags=re.S)
    return " ".join([prose, *labels])


# --- which side, and reachable from where -------------------------------------

def test_the_supervisors_chat_is_the_right_hand_drawer():
    """The one decision the rest of the feature rests on (the operator's explicit
    choice).

    A drawer that looked like the left one, or shared its edge, would make the
    wrong-recipient mistake *easier*: the gesture that opens the run list and the
    gesture that opens the supervisor would be the same one, a few hundred
    pixels apart. So the side is pinned here and the navigator's side is pinned in
    :mod:`tests.test_platform_web_shell`, from both ends.
    """
    tag = _drawer_tag("right")
    assert 'v-model="uberOpen"' in tag, "the right drawer is not the one being toggled"
    app = _read(APP)
    assert "<AssistantChat" in app, "the right drawer holds no chat"
    assert re.search(r"<AssistantChat[^>]*:now=\"now\"", app, re.S), (
        "the chat is not given the shell's clock, so its timestamps stop ageing"
    )
    assert re.search(r"<AssistantChat[^>]*@close=\"uberOpen = false\"", app, re.S), (
        "the drawer's own close button closes nothing"
    )
    # And the navigator is still on the other side, in the same file: the two
    # halves of this decision are one edit apart.
    assert "<RunNav" in _drawer_tag("left") + app


def test_the_drawer_is_reachable_from_the_app_bar_on_every_view():
    """"Stop that run" has to be one tap from wherever you noticed it.

    In the bar rather than on the fleet view, because the supervisor belongs to the
    app and not to one run — and with a name, because an icon-only button is
    unusable to a screen reader and ambiguous to everyone else next to a second
    icon-only drawer toggle.
    """
    app = _read(APP)
    button = None
    for match in re.finditer(r"<v-btn\b[^>]*>.*?</v-btn>", app, re.S):
        if '@click="toggleProjectChat"' in match.group(0):
            button = match.group(0)
            break
    assert button, "nothing in the shell opens the supervisor's drawer"
    toggle = app.split("function toggleProjectChat()", 1)[1].split("\n}", 1)[0]
    assert "if (uberOpen.value) uberOpen.value = false" in toggle, "the bar button cannot close it again"
    assert "openProjectChat(activeProject.value || chatProject.value)" in toggle, "reopening lost its project destination"
    assert "aria-label" in button, "an icon-only button needs a name"
    assert "RatchetFace" in button, (
        "the toggle draws no face, so the assistant's mood has no home in the bar"
    )
    assert "RatchetFace" in _read(DRAWER), (
        "the bar button and the drawer's header no longer share a face, so "
        "nothing connects the tap to the panel it opened"
    )


# --- the words ----------------------------------------------------------------

def test_the_supervisor_is_called_uber_lmer_and_never_an_assistant():
    text = _read(DRAWER)
    assert '${props.workspace.name} · ${assistantName.value}' in text
    assert ':agent-label="assistantName"' in text
    assert 'Waiting to start — assistant limit reached' in text
    # Issue 575: the pane's colour and toolbar say whose it is; the intro lines went.
    assert 'this chat belong to this project' not in ' '.join(text.split())


def test_the_composer_says_who_is_at_the_other_end():
    text = _read(DRAWER)
    assert 'You are talking to' not in text
    assert ':composer-label="`Say something to ${workspace.name} · ${assistantName}`"' in text
    assert ':workspace-id="workspace.id"' in text
    assert 'say something to this session' in _read(CHAT)


def test_the_pane_fills_the_window_and_the_run_view_keeps_its_bound():
    """Issue 575: at 4K the transcript stopped at half the window. The pane is a
    full-height column and Chat's ``fill`` mode lets the conversation take what is
    left; the run view's chat keeps its viewport-share bound, which it needs as a
    tab panel with a composer under it."""
    drawer = _read(DRAWER)
    assert re.search(r"<Chat\b[^>]*\bfill\b", drawer, re.S), "the pane's chat is not in fill mode"
    assert re.search(r"\.uber-pane \{[^}]*min-height: 100%", drawer), "the pane is not a full-height column"
    assert ".uber-pane-slot {\n  height: 100%;" in _read(APP), "the drawer's wrapper does not pass the height on"
    chat = _read(CHAT)
    assert re.search(r"\.chat-fill \.chat \{[^}]*flex: 1 1 0;[^}]*max-height: none;", chat), (
        "fill mode does not lift the transcript's bound"
    )
    assert "max-height: 50dvh;" in chat, "the run view lost its bound"
    run_view = _read(WEB / "src" / "components" / "RunDetail.vue")
    tag = run_view[run_view.index("<Chat"):]
    assert not re.search(r"\bfill\b", tag[:tag.index(">")]), "the run view's chat is in fill mode"


def test_the_conversation_is_the_shared_chat_pointed_at_the_supervisor():
    """Reused, not rebuilt: the supervisor is an lmer session with a session id.

    ``Chat.vue`` already owns the pair of routes this needs — the polled transcript
    read and the control-plane write — plus the held-back bubble for a message the
    transcript has not caught up with. A parallel implementation would be a second
    place to get that delay wrong and a second consumer of the markdown renderer
    to forget the sanitiser in, so a copy here is a failure even when it works.
    """
    text = _read(DRAWER)
    assert "import Chat from './Chat.vue'" in text, "the shared conversation is not used"
    assert re.search(r"<Chat[^>]*:session-id=\"sessionId\"", text, re.S), (
        "the conversation is not pointed at a session"
    )
    assert "status.value.session_id" in text, (
        "the session comes from somewhere other than the supervisor's status, which "
        "is the only thing that knows which session it is"
    )

    # The read and the write stay Chat's. Naming either route here is the copy.
    for route in ("/messages", "/input", "fetchSessionMessages", "sendSessionInput"):
        assert route not in text, (
            f"the drawer talks to {route} itself instead of through the shared chat"
        )
    for route in ("/messages", "/input"):
        assert route in _read(CHAT), "the shared chat no longer owns the session routes"

    # And the renderer is reached through that chat, which defers it into its own
    # chunk — a static import or a second v-html here would undo T42 for the whole
    # app (tests/test_platform_web_bundle.py is the gate on the built output).
    assert "Markdown" not in text, "the drawer reaches for the renderer directly"
    assert "v-html" not in text, "the drawer injects markup of its own"


def test_the_runs_two_chat_props_are_left_to_the_run_that_has_them():
    """A supervisor has no run and no ask channel, and pretending otherwise would
    put a "reply in the box above" warning in a drawer with no box above."""
    text = _read(DRAWER)
    for prop in ("question-pending", "ask-pending"):
        assert prop not in text, (
            f"the drawer passes {prop}, which is a fact about a run's session"
        )
    assert "questionPending" in _read(CHAT), (
        "the shared chat no longer has the run-shaped props this is about"
    )


# --- the states the drawer has to be honest about -----------------------------

def test_a_supervisor_that_is_not_running_says_so_and_offers_a_start():
    text = _read(DRAWER)
    assert 'Stopped until you choose Start' in text
    assert "status?.lifecycle === 'failed' ? 'Retry' : 'Start'" in text
    assert "lifecycle(starting, startAssistant)" in text
    assert "workspaceId)" in _client_body(_read(API_JS), 'startAssistant')
    assert ':loading="starting"' in text
    assert 'Your draft stays here' in text


def test_a_daemon_that_did_not_answer_degrades_instead_of_resetting():
    """An emptied drawer reads as "there is no uber lmer" — and offers to start a
    second one, which is the one refusal the route has to hand out.

    So a failed poll keeps the last answer and adds a line saying it is the last
    answer. Same rule the conversation and the operator channel already follow.
    """
    text = _read(DRAWER)
    catch = text[text.index("async function load()"):text.index("\n}\n", text.index("async function load()"))]
    assert "problem.value = exc.message" in catch, "a failed status read says nothing"
    assert "status.value = null" not in catch, (
        "a failed status read throws away what the daemon last reported"
    )
    words = _operator_words(DRAWER)
    assert "showing what the daemon last reported" in words, (
        "the stale state is shown as if it were current"
    )


def test_the_supervisors_generation_and_age_are_on_the_toolbar_tooltip_not_the_body():
    """Which incarnation you are talking to is not cosmetic: a rotation (§8.3) is
    a fresh context window, so a note it "already has" may be a note the one
    before it had.

    Issue 578 took the line out of the pane body ("nothing visible in the pane
    body"), so it lives in the robot icon's tooltip on the toolbar instead.
    """
    text = _read(DRAWER)
    script = text[:text.index("<template>")]
    identity = script[script.index("const identity = computed("):]
    identity = identity[:identity.index("\n})\n")]
    assert "status.value.generation" in identity, "nothing says which incarnation this is"
    assert "ago(status.value.started_at, props.now)" in identity, (
        "how long this one has been up is not shown, or is computed here rather "
        "than by the one helper that formats a relative time"
    )
    markup = _template(DRAWER)
    toolbar = markup[markup.index("<v-toolbar"):markup.index("</v-toolbar>")]
    tooltip = re.search(r'<v-tooltip\s[^>]*v-model="identityShown"[^>]*:text="identity"[^>]*>', toolbar, re.S)
    assert tooltip, "the incarnation is not on the toolbar's robot tooltip"
    # A tap's compatibility mouseenter opens it first, so a toggle on the click closes it.
    assert "open-on-click" not in tooltip.group(0), "a phone tap opens the tooltip and closes it again"
    assert '@click="identityShown = true"' in toolbar, "a phone, which has no hover, cannot open it"
    assert ':aria-label="identity"' in toolbar and 'tabindex="0"' in toolbar, (
        "the incarnation is out of reach of a screen reader or a keyboard"
    )
    body = markup[markup.index('<div class="uber-body'):]
    assert "generation" not in body and "started_at" not in body, (
        "the generation line is back in the pane body"
    )


# --- the lifecycle controls (T126) --------------------------------------------

def test_the_toolbar_offers_restart_and_stop_and_hides_the_verbs_the_daemon_would_refuse():
    """The operator asked for it in those words — "a way to restart the uber lmer
    process from the ux" — after a wedged incarnation could only be cleared by
    restarting the daemon on the host, which takes the fleet view down with it.

    Issue 575 put stop beside restart on the compact toolbar and removed the
    overflow menu: both ask before acting, so a mis-aimed tap costs a cancel. Both
    are offered only while uber lmer runs. Start is not on the toolbar at all: the
    pane's own Start button is drawn whenever nothing is running, and against a live
    one the route answers 409 to protect the incumbent's context window.
    """
    text = _read(DRAWER)
    toolbar = text[text.index("<v-toolbar"):text.index("</v-toolbar>")]

    for icon, confirm in (("mdiRestart", "confirmingRestart = true"),
                          ("mdiStopCircleOutline", "confirmingStop = true")):
        button = re.search(rf"<v-btn\b[^>]*{icon}[^>]*/>", toolbar, re.S)
        assert button, f"the toolbar has no {icon} control"
        assert 'v-if="running"' in button.group(0), (
            f"{icon} is offered against an uber lmer that is not running"
        )
        assert confirm in button.group(0), f"{icon} fires straight at the daemon instead of asking first"

    assert "<v-menu" not in toolbar, "the overflow menu is back (issue 575 removed it)"
    assert "mdiPlayCircleOutline" not in toolbar, (
        "the toolbar offers a start, which duplicates the pane's Start button and, "
        "against a live uber lmer, can only earn the route's 409"
    )

    # Nothing is drawn before the daemon has answered: a control cluster for a state
    # nobody has read yet is a guess at which verbs apply.
    assert 'v-if="status || problem"' in toolbar, (
        "the cluster is drawn before the first status arrives"
    )
    assert ':aria-label="`restart ${assistantName}`"' in toolbar
    assert ':aria-label="`stop ${assistantName}`"' in toolbar


def test_restart_is_the_rotate_route_and_the_other_two_are_their_own():
    """Three verbs, three routes, and the reason restart is not a stop and a start:
    two calls leave a window where another start lands between them and wins the
    generation counter, which is what ``rotate`` exists to close.

    ``exit`` is the wrong verb from both ends — it refuses ``kind="assistant"``
    outright, because ending the supervisor also has to clear the pointer to it and
    record why it ended — so the drawer must not be able to reach for it.
    """
    text = _read(DRAWER)
    api = _read(API_JS)
    for client, route in (
        ("rotateAssistant", "request(workspacePath('api/assistant/rotate', workspaceId), { method: 'POST' })"),
        ("stopAssistant", "request(workspacePath('api/assistant/stop', workspaceId), { method: 'POST' })"),
    ):
        assert client in text, f"the drawer does not call {client}"
        assert route in api, (
            f"{client} no longer posts the route the daemon serves: {route}"
        )
    assert "lifecycle(restarting, rotateAssistant)" in text, (
        "restart is not the rotation; a stop followed by a start leaves a gap for "
        "another start to win the generation counter in"
    )
    for wrong in ("/exit", "wind-down"):
        assert wrong not in text, (
            f"the drawer reaches for {wrong}, which is a worker session's verb and "
            "skips the pointer and stop-reason bookkeeping a supervisor stop owns"
        )
    # No reason is sent, so the daemon records `operator` — which is what keeps an
    # operator stop distinguishable in state from a rotation.
    assert "reason" not in _client_body(api, "stopAssistant"), (
        "the stop sends a reason of its own; the route's default is the true one and "
        "`rotation` has to stay distinguishable from an operator stop"
    )


def _client_body(api, name):
    """One exported client's body, without the prose above it."""
    start = api.index(f"export function {name}(")
    return api[start:api.index("\n}\n", start)]


def _words(path):
    """:func:`_operator_words` with the markup's line breaks collapsed.

    For the sentences long enough to be wrapped in the template — the confirmations
    are three lines of prose each, and where a line happens to break is not part of
    what the operator reads.
    """
    return " ".join(_operator_words(path).split())


def test_the_two_destructive_verbs_are_confirmed_in_the_apps_own_pattern():
    """Both end a context window, which is the one thing in this drawer that cannot
    be undone: the conversation is a live session's, so there is no draft of it and
    no transcript of the window that made it worth talking to.

    The pattern is the app's, not the browser's: ``confirm()`` is unstyleable, blocks
    the poll behind it and reads as a page in trouble. RunDetail's exit and
    Terminal's Ctrl-C already do this with a dialog, cancel first, the verb second.
    """
    text = _read(DRAWER)
    assert "confirm(" not in text, (
        "the drawer uses the browser's own confirm, which blocks the status poll "
        "behind a dialog nothing in this app can style"
    )
    for flag, verb in (("confirmingRestart", "restart"), ("confirmingStop", "stop")):
        dialog = re.search(
            rf'<v-dialog v-model="{flag}".*?</v-dialog>', text, re.S,
        )
        assert dialog, f"{verb} is not confirmed"
        assert f"{flag} = false" in dialog.group(0), (
            f"the {verb} confirmation cannot be cancelled"
        )
        assert f"@click=\"{verb}UberLmer\"" in dialog.group(0), (
            f"the {verb} confirmation confirms nothing"
        )
    # The action functions are only reachable through those dialogs: a control that
    # called them directly would be the confirmation removed for one entry point.
    for call in ("restartUberLmer", "stopUberLmer"):
        assert text.count(f'@click="{call}"') == (2 if call == 'stopUberLmer' else 1), (
            f"{call} is reachable from somewhere other than its confirmation"
        )


def test_the_confirmations_say_what_is_lost_and_claim_nothing_more():
    words = _words(DRAWER)
    for phrase in ('its current context is lost', 'Your conversation history remains visible',
                   'Standing orders survive', 'the handover note already on record',
                   'It stays stopped until you choose Start', 'pending work remain'):
        assert phrase in words


def test_one_lifecycle_call_at_a_time_and_the_wait_is_drawn():
    """The three verbs contradict each other, so the cluster goes out together.

    A stop landing behind a restart is the state worth refusing: nothing would be
    running while the drawer showed the incarnation the restart had just brought up.
    And each of these costs a container start, so the wait is seconds — long enough
    that a cluster which looked idle would be tapped again.
    """
    text = _read(DRAWER)
    assert re.search(
        r"const busy = computed\(\(\) =>\s*starting\.value \|\| restarting\.value "
        r"\|\| stopping\.value\)",
        text,
    ), "there is no single in-flight state for the cluster"
    assert "if (busy.value) return" in text, (
        "a second lifecycle call is sent while the first is still in flight"
    )
    # Every control in the cluster, and the start in the not-running card too.
    assert text.count(':disabled="busy"') == 3, (
        "part of the cluster stays live during a lifecycle call, so the verbs can "
        "still contradict each other"
    )
    for flag in ("restarting", "stopping", "starting"):
        assert f':loading="{flag}"' in text, f"the wait for the {flag} call draws nothing"
    words = _words(DRAWER)
    assert "restarting — the conversation below is still the incarnation being" in words, (
        "a restart in flight says nothing, so the old conversation reads as the new "
        "incarnation's"
    )
    assert "stopping — the daemon answers once the session is gone." in words, (
        "a stop in flight says nothing"
    )


def test_the_conversation_follows_the_incarnation_a_restart_created():
    """A rotation is a *different session*, and the chat has to end up on it without
    a reload — which needs no machinery here, and that is the point of this test.

    Each lifecycle route answers with the reconciled status, so the reply becomes the
    status, ``sessionId`` derives from it, and ``Chat.vue`` already restarts its
    transcript when the id it was given changes (it was written for a respawned run's
    session, which is the same shape). The history/active key stays stable through
    a live rotation, which must not remount the conversation or discard attachments.
    """
    text = _read(DRAWER)
    body = text[text.index("async function lifecycle("):text.index("function startUberLmer")]
    assert "status.value = reply" in body, (
        "a lifecycle reply is discarded, so the chat keeps reading the session the "
        "restart replaced until the next poll"
    )
    assert 'status.value.session_id || status.value.history_session' in text, (
        "the conversation's session no longer derives from the status the reply "
        "replaced, with retained history available when stopped"
    )
    assert "watch(() => props.sessionId, () => start())" in _read(CHAT), (
        "the shared chat no longer restarts when it is pointed at another session, "
        "so a rotation would leave the previous incarnation's transcript on screen"
    )
    assert "location.reload" not in text, "the drawer reloads the page to follow a rotation"
    chat = re.search(r"<Chat\b[^>]*>", text, re.S).group(0)
    assert ':key="composerActive ? \'active\' : \'history\'"' in chat, (
        "the conversation key must stay stable across live generations"
    )
    assert "running.value || rotating.value" in text
    assert "status.value?.lifecycle === 'rotating'" in text
    assert ':send-paused="rotating"' in chat


def test_a_refused_lifecycle_call_arrives_in_the_daemons_own_words():
    """Same contract as every other write in this UI: the daemon's sentence, and
    nothing thrown away to show it.

    Which matters more here than usual — a refused restart means the incarnation on
    screen is still the one you were talking to, so emptying the drawer would report
    a supervisor that is running as gone, and a half-typed message would go with it.
    """
    text = _read(DRAWER)
    body = text[text.index("async function lifecycle("):text.index("function startUberLmer")]
    assert "problem.value = exc.message" in body, (
        "a refused lifecycle call says nothing, or says something of this UI's own "
        "invention instead of what the daemon answered"
    )
    assert "status.value = null" not in body, (
        "a refused lifecycle call throws away the state the daemon last reported"
    )
    assert "exc.status === 409" not in body, (
        "a 409 is reported as a failure instead of being settled by a re-read; it "
        "means the daemon's view of what is running differs from this one's"
    )


def test_an_uber_lmer_that_was_stopped_is_legible_as_one():
    text = _read(DRAWER)
    assert "status?.lifecycle === 'stopped'" in text
    assert "status?.lifecycle === 'failed' ? 'Retry' : 'Start'" in text
    assert '{{ status.error }}' in text
    assert 'lifecycle(stopping, stopAssistant)' in text


def test_the_drawer_shows_the_digest_count_and_can_never_drain_the_spool():
    """T69's open question, decided here: the operator sees the *number*.

    Detection spools a digest and the supervising session takes it, over
    ``POST /api/assistant/pending`` — a destructive read, and deliberately a POST
    so a prefetch cannot eat it. There is no non-consuming read of the *notes*, and
    this decided not to add one: the notes are derived from facts the fleet view
    already shows first (the attention list), while a GET twin beside a drain route
    is a plausible wrong verb for the session itself — one that would silently
    never drain and re-read the same digests forever. The count on the status is
    the affordance the route's own docstring points a UI at, and it is enough for
    the state that matters: digests queued with nothing running to read them.
    """
    text = _read(DRAWER)
    assert "status.value?.pending" in text, (
        "the queue depth comes from somewhere other than the status"
    )
    words = _operator_words(DRAWER)
    assert "waiting for" in words, (
        "the operator is never told something is queued"
    )
    assert "queued === 1 ? 'digest' : 'digests'" in text, (
        "one queued digest reads as \"1 digests\", which looks like a bug in the "
        "number rather than a plural"
    )
    assert "nothing is reading them while none is running" in words, (
        "a queue with no reader looks the same as one being read, which is the "
        "state the start button exists for"
    )

    # Nothing anywhere in this UI may take the spool. The quoted form is what a
    # request is made with — api.js names the route in prose, which is the point.
    called = re.compile(r"""['"]api/assistant/pending""")
    for source in sorted((WEB / "src").rglob("*")):
        if source.is_file() and source.suffix in {".js", ".vue"}:
            assert not called.search(_read(source)), (
                f"{source.name} drains the supervisor's spool; an operator peeking "
                "would eat digests uber lmer has not read"
            )
    assert "no client here for `POST api/assistant/pending`" in _read(API_JS), (
        "the decision is no longer written down where the next person adding an "
        "assistant route will read it"
    )

    # The premise: the drain is a POST and there is still no read-only GET beside
    # it. If one is ever added, this decision is the thing to revisit.
    api = _read(API_PY)
    assert '@app.post("/api/assistant/pending"' in api, (
        "the drain is no longer a POST, so a browser prefetch can consume it"
    )
    assert '@app.get("/api/assistant/pending"' not in api, (
        "api.py grew a non-consuming read of the spool — the drawer showing only a "
        "count was argued from its absence, so re-argue it rather than leaving "
        "this test as the record of a premise that changed"
    )


# --- the standing orders ------------------------------------------------------

def test_the_standing_orders_button_opens_them_in_project_settings():
    """Issue 575 replaced the drawer's read-only panel with a toolbar button: the
    orders are read and edited in one place, Project settings, which the button
    opens with that panel expanded. Kept that simple because #448 (temporary
    standing orders) will rework this UI."""
    text = _read(DRAWER)
    toolbar = text[text.index("<v-toolbar"):text.index("</v-toolbar>")]
    button = re.search(r"<v-btn\b[^>]*aria-label=\"standing orders\"[^>]*/>", toolbar, re.S)
    assert button and "emit('settings', 'orders')" in button.group(0), (
        "the standing-orders button does not ask the shell for that settings section"
    )
    assert "fetchAssistantInstructions" not in text, (
        "the drawer still reads the orders itself, a second view of one document"
    )
    assert 'setAssistantInstructions' not in text, 'the chat drawer must remain a reader'

    app = _read(APP)
    assert '@settings="openProjectSettings(project.id, $event)"' in app
    assert ':focus="projectSettingsFocus"' in app
    opener = app[app.index("function openProjectSettings("):]
    opener = opener[:opener.index("\n}\n")]
    assert "if (mobile.value) uberOpen.value = false" in opener, (
        "on a phone the drawer stays over the settings it just opened"
    )

    editor = _read(WEB / 'src/components/ProjectSettings.vue')
    assert "props.focus?.section !== 'orders'" in editor
    assert "ordersOpen.value = 0" in editor
    assert 'setAssistantInstructions(id, instructions.value)' in editor
    client = _read(API_JS)
    assert "export function setAssistantInstructions(workspaceId, instructions)" in client
    assert "request(workspacePath('api/assistant/instructions', workspaceId), {" in client


def test_project_settings_says_how_standing_orders_are_usually_set_and_when_they_changed():
    """Issue 578 restores what the drawer's read-only panel said and the settings
    section did not.

    - The hint. The chat stays the usual write path because uber lmer confirms the
      wording there before it stores it, which this textarea skips.
    - An empty state, from the stored document rather than the draft.
    - The last-changed time. ``instructions_at`` is the daemon's own stamp, aged by
      the shell's clock.

    The hint names the permanent document only, so #448's temporary orders can sit
    beside it without contradicting it.
    """
    editor = WEB / "src" / "components" / "ProjectSettings.vue"
    words = _words(editor)
    assert ('Usually set by telling in chat — "from now on…", "always…" — '
            "and it confirms the wording before storing them.") in words
    assert "No standing instructions yet." in words
    assert "last changed" in words
    text = _read(editor)
    assert 'v-if="orders && !savedInstructions"' in text, (
        "the empty state follows the draft, or shows before the orders were read"
    )
    assert "ago(orders.instructions_at, now)" in text
    assert re.search(r"<ProjectSettings\b[^>]*:now=\"now\"", _read(APP), re.S), (
        "Project settings is not given the shell's clock, so last changed stops ageing"
    )


def test_the_settings_dialog_is_its_own_component_and_edits_only_launch_facts():
    """Global execution settings remain separate from the project instruction editor."""
    settings = WEB / "src" / "components" / "AssistantSettings.vue"
    text = _read(settings)
    words = _operator_words(settings)

    assert "fetchAssistantConfig" in text and "setAssistantConfig" in text, (
        "the dialog reads or writes something other than the daemon's config route"
    )
    assert "request('api/assistant/config')" in _read(API_JS), (
        "the read is no longer the GET the daemon serves"
    )
    assert "api/assistant/instructions" not in text, (
        "the settings dialog touches the standing orders, which are the chat's"
    )
    # The naming rule is the drawer's, and it travels with the feature: the
    # dialog is about the same recipient.
    assert "{{ assistantName }} settings" in text, "the dialog never says whose settings these are"
    assert "assistant" not in words.lower(), (
        f"the dialog calls the supervisor an assistant to the operator: {words!r}"
    )
    # The two ways a settings screen lies, said to the operator: scope and
    # shadowing. Both are prose, so both are checked as operator words.
    assert "next" in words and "incarnation" in words, (
        "nothing states that a change applies to the next incarnation — a save "
        "that visibly does nothing reads as broken"
    )
    assert "has no effect until that export is removed" in words, (
        "an env-shadowed save is reported as if it took effect"
    )
    # Fields prefill from the stored layer, never the effective value: saving an
    # export's text into config.json is the baking-in the API refuses to do.
    assert re.search(r"\.stored \|\| ''", text), (
        "the form prefills from something other than the stored layer"
    )
    assert "emit('settings')" in _read(DRAWER)
    assert '<AssistantSettings' in _read(WEB / 'src/components/PlatformSettings.vue')
    # The saved banner's one branch: when every changed key came back still
    # pinned by an export, the restart offer is withheld — an operator taking
    # it would pay a context window to run the export again.
    assert "savedShadowed" in text, (
        "nothing distinguishes an all-shadowed save from an effective one"
    )
    # Whitespace-normalized: the sentence wraps in the template, and the
    # phrase is the assertion — not its line breaks.
    assert "run the export, not what you saved" in " ".join(words.split()), (
        "the all-shadowed save does not tell the operator the restart is "
        "pointless"
    )


# --- what the fleet view pays -------------------------------------------------

def test_the_fleet_view_pays_nothing_for_the_drawer_until_it_is_opened():
    """The landing screen is the fleet, on every glance, from a phone.

    The chat costs two polls once it is mounted — the status and the transcript —
    and neither is any use to an operator looking at the fleet. So it is mounted on
    first open and left mounted afterwards, which is also what keeps a half-typed
    message alive across a close.

    One screen is exempt, and the operator asked for it (#286, from living with the
    band): the band exists to hold the app *beside* this conversation — its width is
    the smallest one at which the run view still fits with this drawer open — so a
    screen the band was left on is a screen the pane belongs on. Not a phone, where
    the band is inert and a full-screen chat would be the whole landing screen.

    And whatever opens it on the first frame has to mount it on the first frame too:
    the watcher fires on a *change*, so a drawer seeded open from anything but its own
    initial value comes up visible and empty, which is the report this answers.
    """
    app = _read(APP)
    assert "const uberOpen = ref(narrow.value && !mobile.value)" in app, (
        "the supervisor's drawer starts open on a screen with no band, so the fleet "
        "opens behind a chat — or a phone does"
    )
    assert re.search(r"<AssistantChat v-if=\"uberSeen\"", app), (
        "the chat is mounted before anyone asked for it, so the landing screen "
        "polls the supervisor"
    )
    assert "const uberSeen = ref(uberOpen.value)" in app, (
        "the chat is mounted from something other than the drawer's own first frame: "
        "either the fleet pays for it unasked, or a drawer that starts open comes up "
        "empty"
    )
    assert "uberSeen.value = true" in app, "the drawer can never be mounted at all"


def test_the_drawer_remembers_nothing_in_a_browser_store():
    """Deliberate, and the reasoning is the landing screen again: a remembered-open
    drawer opens a phone onto a full-screen chat instead of the fleet, and makes
    every glance fetch the supervisor's status and transcript.

    The one key the shell has grown since is not this rule bending: ``.narrow`` is
    whether the whole app is held in a band in the middle of a wide screen (#286), a
    property of the screen being read from like the colour scheme beside it. It does
    decide the drawer's *first frame* now — a screen the band was left on is one the
    pane belongs on — but nothing writes the drawer's own open state anywhere, so
    closing it is still forgotten by the next load and a screen with no band still
    lands on the fleet, which is what a glance would otherwise pay for.

    The width is still the theme's, not a preference.

    The third key is the same shape of decision and not this rule bending either:
    ``lmer.fleet.view`` is whether this operator reads the fleet as the list or as
    the constellation map (#141 fleet map) — again a property of how somebody
    reads a fleet rather than a fact about any run, and again not the drawer. It
    cannot cost a glance what a remembered-open drawer would: both views render
    from the fleet payload the screen already fetches, the map opens no
    connection of its own, and ``list`` is the first mode and therefore the
    default, so an operator who never touches the switch lands exactly where they
    did before.

    The next three are the project memory of #627 — which projects the sidebar has
    folded, the project last open, and the project this browser opens on load.
    None of them is the drawer either: they choose which project the page lands
    on, and the landing screen is still the fleet with the drawer closed.

    What stays true is the actual rule: nothing writes the drawer's own open
    state, so closing it is still forgotten by the next load.
    """
    assert "localStorage" not in _read(DRAWER), (
        "the drawer stores something; if it is a preference it belongs in "
        "preferences.js and in ALLOWED_STORAGE_KEYS with a reason"
    )
    keys = re.findall(r"localStorage\.\w+\(\s*([A-Za-z0-9_.]+)", _read(APP))
    assert set(keys) == {
        "THEME_STORAGE_KEY", "NARROW_STORAGE_KEY", "FLEET_VIEW_STORAGE_KEY",
        "COLLAPSED_PROJECTS_STORAGE_KEY", "LAST_PROJECT_STORAGE_KEY",
        "DEFAULT_PROJECT_STORAGE_KEY",
    }, (
        f"the shell now stores {sorted(set(keys))}; the drawer's open state is "
        "deliberately not remembered, and a further key here is a decision"
    )


def test_the_drawer_takes_its_colour_from_the_theme():
    """A hex here is a colour the theme cannot change (main.js owns the palette).

    The toolbar is the place most likely to reach for a literal. Since issue 578 it
    is a neutral theme surface, `uber-toolbar`, rather than the project colour. What
    sets the drawer apart from the run navigator is its edge and its "uber lmer"
    name, both pinned above.
    """
    text = _read(DRAWER)
    hexes = re.findall(r"#[0-9a-fA-F]{3,8}\b", text)
    assert not hexes, f"the drawer hardcodes {hexes} instead of naming a theme colour"
    toolbar = re.search(r"<v-toolbar\b[^>]*>", text, re.S).group(0)
    assert 'color="uber-toolbar"' in toolbar, "the toolbar does not take the theme's neutral surface"
    assert "projectColor" not in text and ":style" not in toolbar, (
        "the toolbar is painted with the project colour again"
    )
    theme = _read(WEB / "src" / "main.js")
    assert len(re.findall(r"'uber-toolbar': '#[0-9a-f]{6}'", theme)) == 2, (
        "uber-toolbar is not defined in both colour schemes"
    )


def test_thread_settings_form_round_trips_lists_and_clears_them():
    import json
    import subprocess
    from tests.test_platform_web_bundle import node_binary, require_node_toolchain

    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")
    script = r"""
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRenderer } from 'vue'
import { parse, compileScript } from '@vue/compiler-sfc'
const path = 'src/components/AssistantSettings.vue'
const { descriptor } = parse(readFileSync(path, 'utf8'), { filename: path })
assert.match(descriptor.template.content, /<v-textarea[\s\S]*?v-model="fields.threads"/)
const compiled = compileScript(descriptor, { id: 'threads-probe' })
globalThis.savedCalls = []
const source = compiled.content
  .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
  .replace(/from ['"]@mdi\/js['"]/g, `from '${import.meta.resolve('@mdi/js')}'`)
  .replace(/from ['"]\.\.\/operatorChat.js['"]/g, `from '${import.meta.resolve('./src/operatorChat.js')}'`)
  .replace(/from ['"]\.\.\/ratchet.js['"]/g, `from '${import.meta.resolve('./src/ratchet.js')}'`)
  .replace(/from ['"]\.\.\/launchSettings.js['"]/g, `from '${import.meta.resolve('./src/launchSettings.js')}'`)
  .replace(/import .* from ['"]\.\.\/api.js['"]/, `
    const fetchAssistantConfig = async () => ({ settings: {} });
    const setAssistantConfig = async (body) => {
      globalThis.savedCalls.push(body);
      return { settings: { threads: { stored: body.threads, value: body.threads } }, changed: ['threads'] };
    };`)
const component = (await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`)).default
component.render = () => null
const renderer = createRenderer({ createComment: () => ({}), insert() {}, remove() {}, parentNode() {}, nextSibling() {} })
const app = renderer.createApp(component, { running: true, runningSettings: { threads: ['https://matrix.to/#/!room:example.org/$root'] } })
const instance = app.mount({})
const state = instance.$.setupState
const links = ['https://team.slack.com/archives/C1/p1700000000000001', 'https://matrix.to/#/!room:example.org/$root']
const config = { settings: { threads: { stored: links, value: links } } }
state.reply = config
state.prefill(config)
assert.equal(state.fields.threads, links.join('\n'))
assert.deepEqual(state.changes(), {})
assert.equal(state.runningDiffers('threads'), true)
state.fields.threads = links[1]
await state.save()
assert.deepEqual(globalThis.savedCalls[0], { threads: [links[1]] })
assert.equal(state.runningDiffers('threads'), false)
state.fields.threads = ''
await state.save()
assert.deepEqual(globalThis.savedCalls[1], { threads: [] })
app.unmount()
console.log(JSON.stringify({ passed: true }))
"""
    result = subprocess.run([node, "--input-type=module", "-e", script], cwd=WEB,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"passed": True}


# --- the daemon's reminders on the settings screen (issue #446) ----------------

def test_the_settings_dialog_carries_the_three_reminder_settings():
    """#446 item 4 asks for the context threshold "next to checkin and nudge".
    Neither was on this screen, so all four arrive together — and they are the
    keys the write route already accepts, spelled as the config fields they are
    rather than as the groups they are served under."""
    text = _read(WEB / "src" / "components" / "AssistantSettings.vue")

    for key in ("checkin_window_seconds", "nudge_after_seconds",
                "nudge_pending_threshold", "rotation_context_threshold"):
        assert key in text, f"{key} is not written by the settings screen"


def test_the_threshold_is_a_percent_slider_from_one_to_a_hundred():
    """The operator's own shape for it: a slider over the usable range, with the
    off state beside it rather than at the bottom of the scale — a slider whose
    lowest position means "never" reads as "always"."""
    text = _read(WEB / "src" / "components" / "AssistantSettings.vue")
    slider = re.search(r"<v-slider\b.*?</v-slider>", text, re.S)
    assert slider, "the threshold is not a slider"
    assert ':min="1"' in slider.group(0)
    assert ':max="100"' in slider.group(0)
    assert "<v-switch" in text, "there is no explicit off state beside the slider"
    assert "fields.rotationOn ? Number(fields.rotationPercent) : 0" in text, (
        "the off state does not write the 0 the daemon reads as off"
    )


def test_the_reminder_block_says_it_needs_no_restart():
    """Everything else on this screen applies to the next incarnation. These four
    apply on the next detection tick, and a screen that let the opening sentence
    speak for them would have an operator restarting a session for nothing."""
    words = " ".join(
        _operator_words(WEB / "src" / "components" / "AssistantSettings.vue").split()
    )
    assert "apply on its next check" in words
    assert "no restart" in words


def test_the_off_state_says_what_off_means_and_what_it_depends_on():
    words = " ".join(
        _operator_words(WEB / "src" / "components" / "AssistantSettings.vue").split()
    )
    assert "never told its context window is filling" in words
    assert "publishes the reading on its status line" in words, (
        "nothing tells the operator the reminder needs a harness that reports one"
    )
    assert "nothing here ends a session" in words, (
        "a settings screen that sets a rotation threshold has to say it does not "
        "rotate anything"
    )


def test_the_reminder_fields_prefill_from_the_stored_layer():
    """The launch fields' rule, and it matters more here: prefilling an export's
    value into a widget that writes config.json bakes the export into the file."""
    text = _read(WEB / "src" / "components" / "AssistantSettings.vue")
    assert "config[spec.group]?.[spec.key]?.stored" in text
    assert "config.rotation?.context_threshold?.stored" in text
    assert "an exported environment variable pins this to" in text


# --- the reminder settings, driven (issue #446) -------------------------------
#
# Every guard above reads source, and source cannot show what a save *sends*. The
# round trip is where a settings screen lies: a threshold the operator set that
# arrives as nothing, an untouched field stamped with its own value, or an
# export's number written into the file the first time anything is saved. So this
# drives the component — prefill it from a daemon reply, move the widgets, call
# save(), and read the body off the wire.

_SAVE_PROBE = """
import { readFileSync } from 'node:fs'
import { createRenderer, ref, h, nextTick } from 'vue'
import { parse, compileScript } from '@vue/compiler-sfc'

const path = 'src/components/AssistantSettings.vue'
const { descriptor } = parse(readFileSync(path, 'utf8'), { filename: path })
// Not `inlineTemplate`: that closes the setup bindings over the render function
// and `save()` becomes unreachable. The template is not what is under test here.
const compiled = compileScript(descriptor, { id: 'probe' })

const CONFIG = %s

const sent = []
globalThis.fetch = async (url, options = {}) => {
  const body = options.body ? JSON.parse(options.body) : null
  sent.push({ url: String(url), method: options.method || 'GET', body })
  // A write answers with `changed`, as the daemon's own route does: the banner
  // branches on it, so a stub that omitted it would make every flag read false.
  return {
    ok: true,
    status: 200,
    json: async () => (body ? { ...CONFIG, changed: Object.keys(body).sort() } : CONFIG),
  }
}

const source = compiled.content
  .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
  .replace(/from ['"]@mdi\\/js['"]/g, `from '${import.meta.resolve('@mdi/js')}'`)
  .replace(/from ['"]\\.\\.\\/(api|format|launchSettings|operatorChat|ratchet)\\.js['"]/g,
    (_m, name) => `from '${import.meta.resolve(`./src/${name}.js`)}'`)
const component = (await import(
  `data:text/javascript;base64,${Buffer.from(source, 'utf8').toString('base64')}`
)).default
component.render = () => null

const noop = () => {}
const nodeOps = {
  createElement: (t) => ({ tag: t }), createText: (t) => ({ text: t }),
  createComment: (t) => ({ comment: t }), setText: noop, setElementText: noop,
  insert: noop, remove: noop, parentNode: () => null, nextSibling: () => null,
  patchProp: noop, querySelector: () => null, setScopeId: noop,
  cloneNode: (n) => n,
}
const { createApp } = createRenderer(nodeOps)

const child = ref(null)
const open = ref(false)
const app = createApp({
  render: () => h(component, { modelValue: open.value, ref: child }),
})
app.config.warnHandler = noop
app.mount(nodeOps.createElement('root'))
// Opened the way the drawer opens it, which is what triggers the read.
open.value = true
await nextTick()
await new Promise((resolve) => setTimeout(resolve, 20))

// `<script setup>` bindings are deliberately private on the public proxy, so the
// internal instance is how a probe reaches them. Test-only reach-in.
const state = child.value.$.setupState
const prefilled = {
  checkinWindow: state.fields.checkinWindow,
  nudgeAfter: state.fields.nudgeAfter,
  nudgePending: state.fields.nudgePending,
  rotationOn: state.fields.rotationOn,
  rotationPercent: state.fields.rotationPercent,
}

%s

sent.length = 0
await state.save()
// `setupState` unwraps refs, so these are the booleans the template branches on.
const flags = {
  savedTick: state.savedTick === true,
  savedTickShadowed: state.savedTickShadowed === true,
  savedShadowed: state.savedShadowed === true,
  savedLaunch: state.savedLaunch === true,
}
console.log(JSON.stringify({ sent, prefilled, flags }))
"""


def _settings_reply(**overrides):
    """A daemon config reply with every group the dialog reads."""
    def setting(value, stored=None, source="default"):
        return {"value": value, "source": source, "stored": stored}

    reply = {
        "settings": {
            key: setting(None) for key in
            ("model", "harness", "preset", "agents", "threads")
        },
        "chat": {"keep_last": setting(0, 0)},
        "checkin": {"window_seconds": setting(3600)},
        "nudge": {
            "after_seconds": setting(180),
            "pending_threshold": setting(1),
        },
        "rotation": {"context_threshold": setting(0)},
        "changed": [],
    }
    reply.update(overrides)
    return reply


def _drive(script, reply=None):
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")
    probe = _SAVE_PROBE % (json.dumps(reply or _settings_reply()), script)
    result = subprocess.run(
        [node, "--input-type=module", "-e", probe],
        capture_output=True, text=True, timeout=180, cwd=str(WEB),
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout)


def test_turning_the_reminder_on_sends_the_percent_and_nothing_else():
    """The acceptance the operator will check by hand: move the slider, save, and
    the daemon is asked for exactly that threshold. A whole-form write would stamp
    every other key with its own value and make two browsers overwrite each other."""
    driven = _drive(
        "state.fields.rotationOn = true\n"
        "state.fields.rotationPercent = 70\n"
        "await nextTick()"
    )
    writes = [call for call in driven["sent"] if call["method"] == "POST"]
    assert writes, f"the save sent nothing: {driven['sent']}"
    assert writes[0]["body"] == {"rotation_context_threshold": 70}


def test_turning_it_off_sends_the_zero_the_daemon_reads_as_off():
    reply = _settings_reply(
        rotation={"context_threshold": {
            "value": 70, "source": "config.json", "stored": 70,
        }},
    )
    driven = _drive("state.fields.rotationOn = false\nawait nextTick()", reply)
    writes = [call for call in driven["sent"] if call["method"] == "POST"]
    assert writes[0]["body"] == {"rotation_context_threshold": 0}
    assert driven["prefilled"]["rotationOn"] is True, (
        "a stored threshold opened the dialog switched off"
    )
    assert driven["prefilled"]["rotationPercent"] == 70


def test_an_untouched_form_writes_nothing_at_all():
    """Every field prefills from the stored layer, so a save that changed nothing
    has nothing to send — including the three reminder fields, which are unset on
    a host nobody has configured and must not be stamped with the defaults they
    are showing."""
    driven = _drive("await nextTick()")
    assert driven["prefilled"]["checkinWindow"] == ""
    assert driven["prefilled"]["nudgeAfter"] == ""
    assert [call for call in driven["sent"] if call["method"] == "POST"] == []


def test_an_exported_value_is_not_baked_into_the_file_by_a_save():
    """The stored-layer rule, in the case that costs: an export pins the interval
    at 900, the dialog shows 900 as what is in effect, and a save touching
    something else must not persist 900 into config.json — where it would outlive
    the export."""
    reply = _settings_reply(
        nudge={
            "after_seconds": {"value": 900, "source": "env", "stored": None},
            "pending_threshold": {"value": 1, "source": "default", "stored": None},
        },
    )
    driven = _drive(
        "state.fields.rotationOn = true\n"
        "state.fields.rotationPercent = 80\n"
        "await nextTick()",
        reply,
    )
    writes = [call for call in driven["sent"] if call["method"] == "POST"]
    assert driven["prefilled"]["nudgeAfter"] == "", (
        "the export's value was prefilled into a field that writes the file"
    )
    assert writes[0]["body"] == {"rotation_context_threshold": 80}


def test_a_reminder_field_the_operator_typed_is_sent_as_its_config_key():
    driven = _drive(
        "state.fields.nudgeAfter = '600'\n"
        "state.fields.checkinWindow = '0'\n"
        "await nextTick()"
    )
    writes = [call for call in driven["sent"] if call["method"] == "POST"]
    assert writes[0]["body"] == {
        "nudge_after_seconds": 600, "checkin_window_seconds": 0,
    }


def test_a_shadowed_reminder_save_does_not_promise_the_next_tick():
    """Round 1: with `LMER_PLATFORM_ROTATION_CONTEXT_THRESHOLD=85` exported,
    switching the reminder off persists `0` correctly — and the success banner
    said it applies on the daemon's next check, which still reads 85. The field
    caption already exposed the shadow; the banner contradicted it."""
    reply = _settings_reply(
        rotation={"context_threshold": {
            "value": 85, "source": "env", "stored": 70,
        }},
    )
    driven = _drive("state.fields.rotationOn = false\nawait nextTick()", reply)
    writes = [call for call in driven["sent"] if call["method"] == "POST"]

    assert writes[0]["body"] == {"rotation_context_threshold": 0}, (
        "the stored-only write is unchanged"
    )
    assert driven["flags"]["savedTickShadowed"] is True
    assert driven["flags"]["savedTick"] is False, (
        "the banner promised a next-tick effect the export will not allow"
    )


def test_an_effective_reminder_save_still_says_it_applies_next_tick():
    driven = _drive(
        "state.fields.nudgeAfter = '600'\nawait nextTick()"
    )
    assert driven["flags"]["savedTick"] is True
    assert driven["flags"]["savedTickShadowed"] is False


def test_a_half_shadowed_save_says_both_things():
    """One save can change an effective field and a pinned one; the operator
    needs the true half as well as the warning."""
    reply = _settings_reply(
        nudge={
            "after_seconds": {"value": 900, "source": "env", "stored": None},
            "pending_threshold": {"value": 1, "source": "default", "stored": None},
        },
    )
    driven = _drive(
        "state.fields.nudgeAfter = '600'\n"
        "state.fields.nudgePending = '3'\n"
        "await nextTick()",
        reply,
    )
    assert driven["flags"]["savedTick"] is True
    assert driven["flags"]["savedTickShadowed"] is True


def test_clearing_a_reminder_field_clears_the_stored_key():
    reply = _settings_reply(
        nudge={
            "after_seconds": {
                "value": 600, "source": "config.json", "stored": 600,
            },
            "pending_threshold": {
                "value": 1, "source": "default", "stored": None,
            },
        },
    )
    driven = _drive("state.fields.nudgeAfter = ''\nawait nextTick()", reply)
    writes = [call for call in driven["sent"] if call["method"] == "POST"]
    assert writes[0]["body"] == {"nudge_after_seconds": None}
