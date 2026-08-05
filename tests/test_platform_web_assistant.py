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

import re
from pathlib import Path

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
    for match in re.finditer(r"<v-btn\b[^>]*>", app, re.S):
        if "uberOpen" in match.group(0):
            button = match.group(0)
            break
    assert button, "nothing in the shell opens the supervisor's drawer"
    assert "uberOpen = !uberOpen" in button, "the bar button cannot close it again"
    assert "aria-label" in button, "an icon-only button needs a name"
    assert "mdiRobot" in button, (
        "the toggle draws no icon, or one pasted as path data rather than taken "
        "from @mdi/js"
    )
    assert "mdiRobot" in _read(DRAWER), (
        "the bar button and the drawer's header no longer share an icon, so "
        "nothing connects the tap to the panel it opened"
    )


# --- the words ----------------------------------------------------------------

def test_the_supervisor_is_called_uber_lmer_and_never_an_assistant():
    """Two visibly different names are what stand between an operator and the
    wrong recipient (operator request, 2026-07-27).

    "assistant" stays the spelling of the module, the taskdef and every API field —
    this is a label decision — so the check is on the strings an operator reads and
    on nothing else. Which is also why "uber lmer" has to be *present*: a drawer
    that merely avoided the forbidden word would be a nameless panel, and a
    nameless panel is one an operator has to guess the recipient of.
    """
    words = _operator_words(DRAWER)
    assert "uber lmer" in words, "the drawer never says whose chat it is"
    assert "assistant" not in words.lower(), (
        f"the drawer calls the supervisor an assistant to the operator: {words!r}"
    )
    assert "lmer working in a repository" in words, (
        "nothing tells the operator that this is not one of the runs, which is the "
        "distinction the two names exist to carry"
    )
    # The bar button is the other operator-visible surface, and it is one word.
    shell = _operator_words(APP)
    assert "uber lmer" in shell, "the app bar's toggle is unnamed or misnamed"
    assert "assistant" not in shell.lower(), (
        "the shell calls the supervisor an assistant to the operator"
    )


def test_the_composer_says_who_is_at_the_other_end():
    """The shared chat's own label is written for a worker session ("this session").

    Which is correct there and not enough here: the last line read before typing
    has to name the recipient, because that is the moment the mistake happens. So
    two things say it — the line above the composer, and the composer's own label,
    which Chat.vue takes as a prop for exactly this reason. Both, rather than one:
    the sentence is above a conversation that scrolls, and the label is inside the
    field a thumb is already in.
    """
    words = _operator_words(DRAWER)
    assert "You are talking to uber lmer." in words, (
        "nothing above the composer names the recipient"
    )

    label = re.search(r"<Chat[^>]*\scomposer-label=\"([^\"]*)\"", _read(DRAWER), re.S)
    assert label, (
        "the drawer leaves the shared composer on its default label, which says "
        '"this session" in a panel that is deliberately not one of the runs'
    )
    assert "uber lmer" in label.group(1), (
        f"the composer is labelled {label.group(1)!r}, which names no recipient — "
        "and the recipient is the whole of what this drawer has to get right"
    )
    # The default it overrides is the run's, and still exists: this is a wording
    # decision in one component, not a second composer.
    assert "say something to this session" in _read(CHAT), (
        "the shared chat no longer has a label of its own to override"
    )


# --- the conversation is the shared one ---------------------------------------

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
    """The state an operator most needs the drawer in is the one it is useless in.

    The daemon supervises the supervisor (T63), so a manual start is a legitimate
    verb rather than a workaround — and the drawer says both halves: one is coming
    on its own, and here is the button if it has not.
    """
    text = _read(DRAWER)
    words = _operator_words(DRAWER)
    assert "No uber lmer is running." in words, (
        "a stopped supervisor is not stated, so an empty drawer reads as a broken one"
    )
    assert "start uber lmer" in words, "there is no way to start one"
    assert "The daemon starts one and keeps it up" in words, (
        "nothing says the daemon is already trying, so the button reads as the "
        "only thing that can start one"
    )
    assert "startAssistant" in text and "startAssistant" in _read(API_JS), (
        "the start goes somewhere other than the daemon's own route"
    )
    assert "request('api/assistant/start', { method: 'POST' })" in _read(API_JS), (
        "the start route is no longer the POST the daemon serves"
    )
    # A 409 is "one is already running" — two taps on a slow connection — and must
    # not be reported as a failure to start.
    assert "exc.status === 409" in text, (
        "a second start is reported as an error instead of being re-read"
    )
    assert ":loading=\"starting\"" in text and "starting" in words, (
        "the wait between the tap and the session being up draws nothing"
    )


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


def test_the_supervisors_generation_and_age_are_shown_from_the_status():
    """Which incarnation you are talking to is not cosmetic: a rotation (§8.3) is
    a fresh context window, so a note it "already has" may be a note the one
    before it had."""
    text = _read(DRAWER)
    assert "status.generation" in text, "nothing says which incarnation this is"
    assert "ago(status.started_at, now)" in text, (
        "how long this one has been up is not shown, or is computed here rather "
        "than by the one helper that formats a relative time"
    )


# --- the lifecycle controls (T126) --------------------------------------------

def test_the_header_offers_a_restart_and_hides_the_verb_the_daemon_would_refuse():
    """The operator asked for it in those words — "a way to restart the uber lmer
    process from the ux" — after a wedged incarnation could only be cleared by
    restarting the daemon on the host, which takes the fleet view down with it.

    Restart is the primary control because it is the only one of the three that
    always leaves something running: the route replaces a live incarnation and starts
    one when nothing is up, in a single call. Stop leaves nothing, so it sits in the
    overflow; start is offered only while nothing is running, because against a live
    one the route answers 409 to protect the incumbent's context window, and an
    affordance whose only outcome is a refusal is worse than none.
    """
    text = _read(DRAWER)
    header = text[text.index("<v-toolbar"):text.index("</v-toolbar>")]

    restart = re.search(r"<v-btn\b[^>]*mdiRestart.*?</v-btn>", header, re.S)
    assert restart, "the header has no restart control"
    assert 'v-if="running"' in restart.group(0), (
        "restart is offered against an uber lmer that is not running, where what it "
        "means is a start with a different name"
    )
    assert "confirmingRestart = true" in restart.group(0), (
        "restart fires straight at the daemon instead of asking first"
    )

    assert "<v-menu" in header, (
        "stop sits beside restart rather than behind an overflow, one mis-aimed tap "
        "from ending the supervisor when the operator meant to replace it"
    )
    stop_item = re.search(r"<v-list-item\b[^>]*mdiStopCircleOutline[^>]*>", header, re.S)
    assert stop_item and 'v-if="running"' in stop_item.group(0), (
        "the overflow offers a stop with nothing running, which stops nothing"
    )
    assert "confirmingStop = true" in stop_item.group(0), (
        "stop fires straight at the daemon instead of asking first"
    )
    start_item = re.search(
        r"<v-list-item\b[^>]*mdiPlayCircleOutline[^>]*>", header, re.S,
    )
    assert start_item and 'v-else' in start_item.group(0), (
        "the overflow offers a start against a live uber lmer, whose only possible "
        "answer is the 409 the route hands out to keep the incumbent's window"
    )

    # Nothing is drawn before the daemon has answered: a control cluster for a state
    # nobody has read yet is a guess at which verbs apply.
    assert 'v-if="status || problem"' in header, (
        "the cluster is drawn before the first status arrives"
    )
    words = _operator_words(DRAWER)
    assert "stop uber lmer" in words and "start uber lmer" in words, (
        "the overflow's entries are unnamed or misnamed"
    )


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
        ("rotateAssistant", "request('api/assistant/rotate', { method: 'POST' })"),
        ("stopAssistant", "request('api/assistant/stop', { method: 'POST' })"),
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
        assert text.count(f'@click="{call}"') == 1, (
            f"{call} is reachable from somewhere other than its confirmation"
        )


def test_the_confirmations_say_what_is_lost_and_claim_nothing_more():
    """The copy is the whole safety feature here, so it is checked as one.

    What a rotation costs is the conversation window and *only* that: standing orders
    are never consumed and a rotation carries them forward, and the handover note
    travels untouched when none is sent (``assistant.start``/``stop`` edit state with
    ``dataclasses.replace``, and ``_handoff_update`` keeps the recorded note when the
    field is omitted). So the confirm says both halves — and says the note is the one
    on record rather than a fresh summary, because nothing in this UI asks the
    outgoing incarnation to write one.

    A stop must not promise it stays stopped. The daemon's supervisor respawns
    anything that is not running, whatever ended it (``Supervisor.supervise_once``
    reads liveness and nothing else), so "nothing restarts it until you say so" is
    true only of a daemon that is not supervising or has given up — which is exactly
    what the start button is for, and what the copy says instead.
    """
    # Collapsed, because a sentence this long is wrapped in the markup and what is
    # being checked is the sentence rather than where it happens to break.
    words = _words(DRAWER)
    for phrase in (
        "Restart uber lmer?",
        "the conversation window is lost",
        "Standing orders survive",
        "the handover note already on record",
    ):
        assert phrase in words, (
            f"the restart confirmation does not say {phrase!r}, so the operator is "
            "asked to accept a loss nobody named"
        )
    for phrase in (
        "Stop uber lmer?",
        "nothing in this drawer brings one back",
        "A daemon that is supervising puts a fresh one up on its own",
        "none comes back until you start one here",
    ):
        assert phrase in words, (
            f"the stop confirmation does not say {phrase!r}"
        )
    # The claim the code cannot support, in the shapes it would be written in.
    for lie in (
        "nothing restarts it",
        "nothing will restart it",
        "stays stopped",
        "stays down until you start",
    ):
        assert lie not in words.lower(), (
            f"the drawer promises {lie!r}, which the supervisor contradicts: it "
            "respawns anything that is not running, whatever ended it"
        )


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
    session, which is the same shape). Anything else — a ``:key`` to force a remount,
    a reload, a second poll — would be a second mechanism for a property one already
    covers.
    """
    text = _read(DRAWER)
    body = text[text.index("async function lifecycle("):text.index("function startUberLmer")]
    assert "status.value = reply" in body, (
        "a lifecycle reply is discarded, so the chat keeps reading the session the "
        "restart replaced until the next poll"
    )
    assert 'const sessionId = computed(() => (running.value ? status.value.session_id' in text, (
        "the conversation's session no longer derives from the status the reply "
        "replaced"
    )
    assert "watch(() => props.sessionId, () => start())" in _read(CHAT), (
        "the shared chat no longer restarts when it is pointed at another session, "
        "so a rotation would leave the previous incarnation's transcript on screen"
    )
    assert "location.reload" not in text, "the drawer reloads the page to follow a rotation"
    assert not re.search(r"<Chat[^>]*:key=", text, re.S), (
        "the conversation is remounted by key; the session-id watch is what follows "
        "an incarnation, and two mechanisms for it is one to get wrong"
    )


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
    assert "exc.status === 409" in body and "await load()" in body, (
        "a 409 is reported as a failure instead of being settled by a re-read; it "
        "means the daemon's view of what is running differs from this one's"
    )


def test_an_uber_lmer_that_was_stopped_is_legible_as_one():
    """The zombie problem is a supervisor that is *gone* and looks merely quiet, so
    the drawer has to say which of the two it is.

    It can, from the status alone and without a new field: a stop clears the pointer
    to the session and a crash leaves it, which is what ``stale`` reports. So the
    card names the generation either way and says which of the two happened — and
    the stop reply is itself a status, so the drawer lands on that card the moment
    the daemon answers rather than at the next poll.
    """
    text = _read(DRAWER)
    words = _words(DRAWER)
    assert "No uber lmer is running." in words, "a stopped supervisor is not stated"
    assert "a stop looks like from here rather than a crash" in words, (
        "a stopped uber lmer and one that crashed read identically, which is the "
        "state this control cluster exists to get out of"
    )
    # Optional chaining on both branches, and it is not decoration: `load()`'s
    # catch leaves `status` null while setting `problem`, and the spinner above
    # is gated on both being absent — so a first poll that fails renders this
    # card with nothing behind it. A bare deref throws on every re-render and
    # leaves the drawer blank, which is the moment it is opened to find out
    # what is wrong.
    assert re.search(r'<template v-else-if="status\?\.generation">', text), (
        "the clean-stop line is not the other branch of the stale check, so it "
        "either claims a crash was a stop or fires on a host that never had one"
    )
    assert not re.search(r'v-(if|else-if)="status\.(stale|generation)"', text), (
        "a branch of this card dereferences `status` without optional chaining, "
        "so it throws whenever the drawer is opened while the daemon is not "
        "answering"
    )
    assert "stopUberLmer" in text and "lifecycle(stopping, stopAssistant)" in text, (
        "the stop the card describes is not the one this drawer performs"
    )


# --- the digest spool ---------------------------------------------------------

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
    assert "waiting for uber lmer" in words, (
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

def test_the_drawer_shows_the_standing_orders_and_offers_no_way_to_edit_them():
    """T87. The operator asked for the chat to be the write path — "not some ux
    config thing" — so this panel is a window onto the document and nothing else.

    Which is a real temptation to guard: it is a text document with a POST route
    beside its GET, and a textarea plus a save button is the obvious next commit.
    What that would cost is the confirm-the-wording step — uber lmer reads the rule
    back before storing it, in the operator's own words — and it would make two
    writers of one document with no merge between them. So the absence of a write
    path here is the feature, and it is checked as one.
    """
    text = _read(DRAWER)
    words = _operator_words(DRAWER)

    assert "fetchAssistantInstructions" in text and (
        "fetchAssistantInstructions" in _read(API_JS)
    ), "the standing orders are read from somewhere other than the daemon's route"
    assert "request('api/assistant/instructions')" in _read(API_JS), (
        "the read route is no longer the GET the daemon serves"
    )
    assert "standing orders" in words, "the panel is unnamed, or named something else"
    assert "orders.instructions" in text, (
        "the document shown comes from somewhere other than the daemon's reply"
    )
    assert "nothing to edit here on purpose" in words, (
        "the panel does not say why it is read-only, so its absence of a form "
        "reads as an unfinished feature"
    )
    assert "uber lmer confirms the wording" in words, (
        "nothing tells the operator what happens when they state a rule in the chat"
    )

    # No write path, from either end: no field to type in, and no client that could
    # send one. `POST api/assistant/instructions` exists and is the assistant's.
    for affordance in ("v-textarea", "v-text-field", "v-form", "v-switch"):
        assert affordance not in text, (
            f"the drawer grew a {affordance}: editing the standing orders here "
            "bypasses the confirm-the-wording step that makes them the operator's"
        )
    writer = re.compile(r"""['"]api/assistant/instructions['"][^)]*method""")
    for source in sorted((WEB / "src").rglob("*")):
        if source.is_file() and source.suffix in {".js", ".vue"}:
            assert not writer.search(_read(source)), (
                f"{source.name} writes the standing orders; the chat is the write "
                "path by the operator's own choice"
            )
    api = _read(API_PY)
    assert '@app.post("/api/assistant/instructions"' in api, (
        "the write route is gone, so the chat has nothing to store a rule through"
    )


def test_the_settings_dialog_is_its_own_component_and_edits_only_launch_facts():
    """Issue #234. The drawer's no-affordance guard above is a one-line ban, and
    keeping it one is why the settings dialog lives in its own file: launch facts
    (model/harness/preset/agents) are a config file's to edit and a form's to
    write, while the standing orders' write path stays the chat. What this pins
    is the boundary — the dialog writes the config route and could not reach the
    instructions route even by mistake (the rglob in the standing-orders test
    covers every source file, this one included), and the drawer file itself
    stays free of input widgets.
    """
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
    assert "uber lmer" in words, "the dialog never says whose settings these are"
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
    # The restart the saved banner offers is the drawer's own confirmed one —
    # ending a context window is one decision however it is reached.
    drawer = _read(DRAWER)
    assert '@restart="confirmingRestart = true"' in drawer, (
        "the dialog's restart bypasses the drawer's confirmation"
    )
    assert "AssistantSettings" in drawer, (
        "the dialog is not reachable from the drawer at all"
    )
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


def test_the_standing_orders_are_fetched_on_demand_and_not_polled():
    """The drawer's cost is one status poll every ten seconds, and standing orders
    change a few times a month — so they are read when the panel is expanded.

    Re-read on every expand rather than cached forever, because uber lmer rewrites
    the document from the chat: a panel showing the rules from the last time it was
    opened is a read-only view of the wrong document, which is worse than none.
    """
    text = _read(DRAWER)
    assert "@update:model-value=\"onOrdersToggle\"" in text, (
        "nothing loads the document when the panel is opened"
    )
    assert re.search(
        r"function onOrdersToggle\(value\) \{\s*\n"
        r"\s*if \(value !== undefined\) loadOrders\(\)",
        text,
    ), "collapsing the panel refetches, or expanding it does not"
    # Not on the status timer: that would make an unopened panel cost a request
    # every ten seconds for a document nobody is reading.
    poll = text[text.index("onMounted("):text.index("onBeforeUnmount(")]
    assert "loadOrders" not in poll, (
        "the standing orders are fetched on the drawer's timer rather than on demand"
    )
    assert "setInterval(loadOrders" not in text


# --- what the fleet view pays -------------------------------------------------

def test_the_fleet_view_pays_nothing_for_the_drawer_until_it_is_opened():
    """The landing screen is the fleet, on every glance, from a phone.

    The chat costs two polls once it is mounted — the status and the transcript —
    and neither is any use to an operator looking at the fleet. So it is mounted on
    first open and left mounted afterwards, which is also what keeps a half-typed
    message alive across a close.
    """
    app = _read(APP)
    assert "const uberOpen = ref(false)" in app, (
        "the supervisor's drawer starts open, so the fleet opens behind a chat"
    )
    assert re.search(r"<AssistantChat v-if=\"uberSeen\"", app), (
        "the chat is mounted before anyone asked for it, so the landing screen "
        "polls the supervisor"
    )
    assert "const uberSeen = ref(false)" in app
    assert "uberSeen.value = true" in app, "the drawer can never be mounted at all"


def test_the_drawer_remembers_nothing_in_a_browser_store():
    """Deliberate, and the reasoning is the landing screen again: a remembered-open
    drawer opens a phone onto a full-screen chat instead of the fleet, and makes
    every glance fetch the supervisor's status and transcript.

    Which is why ``ALLOWED_STORAGE_KEYS`` in :mod:`tests.test_platform_web_app` did
    not have to grow for this feature. The width is the theme's, not a preference.
    """
    assert "localStorage" not in _read(DRAWER), (
        "the drawer stores something; if it is a preference it belongs in "
        "preferences.js and in ALLOWED_STORAGE_KEYS with a reason"
    )
    keys = re.findall(r"localStorage\.\w+\(\s*([A-Za-z0-9_.]+)", _read(APP))
    assert set(keys) == {"THEME_STORAGE_KEY"}, (
        f"the shell now stores {sorted(set(keys))}; the drawer's open state is "
        "deliberately not remembered"
    )


def test_the_drawer_takes_its_colour_from_the_theme():
    """A hex here is a colour the theme cannot change (main.js owns the palette).

    The accent header is what makes this panel unmistakably not the run navigator,
    so it is the one place most likely to reach for a literal.
    """
    text = _read(DRAWER)
    hexes = re.findall(r"#[0-9a-fA-F]{3,8}\b", text)
    assert not hexes, f"the drawer hardcodes {hexes} instead of naming a theme colour"
    assert 'color="primary"' in text, (
        "the drawer has no accent, so it reads as the same panel as the run "
        "navigator on the other edge"
    )
