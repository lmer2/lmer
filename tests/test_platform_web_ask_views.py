"""Guards on the two views of a session's ask channel (issue #141, T40).

The channel used to be one component in one place: a list that only grew, in the
lmer tab's third pane. Two things were wrong with that at once. A question a
running session is *blocked on* was behind a remembered tab — the operator's
choice since T49, so it could perfectly well be the terminal — and everything the
channel had ever carried was on screen forever, pushing the tabs and the panes
down the page on the device this app is for.

So there are two views now, because there are two questions:

- ``AskChannel.vue`` is docked above the tabs and answers "is anything waiting on
  me". It shows the questions a live session is blocked on, and it is
  **clearable**: what has been dealt with can be dismissed from it;
- ``AskHistory.vue`` fills the operator-chat pane and answers "what was said". It
  is the whole record — notes, answers, questions the session closed, questions
  nobody ever answered — and it is read-only.

The property that makes the pair honest is that **clearing is a view operation**.
It dismisses an entry from the dock and touches nothing else: no request, no
delete, and the record below is untouched. Lose that and the history tab is a
lie, which is the one failure here that nothing on screen would report.

The cleared state lives for as long as the page does, and neither less nor more —
two decisions, pinned separately here because they are argued from different
things.

Neither less: it is held in ``dismissals.js``, a module-level store keyed by
session, and not in the component. The dock is rebuilt every time a run is left and
re-entered, so component-local ids came back on the next visit — the operator
reported exactly that ("when i cleared operator notes with 'clear from here' they
come back once i leave and re-enter the session").

And neither more: it is still *not persisted*. What "cleared" means is "not now": a
question a live session is still blocked on is never clearable at all, and a
dismissal that outlived the page would hide a stranded container days later.
Persisting it would need a per-run set of question ids in browser storage — which
``preferences.js`` could not validate against anything (its whole contract is that a
remembered value is checked against what exists *now*), which would grow without
bound, and which nobody could find to undo. The terminal's height preset is the
precedent for *not* doing it: what earns storage there is a property of the
operator's screen, unchanged from run to run.

What the two views *disagree* about is the whole of the above. What one entry looks
like they owe each other, and issue #254 is why that is pinned here too: the
operator read the old list as one thing ("clearer separation of messages is needed …
messages look like the kinda flow into each other"), so an entry is a card of its
own now, with the two halves of the exchange named where each of them starts. Both
views render that same shape, and the labels take their colour from ``format.js``
for the reason ``askEntryLabel`` already lives there — a channel that spoke about
the operator's own words differently in the dock and in the record would be two
accounts of who said what.

They render it by *composing the same component* (``AskEntry.vue``) since issue
#274, and that is a change to what the guards below can be. The card, both labels
and five CSS rules were copied into each view, each carrying a comment saying the
other must not disagree with it — a rule kept by hand, which is how every drift
starts: the next edit lands in one of the two. So the guards on what an entry *is*
read that one component, and the guards on which entries a view shows read the
view; the two are tied together by asserting that each view still hands its entries
over rather than drawing them again.

Where each view is mounted, and that the record is not answerable, are pinned
next door in :mod:`tests.test_platform_web_details_tabs`; the channel's own
protocol is :mod:`tests.test_platform_ask`. Source-level, like every web test in
this repo — there is no JS runner here (see :mod:`tests.test_platform_web_app`).

How the dock reads on a phone, and whether a cleared channel feels cleared, are
verified by building the bundle and by live test LT3 on a real phone.
"""

import re
from pathlib import Path

from tests.test_platform_web_app import ALLOWED_STORAGE_KEYS

WEB = Path(__file__).resolve().parent.parent / "web"
COMPONENTS = WEB / "src" / "components"
DOCK = COMPONENTS / "AskChannel.vue"
RECORD = COMPONENTS / "AskHistory.vue"
#: What one entry looks like, for both of them (#274). The card, the two labels and
#: the stylesheet were copied into each view, with a comment in each saying the
#: other must not disagree — which is a rule kept by hand. Both views compose this
#: now, so the guards below on what an entry *is* read one file and the guards on
#: which entries a view shows read that view.
ENTRY = COMPONENTS / "AskEntry.vue"
RUN_DETAIL = COMPONENTS / "RunDetail.vue"
#: Where the cleared ids live: outside the component, so a remount cannot lose them,
#: and in memory, so a reload cannot keep them.
STORE = WEB / "src" / "dismissals.js"
#: The words and the colours both views label an entry with, in one place.
FORMAT = WEB / "src" / "format.js"
#: The two schemes those colours have to be defined in, or a label renders unpainted.
THEME = WEB / "src" / "main.js"


def _read(path):
    return path.read_text(encoding="utf-8")


def _without_comments(text):
    """*text* with its markup and line comments removed.

    Both files explain themselves at length and talk about the things they must
    not do — "no request", "never deleted" — so a token in prose reaches nothing
    and every assertion about behaviour runs over this.
    """
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)


def _function_body(text, signature):
    """Source of one top-level function in a ``<script setup>`` block.

    Same helper as :mod:`tests.test_platform_web_details_tabs`: every function in
    these components is top-level, so a ``}`` in column zero ends one.
    """
    start = text.index(signature)
    return text[start:text.index("\n}\n", start)]


def _binding(text, name):
    """Source of one top-level ``const`` binding, up to whatever follows it."""
    rest = text[text.index(f"const {name} = "):]
    match = re.search(r"\n(?=(?:const|function|//|/\*|onMounted|watch)\s?)", rest)
    return rest[:match.start()] if match else rest


def _entry_element(text):
    """The opening tag of the element one channel entry is rendered as.

    Read off the ``v-for`` rather than looked for by name, because the question
    here is what a view does per entry — which since #274 is "hands it to the one
    component that knows what an entry looks like".
    """
    match = re.search(
        r'<[\w-]+\b[^>]*\sv-for="entry in \w+"[^>]*>', _without_comments(text), re.S,
    )
    assert match, "nothing here renders one element per channel entry"
    return " ".join(match.group(0).split())


def _entry_card(text):
    """The opening tag of the card an entry *is*, off the entry component's root.

    Its root rather than anything found by name: a comment or a wrapper above it
    would make this component a fragment, which is a different component from the
    one that ships, and the class and the variant asserted on it would then be
    reaching nothing.
    """
    template = text[text.index("\n<template>\n"):]
    match = re.match(r"\n<template>\s*(<[\w-]+\b[^>]*>)", template, re.S)
    assert match, "the entry component's template does not open with an element"
    return " ".join(match.group(1).split())


def _composes_the_entry(text):
    """Whether a view draws its entries with the shared component."""
    return "<AskEntry" in _without_comments(text) and (
        "from './AskEntry.vue'" in text
    )


def _label_match(text, word):
    """The element that writes *word* on an entry, as a match on the stripped view."""
    match = re.search(rf"<div\b[^>]*>{word}</div>", _without_comments(text), re.S)
    assert match, f"no {word} label is written on an entry"
    return match


def _label_element(text, word):
    """The element that writes *word* on an entry, whole and whitespace-collapsed."""
    return " ".join(_label_match(text, word).group(0).split())


# --- two views, and which is which -------------------------------------------

def test_the_dock_and_the_record_are_two_components():
    """One would have to be two modes, and the modes differ on the load-bearing bit.

    The dock is allowed to stop showing an entry; the record is not. A single
    component with a ``historic`` prop would put those two rules in one file, one
    ``v-if`` apart, and the failure — a filter that reached the record — renders
    perfectly and looks like a channel nobody used.
    """
    assert DOCK.is_file() and RECORD.is_file()
    dock, record = _without_comments(_read(DOCK)), _without_comments(_read(RECORD))
    assert "AskHistory" not in dock, "the dock renders the record inside itself"
    assert "AskChannel" not in record, "the record renders the dock inside itself"
    # Each says which question it answers, in its own words, at the top.
    assert "docked" in _read(DOCK)
    assert "record" in _read(RECORD)


def test_the_record_answers_nothing():
    """Replying happens where the question that needs an answer is, and once.

    A second box down here would be a second way to write the same file — out of
    sight of the alert that says whether the session is even alive, which is the
    thing that decides whether a reply can be delivered at all.
    """
    record = _without_comments(_read(RECORD))
    for answering in ("<AskBox", "answerSessionQuestion", "v-textarea", "v-btn"):
        assert answering not in record, (
            f"the channel record offers {answering}, so there are two places to "
            "answer one question"
        )
    assert "emit" not in record, "the record tells the fleet something changed"


def test_the_record_holds_every_entry_the_channel_has():
    """Including the ones the dock stopped showing — that is the whole point.

    Answered, closed by the session, never answered, and cleared: a record that
    filtered any of those would leave an operator who cleared the dock with no way
    to read back what they cleared, which is the failure that makes clearing
    unsafe rather than the clearing itself.
    """
    record = _without_comments(_read(RECORD))
    assert re.search(r'v-for="entry in entries"', record), (
        "the record renders something other than the channel's own entries"
    )
    # What it renders is the whole list, so nothing may sit between the fetch and
    # the loop. (`!entry.answered` is a different thing and stays: it decides
    # whether the closure sentence is shown *under* an entry, not whether the
    # entry is.)
    for filtering in ("dismissed", ".filter(", "v-for=\"entry in "):
        assert record.count(filtering) == (1 if filtering.startswith("v-for") else 0), (
            f"the record drops entries ({filtering}); it is the one view that must "
            "show all of them"
        )
    # Each entry keeps what makes it a record rather than a summary. Drawn by
    # AskEntry.vue since #274, so what an entry carries is read there — and the
    # record is checked to be handing every entry to it, which is what makes that
    # the same statement as before.
    assert _composes_the_entry(record), (
        "the record draws its entries itself again, so what one carries is not "
        "what the dock shows for the same entry"
    )
    entry = _without_comments(_read(ENTRY))
    assert "entry.at" in entry, "the record carries no timestamps"
    assert "askEntryLabel(entry, live)" in entry, "an entry does not say what it is"
    assert 'live="live"' in record, "the record's entries are labelled for no session"
    assert "entry.answer.text" in entry, "your reply is not in the record"


# --- clearing is a view operation --------------------------------------------

def test_clearing_dismisses_from_the_dock_and_nothing_else():
    """The property the second view rests on.

    A clear that deleted, or that told the daemon anything at all, would make the
    record incomplete — and nothing on screen would say so. So the whole of it is
    a list of ids in this component: no request, no route, no verb.
    """
    dock = _read(DOCK)
    clear = _function_body(dock, "function clear()")
    assert "dismissed.value" in clear, "clearing does not dismiss anything"
    for reaching_out in ("fetch(", "api", "await", "delete", "DELETE", "emit("):
        assert reaching_out not in clear, (
            f"clearing reaches for {reaching_out}; it is a view operation, and the "
            "channel record must survive it untouched"
        )
    code = _without_comments(dock)
    for verb in ("'DELETE'", '"DELETE"', "deleteEntry", "removeEntry"):
        assert verb not in code, f"the dock can destroy a channel entry ({verb})"

    # And the operator is told where what they cleared went, because a view that
    # can hide a record owes them the way back to it.
    assert "cleared from here" in dock, (
        "clearing says nothing about what happened to the entries"
    )
    assert "operator chat" in dock, (
        "nothing points at the pane that still has them, so a cleared channel "
        "reads as a deleted one"
    )


def test_a_question_a_live_session_is_blocked_on_cannot_be_cleared():
    """The one entry the dock exists for, so it is the one clearing may not touch.

    A container sitting in a poll loop is a person waiting; dismissing that from
    the only view that shows it — while the session keeps waiting — is the exact
    failure this whole channel exists to prevent.
    """
    dock = _read(DOCK)
    waiting = _binding(dock, "waiting")
    assert "props.live ? open.value : []" in waiting, (
        "what is waiting on an answer is no longer gated on the session being alive"
    )
    recent = _binding(dock, "recent")
    assert "!waiting.value.includes(entry)" in recent, (
        "an open question a live session is blocked on falls into the clearable "
        "list, so it can be dismissed while the session keeps waiting"
    )
    clear = _function_body(dock, "function clear()")
    assert "recent.value" in clear and "waiting" not in clear, (
        "clearing reaches past what has been dealt with"
    )


def test_the_dismissals_belong_to_the_session_they_were_made_against():
    """The ids are one channel's, and another channel's are not the same entries.

    Switching runs rebuilds this component, but switching *sessions* under it does
    not have to — and carrying a dismissal across would hide an entry nobody has
    seen, under an id that means something else.

    Which is what the store being *keyed* buys, and it is the same read that makes a
    remount keep what was cleared: the dock asks for this session's ids rather than
    resetting to none, so a change of session lands on that session's own set.
    """
    dock = _read(DOCK)
    start = _function_body(dock, "function start()")
    assert "dismissed.value = clearedIds(props.sessionId)" in start, (
        "the dock does not re-read the cleared ids for the session it is starting "
        "on, so they either survive a change of session — hiding an entry in a "
        "channel they were never made against — or are dropped on every restart"
    )
    assert re.search(r"watch\(\(\) => props\.sessionId, start\)", dock), (
        "nothing restarts the dock when the session changes"
    )
    store = _read(STORE)
    assert "new Map()" in store, (
        "the store is not keyed at all, so every session shares one set of ids"
    )
    for signature in ("export function clearedIds(sessionId)",
                      "export function rememberCleared(sessionId, ids)"):
        assert signature in store, (
            f"{STORE.name} no longer takes the session as its key ({signature})"
        )


def test_clearing_survives_leaving_and_re_entering_the_session():
    """The operator's report: "they come back once i leave and re-enter".

    Entering a run mounts this component and leaving it unmounts it, so a ref inside
    it is a dismissal that lasts as long as the tab you cleared it in — which reads
    as a button that does not work. The ids therefore live in a module, where the
    remount finds them again, and the component starts from that store rather than
    from an empty list (the initial value matters: ``start()`` runs after the first
    render, so a dock initialised empty would flash the cleared entries back).
    """
    assert STORE.is_file(), "there is nowhere for a dismissal to outlive a remount"
    dock = _read(DOCK)

    assert "from '../dismissals.js'" in dock, (
        "the dock holds the cleared ids itself again, so they die with the view"
    )
    assert "const dismissed = ref(clearedIds(props.sessionId))" in dock, (
        "the dock starts from an empty list rather than from what was cleared, so "
        "re-entering a run shows the cleared entries again"
    )
    clear = _function_body(dock, "function clear()")
    assert "rememberCleared(" in clear, (
        "clearing writes nowhere outside the component, so leaving the run undoes it"
    )
    # The store is the only thing that outlives the view, and it holds ids and
    # nothing else: an entry kept here would be a second copy of a channel that the
    # record is supposed to be the only one of.
    store = _without_comments(_read(STORE))
    for holding in ("text", "answer", "entry"):
        assert holding not in store, (
            f"{STORE.name} keeps a {holding}; what survives a remount is a list of "
            "ids, and the channel itself is re-read from the daemon"
        )


def test_the_cleared_state_is_not_persisted_anywhere():
    """Decided, and the decision is "not everything deserves storage".

    A stored dismissal is a *standing* one: an unanswerable question hidden on
    every future visit, with the preference nowhere the operator can see it. It is
    also a set of ids, which is precisely the shape ``preferences.js`` refuses to
    hold — that contract is "validate a remembered value against what exists now",
    and there is nothing to validate a growing per-run id list against.

    The store the ids moved into is memory, which is what keeps that true: it holds
    them across a remount and loses them on a reload, so the way back from a clear
    an operator regrets is the one thing every browser has.
    """
    # On the code, not the prose: the dock's header argues at length about the
    # storage it deliberately does not use, and a comment stores nothing.
    for path in (DOCK, RECORD, STORE):
        text = _without_comments(_read(path))
        assert "localStorage" not in text, (
            f"{path.name} stores something in the browser; a dismissal is for this "
            "visit, and a stored one hides a blocked session forever"
        )
        assert "sessionStorage" not in text, (
            f"{path.name} stores the dismissals in sessionStorage, which survives a "
            "reload of the tab — the same standing dismissal by another name"
        )
        assert "preferences.js" not in text, f"{path.name} remembers a choice"
        assert "STORAGE_KEY" not in text, f"{path.name} declares a storage key"
    # And no key was quietly added to the allowlist for one either.
    assert not [key for key in ALLOWED_STORAGE_KEYS if "ASK" in key or "CHANNEL" in key], (
        f"a channel preference is in ALLOWED_STORAGE_KEYS: {sorted(ALLOWED_STORAGE_KEYS)}"
    )

    # Module scope and nothing further out: a store hung off `window` is reachable
    # from anything on the page, and one on the app instance would be handed around
    # as a dependency nobody asked for.
    store = _without_comments(_read(STORE))
    assert re.search(r"^const cleared = new Map\(\)$", store, re.M), (
        "the cleared ids are not a plain module-level Map, which is the whole of "
        "what makes them last exactly as long as the page"
    )
    for reaching_out in ("window.", "globalThis", "document."):
        assert reaching_out not in store, (
            f"{STORE.name} hangs the cleared ids off {reaching_out}"
        )


# --- what the dock still owes the operator ------------------------------------

def test_the_dock_renders_nothing_when_there_is_nothing_to_show():
    """Almost every run, and it sits above the tabs — so the cost is the tab bar
    moving down the page on a device that has no room for it."""
    dock = _read(DOCK)
    assert '<div v-if="!empty">' in dock, "the dock is drawn whether or not it has"
    empty = _binding(dock, "empty")
    for part in ("waiting.value.length", "recent.value.length", "problem.value"):
        assert part in empty, f"an empty dock is decided without {part}"


def test_the_dock_still_says_a_dead_sessions_question_cannot_be_answered():
    """T23's refusal, kept through the split — and it goes with the questions.

    The sentence stands in for the reply box, so it belongs to the entries it is
    about: left standing after they are cleared it is a warning about a session
    that ended hours ago, which is the noise the dock is being cleared of.
    """
    dock = _read(DOCK)
    stranded = _binding(dock, "stranded")
    assert "!props.live" in stranded, "the sentence is not gated on the session"
    assert "recent.value.some(" in stranded, (
        "the sentence outlives the questions it explains"
    )
    assert "nothing is reading the channel any more" in dock


def test_both_views_poll_only_while_the_session_is_live():
    """Once it has exited nothing can arrive, so a poll is a request per five
    seconds that can only return what is already on screen."""
    for path in (DOCK, RECORD):
        text = _read(path)
        start = _function_body(text, "function start()")
        assert "if (props.live) timer = setInterval(load, POLL_MS)" in start, (
            f"{path.name} polls a channel nothing can post to"
        )
        assert "clearInterval(timer)" in _read(path), f"{path.name} leaks its timer"


def test_the_run_detail_view_mounts_the_dock_above_the_tabs_and_the_record_below():
    """The whole point of there being two, in the one file that places them.

    The dock is in the region a remembered tab cannot hide (T49), and the record is
    behind the tab the operator chooses to open. Reversed, this slice would have
    changed nothing at all.
    """
    detail = _read(RUN_DETAIL)
    bar = detail.index('<v-tabs v-model="tab"')
    # Matched as whole element names: a substring search calls `<AskChannelSomething`
    # the dock, and this test is the one that says where the dock is.
    dock_at = re.search(r"<AskChannel\b", detail)
    record_at = re.search(r"<AskHistory\b", detail)
    assert dock_at and dock_at.start() < bar, (
        "the dock is behind a tab, so a remembered terminal hides a session that "
        "is blocked on a question"
    )
    assert record_at and record_at.start() > bar, "the record is not in a tab"
    # Keyed on the session the channel belongs to, and told whether it is alive:
    # both views take those two, and a dock with neither is a dock for no run.
    dock = detail[detail.index("<AskChannel"):]
    dock = dock[:dock.index("/>") + 2]
    for prop in (':session-id="terminalSession"', ':live="!!run.live"'):
        assert prop in dock, f"the dock is mounted without {prop}"


# --- what one entry looks like, in both views (issue #254) ---------------------

def test_one_component_draws_an_entry_for_both_views():
    """Issue #274, and the reason every guard under this heading reads one file.

    A copy is not a shared decision. Both views carried the entry card, both label
    elements and the same five rules verbatim — each with a comment saying the other
    must not disagree — and by the time that was extracted the copies had already
    drifted in four places. Composition cannot drift.

    What is *not* here is a prop naming the view: the handful of things one view
    says about an entry and the other does not are separate props, each with its
    own argument, because "this is the record" is not a rendering decision and a
    component that took one would be two presentations one ``v-if`` apart again.
    """
    assert ENTRY.is_file(), "there is nowhere for the two views to agree"
    entry = _without_comments(_read(ENTRY))

    # Presentation and nothing else. A fetch here would be a third reader of the
    # channel; an emit would be a way for the record to send something.
    for reaching_out in ("fetchSessionAsk", "defineEmits", "setInterval", "localStorage"):
        assert reaching_out not in entry, (
            f"the entry component {reaching_out}s; what it does is draw an entry, "
            "and everything else belongs to the view that owns the channel"
        )
    assert not re.search(r"\bmode\b|\bview:|historic", entry), (
        "the entry component takes the view it is drawn in as a value, so the two "
        "presentations are back, one v-if apart"
    )

    for path in (DOCK, RECORD):
        text = _read(path)
        assert _composes_the_entry(text), (
            f"{path.name} does not draw its entries with {ENTRY.name}, so the two "
            "views can disagree about what a message looks like again"
        )
        # And nothing of the entry is left behind to drift beside it.
        stripped = _without_comments(text)
        for copied in (">QUESTION</div>", ">ANSWER</div>", "askEntryLabel",
                       ".entry {", ".part {", ".said {", ".said.plain {", ".reply {"):
            assert copied not in stripped, (
                f"{path.name} still carries {copied!r} of its own, which is the "
                "copy issue #274 removed"
            )


def test_an_entry_is_a_card_of_its_own_in_both_views():
    """The operator: "messages look like the kinda flow into each other".

    They were divs in one card, separated by a bottom margin — so a note followed
    by a question read as one long thing with a dim line somewhere in the middle of
    it. Each entry is its own card now, which is the operator's own suggestion ("a
    v-card for the message itself, or a v-divider could help"), and both views get
    it: they render the same entries, and a dock that separated them where the
    record did not would be one channel read two ways.

    Which is now a thing they cannot do differently rather than a thing they agree
    about (#274): each view hands an entry to AskEntry.vue, and the card is that
    component's root.
    """
    element = _entry_card(_read(ENTRY))
    assert element.startswith("<v-card"), (
        f"an entry is drawn as {element.split()[0]}>, so the separation between "
        "two messages is whatever margin it carries"
    )
    assert 'variant="outlined"' in element, (
        "the entry card is elevated or tonal: the pane's own card is already the "
        "shadow, and a second one — or a wash under every entry — repaints the "
        "pane rather than separating what is in it"
    )
    for path in (DOCK, RECORD):
        entry_element = _entry_element(_read(path))
        assert entry_element.startswith("<AskEntry"), (
            f"{path.name} renders an entry as {entry_element.split()[0]}> of its "
            "own, so the two views can disagree about what a message looks like "
            "again"
        )
        # Inside the pane's card, not instead of it: the entries are one list, and
        # the dock hangs its "clear from here" off the same container.
        text = _without_comments(_read(path))
        assert "<v-card" in text and text.index("<v-card") < text.index("<AskEntry"), (
            f"{path.name} lost the card around the list; the entries are a stack of "
            "loose cards on the page"
        )


def test_both_views_name_the_question_and_the_answer_where_each_starts():
    """The operator: "clearly have a 'QUESTION' and 'ANSWER' label".

    Where each half starts, rather than in the header row: the header says what
    happened to an entry ("answered", "closed by the session") and how long ago,
    which is a different question from which of two voices the next paragraph is.

    Both views, in one file since #274: the labels are written by the component
    each of them draws an entry with, so the "both" is what is asserted of the
    views and the placement is asserted of the component.
    """
    for path in (DOCK, RECORD):
        assert _composes_the_entry(_read(path)), (
            f"{path.name} draws a channel entry itself again, so the labels below "
            "are one view's and the other's are unpinned"
        )

    text = _read(ENTRY)
    stripped = _without_comments(text)
    question, answer = _label_match(text, "QUESTION"), _label_match(text, "ANSWER")

    # A note is one voice saying one thing: labelling it QUESTION would promise an
    # answer that is never coming.
    assert "v-if=\"entry.kind !== 'note'\"" in question.group(0), (
        "QUESTION is written on every entry, notes included"
    )
    assert question.start() < stripped.index(':text="entry.text"'), (
        "the question is labelled somewhere other than where it starts"
    )

    # And the answer's label goes with the answer, which most entries do not have —
    # an unanswered question with an ANSWER heading over nothing reads as a reply
    # that was lost.
    answered = stripped.index('v-if="entry.answer"')
    assert answered < answer.start() < stripped.index("entry.answer.text"), (
        "ANSWER is written outside the answer it labels"
    )


def test_the_two_labels_are_painted_from_one_map_the_theme_defines():
    """Same words in the same ink in both views, and neither picks the ink.

    ``askEntryLabel``'s argument, one line down: two views of one channel that
    coloured the operator's own words differently would be two accounts of who said
    what. A colour named in a component is also one the scheme switcher cannot
    repaint — main.js has to define it, in both schemes.
    """
    fmt = _read(FORMAT)
    assert "export function askPartColor(part)" in fmt, (
        "the label colours are not shared, so each view picks its own"
    )
    block = re.search(r"const ASK_PART_COLORS = \{(.*?)\n\}", fmt, re.S)
    assert block, "format.js no longer maps a half of an exchange onto a colour"
    colours = dict(re.findall(r"(\w+): '([^']+)'", block.group(1)))
    assert set(colours) == {"question", "answer"}, (
        f"an exchange has two halves here and the map has {sorted(colours)}"
    )
    assert colours["question"] != colours["answer"], (
        "both labels are the same colour, which is the coding the operator asked "
        "for doing nothing"
    )
    theme = _read(THEME)
    for part, colour in colours.items():
        assert not colour.startswith("#"), (
            f"the {part} label is a literal {colour}, which the scheme switcher "
            "cannot repaint"
        )
        schemes = re.findall(rf"^\s+'?{re.escape(colour)}'?: '#", theme, re.M)
        assert len(schemes) == 2, (
            f"main.js defines {colour!r} in {len(schemes)} of its two schemes; the "
            f"{part} label renders as body ink in the other one"
        )

    entry = _without_comments(_read(ENTRY))
    assert "askPartColor" in entry and "from '../format.js'" in entry, (
        "an entry does not take the label colours from format.js"
    )
    for word, part in (("QUESTION", "question"), ("ANSWER", "answer")):
        label = _label_element(_read(ENTRY), word)
        assert f"askPartColor('{part}')" in label, (
            f"the {word} label is coloured by something other than the shared map"
        )
    # And neither view may name an ink for a label itself: they draw entries with
    # the component above, and a colour passed in or set beside it is the second
    # account of who said what that the map exists to prevent.
    for path in (DOCK, RECORD):
        text = _without_comments(_read(path))
        assert _composes_the_entry(_read(path)), (
            f"{path.name} draws a channel entry itself again"
        )
        assert "askPartColor" not in text, (
            f"{path.name} paints a label of its own beside the shared component"
        )


def test_the_colour_is_on_the_labels_and_never_on_what_was_said():
    """The operator, in the same breath: "i don't think color code the text
    entirely".

    A label is a word the eye lands on; the message under it is prose to read back,
    and painting it recolours a whole channel every time an agent posts. So the
    entry's two bodies — the rendered text and the verbatim reply — carry the
    emphasis classes and nothing that names a colour.
    """
    text = _without_comments(_read(ENTRY))
    said = re.findall(r"<(?:Markdown|p)\b[^>]*\bsaid\b[^>]*>", text, re.S)
    assert said, "an entry renders neither half of itself"
    for element in said:
        assert "askPartColor" not in element, (
            f"an entry colours what was said: {' '.join(element.split())}"
        )
        assert not re.search(r"text-(?:primary|success|warning|error|tone-)", element), (
            f"an entry colours what was said: {' '.join(element.split())}"
        )
    # Nothing is said in either view any more, which is what keeps the rule to one
    # place: a body drawn beside the shared component would be a half of an entry
    # nothing above has looked at.
    for path in (DOCK, RECORD):
        view = _without_comments(_read(path))
        assert not re.search(r'class="[^"]*\bsaid\b', view), (
            f"{path.name} draws half of an entry itself, outside the component "
            "this rule is kept in"
        )
