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
  daemon — a 400 must not be the first the operator hears of it;
- and the editor (#256) reports the daemon's own verdict rather than its own,
  sends the version it read the row at, and makes removing an occupied slot a
  second deliberate press. Each of those is a way the editor could look like it
  works while quietly overwriting somebody — the form agreeing with itself, a
  save winning a race against a text editor, a removal landing under a running
  agent.

How any of it looks on a 390px row is verified by building the bundle and by a
live look at a real fleet.
"""

import json
import re
import subprocess
from pathlib import Path

from tests.conftest import node_binary, require_node_toolchain

WEB = Path(__file__).resolve().parent.parent / "web"
SLOT_ROW = WEB / "src" / "components" / "SlotRow.vue"
SLOT_EDITOR = WEB / "src" / "components" / "SlotEditor.vue"
API = WEB / "src" / "api.js"
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
    # `v-if` or `v-else-if` — #256 put the unreadable-config alert ahead of it
    # in the same chain, and which arm the hint sits on is not what this guard
    # is about.
    assert re.search(r'v-(?:else-)?if="!slots\.length && state"', tag), (
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

    condition = re.search(r'v-(?:else-)?if="([^"]*)"', tag)
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
    # `edit` joined `open` with #256. It is not a marking of the kind this guard
    # bans — it opens the editor for a fixture of the host, and it is still the
    # operator asking rather than the row asking for an operator.
    assert re.findall(r"defineEmits\(\[([^\]]*)\]\)", text) == [
        "'open', 'edit'"
    ], "the row emits something other than the navigation and the edit"


# --- the editor (issue #256) -------------------------------------------------

def test_the_editor_component_exists_and_is_mounted_by_the_shell():
    app = APP.read_text(encoding="utf-8")

    assert SLOT_EDITOR.is_file()
    assert "import SlotEditor from './components/SlotEditor.vue'" in app
    assert re.search(r"<SlotEditor\b", app), "the shell mounts no editor"


def test_every_row_can_be_edited_and_a_new_slot_declared():
    row = SLOT_ROW.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")

    assert "emit('edit', row)" in row, "the row offers no edit"
    assert '@edit="editSlot"' in app, "the shell does not listen for it"
    assert 'editSlot(null)' in app, "there is no way to declare a new slot"


def test_the_editor_avoids_vues_reserved_slot_vocabulary():
    """SlotRow.vue's rule, for the same reason — see the row's own guard."""
    text = SLOT_EDITOR.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")

    assert re.search(r"^\s*row: \{ type: Object", text, re.M)
    assert "slot: { type: Object" not in text
    assert ':row="editingSlot"' in app


def test_the_editor_previews_through_the_daemons_dry_run():
    """The issue's "same sentence" rule, at the only place it can be lost.

    A form that decided for itself whether a slot was usable would be a second
    implementation of ``_resolve_definition``; the dry run is what makes the
    verdict shown before a save the row's own.
    """
    text = SLOT_EDITOR.read_text(encoding="utf-8")

    assert "dryRun: true" in text, "the editor previews nothing"
    assert "slotMeta(preview.value?.state)" in text, (
        "the preview paints its verdict with something other than the row's map"
    )
    assert "preview?.reason" in text, "the preview drops the daemon's reason"


def test_the_editor_paints_its_verdict_through_the_shared_tone_map():
    """The row's rule (see the chip guard above): the ramp alone is not a signal."""
    text = SLOT_EDITOR.read_text(encoding="utf-8")

    assert "toneColor(meta.tone)" in text


def test_a_save_carries_the_version_it_was_read_at():
    """Without the etag the daemon cannot tell an edit from an overwrite.

    Read at *open*, not at the last poll — see the mounted probe below, which is
    the guard that actually binds it. This one only keeps the header in place.
    """
    text = SLOT_EDITOR.read_text(encoding="utf-8")
    client = API.read_text(encoding="utf-8")

    assert "etag: opened.value.etag" in text, "the editor sends no version"
    # The prop may be *compared* against the snapshot — that is how the dialog
    # notices it moved — but never sent.
    assert "etag: props.row.etag" not in text, (
        "a save sends the etag off the polled prop, which moves under it"
    )
    assert "'If-Match': etag" in client, "the client does not send it as If-Match"


def test_the_confirm_reaches_both_verbs():
    """The 409 for a re-point names `confirm=true`; the client has to be able to
    send it, or the refusal is a dead end and the docs promise a press that does
    not exist."""
    text = SLOT_EDITOR.read_text(encoding="utf-8")
    client = API.read_text(encoding="utf-8")

    assert re.search(r"export function updateSlot\([^)]*confirm", client, re.S), (
        "updateSlot cannot send a confirm"
    )
    assert "confirm: confirmingRebind.value" in text
    assert "'save anyway'" in text


def test_a_slot_that_changed_underneath_reloads_rather_than_retrying():
    """The fields were composed against a version that is gone."""
    text = SLOT_EDITOR.read_text(encoding="utf-8")

    assert "exc.code === 'slot_changed'" in text
    assert text.count("emit('changed')") >= 3, (
        "a refused save leaves the view holding the version it was refused for"
    )


def test_removing_an_occupied_slot_takes_a_second_deliberate_press():
    """The daemon refuses first; the confirm exists only after that refusal.

    An up-front checkbox is one people learn to tick — the refusal has to be
    read before it can be answered.
    """
    text = SLOT_EDITOR.read_text(encoding="utf-8")

    assert "confirm: confirmingRemoval.value" in text
    assert re.search(
        r"confirmingRemoval = ref\(false\)", text,
    ), "the confirm does not start off"
    assert "exc.code === 'slot_occupied'" in text, (
        "nothing turns the daemon's refusal into the confirm"
    )
    assert "'remove anyway'" in text


def test_the_preset_field_is_a_menu_rather_than_free_text():
    """Unlike the spawn dialog's: there the list is a suggestion, here it is the
    rule — the write refuses a preset this host does not have, so free text
    would be offering a 400."""
    text = SLOT_EDITOR.read_text(encoding="utf-8")

    at = text.index('v-model="preset"')
    tag = text[text.rindex("<", 0, at) + 1:text.index(">", at)]
    assert tag.startswith("v-select"), f"the preset field is a {tag.split()[0]}"
    assert ':items="presets"' in tag


def test_the_editor_debounces_its_preview():
    """One resolution per keystroke would probe a dev container per character."""
    text = SLOT_EDITOR.read_text(encoding="utf-8")

    assert "PREVIEW_DELAY_MS" in text
    assert "clearTimeout(previewTimer)" in text


def test_a_stale_preview_cannot_land_on_a_newer_one():
    text = SLOT_EDITOR.read_text(encoding="utf-8")

    assert "function stale(mine)" in text
    assert "if (stale(mine)) return" in text


# --- the editor, mounted (issue #256) ----------------------------------------
#
# Every guard above reads source, and no amount of reading source can observe a
# lifecycle: the dialog comes into existence with `modelValue` already true (the
# shell sets the name and the flag in one tick, and the `:key` remounts it), so a
# watcher that is not `immediate` has no transition to see and the form opens
# with no verdict and no preset list — looking exactly like a host that has
# neither. tests/test_platform_web_sketch.py records the same defect shipping
# green past source-level guards; this is its rig, narrowed to this component.

_MOUNT_PROBE = """
import { readFileSync } from 'node:fs'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { parse, compileScript } from '@vue/compiler-sfc'

const path = 'src/components/SlotEditor.vue'
const { descriptor } = parse(readFileSync(path, 'utf8'), { filename: path })
const compiled = compileScript(descriptor, { id: 'probe', inlineTemplate: true })

// The daemon, recorded rather than faked away: reaching it is the assertion.
const calls = []
globalThis.fetch = async (url) => {
  calls.push(String(url).split('?')[0])
  return {
    ok: true,
    status: 200,
    json: async () => ({ presets: ['webapp_dev'], name: 'webapp', state: 'free' }),
  }
}

const source = compiled.content
  .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
  .replace(/from ['"]@mdi\\/js['"]/g, `from '${import.meta.resolve('@mdi/js')}'`)
  .replace(/from ['"]\\.\\.\\/(api|format)\\.js['"]/g,
    (_m, name) => `from '${import.meta.resolve(`./src/${name}.js`)}'`)
const component = (await import(
  `data:text/javascript;base64,${Buffer.from(source, 'utf8').toString('base64')}`
)).default

// Mounted the way App.vue mounts it: open from the first frame.
const app = createSSRApp({
  render: () => h(component, {
    modelValue: true,
    row: %s,
  }),
})
app.config.compilerOptions.isCustomElement = (tag) => tag.startsWith('v-')
app.config.warnHandler = () => {}
try {
  await renderToString(app)
} catch (error) {
  calls.push(`render-error:${error.message}`)
}
console.log(JSON.stringify({ calls }))
"""

EXISTING_ROW = (
    "{ name: 'webapp', preset: 'webapp_dev', description: '', etag: 'abc123' }"
)


def _mounted(row="null"):
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")
    result = subprocess.run(
        [node, "--input-type=module", "-e", _MOUNT_PROBE % row],
        capture_output=True, text=True, timeout=180, cwd=str(WEB),
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout)["calls"]


def test_mounting_the_editor_open_asks_the_host_for_its_presets():
    """A select whose list never loads is a select with nothing in it."""
    assert "api/spawn-options" in _mounted(EXISTING_ROW)


def test_mounting_the_editor_on_a_row_previews_it_straight_away():
    """Without this the dialog opens with no verdict until something is typed —
    and an operator opening a `misconfigured` slot to fix it sees nothing at all
    about what is wrong with it."""
    calls = _mounted(EXISTING_ROW)

    assert "api/slots/webapp" in calls, f"no preview at mount: {calls}"
    assert not [call for call in calls if call.startswith("render-error:")], calls


def test_mounting_the_editor_for_a_new_slot_previews_nothing_yet():
    """Empty fields are not a slot to have an opinion about."""
    calls = _mounted("null")

    assert "api/slots" not in calls
    assert not [call for call in calls if call.startswith("render-error:")], calls

# --- the etag the save actually carries (issue #256, review iteration 1) -----
#
# The source guards above can see that *an* etag is sent. They cannot see *which*
# one, and that is the whole property: `props.row` is a computed over the polled
# payload, so a dialog left open for ten seconds is handed a fresh etag for a
# change the operator has not seen. Sending it makes `If-Match` match the very
# edit it exists to refuse — and because the form still holds the old preset, the
# save then reverts it.
#
# So this drives the component: one instance, mounted with etag E0, then given a
# new row object the way a poll does, then saved. The rig is heavier than the
# mount probe above (a custom renderer, so the instance survives a prop change
# and `save()` can be called on it) because a re-mounting rig cannot observe this
# at all — a fresh instance snapshots the new row and looks correct either way.

_SAVE_PROBE = """
import { readFileSync } from 'node:fs'
import { createRenderer, ref, h, nextTick } from 'vue'
import { parse, compileScript } from '@vue/compiler-sfc'

const path = 'src/components/SlotEditor.vue'
const { descriptor } = parse(readFileSync(path, 'utf8'), { filename: path })
// Not `inlineTemplate`: that closes the setup bindings over the render function
// and `save()` becomes unreachable. The template is not what is under test here.
const compiled = compileScript(descriptor, { id: 'probe' })

const sent = []
globalThis.fetch = async (url, options = {}) => {
  sent.push({
    url: String(url),
    method: options.method || 'GET',
    ifMatch: (options.headers || {})['If-Match'] || null,
    body: options.body ? JSON.parse(options.body) : null,
  })
  return {
    ok: true,
    status: 200,
    json: async () => ({ presets: ['webapp_dev', 'api_dev'], state: 'free' }),
  }
}

const source = compiled.content
  .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
  .replace(/from ['"]@mdi\\/js['"]/g, `from '${import.meta.resolve('@mdi/js')}'`)
  .replace(/from ['"]\\.\\.\\/(api|format)\\.js['"]/g,
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
const row = ref({
  name: 'webapp', preset: 'webapp_dev', description: '', etag: 'E0',
})
const app = createApp({
  render: () => h(component, { modelValue: true, row: row.value, ref: child }),
})
app.config.warnHandler = noop
app.mount(nodeOps.createElement('root'))
await nextTick()

// `<script setup>` bindings are deliberately private on the public proxy, so the
// internal instance is how a probe reaches them. Test-only reach-in: exposing
// them for this would be production surface bought with a test.
const state = child.value.$.setupState
const moved = [state.movedUnderneath]

// The operator types something.
state.description = 'typed by the operator'
await nextTick()

// Ten seconds of polling: a new object, a preset nobody in this dialog picked,
// and the etag that change earned.
row.value = { name: 'webapp', preset: 'api_dev', description: '', etag: 'E1' }
await nextTick()
moved.push(state.movedUnderneath)

sent.length = 0
await state.save()

console.log(JSON.stringify({ sent, movedUnderneath: moved }))
"""


def _save_probe():
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")
    result = subprocess.run(
        [node, "--input-type=module", "-e", _SAVE_PROBE],
        capture_output=True, text=True, timeout=180, cwd=str(WEB),
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout)


def test_a_save_carries_the_etag_the_dialog_opened_at_not_the_polls():
    """The critical finding of review iteration 1.

    Concretely: the row is at E0 when the pencil is pressed; a colleague edits
    `config.json` and the entry becomes E1; the poll delivers E1 to the open
    dialog. The save must still carry E0 — that is what makes the daemon's 412
    fire, and the colleague's edit survive.
    """
    sent = _save_probe()["sent"]

    writes = [call for call in sent if call["method"] == "PUT"]
    assert writes, f"the save sent nothing: {sent}"
    assert writes[0]["ifMatch"] == "E0", (
        f"the save carried {writes[0]['ifMatch']!r} — the poll's version, not "
        "the one the fields were composed against"
    )


def test_a_save_does_not_revert_a_change_it_never_saw():
    """The same defect's second half, and the one that does the damage.

    `changes()` diffs the form against its snapshot. Diffed against the *polled*
    prop instead, the preset the operator never touched reads as changed — so
    the save actively writes the old value back over the colleague's.
    """
    writes = [call for call in _save_probe()["sent"] if call["method"] == "PUT"]

    assert "preset" not in writes[0]["body"], (
        "the save asserts a preset nobody in this dialog changed: "
        f"{writes[0]['body']}"
    )
    assert writes[0]["body"]["description"] == "typed by the operator"


def test_the_dialog_notices_the_row_moved_under_it():
    """Said before the save rather than after its refusal: the fields on screen
    were composed against a version that no longer exists."""
    assert _save_probe()["movedUnderneath"] == [False, True]


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
