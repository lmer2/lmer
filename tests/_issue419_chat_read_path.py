"""Acceptance tests for the chat pane's read path — issue 419. NOT COLLECTED.

`pyproject.toml` collects `test_*.py`, so nothing here runs in the suite. That is
deliberate and it is not a quarantine for flakiness: these drive a client that
does not exist yet. Run them with the path spelled out, which collects a file
regardless of `python_files`:

    pytest tests/_issue419_chat_read_path.py

**25 fail and 3 pass**, measured at the split (`7952cf7a`), and both halves of
that are meant. The 25 fail at extraction or on the rework's own behaviour —
`_function("place")`, `_function("reload")` and `_constants()`'s
`OVERLAP`/`MAX_PAGE` all read a `Chat.vue` whose window and paging rework was
taken back out of !288 (#407) and handed to #419.

The 3 that pass are the first three `settlePending` anchor cases, and they pass
for a reason that has nothing to do with an anchor: the restored rule is
`message.seq >= item.since`, and each of them puts the older identical turn
*below* the bubble's cursor, so the cursor alone refuses it. They are not
evidence that the anchor works — the anchor does not exist. The fourth,
`..._settles_against_its_own_turn`, fails on its second assertion because
`consumed` is keyed by `seq` again, not by `id`.

A #419 session that runs this file and sees three green should read that as
intact, not as half-applied. What the restored rule does today has its own
collected cover in `tests/test_web_chat_merge.py`
(`test_an_older_identical_turn_does_not_settle_a_bubble` and
`test_a_bubble_settles_against_its_own_turn`), so these four are free to change
shape with the design.

The pane holds a partial window of a transcript that changes underneath it three
ways at once: turns append, the join replaces a harness prose turn with the bus
record that mirrors it, and turns are deleted. Reading that window by position
means every change shifts what is already held, and eight review rounds went into
inferring the shift from the page — each patch satisfying one invariant and
breaking another. #419 asks for a read path that never reasons about positions;
these are what it has to satisfy. The attempt history is in the run dir's
`handover.md`, and the review reports are under
`runs/review-mr-288--review-mr-288/reports/`.

What !288 kept is the half that needs none of this: the server puts a stable `id`
on every turn (`lmer_platform.transcripts`), and the pane drops a page's turn it
already holds by that id. That is `tests/test_web_chat_merge.py`, which does run.

What this runs is the function as it stands in `Chat.vue` — extracted from the
file and executed in node against a `messages` shim — so an edit to it is an edit
to what is tested. What it does **not** cover is the component around it: there is
no DOM runtime in `web/` (SSR runs `setup()` but never `onMounted`, which is where
the poll lives), so the wiring from poll to `absorb` to `merged` is held by the
source-level guards in `test_platform_conversation_merge.py`.

A generator caveat that cost two review rounds, also written into the sweep's own
docstring: an edit outside the range a request covers cannot be placed from that
one page by any rule, and a turn the server deletes cannot settle a bubble.
Sweeping either grades the generator, not the code.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

from lmer_platform import transcripts

from tests.conftest import node_binary, require_node_toolchain

WEB = Path(__file__).resolve().parent.parent / "web"
CHAT = WEB / "src" / "components" / "Chat.vue"


def _constants():
    """The paging constants as `Chat.vue` declares them.

    Read from the file rather than restated here: a harness that pins its own
    copy tests numbers the component does not use, and a constant the extracted
    code newly depends on would surface as a ReferenceError inside node rather
    than as a missing definition.
    """
    text = CHAT.read_text(encoding="utf-8")
    found = dict(re.findall(r"^const ([A-Z_]+) = (\d+)$", text, re.MULTILINE))
    missing = {"PAGE", "OVERLAP", "INITIAL_TAIL", "MAX_PAGE"} - set(found)
    assert not missing, f"Chat.vue no longer declares {sorted(missing)}"
    declared = "\n".join(f"const {name} = {value}" for name, value in found.items())
    # `wanted` is the component's own mutable window size, declared beside the
    # constants so every harness in this file starts where a fresh view does.
    return declared + "\nlet wanted = INITIAL_TAIL"


def _function(name):
    """The named top-level function's source, verbatim, from Chat.vue."""
    text = CHAT.read_text(encoding="utf-8")
    start = text.index(f"function {name}(")
    # `async function` — step back over the keyword so the extract stays callable.
    if text[max(0, start - 6):start] == "async ":
        start -= 6
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


def _drive(held, calls, *, window=0, cursor=0, following=True, fetches=None):
    """Run `absorb` (with `place` and `reload`) over a held window.

    Executes the real functions from Chat.vue — the whole absorb path, not one
    branch of it — and returns the component state they leave behind. *calls* is a
    list of ``(page, mode)``; *fetches* are the pages `reload` is served, or the
    string "reject" to make the re-read fail.
    """
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")
    script = """
const ref = (v) => ({ value: v })
const messages = ref(%s)
const windowStart = ref(%d)
const following = ref(%s)
const total = ref(0), sessions = ref([]), note = ref(null), live = ref(false)
let cursor = %d
let generation = 0
let followed = 0
const stale = () => false
const settlePending = () => {}
const nextTick = (fn) => { fn() }
const stickToBottom = () => { followed += 1 }
%s
const props = { sessionId: 's-x' }
const reloads = []
const canned = %s
function fetchSessionMessages(id, opts) {
  reloads.push(opts)
  if (canned === 'reject') return Promise.reject(new Error('boom'))
  return Promise.resolve(canned.shift())
}
%s

%s

%s

const calls = %s
let threw = null
try {
  for (const [page, mode] of calls) await absorb(page, mode)
} catch (exc) { threw = exc.message }
console.log(JSON.stringify({
  rendered: messages.value.map((m) => [m.id, m.text]),
  windowStart: windowStart.value, cursor, followed, threw, reloads,
  total: total.value,
}))
""" % (
        json.dumps(held), window, "true" if following else "false", cursor,
        _constants(), "'reject'" if fetches == "reject" else json.dumps(fetches or []),
        _function("reload"), _function("place"), _function("absorb"),
        json.dumps(calls),
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout)


def _serve(conversation, held, *, window, cursor=None, following=False):
    """Drive the real `loadEarlier` against a conversation the route serves.

    The request shape comes from the component rather than from a fixture: the
    stub answers `messages[since:since + limit]` the way `read_messages` does, so
    a test cannot accidentally hand the prepend a page the server would never
    return (!288 review round 8).
    """
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")
    script = """
const ref = (v) => ({ value: v })
const conversation = %s
const messages = ref(%s)
const windowStart = ref(%d)
const following = ref(%s)
const total = ref(conversation.length), sessions = ref([]), note = ref(null), live = ref(false)
const problem = ref(null)
let cursor = %d
let generation = 0
let followed = 0
const stale = () => false
const settlePending = () => {}
const nextTick = (fn) => { fn() }
const stickToBottom = () => { followed += 1 }
%s
const props = { sessionId: 's-x' }
const asked = []
function fetchSessionMessages(id, opts) {
  asked.push(opts)
  const start = opts.since < 0
    ? Math.max(0, conversation.length + opts.since)
    : Math.min(opts.since, conversation.length)
  const slice = conversation.slice(start, start + opts.limit)
  return Promise.resolve({
    messages: slice, from: start, cursor: start + slice.length,
    total: conversation.length, sessions: [], note: null, live: true,
  })
}
%s

%s

%s

%s

let threw = null
try { await loadEarlier() } catch (exc) { threw = exc.message }
console.log(JSON.stringify({
  rendered: messages.value.map((m) => m.id),
  windowStart: windowStart.value, followed, threw, asked,
  problem: problem.value,
}))
""" % (
        json.dumps(conversation), json.dumps(held), window,
        "true" if following else "false",
        cursor if cursor is not None else len(conversation), _constants(),
        _function("reload"), _function("place"), _function("absorb"),
        _function("loadEarlier"),
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout)


def _page(messages, start, *, total=None, cursor=None):
    return {"messages": messages, "from": start,
            "cursor": cursor if cursor is not None else start + len(messages),
            "total": total if total is not None else start + len(messages)}


def _run(held, page, *, window=0):
    """Backwards-compatible single append, returning (placed, rendered)."""
    state = _drive(held, [[page, "append"]], window=window)
    return not state["reloads"], state["rendered"]


RECORD = {"id": "s/000001", "text": "three running", "comms_kind": "message"}
ROW = {"id": "s:rows#0", "text": "looking at the branch"}
LATE = {"id": "s:rows#1", "text": "running the suite"}
COPY = {"id": "s:rows#2", "text": "three running"}


def test_a_record_already_drawn_is_not_drawn_again():
    """The operator's screenshot: the record comes back under a new position after
    a transcript row landed below the cursor, and it must not be drawn twice."""
    placed, rendered = _run([ROW, RECORD], _page([ROW, LATE, RECORD], 0))
    assert placed
    assert rendered == [
        ["s:rows#0", "looking at the branch"],
        ["s:rows#1", "running the suite"],
        ["s/000001", "three running"],
    ], "and the late row sits where the server put it, not at the end"


def test_a_turn_the_server_stopped_serving_disappears():
    """Removal is half of what the read-path join does: the harness's own copy of a
    record is dropped once the record arrives. A view that only added and replaced
    kept drawing the copy beside the record — the duplicate this change removes."""
    placed, rendered = _run([ROW, COPY], _page([ROW, RECORD], 0))
    assert placed
    assert rendered == [
        ["s:rows#0", "looking at the branch"],
        ["s/000001", "three running"],
    ], "the suppressed copy is gone, not kept beside the record"


def test_a_turn_that_changed_is_replaced_where_it_stands():
    """A turn can lose its words and keep its tool calls between two reads."""
    emptied = dict(ROW, text="")
    placed, rendered = _run([ROW, RECORD], _page([emptied, RECORD], 0))
    assert placed
    assert rendered == [["s:rows#0", ""], ["s/000001", "three running"]]


def test_an_unnamed_turn_is_not_re_added_on_every_poll():
    """The poll re-reads a tail, so anything the page repeats must not accumulate —
    including a turn the server could not name."""
    # At `windowStart > 0`, so the shared-id loop actually runs: at 0 the page
    # takes the whole-window branch and the unnamed turn is never examined.
    nameless = {"id": None, "text": "from an older daemon"}
    state = _drive(
        [ROW, nameless], [[_page([nameless], 1), "append"]], window=0,
        fetches=[_page([ROW, nameless], 0)],
    )
    assert state["reloads"], "an unnamed turn in the shared region must refuse"
    assert [row[0] for row in state["rendered"]] == ["s:rows#0", None], (
        "and the re-read must not leave it duplicated"
    )


def test_a_page_that_starts_past_the_window_is_refused():
    """A gap between what is held and what arrived cannot be laid over anything."""
    placed, _ = _run([ROW], _page([RECORD], 5))
    assert placed is False, "the caller must re-read rather than guess"


def test_a_page_with_nothing_in_common_is_refused():
    """The one thing identity cannot answer: a page sharing no turn with the list
    says nothing about where it belongs — a whole transcript file entering the
    merge does that in one step. The view re-reads rather than guessing."""
    placed, _ = _run([ROW, RECORD], _page([{"id": "far", "text": "elsewhere"}], 1), window=0)
    assert placed is False, "a page with no turn in common cannot be placed"


def test_the_window_is_carried_so_earlier_turns_stay_reachable():
    """`loadEarlier` reads from the window's start; a held turn's own seq is stale
    the moment anything is inserted below it."""
    placed, rendered = _run([RECORD], _page([LATE, RECORD], 40), window=41)
    assert placed
    assert [row[0] for row in rendered] == ["s:rows#1", "s/000001"]


# --- what the previous harness could not see (!288 review round 7) -------------
#
# The earlier version of this file extracted `splice` alone, so `absorb`'s prepend
# and replace branches never ran and nothing asserted `windowStart` or `cursor`.
# Deleting the line that carries the window forward left every test green. These
# drive the whole absorb path and assert the state it leaves.


def test_load_earlier_does_not_redraw_turns_already_held():
    """The reviewer's scenario, driven through the real `loadEarlier`.

    The conversation was `t#0..t#99` when the window was read; the join has since
    dropped three turns from below it, so the positions `loadEarlier` asks for now
    hold different turns and the page overlaps what is rendered. Trimming by the
    offset the request was computed from cannot see that: `since + limit` equals
    `windowStart` by construction, so the trim is zero on every input the request
    can produce, and the overlap was drawn twice.
    """
    conversation = [{"id": f"t#{n}", "text": f"turn {n}"} for n in range(100)]
    del conversation[20:23]          # three harness copies dropped by the join
    held = [{"id": f"t#{n}", "text": f"turn {n}"} for n in range(60, 100)]

    state = _serve(conversation, held, window=60)

    ids = state["rendered"]
    assert len(ids) == len(set(ids)), (
        f"turns drawn twice: {sorted({i for i in ids if ids.count(i) > 1})}"
    )
    assert ids[-1] == "t#99" and ids[0] == "t#23"
    assert ids == [f"t#{n}" for n in list(range(23, 60)) + list(range(60, 100))], (
        "the view must be contiguous: no duplicate, and no gap"
    )
    assert state["windowStart"] == 20


def test_an_insertion_within_the_overlap_budget_still_stitches():
    """The mirror of the removal case, inside the seam budget: turns inserted
    below the window push the held head down the page, and the deliberate overlap
    is what still finds it. Refusal past the budget is its own test below."""
    conversation = [{"id": f"t#{n}", "text": f"turn {n}"} for n in range(100)]
    for offset in range(10):
        conversation.insert(5, {"id": f"new#{offset}", "text": "inserted"})
    held = [{"id": f"t#{n}", "text": f"turn {n}"} for n in range(60, 100)]

    state = _serve(conversation, held, window=60)

    ids = state["rendered"]
    assert len(ids) == len(set(ids)), "no turn may be drawn twice"
    gaps = [n for n in range(len(ids) - 1) if ids[n].startswith("t#") and ids[n + 1].startswith("t#")
            and int(ids[n + 1][2:]) != int(ids[n][2:]) + 1]
    assert not gaps, f"the view has a hole at {[ids[n] for n in gaps]}"


def test_a_re_read_does_not_move_a_reader_who_scrolled_back():
    """`reload` runs from a poll, not from anything the operator did. The comment
    above `absorb` says a poll must never yank the view down."""
    held = [{"id": f"t#{n}", "text": f"turn {n}"} for n in range(20, 100)]
    # A whole transcript file joined the merge, so the page shares no turn with the
    # list and cannot be placed — which is what makes `reload` run.
    shifted = _page([{"id": f"new#{n}", "text": "inserted"} for n in range(5)], 80)
    served = _page([{"id": f"t#{n}", "text": f"turn {n}"} for n in range(20, 80)], 20)
    state = _drive(
        held, [[shifted, "append"]],
        window=20, cursor=100, following=False, fetches=[served],
    )
    assert state["followed"] == 0, "the pane scrolled to the bottom under a reader"
    assert state["reloads"][0]["since"] == 20, (
        "a scrolled-back reader is re-read around their window, not at the end"
    )
    assert state["windowStart"] == 20 and len(state["rendered"]) == 60


def test_a_failed_re_read_reaches_the_caller():
    """Every other read path in the component sets `problem`; a re-read that
    resolved into nothing left the pane silently stale."""
    held = [{"id": "t#0", "text": "turn 0"}]
    state = _drive(
        held, [[_page([{"id": "t#9", "text": "moved"}], 5), "append"]],
        window=0, fetches="reject",
    )
    assert state["threw"] == "boom", "the failure must reach the poll's catch"


def test_an_equal_count_edit_below_the_page_is_rendered_as_served():
    """The join inserts a bus turn and drops a copy per record, so a record
    absorbed inside the page's range leaves the position count unchanged. The page
    is authoritative over what it covers, so this needs no detection at all: the
    pane renders what was served."""
    # The join's own shape: the harness copy dropped, the record interleaved.
    held = [
        {"id": "head", "text": "held below the page"},
        {"id": "a", "text": "one"}, {"id": "copy", "text": "two"},
        {"id": "b", "text": "three"}, {"id": "c", "text": "four"},
    ]
    # `copy` is gone and a record took its count, so `b` and `c` shift up one while
    # the turn at page.from is still `a`.
    # The poll reads from the window's own start, so the page carries the head.
    served = [{"id": "head", "text": "held below the page"},
              {"id": "a", "text": "one"}, {"id": "rec", "text": "two"},
              {"id": "b", "text": "three"}, {"id": "c", "text": "four"}]
    state = _drive(held, [[_page(served, 0), "append"]], window=0)

    assert not state["reloads"], "the page can be placed; nothing needs re-reading"
    assert [row[0] for row in state["rendered"]] == ["head", "a", "rec", "b", "c"], (
        "the dropped copy goes and the record takes its place"
    )
    assert state["windowStart"] == 0


def test_an_unverifiable_page_is_refused_rather_than_accepted():
    """An unnamed turn on either side, or an empty page against a window that still
    holds turns, is exactly what this check exists to notice."""
    held = [{"id": "a", "text": "one"}, {"id": None, "text": "unnamed"}]
    served = _page([], 1, total=1, cursor=1)
    state = _drive(held, [[served, "append"]], window=0, fetches=[_page([{"id": "a", "text": "one"}], 0)])
    assert state["reloads"], "an empty page over a held window must re-read"


def test_a_page_starting_before_the_window_carries_the_window_back():
    """A page that reaches further back than the window must carry the window with
    it, or every later request is computed against a start that no longer exists."""
    held = [{"id": f"t#{n}", "text": f"turn {n}"} for n in range(20, 40)]
    served = [{"id": f"t#{n}", "text": f"turn {n}"} for n in range(5, 40)]
    state = _drive(held, [[_page(served, 5), "append"]], window=20, cursor=40)

    assert state["windowStart"] == 5, "the window must follow the page that replaced it"
    assert [row[0] for row in state["rendered"]][0] == "t#5"


def test_a_reader_who_paged_to_the_top_keeps_their_history():
    """`windowStart == 0` is the furthest-back reader there is, and the guard that
    was meant to protect scrolled-back readers excluded exactly them: `anchored`
    was false, so the re-read took the tail and their loaded range was discarded
    with them left at the end of the conversation."""
    held = [{"id": f"t#{n}", "text": f"turn {n}"} for n in range(200)]
    served = _page([{"id": f"t#{n}", "text": f"turn {n}"} for n in range(200)], 0)
    # A page sharing no turn with the list, which is the one thing identity cannot
    # place — so `reload` runs, which is what this test is about.
    state = _drive(
        held, [[_page([{"id": "new#0", "text": "inserted"}], 190), "append"]],
        window=0, cursor=200, following=False, fetches=[served],
    )
    assert state["reloads"][0]["since"] == 0, (
        "the reader at the top is re-read from the top, not from the tail"
    )
    assert state["reloads"][0]["limit"] >= 200, (
        "and the read is sized from what they hold, not one page"
    )
    assert len(state["rendered"]) == 200, "their loaded history survives"
    assert state["followed"] == 0


def test_the_earlier_page_owns_the_overlap_it_asked_for():
    """`loadEarlier` asks for `OVERLAP` turns past the window on purpose, so the
    page carries the server's current version of turns the pane already renders.
    Keeping the rendered copies of those throws that away: a record absorbed
    inside the overlap between the window read and the click leaves the pane
    showing the copy the server dropped and never showing the record."""
    conversation = [{"id": f"t#{n}", "text": f"turn {n}"} for n in range(100)]
    conversation.insert(65, {"id": "r#x", "text": "the record"})
    conversation.remove({"id": "t#70", "text": "turn 70"})
    held = [{"id": f"t#{n}", "text": f"turn {n}"} for n in range(60, 100)]

    state = _serve(conversation, held, window=60)

    ids = state["rendered"]
    assert "t#70" not in ids, "the pane still shows a copy the server dropped"
    assert "r#x" in ids, "and never shows the record that replaced it"


def test_load_earlier_shows_only_what_the_server_still_serves():
    """When the conversation has shrunk below `windowStart`, the request lands past
    the rendered head and the page carries only part of what is on screen. What it
    does not carry is gone, and goes — a shorter list, which is the truth catching
    up rather than a loss, and `start` follows the page so the rest is one more
    *earlier messages* away.

    The alternative was measured and is worse: keeping the rendered head because
    the page did not carry it resurrects deleted turns in 75 of 1000 swept runs,
    since "not carried" and "below the page" are the same observation."""
    conversation = [{"id": f"t#{n}", "text": f"turn {n}"} for n in range(100)]
    del conversation[5:55]           # fifty turns removed below the window
    held = [{"id": f"t#{n}", "text": f"turn {n}"} for n in range(60, 100)]

    state = _serve(conversation, held, window=60)

    ids = state["rendered"]
    truth = [m["id"] for m in conversation]
    start = truth.index(ids[0])
    assert truth[start:start + len(ids)] == ids, "only what the server still serves"
    assert state["windowStart"] == start, "and the window names where that begins"
    assert start > 0, "the turns below it stay reachable by asking again"


def test_load_earlier_refuses_when_the_overlap_is_exhausted():
    """The one place the stitch gives up. Fewer insertions than `OVERLAP` still
    find a seam, so the refusal has to be provoked past that budget."""
    conversation = [{"id": f"t#{n}", "text": f"turn {n}"} for n in range(100)]
    for offset in range(25):
        conversation.insert(5, {"id": f"new#{offset}", "text": "inserted"})
    held = [{"id": f"t#{n}", "text": f"turn {n}"} for n in range(60, 100)]

    state = _serve(conversation, held, window=60)

    assert len(state["asked"]) == 2, (
        f"no seam must re-read; requests were {state['asked']}"
    )
    ids = state["rendered"]
    assert len(ids) == len(set(ids))


# --- the property, swept (!288 review round 10) --------------------------------
#
# Six rounds of this pane fixed one stitch defect and introduced the next, each
# time on an input the previous round's tests did not have. What every round was
# reaching for is one property, so it is pinned as one property over many inputs
# rather than as another set of named cases: **what the pane renders is a
# contiguous slice of the conversation the server is serving.** A duplicate turn
# breaks it, a hole breaks it, a resurrected turn breaks it, and so does a turn
# rendered in a position the server does not put it in.
#
# Edits are confined to the range the request covers, deliberately. An edit
# outside it cannot be placed from that one page by any rule — the next poll and
# the re-read are what cover it — so sweeping it grades the generator rather than
# the code. Both the author and the reviewer of this file made that mistake
# first, and each reported divergences that were nothing of the kind.


_SWEEP = """
const ref = (v) => ({ value: v })
%s
const props = { sessionId: 's-x' }
let generation = 0
let polling = false
const stale = () => false
const settlePending = () => {}
const nextTick = (fn) => { fn() }
const stickToBottom = () => {}

// A deterministic generator, so a failure names an input that can be replayed.
function rng(seed) {
  let state = seed >>> 0
  return () => {
    state = (state * 1103515245 + 12345) >>> 0
    return state / 4294967296
  }
}

const duplicatedIds = (list) => list.length !== new Set(list).size
const FORWARD = %s
const runs = []
for (let seed = 1; seed <= %d; seed += 1) {
  const random = rng(seed)
  const pick = (n) => Math.floor(random() * n)

  const conversation = []
  for (let n = 0; n < 100; n += 1) conversation.push({ id: `t#${n}`, text: `turn ${n}` })
  const windowAt = 60
  const messages = ref(conversation.slice(windowAt).map((m) => ({ ...m })))
  const windowStart = ref(windowAt)
  let cursor = conversation.length
  const total = ref(conversation.length)
  const sessions = ref([]), note = ref(null), live = ref(false), following = ref(false)
  const problem = ref(null)

  // 1-4 bursts of 1-8 turns, inserted or removed inside the range the request
  // will cover, plus a deletion at the very start on some seeds.
  //
  // Edits range over everything the request will cover, the overlap included,
  // because that is where held index and served position stop differing by a
  // constant — the input every previous version of this stitch broke on. The net
  // shift stays inside `OVERLAP` so the rendered head is still inside the page.
  const edits = []
  let shift = 0
  // Where the request will look, which is where an edit has to land for the run
  // to have a correct answer at all. Backward: the page starts a `PAGE` below the
  // window. Forward: it starts `OVERLAP` back from the end.
  const lowest = FORWARD ? Math.max(0, cursor - OVERLAP) : 0
  const highest = FORWARD ? conversation.length : windowAt + OVERLAP - 1
  for (let burst = 0, count = 1 + pick(4); burst < count; burst += 1) {
    const size = 1 + pick(8)
    const at = lowest + pick(Math.max(1, highest - lowest))
    if (random() < 0.5) {
      if (shift + size > OVERLAP - 1) continue
      const made = []
      for (let n = 0; n < size; n += 1) made.push({ id: `x#${seed}.${burst}.${n}`, text: 'inserted' })
      conversation.splice(at, 0, ...made)
      shift += size
      edits.push(`insert ${size} at ${at}`)
    } else {
      if (shift - size < -(OVERLAP - 1)) continue
      conversation.splice(at, size)
      shift -= size
      edits.push(`delete ${size} at ${at}`)
    }
  }
  // Backward only: the forward page starts near the end, so an edit at position 0
  // is outside everything it covers and no rule can place it — sweeping it would
  // grade the generator rather than the code.
  if (!FORWARD && seed %% 7 === 0 && shift - 3 >= -(OVERLAP - 1)) {
    conversation.splice(0, 3); shift -= 3; edits.push('delete 3 at start')
  }
  const asked = []
  function fetchSessionMessages(id, opts) {
    asked.push(opts)
    const start = opts.since < 0
      ? Math.max(0, conversation.length + opts.since)
      : Math.min(opts.since, conversation.length)
    const slice = conversation.slice(start, start + opts.limit)
    return Promise.resolve({
      messages: slice, from: start, cursor: start + slice.length,
      total: conversation.length, sessions: [], note: null, live: true,
    })
  }

%s

  let threw = null
  try { await (FORWARD ? poll() : loadEarlier()) } catch (exc) { threw = exc.message }

  const rendered = messages.value.map((m) => m.id)
  const truth = conversation.map((m) => m.id)
  const served = asked[asked.length - 1]
  const page = truth.slice(
    Math.min(served.since, truth.length),
    Math.min(served.since, truth.length) + served.limit,
  )

  // What the client can be held to, and nothing beyond it. The page is
  // authoritative over the range it covers, so that range must be rendered
  // exactly as served; the rendered turns before it must be the conversation's
  // own, immediately preceding it; and nothing may be drawn twice. Turns *above*
  // the page's end are not knowable from this read by anyone — the next poll and
  // the re-read are what cover them — so they are not asserted.
  if (FORWARD) {
    // The poll reads to the end, so the page speaks for every rendered turn: the
    // list must be the conversation's own slice and `cursor` must name its end.
    const at = truth.indexOf(rendered[0])
    const slice = at < 0 ? null : truth.slice(at, at + rendered.length)
    const exact = slice !== null && slice.join('|') === rendered.join('|')
    runs.push({ seed, edits, forward: true,
                ok: exact && !duplicatedIds(rendered) && windowStart.value === at
                    && cursor === windowStart.value + rendered.length && !threw,
                contiguous: exact, duplicated: duplicatedIds(rendered),
                anchored: windowStart.value === at,
                cursorOk: cursor === windowStart.value + rendered.length,
                threw, windowStart: windowStart.value, requests: asked.length })
    continue
  }
  const start = rendered.indexOf(page[0])
  const carriesPage = start >= 0
    && rendered.slice(start, start + page.length).join('|') === page.join('|')
  const headTruth = truth.slice(Math.max(0, served.since - start), served.since)
  const headOk = start >= 0 && rendered.slice(0, start).join('|') === headTruth.join('|')
  const duplicated = rendered.length !== new Set(rendered).size
  const anchored = windowStart.value === served.since - start

  runs.push({ seed, edits, ok: carriesPage && headOk && !duplicated && anchored && !threw,
              carriesPage, headOk, duplicated, anchored, threw,
              windowStart: windowStart.value, requests: asked.length })
}
console.log(JSON.stringify(runs))
"""


def _sweep(runs=150, *, forward=False):
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")
    body = "\n".join(
        "  " + line if line else ""
        for name in ("reload", "place", "absorb", "loadEarlier", "poll")
        for line in _function(name).splitlines() + [""]
    )
    script = _SWEEP % (_constants(), "true" if forward else "false", runs, body)
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout)


def test_what_the_pane_renders_is_always_a_contiguous_slice_of_the_conversation():
    """The property every round of this pane has been reaching for, over inputs no
    round's named cases covered: edits of both signs, inside the overlap the
    request re-reads, and deletions at the very start."""
    runs = _sweep()
    failures = [run for run in runs if not run["ok"]]
    assert not failures, (
        f"{len(failures)}/{len(runs)} runs diverged; "
        f"{sum(1 for f in failures if f['duplicated'])} duplicated a turn. "
        f"First: {failures[0]}"
    )


def test_the_forward_poll_renders_exactly_the_window_the_server_serves():
    """The same property through the other caller. The sweep drove only the
    backward read until now, and the read the defect was in was never exercised —
    which is how a page keeping turns its own `total` said were deleted passed a
    green sweep (!288 review round 11).

    The forward read is held to more than the backward one: it asks from the
    window's own start and reads to the end, so the page speaks for every rendered
    turn and `cursor` must name where the list ends."""
    runs = _sweep(forward=True)
    failures = [run for run in runs if not run["ok"]]
    assert not failures, (
        f"{len(failures)}/{len(runs)} runs diverged; "
        f"{sum(1 for f in failures if f['duplicated'])} duplicated a turn. "
        f"First: {failures[0]}"
    )


# --- the window over a long session (!288 review round 12) ---------------------
#
# The sweeps above each drive a single read. What they cannot see is a pane that
# has stopped moving: every individual placement can be correct while the window
# the requests are anchored on grows past what one request can serve, at which
# point the poll can never reach the end again — silently, because each page still
# places. This runs the pane instead: thousands of polls, the operator sending and
# paging back, and the conversation changing underneath.

_SESSION = """
const ref = (v) => ({ value: v })
%s
const props = { sessionId: 's-x' }
let generation = 0
let polling = false
const stale = () => false
const nextTick = (fn) => { fn() }
const stickToBottom = () => {}
const release = () => {}
const comparable = (text) => String(text || '').replace(/\\s+/g, ' ').trim()

function rng(seed) {
  let state = seed >>> 0
  return () => { state = (state * 1103515245 + 12345) >>> 0; return state / 4294967296 }
}
const random = rng(%d)
const pick = (n) => Math.floor(random() * n)

const conversation = []
let minted = 0
const append = (text) => conversation.push({ id: `t#${minted}`, text: text || `turn ${minted}`, role: 'user', kind: 'said' }) && (minted += 1)
for (let n = 0; n < 200; n += 1) append()

const messages = ref([])
const windowStart = ref(0)
const pending = ref([])
const consumed = new Set()
const following = ref(true)
const total = ref(0), sessions = ref([]), note = ref(null), live = ref(false)
const problem = ref(null)
let cursor = 0
const asked = []

function fetchSessionMessages(id, opts) {
  asked.push(opts)
  const start = opts.since < 0
    ? Math.max(0, conversation.length + opts.since)
    : Math.min(opts.since, conversation.length)
  const slice = conversation.slice(start, start + opts.limit)
  return Promise.resolve({
    messages: slice.map((m, i) => ({ ...m, seq: start + i })),
    from: start, cursor: start + slice.length, total: conversation.length,
    sessions: [], note: null, live: true,
  })
}

%s

// Opening the pane, the way `start()` does.
{
  const page = await fetchSessionMessages(props.sessionId, { since: -INITIAL_TAIL, limit: INITIAL_TAIL })
  await absorb(page, 'replace')
}

const problems = []
let widest = 0
let lastCursor = cursor
let sent = 0
for (let step = 0; step < %d; step += 1) {
  // The conversation moves: new turns, an edit inside the tail, a deletion.
  const grew = pick(3)
  for (let n = 0; n < grew; n += 1) append()
  if (random() < 0.15 && conversation.length > 60) {
    const at = conversation.length - 1 - pick(30)
    if (!conversation[at].id.startsWith('s#')) {
      conversation.splice(at, 1)
      conversation.splice(at, 0, { id: `r#${step}`, text: 'record', role: 'assistant', kind: 'said' })
    }
  }
  // Never the operator's own message: a bubble whose turn the server deleted
  // cannot settle, and sweeping that would grade the generator.
  if (random() < 0.08 && conversation.length > 80) {
    const at = conversation.length - 1 - pick(20)
    if (!conversation[at].id.startsWith('s#')) conversation.splice(at, 1)
  }

  // The operator sends, the way `send()` snapshots its bubble.
  if (random() < 0.2) {
    sent += 1
    const text = `sent ${sent}`
    const after = messages.value.length ? messages.value[messages.value.length - 1].id : null
    pending.value = [...pending.value, { at: step, text, since: cursor, after, attachments: [] }]
    conversation.push({ id: `s#${sent}`, text, role: 'user', kind: 'said' })
  }
  if (random() < 0.05) { following.value = false; await loadEarlier(); following.value = true }

  await poll()

  widest = Math.max(widest, messages.value.length)
  // Not raw monotonicity — a deletion shrinks the conversation and the cursor
  // with it. What must hold is that the cursor names the end of what is
  // rendered, and that the rendered end is the conversation's end.
  if (cursor !== windowStart.value + messages.value.length) {
    problems.push(`step ${step}: cursor ${cursor} != windowStart ${windowStart.value} + ${messages.value.length}`)
  }
  if (cursor !== conversation.length) {
    problems.push(`step ${step}: cursor ${cursor} behind conversation ${conversation.length}`)
  }
  lastCursor = cursor
  const tail = conversation.slice(windowStart.value, windowStart.value + messages.value.length)
  if (tail.map((m) => m.id).join('|') !== messages.value.map((m) => m.id).join('|')) {
    problems.push(`step ${step}: rendered list is not the server's window at ${windowStart.value}`)
  }
  if (messages.value.length && messages.value[messages.value.length - 1].id !== conversation[conversation.length - 1].id) {
    problems.push(`step ${step}: newest turn not rendered (pane stopped moving)`)
  }
  if (messages.value.length > MAX_PAGE) problems.push(`step ${step}: window ${messages.value.length} exceeds MAX_PAGE`)
  if (problems.length > 3) break
}

console.log(JSON.stringify({
  problems, widest, sent, stillPending: pending.value.length,
  rendered: messages.value.length, windowStart: windowStart.value,
  cursor, conversation: conversation.length,
  widestRequest: asked.reduce((most, o) => Math.max(most, o.limit || 0), 0),
}))
"""


def _session(steps=2000, seed=1):
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")
    body = "\n".join(
        line
        for name in ("reload", "place", "absorb", "loadEarlier", "poll", "settlePending")
        for line in _function(name).splitlines() + [""]
    )
    script = _SESSION % (_constants(), seed, body, steps)
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout)


def test_a_long_session_keeps_its_window_bounded_and_keeps_moving():
    """A pane left open. Every placement can be correct while the window the
    requests are anchored on grows past what one request can serve — after which
    the poll never reaches the end again, nothing says so, and the operator's own
    sends never settle because the list they settle against has stopped."""
    state = _session()
    assert not state["problems"], state
    assert state["stillPending"] == 0, (
        f"{state['stillPending']} of {state['sent']} sent bubbles never settled"
    )
    assert state["widest"] <= transcripts.MAX_MESSAGE_LIMIT, state




# --- the pending anchor (!288 review round 13) --------------------------------
#
# A bubble carries the id of the turn the conversation ended on when it went, and
# only turns after that one may settle it. The half that matters is what happens
# when that anchor is *gone*: it can be dropped by the join that is this MR's whole
# subject, or trimmed away by a backward read, and neither means "everything
# rendered is past it". Treating it that way settles the operator's message
# against an older identical turn and takes it off the screen unconfirmed.

_ANCHOR_PROBE = """
const ref = (v) => ({ value: v })
const messages = ref(%s)
const pending = ref(%s)
const consumed = new Set()
const release = () => {}
const comparable = (text) => String(text || '').replace(/\\s+/g, ' ').trim()
%s
settlePending()
console.log(JSON.stringify({
  left: pending.value.map((item) => item.text),
  consumed: [...consumed],
}))
"""


def _settle(messages, pending):
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")
    script = _ANCHOR_PROBE % (
        json.dumps(messages), json.dumps(pending), _function("settlePending"),
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout)


def _said(seq, text, ident=None, role="user"):
    return {"seq": seq, "id": ident or f"t#{seq}", "role": role, "kind": "said", "text": text}


def test_an_older_identical_turn_does_not_settle_a_bubble_whose_anchor_is_in_view():
    """The ordinary case: the anchor is rendered, so only what follows it counts."""
    rendered = [_said(50, "yes"), _said(51, "Pushed.", role="assistant")]
    state = _settle(rendered, [{"text": "yes", "since": 52, "after": "t#51"}])
    assert state["left"] == ["yes"], "the bubble must stay up until its own turn lands"


def test_an_older_identical_turn_does_not_settle_a_bubble_whose_anchor_was_dropped():
    """The anchor was the harness's own copy of a line that also went to the bus,
    and the join dropped it when the record arrived — this MR's core behaviour, no
    paging required. "Gone" is not evidence that the list is past it, so the cursor
    snapshot is the only thing still true."""
    rendered = [_said(50, "yes"), _said(51, "the record", "s/000001", role="assistant")]
    state = _settle(rendered, [{"text": "yes", "since": 61, "after": "t#60"}])
    assert state["left"] == ["yes"], (
        "the operator's message was taken off the screen and settled against a "
        "'yes' from before they typed"
    )


def test_a_bubble_with_no_anchor_falls_back_to_the_cursor_it_snapshotted():
    """`after: null` is "nothing was rendered when this went", which says nothing
    about what is rendered now. Four comments in this change say the cursor guards
    it; this is the test that makes that true."""
    rendered = [_said(50, "yes")]
    state = _settle(rendered, [{"text": "yes", "since": 61, "after": None}])
    assert state["left"] == ["yes"]


def test_the_bubble_still_settles_against_its_own_turn():
    """The guard must not become a wall: the turn the operator actually sent."""
    rendered = [_said(50, "yes"), _said(61, "yes")]
    state = _settle(rendered, [{"text": "yes", "since": 61, "after": "t#50"}])
    assert state["left"] == [], "the bubble must go when its own turn arrives"
    assert state["consumed"] == ["t#61"]


# --- the source-level guards that paired with the above ------------------------
#
# Moved here whole from `test_platform_conversation_merge.py` when the rework came
# out of !288: what they pin is the *wiring* — a template key, a request shape, a
# constant copied from the route — which the node harnesses above cannot see.
# `#419` has to re-establish whichever of them its design keeps.


def _chat():
    return CHAT.read_text(encoding="utf-8")


def test_the_pane_lays_the_page_over_the_window_and_re_reads_the_end():
    """Source-level, because what these pin is wiring rather than logic — the
    behaviour of `place` itself is executed by the harnesses above."""
    text = _chat()
    assert "function place" in text, "one placement rule for both reads"
    assert "page.from - windowStart.value" not in text, (
        "a page's position is decided by identity, never by arithmetic on a "
        "remembered window start"
    )
    assert "since: windowStart.value," in text, (
        "the poll reads from the window's own start, so its page has no near edge"
    )
    assert "cursor - OVERLAP" not in text, "the poll no longer reads from near the end"
    assert "return reload()" in text, "a page that cannot be placed must re-read"
    # The three consumers that must not read a position as a name.
    assert ':key="message.id ?? message.seq"' in text
    assert "consumed.has(message.id ?? message.seq)" in text
    assert "messages.value[0].seq" not in text, "the window start is not a held seq"


def test_the_panes_window_leaves_room_for_what_arrives_between_polls():
    """The poll refreshes the whole window in one request, so the window must stay
    strictly below what the route will serve — with room for the turns that
    arrived since. Without that headroom the page stops reaching the end, and
    because each page still *places*, the pane freezes with nothing to say so."""
    text = _chat()
    window = int(re.search(r"^const WINDOW_MAX = (\d+)$", text, re.MULTILINE).group(1))
    page = int(re.search(r"^const PAGE = (\d+)$", text, re.MULTILINE).group(1))
    assert window + page <= transcripts.MAX_MESSAGE_LIMIT, (
        f"a full-window refresh asks for {window + page}, over the route's "
        f"{transcripts.MAX_MESSAGE_LIMIT}"
    )
    assert "list.length > wanted" in text, "every widening needs its matching trim"


def test_the_pane_clamps_its_re_read_to_what_the_route_accepts():
    """`Chat.vue` copies the route's own cap. A wrong value is not cosmetic: the
    anchored re-read sends it as `limit`, so a lower server cap means a 422 and an
    error banner every five seconds for the scrolled-back readers it protects."""
    declared = re.search(r"^const MAX_PAGE = (\d+)$", _chat(), re.MULTILINE)
    assert declared, "Chat.vue no longer declares MAX_PAGE"
    assert int(declared.group(1)) == transcripts.MAX_MESSAGE_LIMIT
