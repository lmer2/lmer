"""Guards on marking up an image before it is sent (issue 246, slice 2).

The operator's words for it were "very simply draw shapes on the image — e.g.
highlight an area or draw an arrow pointing at something. Simple marks, not an
editor." So most of what this module pins is what the component *is not*: no
colour picking, no text, no cropping, no history — each of which turns a report
into a task — and that an unmarked attachment is sent exactly as it was picked.

Two things here are executed rather than read, and they are the two that cannot
be seen in markup:

- the pointer→image coordinate mapping. The canvas is the image's own pixel size
  and is *displayed* scaled to fit, so on a phone every mark passes through a
  ~7x reduction. Getting this wrong puts the arrow somewhere the operator did
  not point, and no amount of reading the template shows it.
- the name the marked-up copy carries, because "the same file again" and "a new
  file beside it" read very differently in a chat.

The rest — the touch-action that makes a drag draw rather than scroll, the
thumb-sized controls, the chunk split — is source-level for the reason every
other web test here is: no browser in this image.
"""

import json
import re
import subprocess
from pathlib import Path

from tests.conftest import node_binary, require_node_toolchain
from tests.test_platform_web_app import _chat_source

WEB = Path(__file__).resolve().parent.parent / "web"
SKETCH = WEB / "src" / "components" / "SketchPad.vue"


def _source():
    return SKETCH.read_text(encoding="utf-8")


def _function(signature):
    """Source of one top-level function in SketchPad.vue's ``<script setup>``.

    Same shape as the Chat.vue extractor: a ``}`` in column zero ends a
    top-level function, and locality is part of what is being asserted.
    """
    text = _source()
    start = text.index(signature)
    return text[start:text.index("\n}\n", start)] + "\n}"


def _code():
    """The component's script with its comments taken out.

    Because one guard below is about what the component *does not do*, and the
    header explains at length which editor features were left out — read as
    text, the prose naming them is indistinguishable from code doing them. Same
    reason ``tests.test_platform_web_chat._chat_declarations`` strips comments.
    """
    text = _source()
    script = text[text.index("<script"):text.index("</script>")]
    script = re.sub(r"/\*.*?\*/", " ", script, flags=re.S)
    return "\n".join(
        re.sub(r"//.*$", "", line) for line in script.splitlines()
    )


def _rule(selector):
    style = _source()
    style = style[style.index("<style"):]
    block = style[style.index(f"{selector} {{"):]
    return block[:block.index("}")]


# --- what it is, and what it is not ------------------------------------------

def test_the_sketch_phase_is_offered_for_images_and_nothing_else():
    """There is nothing to draw on otherwise, and the same fact that decides the
    tray's thumbnail decides this."""
    chat = _chat_source()
    tray = chat[chat.index('class="attachment"'):]
    tray = tray[:tray.index("</div>")]
    assert 'v-if="item.preview"' in tray, "the mark-up control is offered for any file"
    assert "markUp(item.id)" in tray


def test_marking_up_is_closed_while_a_send_is_in_flight():
    """Accepting a mark replaces the staged file, and replacing one whose upload
    is already running would send the marks nowhere — the same rule the remove
    control got in the !272 review, for the same reason."""
    chat = _chat_source()
    tray = chat[chat.index('class="attachment"'):]
    tray = tray[:tray.index("</div>")]
    mark = tray[tray.index("mdiDraw"):]
    assert ':disabled="sending"' in mark[:mark.index("/>")]


def test_it_is_optional_in_the_strong_sense():
    """Nothing is re-encoded unless a mark was accepted: an image nobody marks up
    is uploaded exactly as it was picked, and `done` is dead until there is
    something to keep."""
    source = _source()
    assert source.count("toBlob") == 1, (
        "the image is flattened somewhere other than the accept path"
    )
    assert "toBlob" in _function("function accept("), (
        "flattening is not on the accept path"
    )
    assert 'canAccept' in source and "marks.value.length > 0" in source
    assert ':disabled="!canAccept"' in source


def test_the_composer_only_replaces_a_file_when_one_comes_back():
    """The tray's file is swapped on the `marked` event and on nothing else —
    cancelling leaves the attachment untouched."""
    chat = _chat_source()
    assert '@marked="useMarked"' in chat
    replace = chat[chat.index("function useMarked("):]
    replace = replace[:replace.index("\n}\n")]
    assert "URL.revokeObjectURL" in replace, (
        "the replaced preview is leaked — this is the one place a staged file is "
        "replaced rather than removed"
    )


def test_it_carries_three_marks_and_an_undo_and_stops_there():
    """Simple marks, not an editor. Each addition below is a decision the
    operator explicitly did not ask for."""
    source = _source()
    tools = re.findall(r"\{ id: '(\w+)'", source)
    assert tools == ["arrow", "box", "free"]
    assert "function undo(" in source
    # The APIs each of those features needs, in the code rather than the prose
    # explaining why they are absent.
    code = _code()
    for api, feature in (
        ("fillText", "text tool"), ("font =", "text tool"),
        ("crop", "crop tool"), ("localStorage", "saved history"),
        ("colorPicker", "colour picking"), ("type=\"color\"", "colour picking"),
    ):
        assert api not in code, f"the sketch pad grew a {feature}"


def test_the_mark_colour_comes_from_the_theme():
    """One palette, in src/main.js. A literal here is a colour the theme cannot
    change — and the composer's own guard would refuse one anyway."""
    source = _source()
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", source), (
        "SketchPad hardcodes a colour"
    )
    assert "--v-theme-error" in _function("function markColour(")


def test_a_stray_tap_leaves_no_mark():
    """Every touch on the image would otherwise leave a dot, and an accidental
    one is indistinguishable from a deliberate full stop."""
    finish = _function("function onUp(")
    assert "extent(mark) >= TAP_SLOP_CSS_PX * imagePerCssPixel()" in finish


def test_what_counts_as_a_tap_holds_for_all_four_cases_at_once():
    """Three answers were needed here, and the first two each had a hole.

    First-to-last distance discarded a closed freehand stroke, so circling
    something — the operator's own example — could never be kept (!273 round 1).
    Accumulated path length fixed that and brought its own: length is cumulative,
    so a tap that wobbles enough times clears any bound a still tap cannot, and
    at this feature's scale the bound was ~1 CSS px (!273 iteration 2). Reach —
    the greatest distance any point gets from the first — has neither property,
    and for a two-point mark it *is* first-to-last, so the tools need no branch.

    Run against the reviewer's own fixture: a 3000px screenshot displayed 342 CSS
    px wide, where one CSS pixel is ~8.8 image pixels.
    """
    measure = _function("function extent(")
    assert "mark.tool" not in measure, (
        "the measure branches on the tool again; reach does not need to"
    )
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")
    script = """
const canvas = { value: { width: 3000, getBoundingClientRect: () => ({ width: 342 }) } }
const TAP_SLOP_CSS_PX = 8

%s

%s

const scale = imagePerCssPixel()
const slop = TAP_SLOP_CSS_PX * scale
const kept = (tool, points) => extent({ tool, points }) >= slop
// A tap with `moves` small wobbles around one point.
const wobble = (css, moves) => {
  const step = css * scale
  const points = [{ x: 100, y: 100 }]
  for (let i = 0; i < moves; i += 1) {
    points.push(i %% 2 ? { x: 100, y: 100 + step } : { x: 100 + step, y: 100 })
  }
  points.push({ x: 100, y: 100 })
  return points
}
const circle = Array.from({ length: 25 }, (_, i) => {
  const angle = (i / 24) * Math.PI * 2
  return { x: 500 + 300 * Math.cos(angle), y: 500 + 300 * Math.sin(angle) }
})
console.log(JSON.stringify({
  scale,
  stillTap: kept('free', [{x:100,y:100},{x:100,y:100}]),
  wobbleOne: kept('free', wobble(1, 4)),
  wobbleTwo: kept('free', wobble(2, 4)),
  wobbleTwoTenMoves: kept('free', wobble(2, 10)),
  closedCircle: kept('free', circle),
  deliberateStroke: kept('free', [{x:0,y:0},{x:20*scale,y:0}]),
  arrowDrag: kept('arrow', [{x:0,y:0},{x:20*scale,y:0}]),
  arrowTap: kept('arrow', [{x:100,y:100},{x:100+scale,y:100}]),
}))
""" % (_function("function imagePerCssPixel("), _function("function extent("))
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    seen = json.loads(result.stdout)
    assert seen["scale"] > 8, "the fixture no longer exercises a scaled-down image"
    # Taps, however jittery and however long they jitter for.
    assert (seen["stillTap"], seen["wobbleOne"], seen["wobbleTwo"]) == (
        False, False, False,
    )
    assert seen["wobbleTwoTenMoves"] is False, "a longer jitter still clears it"
    assert seen["arrowTap"] is False
    # Marks.
    assert seen["closedCircle"] is True, "circling something is discarded again"
    assert (seen["deliberateStroke"], seen["arrowDrag"]) == (True, True)


def test_the_tap_threshold_is_measured_in_what_the_finger_did():
    """`mark.width` is image pixels and a finger works in screen pixels: on a
    3000px screenshot shown 342px wide the bound came to 1.14 CSS px, so the
    filter held only for a perfectly still finger (!273 iteration 2)."""
    assert "TAP_SLOP_CSS_PX" in _source()
    scale = _function("function imagePerCssPixel(")
    assert "getBoundingClientRect" in scale and "element.width" in scale
    # Over the code, not the prose: the comment in `onUp` names the old bound
    # while explaining why it went — the same confusion `_code()` exists for.
    finish = re.sub(r"//.*$", "", _function("function onUp("), flags=re.M)
    assert "mark.width" not in finish, "the tap bound is back in image pixels"


def test_a_cancelled_gesture_is_abandoned_rather_than_committed():
    """A system edge swipe or an arriving call used to share `onUp`, which kept
    whatever had been drawn up to that moment (!273 review)."""
    assert '@pointercancel="onCancel"' in _source()
    cancel = _function("function onCancel(")
    assert "drawing = null" in cancel
    assert "marks.value" not in cancel, "a cancelled gesture still records a mark"


def test_one_pointer_owns_the_stroke():
    """A resting thumb on a phone held one-handed fires its own `pointerdown`;
    the single `drawing` slot was overwritten from that finger's position and
    whichever pointer came up first committed the mixture. Capture does not help
    — it is per pointer (!273 review)."""
    assert "if (!image || drawing) return" in _function("function onDown(")
    owns = _function("function owns(")
    assert "event.pointerId === drawing.pointerId" in owns
    for handler in ("function onMove(", "function onUp(", "function onCancel("):
        assert "owns(event)" in _function(handler), (
            f"{handler} acts on any pointer's event"
        )


def test_a_lossy_source_comes_back_in_its_own_format():
    """Flattening everything to PNG could push a photo that was inside the upload
    cap outside it — measured at 3.2 MiB as JPEG against 22.7 MiB as PNG for one
    4032x3024 frame, on a default cap of 8 MiB. The camera roll is one of the
    three input paths this feature was asked for (!273 review)."""
    output = _function("const OUTPUT_TYPE = computed(")
    assert "image/jpeg" in output
    accept = _function("function accept(")
    assert "OUTPUT_TYPE.value" in accept
    assert "JPEG_QUALITY" in accept
    # And the name follows the bytes, since the daemon names the stored file from
    # it: a `.png` holding JPEG bytes would be a name that lies.
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")
    script = """
%s

console.log(JSON.stringify([
  markedName('photo.jpeg', 'image/jpeg'),
  markedName('shot.png', 'image/png'),
  markedName('shot.png'),
]))
""" % _function("function markedName(")
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert json.loads(result.stdout) == [
        "photo-marked.jpg", "shot-marked.png", "shot-marked.png",
    ]


def test_the_marked_copy_does_not_inherit_the_upload_memo():
    """Slice 1 remembers an upload against a tray entry so a retry does not store
    a second copy. Carried onto the replacement, that memo made a send after a
    partial failure name the **unmarked** original while the tray showed
    `…-marked.png` (!273 review)."""
    chat = _chat_source()
    replace = chat[chat.index("function useMarked("):]
    replace = replace[:replace.index("\n}\n")]
    assert "stored: _replaced" in replace, "the memo is carried onto new bytes"


def test_it_is_drawable_with_a_finger():
    """Without `touch-action: none` a drag scrolls the dialog instead of drawing,
    which is the whole feature not working on the device it is for."""
    assert "touch-action: none" in _rule(".sketch-canvas")
    source = _source()
    for handler in ("@pointerdown", "@pointermove", "@pointerup", "@pointercancel"):
        assert handler in source, f"{handler} is not bound — a drag would be lost"
    assert "setPointerCapture" in _function("function onDown("), (
        "a drag that leaves the canvas leaves a half-drawn mark"
    )


def test_the_controls_are_thumb_targets():
    """The app's one-handed rule, which this dialog is squarely inside: it is
    used by an operator holding a phone in one hand."""
    source = _source()
    actions = source[source.index("<v-card-actions"):]
    assert actions.count('size="large"') >= 3
    assert 'aria-label' in actions


def test_the_image_never_pushes_the_dialog_sideways():
    """A screenshot is routinely wider than a phone, and a canvas has an
    intrinsic size."""
    rule = _rule(".sketch-canvas")
    assert "max-width: 100%" in rule
    assert "dvh" in rule, "a viewport bound in vh alone is wrong on a phone"
    assert "height: auto" in rule, "the aspect ratio is not kept"


def test_the_editor_is_a_modal_not_a_full_screen_takeover():
    """Marking up a screenshot is work done *about* a conversation, so the
    conversation stays on screen behind it — the operator asked for this after
    using the full-screen version (!273 followup). It is also the shape every
    other dialog in the app already has: centered, scrimmed, and dismissable by
    the scrim or Escape as well as by the close button."""
    dialog = _source()
    dialog = dialog[dialog.index("<v-dialog"):dialog.index("<v-card>")]
    assert "fullscreen" not in dialog, (
        "a full-screen dialog paints over the conversation being reported"
    )
    assert re.search(r'max-width="\d+"', dialog), (
        "with no width bound the dialog grows back out to the viewport"
    )
    assert "persistent" not in dialog, (
        "the scrim and Escape are two of the three ways out"
    )
    # And the canvas leaves the page room around the dialog rather than taking
    # the height the full-screen version could.
    bound = re.search(r"max-height: (\d+)dvh", _rule(".sketch-canvas"))
    assert bound and int(bound.group(1)) < 100, (
        "a canvas bounded at the whole viewport is the full-screen dialog again"
    )


def test_the_editor_is_its_own_chunk():
    """Most sends never open it, and the fleet view is the landing screen — the
    same argument the renderer and the terminal are split on."""
    chat = _chat_source()
    assert "defineAsyncComponent(() => import('./SketchPad.vue'))" in chat
    assert 'v-if="marking !== null"' in chat, (
        "the dialog is mounted for every conversation, so the chunk is fetched "
        "whether or not anybody marks anything up"
    )


# --- executed: the component, mounted the way the composer mounts it ---------
#
# The !273 review found a defect no test in this module could see: the dialog
# never loaded its image, so nothing could be drawn and *done* was disabled
# forever — and the pipeline was green on that commit, because every guard here
# read source or ran an extracted pure function. Neither shape can observe a
# lifecycle.
#
# This one mounts the real component. Vue's **server** renderer is what makes it
# possible without a browser: `renderToString` runs `setup()`, which is where an
# immediate watcher fires, and the defect is precisely that the watcher was not
# immediate while the composer mounts the component already open (`v-if` plus
# `:model-value="true"`). The browser APIs the load path touches are stubbed, so
# what is asserted is that the path was *reached* at mount — the pixels are the
# browser rig's business (docs in the run's repro notes).

_MOUNT_PROBE = """
import { readFileSync } from 'node:fs'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { parse, compileScript } from '@vue/compiler-sfc'

const path = 'src/components/SketchPad.vue'
const { descriptor } = parse(readFileSync(path, 'utf8'), { filename: path })
const compiled = compileScript(descriptor, { id: 'probe', inlineTemplate: true })

// Everything the load path reaches for that node does not have. Recorded rather
// than faked away: reaching them *is* the assertion.
const calls = []
globalThis.URL.createObjectURL = () => { calls.push('createObjectURL'); return 'blob:probe' }
globalThis.URL.revokeObjectURL = () => {}
globalThis.Image = class { set src(_value) { calls.push('decode') } }
globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' })
globalThis.document = { documentElement: {} }

// A data: URL module cannot resolve a bare specifier, so the compiled component's
// imports are rewritten to the URLs this rig resolved them to.
const source = compiled.content
  .replace(/from ['"]vue['"]/g, `from '${import.meta.resolve('vue')}'`)
  .replace(/from ['"]@mdi\\/js['"]/g, `from '${import.meta.resolve('@mdi/js')}'`)
const component = (await import(
  `data:text/javascript;base64,${Buffer.from(source, 'utf8').toString('base64')}`
)).default

// Mounted the way Chat.vue mounts it: open from the first frame.
const app = createSSRApp({
  render: () => h(component, {
    modelValue: %s,
    file: { name: 'shot.png', type: 'image/png' },
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


def _mounted(open_at_mount="true"):
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")
    result = subprocess.run(
        [node, "--input-type=module", "-e", _MOUNT_PROBE % open_at_mount],
        capture_output=True, text=True, timeout=180, cwd=str(WEB),
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout)["calls"]


def test_mounting_the_dialog_open_loads_the_image():
    """The one the !273 review had to run a browser to find. The composer mounts
    this component under a `v-if` with `:model-value="true"`, so it comes into
    existence already open and a watcher on that prop has no transition to
    observe — the canvas stayed at its 300x150 default, no drag could record a
    mark and *done* was disabled forever."""
    assert _mounted("true") == ["createObjectURL", "decode"], (
        "the image is not loaded when the component is mounted already open"
    )


def test_mounting_it_closed_loads_nothing():
    """The other half, so the fix cannot become "load whatever happens": a caller
    that keeps the component mounted and toggles `v-model` must not decode an
    image nobody is looking at."""
    assert _mounted("false") == []


# --- executed: the two things markup cannot show -----------------------------

def _probe(script):
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout)


def test_a_mark_lands_where_the_finger_was_whatever_size_the_canvas_is_drawn():
    """The mapping every mark passes through. A 3000x2000 screenshot on a 390px
    phone is a ~7.7x reduction, so an unmapped pointer would put a mark in the
    top-left corner of the image and nothing on screen would explain why."""
    script = """
%s

const phone = { left: 0, top: 100, width: 390, height: 260 }
const natural = { width: 3000, height: 2000 }
console.log(JSON.stringify({
  centre: toNatural(195, 230, phone, natural),
  origin: toNatural(0, 100, phone, natural),
  corner: toNatural(390, 360, phone, natural),
  offCanvas: toNatural(-40, 40, phone, natural),
  beyond: toNatural(4000, 4000, phone, natural),
  unlaidOut: toNatural(10, 10, { left: 0, top: 0, width: 0, height: 0 }, natural),
}))
""" % _function("function toNatural(")
    seen = _probe(script)
    assert seen["centre"] == {"x": 1500, "y": 1000}
    assert seen["origin"] == {"x": 0, "y": 0}
    assert seen["corner"] == {"x": 3000, "y": 2000}
    # Clamped rather than drawn into the margins, in both directions.
    assert seen["offCanvas"] == {"x": 0, "y": 0}
    assert seen["beyond"] == {"x": 3000, "y": 2000}
    # An unreachable case pinned as one: a 0x0 element is not hit-testable, so no
    # pointer event can arrive while the canvas has no layout. What the earlier
    # version of this assertion claimed — that the unguarded arithmetic would put
    # every mark at the origin — is not what it does (dividing by zero gives
    # Infinity, which the clamp turns into the far corner, or NaN). The branch is
    # kept and pinned; the reasoning for it is not (!273 review).
    assert seen["unlaidOut"] == {"x": 0, "y": 0}


def test_the_marked_up_copy_is_a_file_you_can_tell_apart():
    """It arrives beside the original in the chat and in the store, so "the same
    file again" is the wrong answer."""
    script = """
%s

console.log(JSON.stringify([
  markedName('shot.png'),
  markedName('screen shot.jpeg'),
  markedName('no-extension'),
  markedName(undefined),
]))
""" % _function("function markedName(")
    assert _probe(script) == [
        "shot-marked.png",
        "screen shot-marked.png",
        "no-extension-marked.png",
        "image-marked.png",
    ]
