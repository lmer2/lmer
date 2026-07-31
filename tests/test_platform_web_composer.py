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

What each test here pins, and what its loss looks like:

- the send control sits *inside* the field. Below it, it is a second row of chrome
  under a box that already grew; inside it, it costs nothing and can therefore
  stay on every device, which is the whole argument for the next one.
- it is **not** hidden for a fine pointer. The terminal's key pad is, because there
  it is redundant — xterm takes the keystrokes itself. Here the field is the only
  way in, so hiding the icon leaves a desktop operator with a box and no visible
  sign of what sends it, and ``pointer: coarse`` reads the *primary* pointer, so a
  touchscreen laptop would lose the button to a finger as well.
- Ctrl+Enter sends, Cmd+Enter with it (same event, ``metaKey``), and a bare Enter
  never does: these are multi-line boxes an operator writes a paragraph in, and
  ``auto-grow``/``max-rows`` are there because that is expected.
- the control keeps the disabled and loading states the buttons had. A send that
  spawns a container takes seconds, and the spinner is the only thing that says so.
- an icon-only control still has a name, and each name still carries the one fact
  its old label did — that answering starts a session, and that replying does not.

Whether any of it is actually reachable one-handed is verified by building the
bundle and by live test LT3 on a real phone.
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


def _field(text):
    """The one ``<v-textarea>`` element in a composer, markup and all.

    Sliced rather than matched with a regex because what these tests are about is
    what sits *inside* the field: a control that has slipped back out below it
    still matches every pattern in the file.
    """
    assert text.count("<v-textarea") == 1, (
        "a composer with two fields is not what these guards describe"
    )
    start = text.index("<v-textarea")
    return text[start:text.index("</v-textarea>") + len("</v-textarea>")]


def _hint(text):
    match = re.search(r'hint="([^"]+)"', text)
    assert match, "the field lost its hint"
    return match.group(1)


def _keydown_modifiers(block, handler):
    """The Vue modifier sets on every ``@keydown`` bound to *handler*.

    A set per binding, so the assertions are about which modifiers are present
    rather than about the order they were written in — ``.ctrl.enter.prevent`` and
    ``.prevent.ctrl.enter`` are the same binding to Vue and must be the same
    binding here.
    """
    found = re.findall(rf'@keydown((?:\.[a-z]+)+)="{handler}"', block)
    return [set(modifiers.strip(".").split(".")) for modifiers in found]


def _style(text):
    return text[text.index("<style"):] if "<style" in text else ""


# --- where the control is ----------------------------------------------------

@pytest.mark.parametrize("name,handler", sorted(COMPOSERS.items()))
def test_the_send_control_is_inside_the_field(name, handler):
    """In the ``append-inner`` slot, not on a row of its own under the box.

    The move is what pays for the decision below it. A button on its own row is a
    band of chrome that grows with an ``auto-grow`` box and pushes whatever follows
    it down the page — worth removing from a screen that has another way to send,
    which is what made the terminal's composer touch-only. An icon in a corner the
    field already occupies costs none of that, so it does not have to be traded
    against anything and can stay on every device.
    """
    text = _read(name)
    field = _field(text)
    assert "<template #append-inner>" in field, (
        f"{name}'s send control is not in the field's append-inner slot"
    )
    assert f'@click="{handler}"' in field, (
        f"{name} has an append-inner slot but the control that sends is elsewhere"
    )
    # Exactly one thing clicks send anywhere in the file, and it is the one just
    # found inside the field — which is how "the button below is gone" is stated
    # without naming a button that no longer exists.
    assert text.count(f'@click="{handler}"') == 1, (
        f"{name} sends from more than one control; the second is outside the field"
    )


@pytest.mark.parametrize("name", sorted(COMPOSERS))
def test_the_only_send_control_is_not_hidden_from_a_desktop(name):
    """The one deliberate difference from the terminal's touch-only composer.

    There, a fine pointer means the key pad and the line field are *redundant* —
    xterm takes arrows, chords and pasted text directly, and clicking the terminal
    is what focuses it — so the block is dead chrome and is hidden as a unit. A
    composer has no such second path: the field is the only way in on every device,
    and with the chord as the only other way to send, hiding the icon leaves
    nothing on screen that says a draft can be sent at all.

    ``pointer: coarse`` is also the *primary* pointer, so a laptop with a
    touchscreen reports ``fine``: gating the only send control on it takes the
    button away from a finger that is really there.

    If this is ever reversed, the hint has to grow the affordance the icon was
    carrying — a visible, persistent line naming the chord — and this test is where
    that trade gets re-argued rather than quietly lost.
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
    field = _field(text)
    control = field[field.index("<template #append-inner>"):]
    assert not re.search(r'\bd-(?:none|sm-|md-|lg-|xl-)', control), (
        f"{name}'s send control is hidden at some viewport width"
    )


# --- how it is reached from a keyboard ---------------------------------------

@pytest.mark.parametrize("name,handler", sorted(COMPOSERS.items()))
def test_ctrl_enter_sends_and_cmd_enter_with_it(name, handler):
    """One chord, both platforms. Cmd is the same event with ``metaKey`` set.

    ``.prevent`` on both: a browser that would have inserted a newline for the
    chord must not leave one behind in a draft — and a failed send deliberately
    *keeps* the draft in two of these three boxes, so the stray line would survive
    into the retry.
    """
    field = _field(_read(name))
    bindings = _keydown_modifiers(field, handler)
    assert any({"ctrl", "enter"} <= binding for binding in bindings), (
        f"{name} does not send on Ctrl+Enter"
    )
    assert any({"meta", "enter"} <= binding for binding in bindings), (
        f"{name} does not send on Cmd+Enter, so a Mac has only the icon"
    )
    for binding in bindings:
        assert "prevent" in binding, (
            f"{name} sends on {sorted(binding)} without preventing the default; "
            "the chord can leave a newline in a draft a failed send keeps"
        )


@pytest.mark.parametrize("name,handler", sorted(COMPOSERS.items()))
def test_enter_on_its_own_is_still_a_new_line(name, handler):
    """These are paragraphs, not chat one-liners, and the fields say so.

    ``auto-grow`` with ``max-rows`` exists precisely because an operator writes
    several lines here — a question is answered in sentences, and a message to a
    session routinely carries a path on its own line. Sending on a bare Enter would
    cut every one of them off at the first line break, and the text is already gone
    from the box by the time that is visible.
    """
    text = _read(name)
    field = _field(text)
    # Every binding that sends has to name a modifier key. Asserting the absence
    # of the exact string ``@keydown.enter=`` is not enough: ``.enter.prevent``
    # sends on Enter just as thoroughly and would have gone straight past it.
    for binding in _keydown_modifiers(field, handler):
        assert binding & {"ctrl", "meta"}, (
            f"{name} sends on {sorted(binding)}, with no modifier key — a "
            "paragraph is cut off at its first line break, and the text is out "
            "of the box before that is visible"
        )
    assert "auto-grow" in field and "max-rows" in field, (
        f"{name}'s field no longer grows, which is the reason Enter is a newline"
    )
    assert "Enter is a new line" in _hint(text), (
        f"{name} does not say what Enter does, which is the half of the chord an "
        "operator finds out about by losing a paragraph"
    )


@pytest.mark.parametrize("name", sorted(COMPOSERS))
def test_the_chord_is_written_down_where_it_is_used(name):
    """A shortcut nobody is told about is a shortcut nobody uses.

    The hint is already persistent on all three fields — it has to be, because each
    says something about where the text goes that an operator must read *before*
    sending — so naming the chord there costs no space and cannot be dismissed.
    """
    hint = _hint(_read(name))
    assert "Ctrl+Enter" in hint, f"{name}'s hint does not name the chord"
    assert "Cmd" in hint, (
        f"{name}'s hint names Ctrl only; on a Mac that key sends nothing"
    )
    assert "persistent-hint" in _read(name), (
        f"{name}'s hint is only shown on focus, which is not where it is needed"
    )


# --- what the control still has to be ----------------------------------------

@pytest.mark.parametrize("name", sorted(COMPOSERS))
def test_the_inline_control_keeps_its_disabled_and_loading_states(name):
    """Both moved in with it; both are the only feedback these sends have.

    Loading, because a send is a round trip to the daemon and one of these three
    starts a *container* — seconds in which nothing else on the card changes.
    Disabled, because an empty draft is the common state of a composer and a
    control that looks armed when there is nothing to send is a control that has
    to be tried to be understood.
    """
    field = _field(_read(name))
    control = field[field.index("<template #append-inner>"):]
    assert ":loading=" in control, (
        f"{name}'s send control no longer shows that a send is in flight"
    )
    assert ":disabled=" in control, (
        f"{name}'s send control looks armed with an empty draft"
    )


@pytest.mark.parametrize("name", sorted(COMPOSERS))
def test_an_icon_only_control_still_has_a_name(name):
    """The label is gone from the screen; it cannot be gone from the accessibility
    tree as well.

    A button whose only content is a path from ``@mdi/js`` announces nothing at
    all, and these three are a screen apart doing three different things — "send"
    is not enough to tell an answer that respawns a run from a reply that does not.
    """
    field = _field(_read(name))
    control = field[field.index("<template #append-inner>"):]
    match = re.search(r'aria-label="([^"]+)"', control)
    assert match, f"{name}'s send control announces nothing"
    assert len(match.group(1).split()) > 1, (
        f"{name}'s send control is announced as {match.group(1)!r}, which does not "
        "say which of the three boxes it belongs to"
    )


def test_each_control_still_carries_its_own_consequence():
    """The three labels were the one place the difference was written down.

    Answering respawns the run in a new container; replying to an ask channel puts
    a file in a directory the session is already polling. On a host with a
    concurrency cap that difference is the whole story, and losing the labels to an
    icon is exactly how it would have gone quiet.
    """
    answer = _read("AnswerBox.vue")
    assert "aria-label=\"answer and start a session\"" in answer, (
        "the answer control no longer says a session starts"
    )
    # Not only in the accessibility tree: the sentence that used to sit beside the
    # button is what a sighted operator reads before the tap.
    assert "this starts a new session for the run" in answer

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

    An icon button in a field is exactly where a hex would sneak back in — it is
    small, it is one component, and "just this one" is how the palette ended up in
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
    field = _field(text)
    control = field[field.index("<template #append-inner>"):]
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
    LAN this UI is for."""
    text = _read(name)
    field = _field(text)
    control = field[field.index("<template #append-inner>"):]
    match = re.search(r':icon="(\w+)"', control)
    assert match, f"{name}'s send control has no icon bound from a path constant"
    imports = text.split("from '@mdi/js'")[0]
    assert re.search(rf"\b{match.group(1)}\b", imports), (
        f"{name} does not import {match.group(1)} from @mdi/js"
    )
    assert "import * as" not in text, "a barrel import pulls in every icon path"
