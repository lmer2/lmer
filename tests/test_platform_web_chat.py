"""Guards on how the conversation view renders a container's output (T38).

A transcript is written by an agent, from a repository it was pointed at, with tool
output pasted into it, and this pane turns it into markup in the operator's
browser. The renderer that does it is no longer chat's own: T44 moved it to
``Markdown.vue``, shared with the operator channel, and every guard on it —
``html: false``, the DOMPurify allowlist, the scheme allowlist, the fence that
never reflows — moved with it to :mod:`tests.test_platform_web_markdown`. The
extraction was for exactly that reason: one implementation of the dangerous thing,
so a third consumer cannot be the one that ends up without the sanitiser.

What is left here is what belongs to the *conversation* rather than to rendering,
and all of it is invisible in a desktop window:

- a rendered turn is taller than the text it came from, so the 30dvh cap on one
  message and the remembered stick-to-bottom flag have to still be there;
- what *you* sent is still shown verbatim, so the two sides of the conversation
  do not turn into one wall of formatted prose — and the bubble carrying it leans
  right while its words do not, which is one operator report (2026-07-29) and two
  separate CSS declarations doing what looked like one job;
- and, since T84, that the three kinds of turn are drawn on three different
  grounds — the operator, from live testing: *"assistant messages, user messages,
  assistant actions should all be color coded backgrounds"*. The colours are the
  theme's, so what is guarded here is which class of turn names which one, and
  that nothing in this component writes a colour of its own;
  :mod:`tests.test_platform_web_theme` holds the other end (both schemes define
  every name, and the tints stay readable under body text);
- and that the composer still says which session it types into. The wording is a
  prop now, because this component is also the supervisor's chat in the right-hand
  drawer, where naming the recipient is the whole point
  (:mod:`tests.test_platform_web_assistant` holds that end);
- and, since T98, *who each turn is titled as*. The operator, from live testing:
  *"in the agent chats the agent messages are titled as `assistant` - they should
  be `lmer` in run chats and `uber lmer` in the uber lmer chat"*. The name is a
  prop for the same reason the composer's label is, and the underlying property is
  stricter than the wording: a transcript role is a *code* spelling and must reach
  an operator nowhere, so what is guarded is that no role is ever rendered — not
  ``assistant``, and not one a later harness release invents either;
- and, since T99, that a watch firing is drawn as neither party. The harness
  delivers a background monitor's event as a turn in the operator's own role, so
  the server re-attributes it (:mod:`tests.test_platform_transcripts` holds that
  end) and this view has to draw it as the event it is rather than as a bubble;
- and, since T121, that a message you sent stops being *pending* once the run has
  it. Reported live: one stuck at the tail of the conversation as if it had just
  been sent, for the rest of the run, while the transcript above it held the same
  message as an ordinary earlier turn and the agent had plainly acted on it. That
  half is executed rather than read — the transcript copy of a message is not the
  bytes that were sent, and which of them still match is not a question source
  text answers.

Rendering, and how any of it feels one-handed, is verified by building the bundle
and by live test LT3 on a real phone.
"""

import json
import re
import subprocess
from pathlib import Path

# node_binary is the two-root Node lookup (T47): the pinned toolchain is
# invisible from inside the suite through the isolated platform dir, and a copy
# that forgot the second root would not fail, it would skip everywhere. In
# conftest because five modules want it, this one included.
from tests.conftest import node_binary, require_node_toolchain
# The function extractor, imported rather than copied for the reason
# test_platform_web_theme gives about its own import: a second copy of a helper
# that locates source is how a guard starts passing against the wrong text.
from tests.test_platform_web_app import _chat_function
# Same rule for the two halves of "what an operator can actually read": the drawer's
# guards and these are one seam, and a second extractor would be a second answer to
# which strings count as visible.
from tests.test_platform_web_assistant import _operator_words

WEB = Path(__file__).resolve().parent.parent / "web"
CHAT = WEB / "src" / "components" / "Chat.vue"
DRAWER = WEB / "src" / "components" / "AssistantChat.vue"

#: The two names an lmer session is titled with, and nothing else: a worker is
#: ``lmer`` and the one supervising them is ``uber lmer`` (operator request,
#: 2026-07-27). Held here as the closed set because this is the component that
#: renders whichever one it is handed.
AGENT_NAMES = ("lmer", "uber lmer")

#: The theme colour each class of turn is drawn on, and the class in Chat.vue's
#: scoped stylesheet that reaches it. Here rather than inline because two modules
#: hold one seam between them: this one checks the markup names these colours,
#: :mod:`tests.test_platform_web_theme` checks both schemes define them.
CHAT_GROUNDS = {
    # The agent's own words — the bulk of the pane, so the calmest of the three.
    "chat-agent": "ground-agent",
    # Anything you sent, however it travelled: the composer, an answer merged back
    # from the ask channel, or a bubble the transcript has not caught up with.
    "chat-operator": "ground-operator",
    # What the agent *did*: the tool rows, and the internals behind the toggle.
    "chat-action": "ground-action",
}


def _read(path):
    return path.read_text(encoding="utf-8")


def _chat_rule(selector):
    """One declaration block from Chat.vue's scoped stylesheet.

    Same helper as :mod:`tests.test_platform_web_app`.
    """
    style = _read(CHAT)
    style = style[style.index("<style"):]
    block = style[style.index(f"{selector} {{"):]
    return block[:block.index("}")]


def test_your_own_lines_are_still_shown_exactly_as_you_sent_them():
    """Two reasons, and either one on its own would be enough.

    What you typed went to a session as bytes; a bubble that quietly ate a pair of
    asterisks would be misreporting what the run actually received. And the two
    sides of this conversation have to stay tellable apart at a glance — with both
    halves rendered the same way, a long scroll is one wall of formatted prose.
    """
    text = _read(CHAT)
    assert 'v-if="message.text && message.role === \'user\'"' in text, (
        "the user's own turns go through the renderer too"
    )
    assert text.count("said plain") == 2, (
        "a sent message and one still pending must both be shown verbatim"
    )
    # The lean is what carries the distinction on a phone, and it predates this.
    assert ".turn-user {" in text and "align-self: flex-end" in text


def test_the_bubble_leans_right_and_the_words_in_it_do_not():
    """The operator, reading their own messages back: "the right alignment of the
    container is fine, the text being right justified is odd to read though".

    Two different things were doing one job. ``align-self`` puts the bubble on the
    right, which is the signal — a thumb-scroll tells the two sides apart without
    reading either. ``text-align: end`` right-*justified* the words inside it, which
    gives every line a ragged left edge, and the left edge is where the eye goes back
    to for the next line.

    So the alignment declaration is gone rather than replaced: the inherited value is
    ``start``, and a component that restated it would be a second place to change.
    The one restatement that stays is ``.tools``, and it stays *because* nothing leans
    the text any more — an injected turn carries the operator's role without being
    the operator's words, and machine text must read left-to-right whatever a future
    lean does to the bubble around it.
    """
    lean = _chat_rule(".turn-user")
    assert "align-self: flex-end" in lean, "your own turns no longer lean right"
    assert "text-align" not in lean, (
        "the operator's own words are justified against the right edge again; the "
        "container leans, the prose in it does not"
    )
    assert "text-align: start" in _chat_rule(".tools"), (
        "a tool row inherits whatever the turn around it aligns to, so a lean put "
        "back on .turn-user would justify machine output to the right"
    )


def test_the_cap_on_one_message_survives_a_rendered_one():
    """The bound moved from a ``<p>`` to a component; it had to move with it.

    A rendered turn is *taller* than the text it came from — a list gains its
    bullets and its gaps, a table its rows — so the cap that keeps one message
    from filling the pane and pushing every other turn out of reach matters more
    after this change than before it, and the class that carries it is now on two
    different elements: the ``<p>`` for your half and ``<Markdown>`` for the
    agent's. It reaches the second one because a child component's root element
    carries its parent's scope attribute as well as its own — which is also why
    the cap can stay here, in the view that has a reason to bound a turn, while the
    operator channel's entries (short, and not in a scrolling pane) do not get one.
    """
    text = _read(CHAT)
    rule = _chat_rule(".said")
    assert "max-height" in rule and "overflow-y: auto" in rule, (
        "one message stretches the conversation box instead of scrolling"
    )
    assert "dvh" in rule, "vh does not notice the chrome a phone browser hides"
    assert re.search(r"<Markdown[^>]*\sclass=\"[^\"]*\bsaid\b", text, re.S), (
        "the rendered body does not carry the bound"
    )
    assert 'class="text-body-medium said plain"' in text, (
        "your own half does not carry the bound"
    )
    # Still the same conversation box, still only following when it is being read.
    assert "const following = ref(true)" in text
    assert "if (follow) nextTick(stickToBottom)" in text
    assert "max-height: 30dvh" in _chat_rule(".said")


# --- the colour coding (T84) --------------------------------------------------

def test_the_three_kinds_of_turn_are_drawn_on_three_different_grounds():
    """The request, and the reason it is three and not two.

    A scroll of this pane holds the agent talking, you talking, and the agent
    *doing*, and rendered as one column of prose it takes reading a header per turn
    to tell them apart — which is precisely what nobody does on a phone. Three
    grounds, one per class, and three *different* ones: two classes sharing a tone
    is the coding that reads as complete and answers nothing.
    """
    text = _read(CHAT)

    assert len(set(CHAT_GROUNDS.values())) == 3, "the fixture lost a class"
    for colour, css_class in CHAT_GROUNDS.items():
        rule = _chat_rule(f".{css_class}")
        assert f"rgb(var(--v-theme-{colour}))" in rule, (
            f".{css_class} does not paint the theme's {colour!r}"
        )

    # Every turn gets one, decided in one place: a turn whose header sits on one
    # colour and whose words sit on another is worse than no coding at all.
    assert "`ground-${ground(message)}`" in text, (
        "the turn's ground is not driven by the one function that decides it"
    )


def test_the_coding_writes_no_colour_of_its_own():
    """The theme owns the palette (main.js), in both schemes.

    A literal here is a colour the scheme switcher cannot repaint — which for a
    *background under body text* is not a drifting shade, it is a dark-scheme pane
    with light-scheme bubbles in it. House style rides along: `outlined` and `flat`
    were swept out of every component.
    """
    text = _read(CHAT)

    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", text), (
        "the conversation hardcodes a colour instead of naming a theme one"
    )
    assert not re.findall(r"rgb\((?!var\()", text), (
        "a literal rgb() is the same colour outside the theme, spelled differently"
    )
    assert 'variant="outlined"' not in text and 'variant="flat"' not in text


def test_everything_you_sent_shares_one_ground():
    """One identity for "something I sent", however it travelled.

    Three routes reach this pane with the operator's words: typed into the composer
    and read back from the transcript, merged in from the ask channel (T67, which
    the harness records nothing about), and held locally as pending until the
    transcript catches up. They are one thing to the person who sent them, so a
    pending bubble that changed colour when it settled — or an answer that looked
    like the agent's — would be the view inventing a distinction nobody made.

    The ask-channel marker stays inside that identity rather than replacing it:
    where a turn came from is a caption, not a fourth class.
    """
    text = _read(CHAT)
    ground = _chat_function("function ground(message)")

    assert "if (message.role === 'user') return 'operator'" in ground, (
        "what you said is no longer keyed on the role, which is the only thing the "
        "three routes have in common"
    )
    assert text.count('class="turn turn-user ground-operator"') == 1, (
        "the pending bubble is not on the same ground as the message it becomes"
    )
    assert "message.via" not in ground, (
        "the ground consults where a turn came from; a merged answer is yours, and "
        "the header is where that is said"
    )
    assert "· ask channel" in text, "the merged-turn marker went with the coding"


def test_what_the_harness_injected_is_coded_as_machinery_and_not_as_yours():
    """``kind`` is asked before ``role``, and that order is the whole property.

    A hook's output and a system reminder are injected into the model's context as
    turns with role ``'user'`` (``transcripts.py``: ``kind = 'injected' if
    record.get('isMeta')``), and they are not something the operator sent. They are
    the same machinery as a tool call and they hide behind the same toggle, so they
    share its ground — asking the role first would paint every internal turn as one
    of your own messages, on the one screen an operator uses to check what the run
    was actually told.
    """
    ground = _chat_function("function ground(message)")

    assert ground.index("message.kind !== 'said'") < ground.index("message.role"), (
        "the role decides before the kind does, so an injected turn is coded as "
        "something the operator sent"
    )
    assert "return 'action'" in ground


def test_the_agents_actions_keep_their_ground_inside_a_turn_that_also_spoke():
    """A said turn with tool rows is one turn holding two kinds of content.

    Which is the common shape: an agent says what it is about to do and then does
    it, in one transcript turn. Colouring the whole turn as prose would leave the
    doing invisible, and the request named it as its own class.
    """
    text = _read(CHAT)

    assert 'class="tools ground-action"' in text, (
        "the tool rows take the surrounding turn's ground"
    )
    assert 'v-if="message.tools.length"' in text, (
        "a turn with no tools still draws the block, so every turn carries an "
        "empty strip of the action ground"
    )
    # And they stay one block rather than a stack of striped lines.
    rule = _chat_rule(".tools")
    assert "border-radius" in rule and "padding" in rule


# --- who a turn is titled as (T98) --------------------------------------------

def _chat_consumers():
    """Every component that mounts the shared conversation, with its tag.

    Found rather than listed, because the property being guarded is about *all* of
    them: a third consumer that titled its turns from the raw role would be the
    reported bug back on a surface nobody thought to check.
    """
    found = {}
    for path in sorted((WEB / "src").rglob("*.vue")):
        tags = re.findall(r"<Chat\b[^>]*>", _read(path), re.S)
        if tags:
            found[path.name] = tags
    return found


def test_the_agents_turns_are_titled_lmer_rather_than_by_their_transcript_role():
    """The report, and the reason the fix is a map rather than a nicer default.

    The operator, from live testing: *"in the agent chats the agent messages are
    titled as `assistant` - they should be `lmer` in run chats and `uber lmer` in
    the uber lmer chat"*. What leaked was the transcript's own role, rendered
    straight into the header — so the name is now a prop with the worker's name as
    its default (a run's chat passes nothing, which is why that default is the one
    that has to be right) and the role itself is never shown.
    """
    text = _read(CHAT)

    assert re.search(
        r"agentLabel: \{ type: String, default: 'lmer' \}", text,
    ), "the agent's name is not a prop defaulting to the worker's name"
    assert "{{ speaker(message) }}" in text, (
        "the header renders something other than the one function that decides "
        "what a turn is titled"
    )
    speaker = _chat_function("function speaker(message)")
    assert "props.agentLabel" in speaker, (
        "the agent's turns ignore the name they were given, so passing one does "
        "nothing"
    )


def test_no_turn_can_be_titled_with_a_raw_transcript_role():
    """Stricter than "don't say assistant", and deliberately.

    The roles this adapter emits are ``user``, ``assistant``, ``system`` and — since
    T99 — ``monitor``, and the format they come from is explicitly not a contract
    (spec D6), so the next release may add another. A fallthrough that rendered the
    role would put ``system`` on the screen today and whatever is invented next
    tomorrow, which is the same bug with a different word in it.
    """
    speaker = _chat_function("function speaker(message)")

    assert "OTHER_SPEAKER" in speaker, (
        "an unmapped role has no name to fall back on, so it renders as itself"
    )
    assert not re.search(r"return\s+message\.role", speaker), (
        "a role is handed to the operator as the name of a turn"
    )
    text = _read(CHAT)
    assert re.search(r"const OTHER_SPEAKER = '[^']+'", text), (
        "the fallback is not a name of its own"
    )


def test_the_word_assistant_is_shown_to_an_operator_nowhere():
    """AssistantChat.vue's header states the contract; this is the other end of it.

    ``assistant`` is the spelling of the module, the taskdef and every API field,
    and it reaches an operator nowhere — and this component is where it would,
    because it is the one that renders a transcript role. Checked on the strings an
    operator can actually read, so identifiers keep the API's spelling.
    """
    words = _operator_words(CHAT)
    assert "assistant" not in words.lower(), (
        f"the conversation calls a session an assistant to the operator: {words!r}"
    )


def test_every_consumer_of_the_conversation_titles_its_turns_deliberately():
    """The audit, kept as a test because the default is what makes it cheap.

    A run's chat passes nothing and gets ``lmer``; the supervisor's drawer passes
    ``uber lmer``, which is the whole of what the drawer's two-names defence is
    (:mod:`tests.test_platform_web_assistant` holds the rest of it). What must not
    happen is a consumer inventing a third name for the same thing, or passing the
    code's word down as a label.
    """
    consumers = _chat_consumers()
    assert set(consumers) == {"AssistantChat.vue", "RunDetail.vue"}, (
        f"the set of surfaces showing a conversation changed: {sorted(consumers)}"
    )

    for name, tags in consumers.items():
        for tag in tags:
            passed = re.search(r'\sagent-label="([^"]*)"', tag)
            if passed is None:
                continue
            assert passed.group(1) in AGENT_NAMES, (
                f"{name} titles the agent's turns {passed.group(1)!r}, which is "
                "neither of the two names an lmer session goes by"
            )

    drawer = re.search(r'<Chat\b[^>]*\sagent-label="([^"]*)"', _read(DRAWER), re.S)
    assert drawer and drawer.group(1) == "uber lmer", (
        "the drawer leaves the shared conversation on its default name, so the "
        "supervisor's own turns are titled as a worker's"
    )


# --- a watch firing is neither party (T99) -----------------------------------

def test_a_watch_firing_is_drawn_as_an_event_and_not_as_anybodys_bubble():
    """What the operator saw: a monitor event styled as a message they had sent.

    The harness injects a watch's event into the session as a turn in the
    *operator's* role, so the server re-attributes it and this view keys on that —
    on the role, like every other drawing decision here, which is what keeps ``via``
    a caption rather than a second way to decide how a turn looks. Drawn on the
    action ground because that is what this pane already means by machinery; a
    fourth colour would be one more thing to tell apart for a rare turn.
    """
    text = _read(CHAT)
    ground = _chat_function("function ground(message)")

    assert "return message.role === 'assistant' ? 'agent' : 'action'" in ground, (
        "the agent ground is 'not the operator' again, so a watch firing is drawn "
        "as the session talking"
    )
    assert "message.via" not in ground, (
        "the ground consults where a turn came from rather than who it is from"
    )
    assert text.count("message.via") == 1, (
        "`via` decides something besides the ask-channel caption; the monitor's "
        "turns are identified by their role, on both sides of the wire"
    )

    assert "monitor: 'the watch fired'" in text, (
        "a monitor turn is titled from the fallback or from its role, so nothing "
        "says an event fired"
    )
    assert re.search(
        r'v-else-if="message\.text && message\.role === \'monitor\'"', text,
    ), "a watch's event goes through the operator's branch or the renderer"
    # Its own line, but still inside the bound every other body carries: an event
    # is short, and an event that was not would push the conversation out of reach.
    assert 'class="text-body-small said watch"' in text
    assert "white-space: pre-wrap" in _chat_rule(".watch"), (
        "the condition and the event run into one line"
    )


# --- the composer ---------------------------------------------------------------

def test_the_composer_names_the_session_and_can_be_told_a_better_name():
    """Whose conversation this is has to be readable at the moment of typing.

    The default is the run's wording, which is right where this component started:
    a run's chat is opened *from* that run, so "this session" is the session on the
    screen. It is a prop because the same component is docked in the supervisor's
    drawer (AssistantChat.vue), and there "this session" is the one sentence the
    whole feature exists to prevent — the mistake is telling the supervisor to stop
    a run while typing at the run itself. Hardcoding the label would leave the
    drawer with a worker's wording on the last line read before the message goes.
    """
    text = _read(CHAT)

    assert re.search(
        r"composerLabel: \{ type: String, default: '([^']*session[^']*)' \}", text,
    ), "the composer's label is not a prop that still defaults to the run's wording"
    assert ':label="composerLabel"' in text, (
        "the field is labelled with something other than the prop, so passing one "
        "changes nothing"
    )


# --- executed: a sent message stops being pending (T121) ----------------------
#
# The one thing in this file that cannot be pinned by reading. A bubble is held
# until the poll finds the message in the transcript, and what comes back is not
# what went out: the harness records the turn its own way and the server normalises
# it on the way to the browser. So both halves are real here — the server's own
# normaliser builds the page, and the component's own settle rule runs over it
# under Node — and the fixtures are transcript *records*, not messages, because the
# transformation being reproduced happens inside that normaliser.

#: Timestamps for the fixtures. Fixed rather than derived: nothing here depends on
#: a clock (the settle rule has none — see the test at the end of this section),
#: and a real transcript's stamps are the harness's milliseconds.
_STAMPS = [f"2026-07-29T10:0{index}:00.000Z" for index in range(6)]


def _operator_turn(text, at=_STAMPS[1]):
    """One transcript record for a message typed at a live session.

    The shape a real one has, from transcripts on this codebase: role ``user`` with
    the harness's own ``promptSource``/``origin`` on it, which is what keeps it out
    of the injected classification and makes it a turn the operator said. *text* is
    either the string the harness stores or a list of content blocks — both are real
    shapes, and the second one is a turn the harness recorded in pieces.
    """
    return {
        "type": "user",
        "timestamp": at,
        "promptSource": "typed",
        "origin": {"kind": "human"},
        "message": {"role": "user", "content": text},
    }


def _agent_turn(text, at=_STAMPS[2]):
    """One transcript record for the session's own reply."""
    return {
        "type": "assistant",
        "timestamp": at,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _conversation(records):
    """Those records as the messages route would serve them, ``seq`` and all.

    The server's normaliser rather than a description of it: everything that breaks
    the text match happens inside it — a wrapper tag cut out of the middle of an
    operator turn, a credential shape masked, a turn past ``TEXT_LIMIT`` kept by its
    tail — and a fixture stating the result by hand would keep passing after that
    code changed under it.

    ``seq`` is assigned exactly where :func:`lmer_platform.transcripts.read_messages`
    assigns it, as the index of the turn in the whole run's conversation. That is
    what makes a ``since`` in these fixtures the cursor a poll really hands back: it
    is the index one past the last turn served.
    """
    from lmer_platform import transcripts

    messages = transcripts.normalise_records(records)
    for index, message in enumerate(messages):
        message.seq = index
    return [message.to_dict() for message in messages]


def _chat_body(signature):
    """One Chat.vue function, closing brace included.

    The shared extractor stops *at* that brace, which is all its callers need to
    read a decision out of it — this is the one place the function has to run.
    """
    return _chat_function(signature) + "\n}"


#: One Node run over the component's own settle rule, once per case: the two refs
#: it reads are the plain boxes ``ref()`` hands it, and the JS half only reports
#: which bubbles it left standing. Every assertion is in Python.
_SETTLE_PROBE = """
const messages = { value: [] }
const pending = { value: [] }

%s

%s

const cases = %s
const seen = {}
for (const [name, probe] of Object.entries(cases)) {
  messages.value = probe.messages
  pending.value = probe.pending
  settlePending()
  seen[name] = pending.value.map((item) => item.text)
}
console.log(JSON.stringify(seen))
"""

#: ``send()`` itself, run against a stubbed control plane (issue 194). Everything
#: the function closes over is a plain box or a no-op — the point is narrow: what
#: the reply becomes on the item that is held. The refs are the shapes ``ref()``
#: hands it, and the JS half reports only the pending list it produced.
_SEND_PROBE = """
const props = { sessionId: 'probe-session' }
let cursor = %d
let reply = null
const draft = { value: '' }
const sending = { value: false }
const problem = { value: null }
const pending = { value: [] }
const following = { value: false }
const stale = () => false
const nextTick = () => {}
const stickToBottom = () => {}
const poll = () => {}
const sendSessionInput = async () => reply
const generation = 0

%s

const cases = %s
const typed = %s
;(async () => {
  const held = {}
  for (const [name, answer] of Object.entries(cases)) {
    reply = answer
    pending.value = []
    draft.value = typed
    await send()
    held[name] = pending.value.map((item) => ({
      text: item.text,
      submitConfirmed: item.submitConfirmed,
      since: item.since,
    }))
  }
  console.log(JSON.stringify(held))
})()
"""

#: The label ladder, run rather than read (issue 194). ``props`` and the grace
#: constant are what the function closes over in the component; the cases are the
#: three rungs, and the JS half only reports the caption it produced.
_LABEL_PROBE = """
const PENDING_GRACE_MS = %d
const SUBMIT_UNCONFIRMED_LABEL = %s
const props = { now: 0 }

%s

const cases = %s
const said = {}
for (const [name, probe] of Object.entries(cases)) {
  props.now = probe.now
  said[name] = pendingLabel(probe.item)
}
console.log(JSON.stringify(said))
"""

#: A message about the harness's own markup, which the operator writes on the one
#: screen they debug this platform from. ``<system-reminder>`` is in
#: ``transcripts._WRAPPER_TAGS``, and the tag is cut out of a *user* turn wherever
#: it appears — quoted in a sentence included.
_QUOTING_MARKUP = "check the <system-reminder> handling before you push"

#: A message naming a credential, which the read path masks on its way out.
_NAMING_A_TOKEN = "the runner env still has GITLAB_TOKEN=glpat-AAAABBBBCCCCDDDD in it"

#: A message the harness recorded in two blocks, and what was typed. The join is a
#: blank line, so the two differ by one newline and by nothing else — the whole of
#: what the whitespace layer is for.
_IN_PIECES = [
    {"type": "text", "text": "two things:"},
    {"type": "text", "text": "- rebase on prep-release\n- then push"},
]
_TYPED_IN_ONE = "two things:\n- rebase on prep-release\n- then push"


def _long_paste():
    """A message longer than the server's per-turn cap, and its head is the point.

    ``keep="tail"`` is deliberate there (an agent's turn ends with its conclusion),
    so what the transcript loses from a *pasted* one is the sentence the operator
    wrote above it. No normalisation can put that back.
    """
    from lmer_platform import transcripts

    frames = "\n".join(f"  at frame {index} of the traceback" for index in range(900))
    text = f"this is the failure, the interesting part is the top:\n{frames}"
    assert len(text) > transcripts.TEXT_LIMIT, "the fixture no longer exceeds the cap"
    return text


def _cases():
    """Every fixture, built once. Case name → the page and the bubbles held."""
    working = _agent_turn("Working on it.", _STAMPS[0])
    return {
        # The reported shape: the turn is in the transcript, later than the send,
        # and the agent answered it — but the words are not the ones that went.
        "quoted_markup": {
            "messages": _conversation([
                working,
                _operator_turn(_QUOTING_MARKUP),
                _agent_turn("Checked — that strip is deliberate.", _STAMPS[2]),
            ]),
            "pending": [{"text": _QUOTING_MARKUP, "at": 0, "since": 1}],
        },
        "masked_credential": {
            "messages": _conversation([
                working,
                _operator_turn(_NAMING_A_TOKEN),
                _agent_turn("Rotated it and re-spawned.", _STAMPS[2]),
            ]),
            "pending": [{"text": _NAMING_A_TOKEN, "at": 0, "since": 1}],
        },
        "kept_by_its_tail": {
            "messages": _conversation([
                working,
                _operator_turn(_long_paste()),
                _agent_turn("That is a broken mount.", _STAMPS[2]),
            ]),
            "pending": [{"text": _long_paste(), "at": 0, "since": 1}],
        },
        # No reply after the turn, so only the whitespace layer can settle this one.
        "recorded_in_pieces": {
            "messages": _conversation([working, _operator_turn(_IN_PIECES)]),
            "pending": [{"text": _TYPED_IN_ONE, "at": 0, "since": 1}],
        },
        # Same shape, different words: whitespace is forgiven and nothing else is.
        "different_words": {
            "messages": _conversation([
                working, _operator_turn("rebase on prep-release"),
            ]),
            "pending": [{"text": "rebase on main", "at": 0, "since": 1}],
        },
        "a_prefix_of_a_longer_turn": {
            "messages": _conversation([
                working, _operator_turn("yes please, and push it when it is green"),
            ]),
            "pending": [{"text": "yes", "at": 0, "since": 1}],
        },
        # The harness is streaming the turn it was already on and has this message
        # queued: nothing it has written is an operator turn yet.
        "queued_and_unwritten": {
            "messages": _conversation([
                working,
                _agent_turn("Still reading the log.", _STAMPS[2]),
                _agent_turn("Ran the tests.", _STAMPS[3]),
            ]),
            "pending": [{"text": _QUOTING_MARKUP, "at": 0, "since": 1}],
        },
        # An operator turn after the send, and every agent turn older than it.
        "written_but_not_answered": {
            "messages": _conversation([working, _operator_turn("something else")]),
            "pending": [{"text": _QUOTING_MARKUP, "at": 0, "since": 1}],
        },
        # The double "yes": the identical answer is *earlier* in the conversation.
        "an_older_identical_answer": {
            "messages": _conversation([
                _operator_turn("yes", _STAMPS[0]),
                _agent_turn("Pushed.", _STAMPS[1]),
            ]),
            "pending": [{"text": "yes", "at": 0, "since": 2}],
        },
    }


def _settled():
    """Run the settle rule over every case. Returns case name → bubbles left."""
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")

    script = _SETTLE_PROBE % (
        _chat_body("function comparable"),
        _chat_body("function settlePending"),
        json.dumps(_cases()),
    )
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"the settle probe failed:\n{result.stdout}\n{result.stderr}"
    )
    return json.loads(result.stdout)


def test_the_transcript_really_does_come_back_saying_something_else():
    """The reproduction, stated on the server's side before anything is asked of
    the view: these are not paraphrases, they are what the messages route serves.

    Three transformations, all deliberate where they live and none of them the
    view's business (:mod:`lmer_platform.transcripts`): a wrapper tag is cut out of
    a user turn wherever it appears, a credential shape is masked, and a turn past
    the per-turn cap keeps its tail. Each one leaves a transcript copy that is not
    the string that was sent, which is what the exact-text match was measuring.
    """
    cases = _cases()

    quoted = cases["quoted_markup"]["messages"][1]
    assert quoted["role"] == "user" and quoted["kind"] == "said", (
        "the turn is no longer served as something the operator said, so this "
        "fixture reproduces a different bug than the reported one"
    )
    assert "<system-reminder>" not in quoted["text"], (
        "the wrapper strip no longer touches a quoted tag"
    )
    assert quoted["text"] != _QUOTING_MARKUP

    masked = cases["masked_credential"]["messages"][1]
    assert "glpat-AAAABBBBCCCCDDDD" not in masked["text"], (
        "the read path served a credential; the scrub is what this case is about"
    )

    tail = cases["kept_by_its_tail"]["messages"][1]
    assert tail["truncated"] is True, "the fixture no longer trips the per-turn cap"
    assert not tail["text"].startswith("this is the failure"), (
        "the cap kept the head, so the operator's own sentence survived"
    )

    # And the whitespace case differs in whitespace *only*, which is the property
    # that makes it safe to forgive.
    pieces = cases["recorded_in_pieces"]["messages"][1]["text"]
    assert pieces != _TYPED_IN_ONE, "the fixture round-trips and reproduces nothing"
    assert pieces.split() == _TYPED_IN_ONE.split(), (
        "the two differ in more than whitespace, so this case would be asking the "
        "match to forgive words"
    )


def test_a_message_the_run_answered_stops_being_pending():
    """The report: a sent message stuck at the tail of the conversation for the
    rest of the run, while the transcript above it held that same message as an
    ordinary earlier turn and the agent had acted on it.

    All three are cases no text rule can settle — the words really are different by
    the time they come back — so what settles them is arrival: an operator turn
    recorded after the send's cursor, with a turn of the agent's after *that*. The
    bubble is a stand-in for the transcript's copy, and once the transcript has one,
    a second copy of the message on the screen is the bug being fixed.
    """
    settled = _settled()

    for case in ("quoted_markup", "masked_credential", "kept_by_its_tail"):
        assert settled[case] == [], (
            f"{case}: the bubble is still pending after the run answered it — "
            f"{settled[case]}"
        )


def test_the_text_match_forgives_whitespace_and_nothing_else():
    """The other layer, and it is the one that settles a bubble on the words.

    A turn the harness recorded in pieces comes back joined with a blank line
    between them, so the transcript's copy and the sent string differ by one
    newline. Collapsing whitespace on both sides matches them — and cannot do more
    than that: two messages whose words differ stay two messages, and a message
    that is a *prefix* of a longer turn is not that turn.

    Isolated deliberately: neither of those cases has an agent turn after the
    operator's, so the arrival backstop cannot be what answers them.
    """
    settled = _settled()

    assert settled["recorded_in_pieces"] == [], (
        "a turn the harness recorded in two blocks never matches what was typed, "
        f"so the bubble outlives it — {settled['recorded_in_pieces']}"
    )
    assert settled["different_words"] == ["rebase on main"], (
        "a different message settled this bubble, so the match now forgives words"
    )
    assert settled["a_prefix_of_a_longer_turn"] == ["yes"], (
        "a longer turn that merely starts with what was sent settled the bubble"
    )


def test_a_bubble_stays_up_until_the_transcript_has_the_message():
    """The two ways the backstop must not fire, and both are the same failure: the
    operator's words gone from the view with nothing holding them.

    A session that was working when the message went keeps streaming the turn it
    was already on and queues the message — Claude Code records such a turn with a
    prompt source of its own — so agent turns later than the send prove nothing
    about whether this message has been read. And an operator turn with no reply
    after it is not evidence either: the ask channel's answers are merged into this
    timeline by their own clock (T67), not by the queue a typed message is in.

    The seq guard rides along here: the identical answer *earlier* in the
    conversation is the case it was written for, and neither layer may reach back
    past the cursor to find it.
    """
    settled = _settled()

    assert settled["queued_and_unwritten"] == [_QUOTING_MARKUP], (
        "the bubble went while the harness still had the message queued and "
        "unwritten, which is the drop a timeout would have caused"
    )
    assert settled["written_but_not_answered"] == [_QUOTING_MARKUP], (
        "an operator turn the agent has not replied to settled a different "
        "message's bubble"
    )
    assert settled["an_older_identical_answer"] == ["yes"], (
        "answering \"yes\" twice settles the second bubble against the first "
        "answer, and the view stops showing that the new one is in flight"
    )


def test_nothing_about_the_bubble_is_decided_by_a_clock():
    """Why a timeout was rejected, kept as a property of the code.

    A bubble dropped on a timer is dropped while the message is genuinely on its
    way, which is the one thing this view must never do — a send that succeeded may
    not read as a send that went nowhere. So the settle rule may consult the
    transcript and nothing else; the clock in this component belongs to the
    *label*, which changes its wording without touching what is held.
    """
    settle = _chat_function("function settlePending")
    match = _chat_function("function comparable")

    for name, body in (("settlePending", settle), ("comparable", match)):
        assert "Date.now" not in body and "item.at" not in body, (
            f"{name} decides on a clock, so a slow transcript drops the bubble"
        )
    assert "message.seq >= item.since" in settle, (
        "the settle rule no longer bounds itself to turns that arrived after the "
        "send, so an older identical message can settle a new bubble"
    )
    label = _chat_function("function pendingLabel")
    assert "props.now - item.at" in label and "PENDING_GRACE_MS" in label, (
        "the grace period stopped being about the wording"
    )
    assert "pending.value" not in label and "splice" not in label, (
        "the label reaches into what is held; only the transcript may do that"
    )


# --- and what it says once the grace window is up (issue 194) -----------------

#: A message with a newline in it, which is the shape the issue was reported over.
_TYPED_AT_A_SESSION = "first line about the failure\nsecond line with /a/path in it"

#: The same message with a newline left on the end — a composer the operator hit
#: Enter in once more, or a paste that carried its own line break. It differs from
#: `_TYPED_AT_A_SESSION` only in the whitespace `send()`'s `trim()` is there to take
#: off, which is what makes it the fixture that can tell whether the call is still
#: being made.
_TYPED_WITH_A_TRAILING_NEWLINE = _TYPED_AT_A_SESSION + "\n"

#: The cursor the send is made at, so the held item's `since` can be checked to be
#: the transcript's end rather than a count of anything.
_CURSOR_AT_SEND = 7


def _held(typed=_TYPED_AT_A_SESSION):
    """Run ``send()`` against three replies. Case name → the pending items."""
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")

    cases = {
        # What every `/input` with an Enter answers today: the CR was written, the
        # submit is not observable from outside the TUI.
        "unconfirmed": {
            "session": "probe-session", "bytes_written": 61,
            "submit_confirmed": False, "note": "…terminal view.",
        },
        # A control plane that can confirm one. Nothing does today; the rung is kept
        # so the *reason* the caption is weak stays tied to the fact rather than to
        # the route's current behaviour.
        "confirmed": {
            "session": "probe-session", "bytes_written": 61,
            "submit_confirmed": True,
        },
        # A reply that says nothing about a submit — an older daemon, or a shape
        # that changes. Silence is not a confirmation.
        "silent": {"session": "probe-session", "bytes_written": 61},
    }
    script = _SEND_PROBE % (
        _CURSOR_AT_SEND,
        _chat_body("async function send"),
        json.dumps(cases),
        json.dumps(typed),
    )
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"the send probe failed:\n{result.stdout}\n{result.stderr}"
    )
    return json.loads(result.stdout)


def test_the_reply_from_the_control_plane_reaches_the_bubble():
    """The root cause of the reported failure, pinned where it lived.

    `send()` discarded the reply. That is the whole bug on this side: the route was
    already answering `submit_confirmed: false` on every message typed at a TUI, and
    `POST /api/sessions/{id}/input` was already forwarding it, so nothing had to be
    built to know — the answer was arriving and being dropped, and the pane went on
    to call the message sent.

    So this runs the real `send()` and looks at what it held: the reply's verdict has
    to be on the item, or the caption below has nothing to be honest with. Executed
    rather than read, because "the function assigns a field" is what a source check
    would confirm, and the thing that matters is which value ends up there — an
    unconfirmed reply must not produce a confirmed item.
    """
    held = _held()

    assert held["unconfirmed"] == [{
        "text": _TYPED_AT_A_SESSION,
        "submitConfirmed": False,
        "since": _CURSOR_AT_SEND,
    }], (
        "the reply is not reaching the held item, so the view is back to claiming "
        f"every send was submitted — {held['unconfirmed']}"
    )
    assert held["silent"][0]["submitConfirmed"] is False, (
        "a reply that said nothing about the submit produced a confirmed item"
    )
    assert held["confirmed"][0]["submitConfirmed"] is True, (
        "a confirmed submit is being reported as unconfirmed, so the caption would "
        "hedge about something that is a fact"
    )


def test_what_was_typed_is_what_is_held_and_sent():
    """The bubble holds the message, newline and all.

    Both halves of the issue meet here: the composer sends a multi-line body (the
    TUI accepts one whole — that half was established against a real claude), and
    the bubble that stands in for it until the transcript catches up has to be the
    same text, or the matching rule that settles it later cannot work.
    """
    held = _held()

    for case, items in held.items():
        assert len(items) == 1, f"{case}: one send, {len(items)} bubbles"
        assert items[0]["text"] == _TYPED_AT_A_SESSION, (
            f"{case}: the bubble holds {items[0]['text']!r}"
        )
        assert items[0]["since"] == _CURSOR_AT_SEND, (
            f"{case}: `since` is {items[0]['since']}, not the transcript's end, so "
            "the settle rule would look for the message in the wrong place"
        )


def test_a_newline_left_on_the_end_is_taken_off_before_the_message_goes():
    """The one `send()` call the rest of this module cannot see.

    `send()` derives a single value — `draft.value.trim()` (`Chat.vue:486`) — and
    hands that same binding to `sendSessionInput` and to the held item, so the text
    on the bubble is the text the POST body carried. That coupling is what lets this
    check the request through what it holds.

    It needs its own fixture because `_TYPED_AT_A_SESSION` has no whitespace at
    either end: with the `trim()` deleted, every other assertion here still passes
    while the body on the wire changes. The newline is the shape worth pinning
    rather than spaces, because it is the character this whole issue is about, and
    because it does not fail loudly downstream — `_ensure_submit_cr`
    (`supervisor.py:1260`) treats a trailing LF as *not* a submit and appends the CR
    behind it, so an untrimmed newline is typed into the TUI's box as a literal line
    break and the message lands with a blank line on the end. Delivered, unremarkable
    in the log, and different from what the operator wrote.
    """
    held = _held(_TYPED_WITH_A_TRAILING_NEWLINE)

    for case, items in held.items():
        assert len(items) == 1, f"{case}: one send, {len(items)} bubbles"
        text = items[0]["text"]
        assert not text.endswith("\n"), (
            f"{case}: the trailing newline survived into {text!r}, so it is in the "
            "POST body too — typed at the TUI as a line break rather than dropped"
        )
        assert text == _TYPED_AT_A_SESSION, (
            f"{case}: the message came out as {text!r} rather than what was typed "
            "with the trailing newline taken off, so the send is no longer trimming"
        )


def _labels():
    """Run the label ladder over its three rungs. Case name → the caption."""
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")

    grace = int(re.search(
        r"const PENDING_GRACE_MS = (\d+)", _read(CHAT),
    ).group(1))
    unconfirmed = re.search(
        r"const SUBMIT_UNCONFIRMED_LABEL = \(\n(.*?)\n\)\n", _read(CHAT), re.S,
    )
    assert unconfirmed, "the unconfirmed caption is no longer a named constant"
    cases = {
        # Inside the grace window, whatever the control plane said: a transcript
        # that has not caught up in a few seconds is the ordinary case.
        "still_going": {"now": 1000, "item": {"at": 0, "submitConfirmed": False}},
        # Past it, and the submit was never confirmed — what every chat send is
        # today, because a CR typed at a TUI is not an observable submit.
        "unconfirmed": {"now": grace + 1000, "item": {"at": 0, "submitConfirmed": False}},
        # Past it, with the submit a fact: only the transcript is behind.
        "confirmed": {"now": grace + 1000, "item": {"at": 0, "submitConfirmed": True}},
        # A reply that said nothing about the submit is not a confirmation of one.
        "silent_reply": {"now": grace + 1000, "item": {"at": 0}},
    }
    script = _LABEL_PROBE % (
        grace,
        "(" + unconfirmed.group(1) + ")",
        _chat_body("function pendingLabel"),
        json.dumps(cases),
    )
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"the label probe failed:\n{result.stdout}\n{result.stderr}"
    )
    return json.loads(result.stdout)


def test_a_message_the_session_may_never_have_read_is_not_called_sent():
    """The reported failure, at the layer it was actually in (issue 194).

    An operator typed into this pane and nothing arrived in the session, for a
    string of messages, and the pane called every one of them sent — so the app
    reported a working conversation while the run heard nothing. The control plane
    was not the one being coy about it: ``POST /input`` answers
    ``submit_confirmed: false`` with a note whenever it typed an Enter it cannot
    observe landing, which is every message typed at a TUI, and this view threw the
    reply away.

    A CR is not a submit it can see: a dialog on screen consumes the keystrokes and
    answers *itself* with the Enter, and a re-render can swallow it — both
    reproduced against a real claude TUI while this was diagnosed. So past the grace
    window the caption may not say the word "sent", and it has to name the one place
    an operator can see which happened.
    """
    said = _labels()

    # The word on its own, because "unsent" is exactly what this caption is allowed
    # — and required — to say.
    assert not re.search(r"\bsent\b", said["unconfirmed"]), (
        f"an unconfirmed submit is still reported as sent — {said['unconfirmed']!r}"
    )
    assert "terminal" in said["unconfirmed"], (
        "the caption does not say where the message can be seen and submitted — "
        f"{said['unconfirmed']!r}"
    )
    assert said["silent_reply"] == said["unconfirmed"], (
        "a reply that said nothing about the submit is treated as a confirmation; "
        "the fallback has to be the quiet answer"
    )


def test_the_two_honest_captions_are_still_there():
    """The change is one rung, not a rewrite: a send in flight still says so, and a
    submit that IS a fact still gets the sentence about the transcript being behind.

    Both matter for the same reason the third rung does — a message on its way must
    not read as a failure. The grace window is what separates them, and it is the
    only thing in this component a clock decides.
    """
    said = _labels()

    assert said["still_going"] == "sending…", (
        f"a send inside the grace window says {said['still_going']!r}"
    )
    assert said["confirmed"] == "sent — the transcript has not caught up yet", (
        f"a confirmed submit lost its caption — {said['confirmed']!r}"
    )


# --- dependencies -------------------------------------------------------------

def test_the_markdown_packages_are_declared_and_locked():
    """The two packages this feature adds, named so the addition stays reviewed.

    ``markdown-it`` for the parsing, chosen over the alternatives because raw HTML
    off is its documented default rather than something reconstructed from
    renderer overrides; ``dompurify`` for the second layer. Both widen the
    allowlist in ``test_platform_web_app.test_dependency_set_is_minimal``, which
    has to be updated to match — this test is the statement of intent, that one is
    the gate.
    """
    payload = json.loads(_read(WEB / "package.json"))
    for name in ("markdown-it", "dompurify"):
        assert name in payload["dependencies"], f"{name} is not declared"
    lock = json.loads(_read(WEB / "package-lock.json"))
    for name in ("markdown-it", "dompurify"):
        entry = lock["packages"].get(f"node_modules/{name}")
        assert entry and entry.get("resolved"), f"{name} is not locked"
