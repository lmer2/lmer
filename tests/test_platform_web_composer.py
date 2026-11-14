"""Guards on the three places an operator types something and sends it (T54).

Source-level, like every other web test here: there is no JS runner and no browser
in this image (see :mod:`tests.test_platform_web_app`), so what is pinned is the
markup that decides how the control behaves in a hand.

The three composers are the conversation (``Chat.vue``), the reply to a live
session's ask channel (``AskBox.vue``) and the answer to a run that stopped on a
question (``AnswerBox.vue``). They do three different things to three different
routes and they must *read* the same, because they sit a screen apart from each
other on the same run — an operator who learns to send in one has learned all
three.

Two other text inputs in this UI are deliberately not in that set. The terminal's
"type a line" field is a single line typed straight at a PTY, where a bare Enter is
the whole point — a prompt is waiting for it. The spawn form is a form: several
fields and one submit, where "send" is not a property of any one box.

Why the send control is no longer inside the field (issue 194)
--------------------------------------------------------------
It used to be, and this module used to pin that, on an argument worth restating
because it was wrong in one specific way rather than sloppy: an icon in a corner
the field already occupies costs no layout, while a row of its own is a band of
chrome that grows with an ``auto-grow`` box — so the icon could stay on every
device without being traded against anything.

What it costs is not layout, it is the tap. The field's whole surface focuses the
textarea and raises the keyboard, so a control inside it competes with the one
gesture an operator is trying *not* to make; the three composers sized theirs
``small``, which is 28px, the one size the app's own one-handed-use rule exempts
(``.v-btn:not(.v-btn--icon)`` in style.css); and the only other way to send was a
chord that a phone keyboard does not have a key for. This fleet is driven from a
phone. Reported live: messages typed into the assistant chat never reached the
session at all, and the operator drove the orchestrator from a host console
instead — *"newlines from the app aren't sending"*, which is what a return key that
inserts a newline feels like when nothing else leaves.

So the property is inverted, and it is the *same* property for the three: one
labelled control, on its own row, big enough to hit, in every build and at every
width.

What each test here pins, and what its loss looks like:

- the send control is **outside** the field, on a ``.send-row``, labelled rather
  than icon-only, and at least 44px tall — the smallest comfortable target the
  platform HIGs agree on, spelled ``size="large"`` in Vuetify's own sizes.
- it is **not** hidden or shrunk at any width or for any pointer. The terminal's
  key pad is hidden for a fine pointer, because there it is redundant — xterm takes
  the keystrokes itself. A composer has no second path in, and ``pointer: coarse``
  reads the *primary* pointer, so a touchscreen laptop would lose the button to a
  finger that is really there.
- every box routes Enter through the one shared rule in ``web/src/composerKeys.js``
  (issue 663) and binds no key of its own beside it. That rule sends on Enter,
  keeps Shift+Enter a newline, and sends on Ctrl/Cmd+Enter in every mode. Enter stays
  a newline when the platform's ``chat_enter_sends`` is off or the primary pointer
  is coarse, because on a phone the return key is the *only* way to type one. The
  behaviour itself is exercised per box in ``web/tests/ComposerKeys.test.js``. What
  is pinned here is that no box can step around the rule.
- the hint leads with the tap and then says what Enter does in the mode in force. The affordance that
  always works is the one an operator must not have to already know, which is the
  half of this that a button alone does not fix.
- the control keeps the disabled and loading states the buttons had. A send that
  spawns a container takes seconds, and the spinner is the only thing that says so.
- each control's *name* still carries the one fact its label did — that answering
  starts a session, and that replying does not — now on screen as well as in the
  accessibility tree, because a visible label is what a thumb reads before a tap
  that spends a container slot.

Two checks sit outside this module, and the difference between them matters. The
markup was driven in a real browser at 390x844 with touch — a two-line message
typed with the return key, the control tapped with no modifier, the POST body read
back whole (screenshots in the issue-194 run dir) — which establishes that the
control is there, is 44px, is full width, and sends. What it cannot establish is
reach with a **soft keyboard up**: a headless viewport raises none, and a grown
field plus the hint can push the row down the screen. That one is LT3 on a real
phone, and it is the check that would fail if this row needs to become sticky.
"""

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"
COMPONENTS = WEB / "src" / "components"

#: Each composer and the function its send control has to reach. Everything below
#: is parametrized over this: three views, one treatment, and a new one is meant to
#: arrive by being added here.
COMPOSERS = {
    "Chat.vue": "send",
    "AnswerBox.vue": "submit",
    "AskBox.vue": "submit",
}


def _read(name):
    return (COMPONENTS / name).read_text(encoding="utf-8")


#: The element the send control lives on, and the class that makes it a thumb
#: target. One name, shared by the three composers and defined once in style.css.
SEND_ROW = re.compile(r'<div class="[^"]*\bsend-row\b[^"]*">')


def _field(text):
    """The one ``<v-textarea>`` element in a composer, markup and all.

    Sliced rather than matched with a regex because these tests are about *where*
    things sit: a control that has slipped back inside the field, or a chord bound
    on the row instead of the box, still matches every pattern in the file.

    Self-closing since the control moved out, so the slice ends at whichever
    terminator the element actually has — a composer that grows a slot again is
    still measured, and it is :func:`test_the_send_control_is_outside_the_field`
    that objects rather than an IndexError here.
    """
    assert text.count("<v-textarea") == 1, (
        "a composer with two fields is not what these guards describe"
    )
    start = text.index("<v-textarea")
    closing = text.find("</v-textarea>", start)
    if closing != -1:
        return text[start:closing + len("</v-textarea>")]
    end = text.index("/>", start)
    return text[start:end + 2]


def _control(text):
    """The send row and the labelled control on it.

    The counterpart of :func:`_field`: what used to be sliced out of the field's
    ``append-inner`` slot is now sliced out of the row below it, so every assertion
    about the control is still made against the control and not against the file.
    """
    rows = list(SEND_ROW.finditer(text))
    assert len(rows) == 1, (
        f"a composer needs exactly one send row, found {len(rows)}; see this "
        "module's docstring for why the control is no longer in the field"
    )
    start = rows[0].start()
    control = text[start:text.index("</div>", start) + len("</div>")]
    # The slice ends at the FIRST closing div, so a wrapper element inside the row
    # would silently hand every assertion below a fragment. Made loud instead: if
    # the row grows a child element, this helper is what has to change.
    assert "<div" not in control[rows[0].end() - start:], (
        "the send row has a nested element in it, so this slice no longer holds "
        f"the whole control: {control!r}"
    )
    return control


def _hint(text):
    """The field's hint, whether a literal ``hint="…"`` or a ``:hint`` template."""
    match = re.search(r'(?<![:\w-])hint="([^"]+)"', text) or re.search(
        r':hint="`([^`]+)`"', text,
    )
    assert match, "the field lost its hint"
    return match.group(1)


KEYS = Path(__file__).resolve().parents[1] / "web" / "src" / "composerKeys.js"


def _style(text):
    return text[text.index("<style"):] if "<style" in text else ""


# --- where the control is ----------------------------------------------------

@pytest.mark.parametrize("name,handler", sorted(COMPOSERS.items()))
def test_the_send_control_is_outside_the_field(name, handler):
    """On its own row under the box, labelled, and big enough to hit.

    The reversal this module's docstring argues, stated as three facts about the
    markup: nothing sends from inside the field (the slot that competed with the
    tap that focuses it is gone), the one control that sends is on the send row,
    and it is one of Vuetify's ``large`` sizes rather than the ``small`` the
    one-handed-use rule in style.css exempts.
    """
    text = _read(name)
    field = _field(text)
    # Issue 575 put uber lmer's question badge in that corner, forwarded from the
    # pane as a slot; the corner may carry that slot and nothing of the composer's.
    inner = field[field.index("#append-inner"):] if "#append-inner" in field else ""
    assert handler not in inner and (
        not inner or re.search(r"#append-inner>\s*<slot name=\"composer-inner\" />\s*</template>", inner)
    ), (
        f"{name}'s send control is back inside the field, where the tap that "
        "reaches it is the tap that focuses the textarea"
    )
    control = _control(text)
    assert f'@click="{handler}"' in control, (
        f"{name} has a send row but the control that sends is elsewhere"
    )
    # Exactly one thing clicks send anywhere in the file, and it is the one just
    # found on the row — which is how "there is no second, smaller one" is stated
    # without naming a control that should not exist.
    assert text.count(f'@click="{handler}"') == 1, (
        f"{name} sends from more than one control"
    )
    assert 'size="large"' in control, (
        f"{name}'s send control is not sized for a thumb; large is 44px, which is "
        "the floor the HIGs agree on"
    )
    # A label a thumb can read, not only a name a screen reader can: the tag has
    # text between its ends rather than being an icon-only button.
    assert re.search(r">[^<>]*[a-z][^<>]*</v-btn>", control), (
        f"{name}'s send control is icon-only again, so what it does is guessed at"
    )


@pytest.mark.parametrize("name", sorted(COMPOSERS))
def test_the_only_send_control_is_not_hidden_or_shrunk_anywhere(name):
    """The one deliberate difference from the terminal's touch-only composer.

    There, a fine pointer means the key pad and the line field are *redundant* —
    xterm takes arrows, chords and pasted text directly, and clicking the terminal
    is what focuses it — so the block is dead chrome and is hidden as a unit. A
    composer has no such second path: the field is the only way in on every device,
    and the chord does not exist on a phone keyboard at all, so a hidden control
    leaves nothing on screen that can send a draft.

    ``pointer: coarse`` is also the *primary* pointer, so a laptop with a
    touchscreen reports ``fine``: gating the only send control on it takes the
    button away from a finger that is really there.
    """
    text = _read(name)
    style = _style(text)
    # The stylesheet, because that is where the terminal's gate lives and has to:
    # `d-flex` and friends are !important, so a media query can only beat them from
    # the scoped sheet that owns `display`.
    assert "@media" not in style and "pointer:" not in style, (
        f"{name}'s stylesheet gates something on the device; the composer is the "
        "one control that has to be reachable on all of them"
    )
    # The other way to hide it, and the one that needs no stylesheet at all:
    # Vuetify's responsive display helpers, which are a width question rather than
    # a pointer one — wrong twice over on a tablet in landscape.
    control = _control(text)
    assert not re.search(r'\bd-(?:none|sm-|md-|lg-|xl-)', control), (
        f"{name}'s send control is hidden at some viewport width"
    )


def test_the_send_row_is_a_thumb_target_at_every_width():
    """The row's own rule, and it lives in one place for the three composers.

    Two halves, and the second is the phone: a floor under the height at every
    width, and full width below Vuetify's ``sm`` breakpoint, where the widest
    possible target belongs at the bottom of the card because that is where a thumb
    already is. Pinned here rather than left to the components, because "never
    write the same file in two places" is exactly what put a 28px control in three
    of them.
    """
    css = (WEB / "src" / "style.css").read_text(encoding="utf-8")

    # The row is a flex container. Stated because everything else here is a flex
    # *property*: drop this one declaration and `justify-content` and the phone
    # rule's `flex-grow` both become inert, so the control quietly returns to
    # content width with every other assertion below still passing.
    row = re.search(r"\.send-row\s*\{([^}]*)\}", css)
    assert row, "the send row lost the rule that makes it a target"
    assert re.search(r"display:\s*flex", row.group(1)), (
        "the send row is not a flex container, so the widening below does nothing"
    )

    height = re.search(r"\.send-row\s*>\s*\.v-btn\s*\{[^}]*min-height:\s*(\d+)px", css)
    assert height, "the send control has no floor under its height"
    assert int(height.group(1)) >= 44, (
        f"the send control's floor is {height.group(1)}px, under the 44px the "
        "platform HIGs agree on"
    )

    phone = re.search(
        r"@media[^{]*max-width:\s*599[^{]*\{\s*\.send-row\s*>\s*\.v-btn\s*\{([^}]*)\}",
        css,
    )
    assert phone, "the send control is no longer full width on a phone"
    # It has to *grow*, which `flex: 0 0 auto` and `flex-shrink: 0` do not — both
    # would satisfy a check for the word "flex" while leaving a content-width
    # button on the screen this rule exists for.
    grow = re.search(r"flex:\s*([\d.]+)", phone.group(1))
    widened = (grow and float(grow.group(1)) > 0) or re.search(
        r"(?:flex-grow:\s*[1-9]|width:\s*100%)", phone.group(1),
    )
    assert widened, (
        f"the phone rule does not widen the control: {phone.group(1).strip()!r}"
    )

    # And nothing about the row may be gated on the pointer type: a touchscreen
    # laptop reports `fine`, so that gate takes the button away from a finger that
    # is really there. Checked on the *conditions* of every media block containing a
    # send-row rule — a slice from the first `.send-row` to EOF would sit inside
    # such a block and report it as absent.
    for condition in re.findall(r"@media([^{]*)\{[^}]*\.send-row", css):
        assert "pointer:" not in condition, (
            f"the send row is gated on the pointer type: @media{condition.strip()}"
        )


# --- how it is reached from a keyboard ---------------------------------------

@pytest.mark.parametrize("name,handler", sorted(COMPOSERS.items()))
def test_enter_goes_through_the_shared_rule(name, handler):
    """One rule for all three boxes (issue 663), and nothing beside it.

    An operator who learns what Enter does in one box has learned all three only
    if no box binds a key of its own. A stray ``@keydown.ctrl.enter`` left next to
    the shared handler would call the send path twice per chord (the box's busy
    guard drops the second, but AnswerBox then announces a refusal), and an
    ``@keydown.enter`` without it would skip the IME and touch-device checks.
    """
    text = _read(name)
    field = _field(text)
    bindings = re.findall(r'@keydown[\w.]*="([^"]+)"', field)
    assert bindings == [f"composerKeydown($event, {handler})"], (
        f"{name}'s field binds {bindings!r}; Enter must go through composerKeys.js "
        "and nothing else"
    )
    assert re.search(r"@keydown\.enter=", field), f"{name} binds the rule to the wrong key"
    assert "from '../composerKeys.js'" in text, f"{name} does not import the shared rule"
    assert "auto-grow" in field and "max-rows" in field, (
        f"{name}'s field no longer grows; multi-line messages are still the point"
    )


def test_the_shared_rule_keeps_the_cases_that_must_not_send():
    """Source-level backstop for ``web/tests/ComposerKeys.test.js``.

    The vitest suite drives every case through every box, but it only runs where
    Node does. These are the four facts whose loss would cost a message: an IME
    commit sent half-typed, Shift+Enter cutting a paragraph, a phone unable to
    type a newline, and the setting doing nothing.
    """
    # Matched against code shapes, not words: each name also appears in the
    # module's comments, which would satisfy a bare substring check.
    source = KEYS.read_text()
    assert re.search(r"event\.isComposing\s*\|\|\s*event\.keyCode\s*===\s*229\)\s*return", source), (
        "an IME commit would send"
    )
    assert re.search(r"!event\.shiftKey\s*&&", source), "Shift+Enter would send instead of breaking the line"
    assert re.search(r"&&\s*!coarsePointer\.value", source), "a phone would lose its only newline key"
    assert re.search(r"chatSettingsLoaded\.value\s*&&\s*chatEnterSends\.value", source), (
        "the platform setting would do nothing, or a slow fetch would send a draft"
    )
    assert re.search(r"event\.ctrlKey\s*\|\|\s*event\.metaKey", source), "the chord would stop sending"


@pytest.mark.parametrize("name", sorted(COMPOSERS))
def test_what_enter_does_is_written_down_where_it_is_used(name):
    """A key rule nobody is told about is one an operator finds out by losing a draft.

    The hint is persistent on all three fields, and it is the rule's own sentence,
    so it cannot go out of step with the mode in force. Both phrasings name what
    Enter does, and the newline one names the chord with Cmd for a Mac.
    """
    text = _read(name)
    assert "${enterHint}" in _hint(_field(text)), f"{name}'s hint does not say what Enter does"
    assert "persistent-hint" in text, (
        f"{name}'s hint is only shown on focus, which is not where it is needed"
    )
    source = KEYS.read_text()
    assert "Enter sends, Shift+Enter is a new line" in source
    assert "Enter is a new line, Ctrl+Enter (Cmd on a Mac) sends" in source


@pytest.mark.parametrize("name", sorted(COMPOSERS))
def test_the_hint_leads_with_the_way_in_that_always_works(name):
    """The other half of issue 194, and a button alone does not fix it.

    The operator's instruction: *"whatever you do about Enter, make the composer's
    behaviour discoverable rather than something you have to already know."* The
    hint opens with the tap, which works on every device, then says what Enter
    does.
    """
    hint = _hint(_field(_read(name)))
    assert hint.lower().startswith("tap"), (
        f"{name}'s hint opens with {hint.split('.')[0]!r} rather than with the "
        "affordance that works on every device"
    )
    assert hint.index("Tap") < hint.index("${enterHint}")


# --- what the control still has to be ----------------------------------------

@pytest.mark.parametrize("name", sorted(COMPOSERS))
def test_the_send_control_keeps_its_disabled_and_loading_states(name):
    """Both moved out with it; both are the only feedback these sends have.

    Loading, because a send is a round trip to the daemon and one of these three
    starts a *container* — seconds in which nothing else on the card changes.
    Disabled, because an empty draft is the common state of a composer and a
    control that looks armed when there is nothing to send is a control that has
    to be tried to be understood.
    """
    control = _control(_read(name))
    assert ":loading=" in control, (
        f"{name}'s send control no longer shows that a send is in flight"
    )
    assert ":disabled=" in control, (
        f"{name}'s send control looks armed with an empty draft"
    )


@pytest.mark.parametrize("name", sorted(COMPOSERS))
def test_the_control_is_named_as_well_as_labelled(name):
    """The label is back on the screen; the name it grew while it was gone stays.

    A button whose only content is a path from ``@mdi/js`` announces nothing at all,
    which is what the accessible names were added for — and they are still the
    stricter statement, because these three sit a screen apart doing three different
    things and "send" is not enough to tell an answer that respawns a run from a
    reply that does not. Both, now: what is read and what is announced.
    """
    control = _control(_read(name))
    match = re.search(r'aria-label="([^"]+)"', control)
    assert match, f"{name}'s send control announces nothing"
    name_in_tree = match.group(1)
    assert len(name_in_tree.split()) > 1, (
        f"{name}'s send control is announced as {name_in_tree!r}, which does not "
        "say which of the three boxes it belongs to"
    )
    # WCAG 2.5.3: the visible text has to be *contained in* the announced name, or
    # voice control ("tap answer") addresses a control by a name the software does
    # not know it by. So the label may be shorter than the name — "send" inside
    # "send to this session" — but not a paraphrase of it.
    label = re.search(r">([^<>]*[a-z][^<>]*)</v-btn>", control)
    assert label, f"{name}'s send control has no visible label"
    visible = label.group(1).strip()
    assert visible in name_in_tree, (
        f"{name} reads {visible!r} and announces {name_in_tree!r}; the visible "
        "words are not in the accessible name (WCAG 2.5.3)"
    )


def test_each_control_still_carries_its_own_consequence():
    """The three labels are the one place the difference is written down.

    Answering respawns the run in a new container; replying to an ask channel puts
    a file in a directory the session is already polling. On a host with a
    concurrency cap that difference is the whole story, and losing the labels to an
    icon is exactly how it went quiet the first time.
    """
    answer = _read("AnswerBox.vue")
    assert "aria-label=\"answer and start a session\"" in answer, (
        "the answer control no longer says a session starts"
    )
    # And where a sighted operator reads it: the sentence directly under the
    # control, which is the one that survives the label being a single word (which
    # it is, so that the visible text stays inside the announced name — see
    # test_the_control_is_named_as_well_as_labelled).
    assert "this starts a new session for the run" in answer, (
        "the sentence that says a container starts is gone from beside the control"
    )

    ask = _read("AskBox.vue")
    assert "aria-label=\"send reply\"" in ask
    assert "starts a new session" not in ask, (
        "that promise belongs to AnswerBox, where a container really does start"
    )


# --- house style --------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(COMPOSERS))
def test_the_control_is_themed_not_painted(name):
    """The theme owns colour, ``flat`` stays swept out, and ``outlined`` is allowed
    on a ``<v-chip`` and nowhere else.

    A send control is exactly where a hex would sneak back in — it is one button,
    it is the accent of the card, and "just this one" is how the palette ended up in
    two places the last time.

    The chip exception is the operator's, from a live pass: "the chips (questions
    answers, port links) are quite hard to read, lets try variant outlined". A tonal
    chip is a wash of colour behind mid-emphasis text, which on a dark card is a
    label to be guessed at; outlined gives the word full-strength colour on the
    card's own surface. It buys nothing on a control that is already a solid tap
    target, so the ban holds for every button here — which is what the tag-scoped
    scan below is checking, rather than the mere absence of a string.
    """
    text = _read(name)
    control = _control(text)
    assert 'variant="flat"' not in text, (
        f'{name} brings back variant="flat", which was swept out'
    )
    outlined = re.findall(r"<([\w-]+)\b[^>]*variant=\"outlined\"", text)
    assert set(outlined) <= {"v-chip"}, (
        f"{name} puts variant=\"outlined\" on {sorted(set(outlined) - {'v-chip'})}; "
        "the chips are the one place it is allowed back"
    )
    assert 'variant="tonal"' in control, (
        f"{name}'s send control is not one of the two house variants"
    )
    assert 'color="primary"' in control, (
        f"{name}'s send control takes no theme colour"
    )
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", text), (
        f"{name} hardcodes a colour the theme cannot change"
    )


@pytest.mark.parametrize("name", sorted(COMPOSERS))
def test_the_icon_is_a_bundled_path(name):
    """A named ``mdi-`` string is the webfont, which is a wall of empty boxes on the
    LAN this UI is for. The icon rides beside the label now, so the binding is
    ``prepend-icon`` rather than ``icon`` — the same path constant either way."""
    text = _read(name)
    control = _control(text)
    match = re.search(r':(?:prepend-)?icon="(\w+)"', control)
    assert match, f"{name}'s send control has no icon bound from a path constant"
    imports = text.split("from '@mdi/js'")[0]
    assert re.search(rf"\b{match.group(1)}\b", imports), (
        f"{name} does not import {match.group(1)} from @mdi/js"
    )
    assert "import * as" not in text, "a barrel import pulls in every icon path"
