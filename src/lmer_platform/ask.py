"""The platform's end of a live session's ask channel (issue #141, T23).

Not the same thing as :mod:`lmer_platform.answer`
-------------------------------------------------
That module answers a run that **stopped** on a question: the session recorded
``stop_reason=question`` and exited, so answering it means respawning the run
with the answer attached, and a container starts. This module answers a session
that is **still running** and is sitting in a poll loop waiting: the reply is a
file in a directory the container already has mounted, and nothing is spawned.

The two are deliberately kept apart everywhere they surface — different
attention reason (``live_question`` against ``question``), different routes,
different component, different words on the button — because conflating them
would put an operator one tap away from starting a container when they meant to
answer a session that was already waiting.

Why not the session's control plane
-----------------------------------
:func:`lmer_platform.session_io.send_input` can already write to a live session,
and it is the wrong instrument for a question: it writes raw bytes to the PTY, so
what happens to them depends on where the harness's focus is. If the session is
showing a permission dialog or an open ``/``-menu, an answer becomes keystrokes
in that menu, and nothing on the writing side can tell. A file the session polls
for cannot be misdelivered, which is the entire point of this channel.

What lives here and what lives in :mod:`ask_channel.protocol`
-------------------------------------------------------------
The format — filenames, id allocation, atomic writes, tolerant reads — is shared
with the container-side CLI and lives in ``ask_channel.protocol``. This module
owns only the host's questions about it: which directory belongs to which
session, how it is created before a container starts, and what each refusal
should mean over HTTP.

A question the session stopped waiting for
------------------------------------------
``lmer-ask close`` files a record beside the question, and this side reads it: the
question stays in the feed as a record while :class:`QuestionClosed` refuses a
reply to it. Nothing here ever *writes* one. A session that has exited is
recognised from the registry instead — :func:`pending_by_session` consults only
live sessions — which is both better evidence than a file a dying session may not
have managed to write and the only version that leaves a *detached* session's
questions answerable: detached is alive, and this channel is a mount rather than a
control plane (spec D23).

A session that is not there any more
------------------------------------
The channel is *one* session's directory, so once nothing is reading it nothing
ever will — and resuming does not change that, because the resumed run is a new
session with a channel of its own. So :func:`answer_question` refuses
(:class:`SessionGone`) instead of filing a reply into a directory with no reader
while the operator is told it was delivered, and the refusal points at resume as
the way to continue the run.

The test is "**is there a live reader**" rather than "did something die", and the
difference is the class of lie this channel used to tell. The reader is the
``lmer-ask`` poll running *inside* the container
(:func:`ask_channel.protocol.wait_for_answer`), so proving one takes three
answers and any one of them missing is a refusal (:func:`_reader_state`):

1. **a registry entry.** No entry is no session to read for: it is what a clean
   exit leaves behind (:mod:`lmer_platform.spawn` removes it) and what a channel
   prepared before its session was registered looks like, and a reply filed
   against either sits unread forever.
2. **a live pid.** An entry that is *there and dead* is the crashed session this
   channel already refused, unchanged.
3. **a control plane that answers**
   (:func:`lmer_platform.session_io.control_plane_answers`). The leg the registry
   cannot supply, and the one a live incident turned on: the host-side ``lmer``
   whose pid the entry carries stays alive through teardown — up to minutes of
   run-state commits — with the supervisor, the harness and the poll that was
   reading this directory already gone. Both of the other legs hold in that
   window, and the answer written in it is read by nobody while the route says
   200.

Which is also why that probe is on the *write* path and on no read path here.
:func:`pending_by_session` asks the registry only, because a per-row round trip
into every container would put the whole fleet view behind one container that
stopped answering — so a session in teardown can still *show* a question, and the
reply is refused at the tap with the draft kept. That is the trade this module
takes everywhere: refuse what cannot land rather than file it quietly.

A detached session keeps working, and that is a property of the third leg rather
than an exception to it. Detached means the *host PTY* is gone (spec D23,
:mod:`lmer_platform.reattach`), and a re-attached session is by construction one
whose control plane answers — that is how the re-attach found it, and over that
same plane its output is being read. The fear that reachability would turn a
daemon restart into a channel nobody can reply through has it backwards: a daemon
restart costs the log its writer, not the container its supervisor.

Ownership across the mount
--------------------------
The directory is created by the daemon (0700) and mounted rw, so the container
writes into it as the container user — the same uid as the daemon under rootless
podman's ``--userns=keep-id``, which is what makes the mode workable at all (see
``lmer_cli.mounts.build_dir_mounts``). Neither side rewrites the other's files,
so nothing here depends on being able to. Reads are tolerant of a file this user
cannot even open, for the runtimes where that assumption does not hold.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional

from ask_channel import protocol
from ask_channel.protocol import (
    ANSWER_SUFFIX,
    ASK_DIR_ENV,
    CONTAINER_ASK_DIR,
    DIR_MODE,
    MAX_ANSWER_CHARS,
    AskError,
    Entry,
)

from . import registry, runs
from .store import append_event, logs_dir
from . import store

logger = logging.getLogger("lmer_platform.ask")

__all__ = [
    "ASK_DIR_ENV", "CONTAINER_ASK_DIR", "DIR_MODE", "ANSWER_SUFFIX",
    "MAX_ANSWER_CHARS", "SESSION_DIR_SUFFIX",
    "AskChannelError", "ChannelNotFound", "QuestionNotFound", "AlreadyAnswered",
    "QuestionClosed", "SessionGone",
    "READER_LIVE", "READER_UNREGISTERED", "READER_DEAD", "READER_SHUTTING_DOWN",
    "session_ask_dir", "prepare_ask_dir", "read_entries", "pending_questions",
    "pending_by_session", "answer_question",
]

#: Suffix of the per-session channel directory, beside the PTY log and the
#: transcript directory in ``logs/``. Derived from the session id, so resolving a
#: session's channel needs no recorded state — the same property that makes
#: :func:`lmer_platform.transcripts.session_transcript_dir` a mount and nothing
#: else.
SESSION_DIR_SUFFIX = ".ask"


class AskChannelError(RuntimeError):
    """Base refusal, carrying the HTTP status a route should answer.

    The status rides on the exception exactly as in
    :mod:`lmer_platform.session_io`: one handler per route, and a refusal added
    later arrives with a code instead of a 500.
    """

    status = 400


class ChannelNotFound(AskChannelError):
    """This session has no channel directory — it was not spawned with one."""

    status = 404


class QuestionNotFound(AskChannelError):
    """No such question on this session's channel."""

    status = 404


class AlreadyAnswered(AskChannelError):
    """The question already has an answer, and answers are not overwritten.

    409 rather than 400: the request was well-formed and would have been fine a
    moment earlier. Two operators (or one double-tap) racing on the same question
    is the case this makes harmless — the first answer stands, and the second
    caller is told so instead of quietly replacing a reply the session may
    already have acted on.
    """

    status = 409


class QuestionClosed(AskChannelError):
    """The session stopped waiting for this one, so a reply would reach nobody.

    409 for the same reason as :class:`AlreadyAnswered`: the request was
    well-formed and would have been fine a moment earlier. Refused rather than
    accepted-and-filed, because a reply written into a channel nothing is waiting
    on is worse than a refusal — the operator would be told their answer was
    delivered. The refusal reaches them with their text still in the box.
    """

    status = 409


class SessionGone(AskChannelError):
    """Nothing is reading this channel any more: no reply can land.

    410 rather than :class:`QuestionClosed`'s 409, because the two are different
    news. Closed means "would have been fine a moment earlier" on a session that
    is still up and could ask again; this one is permanent — a channel belongs to
    exactly one session (the directory is named after its id and mounted into that
    one container), so nothing started later inherits it and there is no version of
    "try again" that delivers this reply. Hence a message that points at resume
    instead: continuing the run is the thing the operator can still do.

    One status for every missing reader (:func:`_reader_state`), and different
    words for each, because the operator's next move is the same in all of them and
    only their reading of *what happened* differs. A session still tearing down is
    the case that most invites a retry, so its wording says outright that waiting
    changes nothing: the harness that was polling is what has gone, and the host
    process finishing its commits is not going to start reading for it.
    """

    status = 410


def _validated(session_id: str) -> str:
    """Reject an id that could not name a session before it reaches a path.

    Borrowed from the registry rather than written again, as
    :mod:`lmer_platform.transcripts` does: two different notions of a legal
    session id is how ``..`` eventually gets past one of them.
    """
    try:
        registry.session_path(session_id)
    except registry.RegistryError as exc:
        raise ChannelNotFound(str(exc)) from exc
    return session_id


def session_ask_dir(session_id: str) -> Path:
    """Host directory holding one session's channel."""
    return logs_dir() / f"{_validated(session_id)}{SESSION_DIR_SUFFIX}"


def prepare_ask_dir(session_id: str) -> Optional[Path]:
    """Create the channel directory 0700. ``None`` when that failed.

    Created with the mode rather than chmod'ed into it (``os.mkdir`` applies
    ``mode & ~umask``, so it is never *wider* than 0700 even for an instant),
    then chmod'ed so the mode is exact under any umask and a directory that
    somehow already existed is corrected.

    Fail-soft, like the transcript directory: a session without a channel is the
    pre-T23 status quo — it simply has no way to ask, and ``lmer-ask`` says so
    rather than hanging — which is not worth refusing to start a session over.
    """
    directory = session_ask_dir(session_id)
    try:
        # The parent through the store: mkdir(mode=...) is leaf-only, and the
        # ask root was taking the umask around 0700 leaves (T93 finding).
        store.ensure_state_dir(directory.parent)
        directory.mkdir(mode=DIR_MODE, exist_ok=True)
        directory.chmod(DIR_MODE)
    except OSError as exc:
        logger.warning(
            "platform_ask_dir_unusable id=%s path=%s error=%s — the session "
            "runs without an operator channel; lmer-ask will refuse rather "
            "than hang",
            session_id, directory, exc,
        )
        return None
    return directory


def read_entries(session_id: str) -> list:
    """Everything on one session's channel, oldest first, answers attached.

    An absent directory is an empty channel, not an error: most sessions never
    ask anything, and a session spawned before this existed has no directory at
    all.
    """
    directory = session_ask_dir(session_id)
    if not directory.is_dir():
        return []
    return protocol.read_entries(directory)


def pending_questions(session_id: str) -> list:
    """Questions this session is still waiting on, oldest first.

    "Still waiting" is the channel format's own definition
    (:func:`ask_channel.protocol.is_answerable`), so a question the session closed
    leaves the operator's attention list here rather than in each of the three
    views that render it.
    """
    return [
        entry for entry in read_entries(session_id)
        if protocol.is_answerable(entry)
    ]


def pending_by_session(sessions: Iterable[dict]) -> dict:
    """Map live session ids to their unanswered questions, for the fleet view.

    Only **live** sessions are consulted. A question from a session that has
    exited is not answerable — nothing is polling for the reply — and surfacing
    it as something the operator can act on would be a lie; the crashed-run
    attention record is what covers that case.

    Every read is best-effort: one session's unreadable channel must not cost
    the whole fleet view, which is the same bargain :mod:`lmer_platform.store`
    makes for reads.
    """
    pending: dict = {}
    for entry in sessions:
        if not isinstance(entry, dict) or not entry.get("live"):
            continue
        session_id = entry.get("id")
        if not isinstance(session_id, str) or not session_id:
            continue
        try:
            questions = pending_questions(session_id)
        except (AskChannelError, AskError, OSError) as exc:
            logger.warning(
                "platform_ask_channel_unreadable id=%s error=%s", session_id, exc
            )
            continue
        if questions:
            pending[session_id] = [question.to_dict() for question in questions]
    return pending


#: :func:`_reader_state`: an entry, a live pid, and a container that answered.
#: The only value an answer is written under.
READER_LIVE = "live"

#: :func:`_reader_state`: no registry entry at all. A clean exit removes it, so
#: this is what the *ordinary* end of a session looks like from here.
READER_UNREGISTERED = "unregistered"

#: :func:`_reader_state`: the entry is there and its pid is not. The crash signal
#: (:mod:`lmer_platform.inventory` reads the same fact).
READER_DEAD = "dead"

#: :func:`_reader_state`: the pid is alive and the container is not answering —
#: the teardown window, where the host-side ``lmer`` is still committing run state
#: for a session whose harness has already gone.
READER_SHUTTING_DOWN = "shutting_down"


def _session_io():
    """The :mod:`lmer_platform.session_io` module, imported on demand.

    Inside a function rather than at module scope because
    :mod:`lmer_platform.spawn` imports *this* module and ``session_io`` imports
    ``spawn`` — a top-level import here would close that cycle. Same lazy-import
    shape, and same one named seam, as ``lmer_platform.spawn._transcripts``.
    """
    from . import session_io

    return session_io


def _reader_state(session_id: str) -> str:
    """Whether anything is reading this channel, and if not, what happened.

    The three legs are in the module docstring, and the order they are asked in is
    the point of this function: each one is cheaper than the next and each one
    makes the next meaningless. A missing entry has nothing to probe; a dead pid
    has no container left to answer, and asking anyway would spend a round trip
    (and up to :data:`lmer_platform.session_io.ACTIVITY_TIMEOUT_SECONDS`) to be
    told what the registry just said.

    So only a session that is registered *and* alive is asked whether its control
    plane answers — which is the leg that closes the teardown window, where the
    other two say yes for the minutes a host-side ``lmer`` spends finishing its
    run-state commits after its container is gone.

    Reachability is read in one direction only (see
    :func:`lmer_platform.session_io.control_plane_answers`): an answer proves a
    container, no answer proves nothing except that no reader can be *shown*. That
    asymmetry is the reason this decides a refusal rather than a delivery — the
    reply is kept in the operator's hands either way, and the failure worth
    avoiding is the other one, where they are told it landed.
    """
    try:
        entry = registry.read_session(session_id)
    except registry.RegistryError:  # pragma: no cover - the id is validated above
        return READER_UNREGISTERED
    if entry is None:
        return READER_UNREGISTERED
    if not registry.is_live(entry):
        return READER_DEAD
    if not _session_io().control_plane_answers(session_id):
        return READER_SHUTTING_DOWN
    return READER_LIVE


def _run_ref(session_id: str) -> Optional[str]:
    """The run key on this session's entry, for a refusal to name. ``None`` if any.

    :func:`lmer_platform.runs.run_key` rather than a format string, so what an
    operator is told to resume is spelled exactly as the routes and the index spell
    it. A *path* would be the other option and is never used in these messages: a
    host filesystem layout is not an answer to "which run", and the run key is what
    ``/api/runs/resume`` takes.
    """
    entry = registry.read_session(session_id)
    run = entry.get("run") if isinstance(entry, dict) else None
    if not isinstance(run, dict):
        return None
    try:
        return runs.run_key(
            run.get("host") or "", run.get("project") or "", run.get("slug") or ""
        )
    except runs.RunIndexError:
        # A session with no run identity yet — spawned, nothing committed. The
        # refusal simply names one less thing.
        return None


def _no_reader_message(session_id: str, question_id: str, state: str) -> str:
    """The refusal for a channel with no reader, worded for what happened.

    All three end in the same advice because there is only one thing left to do,
    and none of them offers a retry: a channel belongs to one session, so a reply
    to *this* question has nowhere to be delivered later.
    """
    run = _run_ref(session_id)
    resume = (
        f"Resume the run ({run}) to continue it" if run
        else "Resume the run to continue it"
    )
    if state == READER_SHUTTING_DOWN:
        return (
            f"session {session_id} is shutting down — its container stopped "
            f"answering, so the poll that was waiting on question {question_id} is "
            "already gone even though the host-side process is still finishing the "
            "session's bookkeeping. Waiting will not help: nothing starts reading "
            f"this channel again. {resume}; the resumed session asks on a channel "
            "of its own."
        )
    if state == READER_UNREGISTERED:
        return (
            f"session {session_id} is over — it is no longer registered on this "
            f"host, so nothing is reading its ask channel and a reply to question "
            f"{question_id} would reach nobody. {resume}; the resumed session asks "
            "on a channel of its own."
        )
    return (
        f"session {session_id} has exited — nothing is reading its ask channel "
        f"any more, so a reply to question {question_id} would reach nobody. "
        f"{resume}; the resumed session asks on a channel of its own."
    )


def _require_question(directory: Path, question_id: str) -> Entry:
    """The question *question_id* names, or the refusal that says why not."""
    if not protocol.valid_entry_id(question_id):
        # The id arrives as a URL path segment, so this is reachable input rather
        # than a caller mistake, and it is refused at this boundary — before it
        # is joined to a path — rather than on the strength of the protocol
        # layer's identical check. Two layers, one validator
        # (:func:`ask_channel.protocol.valid_entry_id`), so they cannot disagree
        # about what a legal id is; the message is this layer's, because it is
        # the one an operator sees.
        raise AskChannelError(
            f"invalid question id {question_id!r}: expected digits only"
        )
    try:
        entry = protocol.read_entry(directory, question_id)
    except AskError as exc:
        raise AskChannelError(str(exc)) from exc
    if entry is None or entry.kind != protocol.KIND_QUESTION:
        raise QuestionNotFound(
            f"no question {question_id} on this session's channel"
        )
    return entry


def answer_question(
    session_id: str,
    question_id: str,
    text: str,
    *,
    source: str = "operator",
) -> dict:
    """Record the operator's reply to one live session's question.

    Returns the answer as a dict. The session's own poll picks the file up —
    nothing is signalled, and nothing needs to be: the container is already
    reading this directory, and a channel that also required a signal would stop
    working the moment a session was busy when the answer landed.

    Which is exactly why a reply is only ever written where that poll can be shown
    to still exist (:func:`_reader_state`): with nothing to signal, a channel with
    no reader accepts an answer as readily as a channel with one, and the operator
    would be told it was delivered. Every refusal here leaves their text with them.
    """
    directory = session_ask_dir(session_id)
    if not directory.is_dir():
        raise ChannelNotFound(
            f"session {session_id} has no ask channel on this host — it was "
            "started without one, so it cannot be waiting for an answer"
        )
    question = _require_question(directory, question_id)
    if question.answer is not None:
        raise AlreadyAnswered(
            f"question {question_id} was already answered at "
            f"{question.answer.answered_at or 'an unknown time'}"
        )
    if question.closure is not None:
        # Answered is checked first, deliberately: an answer that raced a close
        # stands (``ask_channel.protocol``), so this branch only ever refuses a
        # reply to a question with no answer at all.
        when = question.closure.closed_at or "an unknown time"
        why = f" ({question.closure.reason})" if question.closure.reason else ""
        raise QuestionClosed(
            f"question {question_id} was closed by the session at {when}{why} — "
            "it stopped waiting for a reply, so nothing would read one"
        )
    reader = _reader_state(session_id)
    if reader != READER_LIVE:
        # Last of the three refusals, after both records the session wrote about
        # *this question*. Those are the truer answer for it even once the reader is
        # gone: an already-answered question says answered, because that reply was
        # real and the session may have acted on it before it died, and a closed one
        # names the reason the session gave — more use to an operator than a second
        # way of hearing that nothing is listening. What is left here is the case
        # with no record at all, which is the one the operator cannot see coming.
        #
        # Also the last thing done before the write, because it is the only check
        # here that costs a round trip: an id that cannot name a question, or a
        # question that already has an answer, is refused without asking a container
        # anything.
        raise SessionGone(_no_reader_message(session_id, question_id, reader))
    try:
        answer = protocol.write_answer(directory, question, text, source=source)
    except FileExistsError as exc:
        # Lost the race with another answer between the read above and the link.
        raise AlreadyAnswered(
            f"question {question_id} was answered by someone else just now"
        ) from exc
    except AskError as exc:
        raise AskChannelError(str(exc)) from exc

    # Ids only, here and in the log line: the answer is content, and the platform
    # keeps content out of its history for the same reason
    # :mod:`lmer_platform.answer` does — the reply lives in the channel, and a
    # second copy in an append-only file nobody prunes is a copy that outlives
    # the run.
    append_event(
        "session_question_answered",
        note=session_id,
        data={"session": session_id, "question": question_id},
    )
    logger.info(
        "platform_ask_answered session=%s question=%s", session_id, question_id
    )
    return answer.to_dict()
