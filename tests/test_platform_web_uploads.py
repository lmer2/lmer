"""Guards on attaching a file to a chat message (issue #246).

Source-level, like every other web test here: there is no JS runner and no
browser in this image (:mod:`tests.test_platform_web_app`), so what is pinned is
the markup and the order of the two calls that make a send safe.

Both chat surfaces are one component — ``Chat.vue`` is the run's pane *and* the
uber lmer drawer's — which is the issue's "both surfaces share one mechanism"
holding structurally rather than by agreement between two implementations. So
these guards are on that one file, and the drawer test module is where the
surfaces are checked to still be the same one.

Three properties are worth stating out loud, because each of them fails quietly:

- **All three ways in exist.** Drag-and-drop, paste and the file picker were
  asked for together, and the picker is the phone's camera roll — the device this
  fleet is driven from. A missing one is not a broken feature, it is a feature the
  operator concludes does not work.
- **The upload happens before the message is typed.** The message names the paths
  the files were stored at, so a message sent while an upload had failed points
  the agent at a file that is not there.
- **The picker's ``accept`` is a superset of what the daemon takes.** It is a hint
  to the OS dialog, not a check — a copy of the allowlist that drifted *narrow*
  would hide an allowed file on a phone with nothing to explain why, while a wide
  one can only offer a file the daemon then refuses with a sentence this pane
  shows.
"""

import json
import re
import subprocess
from pathlib import Path

from lmer_platform import uploads
from tests.conftest import node_binary, require_node_toolchain
# Imported rather than copied, for the reason the neighbouring modules give: a
# second copy of a helper that locates source is how a guard starts passing
# against the wrong text.
from tests.test_platform_web_app import _chat_function, _chat_source
from tests.test_platform_web_chat import _chat_body

WEB = Path(__file__).resolve().parent.parent / "web"
CHAT = WEB / "src" / "components" / "Chat.vue"
API = WEB / "src" / "api.js"


def _read(path):
    return path.read_text(encoding="utf-8")


# --- the three ways in --------------------------------------------------------

def test_a_file_can_be_picked_from_the_device():
    """Which on a phone is the camera roll, and is why this is not the
    afterthought a file input usually is."""
    source = _chat_source()
    assert 'type="file"' in source, "no file picker at all"
    assert "multiple" in source, "one file per message is not what was asked for"
    assert 'ref="picker"' in source and "picker?.click()" in source, (
        "a bare file input is not a tap target — it is driven from the button"
    )


def test_the_attach_control_is_a_thumb_target_like_send():
    """The one-handed-use rule this pane already lives under (issue #194): a
    labelled control, on the send row, at the size the HIGs agree on."""
    source = _chat_source()
    row = source[source.index('<div class="send-row">'):]
    row = row[:row.index("</div>")]
    assert "mdiPaperclip" in row, "the attach control is not on the send row"
    assert 'size="large"' in row, "smaller than the app's own minimum target"
    assert "aria-label" in row


def test_a_paste_carrying_a_file_attaches_it():
    """The fastest report from a phone is a screenshot, and paste is how one
    arrives. Text pasted into the box has to stay text, so the handler takes the
    default away only when there are files."""
    assert '@paste="onPaste"' in _chat_source()
    handler = _chat_function("function onPaste(")
    assert "clipboardData?.files" in handler
    assert re.search(r"if \(!files\?\.length\) return", handler), (
        "a paste of plain text would stop working"
    )
    assert handler.index("return") < handler.index("preventDefault"), (
        "preventDefault runs before the no-files case is let through"
    )


def test_the_composer_is_the_drop_target():
    """A screenshot is dragged at the place you were about to type. `.prevent` on
    dragover is what stops the browser navigating away to the file instead."""
    source = _chat_source()
    assert '@drop.prevent="onDrop"' in source
    assert '@dragover.prevent' in source, (
        "without it the browser opens the file instead of dropping it"
    )
    assert 'class="composer"' in source


def test_what_is_staged_is_visible_before_it_is_sent():
    """An attachment nobody can see is one an operator cannot decide to remove."""
    source = _chat_source()
    assert 'class="attachment-thumb"' in source, "no preview of an attached image"
    assert "unstage(item.id)" in source, "nothing removes a staged file"
    assert 'class="attachments' in source


# --- sending ------------------------------------------------------------------

def test_the_files_go_before_the_message_does():
    """The message names the paths, so typing first would point the agent at a
    file that may never have been stored."""
    send = _chat_function("async function send(")
    assert "uploadSessionFile" in send, "the composer never uploads anything"
    assert send.index("uploadSessionFile") < send.index("sendSessionInput"), (
        "a failed upload would still have sent the message naming its file"
    )


def test_a_message_may_be_nothing_but_a_file():
    """A screenshot on its own is a report. Requiring words with it would make
    the fastest thing to send the one thing you cannot."""
    send = _chat_function("async function send(")
    assert "!typed && !staged.length" in send
    source = _chat_source()
    assert ':disabled="!draft.trim() && !attachments.length"' in source, (
        "send stays disabled for a message that is only an attachment"
    )


def test_the_reference_line_is_the_daemon_s_and_not_this_end_s():
    """One wording, composed where the file was stored. This end includes it
    verbatim rather than assembling a second copy — the path, the type and the
    size are the daemon's to phrase."""
    send = _chat_function("async function send(")
    assert "stored.reference" in send
    source = _chat_source()
    assert "container_path" not in source, (
        "the pane assembles its own copy of the reference line"
    )


def test_a_failed_upload_sends_nothing_and_keeps_the_draft():
    """The tray and the draft are cleared after the send, not before it: an
    operator whose upload failed still has their words and their file."""
    send = _chat_function("async function send(")
    clearing = send.index("draft.value = ''")
    assert send.index("sendSessionInput") < clearing
    # The tray is filtered — not emptied — and only after the send went; see
    # test_the_send_captures_the_ids_it_sends_and_clears_only_those.
    assert send.index("!sent.has(item.id)") > clearing, (
        "the tray is cleared somewhere other than after a successful send"
    )


def test_the_bubble_shows_what_was_attached():
    """In the seconds before the transcript catches up, from the file still in
    the browser — no route, no round trip."""
    source = _chat_source()
    assert "item.attachments?.length" in source
    assert 'class="attached-thumb"' in source


def test_the_previews_are_released_when_the_bubble_goes():
    """An object URL holds the file alive until it is revoked, and a long session
    of pasting screenshots is exactly where that adds up."""
    source = _chat_source()
    assert source.count("URL.revokeObjectURL") >= 2
    for site in ("function settlePending(", "function dismissPending(",
                 "async function start("):
        # ``release`` either called or handed to `forEach` — what matters is
        # that the bubble's previews go with the bubble.
        assert "release" in _chat_function(site), (
            f"a bubble dropped in {site} leaks its preview"
        )


# --- the bound ----------------------------------------------------------------

def test_a_dropped_folder_cannot_become_a_hundred_uploads():
    """And the bound is said out loud: silently keeping the first few reads as
    all of them having been attached."""
    stage = _chat_function("function stage(")
    assert "MAX_ATTACHMENTS" in stage
    assert "problem.value" in stage, "the cap is applied without saying so"


# --- what the picker offers ---------------------------------------------------

def test_the_accept_hint_covers_every_type_the_daemon_can_take():
    """A hint, and a superset: narrower and an allowed file is missing from the
    picker on a phone, with nothing to explain why."""
    source = _chat_source()
    declaration = source[source.index("const ACCEPT = ["):]
    declaration = declaration[:declaration.index("].join")]
    for kind in uploads.KNOWN_TYPES.values():
        # The charset the text type carries is a response header's business, not
        # an accept token's.
        media_type = kind.content_type.split(";")[0]
        assert media_type in declaration, (
            f"{kind.name} is accepted by the daemon and hidden by the picker"
        )


def test_the_client_holds_no_second_copy_of_the_policy():
    """The size cap and the allowlist are the daemon's, and its refusal names
    both. A copy here is a second opinion that drifts silently."""
    source = _chat_source()
    assert "max_bytes" not in source and "MAX_BYTES" not in source
    assert "upload_types" not in source


# --- the client ---------------------------------------------------------------

# --- what the !272 review found ----------------------------------------------

def test_the_send_captures_the_ids_it_sends_and_clears_only_those():
    """The tray is replaced, never mutated, so holding the array meant a file
    removed mid-send was uploaded anyway and a file dropped mid-send was thrown
    away unsent. Both were silent."""
    send = _chat_function("async function send(")
    assert "[...attachments.value]" in send, (
        "the tray is captured by reference again"
    )
    assert "attachments.value = []" not in send, (
        "the whole tray is cleared, so a file staged during the send is lost"
    )
    assert "!sent.has(item.id)" in send


def test_the_tray_cannot_be_edited_while_a_send_is_in_flight():
    """Otherwise ✕ removes the row while that file's upload carries on and its
    path goes into the message — the pane saying it was pulled back when it was
    not."""
    source = _chat_source()
    row = source[source.index('class="attachment"'):]
    row = row[:row.index("</div>")]
    removes = [line for line in row.splitlines() if "unstage(item.id)" in line]
    assert removes, "no remove control to check"
    assert row.count(':disabled="sending"') >= 1, (
        "the remove control stays live during a send"
    )


def test_a_file_is_uploaded_once_across_retries():
    """A send refused on its second file leaves the first stored; sending again
    used to store it a second time under a new name, so one screenshot became two
    copies in a directory the agent is told to organise."""
    send = _chat_function("async function send(")
    assert "item.stored?.session !== target" in send, (
        "the memo is either gone or no longer keyed to the session that made it"
    )
    assert "const target = props.sessionId" in send, (
        "the session is read more than once across the send's awaits"
    )
    assert "uploadSessionFile(target" in send and "sendSessionInput(target" in send
    assert "item.stored.reference" in send


def test_a_session_switch_mid_send_does_not_leave_the_composer_disabled():
    """`finally` is guarded by `stale(mine)`, so the flag survives the switch —
    and the field is `:disabled="sending"`."""
    assert "sending.value = false" in _chat_function("async function start(")


def test_a_thumbnail_is_asked_of_the_session_that_received_the_file():
    """A run's history spans every session it has had while a worker's store is
    per session, so resolving every thumbnail against the current session breaks
    every upload made before a resume — the ordinary life of a run."""
    resolver = _chat_function("function uploadUrl(")
    assert "sessionByOrigin" in resolver
    assert "props.sessionId" in resolver, "no fallback for a turn with no origin"
    map_source = _chat_function("const sessionByOrigin = computed(")
    assert "source.session" in map_source and "source.id" in map_source
    assert 'uploadUrl(name, message)' in _chat_source()


def test_the_404_memory_is_keyed_per_store_not_per_name():
    """Two sessions of one run can hold files of the same name, and a 404 in one
    store says nothing about the other."""
    assert "uploadUrl(name, message)" in _chat_source()
    assert "missingUploads.has(uploadUrl(" in _chat_source()


def test_a_stray_drop_anywhere_on_the_page_is_a_no_op():
    """The browser's default for a drop on a page is to navigate to the file, so
    a screenshot released on the message list unloaded the app and took the
    draft, the tray and every unsettled bubble with it."""
    app = (WEB / "src" / "App.vue").read_text(encoding="utf-8")
    assert "swallowStrayDrop" in app
    for event in ("'dragover'", "'drop'"):
        assert f"window.addEventListener({event}, swallowStrayDrop)" in app
        assert f"window.removeEventListener({event}, swallowStrayDrop)" in app, (
            "the listener outlives the component that added it"
        )
    assert "event.preventDefault()" in app


def test_the_api_client_sends_base64_and_never_a_data_url_header():
    """The daemon takes base64; a data URL's ``data:image/png;base64,`` prefix is
    not base64 and would be refused as a corrupt payload."""
    assert "uploadSessionFile" in _read(API)
    reader = _chat_function("function base64Of(")
    assert "indexOf(','" in reader, "the data-URL header would be sent as content"
    assert "readAsDataURL" in reader


# --- the send, executed ------------------------------------------------------
#
# Everything above reads source. This part runs the real ``send()`` over a staged
# tray, because the two things that matter about it are not visible in the text:
# what the composed message *is*, and what is left behind when an upload fails.
# The browser-only halves are the harness's stubs — ``base64Of`` needs a
# FileReader and ``uploadSessionFile`` needs the daemon — and they are stubbed
# rather than extracted, exactly as ``sendSessionInput`` already is in
# :mod:`tests.test_platform_web_chat`.

_UPLOAD_SEND_PROBE = """
const props = { sessionId: 'probe-session' }
let cursor = 7
const draft = { value: '' }
const sending = { value: false }
const problem = { value: null }
const pending = { value: [] }
const following = { value: false }
const attachments = { value: [] }
let bubbleId = 0
let uploads = 0
let failing = false
// Which staged file the daemon refuses, by name — the partial-failure case the
// first version of this probe could not express (it only failed the first).
let refuse = null
// A file the operator pastes *while* the upload loop is running.
let stageDuring = null
const uploaded = []
// The same calls, qualified by the session they were aimed at: a store is per
// session, so "was this uploaded" and "was this uploaded *here*" are different
// questions and only the second one catches a memo that crossed a switch.
const uploadTargets = []
const sent = []
// Real, not a stub: what the turnover above is testing is that the send aborts.
const stale = (mine) => mine !== generation
const nextTick = () => {}
const stickToBottom = () => {}
const poll = () => {}
const release = () => {}
// A browser's FileReader is what the component uses; here the payload is beside
// the point — what is being asked is what the message ends up saying.
const base64Of = async () => 'AAAA'
// A run turning over *during* an upload: the prop changes, the generation bumps
// and `sending` clears — what start() does on a session switch. Armed per case,
// and once, so the send that follows the turnover runs normally.
let turnOverDuringUpload = null
const uploadSessionFile = async (session, body) => {
  uploads += 1
  uploaded.push(body.name)
  uploadTargets.push(`${session}:${body.name}`)
  if (turnOverDuringUpload) {
    props.sessionId = turnOverDuringUpload
    turnOverDuringUpload = null
    generation += 1
    sending.value = false
  }
  if (stageDuring) {
    // Exactly what a paste mid-send does: the tray is *replaced*, so a captured
    // array would not see this and a whole-tray clear would discard it.
    attachments.value = [...attachments.value, {
      id: 900, file: {}, name: stageDuring, size: 10, preview: null,
    }]
    stageDuring = null
  }
  if (failing || body.name === refuse) throw new Error('that file is too big')
  return {
    name: body.name,
    reference: `[lmer upload] /home/developer/.lmer-uploads/${body.name} (image/png, 1.0 KB)`,
  }
}
const sendSessionInput = async (_session, text) => {
  sent.push(text)
  return { submit_confirmed: true }
}
let generation = 0

%s

const cases = %s
;(async () => {
  const seen = {}
  for (const [name, probe] of Object.entries(cases)) {
    uploads = 0
    uploaded.length = 0
    uploadTargets.length = 0
    sent.length = 0
    // Each case starts on its own session, because one of them changes it.
    props.sessionId = 'probe-session'

    problem.value = null
    pending.value = []
    failing = !!probe.failing
    refuse = probe.refuse || null
    stageDuring = probe.stageDuring || null
    turnOverDuringUpload = probe.turnOverDuringUpload || null
    generation = 0
    draft.value = probe.typed
    attachments.value = (probe.attached || []).map((file, index) => ({
      id: index + 1, file: {}, name: file, size: 10, preview: null,
    }))
    await send()
    // The memo as the *first* send left it. Read here because a second send that
    // succeeds clears the tray, and this is the value the next send believes.
    const memosAfterFirst = attachments.value.map((item) => item.stored?.session || null)
    if (probe.thenRetry) {
      // The operator drops the refused file and sends again — the case that used
      // to store the first file a second time.
      refuse = null
      if (probe.switchTo) {
        // ...having gone to look at another run first. `start()` runs on that
        // switch and deliberately leaves the tray alone, like the draft.
        props.sessionId = probe.switchTo
        sending.value = false
      }
      attachments.value = attachments.value.filter(
        (item) => item.name !== probe.refuse,
      )
      await send()
    }
    seen[name] = {
      uploads,
      // Copied, not the array itself: the next case empties it, and a reference
      // would report every earlier case as having sent nothing.
      sent: [...sent],
      uploaded: [...uploaded],
      uploadTargets: [...uploadTargets],
      sentTo: sent.map(() => props.sessionId),
      memosAfterFirst,
      draft: draft.value,
      staged: attachments.value.map((item) => item.name),
      problem: problem.value,
      bubbles: pending.value.map(
        (item) => ({ text: item.text, attachments: item.attachments }),
      ),
    }
  }
  console.log(JSON.stringify(seen))
})()
"""

_CASES = {
    # The ordinary paste-and-say-something.
    "a file with words": {"typed": "look at this", "attached": ["shot.png"]},
    # A screenshot on its own — the fastest report there is.
    "a file alone": {"typed": "", "attached": ["shot.png"]},
    # Two, because a before-and-after pair is one report.
    "two files": {"typed": "", "attached": ["before.png", "after.png"]},
    # And the failure that must not half-deliver.
    "an upload that fails": {
        "typed": "look at this", "attached": ["shot.png"], "failing": True,
    },
    # The partial failure the !272 review named: the first file is stored, the
    # second is refused, and the operator drops it and sends again.
    "a refused second file, then a retry": {
        "typed": "look", "attached": ["shot.png", "report.pdf"],
        "refuse": "report.pdf", "thenRetry": True,
    },
    # The same partial failure, with a look at another run in the middle — the
    # memo used to survive that and skip the upload the new session never got.
    "a refused second file, a session switch, then a retry": {
        "typed": "look", "attached": ["shot.png", "report.pdf"],
        "refuse": "report.pdf", "thenRetry": True, "switchTo": "other-session",
    },
    # The run turns over while the bytes are in flight, and the operator sends the
    # still-staged file afterwards. Two sends: the first aborts on the stale
    # guard, the second runs in the session the view moved to.
    "a turnover during the upload, then a send": {
        "typed": "look", "attached": ["shot.png"],
        "turnOverDuringUpload": "other-session", "thenRetry": True,
    },
    # A paste that lands while the upload loop is running.
    "a file staged during the send": {
        "typed": "look", "attached": ["shot.png"], "stageDuring": "late.png",
    },
}


def _sends():
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")
    script = _UPLOAD_SEND_PROBE % (
        _chat_body("async function send("), json.dumps(_CASES),
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=120, cwd=str(WEB),
    )
    assert result.returncode == 0, (
        f"the upload-send probe failed:\n{result.stdout}\n{result.stderr}"
    )
    return json.loads(result.stdout)


def test_the_message_leads_with_the_files_and_then_your_words():
    """One turn, not two: the agent reads the paths and the sentence about them
    together, and a separate message for each would be two turns to correlate."""
    case = _sends()["a file with words"]
    assert case["uploads"] == 1
    assert len(case["sent"]) == 1
    text = case["sent"][0]
    assert text.startswith(uploads.REFERENCE_PREFIX)
    assert text.endswith("look at this")
    assert "shot.png" in text


def test_a_screenshot_on_its_own_is_a_message():
    case = _sends()["a file alone"]
    assert len(case["sent"]) == 1
    assert case["sent"][0].strip().startswith(uploads.REFERENCE_PREFIX)


def test_every_attachment_is_named_in_the_one_message():
    """A pair of screenshots is one report; naming only the last would send the
    agent looking for the other."""
    case = _sends()["two files"]
    assert case["uploads"] == 2
    assert len(case["sent"]) == 1
    assert case["sent"][0].count(uploads.REFERENCE_PREFIX) == 2


def test_the_bubble_carries_the_attachments_it_sent():
    case = _sends()["a file with words"]
    assert [item["name"] for item in case["bubbles"][0]["attachments"]] == ["shot.png"]


def test_the_tray_and_the_draft_are_emptied_only_on_a_send_that_went():
    sent, failed = _sends()["a file with words"], _sends()["an upload that fails"]
    assert (sent["draft"], sent["staged"]) == ("", [])
    assert failed["draft"] == "look at this", "the operator's words were thrown away"
    assert failed["staged"] == ["shot.png"], "the file was dropped from the tray"


def test_a_file_already_stored_is_not_stored_again_on_the_retry():
    """The partial failure (!272 review): the first file is on the host, the
    second is refused, and the operator drops it and sends again. Storing the
    first a second time leaves one screenshot as two copies of itself in a
    directory the agent is told to organise."""
    case = _sends()["a refused second file, then a retry"]
    # Attempt one uploads both (the second throws); attempt two uploads nothing —
    # the survivor's reply was remembered.
    assert case["uploaded"] == ["shot.png", "report.pdf"], case["uploaded"]
    assert len(case["sent"]) == 1, "the retry did not go, or went twice"
    assert case["sent"][0].count(uploads.REFERENCE_PREFIX) == 1
    assert "shot.png" in case["sent"][0]
    assert case["staged"] == []


def test_a_staged_file_that_crosses_a_session_switch_is_uploaded_again():
    """The tray survives a session switch, like the draft, and an upload does not:
    a store is per session. The memo made in one run skipped the upload in the
    next and handed its agent a path that exists in the *previous* run's
    container — indistinguishable from a good one, since the container path is the
    same in both (!272 review round 2)."""
    case = _sends()["a refused second file, a session switch, then a retry"]
    assert case["uploadTargets"] == [
        "probe-session:shot.png",
        "probe-session:report.pdf",
        "other-session:shot.png",
    ], case["uploadTargets"]
    assert len(case["sent"]) == 1
    assert case["sent"][0].count(uploads.REFERENCE_PREFIX) == 1


def test_a_memo_is_reused_only_within_the_session_that_made_it():
    """The other half of the same rule, so neither can be "fixed" into the other:
    a retry in the *same* session must still not store a second copy."""
    case = _sends()["a refused second file, then a retry"]
    assert case["uploadTargets"] == [
        "probe-session:shot.png", "probe-session:report.pdf",
    ], case["uploadTargets"]


def test_a_memo_names_the_session_the_bytes_actually_went_to():
    """`props.sessionId` was read once to upload to and again to label with, with
    a round trip in between — so a turnover landing inside the upload window left
    a memo claiming an upload that never happened in that session, and the next
    send believed it (!272 review round 3). One read before the await is what
    makes the call and the label the same fact."""
    case = _sends()["a turnover during the upload, then a send"]
    # The first send uploaded to the session it was started in and stamped that
    # same one, then aborted on the stale guard; the second re-uploaded, because
    # the memo correctly says the file has not been to the new session.
    assert case["uploadTargets"] == [
        "probe-session:shot.png", "other-session:shot.png",
    ], case["uploadTargets"]
    assert case["memosAfterFirst"] == ["probe-session"], (
        "the aborted send stamped the memo with the session the view moved to, "
        "not the one the bytes went to"
    )
    assert len(case["sent"]) == 1, "the aborted send typed at the session anyway"


def test_a_file_pasted_during_the_send_is_kept_rather_than_discarded():
    """It has not been sent, so clearing the whole tray afterwards would throw it
    away with no message at all."""
    case = _sends()["a file staged during the send"]
    assert case["uploaded"] == ["shot.png"], "the late file was swept into the send"
    assert case["staged"] == ["late.png"], "the late file was discarded unsent"
    assert case["sent"][0].count(uploads.REFERENCE_PREFIX) == 1


def test_a_failed_upload_types_nothing_at_the_session():
    """The whole reason the upload goes first: a message naming a file that was
    never stored points the agent at a path that is not there."""
    case = _sends()["an upload that fails"]
    assert case["sent"] == []
    assert case["bubbles"] == [], "a bubble claims the message was delivered"
    assert "too big" in (case["problem"] or ""), (
        "the daemon's own sentence is what says why — it names the limit"
    )


# --- the same line, recognised on the way back --------------------------------

def test_the_marker_the_pane_matches_is_the_one_the_daemon_writes():
    """The one literal the two ends share, so a change to the wording on the
    daemon's side fails here rather than silently ending the thumbnails."""
    pattern = _chat_function("function uploadsIn(")
    source = _chat_source()
    declaration = source[source.index("const UPLOAD_REFERENCE ="):]
    declaration = declaration[:declaration.index("\n")]
    # The regex escapes the brackets, so the marker is compared without them.
    marker = uploads.REFERENCE_PREFIX.strip("[]")
    assert marker in declaration, (
        f"the pane matches something other than {uploads.REFERENCE_PREFIX!r}"
    )
    assert "matchAll" in pattern


def test_only_images_are_drawn_and_only_from_your_own_turns():
    """Everything else in a store is a file to open. And a line in an agent's
    turn is a quotation — the attachments are what this pane put in a message."""
    assert "type.startsWith('image/')" in _chat_function("function uploadsIn(")
    visible = _chat_function("const visible = computed(")
    assert "message.role === 'user'" in visible


def test_a_thumbnail_is_asked_for_by_name_not_by_path():
    """The read route is scoped to this session's own store and matches the name
    against what is in it, so sending a path would be sending something the
    route has no use for — and inviting one to be built by hand later."""
    pattern = _chat_function("function uploadsIn(")
    assert "lastIndexOf('/')" in pattern
    assert "sessionUploadUrl" in _read(API)


def test_a_file_the_store_no_longer_holds_leaves_the_path_behind():
    """A worker that copied its uploads into its work dir and deleted them is
    the ordinary case, not an error state: the image goes, the turn's text
    stays."""
    source = _chat_source()
    assert '@error="noteMissing(' in source
    assert "missingUploads.has(" in source


def test_the_pane_recognises_a_line_the_daemon_actually_composed():
    """The contract between the two ends, executed rather than read: the line is
    built by :func:`lmer_platform.uploads.reference_line` and matched by the
    component's own function. Both sides are source-level guarded above; this is
    the one that fails if either spelling drifts.
    """
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")
    lines = [
        uploads.reference_line(uploads.StoredUpload(
            session_id="probe", name="20260826-131500-shot.png",
            path=Path("/dev/null"), bytes=42_233, kind="png",
            content_type="image/png",
            container_path=f"{uploads.CONTAINER_UPLOADS_DIR}/20260826-131500-shot.png",
        )),
        # A text file, which is a file to open rather than a picture to draw.
        uploads.reference_line(uploads.StoredUpload(
            session_id="probe", name="20260826-131501-notes.txt",
            path=Path("/dev/null"), bytes=900, kind="txt",
            content_type="text/plain; charset=utf-8",
            container_path=f"{uploads.CONTAINER_UPLOADS_DIR}/20260826-131501-notes.txt",
        )),
    ]
    message = "\n\n".join([*lines, "the pane looks like this on my phone"])
    script = """
%s

%s

console.log(JSON.stringify(uploadsIn(%s)))
""" % (
        _chat_declaration("const UPLOAD_REFERENCE ="),
        _chat_body("function uploadsIn("),
        json.dumps(message),
    )
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert json.loads(result.stdout) == ["20260826-131500-shot.png"]


def _chat_declaration(prefix):
    """One top-level ``const`` line from Chat.vue's script, whole."""
    source = _chat_source()
    start = source.index(prefix)
    return source[start:source.index("\n", start)]
