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
- and, since T121, what makes a message you sent stop being *pending* — and, since
  #254, what may not. The transcript having the message is the only thing that
  settles a bubble; nothing removes one for want of a confirmation. The bug that
  bought that rule: a message typed at a session that was still working is queued
  and unwritten for as long as the current turn runs, and the old arrival backstop
  let a *stranger's* turn plus any later reply drop its bubble — so the operator's
  words left the screen having never appeared in the history they were sent into.
  Both halves are executed rather than read — the transcript copy of a message is
  not the bytes that were sent, and which of them still match is not a question
  source text answers. One of those copies is the platform's own doing and is the
  reason the settle rule knows about the ``!`` defusal: a message the supervisor
  gave a ``.`` to (#254) comes back with the prefix on it, and a bubble that cannot
  recognise its own defused turn is a message shown twice for the rest of the run.

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
#: it reads are the plain boxes ``ref()`` hands it, the turn memory beside them is
#: the component's own ``Set``, and the JS half only reports which bubbles it left
#: standing. Every assertion is in Python.
#:
#: The bubbles are reported whole rather than as their texts, because two identical
#: messages are what the pairing rule is *about* and a list of texts cannot say which
#: of the two survived. ``since`` and ``at`` are what tell them apart — neither is
#: read by the rule (``at`` is the label's, and the clock test below pins that), so
#: carrying a distinct one on each bubble tags it without changing what it is.
_SETTLE_PROBE = """
const messages = { value: [] }
const pending = { value: [] }
const consumed = new Set()

%s

%s

const cases = %s
const seen = {}
for (const [name, probe] of Object.entries(cases)) {
  messages.value = probe.messages
  pending.value = probe.pending
  // Each case is a view of its own, so it starts with no memory of the last one's
  // turns — what `start()` does with the same three boxes.
  consumed.clear()
  settlePending()
  seen[name] = pending.value.map(
    (item) => ({ text: item.text, since: item.since, at: item.at }),
  )
}
console.log(JSON.stringify(seen))
"""

#: The same rule run over a transcript that *grows*, which is the shape a poll makes:
#: ``absorb`` appends a page and calls ``settlePending`` on what it has so far. One
#: bubble, many passes, and the JS half reports what was left standing after each —
#: which is the only way to ask whether a bubble survives the run rather than the
#: call (#254).
_CYCLES_PROBE = """
const messages = { value: [] }
const pending = { value: [] }
const consumed = new Set()

%s

%s

const probe = %s
pending.value = probe.pending
const kept = []
for (const page of probe.pages) {
  messages.value = [...messages.value, ...page]
  settlePending()
  kept.push(pending.value.map((item) => item.text))
}
console.log(JSON.stringify(kept))
"""

#: The same growing transcript, with the bubbles reported whole. Two identical
#: messages are what the pairing rule is *about*, and which of the pair is still up
#: cannot be read off a list of texts — ``at`` is what tells them apart, and the rule
#: never reads it (the clock test below pins that). An empty page is a pass like any
#: other here, because a poll that finds nothing still absorbs and still settles.
_PAIR_CYCLES_PROBE = """
const messages = { value: [] }
const pending = { value: [] }
const consumed = new Set()

%s

%s

const probe = %s
pending.value = probe.pending
const kept = []
for (const page of probe.pages) {
  messages.value = [...messages.value, ...page]
  settlePending()
  kept.push(pending.value.map((item) => ({ text: item.text, at: item.at })))
}
console.log(JSON.stringify(kept))
"""

#: The same rule across a *restart* — a respawn puts a different session behind the
#: same run, and ``start()`` empties the three boxes this holds. Mirrored here rather
#: than run, because ``start()`` is a fetch; the test beside the one using this pins
#: that the real function drops all three, so the mirror cannot drift from it.
_RESTART_PROBE = """
const messages = { value: [] }
const pending = { value: [] }
const consumed = new Set()

%s

%s

const probe = %s
const kept = []
for (const view of probe.views) {
  messages.value = []
  pending.value = []
  consumed.clear()
  messages.value = view.messages
  pending.value = view.pending
  settlePending()
  kept.push(pending.value.map((item) => item.text))
}
console.log(JSON.stringify(kept))
"""

#: ``send()`` itself, run against a stubbed control plane (issue 194). Everything
#: the function closes over is a plain box or a no-op — the point is narrow: what
#: the reply becomes on the item that is held. The refs are the shapes ``ref()``
#: hands it, and the JS half reports only the pending list it produced.
#:
#: The stub can move ``cursor`` while the POST is in flight
#: (``cursorAdvanceDuringSend``), because that is what really happens under
#: ``send()``: the poll keeps running during the round trip, and one that finds the
#: just-written turn advances the cursor past it (issue 237). The increment lives
#: inside ``sendSessionInput`` so it lands exactly where the race puts it — after
#: the send began, before its reply is handled.
_SEND_PROBE = """
const props = { sessionId: 'probe-session' }
const CURSOR_AT_SEND = %d
let cursor = CURSOR_AT_SEND
let reply = null
let cursorAdvanceDuringSend = 0
const draft = { value: '' }
const sending = { value: false }
const problem = { value: null }
const pending = { value: [] }
const following = { value: false }
const stale = () => false
const nextTick = () => {}
const stickToBottom = () => {}
const poll = () => {}
const sendSessionInput = async () => {
  cursor += cursorAdvanceDuringSend
  return reply
}
const generation = 0

%s

const cases = %s
const typed = %s
;(async () => {
  const held = {}
  for (const [name, probe] of Object.entries(cases)) {
    reply = probe.reply
    cursorAdvanceDuringSend = probe.cursorAdvanceDuringSend || 0
    cursor = CURSOR_AT_SEND
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
#: rungs, and the JS half only reports the caption it produced. There are two rungs
#: since #254 and the second one is empty, which is exactly why this stays executed:
#: "the function returns nothing here" is a value, and only running it shows it.
_LABEL_PROBE = """
const PENDING_GRACE_MS = %d
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


def _multi_kb_fenced_paste():
    """Issue #297's shape: large enough to span terminal reads, one code fence."""
    from lmer_platform import transcripts

    text = "```python\n" + "print('one submitted turn')\n" * 250 + "```"
    assert 4095 < len(text) < transcripts.TEXT_LIMIT
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
        "multi_kb_fenced_paste": {
            "messages": _conversation([
                working,
                _operator_turn(_multi_kb_fenced_paste()),
            ]),
            "pending": [
                {"text": _multi_kb_fenced_paste(), "at": 0, "since": 1}
            ],
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
        # The shape #254 was reported over, and the one the old arrival backstop
        # dropped: the turn past `since` is a *stranger's* message, this message is
        # nowhere in the transcript, and a reply follows that stranger's turn. All
        # the backstop asked for, and none of it about this message — which is the
        # ordinary state of a message queued at a session that was already working.
        "a_strangers_turn_and_a_reply": {
            "messages": _conversation([
                _agent_turn("Working on it.", _STAMPS[0]),
                _operator_turn("something entirely different", _STAMPS[1]),
                _agent_turn("Looked at that instead.", _STAMPS[2]),
            ]),
            "pending": [{"text": _QUOTING_MARKUP, "at": 0, "since": 1}],
        },
        # Two identical sends inside one poll interval: the cursor never moved
        # between them, so they share a `since` and the seq guard cannot separate
        # them at all. One recorded turn, so exactly one of them may clear — and the
        # two `at`s are how the assertion can say *which*, since the rule never reads
        # one (it is the label's field) and the texts are identical by construction.
        "two_identical_sends_one_interval": {
            "messages": _conversation([
                _agent_turn("Ready.", _STAMPS[0]),
                _operator_turn("yes", _STAMPS[1]),
            ]),
            "pending": [
                {"text": "yes", "at": 0, "since": 1},
                {"text": "yes", "at": 1000, "since": 1},
            ],
        },
        # The same pair once a reply follows that turn. Still one recorded turn, so
        # the second "yes" is still in flight and still on the screen: a reply is not
        # a second message. Before #254 this pair both settled here, on evidence that
        # was about neither of them.
        "two_identical_sends_then_a_reply": {
            "messages": _conversation([
                _agent_turn("Ready.", _STAMPS[0]),
                _operator_turn("yes", _STAMPS[1]),
                _agent_turn("Pushed.", _STAMPS[2]),
            ]),
            "pending": [
                {"text": "yes", "at": 0, "since": 1},
                {"text": "yes", "at": 1000, "since": 1},
            ],
        },
        # And the same pair once the harness has written *both* down, which is what
        # actually clears them: two identical messages resolve to two turns, in the
        # order they were sent, or one of them is a message the operator sent that
        # the view never shows again.
        "two_identical_sends_two_turns": {
            "messages": _conversation([
                _agent_turn("Ready.", _STAMPS[0]),
                _operator_turn("yes", _STAMPS[1]),
                _operator_turn("yes", _STAMPS[2]),
                _agent_turn("Pushed.", _STAMPS[3]),
            ]),
            "pending": [
                {"text": "yes", "at": 0, "since": 1},
                {"text": "yes", "at": 1000, "since": 1},
            ],
        },
        # Two different messages sent in one interval, the second one answered first
        # — the ask channel merges an answer in by its own clock, so the transcript's
        # order is not always the send order. Each bubble takes its own words.
        "two_sends_answered_out_of_order": {
            "messages": _conversation([
                _agent_turn("Ready.", _STAMPS[0]),
                _operator_turn("go ahead", _STAMPS[1]),
            ]),
            "pending": [
                {"text": "yes", "at": 0, "since": 1},
                {"text": "go ahead", "at": 1000, "since": 1},
            ],
        },
        # The message the platform defused on the way in (#254): on claude the
        # supervisor gives the first column to a `.` before typing, so the turn the
        # harness records is not the string the composer sent. The whitespace layer
        # cannot bridge this one — a `.` is not whitespace and does not collapse —
        # so without the defused form the bubble outlives its own delivery and the
        # message stays on the screen twice for the rest of the run.
        "defused_on_the_way_in": {
            "messages": _conversation([
                working, _operator_turn(_AS_THE_SESSION_RECORDS_IT),
            ]),
            "pending": [
                {"text": _A_MESSAGE_THAT_STARTS_LIKE_A_COMMAND, "at": 0, "since": 1},
            ],
        },
        # The same recorded turn beside a *different* `!` message: the second form is
        # the defused rendering of this item's own words and of nothing else, or one
        # message's bubble comes down on another message's turn.
        "another_messages_defused_turn": {
            "messages": _conversation([
                working, _operator_turn(_AS_THE_SESSION_RECORDS_IT),
            ]),
            "pending": [{"text": "!207 was merged", "at": 0, "since": 1}],
        },
        # And a dotted turn beside a message that was never a command. ". yes" is not
        # the defused rendering of "yes" — nothing would have defused it, because the
        # transform only ever fires on a leading `!` — so this is a stranger's turn
        # that happens to start with a dot, and it settles nothing.
        "a_dotted_turn_that_defused_nothing": {
            "messages": _conversation([working, _operator_turn(". yes")]),
            "pending": [{"text": "yes", "at": 0, "since": 1}],
        },
    }


def _run_probe(script, what):
    """One Node run of *script*, with its parsed output. *what* names it on failure."""
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")

    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"the {what} probe failed:\n{result.stdout}\n{result.stderr}"
    )
    return json.loads(result.stdout)


def _bubbles():
    """Run the settle rule over every case. Case name → the bubbles left, whole.

    Whole because the pairing rule is about messages that are identical *as text*:
    which of two "yes" bubbles is still up is the assertion, and only the fields the
    fixtures tag them with can answer it.
    """
    return _run_probe(_SETTLE_PROBE % (
        _chat_body("function comparable"),
        _chat_body("function settlePending"),
        json.dumps(_cases()),
    ), "settle")


def _settled():
    """The same run, as the texts left standing — what most cases are about."""
    return {
        name: [item["text"] for item in items]
        for name, items in _bubbles().items()
    }


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


def test_a_message_the_transcript_rewrote_keeps_its_bubble_and_this_is_the_cost():
    """The side of #254's trade that is paid rather than collected, pinned so it is
    a checked fact and not a line in a comment.

    All three are cases no text rule can settle — the words really are different by
    the time they come back — and the arrival backstop is what used to clear them
    (T121: a sent message stuck at the tail of the conversation for the rest of the
    run, while the transcript above it held the same message as an ordinary turn and
    the agent had plainly acted on it). It is gone, so these bubbles stay up beside
    the transcript's own rewritten copy of themselves, for the rest of the run.

    That is the operator's call in #254 and the direction of it is the whole point:
    the same message shown twice, rather than a message shown nowhere. What bought it
    is the case below — the backstop cleared these three by accepting evidence about
    *any* message, which is the same rule that dropped a queued message's bubble
    before the harness had written it down. #238 is where this cost actually ends, on
    the server, by making a message correlatable instead of guessable.
    """
    settled = _settled()

    for case, sent in (
        ("quoted_markup", _QUOTING_MARKUP),
        ("masked_credential", _NAMING_A_TOKEN),
        ("kept_by_its_tail", _long_paste()),
    ):
        assert settled[case] == [sent], (
            f"{case}: the bubble was dropped for a transcript turn that does not "
            "hold this message's words — if a new settle path was added, it decides "
            "on something other than the text and #254's decision is what it has to "
            f"argue with — {settled[case]}"
        )


def test_the_text_match_forgives_whitespace_and_nothing_else():
    """The one layer left, and it settles a bubble on the words.

    A turn the harness recorded in pieces comes back joined with a blank line
    between them, so the transcript's copy and the sent string differ by one
    newline. Collapsing whitespace on both sides matches them — and cannot do more
    than that: two messages whose words differ stay two messages, and a message
    that is a *prefix* of a longer turn is not that turn.

    Both halves matter more since #254 than they did when they were written: with
    the arrival backstop gone, a bubble this rule cannot match is a bubble nothing
    else will clear, and a bubble it matches too loosely is a message the operator
    sent that the view stops showing.
    """
    settled = _settled()

    assert settled["recorded_in_pieces"] == [], (
        "a turn the harness recorded in two blocks never matches what was typed, "
        f"so the bubble outlives it — {settled['recorded_in_pieces']}"
    )
    assert settled["multi_kb_fenced_paste"] == [], (
        "the one multi-kilobyte fenced turn did not settle its pending bubble"
    )
    assert settled["different_words"] == ["rebase on main"], (
        "a different message settled this bubble, so the match now forgives words"
    )
    assert settled["a_prefix_of_a_longer_turn"] == ["yes"], (
        "a longer turn that merely starts with what was sent settled the bubble"
    )


def test_a_message_the_platform_defused_settles_on_its_own_defused_turn():
    """The one rewriting this end can undo exactly, and the regression the dot
    introduced (#254).

    A chat message that starts with `!` is typed at a claude session with a `.` in
    front of it, or Claude Code runs the sentence as a shell command. The earlier
    version of that defusal was a leading *space*, which the whitespace collapse
    forgave by accident: the recorded copy matched its bubble and this component
    never had to know the mechanic existed. A `.` does not collapse, so the same
    rule would leave every defused message's bubble up for the rest of the run, next
    to the transcript's dotted copy of it — the same message twice, on the ordinary
    path rather than the rewritten-copy one the trade above accepted.

    So a pending item that starts with `!` accepts its own defused rendering as its
    turn, and the two guards are what keep that from being a loophole: the second
    form belongs to one item's words, and it is offered only by an item the transform
    would actually have fired on. A dotted turn is otherwise a stranger's turn, and
    the visible prefix stays in the transcript either way — this decides what settles
    a bubble, not what is shown.
    """
    from lmer_cli.supervisor import _sanitize_user_chat

    # The other end of the pair, because two processes have to agree on one string:
    # the client reconstructs what the supervisor typed, and neither file can see the
    # other. A prefix changed on one side alone is a bubble that never settles again.
    assert _sanitize_user_chat(_A_MESSAGE_THAT_STARTS_LIKE_A_COMMAND, "claude") == (
        _AS_THE_SESSION_RECORDS_IT
    ), "the supervisor defuses a message into something this view does not expect"

    settled = _settled()

    assert settled["defused_on_the_way_in"] == [], (
        "the bubble survived the transcript holding this very message as the session "
        "recorded it, so every message an operator opens with `!` is shown twice for "
        f"the rest of the run — {settled['defused_on_the_way_in']}"
    )
    assert settled["another_messages_defused_turn"] == ["!207 was merged"], (
        "a defused turn settled a different message's bubble, so the second form is "
        "matched as any dotted turn rather than as this item's own words"
    )
    assert settled["a_dotted_turn_that_defused_nothing"] == ["yes"], (
        "a turn that merely starts with a dot settled a message the transform would "
        "never have touched, which is a stranger's turn taking a bubble down"
    )


def test_a_bubble_stays_up_until_the_transcript_has_the_message():
    """The ways a bubble must not go, and all of them are the same failure: the
    operator's words gone from the view with nothing holding them.

    A session that was working when the message went keeps streaming the turn it
    was already on and queues the message — Claude Code records such a turn with a
    prompt source of its own — so agent turns later than the send prove nothing
    about whether this message has been read. And an operator turn with no reply
    after it is not evidence either: the ask channel's answers are merged into this
    timeline by their own clock (T67), not by the queue a typed message is in.

    The seq guard rides along here: the identical answer *earlier* in the
    conversation is the case it was written for, and the match may not reach back
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


def test_two_identical_messages_need_two_turns_and_take_them_in_send_order():
    """Two identical sends inside one poll interval, which is the pair the whole
    pairing rule exists for (review of !202, and #254's half of it).

    They share a cursor, so they share a `since`, and the seq guard — written for
    exactly the double-"yes" pair — cannot tell them apart at all. Before the turn
    allocation one recorded turn matched *both* bubbles by text, so the operator's
    second message vanished while it was still in flight even with nothing else in
    the transcript.

    Two things are pinned, and the second one is what #254 changed. A turn is taken
    by one bubble, and it is the *oldest* unmatched one that takes it, which is
    checked on the bubble left standing rather than on their count: `pending` is in
    send order and `filter` walks it in that order, so the pair resolves onto the
    transcript in the order it was typed. And the second bubble is then held until
    its own turn lands — a reply is not a second message, where before #254 the
    arrival backstop cleared it on the first bubble's turn plus any reply, which was
    evidence about neither of them. Both identical messages resolve, to two turns.
    """
    bubbles = _bubbles()

    left = bubbles["two_identical_sends_one_interval"]
    assert [item["text"] for item in left] == ["yes"], (
        "the text match answered for a different number than one bubble — "
        f"{left}; both gone means one turn matched two messages by text, none "
        "means the first no longer settles against its own turn"
    )
    assert left[0]["at"] == 1000, (
        "the turn was taken by the message sent second, so a pair of identical "
        "sends resolves onto the transcript in the wrong order — the first one "
        "is the one that was recorded"
    )

    held = [item["at"] for item in bubbles["two_identical_sends_then_a_reply"]]
    assert held == [1000], (
        "a reply after the first bubble's turn cleared the second bubble as well, "
        f"which is the #254 drop arriving through the double send — {held}"
    )

    assert bubbles["two_identical_sends_two_turns"] == [], (
        "two identical messages did not resolve to two recorded turns, so one of "
        "them is a message the operator sent that the view holds forever — "
        f"{bubbles['two_identical_sends_two_turns']}"
    )

    assert [item["at"] for item in bubbles["two_sends_answered_out_of_order"]] == [0], (
        "the bubble whose words are in the transcript is not the one that settled, "
        "so the pairing follows position rather than text"
    )


def test_a_strangers_turn_no_longer_takes_a_message_off_the_screen():
    """#254, at the exact rule it was reported against.

    This assertion used to read ``== []``, and that encoded the bug: the arrival
    backstop asked two questions about the transcript, neither of them about text,
    so any operator turn past the send's cursor with any later reply cleared this
    bubble — and a settled item is dropped, never re-added. The fixture is a
    stranger's message and a stranger's reply, and it is not a rare shape: a
    message typed at a working session is queued and unwritten while that session
    finishes what it was doing, which is precisely when somebody else's turn and a
    reply to it land. The operator's message then left the screen having never
    appeared in the history it was sent into — messages "in between turns" going
    missing, as #254 puts it.

    The decision is the operator's and it is not a tuning: a send this pane accepted
    is assumed delivered, and only the transcript holding the message takes the
    bubble down. So the bubble stays, and the cost of that is asserted above — a
    message whose recorded copy was rewritten is now shown twice.
    """
    settled = _settled()

    assert settled["a_strangers_turn_and_a_reply"] == [_QUOTING_MARKUP], (
        "a stranger's turn settled the bubble, so a message queued at a working "
        "session is dropped from the conversation before the harness writes it "
        "down — that is #254, and the arrival backstop is where it lived"
    )
    settle = _chat_function("function settlePending")
    assert "assistant" not in settle, (
        "the settle rule reads the agent's turns again; the only thing it may ask "
        "is whether the transcript holds this message's words"
    )


#: A message typed at a session that was already working: it sits in the harness's
#: queue while the current turn finishes, and nothing about it is in the transcript
#: until then. Plain words on purpose — the normaliser leaves them alone, so when the
#: turn finally arrives it really does match, and the last pass below can show that
#: this probe would have noticed a settle.
_QUEUED_MID_TURN = "check the runner mount before you restart it"


def _cycles():
    """One bubble, and the run going on around it poll by poll.

    The pages are slices of one conversation rather than pages built apart, because
    ``seq`` is the index of a turn in the whole run — sliced, the numbering is the
    one a real sequence of polls hands the view.
    """
    def stamp(index):
        return f"2026-07-29T10:{index:02d}:00.000Z"

    conversation = _conversation([
        _agent_turn("Working on it.", stamp(0)),
        # Somebody else's message, and a reply to it: everything the old arrival
        # backstop asked for, and none of it about the queued message.
        _operator_turn("something entirely different", stamp(1)),
        _agent_turn("Looked at that instead.", stamp(2)),
        # The session keeps working, which is why the message is still queued.
        _agent_turn("Still reading the log.", stamp(3)),
        _agent_turn("Ran the tests.", stamp(4)),
        # A second stranger's turn, answered too.
        _operator_turn("and check the disk while you are there", stamp(5)),
        _agent_turn("Disk is fine.", stamp(6)),
        # And finally the harness writes down what it had queued.
        _operator_turn(_QUEUED_MID_TURN, stamp(7)),
        _agent_turn("Mount looks wrong, fixing it.", stamp(8)),
    ])
    return {
        "pending": [{"text": _QUEUED_MID_TURN, "at": 0, "since": 1}],
        "pages": [
            conversation[:1], conversation[1:3], conversation[3:5],
            conversation[5:7], conversation[7:],
        ],
    }


def test_a_bubble_nothing_confirms_survives_every_poll_until_its_turn_arrives():
    """The failure #254 describes, followed through the polls that produce it.

    One call of the settle rule is not what an operator experiences: a run polls
    every few seconds for as long as it lives, and each poll asks again. Under the
    arrival backstop the second page here was already fatal — a stranger's turn and a
    reply to it — and the message was gone from the conversation four polls before
    the harness got round to writing it down. There is no timer either: passes go by
    with nothing about this message in the transcript, and the bubble is still there,
    because "nothing has confirmed it" is not a reason to remove somebody's words.

    The last page is what keeps this honest: the harness records the queued message,
    and the bubble goes on the pass that sees it. A probe that could not settle
    anything would pass every assertion above it.
    """
    kept = _run_probe(_CYCLES_PROBE % (
        _chat_body("function comparable"),
        _chat_body("function settlePending"),
        json.dumps(_cycles()),
    ), "poll cycles")

    assert kept[:-1] == [[_QUEUED_MID_TURN]] * (len(kept) - 1), (
        "the bubble went before the transcript had the message, and the pass it "
        f"went on says what took it — {kept}"
    )
    assert kept[-1] == [], (
        "the message is in the transcript and the bubble is still up, so this "
        "probe cannot tell a bubble that is held from one nothing can settle"
    )


def _identical_sends_across_polls():
    """The double "yes", with its two turns landing on two different polls.

    Which is the ordinary shape of it: the harness writes a queued message down when
    it gets to it, so two messages sent inside one interval are rarely recorded
    inside one. The empty page in the middle is the pass that matters — a poll that
    brings nothing back still absorbs and still settles, and the first message's turn
    is the only operator turn the transcript holds while it goes by.

    Slices of one conversation, like ``_cycles``: ``seq`` is the index of a turn in
    the whole run, so the numbering has to be the one a real sequence of polls sees.
    """
    conversation = _conversation([
        _agent_turn("Ready.", _STAMPS[0]),
        _operator_turn("yes", _STAMPS[1]),
        _operator_turn("yes", _STAMPS[2]),
    ])
    return {
        "pending": [
            {"text": "yes", "at": 0, "since": 1},
            {"text": "yes", "at": 1000, "since": 1},
        ],
        "pages": [conversation[:2], [], conversation[2:]],
    }


def test_the_turn_that_settled_one_bubble_cannot_settle_the_next_one_a_poll_later():
    """The pairing rule asked across polls instead of within one call.

    Its own comment promises that two identical messages need two recorded turns —
    but the memory of which turn an earlier bubble took used to be built fresh on
    every call, and a settled bubble is dropped, so nothing was left to say the turn
    had been taken. `settlePending` runs on every absorb, empty page included, so the
    next pass found the same recorded "yes" unclaimed and cleared the second bubble
    against it: the operator's second message off the screen while it was still in
    flight, a few seconds later than the bug the rule was written for and by the same
    route. Single-pass fixtures cannot see it — the pass that drops the bubble is the
    one that has nothing new to report.

    Both directions are here. The middle pass is the poll that brings back nothing,
    and the bubble must survive it; the last one holds the second message's own turn,
    and the bubble must go on it — a rule that simply never settled twice would pass
    the middle assertion and fail this one.
    """
    kept = _run_probe(_PAIR_CYCLES_PROBE % (
        _chat_body("function comparable"),
        _chat_body("function settlePending"),
        json.dumps(_identical_sends_across_polls()),
    ), "identical sends across polls")

    assert [item["at"] for item in kept[0]] == [1000], (
        "the page holding the first message's turn did not resolve onto the first "
        f"bubble — {kept[0]}; both gone means one turn matched two messages by "
        "text, neither means the match no longer settles at all"
    )
    assert [item["at"] for item in kept[1]] == [1000], (
        "a poll that brought nothing back settled the second bubble against the "
        "first message's turn, so the second message left the screen without ever "
        f"being written down — {kept[1]}"
    )
    assert kept[2] == [], (
        "the second message's own turn arrived and its bubble is still up, so the "
        f"turn memory outlives the turns it is meant to be about — {kept[2]}"
    )


def _restart_views():
    """One session view's conversation, then the next one's, numbered from scratch.

    A respawn puts a different session behind the same run and the server renumbers
    what it serves from the beginning, so the same ``seq`` names a different turn on
    either side of the restart — which is what makes a remembered one dangerous.
    """
    return {
        "views": [
            {
                "messages": _conversation([
                    _agent_turn("Ready.", _STAMPS[0]),
                    _operator_turn("yes", _STAMPS[1]),
                ]),
                "pending": [{"text": "yes", "at": 0, "since": 1}],
            },
            {
                "messages": _conversation([
                    _agent_turn("Ready again.", _STAMPS[0]),
                    _operator_turn("yes", _STAMPS[1]),
                ]),
                "pending": [{"text": "yes", "at": 0, "since": 1}],
            },
        ],
    }


def test_a_new_session_view_remembers_none_of_the_last_ones_turns():
    """The other end of the turn memory: it is the view's, not the run's.

    The numbers it holds mean something only against the list they were read off. A
    respawn hands this component a different session, the server numbers that
    conversation from the beginning, and a seq carried across would say a turn had
    already been taken when the turn wearing that number is one this view has never
    seen — the new session's first "yes" held for the rest of the run, which is the
    same lost message from the opposite direction.

    Two halves, because the probe mirrors the reset rather than running ``start()``
    (which is a fetch): the reset really is what ``start()`` does, and doing it lets
    the second view's bubble settle against its own turn.
    """
    start = _chat_body("async function start")
    for dropped in ("messages.value = []", "pending.value = []", "cursor = 0",
                    "consumed.clear()"):
        assert dropped in start, (
            f"a restart keeps `{dropped}` from the session it is leaving, so this "
            "view opens the next conversation holding the last one's state"
        )

    kept = _run_probe(_RESTART_PROBE % (
        _chat_body("function comparable"),
        _chat_body("function settlePending"),
        json.dumps(_restart_views()),
    ), "restart")

    assert kept == [[], []], (
        "a bubble in the second view was held against a turn the first view "
        f"consumed, so the memory outlived the numbering it was read from — {kept}"
    )


def test_nothing_about_the_bubble_is_decided_by_a_clock():
    """Why a timeout was rejected, kept as a property of the code.

    A bubble dropped on a timer is dropped while the message is genuinely on its
    way, which is the one thing this view must never do — a send that succeeded may
    not read as a send that went nowhere. So the settle rule may consult the
    transcript and nothing else; the clock in this component belongs to the
    *label*, which changes its wording without touching what is held.

    #254 removed the other way out of a bubble and left this one exactly where it
    was: with the arrival backstop gone, a grace window that expired into a drop
    would be the same rejected timeout wearing the caption's clothes.
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


# --- what it says while it waits, and what it stops saying (issue 194, #254) --

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

#: What every ``/input`` carrying an Enter answers today: the CR was written, and
#: whether the TUI took it as a submit is not observable from outside (#194). Shared
#: by two cases below — the plain one and the raced one — so the race case differs
#: from its baseline in the cursor movement alone.
_UNCONFIRMED_REPLY = {
    "session": "probe-session", "bytes_written": 61,
    "submit_confirmed": False, "note": "…terminal view.",
}


def _held(typed=_TYPED_AT_A_SESSION):
    """Run ``send()`` against three replies. Case name → the pending items."""
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")

    cases = {
        "unconfirmed": {"reply": _UNCONFIRMED_REPLY},
        # A control plane that can confirm one. Nothing does today; the case is kept
        # so what the item records stays tied to what the route answered rather than
        # to the route's current behaviour.
        "confirmed": {"reply": {
            "session": "probe-session", "bytes_written": 61,
            "submit_confirmed": True,
        }},
        # A reply that says nothing about a submit — an older daemon, or a shape
        # that changes. Silence is not a confirmation.
        "silent": {"reply": {"session": "probe-session", "bytes_written": 61}},
        # A poll that won the race: while the POST was in flight, the harness wrote
        # the turn down and a poll absorbed it, so the cursor the reply comes back
        # to is already past this very message (issue 237).
        "poll_raced_the_reply": {
            "reply": _UNCONFIRMED_REPLY, "cursorAdvanceDuringSend": 3,
        },
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
    to be on the item, and it has to be the verdict that was sent. Executed rather
    than read, because "the function assigns a field" is what a source check would
    confirm, and the thing that matters is which value ends up there — an unconfirmed
    reply must not produce a confirmed item.

    No caption reads the field since #254 — a send this pane accepted is assumed
    delivered, and the rung that hedged about it is gone. It is still carried, and
    still checked here, because this reply is the only place the view is told: a fact
    already in hand costs nothing to keep, and re-deriving it later would cost a round
    trip. Dropping it again would be the #194 bug's first move.
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
        "a control plane that could confirm a submit is recorded as though it had "
        "not, so the one fact this reply carries is lost on the way to the item"
    )


def test_what_was_typed_is_what_is_held_and_sent():
    """The bubble holds the message, newline and all.

    Both halves of the issue meet here: the composer sends a multi-line body (the
    TUI accepts one whole — that half was established against a real claude), and
    the bubble that stands in for it until the transcript catches up has to be the
    same text, or the matching rule that settles it later cannot work.

    `since` is checked here only for the cases whose cursor never moves, where "the
    transcript's end" and "the cursor as of the send" are the same number and this
    says nothing about which one was read. The raced case is where they differ, so
    the property belongs to the test named for it below rather than to this loop —
    and its failure message has to be that one, not this one.
    """
    held = _held()

    for case, items in held.items():
        assert len(items) == 1, f"{case}: one send, {len(items)} bubbles"
        assert items[0]["text"] == _TYPED_AT_A_SESSION, (
            f"{case}: the bubble holds {items[0]['text']!r}"
        )
        if case == "poll_raced_the_reply":
            continue
        assert items[0]["since"] == _CURSOR_AT_SEND, (
            f"{case}: `since` is {items[0]['since']}, not the transcript's end, so "
            "the settle rule would look for the message in the wrong place"
        )


def test_a_poll_that_wins_the_race_cannot_move_the_bubbles_since():
    """The reported failure (issue 237): a message the session plainly answered
    kept the "not confirmed" caption for the rest of the run.

    The send and the poll overlap. The supervisor takes over a second to type the
    text, wait out the drain and write the CR — and the harness records the turn
    the moment the TUI takes it — so a poll can absorb this very message and move
    the cursor past it before the POST reply is back. A `since` read *after* the
    await then points beyond the turn it exists to find, and the match may not look
    behind it (the double-"yes" guard is that bound), so the bubble can never
    settle: it hardened into the old warning caption for a message the terminal
    showed delivered and answered, and since #254 removed the other way out of a
    bubble it would now sit there for the rest of the run, showing the operator
    their own message twice.

    So `since` has to be the cursor as of *starting* the send, and this runs the
    real `send()` against a stub that advances the cursor mid-flight — the race
    won by the poll every time — and reads the held item. Executed rather than
    read, because "reads the cursor before the await" is an ordering fact only an
    execution can pin.
    """
    held = _held()

    item = held["poll_raced_the_reply"][0]
    assert item["since"] == _CURSOR_AT_SEND, (
        f"`since` is {item['since']}, the cursor as of the reply, not the send — "
        "a poll that absorbs the message during the round trip leaves the bubble "
        "looking for it beyond where it is, and it never settles"
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


#: The composer's send, and then the api.js call it made, run for real against a
#: scripted ``fetch`` — the whole way from the box the operator typed in to the
#: body on the wire. Two hops rather than one because the fact being carried is
#: split across them: the component knows a human typed this, the client turns
#: that into a field, and a test of either half alone passes while the other end
#: drops it. The JS half only reports; every assertion is in Python.
_COMPOSER_WIRE_PROBE = """
const props = { sessionId: 'probe-session' }
let cursor = 0
const draft = { value: %s }
const sending = { value: false }
const problem = { value: null }
const pending = { value: [] }
const following = { value: false }
const stale = () => false
const nextTick = () => {}
const stickToBottom = () => {}
const poll = () => {}
const generation = 0
const reply = { session: 'probe-session', bytes_written: 16 }
const asked = []
const sendSessionInput = async (sessionId, data, options) => {
  asked.push({ sessionId, data, options: options ?? null })
  return reply
}

%s

const calls = []
globalThis.fetch = async (path, options) => {
  calls.push({ path, method: options.method, body: options.body })
  return { ok: true, status: 200, json: async () => reply }
}
const api = await import(%s)

await send()
const composed = asked[asked.length - 1]
await api.sendSessionInput(
  composed.sessionId, composed.data, composed.options ?? undefined,
)
const flagged = calls[calls.length - 1]
await api.sendSessionInput(composed.sessionId, composed.data)
const unflagged = calls[calls.length - 1]
console.log(JSON.stringify({ composed, flagged, unflagged }))
"""


def _wire(typed=_TYPED_AT_A_SESSION):
    """Run the composer and the api client, and report what reached ``fetch``."""
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")

    script = _COMPOSER_WIRE_PROBE % (
        json.dumps(typed),
        _chat_body("async function send"),
        json.dumps(str(WEB / "src" / "api.js")),
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(WEB), capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"the composer-to-wire probe failed:\n{result.stdout}\n{result.stderr}"
    )
    return json.loads(result.stdout)


#: A message about a merge request, and the one the operator reported (#254): typed
#: into the chat pane it reached Claude Code's input box with `!` in the first
#: column, which is that TUI's bash escape, and the sentence ran as a shell command
#: in their session. It goes through as words — the defusing is the supervisor's,
#: because it is the end that knows which harness is reading.
_A_MESSAGE_THAT_STARTS_LIKE_A_COMMAND = '!206 was merged'

#: The same message as the session records it. The supervisor gives the first column
#: to a `.` and types `". !206 was merged"`, and the prefix is part of the turn the
#: harness writes down — which is what the settle rule above has to recognise as this
#: message's own turn. Derived from the sent message so the pair cannot drift, with
#: the prefix spelled out: read off `_sanitize_user_chat` instead, this would agree
#: with whatever that function did and could not fail. The other end of the drift —
#: that the supervisor really produces this string — is asserted in the settle test.
_AS_THE_SESSION_RECORDS_IT = f'. {_A_MESSAGE_THAT_STARTS_LIKE_A_COMMAND}'


def test_a_message_from_the_composer_is_marked_as_typed_by_a_person():
    """Executed, both hops. The chat pane is the only place that knows a human
    wrote these words in a box, and the supervisor cannot infer it: the same route
    carries the terminal's keystrokes, where a leading `!` is an escape somebody
    means. So the composer says so, and the flag has to survive the client — a
    component that sets an option api.js drops puts nothing on the wire, and
    neither half fails on its own.

    The message is not touched here, which is the other half of the design: this
    end asserts a fact, the far end decides what to do about it.
    """
    seen = _wire(_A_MESSAGE_THAT_STARTS_LIKE_A_COMMAND)

    assert seen["composed"]["options"] == {"sanitize": True}, (
        "the composer is not marking its message as one a person typed: "
        f"{seen['composed']['options']}"
    )
    assert seen["composed"]["data"] == _A_MESSAGE_THAT_STARTS_LIKE_A_COMMAND, (
        "the message was edited in the browser — the words that go are the "
        "operator's, and what a TUI would make of them is decided in the session"
    )
    body = json.loads(seen["flagged"]["body"])
    assert body["sanitize"] is True, f"the flag never reached the wire: {body}"
    assert body["data"] == _A_MESSAGE_THAT_STARTS_LIKE_A_COMMAND
    assert body["append_newline"] is True, "a chat message is typed AND submitted"


def test_multi_kilobyte_fenced_paste_reaches_the_wire_as_one_message():
    payload = _multi_kb_fenced_paste()
    seen = _wire(payload)

    assert seen["composed"]["data"] == payload
    body = json.loads(seen["flagged"]["body"])
    assert body["data"] == payload
    assert body["append_newline"] is True


def test_a_send_nobody_flagged_puts_nothing_new_on_the_wire():
    """Every other caller of this client — the terminal's keystrokes, anything
    typing on the operator's behalf — sends the body it always has. Absent rather
    than `false`, so a session running an older image sees a request it already
    understands."""
    seen = _wire(_A_MESSAGE_THAT_STARTS_LIKE_A_COMMAND)

    body = json.loads(seen["unflagged"]["body"])
    assert body == {
        "data": _A_MESSAGE_THAT_STARTS_LIKE_A_COMMAND, "append_newline": True,
    }, f"an unflagged send grew a field: {body}"


def _grace():
    """The grace window, read from the component so the cases are its real rungs."""
    return int(re.search(r"const PENDING_GRACE_MS = (\d+)", _read(CHAT)).group(1))


def _labels():
    """Run the label ladder over its rungs. Case name → the caption."""
    grace = _grace()
    cases = {
        # Inside the grace window, whatever the control plane said: a transcript
        # that has not caught up in a few seconds is the ordinary case, and "did
        # that go through?" is a question worth answering while it is fresh.
        "still_going": {"now": 1000, "item": {"at": 0, "submitConfirmed": False}},
        # A moment past it, and the submit was never confirmed — which is what
        # every chat send is today, because a CR typed at a TUI is not an
        # observable submit (#194).
        "unconfirmed": {"now": grace + 1000, "item": {"at": 0, "submitConfirmed": False}},
        # Past it, with the submit a fact. Nothing answers this way today; the case
        # is kept so the caption is checked to be the same one either way, rather
        # than tracking a field that has stopped deciding anything.
        "confirmed": {"now": grace + 1000, "item": {"at": 0, "submitConfirmed": True}},
        # A reply that said nothing about the submit.
        "silent_reply": {"now": grace + 1000, "item": {"at": 0}},
        # Hours later, on a bubble the transcript never matched: a message the run
        # took and rewrote on the way back (see the settle cases above) sits here
        # for the rest of the run, and this is what it looks like by then.
        "long_past_grace": {
            "now": grace * 100, "item": {"at": 0, "submitConfirmed": False},
        },
    }
    return _run_probe(_LABEL_PROBE % (
        grace,
        _chat_body("function pendingLabel"),
        json.dumps(cases),
    ), "label")


def test_a_message_past_the_grace_window_is_captioned_like_any_other_you_sent():
    """What #254 decided the bubble is, said in captions.

    The rung that used to be here warned, past the grace window, that the session
    might never have read the message and that it could be sitting unsent in an
    input box. Every word of that is possible and it printed on *every* message this
    pane sends — the supervisor types a submit CR at a TUI and cannot observe it
    landing (#194), so nothing a chat send does is ever confirmed — which made it a
    warning about the ordinary case, printed beside messages the session had plainly
    answered. The operator's call: assume the message was delivered, say nothing, and
    leave the rare real case to the terminal view, which is where it is visible
    either way.

    So there is no wording left to check for honesty; what is checked is that there
    is none. A caption is what makes a bubble look provisional, and a bubble nothing
    is ever going to confirm must not keep one — including the one that has been up
    for hours because the transcript's copy of it came back rewritten.
    """
    said = _labels()

    for case in ("unconfirmed", "silent_reply", "confirmed", "long_past_grace"):
        assert said[case] == "", (
            f"{case}: a bubble past the grace window is still captioned "
            f"{said[case]!r}, so the view is hedging about a message #254 decided "
            "is delivered"
        )
    assert "SUBMIT_UNCONFIRMED_LABEL" not in _read(CHAT), (
        "the unconfirmed caption is back as a constant; the wording is what #254 "
        "removed, not just the rung that reached it"
    )


def test_the_only_caption_left_is_the_one_for_a_send_in_flight():
    """The rung that stayed, and it is the honest half of the old ladder.

    A message really is on its way for the seconds between the POST and the harness
    writing the turn down, and saying so answers the question an operator asks in
    exactly that window. It is also the only thing in this component a clock decides
    — what is *held* is the transcript's business (`settlePending`), and the window
    expiring changes a caption and nothing else.
    """
    said = _labels()

    assert said["still_going"] == "sending…", (
        f"a send inside the grace window says {said['still_going']!r}"
    )
    assert set(said.values()) == {"sending…", ""}, (
        f"the label grew a rung: {sorted(set(said.values()))}. Two are all there "
        "are — a send in flight, and a message like any other"
    )
    assert "v-if=\"pendingLabel(item)\"" in _read(CHAT), (
        "the header renders the caption unconditionally, so a bubble past the "
        "grace window shows a separator with nothing after it"
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
