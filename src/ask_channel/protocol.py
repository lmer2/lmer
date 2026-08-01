"""The ask channel's wire format: one directory, five file shapes (spec D26/D27).

The transport is a bind-mounted directory, not HTTP
---------------------------------------------------
The platform binds its API on the *host* (127.0.0.1 by default), and inside a
container that address is the container's own loopback — reaching the daemon
would need a host-gateway address that differs between docker, podman and
rootless podman, and it would mean putting the platform's shared secret inside
every session. A directory the platform already knows how to mount (``lmer
--mount-dir``, the machinery T22 built for transcripts) needs neither: the mount
*is* the authorization, so a session can only ever see its own channel and no
credential travels into the container.

The cost is that both ends poll a filesystem instead of being pushed to, which
is why the shapes below are files rather than messages, and why every read here
tolerates a file it cannot parse.

The format is text plus an optional list of options (D27)
---------------------------------------------------------
Deliberately not modelled on Claude Code's ``AskUserQuestion``: that is one
harness's tool, and codex and pi have no equivalent. A question is text, choices
are a list of strings, an answer is text — so any harness that can run a command
gets this, and an operator answering from a phone can always type something the
agent did not offer.

Files, and who writes which
---------------------------
::

    000001.question.json   written by the session   (the agent asks)
    000001.answer.json     written by the platform  (the operator answers)
    000001.closed.json     written by the session   (it stopped waiting)
    000001.read.json       written by the session   (the answer reached the agent)
    000002.note.json       written by the session   (progress, no reply wanted)
    000003.signal.json     written by the session   (a milestone, for the daemon)

Nobody rewrites anybody else's file. That is not politeness: the directory is
mounted read-write into a container, so host and container write it as different
processes (as different *uids* on a runtime without ``--userns=keep-id`` — the
same constraint T22's transcript mount lives with), and a single-writer-per-file
rule is what keeps that from needing coordination.

Ordering, and why an answer cannot land on the wrong question
-------------------------------------------------------------
The name of every file starts with a zero-padded sequence number, allocated by
scanning the directory. That gives the channel one total order — the operator
reads questions in the order they were asked, and a cursor over the feed means
something.

Allocation is by :func:`os.link`, which is atomic *and* refuses an existing
target, so two agents asking at the same moment cannot both take id 5: the loser
gets ``FileExistsError`` and draws again. Writing the content first and linking
it into place also means a reader never sees a half-written question.

Three things then stand between an answer and the wrong question:

1. The answer's *filename* is derived from the question's id, so pairing is not
   a search.
2. The answer record repeats ``question_id``, and the reader checks it.
3. The question carries a random ``nonce`` which the answer copies, and the
   reader checks that too. This is the one that survives the nasty case: an id
   reused after its question file was deleted, where the id alone would match a
   stale answer to a brand-new question.

Sequence numbers are compared as integers, never as strings: the zero-padding is
for the operator reading a directory listing, and a channel that somehow reached
a million entries must not silently reorder itself.

Closing a question, and why that is a fourth file
-------------------------------------------------
A question outlives the wait that posted it — the agent may have timed out and
moved on, decided without the operator, or be about to exit — and until it is
answered the operator's view offers a reply box for it either way. That is the
"answer into the void" case: work typed into a box nothing is reading.
``lmer-ask close`` marks the question instead, and marking it is *another file*
rather than a field flipped inside the question record, because the question
record belongs to the session and rewriting it in place would break the
single-writer rule above — and would make every reader tolerate a
half-rewritten question, which today none of them ever has to.

Absent means open, and it has to keep meaning that permanently: a session on an
older image writes no close record at all, so the state is the presence of a
file rather than a field that could arrive missing and be mistaken for
"closed".

An answer that raced a close wins
---------------------------------
Both writes are exclusive and neither side waits for the other, so a close and
an answer can both land on the same question. Readers resolve it one way, here
and everywhere it surfaces: an answered question is answered, whatever else is
filed beside it (:func:`is_answerable`, and ``answered`` before ``closed`` in
every presentation of an entry). No operator work is discarded, and a session
that comes back to ``lmer-ask wait`` still gets the reply. In the other
direction :func:`close_question` refuses to file anything once an answer is on
disk, so the sequential case never produces the pair at all.

An answer on disk is not an answer delivered
--------------------------------------------
A session that timed out its wait, worked on something else and never waited
again leaves the operator's reply sitting in the channel, read by nobody. From
the host that is indistinguishable from an answer the agent acted on, and from
the agent it is indistinguishable from a question nobody answered — so the verb
that hands an answer's *text* to the agent files ``NNNNNN.read.json`` beside it,
and "answered, never read" becomes a state something can see.
:func:`mark_answer_read` is where the rule about which verbs count lives.

Written by the session, like the close record and for the same reason: the answer
belongs to the platform and nobody rewrites anybody else's file. Absent means
unread and has to keep meaning that permanently — a session on an older image
files none — and a receipt that does not match the question it is filed under, or
cannot be parsed, is treated as absent too. That direction costs one redundant
delivery of an answer the agent may already have seen; the other direction hides
an operator's reply, which is the incident this file exists for.

Nothing here raises attention on an unread answer. The artifact is what a
platform-side crossing of "answered" against "read" needs, and
``lmer-end-session`` reads it to refuse a shutdown that would strand a reply.

Nothing here closes a question on the session's behalf. The platform already
knows a session has exited — it holds the registry — and inferring
unanswerability from liveness is both stronger evidence than a file a dying
session may not have managed to write and the only version that keeps a
*detached* session's questions answerable (spec D23): detached is alive, and its
channel is a mount rather than a control plane.

A signal is addressed past the operator, to the orchestrator (T122)
-------------------------------------------------------------------
``NNNNNN.signal.json`` is the sixth shape and the only one whose reader is
neither end of this channel's original pair: the session files it to say a
milestone happened — an MR pushed, a review finished, the current task done — and
what picks it up is the platform's detection tick, which turns it into a digest
for the *supervising assistant* (:mod:`lmer_platform.detect`). The operator's
tools are the question and the note; this one exists because "an agent finished
something" is a fact the orchestrator needs in order to route the next step, and
the operator does not need to be woken for it (operator request, 2026-07-29).

It is written exactly like a note — one allocation, one exclusive link, counted
by the same entry cap — and answered by nothing: one-way and terminal, so there
is no answer, no closure and no receipt filed under its id. The id is allocated
from the same sequence anyway, because ids are the channel's one total order and
a shape that took ids from a second counter could collide with the first.

It is deliberately **not** in :func:`read_entries`. That function is the
operator's feed — it is what the fleet view and ``lmer-ask list`` render — and a
milestone the orchestrator already consumed would arrive there as a card that
cannot be answered and did not ask for anything. :func:`read_signals` is the
reader for this shape, and it is called by the daemon.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Optional

__all__ = [
    "SCHEMA_VERSION", "ASK_DIR_ENV", "CONTAINER_ASK_DIR", "DIR_MODE",
    "KIND_QUESTION", "KIND_NOTE", "KIND_SIGNAL", "QUESTION_SUFFIX",
    "NOTE_SUFFIX", "SIGNAL_SUFFIX",
    "ANSWER_SUFFIX", "CLOSED_SUFFIX", "READ_SUFFIX", "MAX_QUESTION_CHARS",
    "MAX_NOTE_CHARS", "MAX_ANSWER_CHARS", "MAX_REASON_CHARS",
    "MAX_SIGNAL_CHARS",
    "MAX_OPTIONS", "MAX_OPTION_CHARS", "MAX_OPEN_QUESTIONS", "MAX_ENTRIES",
    "AskError", "ChannelUnavailable", "AnswerMismatch", "Answer", "Closure",
    "Receipt", "Entry",
    "utc_now_iso", "valid_entry_id", "resolve_channel_dir", "post_question",
    "post_note", "post_signal", "read_signals",
    "read_entries", "read_entry", "load_answer", "answer_for",
    "load_closure", "closure_for", "close_question", "is_answerable",
    "load_receipt", "receipt_for", "mark_answer_read", "is_answer_unread",
    "unread_answers",
    "open_questions", "write_answer", "wait_for_answer",
]

#: Bumped when a field changes meaning. Both ends read tolerantly, so a record
#: from a *newer* writer is used for what this build understands rather than
#: refused — an unanswerable question helps nobody.
SCHEMA_VERSION = 1

#: How a session learns it has a channel at all, and where. Set by the platform
#: at spawn (:mod:`lmer_platform.spawn`) and forwarded into the container by the
#: host CLI's env dict. Unset means "not orchestrated" — the CLI says so and
#: exits rather than inventing a directory nobody is watching.
ASK_DIR_ENV = "LMER_ASK_DIR"

#: Where the platform mounts the session's channel *inside* the container. A
#: dedicated hidden directory under the container home rather than a subdirectory
#: of ``~/.lmer``, which is itself a mount — nesting one bind mount inside another
#: makes the result depend on mount order.
CONTAINER_ASK_DIR = "/home/developer/.lmer-ask"

#: Mode for the channel directory. It holds whatever the session asked and the
#: operator answered, and it is rw-mounted into a container, so it gets the PTY
#: log's sensitivity rather than a cache's.
DIR_MODE = 0o700

KIND_QUESTION = "question"
KIND_NOTE = "note"

#: A milestone the session reports to the orchestrator rather than to the operator
#: (module docstring, T122). Nothing answers one.
KIND_SIGNAL = "signal"

QUESTION_SUFFIX = f".{KIND_QUESTION}.json"
NOTE_SUFFIX = f".{KIND_NOTE}.json"
SIGNAL_SUFFIX = f".{KIND_SIGNAL}.json"
ANSWER_SUFFIX = ".answer.json"
CLOSED_SUFFIX = ".closed.json"

#: Filed when an answer's text has been handed to the agent. A file rather than a
#: field on the answer record because the answer is the platform's file and
#: nothing here rewrites another writer's file (module docstring).
READ_SUFFIX = ".read.json"

#: The files filed *under* a question rather than being entries of their own.
#: Listed once so a reader cannot learn about one and miss the other — the whole
#: cost of adding the close record was the places that enumerate suffixes.
_SIDECAR_SUFFIXES = (ANSWER_SUFFIX, CLOSED_SUFFIX, READ_SUFFIX)

#: Suffix -> kind for the shapes the *operator* is shown: what :func:`read_entries`
#: returns, and nothing else. A signal is an entry with an id of its own and is
#: deliberately absent (module docstring), so this is a mapping rather than a test
#: against the sidecars — a shape added later is out of the operator's feed until
#: somebody puts it in, which is the direction that cannot surprise a reader.
_OPERATOR_SUFFIXES = {QUESTION_SUFFIX: KIND_QUESTION, NOTE_SUFFIX: KIND_NOTE}

#: Suffix -> kind for :func:`read_signals`. Same shape as the mapping above so
#: both readers go through one scan.
_SIGNAL_SUFFIXES = {SIGNAL_SUFFIX: KIND_SIGNAL}

#: Every suffix that is an entry in its own right — one that owns an id. Every
#: shape belongs here or in :data:`_SIDECAR_SUFFIXES`, and both are what id
#: allocation counts: a shape missing from either would have its id handed out a
#: second time.
_ENTRY_SUFFIXES = (*_OPERATOR_SUFFIXES, *_SIGNAL_SUFFIXES)

#: Every suffix this module writes: the entries and the files filed under them.
#: What id allocation scans, and the default for :func:`_entry_parts` — a caller
#: that means the narrower set names it.
_ALL_SUFFIXES = (*_ENTRY_SUFFIXES, *_SIDECAR_SUFFIXES)

#: Ceilings. A question is a sentence an operator reads on a phone, an answer is
#: a sentence they type on one; these are far above either. They exist so a
#: runaway agent pasting a log file into a question cannot fill the state dir,
#: and are enforced on write rather than on read (a record already on disk is
#: shown as-is — refusing to display it would only hide the mistake).
MAX_QUESTION_CHARS = 8192
MAX_NOTE_CHARS = 4096
MAX_ANSWER_CHARS = 16384
MAX_OPTIONS = 12
MAX_OPTION_CHARS = 200

#: Ceiling on a signal. Tighter than a note, and the reason is where it lands: a
#: note is read by a person scrolling a feed, while a signal is charged against the
#: orchestrator's context window every time one is delivered
#: (:func:`lmer_platform.assistant.notify` bounds its own note at less than this
#: and the daemon truncates to fit). "Pushed MR !167 for review" is the shape; the
#: detail belongs in the MR.
MAX_SIGNAL_CHARS = 1000

#: Ceiling on the note a session may leave when it closes a question. A clause —
#: "timed out, took the safe branch" — because it is read beside the question in
#: a list, not instead of it.
MAX_REASON_CHARS = 500

#: How many unanswered questions one session may have outstanding. An agent that
#: loops asking is the failure this bounds: without it the operator's view fills
#: with questions nobody will ever read, and the interesting one is buried.
MAX_OPEN_QUESTIONS = 32

#: Ceiling on entries returned by one read. A cap rather than paging: a channel
#: this long is pathological, and the *recent* end is the part anyone wants.
MAX_ENTRIES = 500

#: Entry ids are digits only and land in a filename, and one of them arrives
#: from a URL path segment, so they are validated rather than sanitized. Linear
#: pattern with a bounded repeat — no nested quantifier for a crafted id to
#: backtrack through.
_ENTRY_ID_RE = re.compile(r"^[0-9]{1,12}$")

#: Width of the zero-padded id. Cosmetic (see the module docstring on ordering).
_ID_WIDTH = 6

#: Bytes of entropy behind a question's nonce.
_NONCE_BYTES = 8

#: Ceiling on a receipt's ``via`` label. It names a verb of ours, not text
#: anybody typed.
_MAX_VIA_CHARS = 64

#: How many times an allocation re-draws after losing the link race. Each loss
#: means another writer took the id, so a handful covers any realistic
#: concurrency; exhausting them is reported rather than retried forever.
_ALLOCATE_ATTEMPTS = 16


class AskError(RuntimeError):
    """Something is wrong with the channel or with what was written to it."""


class ChannelUnavailable(AskError):
    """There is no channel: not orchestrated, or the mount is not there.

    Separate from :class:`AskError` because the caller's move is different — a
    session with no channel should say what it needs in its ordinary output and
    carry on, not retry.
    """


class AnswerMismatch(AskError):
    """An answer file does not belong to the question it is filed under.

    Only corruption or tampering produces this, and it is raised rather than
    swallowed: silently treating someone else's answer as this question's is the
    one failure this format exists to prevent.
    """


def utc_now_iso() -> str:
    """Timestamp in the format the rest of lmer's state files use.

    Second resolution, which is fine because ordering is the sequence number's
    job — two entries written in the same second are still strictly ordered.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Answer:
    """What the operator sent back."""

    question_id: str
    text: str
    answered_at: str
    source: str = "operator"
    nonce: str = ""

    def to_dict(self) -> dict:
        return {
            "question_id": self.question_id,
            "text": self.text,
            "answered_at": self.answered_at,
            "source": self.source,
        }


@dataclass(frozen=True)
class Closure:
    """The session saying it has stopped waiting for a reply.

    Written by the session, never by the platform — see the module docstring on
    why nothing closes a question on a session's behalf.
    """

    question_id: str
    closed_at: str
    reason: str = ""
    nonce: str = ""

    def to_dict(self) -> dict:
        return {
            "question_id": self.question_id,
            "closed_at": self.closed_at,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Receipt:
    """The session saying an answer's text reached the agent.

    ``via`` names the verb that delivered it, which is what a later reader has to
    tell "the agent came back and waited for this" from "the shutdown backstop
    pushed it in front of the agent".
    """

    question_id: str
    read_at: str
    via: str = ""
    nonce: str = ""

    def to_dict(self) -> dict:
        return {
            "question_id": self.question_id,
            "read_at": self.read_at,
            "via": self.via,
        }


@dataclass(frozen=True)
class Entry:
    """One thing the session put on the channel: a question or a note."""

    id: str
    seq: int
    kind: str
    text: str
    at: str
    options: tuple = ()
    nonce: str = ""
    answer: Optional[Answer] = None
    closure: Optional[Closure] = None
    receipt: Optional[Receipt] = None
    #: Set when the entry is readable but something about it is not: today, an
    #: answer file that does not match this question. Carried rather than raised
    #: so one broken pair cannot empty the operator's whole view.
    problem: Optional[str] = None

    @property
    def answered(self) -> bool:
        return self.answer is not None

    @property
    def closed(self) -> bool:
        return self.closure is not None

    @property
    def answer_read(self) -> bool:
        """Whether this question's answer has been handed to the agent.

        ``False`` while unanswered: a receipt with no answer beside it claims a
        delivery of nothing, so it is not one.
        """
        return self.answer is not None and self.receipt is not None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "seq": self.seq,
            "kind": self.kind,
            "text": self.text,
            "at": self.at,
            "options": list(self.options),
            "answered": self.answered,
            "answer": self.answer.to_dict() if self.answer else None,
            # An answer nobody read is not the same state as one the agent acted
            # on, and only the receipt tells them apart.
            "answer_read": self.answer_read,
            "receipt": self.receipt.to_dict() if self.receipt else None,
            # Both flags, both directions of the race: a consumer decides what to
            # show from them, and the rule everywhere is answered first.
            "closed": self.closed,
            "closure": self.closure.to_dict() if self.closure else None,
            "problem": self.problem,
        }


def valid_entry_id(entry_id: object) -> bool:
    """Whether *entry_id* could name an entry. See :data:`_ENTRY_ID_RE`."""
    return isinstance(entry_id, str) and bool(_ENTRY_ID_RE.match(entry_id))


def _require_entry_id(entry_id: object) -> str:
    if not valid_entry_id(entry_id):
        raise AskError(f"invalid entry id {entry_id!r}: expected digits only")
    return str(entry_id)


def resolve_channel_dir(
    explicit: Optional[str] = None,
    *,
    env: Optional[Mapping] = None,
) -> Path:
    """The channel directory, or :class:`ChannelUnavailable` explaining why not.

    Order is ``explicit`` (a ``--dir`` flag) then :data:`ASK_DIR_ENV`.

    A directory that does not exist is refused and **never created**. Creating it
    is the tempting one-liner and it is exactly wrong: the platform makes this
    directory before the container starts, so its absence means the mount did not
    happen — and a channel nobody has mounted is a place to post questions that
    will never be read and to block forever waiting for answers that can never
    come. Failing here turns that hang into an exit code.
    """
    raw = (explicit or "").strip()
    source = "--dir"
    if not raw:
        environ = env if env is not None else os.environ
        raw = (environ.get(ASK_DIR_ENV) or "").strip()
        source = ASK_DIR_ENV
    if not raw:
        raise ChannelUnavailable(
            f"{ASK_DIR_ENV} is not set: this session was not started by the lmer "
            "orchestrator, so there is no operator channel to ask through"
        )
    directory = Path(raw)
    if not directory.is_dir():
        raise ChannelUnavailable(
            f"the ask channel directory {directory} ({source}) does not exist — "
            "the orchestrator mounts it before the session starts, so this means "
            "the mount is missing; nothing written here would be read"
        )
    if not os.access(directory, os.W_OK | os.X_OK):
        # The mount is there and unwritable, which is what a uid mismatch across
        # the bind mount looks like: the host directory is 0700 and the container
        # is not running as its owner (``--userns=keep-id`` or a matching
        # ``BUILD_UID`` is what normally makes it so). Reported as "no channel"
        # rather than as a write error, because the caller's move is the same —
        # say what you need in ordinary output — and because the alternative is
        # discovering it question by question.
        raise ChannelUnavailable(
            f"the ask channel directory {directory} ({source}) is not writable by "
            "this user — the mount is there but the container is not running as "
            "its owner, so a question posted here could not be written"
        )
    return directory


def _entry_parts(name: str, suffixes: Iterable = _ALL_SUFFIXES) -> Optional[tuple]:
    """Split a filename into ``(seq, id, suffix)``, or ``None`` if it is not ours.

    Everything else in the directory — temp files, an editor's backup, a stray
    note from an operator poking around — is simply not an entry.

    *suffixes* is which shapes the caller means, and callers disagree: id
    allocation counts the sidecars too (an id must never be reused), while
    counting *entries* must not — a healthy question accumulates up to three of
    them. The default is the wider set, so a caller that says nothing gets the
    parse that cannot hand out an id twice; a caller that means entries alone
    passes :data:`_ENTRY_SUFFIXES` and says so.
    """
    for suffix in suffixes:
        if not name.endswith(suffix):
            continue
        stem = name[: -len(suffix)]
        if not valid_entry_id(stem):
            return None
        return int(stem), stem, suffix
    return None


def _next_seq(directory: Path) -> int:
    """One past the highest id in the directory, counting the sidecars too.

    Answers and close records count because an id whose question file was deleted
    must not be handed out again — a stale answer beside a fresh question is the
    one pairing the nonce check exists to catch, and not reusing the id keeps it
    from happening in the first place.
    """
    highest = 0
    try:
        names = os.listdir(directory)
    except OSError as exc:
        raise AskError(f"cannot read the ask channel at {directory} ({exc})")
    for name in names:
        parts = _entry_parts(name)
        if parts is not None and parts[0] > highest:
            highest = parts[0]
    return highest + 1


def _write_new(directory: Path, name: str, payload: dict) -> Path:
    """Create *name* holding *payload*, atomically and only if it is not there.

    Content first into a private temp file, then :func:`os.link` into place:
    ``link`` is atomic (a reader sees the whole record or no file at all) and
    fails on an existing target (so the id is claimed exclusively). ``rename``
    would give the first property and silently clobber for the second.
    """
    target = directory / name
    tmp = directory / f".{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.link(tmp, target)
    except FileExistsError:
        _unlink_quietly(tmp)
        raise
    except OSError as exc:
        _unlink_quietly(tmp)
        raise AskError(f"cannot write {name} in {directory} ({exc})")
    _unlink_quietly(tmp)
    return target


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _clean_text(text: object, limit: int, label: str) -> str:
    """Validate one text field: a non-empty string within *limit* characters."""
    if not isinstance(text, str):
        raise AskError(f"{label} must be text, got {type(text).__name__}")
    cleaned = text.strip()
    if not cleaned:
        raise AskError(f"{label} is empty")
    if len(cleaned) > limit:
        raise AskError(
            f"{label} is {len(cleaned)} characters, over the {limit} limit — "
            "say the short version and leave the detail where it already is"
        )
    return cleaned


def _clean_options(options: Optional[Iterable]) -> tuple:
    """Validate the optional choice list. Empty is the normal case."""
    if not options:
        return ()
    cleaned = []
    for option in options:
        if not isinstance(option, str):
            raise AskError(f"options must be text, got {type(option).__name__}")
        value = option.strip()
        if not value:
            raise AskError("an empty option is not a choice")
        if len(value) > MAX_OPTION_CHARS:
            raise AskError(
                f"option is {len(value)} characters, over the "
                f"{MAX_OPTION_CHARS} limit"
            )
        cleaned.append(value)
    if len(cleaned) > MAX_OPTIONS:
        raise AskError(
            f"{len(cleaned)} options is over the {MAX_OPTIONS} limit — an "
            "operator picking from a phone needs a short list"
        )
    return tuple(cleaned)


def _entry_count(directory: Path) -> int:
    """How many entries the channel holds — sidecars are not entries.

    Notes have no answer to wait for, so nothing else bounds them: an agent
    looping on ``lmer-ask note`` would grow a directory in the platform's state
    dir forever, and the reader would stop showing the old ones anyway
    (:data:`MAX_ENTRIES`). Refusing at the cap turns unbounded growth into a
    visible failure in the session that is causing it.

    Only :data:`_ENTRY_SUFFIXES` are counted, and that is the whole point: an
    answered-and-read question is three files, so counting files would refuse a
    session at a third of the cap — and tell it that it was looping, which is a
    long session's ordinary shape rather than a fault (issue #191). The cap is on
    what a session *posted*; the files a question accumulates afterwards are the
    channel working.
    """
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    return sum(1 for name in names if _entry_parts(name, _ENTRY_SUFFIXES) is not None)


def _post(directory: Path, kind: str, payload: dict, suffix: str) -> Entry:
    """Allocate an id and land *payload* under it. Shared by question and note."""
    held = _entry_count(directory)
    if held >= MAX_ENTRIES:
        raise AskError(
            f"this channel already holds {held} entries, which is the limit "
            f"({MAX_ENTRIES}) — a session posting this much is looping, and the "
            "operator stopped reading long ago"
        )
    last_error: Optional[Exception] = None
    for _ in range(_ALLOCATE_ATTEMPTS):
        seq = _next_seq(directory)
        entry_id = f"{seq:0{_ID_WIDTH}d}"
        record = {**payload, "schema": SCHEMA_VERSION, "id": entry_id, "kind": kind}
        try:
            _write_new(directory, f"{entry_id}{suffix}", record)
        except FileExistsError as exc:  # another writer took this id; draw again
            last_error = exc
            continue
        return Entry(
            id=entry_id,
            seq=seq,
            kind=kind,
            text=record["text"],
            at=record["at"],
            options=tuple(record.get("options") or ()),
            nonce=record.get("nonce", ""),
        )
    raise AskError(
        f"could not claim an id in {directory} after {_ALLOCATE_ATTEMPTS} "
        f"attempts — something else is writing the channel as fast as we are "
        f"({last_error})"
    )


def post_question(
    directory: Path,
    text: str,
    options: Optional[Iterable] = None,
) -> Entry:
    """Ask the operator something. Returns the entry, whose ``id`` names it.

    The open-question cap is checked here rather than by the caller because
    every way of asking goes through this function.
    """
    body = _clean_text(text, MAX_QUESTION_CHARS, "the question")
    choices = _clean_options(options)
    outstanding = len(open_questions(directory))
    if outstanding >= MAX_OPEN_QUESTIONS:
        raise AskError(
            f"{outstanding} questions on this channel are still open, which is "
            f"the limit ({MAX_OPEN_QUESTIONS}) — wait for an answer, or close "
            "the ones you stopped waiting for, instead of asking again"
        )
    return _post(
        directory,
        KIND_QUESTION,
        {
            "text": body,
            "options": list(choices),
            "at": utc_now_iso(),
            "nonce": secrets.token_hex(_NONCE_BYTES),
        },
        QUESTION_SUFFIX,
    )


def post_note(directory: Path, text: str) -> Entry:
    """Tell the operator something that wants no reply."""
    body = _clean_text(text, MAX_NOTE_CHARS, "the note")
    return _post(
        directory, KIND_NOTE, {"text": body, "at": utc_now_iso()}, NOTE_SUFFIX
    )


def post_signal(directory: Path, text: str) -> Entry:
    """Tell the orchestrator a milestone happened. Nothing answers it.

    Filed like a note and read by the daemon rather than by the operator — see
    the module docstring on who the reader is, and :func:`read_signals`. There is
    no cap of its own beyond :data:`MAX_ENTRIES`: a session signalling in a loop
    is bounded by the same full-channel refusal a session posting notes in a loop
    hits, and it is the one bound that cannot be spent by a *waiting* agent.
    """
    body = _clean_text(text, MAX_SIGNAL_CHARS, "the signal")
    return _post(
        directory, KIND_SIGNAL, {"text": body, "at": utc_now_iso()}, SIGNAL_SUFFIX
    )


def _read_json(path: Path) -> Optional[dict]:
    """Parse one record. ``None`` for anything that is not a usable object.

    Tolerant on purpose and at the lowest level, so every reader above inherits
    it: a file torn by a crash, a half-written record from a writer that died
    between ``open`` and ``link``, or a directory entry that vanished between the
    listing and the read must cost that one entry and nothing else.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _entry_from_record(entry_id: str, seq: int, kind: str, record: dict) -> Optional[Entry]:
    """Build an :class:`Entry`, or ``None`` when the record cannot be trusted.

    The filename decides the id and the kind; a record disagreeing with its own
    filename is dropped rather than reconciled, because there is no way to tell
    which half is the corrupt one.
    """
    if record.get("id") not in (None, entry_id):
        return None
    if record.get("kind") not in (None, kind):
        return None
    text = record.get("text")
    if not isinstance(text, str):
        return None
    raw_options = record.get("options")
    options = tuple(
        option for option in raw_options if isinstance(option, str)
    ) if isinstance(raw_options, list) else ()
    at = record.get("at")
    nonce = record.get("nonce")
    return Entry(
        id=entry_id,
        seq=seq,
        kind=kind,
        text=text,
        at=at if isinstance(at, str) else "",
        options=options,
        nonce=nonce if isinstance(nonce, str) else "",
    )


def load_answer(directory: Path, entry_id: str) -> Optional[Answer]:
    """Read the answer filed under *entry_id*, if any.

    ``None`` means unanswered. A file that is there but unreadable raises
    :class:`AskError`: an operator did answer, and reporting that as "still
    waiting" would leave the session blocked on a reply that already exists.
    """
    entry_id = _require_entry_id(entry_id)
    path = directory / f"{entry_id}{ANSWER_SUFFIX}"
    if not path.exists():
        return None
    record = _read_json(path)
    if record is None:
        raise AskError(
            f"the answer to question {entry_id} is on disk but unreadable "
            f"({path}) — it was written but something corrupted it"
        )
    text = record.get("text")
    if not isinstance(text, str):
        raise AskError(f"the answer to question {entry_id} carries no text ({path})")
    answered_at = record.get("answered_at")
    source = record.get("source")
    question_id = record.get("question_id")
    return Answer(
        question_id=question_id if isinstance(question_id, str) else "",
        text=text,
        answered_at=answered_at if isinstance(answered_at, str) else "",
        source=source if isinstance(source, str) else "operator",
        nonce=record.get("nonce") if isinstance(record.get("nonce"), str) else "",
    )


def answer_for(directory: Path, question: Entry) -> Optional[Answer]:
    """The answer to *question*, checked against it. ``None`` while unanswered.

    Both checks are the point of this function — see the module docstring. A
    question written before nonces existed carries an empty one, and an answer
    to it is accepted on the id alone rather than being permanently unreadable.
    """
    answer = load_answer(directory, question.id)
    if answer is None:
        return None
    if answer.question_id and answer.question_id != question.id:
        raise AnswerMismatch(
            f"the answer filed under {question.id} says it answers "
            f"{answer.question_id!r} — refusing to treat it as this question's"
        )
    if question.nonce and answer.nonce and answer.nonce != question.nonce:
        raise AnswerMismatch(
            f"the answer filed under {question.id} was written for a different "
            "question with the same id — refusing to treat it as this one's"
        )
    return answer


def load_closure(directory: Path, entry_id: str) -> Optional[Closure]:
    """Read the close record filed under *entry_id*, if there is a usable one.

    Unreadable is treated as absent, which is the opposite of
    :func:`load_answer` and deliberately so. The two failures are not
    symmetrical: an answer on disk that cannot be read means the operator's reply
    exists and must not be reported as "still waiting", while a close record that
    cannot be read only ever *removes* the operator's ability to reply. Failing
    open costs a stale question in the feed; failing closed would refuse an answer
    somebody still wanted to give, on the strength of a corrupt file.
    """
    entry_id = _require_entry_id(entry_id)
    record = _read_json(directory / f"{entry_id}{CLOSED_SUFFIX}")
    if record is None:
        return None
    closed_at = record.get("closed_at")
    reason = record.get("reason")
    question_id = record.get("question_id")
    return Closure(
        question_id=question_id if isinstance(question_id, str) else "",
        closed_at=closed_at if isinstance(closed_at, str) else "",
        reason=reason if isinstance(reason, str) else "",
        nonce=record.get("nonce") if isinstance(record.get("nonce"), str) else "",
    )


def closure_for(directory: Path, question: Entry) -> Optional[Closure]:
    """The close record belonging to *question*, checked against it.

    The same two checks :func:`answer_for` makes, for the same reason and with
    the other outcome: a record naming a different question, or carrying the
    nonce of an earlier question that reused this id, is ignored rather than
    raised. A stale close record must not silence a brand-new question — that
    would take a live question off the operator's screen — and the entry is
    readable either way, so there is nothing for a caller to recover from.
    """
    closure = load_closure(directory, question.id)
    if closure is None:
        return None
    if closure.question_id and closure.question_id != question.id:
        return None
    if question.nonce and closure.nonce and closure.nonce != question.nonce:
        return None
    return closure


def load_receipt(directory: Path, entry_id: str) -> Optional[Receipt]:
    """Read the read receipt filed under *entry_id*, if there is a usable one.

    Unreadable is treated as absent, as for a close record and with the same
    asymmetry in mind: a receipt is the only thing that can say an answer already
    reached the agent, so a corrupt one must not be allowed to say it. Failing
    this way costs one redundant delivery; failing the other way strands an
    operator's reply.
    """
    entry_id = _require_entry_id(entry_id)
    record = _read_json(directory / f"{entry_id}{READ_SUFFIX}")
    if record is None:
        return None
    read_at = record.get("read_at")
    via = record.get("via")
    question_id = record.get("question_id")
    return Receipt(
        question_id=question_id if isinstance(question_id, str) else "",
        read_at=read_at if isinstance(read_at, str) else "",
        via=via if isinstance(via, str) else "",
        nonce=record.get("nonce") if isinstance(record.get("nonce"), str) else "",
    )


def receipt_for(directory: Path, question: Entry) -> Optional[Receipt]:
    """The receipt belonging to *question*, checked against it.

    The two checks :func:`closure_for` makes, with the same outcome: a record
    naming another question, or carrying the nonce of an earlier question that
    reused this id, is ignored rather than raised. A stale receipt would claim
    that an answer nobody has seen was already delivered.
    """
    receipt = load_receipt(directory, question.id)
    if receipt is None:
        return None
    if receipt.question_id and receipt.question_id != question.id:
        return None
    if question.nonce and receipt.nonce and receipt.nonce != question.nonce:
        return None
    return receipt


def mark_answer_read(directory: Path, question: Entry, *, via: str = "") -> Receipt:
    """File the receipt saying *question*'s answer was handed to the agent.

    **Which verbs call this** is the rule, and it is not "everything that touches
    an answer". A receipt means *this answer's text was put in front of the agent
    as the answer to this question*:

    * ``lmer-ask ask`` and ``lmer-ask wait`` mark read — the answer is the
      command's output, printed on stdout, and getting it is why they ran.
    * ``lmer-ask close`` marks read when an answer beat the close, because it
      hands that answer over on stdout exactly as ``wait`` would.
    * ``lmer-end-session`` marks read when it refuses a shutdown over an unread
      answer: the refusal prints the answer, so the refusal is the delivery.
    * ``lmer-ask list`` does **not**, in either output mode. It is a survey of the
      channel rather than the delivery of one answer — a one-line-per-entry
      summary an agent runs to see where things stand — and it is where
      unread-ness is *displayed*: a survey that consumed receipts would erase the
      state it exists to report, and an agent listing the channel for a status
      line would silently clear the one signal that catches a stranded answer.

    Idempotent: a second read files nothing and returns the receipt already there.
    Reading twice is ordinary (wait, then list, then end the session), the first
    read is the fact worth keeping, and nothing here rewrites a record.
    """
    record = {
        "schema": SCHEMA_VERSION,
        "kind": "read",
        "question_id": question.id,
        "nonce": question.nonce,
        "read_at": utc_now_iso(),
        # Our own call sites supply this label, so it is clipped rather than
        # validated — a long one is a bug in a caller, not something to refuse a
        # receipt over.
        "via": str(via)[:_MAX_VIA_CHARS],
    }
    try:
        _write_new(directory, f"{question.id}{READ_SUFFIX}", record)
    except FileExistsError:
        existing = receipt_for(directory, question)
        # A receipt that is on disk and unusable cannot be replaced (the write is
        # exclusive), so the channel keeps reading as unread — the direction that
        # delivers again rather than the one that hides a reply.
        return existing if existing is not None else _receipt_from(record)
    return _receipt_from(record)


def _receipt_from(record: Mapping) -> Receipt:
    return Receipt(
        question_id=record["question_id"],
        read_at=record["read_at"],
        via=record["via"],
        nonce=record["nonce"],
    )


def is_answer_unread(entry: Entry) -> bool:
    """Whether *entry* holds an answer that nobody has handed to the agent.

    Closed does not exempt it. An answer that raced a close is still the
    operator's work (module docstring), and it is precisely the one no verb
    printed: ``lmer-ask close`` only hands over an answer it found before filing.
    """
    return entry.kind == KIND_QUESTION and entry.answered and not entry.answer_read


def unread_answers(directory: Path) -> list:
    """Questions answered on this channel whose answer has not been delivered.

    Oldest first. The one definition of "answered but never read", shared by
    ``lmer-ask list``'s marker and by ``lmer-end-session``'s refusal the way
    :func:`is_answerable` is shared — the layers agree by using the function.
    """
    return [entry for entry in read_entries(directory) if is_answer_unread(entry)]


def is_answerable(entry: Entry) -> bool:
    """Whether a reply could still land on *entry*.

    The one definition of "open", used by the CLI's ``list --open``, by the cap in
    :func:`post_question`, and by the platform's pending-question view — the
    layers agree about it the way they agree about a legal id
    (:func:`valid_entry_id`), by sharing the function rather than the rule.

    Answered or closed is not answerable. *Which* of the two it was is a separate
    question, and everything that presents an entry answers it the same way —
    answered first, because an answer that raced a close is still the operator's
    work (module docstring).
    """
    return (
        entry.kind == KIND_QUESTION
        and entry.answer is None
        and entry.closure is None
    )


def close_question(
    directory: Path,
    question: Entry,
    *,
    reason: str = "",
) -> Optional[Answer]:
    """Record that the session has stopped waiting for a reply to *question*.

    Returns ``None`` normally. Returns the :class:`Answer` — and files nothing —
    when the operator got in first: the caller asked to stop waiting for a reply
    that already exists, and handing it back is the only outcome that does not
    throw away what somebody typed.

    Closing an already-closed question raises :class:`FileExistsError`, like every
    other exclusive write here; the caller decides whether a second close is worth
    reporting (``lmer-ask close`` treats it as done).
    """
    answer = answer_for(directory, question)
    if answer is not None:
        return answer
    note = "" if not reason else _clean_text(reason, MAX_REASON_CHARS, "the reason")
    record = {
        "schema": SCHEMA_VERSION,
        "kind": "closed",
        "question_id": question.id,
        "nonce": question.nonce,
        "closed_at": utc_now_iso(),
        "reason": note,
    }
    _write_new(directory, f"{question.id}{CLOSED_SUFFIX}", record)
    return None


def read_entries(directory: Path, *, limit: int = MAX_ENTRIES) -> list:
    """Every question and note on the channel, oldest first, answers attached.

    Tolerant throughout: an unparseable entry is skipped, and an answer that
    does not match its question leaves the entry unanswered with ``problem`` set
    rather than raising — one broken pair must not empty the view.

    ``limit`` keeps the newest entries, because a channel long enough to hit the
    cap is one where the recent end is what matters.

    Signals are **not** in here (module docstring): this is the operator's feed,
    and a milestone addressed to the orchestrator would arrive in it as a card
    nobody can answer. :func:`read_signals` is that shape's reader.
    """
    found = _scan(directory, _OPERATOR_SUFFIXES, limit)

    attached = []
    for entry in found:
        if entry.kind != KIND_QUESTION:
            attached.append(entry)
            continue
        try:
            answer = answer_for(directory, entry)
        except AskError as exc:
            attached.append(replace(entry, problem=str(exc)))
            continue
        attached.append(_resolved(directory, entry, answer))
    return attached


def _scan(directory: Path, kinds: Mapping, limit: int) -> list:
    """Entries whose suffix is a key of *kinds*, oldest first, nothing attached.

    The one directory walk, shared by :func:`read_entries` and
    :func:`read_signals` so the two cannot disagree about what a readable entry
    is — tolerant of a file it cannot parse, of a name that is not ours, and of a
    record that contradicts its own filename, each costing that one entry.

    ``limit`` keeps the newest, for the reason :data:`MAX_ENTRIES` exists.
    """
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []

    found = []
    for name in names:
        parts = _entry_parts(name)
        if parts is None:
            continue
        seq, entry_id, suffix = parts
        kind = kinds.get(suffix)
        if kind is None:
            continue
        record = _read_json(directory / name)
        if record is None:
            continue
        entry = _entry_from_record(entry_id, seq, kind, record)
        if entry is None:
            continue
        found.append(entry)

    found.sort(key=lambda entry: entry.seq)
    return found[-limit:] if limit and len(found) > limit else found


def read_signals(directory: Path, *, limit: int = MAX_ENTRIES) -> list:
    """Milestones this session signalled, oldest first (T122).

    The daemon's reader (:mod:`lmer_platform.detect`), and separate from
    :func:`read_entries` because these are not the operator's business — see the
    module docstring. Nothing is attached to a signal: no answer, no closure, no
    receipt is ever filed under its id, so there is nothing to resolve.

    An absent or unreadable directory is an empty list, like every other read
    here: a session that never signalled and a channel that was never mounted are
    the same news to a caller that is looping over the fleet.
    """
    return _scan(directory, _SIGNAL_SUFFIXES, limit)


def _resolved(directory: Path, entry: Entry, answer: Optional[Answer]) -> Entry:
    """Attach everything filed under a question: answer, closure, read receipt.

    ``dataclasses.replace`` rather than a fresh :class:`Entry`, because a field
    added to the entry later would otherwise be silently dropped here — which is
    exactly how the answer would have gone missing when the closure was added.
    """
    return replace(
        entry,
        answer=answer,
        closure=closure_for(directory, entry),
        receipt=receipt_for(directory, entry),
    )


def read_entry(directory: Path, entry_id: str) -> Optional[Entry]:
    """One entry by id, with its answer and closure attached.

    ``None`` if there is no such entry — including a signal's id, which resolves
    to nothing here for the same reason signals are not in :func:`read_entries`:
    every caller of this function is looking for something the operator can act
    on, and a signal is neither answerable nor closable.
    """
    entry_id = _require_entry_id(entry_id)
    for suffix, kind in ((QUESTION_SUFFIX, KIND_QUESTION), (NOTE_SUFFIX, KIND_NOTE)):
        record = _read_json(directory / f"{entry_id}{suffix}")
        if record is None:
            continue
        entry = _entry_from_record(entry_id, int(entry_id), kind, record)
        if entry is None:
            return None
        if kind != KIND_QUESTION:
            return entry
        return _resolved(directory, entry, answer_for(directory, entry))
    return None


def open_questions(directory: Path) -> list:
    """Questions a reply could still land on, oldest first.

    Not "unanswered": a question the session closed is off this list, which is
    what frees its slot under :data:`MAX_OPEN_QUESTIONS` and what keeps it out of
    the operator's reply boxes. See :func:`is_answerable`.
    """
    return [entry for entry in read_entries(directory) if is_answerable(entry)]


def write_answer(
    directory: Path,
    question: Entry,
    text: str,
    *,
    source: str = "operator",
) -> Answer:
    """Record the operator's reply to *question*.

    Exclusive, like every other write here: an answer already on disk is not
    overwritten, because the session may already have read it and acted, and a
    second answer would be a decision nobody knows was changed. The caller turns
    that into whatever "already answered" means at its layer.
    """
    body = _clean_text(text, MAX_ANSWER_CHARS, "the answer")
    record = {
        "schema": SCHEMA_VERSION,
        "kind": "answer",
        "question_id": question.id,
        "nonce": question.nonce,
        "text": body,
        "answered_at": utc_now_iso(),
        "source": source,
    }
    _write_new(directory, f"{question.id}{ANSWER_SUFFIX}", record)
    return Answer(
        question_id=question.id,
        text=body,
        answered_at=record["answered_at"],
        source=source,
        nonce=question.nonce,
    )


def wait_for_answer(
    directory: Path,
    question: Entry,
    *,
    timeout: float,
    interval: float,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> Optional[Answer]:
    """Poll until *question* is answered. ``None`` when the timeout elapses.

    ``timeout <= 0`` waits indefinitely. The deadline is wall clock (monotonic),
    and at least one read always happens, so a question answered before the wait
    started returns immediately rather than costing an interval.

    A timeout is not a failure of the question: the entry stays on the channel
    and a later wait picks the answer up. That is why this returns ``None``
    instead of raising — the caller decides what a quiet operator means, and
    :func:`close_question` is how it says "not any more".

    A closure is not polled for. The only writer of one is the session, which is
    also the only waiter, so a wait that noticed a close would be noticing its own
    decision; the answer is the one thing that arrives from outside.
    """
    deadline = None if timeout <= 0 else monotonic() + timeout
    while True:
        answer = answer_for(directory, question)
        if answer is not None:
            return answer
        if deadline is None:
            sleep(interval)
            continue
        remaining = deadline - monotonic()
        if remaining <= 0:
            return None
        sleep(min(interval, remaining))
