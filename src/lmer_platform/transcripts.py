"""The chat view's source: a harness's JSONL transcript, normalised (spec D6/§10.4).

Why not the PTY log
-------------------
:mod:`lmer_platform.session_io` already serves the terminal, and that stream is
the *faithful* view — every byte the session drew. It is the wrong source for a
readable one: it is a redrawing screen, so deriving "who said what" from it means
reverse-engineering a TUI's cursor movements. The harness writes a structured
JSONL transcript instead — already ordered, already on disk, already surviving the
session — so this module reads that and normalises it to one message shape.

Spec D6 took the two costs of that in writing, and both shape this module:

- **The format is not a stable public contract.** Claude Code can change its
  record shapes in any release. So the adapter is small, isolated here, and
  fixture-tested against captured lines; and *nothing* it cannot parse is fatal.
- **It is append-only history, not a live keystroke view.** Reading comes from
  here while writing goes through the session's control plane
  (:func:`lmer_platform.session_io.send_input`), so the two halves have different
  latencies and the UI has to show a sent message as pending until it appears.

Three harnesses, three record vocabularies — and a fourth this platform owns
----------------------------------------------------------------------------
Claude Code, pi and codex each write their own JSONL, and the platform spawns all
three — so a view that spoke only the first normalised a pi or a codex run to
nothing at all (issue #280). The three type vocabularies are disjoint: claude
writes ``user``/``assistant``/``system``, pi writes ``session``, ``message`` and
its state changes, codex wraps everything in an envelope of ``session_meta``,
``response_item`` and ``event_msg``. So the dispatch is per *record* rather than
per file (:func:`_harness_of_record`), which costs a dict lookup and buys two
things: a transcript resolved from somewhere unexpected still reads, and a
:class:`Source` can say which harness wrote it on the evidence of the file rather
than on a label it was handed.

Each adapter is the allowlist the first one is — role, text, timestamp, tool
activity — and each of the two new ones carries a hazard the claude one does not,
stated in its own docstring: a pi session is a tree read here in file order, and
codex writes its injected context in the *operator's* role.

The fourth vocabulary is not a harness's at all. Those three are the maintained
set and the last dialects this module will ever learn (#296): every *other*
harness is a drop-in, and a drop-in cannot ship host-side code — the daemon must
never execute anything out of user-writable ``~/.lmer/harnesses``. What it can
ship is a converter that runs where the harness already runs, in the session
container, translating its native session files into the **canonical format**
this module documents and owns (``docs/TRANSCRIPT-FORMAT.md``, records prefixed
``lmer.``). That format serialises the normalised shape below rather than any
harness's, so :func:`_lmer_message` is field validation rather than
interpretation — and unlike the other three it *is* a contract, versioned here:
this build speaks version 1 (:data:`_LMER_FORMAT`) and a file that declares a
later one is read as nothing rather than guessed at, since the two sides ship
independently of each other.

Tolerance is the contract, not a nicety
---------------------------------------
Every read here is best-effort in the same way :mod:`lmer_platform.store` and
:mod:`lmer_platform.registry` are: an unparseable line is skipped, an unreadable
file is skipped, an absent transcript is an empty conversation. A transcript is a
*record* of a conversation that already happened — refusing to show any of it
because one line was torn by a crash mid-write is the one failure mode that
makes the view useless exactly when it is needed.

What is deliberately dropped
----------------------------
This is a presentation layer, and a transcript carries a great deal that has no
place on a phone (or in an HTTP response): token accounting, thinking-block
signatures, request ids, file-history snapshots, whole tool payloads. So the
normaliser is an allowlist — role, text, timestamp, and tool activity collapsed
to a name, a one-line hint and whether it failed. A failed tool is the
interesting case, which is why ``tool_use`` is correlated with its
``tool_result`` rather than just listed.

Being an allowlist, it drops whole record families: ``queue-operation`` rows of
every operation, ``file-history-delta``, ``attachment`` rows other than the one
below that carries a delivered message, and the system records that carry no
prose. The one thing that costs is a *delivery* dropped by accident — see the
queued message, further down.

How a session id becomes a file
-------------------------------
The two ids do not match, and cannot: a session's platform id (``s-<stamp>-<hex>``,
:func:`lmer_platform.registry.new_session_id`) is minted on the host before the
container exists, while the harness names its transcript after its *own* session
uuid, minted later and inside. So the link has to be recorded, and the spec's
session-entry shape has the field for it —
``"transcript": {"harness": "claude", "path": "…/projects/…/uuid.jsonl"}``.

Two resolutions are therefore supported, in order:

1. **The entry's ``transcript`` pointer**, ``path`` (one file) or ``dir`` (every
   ``.jsonl`` beneath it).
2. **The platform's own per-session directory**,
   :func:`session_transcript_dir` — a path derived from the session id, needing
   no recorded state at all. This is the resolution intended for spawned
   sessions: a directory the platform names, mounted into the container as the
   harness's projects dir, holds exactly that session's transcripts and outlives
   the container the way the PTY log does.

A pointer is *contained* before it is followed. Registry entries are
hand-editable debugging artifacts (:mod:`lmer_platform.registry` says so), and
this module is reachable from an HTTP route — so following an arbitrary path out
of one would turn a tampered entry into a file-read primitive on the daemon's
behalf. Same reasoning as ``registry.token_path`` refusing to dereference
``control.token_ref``.

Spanning the sessions of one run
--------------------------------
A run that stops to ask a question is answered by a *new* session (spec §5.4),
and the operator must still read one conversation — so the view concatenates
every session of the run. What can actually be joined on today is: registry
entries carrying the same ``run`` block (a crashed predecessor keeps its entry
alongside its replacement — see :mod:`lmer_platform.inventory`), plus the
tracked index's ``last_session_id``. Sessions that exited *cleanly* have had
their entry removed, and the index remembers only the most recent id, so those
drop out of the join: a run with three clean sessions can show the last one.
Widening that needs a session *list* in the tracked index, which is a change to
:mod:`lmer_platform.runs` rather than to this module.

Turns the operator's channel holds and the transcript never saw
---------------------------------------------------------------
An answer given through a live session's ask channel (:mod:`lmer_platform.ask`)
is a file the platform writes into a bind-mounted directory the session polls. It
never touches the PTY and it is never handed to the harness as input, so the
harness writes nothing down about it: the operator answers a question, the run
carries on, and the one readable record of the session does not contain the
operator's own words. Closing that gap is what the merge below does — the
channel's questions and answers are interleaved into the conversation by time.

On the **read** path, and only there. The transcript file is the harness's own
artifact and the thing :func:`scrub_transcript` rewrites; writing synthetic
operator turns into it would make the one durable record of a session partly
fiction, and no later reader would be able to tell which turns the session
actually received from which ones the platform had added. So nothing is written
back, and the merged turns exist for the length of one response.

What is merged, and what is not:

- **Questions and answers.** The question comes too because the answer is
  meaningless without it: an operator scrolling back to a bubble of their own
  saying "prep" needs "which branch?" above it. It is not a second copy of
  anything — ``lmer-ask`` is documented to take its text from a file or stdin
  precisely so a shell cannot eat it, so most of the time the question's words
  are not in the transcript at all, and where they are they are a bounded
  one-line hint on the ``Bash`` chip that ran the command rather than a turn.
  That is the whole dedupe story: there is no duplicate turn to remove.
- **Not notes.** A note wants no reply, so it is not half of an exchange anyone
  has to read back; it is progress, and the operator channel is where it shows.
- **Not the options a question offered.** What the operator chose is shown
  verbatim as their answer; the roads not taken are the ask panel's business.

A question is the agent's own words (it ran ``lmer-ask``) and lands as an
``assistant`` turn. An answer is the operator's and lands as a ``user`` turn,
which is what makes the view render it the way it renders everything else the
operator sent — verbatim, not through the markdown renderer. Both carry
:data:`ASK_CHANNEL_VIA` on ``via``, so neither is passed off as something the
harness recorded.

Both go through :func:`_present` like every other string this module emits, so
the credential scrub covers an operator who pasted a secret into an answer. The
channel *file* is not rewritten the way a transcript is
(:func:`scrub_transcript`): the container it is mounted into has already been
handed that answer, so there is nothing left to withhold from it, and the record
sits 0600 in a 0700 directory. The scrub's job here is the one it has everywhere
else in this module — what reaches a browser.

Ordering is by session, then by file, then by line — never a global sort on
timestamps. Sessions of a run are sequential in wall-clock time, so
concatenation *is* chronological, and it makes the sequence numbers append-only:
a client's cursor stays valid because new messages can only arrive at the end.
A global timestamp sort would renumber the whole history the moment one
out-of-order record appeared, and every cursor in flight would silently skip or
repeat.

The channel merge does not weaken that, and is fenced so it cannot:

- it interleaves *within* one session only, so the session order above still
  decides everything between sessions;
- the transcript's own messages keep their file order whatever their timestamps
  say, because :func:`_ordering_clock` clamps the clock it sorts on to be
  non-decreasing — a record dated in the past cannot move ahead of the one
  before it, and an undated one stays where the file put it;
- an answer is clamped to be no earlier than the question it answers, so the
  reply can never render above the question;
- where the two clocks land on the same instant — different sources, different
  precision, seconds against milliseconds — the transcript goes first, because
  the channel record is *caused by* the tool call the transcript is recording.

What it does cost is the honest version of what a late transcript file already
cost: an answer arriving with a timestamp earlier than messages already numbered
renumbers them, and a cursor in flight can then repeat a turn. It is bounded to
the run's newest session and to the instant an answer lands — which is while the
session is blocked on it and no later message exists yet.

A turn nobody said: the harness's background monitor
----------------------------------------------------
A session can arm a *watch* on a condition and be woken when it fires — that is
how an idle assistant learns the digest spool is no longer empty
(:mod:`lmer_platform.assistant`, "Nothing pushes"). The harness delivers the
event by injecting it into the session as a turn, and the record it writes for it
is a ``user`` turn with the operator's own role, a plain string of markup for its
content, and no ``isMeta`` — so the view drew a watch firing as a message the
operator had typed, in raw markup with double-escaped entities in it (operator,
live testing).

Nothing in that record's *role* separates it from a typed message, so the
separation is made here, on the two anchors the record does carry:
:func:`_injected_by_harness` (the harness put it there, not a keyboard) and the
monitor's own summary shape (:data:`_MONITOR_RE`). Both are required, and the
first is what keeps an operator who *pastes* that markup into the chat — which is
how this was reported — from having their own words attributed to the watch.

The monitor is one member of that family and not the whole of it, which is the
other half of the same report (#242): the harness injects a turn every time a
background command exits or a subagent stops, in the same role, with the same
anchor and no ``isMeta`` — and those rendered as the operator's own words, a
block of task ids and output-file paths per finished task. So
:func:`_injected_by_harness` decides ``kind`` on its own: anything it recognises
is ``injected``, and ``said`` is the kind that has to be earned. Only the
monitor's own shape earns the re-attribution below; the rest stay ``user`` turns
the view keeps behind its internals toggle, which is where a record the harness
wrote for the model belongs.

Such a turn is re-attributed to :data:`MONITOR_ROLE` and marked
:data:`MONITOR_VIA`, the same statement :data:`ASK_CHANNEL_VIA` makes: it is not
passed off as something a party said. Its text is the readable half of the markup
— the condition and the event, entity-decoded — because the view is deliberately
dumb about provenance and cannot be the place that learns to parse a harness's
injection format.

A turn the operator said and the transcript filed elsewhere: the queued message
--------------------------------------------------------------------------------
Type into a session that is mid-turn and the harness *queues* the message rather
than refusing it — and it then writes no ``user`` record for it at all. What it
writes is three rows: a ``queue-operation`` enqueue holding the text, the
matching ``remove`` when the queue is drained, and an ``attachment`` of type
``queued_command`` carrying the same text at the point in the file where the
model actually received it.

So the delivery is the attachment, and that is the row this normaliser turns into
a ``user`` turn (#275). The other two are the queue's bookkeeping and would each
duplicate it; and a message that waits for the turn *boundary* instead comes back
as an ordinary ``user`` record with no attachment beside it, measured over a real
session's 24 queued messages with zero overlap in either direction. File order is
delivery order, so nothing is reordered — several messages queued into one turn
are several attachments, in the order the model got them.

The queue is not the operator's alone, though: the harness pushes its own task
notifications through it, so a delivery becomes a turn only when the attachment's
``origin`` says a keyboard produced it (``{"kind": "human"}``). That is the
positive marker rather than a blocklist of machinery kinds, for the reason the
monitor section above gives — an internal turn drawn as the operator's words is
the failure mode this module keeps having to close, and a marker that must be
*present* cannot be walked through by a record shape a later release invents.

Dropping it was not cosmetic. The chat view holds a message the operator sent as
*pending* until the transcript shows it, and settles that bubble only on a
matching ``user`` turn — deliberately (#254, and #238 for the other way that
evidence can go missing). A delivery this module never emits therefore leaves a
bubble that can never settle, pinned below everything said after it, one more per
mid-turn message.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

from ask_channel.protocol import KIND_QUESTION, AskError

from . import ask, registry, runs
from .config import active_assistant_credential, active_secret
from .session_io import SessionNotFound
from .store import logs_dir

logger = logging.getLogger("lmer_platform.transcripts")

__all__ = [
    "TRANSCRIPT_ROOT", "ENV_TRANSCRIPT_ROOT", "NO_TRANSCRIPT_NOTE",
    "EMPTY_TRANSCRIPT_NOTE", "CONTAINER_TRANSCRIPT_DIR", "SESSION_DIR_MODE",
    "TRANSCRIPT_FILE_MODE",
    "DEFAULT_MESSAGE_LIMIT", "MAX_MESSAGE_LIMIT", "TEXT_LIMIT", "DETAIL_LIMIT",
    "MAX_MESSAGES_PER_SOURCE", "MAX_SOURCES", "TOOL_STATUSES", "MESSAGE_KINDS",
    "ASK_CHANNEL_VIA", "MONITOR_VIA", "MONITOR_ROLE",
    "ToolCall", "Message", "Source", "MessagePage",
    "transcript_root", "session_transcript_dir", "locate_sources",
    "sessions_for_run", "normalise_records", "read_source", "read_messages",
    "scrub_transcript", "scrub_session_transcripts",
    "LAST_TURN_TAIL_BYTES", "last_turn",
]

#: Where a harness leaves its transcripts when the platform has not been told
#: otherwise. Overridable for the same reason ``store.PLATFORM_DIR`` is — tests
#: point it at a temp dir — and read at call time so a patch takes effect.
TRANSCRIPT_ROOT: Optional[str] = None

ENV_TRANSCRIPT_ROOT = "LMER_PLATFORM_TRANSCRIPT_ROOT"

#: Claude Code's own layout under its home: one directory per workspace, one
#: ``<uuid>.jsonl`` per session inside it.
_DEFAULT_ROOT = Path(".claude") / "projects"

#: Suffix of the platform's per-session transcript directory, beside the PTY log
#: in ``logs/``. Derived from the session id so no recorded state is needed.
_SESSION_DIR_SUFFIX = ".transcript"

#: Where claude's transcript subdirectory is mounted *inside* a spawned session's
#: container: claude's own projects directory under the container home
#: (``HOME=/home/developer``, pinned by the host CLI's container env dict — the
#: same fact ``lmer_cli.mounts.CONTAINER_UV_CACHE_DIR`` hardcodes). Derived from
#: :data:`_DEFAULT_ROOT` so the destination a spawn mounts and the layout this
#: reader assumes cannot drift apart.
#:
#: Claude's alone, and no longer the whole story: a harness declares where it
#: writes (:attr:`lmer_cli.harness.Harness.session_dir`) and a spawn mounts one
#: host subdirectory of :func:`session_transcript_dir` at that declared path for
#: each harness that does, so pi's and codex's transcripts survive too
#: (#280). It stays here rather than moving into the registry because it is the
#: container-side half of :data:`_DEFAULT_ROOT`, whose host-side half is what
#: :func:`transcript_root` contains a recorded pointer against; ``lmer_platform``
#: keeps the two statements one fact by refusing to spawn at all if no harness
#: declares this path (``spawn._harness_session_dirs``).
CONTAINER_TRANSCRIPT_DIR = str(Path("/home/developer") / _DEFAULT_ROOT)

#: Mode for the platform's per-session transcript directory. A transcript holds
#: whatever the session said and read, so it is treated as the PTY log's
#: sensitivity, not a cache's: only the owner may traverse it.
SESSION_DIR_MODE = 0o700

#: Mode a scrubbed transcript is left with. The harness inside the container
#: writes with its own umask (0644 is normal); :func:`scrub_transcript` tightens
#: that as it rewrites, since the scrub cannot promise the file is clean.
TRANSCRIPT_FILE_MODE = 0o600

#: What to tell an operator when a session has no readable transcript. This is
#: the common answer today and saying nothing would read as "the run said
#: nothing", which is the one wrong impression to leave.
NO_TRANSCRIPT_NOTE = (
    "No transcript is readable for this run on this host. The harness writes its "
    "JSONL inside the container, so the chat view needs that directory mounted "
    "out (or its path recorded on the session entry) — until then the terminal "
    "is the complete record."
)

#: The other empty case, and a different fact: the file is there and holds nothing
#: a conversation can be built from. Normal for the first second of a session (the
#: harness writes its mode and permission rows before anything is said), and the
#: symptom of a transcript this adapter cannot read — which is why the two answers
#: are not the same sentence.
EMPTY_TRANSCRIPT_NOTE = (
    "The transcript for this run is on disk but has nothing to show yet. A session "
    "that has just started looks like this; so does one whose transcript this "
    "build cannot read."
)

#: Messages served by one read when the caller does not say. Small because the
#: first page is what a phone waits for on a cold open.
DEFAULT_MESSAGE_LIMIT = 100

#: Ceiling on one read. A client that wants more of a long conversation pages.
MAX_MESSAGE_LIMIT = 500

#: Per-message text ceiling, and what survives is the END of the message — see
#: :func:`_present`.
#:
#: Raised from 1500, which was about three paragraphs and so cut ordinary agent
#: reports constantly. Two of the three reasons for a tight cap have since gone
#: away: a tool-heavy turn is bounded separately (``DETAIL_LIMIT`` collapses each
#: tool call to one line, so this only ever capped *prose*), and the chat pane now
#: bounds and scrolls each message, so a long one no longer pushes the page
#: around. What remains is payload size — a page of 100 messages at this ceiling
#: is a few hundred KB over a LAN, which is why there is still a limit and why
#: clients page rather than asking for everything.
TEXT_LIMIT = 8000

#: Ceiling on a tool's one-line hint and on a tool failure's text.
DETAIL_LIMIT = 160

#: Memory ceiling per transcript file, not a paging feature: a file this long is
#: pathological, and reaching the cap is reported rather than hidden.
MAX_MESSAGES_PER_SOURCE = 5000

#: Ceiling on transcript files read for one run, for the same reason.
MAX_SOURCES = 64

#: Longest line this reader will parse. A torn write or a binary file can present
#: as one enormous "line", and json.loads on it would cost more than the whole
#: transcript.
_MAX_LINE_BYTES = 1024 * 1024

#: How much of a transcript's tail :func:`last_turn` reads. Big enough to hold
#: the last few records whatever the file has grown to (a turn with a large tool
#: result runs to tens of kilobytes), small enough that a fleet poll can afford
#: one per stalled run. Too small answers ``None``, which is the safe direction.
LAST_TURN_TAIL_BYTES = 256 * 1024

#: How a tool call ended. ``pending`` is a real state, not an unknown: it is the
#: tool the session is running right now, which is often the answer to "what is
#: it doing".
TOOL_STATUSES = ("ok", "failed", "pending")

#: Who a message is from, in the sense the view cares about. ``injected`` is the
#: machinery talking to the model — hook feedback, an expanded slash command —
#: which explains a great deal of agent behavior but is not a person speaking,
#: so the view must be able to tell them apart.
MESSAGE_KINDS = ("said", "injected", "notice")

#: What ``Message.via`` says when a turn came from the session's ask channel
#: rather than from the harness transcript. The view needs to be able to say so:
#: an answer the operator gave through the channel is genuinely theirs, but it is
#: not something the harness wrote down, and a turn that claimed to be transcript
#: content while being assembled here is the one thing this merge must not do.
ASK_CHANNEL_VIA = "ask"

#: What ``Message.via`` says when a turn is the harness's background monitor
#: reporting a condition the session asked to be woken for. The same statement
#: :data:`ASK_CHANNEL_VIA` makes, for the opposite direction: those words are in
#: the transcript, but nobody in the conversation said them, and the record the
#: harness writes for them is indistinguishable by role from a typed message.
MONITOR_VIA = "monitor"

#: The role such a turn is re-attributed to. Not ``user``, which is the role the
#: record arrives with and the reason the view drew a watch firing as an operator
#: message; not ``assistant`` either, since the session did not write it — the
#: watch is a third party and the view has to be able to draw it as one.
MONITOR_ROLE = "monitor"

#: Record ``type`` values each harness writes. Disjoint, which is what makes the
#: dispatch per record (:func:`_harness_of_record`) rather than per file. Each
#: set names the harness's *bookkeeping* types as well as its conversation ones,
#: so a session that has so far written only its header is still labelled by what
#: wrote it — and so a type this build has never seen stays unrecognised instead
#: of being handed to whichever adapter guessed first.
#: ``attachment`` is a turn when it delivers a queued command (see
#: :data:`_QUEUED_COMMAND_ATTACHMENT`); ``queue-operation`` never is, but it is
#: still claude's bookkeeping and belongs to claude's vocabulary — leaving it
#: out would let a queue-heavy transcript open with rows no format claims.
_CLAUDE_RECORD_TYPES = frozenset({
    "user", "assistant", "system", "attachment", "queue-operation",
})
_PI_RECORD_TYPES = frozenset({
    "session", "model_change", "thinking_level_change", "message",
})
_CODEX_RECORD_TYPES = frozenset({
    "session_meta", "response_item", "event_msg", "turn_context", "world_state",
})

#: The canonical vocabulary — "lmer transcript format", version 1 (#296). Not a
#: harness's dialect but this platform's own, written by a drop-in's in-container
#: converter and the one format here that is a public contract
#: (``docs/TRANSCRIPT-FORMAT.md``). Namespaced so it can never collide with a
#: harness's own types, which claim bare words.
_LMER_RECORD_TYPES = frozenset({"lmer.meta", "lmer.message", "lmer.tool_update"})

#: The canonical-format version this build speaks. A file's ``lmer.meta`` states
#: its own (``format``), and a *higher* number is the one thing that cannot be
#: read additively: version 1 tolerance rests on unknown fields and types being
#: safely ignorable, and a later version is free to change what an existing field
#: *means* — ``api_refusal`` already decides a stall's verdict
#: (:func:`lmer_platform.inventory._stalled`), so misreading it is worse than
#: showing nothing. The gate matters precisely because the two sides ship apart:
#: a drop-in's converter is updated by its author, in the container, while this
#: reader is whatever the host happens to be running.
_LMER_FORMAT = 1

#: What actually claims a record for that vocabulary — the prefix, not the set
#: above. Version 1 readers *ignore* unknown ``lmer.*`` types, because the format
#: evolves additively, and ignoring is not the same as failing to recognise: a
#: type claimed here and skipped leaves the file labelled canonical, while one
#: falling through to "nobody recognises this" would make a version-2 file read as
#: a format this build cannot open at all — the one wrong answer, since every
#: record it *does* understand is still readable.
_LMER_TYPE_PREFIX = "lmer."

#: What labels a canonical file that declares no usable ``lmer.meta``: the
#: format's name, since the one thing known about such a writer is which format
#: it chose. A valid meta record replaces it with the drop-in's own harness name.
_LMER_HARNESS = "lmer"

#: The roles a canonical record may claim — the ones the view knows how to title.
#: ``monitor`` is included because a converter may have the provenance to say a
#: turn was a watch firing, which for claude has to be inferred
#: (:func:`_monitor_report`); anything outside this set is a record skipped
#: rather than a turn attributed to a party the view would have to invent.
_LMER_ROLES = frozenset({"user", "assistant", "system", MONITOR_ROLE})

#: Grammar a canonical file's declared harness name must satisfy, restated from
#: ``lmer_cli.user_harnesses`` (which holds manifests to it) rather than imported
#: from it: this name becomes a label in an HTTP response, written by a file the
#: observed container can write, so it is validated here whatever a manifest
#: elsewhere allows. A name that fails it reads as no name at all.
#:
#: Matched with :meth:`re.Pattern.fullmatch`, not ``match``: ``$`` also matches
#: *before* a trailing newline, so ``"opencode\n"`` would satisfy this pattern and
#: reach the API as the source's label verbatim. The anchors are kept so the
#: pattern stays literally the one ``user_harnesses`` holds manifests to — a test
#: asserts the two strings are equal, so the grammars cannot drift apart in
#: silence.
_HARNESS_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

#: Wrapper tags Claude Code puts around injected user content. Stripped from
#: *user* text only, and only these names: a blanket tag strip would eat the
#: markup in an assistant message that is discussing markup.
_WRAPPER_TAGS = (
    "command-message", "command-name", "command-args", "command-contents",
    "local-command-stdout", "local-command-stderr", "local-command-caveat",
    "task-notification", "system-reminder", "tool_use_error",
)
_WRAPPER_RE = re.compile(r"</?(?:%s)>" % "|".join(_WRAPPER_TAGS))

#: ``origin.kind`` on a record the harness's task machinery injected — a watch
#: firing, a background command finishing, an agent stopping. Not a documented
#: contract (spec D6), which is why it is one of two anchors rather than the only
#: one; the other is what the injection *says*.
_TASK_NOTIFICATION_ORIGIN = "task-notification"

#: ``promptSource`` on any user-role record the harness wrote rather than took
#: from a keyboard. Broader than the field above and checked as well, since either
#: one being present is enough to know a keyboard was not involved.
_INJECTED_PROMPT_SOURCE = "system"

#: ``origin.kind`` the harness puts on a queued delivery a *keyboard* produced.
#: The same ``origin`` shape :func:`_injected_by_harness` reads on user records,
#: used in the opposite direction: there the machinery kinds are recognised, here
#: the human one is *required*. See :func:`_message_from_record`.
_HUMAN_ORIGIN = "human"

#: ``attachment.type`` on the record that delivers a message the operator typed
#: while the session was mid-turn. The harness queues such a message and writes
#: three records for it — a ``queue-operation`` enqueue, its ``remove``, and this
#: attachment — of which only the attachment is the *delivery*, written at the
#: point in the file where the model received the text. See
#: :func:`_message_from_record` for why the other two stay dropped.
_QUEUED_COMMAND_ATTACHMENT = "queued_command"

#: How a background monitor's injection reads once :func:`_strip_wrappers` has
#: taken the ``<task-notification>`` wrapper off it, from the live incident:
#:
#:     <task-id>b63lu8hv2</task-id>
#:     <summary>Monitor event: "lmer pending digest count &amp;gt; 0"</summary>
#:     <event>fleet digests pending: 1</event>
#:
#: Anchored at the start of the turn and matching the *structure* — a message that
#: merely mentions a task id is a message, not an event — and requiring the
#: harness's own summary wording, because the rest of that family (an agent that
#: stopped, a command that exited) is a different notification with a different
#: thing to say about it. ``<task-id>`` is optional: the notification is built
#: field by field and skips the ones it has no value for.
_MONITOR_SUMMARY_PREFIX = 'Monitor event: "'
_MONITOR_RE = re.compile(
    r"\A(?:<task-id>[^<>]*</task-id>\s*)?"
    r"<summary>%s(?P<condition>.*?)\"</summary>"
    r"(?:\s*<event>(?P<event>.*?)</event>)?"
    % re.escape(_MONITOR_SUMMARY_PREFIX),
    re.DOTALL,
)

#: The three entities Claude Code escapes on its way into an injected turn, and
#: nothing else (its escaper is ``&`` then ``<`` then ``>``). Undone in one pass
#: over all three rather than three passes, so a literal ``&lt;`` in the event's
#: own text cannot be produced by decoding the ``&amp;`` in front of it.
_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">"}
_ENTITY_RE = re.compile("|".join(map(re.escape, _ENTITIES)))

#: How many decode passes an injected turn gets. More than one because the live
#: incident carried ``&amp;gt;`` — the escaper ran over text that had already been
#: escaped once — and one pass would leave ``&gt;`` on the operator's screen.
#: Bounded rather than run to a fixed point: this is decoding text of unknown
#: provenance, and "until it stops changing" is a rule that rewrites prose which
#: was never markup.
_MAX_ENTITY_PASSES = 2

#: Wrapper tags codex puts around the context it injects — in the *operator's*
#: role, which is the whole reason :func:`_codex_injected` exists. Kept apart
#: from :data:`_WRAPPER_TAGS` so neither harness's names are stripped out of the
#: other's turns.
_CODEX_WRAPPER_TAGS = (
    "environment_context", "skills_instructions", "user_instructions",
    "turn_context",
)
_CODEX_TAG_RE = re.compile(
    r"</?(?:%s)\b[^>]*>" % "|".join(_CODEX_WRAPPER_TAGS)
)
_CODEX_OPENS_RE = re.compile(r"\A\s*<(?:%s)\b" % "|".join(_CODEX_WRAPPER_TAGS))

#: One *opening* wrapper tag, for :func:`_codex_unwrapped`'s scan. Fixed text
#: and a lookahead, with nothing of variable length in it: the whole point of
#: that function is that it cannot backtrack, and a pattern is used only to find
#: the next candidate rather than to match a block. The lookahead admits the
#: attribute form ``<turn_context foo="1">`` and nothing looser; what it buys
#: over the ``\b`` of the patterns above is the punctuation-adjacent forms —
#: ``<turn_context-foo>`` is a wrapper to ``\b`` and prose to this scanner, the
#: safe direction for a tag nothing writes. (``<environment_contextual>`` is not
#: an example: ``\b`` does not match between ``x`` and ``t`` either.) ``\Z``
#: catches a name that runs off the end of a torn write.
_CODEX_OPEN_RE = re.compile(
    r"<(?P<tag>%s)(?=[>\s]|\Z)" % "|".join(_CODEX_WRAPPER_TAGS)
)

#: Content block types that carry prose, per harness. Claude's ``text`` covers pi
#: too; codex names its own ``input_text`` on the way in and ``output_text`` on
#: the way out. An allowlist rather than a denylist is what keeps a model's
#: private reasoning out of the view whatever the next release calls it.
_CODEX_TEXT_TYPES = ("input_text", "output_text")

#: Keys a tool's input might carry that identify *what* it acted on, most
#: specific first. A generic pick rather than a per-tool table: the tool set
#: changes with every harness release, and a table would rot into blank chips.
#: ``cmd`` is codex's spelling of ``command``; ``path`` is pi's of ``file_path``.
_DETAIL_KEYS = (
    "command", "cmd", "file_path", "path", "pattern", "url", "query",
    "description", "prompt", "subagent_type",
)

#: Words that name a credential in a shell argument or a header.
_CREDENTIAL_WORDS = (
    "token", "secret", "passwd", "password", "apikey", "api_key", "credential",
)

#: Longest run of name characters the credential patterns will consider around
#: one of :data:`_CREDENTIAL_WORDS`. Generous for a real option or variable name
#: (``--dangerously-bypass-approvals-and-sandbox`` is 40), and a bound at all
#: because the alternative is a quantifier that rescans a long run of word
#: characters from every position in it — see :data:`_CREDENTIAL_PATTERNS`.
_MAX_NAME_CHARS = 64

_REDACTED = "<redacted>"

#: Credential shapes stripped from everything this module emits. Not a hunch: a
#: real transcript on this codebase recorded a browser tool navigating to
#: ``http://x:<the platform's shared secret>@127.0.0.1:8620/`` — the agent had put
#: the credential in a URL, and the harness wrote the URL down. Tool inputs are
#: whole shell command lines, so ``curl -H "Authorization: Bearer …"`` and
#: ``--fastapi-token …`` are equally routine.
#:
#: Why bother, when the PTY log route already serves those same bytes: because
#: this is a *new* payload built for a browser to display and screenshot, and the
#: rest of this codebase does not make the "it leaked somewhere already" argument
#: (see ``clone_and_exec._scrub_credentials``, whose URL pattern is reused below,
#: and ``session_io._post`` scrubbing the control token out of relayed text).
#: Over-redacting costs a masked word in a chat bubble; under-redacting puts a
#: live credential on a phone screen.
#:
#: Every pattern here has to stay **linear** in the length of what it is given. A
#: transcript record legitimately carries tens of kilobytes in one string (a
#: pasted file, a base64 attachment) and this module hands whole strings to
#: :func:`_scrub` — twice now, once for the browser and once for the file left on
#: disk. Two earlier spellings had an unbounded ``[a-z0-9_]*`` in front of the
#: credential word, which made the engine rescan the run of word characters from
#: every position inside it: 3 KB of base64 cost a second of CPU, 300 KB cost the
#: better part of an hour, and the only symptom would have been a chat request
#: that never came back. Hence: no quantifier that can span the input ahead of a
#: literal, and any name-shaped run bounded by :data:`_MAX_NAME_CHARS`.
_CREDENTIAL_PATTERNS = (
    # ``scheme://user:pass@host`` — the leak clone_and_exec._scrub_credentials
    # exists to fix, in the one shape a regex catches reliably.
    (re.compile(r"(://)[^/\s@]*@"), r"\1"),
    # An HTTP credential header, however it was quoted.
    (
        re.compile(r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)[^\s\"']+"),
        r"\g<1>" + _REDACTED,
    ),
    # ``--fastapi-token XYZ`` and friends: a long option whose name says what
    # follows it is. The ``--`` has to stay in the pattern (it is what makes this
    # an option rather than prose), so the name run around the credential word is
    # bounded — and required to *start* with an alphanumeric, which no long
    # option's name violates and which is what makes a rule of dashes (transcripts
    # are full of them) fail at the second character instead of being rescanned
    # once per dash.
    (
        re.compile(
            r"(?i)(--(?:[a-z0-9][a-z0-9-]{0,%d})?(?:%s)[a-z0-9-]{0,%d}[= ])[^\s\"']+"
            % (_MAX_NAME_CHARS - 1, "|".join(_CREDENTIAL_WORDS), _MAX_NAME_CHARS)
        ),
        r"\g<1>" + _REDACTED,
    ),
    # ``NAME=value`` / ``"name": "value"`` where the name says the same. Eight
    # characters minimum, so ordinary prose ("secret: no") is left alone.
    #
    # The match starts at the credential word, not at the start of the name:
    # whatever precedes it (``LMER_FASTAPI_`` in ``LMER_FASTAPI_TOKEN=…``) is
    # outside the match and therefore kept verbatim, exactly as it was when the
    # pattern captured and re-emitted it — same output, without a quantifier that
    # can run the length of the input before the literal it is looking for.
    (
        re.compile(
            r"(?i)((?:%s)[a-z0-9_]{0,%d}[\"']?\s*[=:]\s*[\"']?)[^\s\"',;&|]{8,}"
            % ("|".join(_CREDENTIAL_WORDS), _MAX_NAME_CHARS)
        ),
        r"\g<1>" + _REDACTED,
    ),
    # Provider-prefixed tokens, the same two shapes work_repo.utils redacts.
    (re.compile(r"\b(?:glpat-|sk-)[A-Za-z0-9_\-.]{20,}"), _REDACTED),
)

#: Shortest configured secret :func:`_platform_secret` will strike out of a
#: transcript by value. The generated one is 43 characters
#: (:func:`lmer_platform.config.ensure_secret`), so this only ever excludes a
#: hand-written one — and it has to, because a five-character "secret" would
#: turn every accidental occurrence of those five characters into ``<redacted>``
#: and make the conversation unreadable. Skipping it costs nothing the shape
#: patterns were already doing: ``LMER_PLATFORM_SECRET=hunter2`` is still masked
#: by the ``NAME=value`` rule below, because that reads the *name*.
_MIN_SECRET_CHARS = 12


def _platform_secret() -> Optional[str]:
    """The platform's shared secret, when there is one worth striking out.

    The shape patterns above catch a credential *because of what surrounds it* —
    a scheme, a header, an option, a name. This one is caught because the value
    itself is known, which is the only rule that survives an agent running
    ``env`` and the harness writing the output down: the shared secret has no
    prefix, no delimiter and no giveaway, so nothing shaped can find it in
    ``LMER_PLATFORM_SECRET`` echoed back on its own line, in a ``curl`` line
    that put it in a query string, or in a sentence.

    It matters more than the other patterns rather than less. A session's
    control token drives one session; this one spawns containers, so a
    transcript that persisted it would be a container-spawning credential
    sitting in a file the chat view serves to a browser.
    """
    return _long_enough(active_secret(), what="secret")


def _assistant_credential() -> Optional[str]:
    """The assistant's minted credential, when there is one (issue #244).

    :func:`_platform_secret`'s reason with more at stake: this credential reaches
    a *container* whose transcript the chat view renders, so an incarnation that
    runs ``env`` writes it into the file this module serves to a browser — and it
    opens the same API as the shared secret.
    """
    return _long_enough(active_assistant_credential(), what="assistant credential")


def _long_enough(value: Optional[str], *, what: str) -> Optional[str]:
    """*value*, unless it is too short to tell from prose."""
    if value is None:
        return None
    if len(value) < _MIN_SECRET_CHARS:
        logger.debug(
            "platform_transcript_secret_too_short kind=%s chars=%d — not masked "
            "by value; a credential this short cannot be told from prose",
            what, len(value),
        )
        return None
    return value


@dataclass
class ToolCall:
    """One tool the assistant ran, collapsed to what a phone can show.

    Mutable on purpose. The transcript records a ``tool_use`` and its
    ``tool_result`` as separate lines, so the outcome is only known later —
    :func:`normalise_records` makes one pass and patches the call in place when
    the result arrives. Two passes would mean holding the whole file.
    """

    name: str
    detail: Optional[str] = None
    status: str = "pending"
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "detail": self.detail,
            "status": self.status,
            "error": self.error,
        }


@dataclass
class Message:
    """One normalised turn: who, when, what they said, and what it did.

    ``seq`` is assigned by :func:`read_messages` across the whole run, because
    that is the only place that knows the order of the run's sessions.

    ``origin`` names the transcript file a turn was read from, and ``via`` names
    how the words got here when that is not simply a party talking:
    :data:`ASK_CHANNEL_VIA` for a turn merged in from the session's ask channel,
    :data:`MONITOR_VIA` for one a background watch fired into the session. So the
    two are not exclusive and the pair is the whole statement — a channel turn has
    a ``via`` and no ``origin`` (it is in no transcript file, which is why it has
    to be merged in), while a monitor turn has both (it is in the file, and still
    nobody said it).
    """

    role: str
    kind: str
    text: str
    at: Optional[str] = None
    origin: Optional[str] = None
    truncated: bool = False
    tools: list = field(default_factory=list)
    seq: int = 0
    via: Optional[str] = None
    #: Whether the harness wrote this turn to say the provider refused
    #: (``isApiErrorMessage``). The fact; the two below are detail a build may
    #: omit, so a refusal carrying neither is still a refusal. ``False`` on every
    #: ordinary turn, including prose *about* an outage — which is why this
    #: crosses as a field rather than being read out of :attr:`text`.
    api_refusal: bool = False
    #: The provider's error class (``billing_error``, ``server_error``) and HTTP
    #: status. ``None`` when the refusal carried neither, or when there is none.
    api_error: Optional[str] = None
    api_error_status: Optional[int] = None

    @property
    def empty(self) -> bool:
        """Whether this message would render as nothing at all."""
        return not self.text and not self.tools

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "role": self.role,
            "kind": self.kind,
            "at": self.at,
            "origin": self.origin,
            "via": self.via,
            "text": self.text,
            "truncated": self.truncated,
            "tools": [tool.to_dict() for tool in self.tools],
            "api_refusal": self.api_refusal,
            "api_error": self.api_error,
            "api_error_status": self.api_error_status,
        }


@dataclass(frozen=True)
class Source:
    """One transcript file, and which platform session it belongs to.

    ``path`` stays out of :meth:`to_dict`: this object is built for an HTTP
    response, and a host filesystem layout is not the browser's business.
    """

    path: Path
    session: str
    harness: str = "claude"
    messages: int = 0
    capped: bool = False

    @property
    def id(self) -> str:
        """The harness's own session id — the file's name without its suffix."""
        return self.path.stem

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session": self.session,
            "harness": self.harness,
            "messages": self.messages,
            "capped": self.capped,
        }


@dataclass(frozen=True)
class MessagePage:
    """One slice of a run's conversation, plus where to continue from.

    ``cursor`` is the next ``since`` to ask for, so a client that polls with it
    can neither skip nor repeat: sequence numbers are append-only (see the module
    docstring on ordering).
    """

    messages: tuple
    start: int
    cursor: int
    total: int
    sources: tuple
    note: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "messages": [message.to_dict() for message in self.messages],
            "from": self.start,
            "cursor": self.cursor,
            "total": self.total,
            "sessions": [source.to_dict() for source in self.sources],
            "note": self.note,
        }


def transcript_root() -> Path:
    """Root the harness writes transcripts under, on this host.

    Module attribute, then environment, then the harness's default under the
    invoking user's home. Only ever used to *contain* a recorded pointer and to
    resolve one — nothing is scanned speculatively, because attaching the wrong
    conversation to a run is worse than attaching none.
    """
    if TRANSCRIPT_ROOT is not None:
        return Path(TRANSCRIPT_ROOT)
    configured = os.environ.get(ENV_TRANSCRIPT_ROOT, "").strip()
    if configured:
        return Path(configured)
    return Path.home() / _DEFAULT_ROOT


def _validated(session_id: str) -> str:
    """Reject an id that could not name a session, before it reaches a path.

    Borrowed from the registry rather than written again, exactly as
    :mod:`lmer_platform.session_io` does: two different notions of a legal
    session id is how ``..`` eventually gets past one of them. Raising
    :class:`~lmer_platform.session_io.SessionNotFound` also means the HTTP route
    keeps one exception-to-status mapping instead of two.
    """
    try:
        registry.session_path(session_id)
    except registry.RegistryError as exc:
        raise SessionNotFound(str(exc)) from exc
    return session_id


def session_transcript_dir(session_id: str) -> Path:
    """The platform's own transcript directory for one session.

    Beside the PTY log, and named from the session id, so it needs no recorded
    state: a spawn that mounts this directory in as the harness's projects dir
    gives every session an unambiguous, container-outliving transcript home. The
    directory not existing is the normal case today and reads as "no
    transcript".
    """
    return logs_dir() / f"{_validated(session_id)}{_SESSION_DIR_SUFFIX}"


def _contained(candidate: Path, roots: Iterable[Path]) -> bool:
    """Whether *candidate* resolves inside one of *roots*.

    Resolve-then-compare, so ``..`` segments and symlinks are both covered —
    the check ``api._safe_asset`` makes of a static path, for the same reason.

    ``RuntimeError`` is caught alongside ``OSError`` because that is what
    :meth:`pathlib.Path.resolve` raises for a symlink loop on Python 3.12 (3.13
    made it an ``OSError``). Not a hypothetical: the harness's projects directory
    is writable by the container, so the loop can be created by the thing being
    observed — and an unhandled one would turn the chat view into a 500.
    """
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return False
    for root in roots:
        try:
            base = root.resolve()
        except (OSError, RuntimeError):
            continue
        if resolved == base or str(resolved).startswith(str(base) + os.sep):
            return True
    return False


def _jsonl_files(directory: Path) -> list:
    """Every ``.jsonl`` *inside* *directory*, in a stable order.

    Recursive because the harness keeps one subdirectory per workspace, so a
    mounted projects dir has its files one level down. Absent or unreadable is
    an empty list — the caller's answer is the same either way.

    "Inside" is checked rather than assumed, and the check is the point of this
    function rather than a belt on it. :meth:`Path.rglob` and :meth:`Path.is_file`
    both follow symlinks, and every directory this is pointed at is mounted
    read-write into the container it belongs to — so a link named ``x.jsonl``
    pointing at another session's transcript is something the *observed* session
    can plant, and following one would serve another run's conversation back
    through ``GET /api/sessions/{id}/messages``. Transcripts are the artifact most
    likely to carry another run's credentials and private repo content, and the
    session that plants the link is agent-driven rather than operator-driven.

    The refusal lives here so no caller can be the one that forgot it:
    :func:`locate_sources` was, while :func:`scrub_session_transcripts` was not.
    """
    try:
        found = sorted(path for path in directory.rglob("*.jsonl") if path.is_file())
    except OSError as exc:
        logger.warning(
            "platform_transcript_dir_unreadable dir=%s error=%s", directory, exc
        )
        return []
    kept = []
    for path in found:
        if _contained(path, [directory]):
            kept.append(path)
            continue
        logger.warning(
            "platform_transcript_link_refused dir=%s path=%s — resolves outside "
            "the directory it was found in", directory, path,
        )
    return kept


def _pointer_sources(session_id: str, pointer: dict, roots: list) -> list:
    """Resolve a session entry's recorded ``transcript`` pointer.

    Refuses anything outside *roots* — see the module docstring on why a registry
    entry's path is not followed on trust.
    """
    harness = pointer.get("harness")
    harness = harness if isinstance(harness, str) and harness else "claude"

    raw_path = pointer.get("path")
    if isinstance(raw_path, str) and raw_path.strip():
        candidate = Path(raw_path)
        if not _contained(candidate, roots):
            logger.warning(
                "platform_transcript_pointer_refused session=%s — recorded path "
                "resolves outside the transcript roots", session_id,
            )
            return []
        if not candidate.is_file():
            return []
        return [Source(path=candidate, session=session_id, harness=harness)]

    raw_dir = pointer.get("dir")
    if isinstance(raw_dir, str) and raw_dir.strip():
        candidate = Path(raw_dir)
        if not _contained(candidate, roots):
            logger.warning(
                "platform_transcript_pointer_refused session=%s — recorded dir "
                "resolves outside the transcript roots", session_id,
            )
            return []
        return [
            Source(path=path, session=session_id, harness=harness)
            for path in _jsonl_files(candidate)
        ]
    return []


def locate_sources(session_id: str) -> list:
    """Transcript files belonging to one session, best first.

    The recorded pointer wins over the derived directory: a session that said
    where its transcript is knows better than a convention. Neither resolving is
    a normal answer — see :data:`NO_TRANSCRIPT_NOTE`.
    """
    own_dir = session_transcript_dir(session_id)
    roots = [own_dir, transcript_root()]

    entry = registry.read_session(session_id)
    pointer = (entry or {}).get("transcript")
    if isinstance(pointer, dict) and pointer:
        located = _pointer_sources(session_id, pointer, roots)
        if located:
            return located

    return [Source(path=path, session=session_id) for path in _jsonl_files(own_dir)]


def _run_key_of(entry: Optional[dict]) -> Optional[tuple]:
    """One entry's run key — :func:`lmer_platform.runs.run_key_of_entry`'s job,
    reused rather than spelled again."""
    return runs.run_key_of_entry(entry)


def sessions_for_run(session_id: str) -> list:
    """Every session of *session_id*'s run that this host can still name.

    Ordered oldest first, because that is the order the conversation happened
    in. The join and its limits are in the module docstring: registry entries
    sharing a ``run`` block, plus the tracked index's ``last_session_id``. A
    session whose run cannot be identified is its own conversation of one, which
    is the truthful answer rather than an empty view.
    """
    _validated(session_id)
    # Both halves live in ``runs.run_for_session``, which the check-in stamping
    # reads too: two resolutions of "which run is this session" is how the
    # stamping surface came to be narrower than the serving one (issue #244).
    key = runs.run_for_session(session_id)

    # Tolerant by its own contract: a corrupt index reads as empty rather than
    # raising, so there is nothing to guard against here.
    tracked = runs.list_tracked()

    if key is None:
        return [session_id]

    found: dict[str, str] = {}
    for other in registry.list_sessions(live_only=False):
        if _run_key_of(other) != key:
            continue
        other_id = other.get("id")
        if isinstance(other_id, str):
            found[other_id] = other.get("started_at") or other_id

    for candidate in tracked:
        if (candidate.host, candidate.project, candidate.slug) != key:
            continue
        last = candidate.last_session_id
        if isinstance(last, str) and last and last not in found:
            found[last] = candidate.last_seen or candidate.first_seen or last

    # The session that was asked about is always in the answer, even if nothing
    # else could date it — an empty view for a session that demonstrably exists
    # would read as a broken page rather than as a missing transcript.
    found.setdefault(session_id, "")
    return sorted(found, key=lambda sid: (found[sid], sid))


def _scrub(text: str) -> str:
    """Mask credential shapes. See :data:`_CREDENTIAL_PATTERNS` for why.

    What this is, exactly: regexes matching a handful of credential *shapes* —
    URL userinfo, an ``Authorization: Bearer/Basic`` header, ``--…token …``,
    ``NAME_TOKEN=…``, ``glpat-``/``sk-`` prefixes — plus one credential matched
    by *value*, this platform's own shared secret. It reduces the credential
    material a transcript carries; it does not remove it. Any *other* secret
    with no recognisable prefix, name or delimiter — a bare hex string the agent
    pasted on its own line — passes straight through, and nothing here can tell
    it from a git sha.

    So this is defense in depth beside the 0700 directory and the 0600 file
    (:data:`SESSION_DIR_MODE`, :data:`TRANSCRIPT_FILE_MODE`), not a licence to
    treat a scrubbed transcript as safe to publish.

    The single definition, used by both directions: the read path
    (:func:`_present`, on every string this module emits) and the write path
    (:func:`scrub_transcript`, on the file left behind when a session ends).

    The platform's own credentials are struck out **first**, by value rather
    than by shape (:func:`_platform_secret`, :func:`_assistant_credential`).
    First because they are the replacements that cannot be wrong: a shape
    substitution that rewrote the text around one could leave the value itself
    behind in a form no later pattern recognises.
    """
    for known in (_platform_secret(), _assistant_credential()):
        if known:
            text = text.replace(known, _REDACTED)
    for pattern, replacement in _CREDENTIAL_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _present(text: str, limit: int, *, keep: str = "head") -> tuple:
    """Scrub then truncate. Returns ``(text, truncated)``.

    The single chokepoint every string this module emits passes through, which is
    what makes the credential scrub a property of the module rather than of the
    caller that remembered. Scrub first: truncating first could cut a token in
    half and leave a prefix no pattern recognises.

    ``keep`` decides which end survives, and the two callers want opposite ends
    (the operator asked: "conversation is 100% useless if its cut off at the tail,
    so if we have to trim it should be at the head"):

    - ``"tail"`` for a message's prose. An agent's turn *ends* with its
      conclusion — what it did, what it found, what it wants. Keeping the opening
      and dropping that leaves the preamble and throws away the point, which is
      how a trimmed report became unreadable rather than merely shortened.
    - ``"head"`` for a one-line hint. A tool line is a command or a path, where
      the beginning identifies it and the end is an argument; keeping the tail of
      one would show the last flag of something unnamed.

    Neither end is right in general, which is why this is a parameter rather than
    a policy.
    """
    text = _scrub(text)
    if len(text) <= limit:
        return text, False
    if keep == "tail":
        return text[-limit:].lstrip(), True
    return text[:limit].rstrip(), True


def _first_line(text: str, limit: int = DETAIL_LIMIT) -> Optional[str]:
    """The first non-empty line of *text*, bounded. ``None`` when there is none."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return _present(line, limit)[0]
    return None


def _block_text(blocks, types: tuple = ("text",)) -> str:
    """Join the prose blocks of a content list.

    ``thinking`` blocks are dropped: they are long, they are the model's private
    reasoning, and the chat view is the readable summary — the terminal shows
    everything the session actually drew. *types* is what each harness calls a
    prose block (:data:`_CODEX_TEXT_TYPES`); everything else is dropped by the
    same allowlist.
    """
    parts = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") not in types:
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n\n".join(parts)


def _result_text(content) -> str:
    """Flatten a ``tool_result``'s content, which is a string or a block list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return _block_text(content)
    return ""


def _tool_detail(payload) -> Optional[str]:
    """A one-line hint at what a tool acted on, from its input."""
    if not isinstance(payload, dict):
        return None
    for key in _DETAIL_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _first_line(value)
    return None


def _strip_wrappers(text: str) -> str:
    """Remove the tags Claude Code wraps injected user content in."""
    return _WRAPPER_RE.sub("", text).strip()


def _decoded(text: str) -> str:
    """Undo the entity escaping the harness applied on the way in.

    See :data:`_MAX_ENTITY_PASSES` for why it is applied more than once and why
    the count is fixed.
    """
    for _ in range(_MAX_ENTITY_PASSES):
        decoded = _ENTITY_RE.sub(lambda match: _ENTITIES[match.group(0)], text)
        if decoded == text:
            return text
        text = decoded
    return text


def _injected_by_harness(record: dict) -> bool:
    """Whether a user-role record was written by the harness, not by a keyboard.

    One of the two anchors the monitor classification needs, and the one that has
    to be there: an operator who pastes an injected turn's markup into the chat —
    which is how this was reported — is quoting it, and their message stays theirs.
    """
    origin = record.get("origin")
    if isinstance(origin, dict) and origin.get("kind") == _TASK_NOTIFICATION_ORIGIN:
        return True
    return record.get("promptSource") == _INJECTED_PROMPT_SOURCE


def _monitor_report(text: str) -> Optional[str]:
    """A monitor injection's readable half, or ``None`` when *text* is not one.

    The condition the watch was armed on and the event that fired it, one per
    line and entity-decoded: the markup around them is a delivery mechanism, and
    a view that had to strip it would be a second reader of a format spec D6 says
    is not a contract. The trailing sentence such an injection can carry — a note
    to the model about whether to notify anyone — is dropped for the same reason
    a system reminder is: it is machinery addressing the session, not an event.
    """
    found = _MONITOR_RE.match(text)
    if found is None:
        return None
    parts = [_decoded(found.group("condition")).strip()]
    event = found.group("event")
    if event:
        parts.append(_decoded(event).strip())
    return "\n".join(part for part in parts if part)


def _api_error_of(record: dict) -> tuple:
    """``(refused, error class, http status)`` for one record.

    The harness marks its own refusal turns rather than leaving them to be
    recognised by wording, which is what lets halt detection avoid reading prose
    (:func:`lmer_platform.inventory._stalled`). Captured from Claude Code 2.1.221
    as ``tests/fixtures/transcripts/claude-api-error.jsonl``::

        "isApiErrorMessage": true, "error": "billing_error", "apiErrorStatus": 400

    ``isApiErrorMessage`` is the gate; the other two are detail a build may omit,
    so the flag is returned in its own right. Everything is read defensively —
    another program writes this file — and anything unexpected reads as absent.
    """
    return _api_error_fields(
        record.get("isApiErrorMessage"),
        record.get("error"),
        record.get("apiErrorStatus"),
    )


def _api_error_fields(flag, kind, status) -> tuple:
    """The validation :func:`_api_error_of` describes, apart from the spelling.

    Two vocabularies name these three facts differently — claude's
    ``isApiErrorMessage``/``error``/``apiErrorStatus`` and the canonical format's
    ``api_refusal``/``api_error``/``api_error_status`` — and both feed the same
    stall detection (:func:`lmer_platform.inventory._stalled`). One definition of
    what counts as a refusal, so a drop-in cannot reach that path through a looser
    door than the built-in harnesses do.

    ``True`` is required rather than truthiness: a refusal is a statement the
    writer makes, and a stray non-empty value is not it. The bool check on the
    status is there because :class:`bool` *is* an :class:`int` in Python, and
    ``"status": true`` would otherwise be served as HTTP 1.
    """
    if flag is not True:
        return False, None, None
    kind = kind.strip()[:64] if isinstance(kind, str) and kind.strip() else None
    if isinstance(status, bool) or not isinstance(status, int):
        status = None
    return True, kind, status


def _claude_message(record: dict, pending: dict) -> Optional[Message]:
    """Normalise one Claude Code record, or ``None`` when it carries nothing.

    *pending* maps ``tool_use`` id to the :class:`ToolCall` awaiting its result,
    and is both read and written here — that is how the single pass correlates a
    failure back onto the message that caused it.

    Sidechain records — a subagent's own conversation — are skipped. Interleaving
    them would put a second agent's "user" turns in the operator's chat, and the
    parent's ``Agent`` tool call is already there as one line saying it happened.
    """
    if record.get("isSidechain") is True:
        return None

    kind_of_record = record.get("type")
    timestamp = record.get("timestamp")
    timestamp = timestamp if isinstance(timestamp, str) else None

    if kind_of_record == "system":
        # Only the system records that carry prose. Hook summaries and turn
        # timings do not, and their substance reaches the model as an injected
        # user record anyway, so nothing is lost by skipping them.
        content = record.get("content")
        if not isinstance(content, str) or not content.strip():
            return None
        text, truncated = _present(
            _strip_wrappers(content), TEXT_LIMIT, keep="tail"
        )
        if not text:
            return None
        return Message(
            role="system", kind="notice", text=text, at=timestamp,
            truncated=truncated,
        )

    if kind_of_record == "attachment":
        # The one attachment that is a turn: a message the operator typed while
        # the session was mid-turn, which the harness queues and then delivers
        # here. Three records are written for such a message — the
        # ``queue-operation`` enqueue, its ``remove``, and this attachment — and
        # the attachment is the delivery: it sits where the model actually
        # received the text, exactly once per queued message, while the other two
        # are the queue's own bookkeeping carrying the same string twice over. A
        # message that instead waits for the turn boundary arrives as an ordinary
        # ``user`` record and gets no attachment at all (measured across a
        # session's 24 queued messages: zero overlap in either direction), so
        # taking the delivery here cannot double a turn.
        #
        # Emitting it is #275. The chat's pending bubble settles only on a
        # matching user turn — by design (#254), and #238 is the other way that
        # evidence goes missing — so a delivery the normaliser drops leaves a
        # bubble that never settles, pinned below everything said afterwards, one
        # per mid-turn message for the life of the view.
        attachment = record.get("attachment")
        if not isinstance(attachment, dict):
            return None
        if attachment.get("type") != _QUEUED_COMMAND_ATTACHMENT:
            return None
        # The queue is not the operator's alone: the harness pushes its own task
        # notifications through the same mechanism, and those carry kilobytes of
        # task ids and tool-use ids that :func:`_strip_wrappers` only takes the
        # outer tag off (verified on a live transcript: of four queued
        # deliveries, two were typed and carried ``origin {"kind": "human"}``
        # with ``commandMode: "prompt"``; two were machinery, with ``origin``
        # null and ``commandMode: "task-notification"``).
        #
        # So the producer's own positive assertion that a keyboard wrote it is
        # required, rather than the machinery kinds being blocklisted — a future
        # release adding a fifth internal ``commandMode`` would walk straight
        # through a blocklist and be drawn as something the operator said, which
        # is the failure :data:`MONITOR_ROLE` exists to prevent one level up.
        # Requiring the marker fails the other way: an odd build that stops
        # emitting it costs a bubble that does not settle, which is exactly
        # today's behavior and nothing worse.
        origin = attachment.get("origin")
        if not isinstance(origin, dict) or origin.get("kind") != _HUMAN_ORIGIN:
            return None
        prompt = attachment.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return None
        # The operator's own words, so through exactly what a typed turn's text
        # goes through below: wrappers off, then the scrub-and-cap chokepoint.
        text, truncated = _present(
            _strip_wrappers(prompt), TEXT_LIMIT, keep="tail"
        )
        if not text:
            return None
        return Message(
            role="user", kind="said", text=text, at=timestamp,
            truncated=truncated,
        )

    if kind_of_record not in ("user", "assistant"):
        return None

    message = record.get("message")
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    if role not in ("user", "assistant"):
        return None

    content = message.get("content")
    tools: list = []
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = _block_text(content)
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "tool_use":
                call = ToolCall(
                    name=str(block.get("name") or "tool"),
                    detail=_tool_detail(block.get("input")),
                )
                tools.append(call)
                use_id = block.get("id")
                if isinstance(use_id, str) and use_id:
                    pending[use_id] = call
            elif block_type == "tool_result":
                # The outcome lands on the *calling* message, so a user record
                # that is only a tool result normalises to nothing and is
                # dropped: it is the harness feeding the model, not a turn. A
                # result whose call is not in this file has no name and no
                # message to attach to, and is dropped for the same reason.
                use_id = block.get("tool_use_id")
                call = pending.pop(use_id, None) if isinstance(use_id, str) else None
                if call is None:
                    continue
                if block.get("is_error"):
                    call.status = "failed"
                    call.error = _first_line(
                        _strip_wrappers(_result_text(block.get("content")))
                    )
                else:
                    call.status = "ok"
    else:
        return None

    via = None
    if role == "user":
        text = _strip_wrappers(text if isinstance(text, str) else "")
        injected = _injected_by_harness(record)
        watched = _monitor_report(text) if injected else None
        if watched is None:
            # Injected is decided before isMeta and both before "said": the
            # harness's other injections — a background task finishing, an agent
            # stopping — carry no isMeta either (#242), so reading isMeta alone
            # drew every one of them as a turn the operator had typed.
            kind = "injected" if injected or record.get("isMeta") else "said"
        else:
            # A watch firing, which the harness delivers as a turn in the
            # operator's own role and with no isMeta to hide it behind the
            # internals toggle. Re-attributed rather than hidden: it explains the
            # turn that follows it, and the module docstring has the rest.
            role, kind, via, text = MONITOR_ROLE, "said", MONITOR_VIA, watched
    else:
        text = (text if isinstance(text, str) else "").strip()
        kind = "said"

    text, truncated = _present(text, TEXT_LIMIT, keep="tail")
    api_refusal, api_error, api_error_status = _api_error_of(record)
    result = Message(
        role=role, kind=kind, text=text, at=timestamp, truncated=truncated,
        tools=tools, via=via, api_refusal=api_refusal,
        api_error=api_error, api_error_status=api_error_status,
    )
    return None if result.empty else result


def _pi_message(record: dict, pending: dict) -> Optional[Message]:
    """Normalise one pi record (pi 0.84.1, session format 3).

    Only ``message`` records carry a conversation. The rest of pi's vocabulary is
    session state — the header, a model change, a thinking-level change, whatever
    an extension appends — and is skipped for the reason claude's bookkeeping
    rows are: none of it is anybody talking.

    **The file is read in order, and a pi session is a tree.** Records carry
    ``id`` and ``parentId``, and ``/tree`` and ``/fork`` branch one, so a forked
    session renders every branch in the order it was written rather than the path
    the operator ended up on. Platform-spawned runs are linear, which is what
    makes reading the file as written good enough here and not in general.
    """
    if record.get("type") != "message":
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None

    timestamp = record.get("timestamp")
    timestamp = timestamp if isinstance(timestamp, str) else None
    role = message.get("role")

    if role == "toolResult":
        # The outcome lands on the *calling* message, exactly as claude's
        # tool_result does, so the record itself is not a turn. A result whose
        # call is not in this file has nothing to attach to and is dropped.
        call_id = message.get("toolCallId")
        call = pending.pop(call_id, None) if isinstance(call_id, str) else None
        if call is not None:
            if message.get("isError"):
                call.status = "failed"
                call.error = _first_line(_result_text(message.get("content")))
            else:
                call.status = "ok"
        return None

    if role not in ("user", "assistant"):
        return None

    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = _block_text(content)
    else:
        return None

    tools: list = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "toolCall":
                continue
            call = ToolCall(
                name=str(block.get("name") or "tool"),
                # pi's arguments are already a mapping — ``command`` for its
                # bash tool, ``path`` for the file ones — so the same generic
                # pick reads them.
                detail=_tool_detail(block.get("arguments")),
            )
            tools.append(call)
            call_id = block.get("id")
            if isinstance(call_id, str) and call_id:
                pending[call_id] = call

    # pi 0.84.1 gives extension-injected content its own role ("custom"), not
    # the operator's, so this strip only bites when a claude-style wrapper tag
    # turns up in pi user text — quoted, say. Cheap, and the tags are not words
    # anyone said.
    text = _strip_wrappers(text) if role == "user" else text.strip()

    text, truncated = _present(text, TEXT_LIMIT, keep="tail")
    result = Message(
        role=role, kind="said", text=text, at=timestamp, truncated=truncated,
        tools=tools,
    )
    return None if result.empty else result


def _codex_arguments(raw) -> Optional[dict]:
    """A codex function call's arguments, which arrive as a JSON *string*.

    Parsed defensively, and only ever for the one-line hint: an argument blob
    that is not JSON, or is JSON but not a mapping, or was truncated by the model
    reads as "no detail" rather than as a failure — a tool chip with a name and
    no hint still says what ran.
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _codex_unwrapped(text: str) -> str:
    """*text* minus complete ``<tag>…</tag>`` wrapper blocks, content included.

    An *unclosed* wrapper takes the rest of *text* with it, which is the same
    direction :func:`_codex_injected` argues for a torn write: what follows an
    opening tag is the harness's material until something says otherwise, and
    the alternative is showing it as an operator's words.

    A forward scan rather than one wrapped-block pattern, because this runs on
    agent-authored input inside the daemon's request thread — the transcript is
    written by the observed container through a read-write mount, and a
    ``<tag><tag><tag>…`` line with no closer made the backtracking
    ``<tag>.*?</tag>`` this replaced quadratic: 0.2s at a thousand repeats, 3.4s
    at four thousand, on lines the reader admits at up to a megabyte. Here each
    opener is found once (:data:`_CODEX_OPEN_RE`, fixed width) and its closer
    once, both cursors only ever move forward, and the closing tag is matched
    literally — so the work is linear in ``len(text)`` whatever an agent writes.
    """
    kept: list = []
    # Start of the text not yet copied out, and where to look for the next
    # opener. Neither ever moves backwards.
    cursor = probe = 0
    while True:
        found = _CODEX_OPEN_RE.search(text, probe)
        if found is None:
            break
        kept.append(text[cursor:found.start()])
        # The closing form takes no attributes, which is what the pattern this
        # replaced required too: ``</environment_context foo>`` closes nothing.
        end = text.find("</%s>" % found.group("tag"), found.end())
        if end < 0:
            return "".join(kept)
        cursor = probe = end + len(found.group("tag")) + 3
    kept.append(text[cursor:])
    return "".join(kept)


def _codex_injected(text: str) -> bool:
    """Whether a user-role codex record is context the harness injected.

    Codex writes its own material in the *operator's* role: the environment
    block, the skills list, a repo's instructions, the turn context. Nothing in
    such a record's role tells it apart from a typed prompt — the same shape as
    the watch injection above, and the same answer: the classification is made
    here, from what the record is made of. A record that is nothing but wrapped
    material is ``injected``; a prompt that merely mentions one of those tags is
    the operator's own words and stays ``said``.

    A wrapper left open by a torn write counts as injected as well. That is the
    one direction which must not be guessed generously: a truncated
    ``<environment_context>`` falling through to ``said`` would put the harness's
    words in an operator's bubble, while an operator whose prompt happens to open
    with the tag loses nothing but a caption.

    "Nothing but wrapped material" is answered by :func:`_codex_unwrapped`, whose
    linear scan is what keeps this question cheap: it is asked of every user
    record in the daemon's request thread, on text an agent wrote.
    """
    if not text.strip():
        return False
    if _CODEX_OPENS_RE.match(text):
        return True
    return not _codex_unwrapped(text).strip()


def _codex_message(record: dict, pending: dict) -> Optional[Message]:
    """Normalise one codex record (codex-cli 0.147.0 rollout format).

    ``response_item`` is the only record type read. Everything else in a rollout
    is either session state (``session_meta``, ``turn_context``, ``world_state``)
    or a *second copy* of the conversation: an ``event_msg`` carries
    ``user_message`` and ``agent_message`` payloads repeating what a
    ``response_item`` already said, so reading both would render every turn twice.

    Its payloads are read at two levels of precision. ``message``,
    ``function_call`` and ``function_call_output`` are read exactly, for prose,
    the tool chip and its outcome. The rest of codex's response-item family —
    ``custom_tool_call``, ``tool_search_call``, ``web_search_call``,
    ``image_generation_call``, ``local_shell_call`` and whatever the next release
    adds — degrades to a chip named after the call, paired with its ``_output``
    twin by ``call_id``. A rule rather than an adapter per variant, because
    without one every variant nobody has written an adapter for yet renders as
    *nothing*: an assistant turn made only of a freeform ``apply_patch`` was a
    blank page, and the consumer of this view reads a blank page as a run that
    said nothing. A named chip with no detail is a small miss; a silent one is a
    wrong answer to "is this run progressing".

    ``reasoning`` is the one kind dropped on purpose: it is the model's private
    reasoning, the same call :func:`_block_text` makes for claude's thinking
    blocks.

    A ``developer`` record is the harness's own instructions to the model, so it
    is served as ``system``/``injected`` rather than as anybody's turn:
    :data:`MESSAGE_KINDS` gives ``injected`` to machinery addressed to the model
    and ``notice`` to what the harness says to the *operator*, and a skills list
    is the former. What it must not be is a ``user``/``said`` turn, which is what
    its role would have made it. A ``message`` in any other role is dropped —
    the safe direction, since nothing here could say whose words they are.
    """
    if record.get("type") != "response_item":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None

    timestamp = record.get("timestamp")
    timestamp = timestamp if isinstance(timestamp, str) else None
    kind_of_payload = payload.get("type")

    if kind_of_payload == "function_call":
        call = ToolCall(
            name=str(payload.get("name") or "tool"),
            detail=_tool_detail(_codex_arguments(payload.get("arguments"))),
        )
        call_id = payload.get("call_id")
        if isinstance(call_id, str) and call_id:
            pending[call_id] = call
        return Message(
            role="assistant", kind="said", text="", at=timestamp, tools=[call],
        )

    if kind_of_payload == "function_call_output":
        # No error flag was observed in the captures: codex reports a failure
        # inside the output text ("Process exited with code 1"), so a call that
        # came back reads as ok. A build that grows a flag is a fixture update.
        call_id = payload.get("call_id")
        call = pending.pop(call_id, None) if isinstance(call_id, str) else None
        if call is not None:
            call.status = "ok"
        return None

    if isinstance(kind_of_payload, str) and kind_of_payload.endswith("_output"):
        # A variant this reader has no adapter for, correlated exactly as
        # ``function_call_output`` is (same no-error-flag caveat) and rendering
        # nothing of its own: the chip its call already drew is the record.
        call_id = payload.get("call_id")
        call = pending.pop(call_id, None) if isinstance(call_id, str) else None
        if call is not None:
            call.status = "ok"
        return None

    if isinstance(kind_of_payload, str) and kind_of_payload.endswith("_call"):
        # The generic degrade this function's docstring argues for. The name is
        # the payload's when it carries one and the variant's own type when it
        # does not — "local_shell_call" says more than a blank chip. Arguments
        # are read where a variant happens to spell them the way the exact
        # adapters do; absent is fine, the chip stands on its name.
        name = payload.get("name")
        arguments = payload.get("arguments")
        if arguments is None:
            arguments = payload.get("input")
        call = ToolCall(
            name=name if isinstance(name, str) and name else kind_of_payload,
            detail=_tool_detail(_codex_arguments(arguments)),
        )
        call_id = payload.get("call_id")
        if isinstance(call_id, str) and call_id:
            pending[call_id] = call
        return Message(
            role="assistant", kind="said", text="", at=timestamp, tools=[call],
        )

    if kind_of_payload != "message":
        # ``reasoning`` lands here, deliberately: private model reasoning, the
        # same call :func:`_block_text` makes for claude's thinking blocks.
        return None

    content = payload.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = _block_text(content, types=_CODEX_TEXT_TYPES)
    else:
        return None

    role = payload.get("role")
    if role == "assistant":
        kind = "said"
        text = text.strip()
    elif role == "user":
        if _codex_injected(text):
            # Wholesale machinery: the tags go, what they wrapped stays. It is
            # marked injected, and the view's internals toggle is there for it.
            kind = "injected"
            text = _strip_wrappers(_CODEX_TAG_RE.sub("", text))
        else:
            # A typed prompt with a wrapped block *in* it — "fix the login bug
            # <user_instructions>…</user_instructions>". The turn is the
            # operator's, so it stays said, but taking only the tags off would
            # leave the harness's words inside their bubble as theirs. The block
            # goes with its content; the named cost is an operator who quotes a
            # complete wrapper losing the quote (and, past an unclosed tag, the
            # rest of the line) out of their own message. That is the direction
            # :func:`_codex_unwrapped` argues for, and the cheaper mistake.
            kind = "said"
            text = _strip_wrappers(_CODEX_TAG_RE.sub("", _codex_unwrapped(text)))
    elif role == "developer":
        role, kind = "system", "injected"
        text = _strip_wrappers(_CODEX_TAG_RE.sub("", text))
    else:
        return None

    text, truncated = _present(text, TEXT_LIMIT, keep="tail")
    result = Message(
        role=role, kind=kind, text=text, at=timestamp, truncated=truncated,
    )
    return None if result.empty else result


def _lmer_declared_harness(record: dict) -> Optional[str]:
    """The harness name an ``lmer.meta`` record declares, when it is usable.

    The file's own answer to "which harness wrote this", which is the only answer
    there is for a drop-in: its converter chose the format, so nothing about the
    records themselves names it, and the directory the file was found in is a
    mount destination rather than evidence. ``None`` for every other record and
    for a name that fails :data:`_HARNESS_NAME_RE` — the label reaches an HTTP
    response, and this file is written by the container being observed.
    """
    if record.get("type") != "lmer.meta":
        return None
    name = record.get("harness")
    if isinstance(name, str) and _HARNESS_NAME_RE.fullmatch(name):
        return name
    return None


def _lmer_declared_format(record: dict) -> Optional[int]:
    """The canonical-format version an ``lmer.meta`` record declares.

    ``None`` for every other record and for a ``format`` that is not an integer —
    absent, a string, a float, ``true`` — which reads as version 1, the same
    direction every other unusable field here takes: a writer that cannot state a
    version is a writer with nothing to gate on. ``True`` is an ``int`` in Python
    and is refused for that reason, as it is for ``api_error_status``.
    """
    if record.get("type") != "lmer.meta":
        return None
    declared = record.get("format")
    if isinstance(declared, bool) or not isinstance(declared, int):
        return None
    return declared


def _lmer_rejected(field: str, value=None) -> None:
    """Say, at debug, which field cost a canonical record. Always ``None``.

    The one vocabulary here whose writer can act on a diagnostic: the other three
    are vendors' formats, where a skipped record is this module's problem to fix,
    while a canonical file is written by a drop-in's own converter against a
    published contract — so "which field did I get wrong" is the answer its author
    needs, and the silence the other adapters keep would send them to read this
    source instead. Debug rather than warning because a busy converter's bug would
    otherwise fill the daemon log at whatever rate it writes records.
    """
    # ``%.80s`` rather than a repr: the formatting is lazy (nothing is rendered
    # unless a handler is at debug), and a claim in a container-written file is
    # bounded only by the line limit.
    logger.debug(
        "platform_transcript_lmer_record_rejected field=%s value=%.80s",
        field, value,
    )
    return None


def _lmer_tool_call(entry, pending: dict) -> Optional[ToolCall]:
    """One entry of a canonical record's ``tools`` list, or ``None`` to drop it.

    ``name`` is the whole of what a chip needs to say something, so an entry
    without one is dropped rather than named "tool": the converter had a tool
    call in front of it and could not say what it was, and an anonymous chip
    invites the reader to guess. An unusable ``status`` drops the entry for the
    same reason and not the turn around it — defaulting it to ``pending`` would
    draw a tool that finished as still running, and skipping the whole record
    would lose prose over a chip.
    """
    if not isinstance(entry, dict):
        return _lmer_rejected("tools[]", entry)
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        return _lmer_rejected("tools[].name", name)

    status = entry.get("status", "pending")
    if status is None:
        # An explicit JSON null on an optional field is how most serialisers say
        # "unset" — a dataclass with ``status: str | None = None``, a struct
        # marshalled with the zero value in place. The document calls this field
        # optional-with-a-default, so the default is what null means here; only a
        # value *outside* the set is a claim this reader cannot honour.
        status = "pending"
    if status not in TOOL_STATUSES:
        return _lmer_rejected("tools[].status", status)

    detail = entry.get("detail")
    error = entry.get("error")
    call = ToolCall(
        name=_present(name.strip(), DETAIL_LIMIT)[0],
        # Already a one-line hint by contract, so this bounds and scrubs a claim
        # rather than deriving one: :func:`_first_line` is exactly the chokepoint
        # the other adapters put their derived hints through.
        detail=_first_line(detail) if isinstance(detail, str) else None,
        status=status,
        error=_first_line(error) if isinstance(error, str) else None,
    )
    call_id = entry.get("id")
    if isinstance(call_id, str) and call_id:
        # Only a call that named itself can be resolved later; one that did not
        # is a converter saying the outcome is already inline.
        pending[call_id] = call
    return call


def _lmer_message(record: dict, pending: dict) -> Optional[Message]:
    """Normalise one record of the canonical format — version 1 (#296).

    The one vocabulary here that is *not* a harness's. It serialises the shape
    this module normalises **to**, so there is nothing to interpret: no wrapper
    tags to strip, no role to re-attribute, no injected-context heuristic. What
    is left is field validation, the :func:`_present` chokepoint and the same
    mutate-in-place tool correlation the other three do — which is the point of
    the format rather than a happy accident, since it is what lets any drop-in
    harness reach this view with no code on the host at all.

    What that costs, and how it is paid: this file is written *by the container*,
    by drop-in code, and every field in it is a claim. So the claims that could
    misattribute a turn are allowlisted rather than echoed — ``role`` against
    :data:`_LMER_ROLES`, ``kind`` against :data:`MESSAGE_KINDS`, ``status``
    against :data:`TOOL_STATUSES` — and one is refused outright:
    :data:`ASK_CHANNEL_VIA` is the platform's statement that a turn came from the
    operator's own channel and was merged in here, so a file cannot claim it. A
    record that fails these rules is skipped — never fatally, exactly as an
    unreadable line is — and says at debug which field cost it
    (:func:`_lmer_rejected`), because this writer is a third party who can fix it.

    Two fields the document calls optional-with-a-default read an explicit JSON
    ``null`` as that default (``kind``, ``tools[].status``): most serialisers emit
    null for an unset optional, and refusing it would drop turns over a habit
    rather than over a claim. Every other field keeps the stricter reading.

    ``lmer.meta`` and ``lmer.tool_update`` are not turns and emit nothing: the
    first labels the file and declares its format version (handled in
    :func:`_normalise`, which is where per-file state lives), the second folds an
    outcome onto a call already emitted. Any other ``lmer.*`` type is a record
    from a later version of the format and is skipped in silence — see
    :data:`_LMER_TYPE_PREFIX` for why that is a claim rather than a fall-through.
    """
    kind_of_record = record.get("type")
    if kind_of_record not in _LMER_RECORD_TYPES:
        # A type from a version of the format this build predates. The dispatch
        # already claimed it for this vocabulary, so it costs itself and nothing
        # else — which is the whole difference between a file with one new record
        # in it and a file this build cannot read.
        return None

    if kind_of_record == "lmer.tool_update":
        # Append-only correlation: the message carrying this call was written
        # while the tool was still running and is never rewritten, so the outcome
        # arrives as its own line. An id from outside this file — or outside the
        # tail ``last_turn`` reads — has nothing to fold onto and no-ops, the same
        # answer claude's orphaned ``tool_result`` gets.
        status = record.get("status")
        if status not in ("ok", "failed"):
            # The format's own set for an update: "pending" is the state a call
            # is already in, so an update saying it is not an outcome.
            return _lmer_rejected("status", status)
        call_id = record.get("id")
        call = pending.pop(call_id, None) if isinstance(call_id, str) else None
        if call is None:
            return None
        call.status = status
        error = record.get("error")
        if isinstance(error, str):
            call.error = _first_line(error)
        return None

    if kind_of_record != "lmer.message":
        return None

    role = record.get("role")
    if role not in _LMER_ROLES:
        return _lmer_rejected("role", role)

    kind = record.get("kind", "said")
    if kind is None:
        # Same reading as ``tools[].status`` above: for the two fields the
        # document calls optional-with-a-default, an explicit null is the default
        # rather than a bad value. Skipping the turn instead would lose prose over
        # a serialiser's habit, and lose it silently.
        kind = "said"
    if kind not in MESSAGE_KINDS:
        return _lmer_rejected("kind", kind)

    text = record.get("text")
    if not isinstance(text, str):
        return _lmer_rejected("text", text)

    # ``at`` reaches the view and the ask-channel merge as a string, and this is
    # the only adapter whose timestamps come from an untrusted writer — the other
    # three copy a vendor's own stamp. Keeping only what the merge's own parser
    # accepts (:func:`_timestamp_key`) bounds it to a timestamp's shape rather
    # than to a length, so a file cannot put arbitrary text beside a speaker's
    # name. Unparseable reads as no time at all, which is already the answer for
    # a turn that carried none.
    at = record.get("at")
    at = at if isinstance(at, str) and _timestamp_key(at) is not None else None

    tools: list = []
    raw_tools = record.get("tools")
    if isinstance(raw_tools, list):
        for entry in raw_tools:
            call = _lmer_tool_call(entry, pending)
            if call is not None:
                tools.append(call)

    # Only the monitor is claimable. ``via`` says how words that nobody in the
    # conversation typed got here, and the ask channel's half of that statement
    # is made by the merge below on evidence the platform holds — a file that
    # could claim it would be a container asserting the operator had spoken.
    via = MONITOR_VIA if record.get("via") == MONITOR_VIA else None

    text, truncated = _present(text, TEXT_LIMIT, keep="tail")
    api_refusal, api_error, api_error_status = _api_error_fields(
        record.get("api_refusal"),
        record.get("api_error"),
        record.get("api_error_status"),
    )
    result = Message(
        role=role, kind=kind, text=text, at=at, truncated=truncated, tools=tools,
        via=via, api_refusal=api_refusal, api_error=api_error,
        api_error_status=api_error_status,
    )
    return None if result.empty else result


def _harness_of_record(record: dict) -> Optional[str]:
    """Which harness's format one record is in, or ``None`` for a shape nobody
    here recognises — a fourth harness, or a file that is not a transcript."""
    kind = record.get("type")
    if kind in _CLAUDE_RECORD_TYPES:
        return "claude"
    if kind in _PI_RECORD_TYPES:
        return "pi"
    if kind in _CODEX_RECORD_TYPES:
        return "codex"
    if isinstance(kind, str) and kind.startswith(_LMER_TYPE_PREFIX):
        # The canonical format, claimed by its namespace rather than by the three
        # known types: a record from a later version is this format's and skipped
        # by its adapter, not a format this build cannot read.
        return _LMER_HARNESS
    return None


def _message_from_record(record: dict, pending: dict) -> Optional[Message]:
    """Normalise one transcript record, whichever harness wrote it.

    Per record rather than per file — see the module docstring — and an
    unrecognised record is skipped in silence, which is what makes an unknown
    format an empty conversation rather than an error.
    """
    harness = _harness_of_record(record)
    if harness == "claude":
        return _claude_message(record, pending)
    if harness == "pi":
        return _pi_message(record, pending)
    if harness == "codex":
        return _codex_message(record, pending)
    if harness == _LMER_HARNESS:
        return _lmer_message(record, pending)
    return None


def _iter_records(path: Path) -> Iterator[dict]:
    """Yield the JSON objects in a JSONL file, skipping what cannot be read.

    Line by line rather than whole-file: a long session's transcript runs to
    megabytes, and the normalised form is a fraction of it. A torn final line
    from a crash mid-append is skipped, matching ``store.read_events``.
    """
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning(
            "platform_transcript_unreadable path=%s error=%s", path, exc
        )
        return
    oversized = False
    with handle:
        while True:
            try:
                raw = handle.readline(_MAX_LINE_BYTES)
            except OSError as exc:
                logger.warning(
                    "platform_transcript_read_failed path=%s error=%s", path, exc
                )
                return
            if not raw:
                return
            if len(raw) >= _MAX_LINE_BYTES and not raw.endswith("\n"):
                # The bound is why the read is chunked at all: a binary file
                # misnamed ``.jsonl``, or a torn write, can present as one
                # enormous line, and reading it whole would put it in the daemon's
                # heap. Chunking makes it unparseable fragments instead — said out
                # loud once per file, because otherwise the only symptom of a file
                # like this is a chat view that is quietly missing part of itself.
                if not oversized:
                    oversized = True
                    logger.warning(
                        "platform_transcript_line_oversized path=%s limit=%d",
                        path, _MAX_LINE_BYTES,
                    )
                continue
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record


def _tail_records(path: Path, *, tail_bytes: int) -> list:
    """The records in the last *tail_bytes* of a JSONL file, oldest first.

    Seeks, so the cost does not grow with the session. A seeked read drops its
    first line, which is almost certainly half a record. Everything unreadable is
    an empty list: the caller is deciding whether to raise attention, and half a
    file is worse evidence than none.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            start = max(0, size - tail_bytes)
            if start:
                handle.seek(start)
            blob = handle.read()
    except OSError as exc:
        logger.warning(
            "platform_transcript_tail_unreadable path=%s error=%s", path, exc
        )
        return []

    lines = blob.decode("utf-8", errors="replace").splitlines()
    if start and lines:
        lines = lines[1:]

    records = []
    for line in lines:
        line = line.strip()
        if not line or len(line) >= _MAX_LINE_BYTES:
            # The same bound ``_iter_records`` applies, for the same reason: one
            # pathological line must not be parsed into the daemon's heap.
            continue
        try:
            record = json.loads(line)
        except ValueError:
            # Ordinary here rather than exceptional: the harness appends while
            # this reads, so the last line can be half-written.
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _head_record(path: Path, *, tail_bytes: int) -> Optional[dict]:
    """The file's first record, when a tail read of *tail_bytes* would miss it.

    One line, read from the top, for one purpose: a file's *header* is the only
    thing in it that describes the whole file, and the canonical format's header
    is the record that says which version the rest is in
    (:func:`_lmer_declared_format`). A seeked read never sees it, so halt
    detection would read a file this build has refused to serve to the chat view
    — and read a redefined ``api_refusal`` out of it as a stall verdict, which is
    the consumer the gate exists for.

    Cheap enough for the poll path this is on: one open and one bounded
    ``readline`` against a file already being stat'ed and seeked. ``None`` when
    the tail did not skip anything (the head is already in it, and prepending it
    again would double a turn), and ``None`` for every way a first line can fail —
    unreadable, oversized, not JSON, not an object — because a header that cannot
    be read is exactly the "no opinion" this whole function is allowed to answer
    with.
    """
    try:
        # The condition ``_tail_records``' seek makes: ``start`` there is
        # ``max(0, size - tail_bytes)``, which is non-zero on just these files.
        if path.stat().st_size <= tail_bytes:
            return None
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            line = handle.readline(_MAX_LINE_BYTES)
    except OSError:
        return None
    line = line.strip()
    if not line or len(line) >= _MAX_LINE_BYTES:
        return None
    try:
        record = json.loads(line)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def last_turn(session_id: str) -> Optional["Message"]:
    """The newest turn in *session_id*'s transcript, or ``None``.

    What halt detection asks (#243): *who spoke last*. Not
    :func:`read_messages`, which reads every transcript of the whole run for a
    paging client — this runs on the fleet-view poll path and reads one file's
    tail (:data:`LAST_TURN_TAIL_BYTES`), newest by mtime when there are several.

    ``None`` for every ordinary way of not knowing — no transcript at all,
    unreadable, nothing that normalises — and callers must read it as "no
    opinion", never as "nothing was said". The per-record dispatch means a pi or
    codex tail answers here too, which it did not while this was Claude-shaped.

    The tail is prefixed with the file's first record when the seek skipped it
    (:func:`_head_record`), which is what keeps a *per-file* statement — the
    canonical format's version, the drop-in's name — readable from a read that
    starts in the middle. Without it this path would answer from a file whose
    records the chat view refuses to show, and answer with the one field
    (``api_refusal``) a later format version is most likely to have redefined.
    """
    sources = locate_sources(session_id)
    if not sources:
        return None
    try:
        newest = max(sources, key=lambda source: source.path.stat().st_mtime)
    except OSError as exc:
        logger.warning(
            "platform_transcript_stat_failed session=%s error=%s", session_id, exc
        )
        return None

    records = _tail_records(newest.path, tail_bytes=LAST_TURN_TAIL_BYTES)
    if not records:
        return None
    header = _head_record(newest.path, tail_bytes=LAST_TURN_TAIL_BYTES)
    if (
        header is not None
        and header.get("type") == "lmer.meta"
        and records[0] != header
    ):
        # Only the canonical header may join the tail. It is the per-file
        # statement this prefix exists to carry, and it can never be a turn —
        # any other first record can (three claude fixtures open with one), and
        # prepending it would let a tail window with no messages answer with the
        # session's *oldest* turn: a stale verdict where callers were promised
        # "no opinion". The equality check, not the size check alone: the
        # harness appends while this reads, so a file can cross the tail bound
        # *between* the two reads and hand back a head the tail already holds.
        # Doubling a record is the one thing a prefix must never do.
        records = [header] + records
    messages, _, _ = _normalise(records, origin=newest.path)
    return messages[-1] if messages else None


#: Transcript files already reported as declaring a canonical format this build
#: does not speak. The condition is a property of a *file*, not of a read, and
#: both readers of one are polls — the chat view every few seconds, the fleet view
#: every cycle — so keying the warning on the read would restate the same fact
#: about the same file forever, which is what makes a log unreadable. One entry
#: per gated file for the daemon's life is the bound; a host with many of those
#: has a bigger problem than this set. Unlocked: two threads reading the same
#: gated file at once can say it twice, which is the whole cost of not holding a
#: lock on a log line.
_LMER_FORMAT_WARNED: set = set()


def _lmer_format_unsupported(declared: int, origin: Optional[Path]) -> None:
    """Say once per file that its canonical format is ahead of this reader.

    Debug rather than warning when there is no file to key on
    (:func:`normalise_records`, and any caller holding records in memory): the
    fact is worth having, and repeating it is the caller's choice rather than a
    poll's, but nothing here can promise to say it only once.
    """
    if origin is None:
        logger.debug(
            "platform_transcript_lmer_format_unsupported format=%d — this reader "
            "speaks format %d; records skipped", declared, _LMER_FORMAT,
        )
        return
    key = str(origin)
    if key in _LMER_FORMAT_WARNED:
        return
    _LMER_FORMAT_WARNED.add(key)
    logger.warning(
        "platform_transcript_lmer_format_unsupported path=%s format=%d — this "
        "reader speaks format %d; records skipped", origin, declared, _LMER_FORMAT,
    )


def _normalise(
    records: Iterable[dict],
    *,
    cap: Optional[int] = None,
    origin: Optional[Path] = None,
) -> tuple:
    """Turn records into messages, one pass. ``(messages, capped, harness)``.

    Correlates each tool call with its later result — a tool still without one
    reads as ``pending``, which is what the session is doing right now. An
    unforeseen record shape is a format change (spec D6: the format is not a
    contract), so it costs that one record and not the conversation.

    ``harness`` is the first format recognised, or ``None`` when nothing was: the
    file's own answer to which harness wrote it, for a caller that would otherwise
    have to trust a label.

    The canonical format is the one that can answer that question *by name*. Its
    records say only "this is the lmer format" — a drop-in's converter wrote them,
    and the drop-in is the harness — so an ``lmer.meta`` record's declared name
    replaces that label wherever it appears in the file (:data:`_LMER_HARNESS` is
    what a file with no usable meta is left as). It can only ever replace the
    canonical label: a meta record turning up in a claude file must not relabel
    it, and the first declaration wins as the first *format* does.

    It is also where the canonical format's **version gate** lives, for the same
    reason: an ``lmer.meta`` declaring a ``format`` this build does not speak
    (:data:`_LMER_FORMAT`) stops the canonical records of *that file* from
    emitting anything, from that record on. Reading them anyway is the failure the
    gate exists for — a later version may change what a field it already has
    means, and a misread ``api_refusal`` states a provider outage that did not
    happen. An empty page says so honestly (:data:`EMPTY_TRANSCRIPT_NOTE`), and
    the file is still *labelled* by such a meta's ``harness``: naming the drop-in
    that wrote a file this build cannot read costs nothing and helps whoever has
    to work out why the page is empty.

    ``origin`` is the file these records came out of, when they came out of one.
    It is what the gate's warning is keyed on — the condition belongs to a file
    and both readers of one are polls, so a warning keyed on the *read* would
    restate the same fact every few seconds for as long as the daemon runs (see
    :data:`_LMER_FORMAT_WARNED`). Records held in memory have no key, and say it
    at debug instead.

    Best-effort in one narrow direction: a tail read only carries the header
    because :func:`last_turn` prefixes it (:func:`_head_record`), and that reads
    the file's *first line*. A canonical file whose ``lmer.meta`` is not its first
    record — which the format recommends against, and no converter in this tree
    writes — is therefore still ungated on that path. Nothing else about a read
    changes the gate.
    """
    pending: dict = {}
    messages: list = []
    harness: Optional[str] = None
    unsupported = False
    for record in records:
        if cap is not None and len(messages) >= cap:
            return messages, True, harness
        try:
            vocabulary = _harness_of_record(record)
            if harness is None:
                harness = vocabulary
            if vocabulary == _LMER_HARNESS:
                if harness == _LMER_HARNESS:
                    harness = _lmer_declared_harness(record) or harness
                declared = _lmer_declared_format(record)
                if declared is not None and declared > _LMER_FORMAT:
                    if not unsupported:
                        unsupported = True
                        _lmer_format_unsupported(declared, origin)
                if unsupported:
                    # Only this vocabulary's records: the gate is the canonical
                    # file's own statement about itself, and says nothing about a
                    # harness's native file read in the same run.
                    continue
            message = _message_from_record(record, pending)
        except Exception as exc:
            logger.warning("platform_transcript_record_skipped error=%r", exc)
            continue
        if message is not None:
            messages.append(message)
    return messages, False, harness


def normalise_records(records: Iterable[dict]) -> list:
    """The adapter alone: transcript records in, normalised messages out."""
    return _normalise(records)[0]


def read_source(source: Source) -> tuple:
    """Read one transcript file. Returns ``(messages, source)``.

    The returned :class:`Source` carries the count and whether the per-file
    ceiling was hit, so the caller can say so rather than silently showing a
    prefix of a conversation.

    It also carries the harness the *records* turned out to be from, which beats
    the label the source arrived with: a pointer's ``harness`` is hand-editable
    (:mod:`lmer_platform.registry`) and the derived directory has no label at all,
    while the file says what it is. Nothing recognised keeps what was passed in —
    there is no evidence to correct it with.
    """
    messages, capped, harness = _normalise(
        _iter_records(source.path), cap=MAX_MESSAGES_PER_SOURCE,
        origin=source.path,
    )
    if capped:
        logger.warning(
            "platform_transcript_capped path=%s limit=%d",
            source.path, MAX_MESSAGES_PER_SOURCE,
        )
    for message in messages:
        message.origin = source.id
    return messages, Source(
        path=source.path,
        session=source.session,
        harness=harness or source.harness,
        messages=len(messages),
        capped=capped,
    )


# --- merging in the turns only the ask channel holds -------------------------
#
# See the module docstring for why this happens on the read path and nowhere
# else, what is merged, and how the two clocks are reconciled.


def _timestamp_key(value: Optional[str]) -> Optional[float]:
    """One timestamp as a number, or ``None`` when it will not parse.

    The two sides of the merge write time differently — the harness stamps
    milliseconds (``2026-07-20T09:00:01.000Z``), the channel stamps whole seconds
    (:func:`ask_channel.protocol.utc_now_iso`) — so they cannot be compared as
    strings: ``"…01.000Z"`` sorts *before* ``"…01Z"`` for no reason but the ASCII
    order of ``.`` and ``Z``, and the answer is that both have to become instants
    first.

    Unparseable reads as "no time", which the callers turn into "stays where it
    already was" rather than into a guess. A naive stamp is read as UTC: every
    writer in this tree emits UTC, and treating one as local time would move a
    turn by hours.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text[-1] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _ordering_clock(messages: Iterable) -> list:
    """A non-decreasing sort time for each message, in the order given.

    The clamp is the guard that keeps this merge from being able to reorder a
    sequence it is only supposed to interleave *into*. A transcript's records are
    stamped by the harness and are not guaranteed monotonic — a resumed session,
    a clock step, or a record written from a queue can date one turn before the
    one above it — and a plain timestamp sort would then move it, renumbering
    history for every cursor in flight. Carrying the previous message's time
    forward instead means an out-of-order or undated record sorts *equal* to its
    predecessor, and the position tiebreak in :func:`_interleave` leaves it where
    it was.

    The same function does the answer-after-question job on the channel side,
    because it is the same property: an answer whose ``answered_at`` is missing or
    somehow older than its question inherits the question's instant instead of
    rendering above it.
    """
    clock: list = []
    last = float("-inf")
    for message in messages:
        parsed = _timestamp_key(message.at)
        if parsed is not None and parsed > last:
            last = parsed
        clock.append(last)
    return clock


def _interleave(transcript: list, channel: list) -> list:
    """Merge channel turns into one session's transcript turns, by time.

    Returns *transcript* untouched when there is nothing to merge, which is the
    normal case and the one that matters most: a session with no ask channel — a
    run that never asked anything, or the assistant's own session, which has no
    run and no channel at all — gets exactly the list it got before this existed,
    without a clock being computed for it.

    The sort key is ``(instant, side, position)``. ``side`` puts the transcript
    first on an exact tie (the channel record is caused by the tool call the
    transcript is recording), and ``position`` is what makes the result
    deterministic rather than merely sorted: two turns that cannot be separated by
    time keep the order their own source gave them, on every read.
    """
    if not channel:
        return transcript
    ordered = [
        (instant, 0, position, message)
        for position, (instant, message) in enumerate(
            zip(_ordering_clock(transcript), transcript)
        )
    ]
    ordered += [
        (instant, 1, position, message)
        for position, (instant, message) in enumerate(
            zip(_ordering_clock(channel), channel)
        )
    ]
    ordered.sort(key=lambda item: item[:3])
    return [item[3] for item in ordered]


def _channel_messages(session_id: str) -> list:
    """One session's ask-channel exchanges as normalised turns, oldest first.

    Best-effort in the same way every other read here is, and for a sharper
    reason: the conversation is the transcript's, and a channel directory that
    cannot be read must cost the questions and nothing else. An empty list is the
    answer for a session that never asked, for one spawned before the channel
    existed, and for one that has no channel at all.

    The answer is attributed to the operator because that is who wrote it — the
    platform only ever records an answer on their behalf, and
    :func:`lmer_platform.ask.answer_question` has no other writer today.
    """
    try:
        entries = ask.read_entries(session_id)
    except (ask.AskChannelError, AskError, OSError) as exc:
        logger.warning(
            "platform_transcript_ask_unreadable session=%s error=%s — the "
            "conversation is served without the channel's turns",
            session_id, exc,
        )
        return []

    built: list = []
    for entry in entries:
        if entry.kind != KIND_QUESTION:
            # Notes are not half of an exchange; see the module docstring.
            continue
        text, truncated = _present(entry.text, TEXT_LIMIT, keep="tail")
        if text:
            built.append(Message(
                role="assistant", kind="said", text=text, at=entry.at or None,
                truncated=truncated, via=ASK_CHANNEL_VIA,
            ))
        answer = entry.answer
        if answer is None:
            continue
        text, truncated = _present(answer.text, TEXT_LIMIT, keep="tail")
        if text:
            built.append(Message(
                role="user", kind="said", text=text,
                at=answer.answered_at or None, truncated=truncated,
                via=ASK_CHANNEL_VIA,
            ))
    return built


def _collect_sources(session_id: str) -> list:
    """The run's sessions with their transcript files, oldest session first.

    Returns ``[(session_id, [Source, …]), …]``. Grouped rather than flattened
    because the ask-channel merge is per session: a channel belongs to one
    container, and interleaving one session's questions into another's transcript
    would put an answer in a conversation that never received it.

    A session with no readable transcript stays in the list with an empty file
    list, which is what makes the merge reach it — for a session whose transcript
    never got mounted out, the channel exchange is the whole conversation there is
    to show.

    Bounded: a run with a pathological number of transcripts must not be able to
    make one request read the whole directory tree. The bound is on files across
    the run, as it was before the grouping.
    """
    grouped: list = []
    files = 0
    for member in sessions_for_run(session_id):
        found: list = []
        for located in locate_sources(member):
            found.append(located)
            files += 1
            if files >= MAX_SOURCES:
                logger.warning(
                    "platform_transcript_sources_capped session=%s limit=%d",
                    session_id, MAX_SOURCES,
                )
                grouped.append((member, found))
                return grouped
        grouped.append((member, found))
    return grouped


def read_messages(
    session_id: str,
    *,
    since: int = 0,
    limit: int = DEFAULT_MESSAGE_LIMIT,
) -> MessagePage:
    """The conversation of *session_id*'s run, as a page of normalised messages.

    A negative *since* reads the last ``|since|`` messages — the same convention
    as the log route's negative offset, and for the same reason: a cold open
    wants the end of a long conversation without a round trip to ask how long it
    is. The reply says where that landed (``from``) and where to continue
    (``cursor``).

    Each session's transcript is merged with its ask channel before the sessions
    are concatenated, so an answer the operator gave through the channel appears
    as their own turn at the point they gave it — see the module docstring for why
    that happens here and not in the file.

    Never raises for a missing or unreadable transcript: the page comes back
    empty with a note. The only failure is an id that could not name a session.
    """
    _validated(session_id)
    limit = max(1, min(int(limit), MAX_MESSAGE_LIMIT))

    messages: list = []
    read: list = []
    for member, sources in _collect_sources(session_id):
        found: list = []
        for source in sources:
            from_file, described = read_source(source)
            read.append(described)
            found.extend(from_file)
        messages.extend(_interleave(found, _channel_messages(member)))

    for index, message in enumerate(messages):
        message.seq = index

    total = len(messages)
    start = max(0, total + since) if since < 0 else min(since, total)
    page = messages[start:start + limit]

    # Which empty is which. "No file" and "a file with nothing in it" are different
    # things to tell an operator, and neither may be left to look like "this run
    # said nothing".
    if total:
        note = None
    elif read:
        note = EMPTY_TRANSCRIPT_NOTE
    else:
        note = NO_TRANSCRIPT_NOTE

    return MessagePage(
        messages=tuple(page),
        start=start,
        cursor=start + len(page),
        total=total,
        sources=tuple(read),
        note=note,
    )


# --- the write path: scrubbing what a finished session left on disk ----------
#
# The read path masks credential shapes on their way to a browser. That was
# enough while the transcript died with the container; it is not enough now that
# a spawn mounts a host directory in and the file outlives the session (T22). So
# the same scrub is applied *to the file* once the session is over, and the file
# is left 0600 in a 0700 directory. See :func:`_scrub` for what that is worth —
# it is a reduction, not a guarantee.


def _scrub_decoded(value):
    """Apply :func:`_scrub` to every string inside a decoded record.

    Values, not the raw line. Some of these patterns end at the first quote and
    others do not, so letting one loose on a whole JSON line risks a match that
    runs past a closing quote and eats the next key — producing a line that is
    still valid JSON but no longer says what the harness wrote. Recursing over
    decoded values confines every substitution to one string and leaves the
    record's shape byte-for-byte intact.
    """
    if isinstance(value, str):
        return _scrub(value)
    if isinstance(value, list):
        return [_scrub_decoded(item) for item in value]
    if isinstance(value, dict):
        return {key: _scrub_decoded(item) for key, item in value.items()}
    return value


def scrub_transcript(path: Path) -> bool:
    """Rewrite one transcript file with credential shapes masked.

    Returns whether the file was replaced. ``False`` covers every failure —
    absent, unreadable, unwritable — because this is cleanup that runs on a
    daemon thread after a session has already ended: there is nothing useful to
    raise at, and a transcript that could not be scrubbed is still a transcript
    the chat view should show (the read path masks it again on the way out).

    **Only call this when the writer is gone.** The harness appends to this file
    and holds it open for the life of the session; rewriting it underneath a
    live writer means the appends after this point land in the replaced inode
    and are lost.

    Written the way :func:`lmer_platform.store.write_json` writes: a temp file in
    the same directory, then ``os.replace``. Atomicity is the point twice over —
    a crash mid-scrub leaves the previous complete file rather than a truncated
    one, and a reader (the chat view polls this) never sees a half-rewritten
    transcript. The temp stays *beside* its target for that second reason: a
    rename that crosses filesystems is a copy, and a copy is not atomic.

    The temp name carries the writer's identity — process *and* thread — because
    two writers sharing one temp path is worse than one of them losing. Both
    truncate it; one renames it onto the transcript; the other is then holding an
    open fd on the *destination* inode, so what it writes next lands on top of
    the file just published, and only then does its own ``os.replace`` fail
    ENOENT on a name that is no longer there. That is a torn transcript produced
    by the mechanism that exists to prevent torn transcripts — and the tear is
    published as a success, because the writer that renamed has no way to know
    its bytes were someone else's. A pid alone was enough while the only
    concurrent writers were separate processes, and stopped being enough once the
    daemon began serving sync handlers from a threadpool.

    No caller in this tree is known to have collided yet: the one production
    caller is a single ``spawn._watch`` thread per session, and a session's
    transcript directory is derived from its own id, so no two of them meet on
    one file. It stops being latent the moment anything re-scrubs on request —
    a route, a retry, an operator tool — beside a session ending. The thread id
    costs a handful of characters and takes the question off the table.

    The leading dot keeps the temp out of directory listings, and the ``.tmp``
    suffix is what keeps it out of :func:`_jsonl_files`: :meth:`Path.rglob`,
    unlike :mod:`glob`, does not skip dotfiles, so a crashed scrub's leftovers
    are excluded by not ending in ``.jsonl`` rather than by the dot. Naming the
    thread has one stated cost: a leftover is only ever truncated and reused by
    the thread that shares its name, so one written by a different thread
    outlives the next scrub. That is the safe direction — a name that could
    belong to a *live* sibling must never be opened O_TRUNC — and what it leaves
    behind is already-scrubbed content, 0600 inside a 0700 directory, that goes
    when the session's log directory does.

    Every line is rewritten, not just the ones that changed: the pass also
    tightens the mode the container's umask left behind
    (:data:`TRANSCRIPT_FILE_MODE`), and comparing whole files to skip a rename
    would cost more than the rename. A line that is not JSON — a torn final write
    — is scrubbed as raw text and kept: it cannot be re-serialised, and dropping
    it would destroy the evidence of the crash that produced it.

    One lossy edge, stated rather than hidden: the file is read as UTF-8 with
    ``errors="replace"``, so a byte sequence that is not valid UTF-8 is replaced
    on the way through. That is the same substitution :func:`_iter_records`
    already makes, so nothing the chat view can show changes — but a transcript
    with undecodable bytes in it does not come back byte-identical.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        source = path.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning(
            "platform_transcript_scrub_unreadable path=%s error=%s", path, exc
        )
        return False
    try:
        with source:
            # O_TRUNC rather than O_EXCL: a leftover temp from a crashed scrub
            # must not make this file unscrubbable forever. fchmod before the
            # first write, because os.open ignores its mode argument for a file
            # that already exists — so a stale temp's wider mode is corrected
            # while the file is still empty.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, TRANSCRIPT_FILE_MODE)
            with os.fdopen(fd, "w", encoding="utf-8") as sink:
                os.fchmod(sink.fileno(), TRANSCRIPT_FILE_MODE)
                while True:
                    # Bounded like _iter_records, and for the same reason: a
                    # binary file misnamed .jsonl must not be read into the
                    # daemon's heap whole. A chunk of an oversized line is
                    # scrubbed and written back as it stands, so the file's
                    # content survives the pass even when it cannot be parsed.
                    raw = source.readline(_MAX_LINE_BYTES)
                    if not raw:
                        break
                    record = None
                    if len(raw) < _MAX_LINE_BYTES or raw.endswith("\n"):
                        try:
                            record = json.loads(raw)
                        except ValueError:
                            record = None
                    if isinstance(record, dict):
                        sink.write(
                            json.dumps(_scrub_decoded(record), ensure_ascii=False) + "\n"
                        )
                    else:
                        sink.write(_scrub(raw))
        os.replace(tmp, path)
    except (OSError, RecursionError) as exc:
        # RecursionError names a real shape rather than a hypothetical: a record
        # nested deeply enough to parse but not deeply enough to walk would
        # otherwise leave the temp file behind and take the exception up into the
        # exit-bookkeeping thread that called this.
        logger.warning(
            "platform_transcript_scrub_failed path=%s error=%r", path, exc
        )
        # No half-written temp left to confuse the next post-mortem — or the
        # next scrub, which would otherwise inherit it.
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    return True


def scrub_session_transcripts(session_id: str) -> int:
    """Scrub the transcripts the platform owns for one session. Returns the count.

    Called when a session's process has exited (``spawn._watch``), on both the
    clean and the crashed path: a crashed session keeps its registry entry as
    the crash signal, but there is no version of that argument in which its
    transcript should sit on disk with raw credentials in it.

    Scope is deliberate and narrow: **only** the platform's own per-session
    directory (:func:`session_transcript_dir`), never a pointer recorded on the
    session entry. :func:`locate_sources` will happily follow a pointer into the
    invoking user's own ``~/.claude/projects`` — reading someone's transcript is
    the chat view's job, and rewriting it in place is not. The platform created,
    mounted and owns this one directory, so that is the one it may rewrite.

    Anything in there that does not resolve inside it is skipped for the same
    reason: the directory is mounted read-write into a container, so a symlink in
    it is something the observed session could have planted, and following one
    would copy an unrelated file's contents into this session's chat view. That
    refusal is :func:`_jsonl_files`' own, so the read path gets it too — this
    used to be the only place it was made, and ``locate_sources`` served whatever
    the link pointed at.
    """
    own_dir = session_transcript_dir(session_id)
    scrubbed = 0
    for path in _jsonl_files(own_dir):
        if scrub_transcript(path):
            scrubbed += 1
    if scrubbed:
        logger.info(
            "platform_transcript_scrubbed session=%s files=%d", session_id, scrubbed
        )
    return scrubbed
