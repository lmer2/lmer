"""The chat pane's dedupe of a turn an arriving page redelivers (#407).

The bug this pins was invisible to a source read and to the server tests, because
both halves were behaving as written: ``read_messages`` numbers turns by their
position in the merged conversation and renumbers on every read, while the pane
paged by that position and appended what came back. A transcript row written
after the record it precedes in time lands below the served cursor, every later
turn shifts up one, and the record the pane had already drawn arrives again — two
identical bubbles, same timestamp, same source chip (operator screenshot,
2026-09-05, uber lmer on dev6; the post came from a Bash heredoc, so no prose turn
with those words existed and the harness-copy join cannot explain it).

The fix has two halves and this file is the client one. The server puts a stable
``id`` on every turn it serves, which a renumber does not move
(``lmer_platform.transcripts``, pinned in ``test_platform_conversation_merge.py``);
both of the pane's reads — the forward poll and the backward *earlier messages*
read — drop an arriving turn whose id is already rendered, and the row is keyed by
that id.

**Scope.** Recognising a turn is the whole of what the pane does about position,
and most of this file tests it; the last two cases cover the settle rule the split
restored, which is the other thing ``absorb`` calls. What the pane cannot do is
place a page: it still reads its window by ``since``/``limit`` and still only
appends, so a turn the server stopped serving is not withdrawn, a turn that
changed is not replaced where it stands, and — the case that is a skip rather than
a duplicate — a turn inserted below the cursor is never drawn at all, because the
cursor advances past it while the dedupe correctly drops the turns it renumbered.
``docs/COMMS.md`` §4 states all three. Removing the position from the read path is
issue 419, and its acceptance tests are parked, uncollected, in
``tests/_issue419_chat_read_path.py`` (25 failing, 3 passing — that file's own
header says which and why).

What this runs are the functions as they stand in ``Chat.vue`` — extracted from
the file and executed in node against a ``messages`` shim — so an edit to one is
an edit to what is tested. What it does **not** cover is the component around it:
there is no DOM runtime in ``web/`` (SSR runs ``setup()`` but never ``onMounted``,
which is where the poll lives), so the wiring from poll to ``absorb`` to
``merged`` is held by the source-level guards in
``test_platform_conversation_merge.py``.
"""

import json
import subprocess
from pathlib import Path

from tests.conftest import node_binary, require_node_toolchain

WEB = Path(__file__).resolve().parent.parent / "web"
CHAT = WEB / "src" / "components" / "Chat.vue"


def _function(name):
    """The named top-level function's source, verbatim, from Chat.vue."""
    text = CHAT.read_text(encoding="utf-8")
    start = text.index(f"function {name}(")
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


_PROBE = """
const ref = (v) => ({ value: v })
const messages = ref(%s)
const following = ref(true)
const total = ref(0), sessions = ref([]), note = ref(null), live = ref(false)
let cursor = 0
let followed = 0
const settlePending = () => {}
const nextTick = (fn) => { fn() }
const stickToBottom = () => { followed += 1 }
%s

for (const [page, mode] of %s) absorb(page, mode)
console.log(JSON.stringify({
  rendered: messages.value.map((m) => [m.id, m.text]),
  cursor, followed,
}))
"""


def _drive(held, calls):
    """Run the real `absorb` over a held list and return the state it leaves.

    *calls* is a list of ``(page, mode)``, the arguments the poll, `start` and
    `loadEarlier` hand it.
    """
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")
    script = _PROBE % (json.dumps(held), _function("absorb"), json.dumps(calls))
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout)


def _page(messages, cursor):
    return {"messages": messages, "cursor": cursor, "total": cursor}


RECORD = {"id": "s/000001", "seq": 2, "text": "three running"}
ROW = {"id": "s:rows#0", "seq": 1, "text": "looking at the branch"}
LATE = {"id": "s:rows#1", "seq": 2, "text": "running the suite"}


def test_a_record_already_drawn_is_not_drawn_again():
    """The operator's screenshot. The late row landed below the cursor, so the
    record it precedes now answers to `seq` 3 and comes back on the next poll —
    under a number the pane has never seen, carrying the id it already holds."""
    redelivered = dict(RECORD, seq=3)
    state = _drive([ROW, RECORD], [[_page([LATE, redelivered], 3), "append"]])
    ids = [row[0] for row in state["rendered"]]
    assert ids.count("s/000001") == 1, f"the record was drawn twice: {ids}"
    assert ids == ["s:rows#0", "s/000001", "s:rows#1"]


def test_a_turn_the_pane_has_not_seen_is_appended():
    """The guard must not become a wall: an ordinary poll still adds what arrived."""
    state = _drive([ROW], [[_page([LATE], 2), "append"]])
    assert [row[0] for row in state["rendered"]] == ["s:rows#0", "s:rows#1"]
    assert state["cursor"] == 2


def test_a_turn_the_route_could_not_name_is_kept_rather_than_guessed_at():
    """Every turn the route serves carries an id
    (`test_every_turn_the_route_serves_carries_an_id`), so this is the shape of a
    bundle talking to a daemon older than itself: with no id there is nothing to
    recognise, and the pane shows the turn rather than dropping it."""
    nameless = {"id": None, "seq": 2, "text": "from an older daemon"}
    state = _drive([ROW], [[_page([nameless], 2), "append"]])
    assert [row[1] for row in state["rendered"]] == [
        "looking at the branch", "from an older daemon",
    ]


def test_an_empty_page_still_moves_the_cursor():
    """A poll that added nothing is still the server's answer about where the end
    is, and history renumbers underneath a live view."""
    state = _drive([ROW], [[_page([], 7), "append"]])
    assert state["cursor"] == 7
    assert [row[0] for row in state["rendered"]] == ["s:rows#0"]


def test_the_earlier_page_does_not_redraw_what_is_already_rendered():
    """`loadEarlier` asks for a range below the window and deliberately overlaps
    it, so the overlap has to be recognised on the way in."""
    older = {"id": "s:rows#-1", "seq": 0, "text": "earlier"}
    state = _drive([ROW], [[_page([older, dict(ROW)], 2), "prepend"]])
    assert [row[0] for row in state["rendered"]] == ["s:rows#-1", "s:rows#0"]


def test_the_earlier_page_recognises_the_overlap_after_a_renumber():
    """The backward read dedupes by the same `id` the forward one does, because
    `seq` cannot survive the event this whole change is about.

    A 42-turn conversation, the window holding its last four. The join drops one
    turn below the window, so everything after it renumbers down one and the turn
    the window starts on answers to 37 on the server while the pane holds it as
    38. `loadEarlier` asks from the held head and the page reaches that turn — a
    `seq` filter compares 37 against {38,39,40,41}, keeps it, and draws it twice.
    Both copies then carry one render key, since the row is keyed by `id`.
    """
    conversation = [{"id": f"c{n}", "text": f"turn {n}"} for n in range(42)]
    del conversation[5]                      # the join drops one turn below the window
    for seq, message in enumerate(conversation):
        message["seq"] = seq
    held = [dict(m, seq=n) for n, m in zip(range(38, 42), conversation[37:41])]

    state = _drive(held, [[_page(conversation[:38], 38), "prepend"]])

    ids = [row[0] for row in state["rendered"]]
    assert len(ids) == len(set(ids)), (
        f"drawn twice: {sorted({i for i in ids if ids.count(i) > 1})}"
    )
    assert ids == [m["id"] for m in conversation], "and the list is the server's"


_SETTLE_PROBE = r"""
const ref = (v) => ({ value: v })
const messages = ref(%s)
const pending = ref(%s)
const consumed = new Set()
const release = () => {}
const comparable = (text) => String(text || '').replace(/\s+/g, ' ').trim()
%s
settlePending()
console.log(JSON.stringify({
  left: pending.value.map((item) => item.text),
  consumed: [...consumed],
}))
"""


def _settle(rendered, pending):
    """Run the real `settlePending` over a rendered list and the bubbles it holds."""
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")
    script = _SETTLE_PROBE % (
        json.dumps(rendered), json.dumps(pending), _function("settlePending"),
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout)


def _said(seq, text, role="user"):
    return {"seq": seq, "id": f"t#{seq}", "role": role, "kind": "said", "text": text}


def test_an_older_identical_turn_does_not_settle_a_bubble():
    """Answering "yes" twice in one conversation: the second bubble must not be
    settled by the first answer's turn, or it stops showing that the new one is
    still in flight. The bound is the cursor the bubble snapshotted when it went.

    Collected cover for the rule the split restored. The four cases in
    `tests/_issue419_chat_read_path.py` drive the *anchored* version #419 is to
    design; three of them pass against this rule for a reason that has nothing to
    do with an anchor, which is why they do not stand in for this one.
    """
    state = _settle([_said(50, "yes")], [{"text": "yes", "since": 61}])
    assert state["left"] == ["yes"], "the bubble must stay up until its own turn lands"


def test_a_bubble_settles_against_its_own_turn():
    """And the guard must not become a wall."""
    state = _settle([_said(50, "yes"), _said(61, "yes")], [{"text": "yes", "since": 61}])
    assert state["left"] == []
    assert state["consumed"] == [61], "the turn it took is remembered by seq"
