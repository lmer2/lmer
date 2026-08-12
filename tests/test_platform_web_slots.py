"""Guards on the fleet view's service-slot rows (issue #245).

Source-level, like :mod:`tests.test_platform_web_app` and
:mod:`tests.test_platform_web_runcard`, because this image has no browser; the
two helpers that are pure logic (``slotMeta``, ``slotIsFree``) are *executed*
under Node instead of pinned by reading.

What each guard keeps, and why losing it is silent:

- there is a row per slot and it sits **under** the runs, never among them. A
  slot is a fixture of the host: it can never ask for anything, and putting one
  in the list whose entire job is to say who is asking is the cheapest way to
  weaken that list;
- the section renders on a fleet with nothing tracked. That is the state an
  operator is most likely to be about to spawn into a slot from, and the runs
  list is replaced — not merely emptied — in exactly that state, so a section
  nested inside it would disappear at the worst moment;
- and it renders on a host that declares no slots at all, as a title and one
  muted line (#254). Nothing at all was the earlier call; it left the feature
  invisible on precisely the hosts that have not got it yet, and asking the
  orchestrating assistant to set a slot up is how one comes to exist — but that
  line waits for a payload, because an empty list is also what a fleet read that
  failed leaves behind, and the sentence is a claim about the host;
- the state chip is painted through ``toneColor()`` like every other verdict in
  this app, and each state carries an icon of its own. The ramp alone hands a
  red/green-blind operator the same muddy tone for the slot they can use and
  the one that is broken (main.js argues this once, for the run ramp);
- every state the daemon can emit has a label here. A raw ``service_down``
  rendered into a row is the kind of thing that only shows up on a phone;
- an occupied row says who has it — as a link when this orchestrator tracks the
  run, and as the bare session id when it does not. A session spawned against a
  repo the daemon could not identify is never tracked, so the fallback is a real
  case rather than defensive coding, and a link to nothing would be worse than
  no link;
- the dialog's picker is fed by a prop and offers free slots only. A picker
  offering something the daemon will refuse teaches the operator to distrust it,
  and a second payload of its own is how the row and the picker start
  disagreeing about which slots exist;
- slot and preset are exclusive in the dialog, because they are exclusive in the
  daemon — a 400 must not be the first the operator hears of it.

How any of it looks on a 390px row is verified by building the bundle and by a
live look at a real fleet.
"""

import re
import subprocess
from pathlib import Path

from tests.conftest import node_binary, require_node_toolchain

WEB = Path(__file__).resolve().parent.parent / "web"
SLOT_ROW = WEB / "src" / "components" / "SlotRow.vue"
APP = WEB / "src" / "App.vue"
ADD_RUN = WEB / "src" / "components" / "AddRun.vue"
FORMAT = WEB / "src" / "format.js"


def _preset_field(text):
    """The opening tag of the preset combobox."""
    at = text.index('v-model="spawn.preset"')
    return text[text.rindex("<", 0, at) + 1:text.index(">", at)]


def _probe(body):
    """Run *body* against the real format.js under Node."""
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")

    script = "\n".join([
        "import assert from 'node:assert/strict'",
        f"import {{ slotIsFree, slotMeta, toneColor }} from {FORMAT.as_uri()!r}",
        body,
        "console.log('probe ok')",
    ])
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "probe ok" in result.stdout
    return result.stdout


# --- the row exists, and sits where it was asked to --------------------------

def test_the_slot_row_component_exists():
    assert SLOT_ROW.is_file()


def test_the_shell_renders_one_row_per_slot():
    text = APP.read_text(encoding="utf-8")

    assert "import SlotRow from './components/SlotRow.vue'" in text
    assert re.search(r'<SlotRow\b', text), "App.vue renders no SlotRow"
    assert re.search(r'v-for="slot in slots"', text), (
        "the section is not one row per declared slot"
    )
    assert re.search(r':key="slot\.name"', text), (
        "rows keyed on something other than the slot's name will re-render "
        "wholesale on every poll"
    )


def test_the_slots_section_is_below_the_runs_and_outside_the_lists():
    """Under the runs, never among them — and not nested in the runs branch.

    Nested, it would vanish on a fleet tracking nothing, which is exactly when
    an operator is about to spawn into a slot.
    """
    text = APP.read_text(encoding="utf-8")

    runs_list = text.index('v-for="run in calmRows"')
    empty_state = text.index("Nothing tracked yet.")
    slot_section = text.index('v-for="slot in slots"')

    assert slot_section > runs_list, "the slot rows are rendered above the runs"
    # The empty-state card and the runs list are the two arms of one v-if chain;
    # a slots section after both is outside it.
    assert slot_section > empty_state, (
        "the slot rows are inside the runs/empty-state branch, so they "
        "disappear on a fleet that is tracking nothing"
    )


def test_a_host_with_no_slots_says_so():
    """#254 reverses the earlier call. A section that renders nothing on a host
    declaring none is a feature the operator never learns exists — and asking
    the orchestrating assistant to configure one is the only way it comes to."""
    text = APP.read_text(encoding="utf-8")

    assert 'v-if="!loading && slots.length"' not in text, (
        "the section is still gated on there being slots, so a host with none "
        "renders nothing and the feature stays invisible"
    )

    # `!loading` and nothing else: an empty payload and an unanswered poll are
    # different things, and only one of them is worth a sentence. .index() is
    # the pin — the section is gone the moment that condition changes shape.
    section = text.index('<template v-if="!loading">')
    title = text.index('<div class="section-title">service slots</div>')
    assert "No service slots configured." in text, (
        "a host with no slots is told nothing"
    )
    hint = text.index("No service slots configured.")
    assert section < title < hint, (
        "the hint is not inside the slots section, under its title"
    )

    tag = text[text.rindex("<p", 0, hint):hint]
    # Conditional on the emptiness, not on the section: a line saying there are
    # none, printed above rows, is worse than the silence it replaced.
    assert 'v-if="!slots.length && state"' in tag, (
        "the hint renders beside real slot rows"
    )
    # Quiet, in the tone this app's other empty states use. A slot-less host is
    # not a fault, and a loud card would compete with the runs above it.
    assert "text-medium-emphasis" in tag, (
        "the hint is louder than the app's other empty states"
    )


def test_the_hint_needs_a_payload_before_it_claims_anything():
    """A read that failed must not be reported as a host with no slots.

    `slots` is `state?.slots || []`, and `load()` clears `loading` in `finally`
    while leaving `state` where it was — null on a first failure. So a daemon
    that is down, or a 401 on the first load, reaches this section with an empty
    list it never read, and `!loading` alone would print a confident sentence
    about the host's configuration under the error alert. A later failed refresh
    keeps the previous payload, which is the case this deliberately still shows.
    """
    text = APP.read_text(encoding="utf-8")

    hint = text.index("No service slots configured.")
    tag = text[text.rindex("<p", 0, hint):hint]

    condition = re.search(r'v-if="([^"]*)"', tag)
    assert condition, "the hint is unconditional"
    assert re.search(r"\bstate\b", condition.group(1)), (
        "the hint's condition does not require a payload, so a fleet read that "
        "failed renders 'No service slots configured.' about a host this app "
        "never managed to ask"
    )


# --- the verdict is readable -------------------------------------------------

def test_the_state_chip_is_painted_through_the_shared_tone_map():
    """Not a colour this component picked: a chip that disagrees with the run
    rows' ramp is two signalling systems on one screen."""
    text = SLOT_ROW.read_text(encoding="utf-8")

    assert "toneColor(meta.tone)" in text
    assert "slotMeta" in text
    # No literal Vuetify colour names anywhere in the component.
    assert not re.search(r'color="(success|warning|error|primary)"', text), (
        "the row paints a colour directly instead of going through toneColor()"
    )


def test_every_state_carries_an_icon_of_its_own():
    """Colour alone is not a signal in this app — main.js says why."""
    text = FORMAT.read_text(encoding="utf-8")
    block = re.search(r"const SLOT_META = \{(.*?)\n\}", text, re.S)
    assert block, "format.js no longer maps slot states"

    icons = re.findall(r"icon:\s*'([^']+)'", block.group(1))
    states = re.findall(r"^\s{2}([a-z_]+):", block.group(1), re.M)
    assert len(icons) == len(states), "a slot state has no icon"
    assert len(set(icons)) == len(icons), (
        f"two slot states share an icon ({icons}) — the pairing exists so the "
        "states are distinguishable without colour"
    )

    # And every name the map uses is actually wired to a glyph in the row.
    row = SLOT_ROW.read_text(encoding="utf-8")
    for icon in icons:
        assert f"'{icon}'" in row or f"{icon}:" in row, (
            f"SlotRow.vue has no glyph for the {icon!r} icon name"
        )


def test_slot_states_cover_the_backend_vocabulary():
    """The UI must not render a raw enum value for a state the daemon can emit."""
    from lmer_platform.slots import SLOT_STATES

    text = FORMAT.read_text(encoding="utf-8")
    for state in SLOT_STATES:
        assert f"{state}:" in text, f"format.js has no label for slot state {state!r}"


def test_every_slot_tone_is_one_the_theme_can_paint():
    """The seam between SLOT_META's tones and the ramp that defines them."""
    text = FORMAT.read_text(encoding="utf-8")
    slot_block = re.search(r"const SLOT_META = \{(.*?)\n\}", text, re.S)
    tone_block = re.search(r"const TONE_COLORS = \{(.*?)\n\}", text, re.S)
    assert slot_block and tone_block

    known = set(re.findall(r"^\s{2}([a-z]+):", tone_block.group(1), re.M))
    for tone in re.findall(r"tone:\s*'([^']+)'", slot_block.group(1)):
        assert tone in known, (
            f"slot tone {tone!r} is not in the ramp, so the chip renders unstyled"
        )


def test_slot_meta_labels_and_falls_back(tmp_path):
    _probe("\n".join([
        "assert.equal(slotMeta('free').label, 'free')",
        "assert.equal(slotMeta('occupied').tone, 'idle')",
        "assert.equal(slotMeta('misconfigured').tone, 'bad')",
        "assert.equal(slotMeta('service_down').tone, 'attention')",
        # A daemon newer than this bundle: named and painted, never blank.
        "assert.equal(slotMeta('quarantined').label, 'quarantined')",
        "assert.ok(toneColor(slotMeta('quarantined').tone))",
        "assert.equal(slotMeta(undefined).label, 'unknown')",
    ]))


def test_only_a_free_slot_reads_as_free(tmp_path):
    """One predicate for the row's wording and the picker's offer."""
    _probe("\n".join([
        "assert.equal(slotIsFree({state: 'free'}), true)",
        "assert.equal(slotIsFree({state: 'occupied'}), false)",
        "assert.equal(slotIsFree({state: 'service_down'}), false)",
        "assert.equal(slotIsFree({state: 'misconfigured'}), false)",
        "assert.equal(slotIsFree(null), false)",
        "assert.equal(slotIsFree({}), false)",
    ]))


# --- who is holding it -------------------------------------------------------

def test_an_occupied_row_opens_the_run_holding_it():
    text = SLOT_ROW.read_text(encoding="utf-8")

    assert "emit('open', entry.run)" in text, "an occupied row opens nothing"
    # Matched on the run identity, not on the session id: the shell's own key.
    for field in ("host", "project", "slug"):
        assert f"run.{field} === entry.held.{field}" in text, (
            f"the holder is matched without {field}"
        )


def test_an_untracked_holder_is_named_rather_than_linked():
    """A session whose run the daemon could not identify is never tracked, so
    this is a real state — and a link to nothing is worse than no link."""
    text = SLOT_ROW.read_text(encoding="utf-8")

    assert "entry.held.session_id" in text
    assert re.search(r'v-if="entry\.run"', text), (
        "the link is rendered unconditionally"
    )
    assert re.search(r"v-else", text), "there is no fallback for an untracked holder"


def test_the_reason_is_rendered_even_on_an_occupied_row():
    """The row says 'in use' because that is true now; the reason is what will
    still be wrong when the session ends."""
    text = SLOT_ROW.read_text(encoding="utf-8")

    reason = re.search(r'v-if="row\.reason"', text)
    assert reason, "a slot's reason is never rendered"
    # Not nested inside the occupant branch.
    assert 'v-if="holders.length"' in text
    assert text.index('v-if="holders.length"') < reason.start()


def test_the_row_carries_no_attention_stripe_or_forget():
    """A slot is a fixture, not something that can need a human. Borrowing the
    run row's markings would put a second, meaningless signal on the screen the
    attention ramp is for."""
    text = SLOT_ROW.read_text(encoding="utf-8")

    assert "tone-edge" not in text, "the row borrows the attention stripe"
    # The emit and the handler, not the word: the component's own comment says
    # it has neither, and a guard that matched prose would pass on a row that
    # grew one and dropped the sentence.
    assert "emit('forget'" not in text and '@forget' not in text, (
        "the row offers a forget"
    )
    assert re.findall(r"defineEmits\(\[([^\]]*)\]\)", text) == ["'open'"], (
        "the row emits something other than the one navigation it is allowed"
    )


# --- the picker --------------------------------------------------------------

def test_the_dialog_takes_its_slots_as_a_prop():
    """Not a payload of its own: two lists is how the row and the picker start
    disagreeing about which slots exist."""
    add_run = ADD_RUN.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")

    assert "freeSlots:" in add_run, "AddRun declares no freeSlots prop"
    assert ':free-slots="freeSlots"' in app, "the shell passes no slots down"
    assert "fetchSlots" not in add_run, "the dialog fetches slots of its own"


def test_the_picker_offers_free_slots_only():
    app = APP.read_text(encoding="utf-8")

    assert "slots.value.filter(slotIsFree)" in app, (
        "the picker's list is not filtered through the shared predicate"
    )


def test_the_picker_is_absent_on_a_host_that_declares_none():
    text = ADD_RUN.read_text(encoding="utf-8")

    assert re.search(r'v-if="freeSlots\.length"', text), (
        "an empty select explaining itself is furniture"
    )


def test_slot_and_preset_are_exclusive_in_the_dialog():
    """They are exclusive in the daemon; a 400 must not be how the operator
    finds that out."""
    text = ADD_RUN.read_text(encoding="utf-8")

    # By clearing each other, never by disabling: the preset field has to stay
    # typeable (tests/test_platform_addrun.py holds that line), so a greyed-out
    # field the operator must work out how to re-enable is not the shape this
    # can take.
    assert re.search(
        r"watch\(\(\) => spawn\.value\.slot,.*?spawn\.value\.preset = ''", text, re.S
    ), "picking a slot leaves a preset standing"
    assert re.search(
        r"watch\(\(\) => spawn\.value\.preset,.*?spawn\.value\.slot = null", text, re.S
    ), "typing a preset leaves a slot standing"
    assert ":disabled" not in _preset_field(text), (
        "the preset field is disabled — the locked menu by another route"
    )
    # And the request carries one or the other, never both.
    assert "else if (typed(spawn.value.preset))" in text, (
        "the payload can name a slot and a preset at once"
    )


def test_a_picked_slot_reaches_the_wire():
    text = ADD_RUN.read_text(encoding="utf-8")

    assert "payload.slot = typed(spawn.value.slot)" in text


# --- review iteration 1 ------------------------------------------------------

def test_the_row_prop_avoids_vues_reserved_slot_vocabulary():
    """`slot` is legal as a prop name in Vue 3 but collides with the framework's
    own slot vocabulary and warns under the migration build. Cheap to avoid."""
    text = SLOT_ROW.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")

    assert re.search(r"^\s*row: \{ type: Object", text, re.M), (
        "the row prop is not named `row`"
    )
    assert "slot: { type: Object" not in text
    assert ':row="slot"' in app, "the shell still binds the reserved name"


def test_a_contended_slot_names_every_holder():
    """One confident name in the one case the race can produce is the failure
    this row exists to avoid."""
    text = SLOT_ROW.read_text(encoding="utf-8")

    assert "occupants.value.length > 1" in text, "the row cannot tell a collision"
    assert re.search(r'v-if="contended"', text), "a collision renders identically"
    assert re.search(r'v-for="\(entry, index\) in holders"', text), (
        "only one holder is rendered"
    )
    # Painted, because two agents on one dev service is urgency, not identity.
    assert "text-error" in text


def test_the_dialog_says_when_every_slot_is_busy():
    """`v-if="freeSlots.length"` alone made an all-busy host render identically
    to a host with no slots — and the slot rows are on the other screen."""
    text = ADD_RUN.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")

    assert "slots: { type: Array" in text, "the dialog cannot see occupied slots"
    assert ':slots="slots"' in app, "the shell passes only the free ones"
    assert re.search(r'v-if="slots\.length && !freeSlots\.length"', text), (
        "an all-busy host is indistinguishable from a slot-less one"
    )


def test_a_service_held_row_can_reach_its_holder():
    """A row occupied only via `service_occupants` used to render "in use" with
    no way to reach whoever had it — the payload field had no UI consumer."""
    text = SLOT_ROW.read_text(encoding="utf-8")

    assert "row.service_occupants" in text, "the payload field is still unread"
    assert "serviceOccupants" in text
    # Folded into the one `holders` list the template already renders, so both
    # shapes link the same way rather than growing a second block.
    assert re.search(r"\.\.\.serviceOccupants\.value\.map", text), (
        "service holders are not folded into the holders list"
    )
    # …and it says which slot they took, since "held by X" on a row X is not
    # holding would be confusing on its own.
    assert "via slot" in text
