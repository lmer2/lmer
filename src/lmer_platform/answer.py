"""Answering a run that stopped to ask a question (issue #141, slice M2 / T19).

Answering is a spawn, not a message
-----------------------------------
A run that stops on a question records ``stop_reason=question``, pushes its state
and **exits** — that is the run-state contract (spec D15), and it is why the fleet
view can show a blocked run at all. So there is no session to type into: the one
route that looks like it should do this job, ``POST /api/sessions/{id}/input``,
requires a live control plane and would rightly refuse.

The delivery mechanism already exists and belongs to ``lmer``: a session launched
with ``--answer`` carries the text into the container as ``LMER_ANSWER``, and
``work session-start`` applies it before rendering the resume brief — appending
``question_answered``, clearing the question stop, and leading the brief with the
question/answer pair. Answering therefore *is* respawning the run with the answer
attached, and this module's whole job is to decide whether that respawn will land
on the run in front of the operator, and to refuse in plain words when it would
not.

The platform never writes run state (spec D3)
---------------------------------------------
Not ``work answer``, not ``state.yaml``, and nothing inside the mirror — which is
a read-only clone the daemon force-resets on every pull, so a write there would
be silently reverted rather than merely misplaced. The *session* records the
answer, in the container, as it always has. Everything here is a read plus one
spawn.

Why the answer is a typed field and not ``extra_args``
------------------------------------------------------
``extra_args`` is what an HTTP caller fills on ``POST /api/sessions``, so anything
that reaches a spawn through it is reachable input — and every refusal in this
module would be bypassed by a caller who simply spelled ``--answer`` there. That
is why ``--answer`` is in :data:`lmer_platform.spawn._RESERVED_ARGS` and the
answer rides on :attr:`~lmer_platform.spawn.SpawnRequest.answer` instead: the
field is set by this module alone, after the checks below, and the flag itself is
spelled by :func:`lmer_platform.spawn._build_command` as one
``--answer=<text>`` token.

One token rather than two, and that is load-bearing: ``lmer`` parses with
argparse, so ``--answer -yes`` exits 2 with "expected one argument" — the value
looks like an option. ``--answer=-yes`` parses. Answers that begin with a dash are
ordinary ("-1 is fine", "--force is what I meant"), and the ``=`` form is the only
spelling that cannot lose one.

The answer is content, not a credential channel
-----------------------------------------------
It is argv on the host and an environment variable in the container, so it is
readable by any local user via ``ps``. That is inherent to ``lmer --answer`` and
not something this module can improve on; what it *can* do is not add copies. So
the text is never logged, never put in the platform's event history, and dropped
from the API's reply (see :meth:`AnswerResult.to_dict`) — the same reasoning that
keeps the control token out of argv in :mod:`lmer_platform.spawn`.

What is refused, and why refusing beats a hopeful spawn
------------------------------------------------------
Every check below exists because the alternative is a container that starts, costs
a slot, and leaves the run exactly as blocked as it was — with the operator
believing they answered it. In rough order of how surprising they are:

- **A live session for the run.** Liveness outranks committed run state (spec
  D24): the mirror can still say "question" while a session is already working.
  Answering would start a second container for one run, and the two would fight
  over the run's owner claim. It also makes a double-tapped answer button harmless.
- **A respawn that would land on a different run.** The container derives its own
  run dir from ``taskdef``/``target``, so the pair the platform respawns with has
  to reproduce the run's recorded slug. Otherwise the answer is applied to a
  question a *different* (probably brand new) run never asked.
- **A run this orchestrator does not track**, whose dir is missing from the
  mirror, or whose state cannot be read: scope comes from the local index (spec
  D25), and everything above needs the state file to decide anything at all.
- **A blank or oversized answer.** ``lmer`` maps an empty ``--answer`` to no
  ``LMER_ANSWER``, and the container strips before testing for content, so
  whitespace is as empty as "". The ceiling keeps a paste from becoming an opaque
  ``E2BIG`` from ``execve`` instead of a sentence.

What is deliberately **not** refused: a question stop that recorded no text
(``stop_reason=question`` with no ``open_question``). That was a refusal until
T24, because ``work session-start`` only applied ``LMER_ANSWER`` alongside a
recorded question and would otherwise drop it. It now applies an answer to the
stop itself — the ``question_answered`` event carries a null question, the stop
clears, and the brief leads with the answer — so the respawn lands, and refusing
it would only keep the commonest question stop unanswerable. The UI still says the
text was not recorded: that is context for whoever writes the answer, not a reason
to withhold it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Optional

from work_repo import run_state

from . import runs
from .config import PlatformConfig
# ANSWER_FLAG is spelled and refused in spawn.py (it owns both ends of it) and
# re-exported here, which is where a reader of this feature looks for it.
from .spawn import (
    ANSWER_FLAG,
    SpawnRequest,
    SpawnResult,
    live_session_for_run,
    spawn_session,
)
from .store import StoreError, append_event
from .workrepo import RunDirRef, resolve_run_dir

logger = logging.getLogger("lmer_platform.answer")

__all__ = [
    "AnswerError", "RunNotTracked", "NotAnswerable", "AnswerRequest",
    "AnswerResult", "ANSWER_FLAG", "MAX_ANSWER_CHARS", "answer_run",
]

#: Ceiling on one answer, in characters. Linux caps a single ``execve`` argument
#: at 128 KiB (``MAX_ARG_STRLEN``), and this leaves room for four bytes per
#: character so even all-astral text stays under it. Far above any prose answer,
#: which is the point: the limit exists to turn a pasted log file into a sentence
#: an operator can act on rather than "Argument list too long" from a failed
#: spawn.
MAX_ANSWER_CHARS = 16384

#: Statuses that mean the run is over — the members of ``run_state.STATUSES``
#: that are not ``in-progress``. A run in one of these is ``complete`` in the fleet
#: view with no attention record, so the UI never offers an answer for it and the
#: API must not accept one either: ``answer_question`` deliberately leaves
#: ``status`` alone, so the respawned session would arrive holding the answer and
#: the completed-run directive telling it not to act on anything.
_FINISHED_STATUSES = ("complete", "archived")

#: Identity components are joined into a filesystem path under the mirror
#: (:func:`lmer_platform.workrepo.resolve_run_dir`), so their segments are checked
#: rather than trusted. ``project`` legitimately carries slashes
#: (``gh/owner/repo``), which is why this rejects bad *segments* instead of
#: banning the separator.
_UNSAFE_SEGMENTS = frozenset({"", ".", ".."})


class AnswerError(RuntimeError):
    """Base for every refusal here, carrying the status a route should answer.

    The status rides on the exception, as in :mod:`lmer_platform.session_io`: the
    route then needs one handler, and a refusal added later arrives with a code
    instead of falling through to a 500 with a traceback. The base itself is the
    request-is-unusable case.
    """

    status = 400


class RunNotTracked(AnswerError):
    """The run is not in this orchestrator's index, so it is not ours to answer."""

    status = 404


class NotAnswerable(AnswerError):
    """The run exists but its state does not permit an answer right now.

    409 rather than 400: the request is well-formed and the same request may
    succeed later (once the mirror catches up, or the live session stops).
    """

    status = 409


@dataclass(frozen=True)
class AnswerRequest:
    """Which run to answer, and with what."""

    host: str
    project: str
    slug: str
    answer: str

    def validate(self) -> "AnswerRequest":
        """Return a normalised copy, or raise :class:`AnswerError`.

        Normalising rather than merely checking, because the identity is used
        twice with different tolerances — :func:`lmer_platform.runs.run_key`
        strips, a filesystem path does not — and a value that matched the index
        but not the mirror would surface as a baffling "not in the mirror".

        The answer is stripped for the same reason: the container strips before
        applying, so the stripped text is what will be recorded, and it is what
        the length and emptiness checks must therefore be about.
        """
        fields = {}
        for name in ("host", "project", "slug"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise AnswerError(f"{name} is required")
            fields[name] = value.strip()
        _reject_traversal(fields)

        if not isinstance(self.answer, str):
            raise AnswerError(
                f"answer must be a string, got {type(self.answer).__name__}"
            )
        answer = self.answer.strip()
        if not answer:
            raise AnswerError(
                "answer is empty — an empty or whitespace-only answer is dropped "
                "by the session that would apply it, so nothing would reach the run"
            )
        if len(answer) > MAX_ANSWER_CHARS:
            raise AnswerError(
                f"answer is {len(answer)} characters, over the {MAX_ANSWER_CHARS} "
                "limit — it is passed to the session as one command-line argument, "
                "and a longer one fails the spawn itself"
            )
        return replace(self, answer=answer, **fields)

    @property
    def key(self) -> str:
        """The run's identity, ``host/project/slug``, for messages and event notes.

        Not a path, which this used to compose as ``…/runs/<slug>``: a named run's
        directory is ``runs/<slug>--<name>`` (see
        :func:`lmer_platform.workrepo.resolve_run_dir`), so a refusal built from the
        request alone would quote a directory that does not exist — for exactly the
        runs whose refusals are already the hardest to read. The key is what the
        operator tracked the run under and what a corrected second request has to
        repeat, so it is what a refusal names. Once the real directory is known the
        message names *that* instead (:attr:`lmer_platform.workrepo.RunDirRef.
        rel_path`), which is a path because by then it has been found.
        """
        return runs.run_key(self.host, self.project, self.slug)


@dataclass(frozen=True)
class AnswerResult:
    """The session started to deliver one answer.

    ``question`` is ``None`` for a question stop that never recorded its text —
    answerable since T24, and the null is how a client tells "it asked nothing in
    writing" from a question it can echo.
    """

    host: str
    project: str
    slug: str
    question: Optional[str]
    session: SpawnResult

    def to_dict(self) -> dict:
        """Wire form, deliberately **without** the spawn's ``command``.

        :meth:`lmer_platform.spawn.SpawnResult.to_dict` echoes the argv it
        launched, and this argv carries ``--answer=<text>``. An answer is whatever
        the operator was asked for, so it does not travel back out in a response
        body — same trade as keeping the control token in the environment rather
        than in argv. The *question* is echoed: the client already has it, and
        repeating it is what makes the reply readable in a log or a CLI.
        """
        return {
            "run": {"host": self.host, "project": self.project, "slug": self.slug},
            "question": self.question,
            "session": {
                "session_id": self.session.session_id,
                "pid": self.session.pid,
                "control": {
                    "host": self.session.control_host,
                    "port": self.session.control_port,
                },
            },
            "note": (
                "a fresh session was started with the answer attached — it records "
                "the answer and clears the question stop at its own session start, "
                "so the run's state catches up a moment later"
            ),
        }


def _reject_traversal(fields: dict) -> None:
    """Refuse an identity that would not stay under the mirror.

    ``host``/``project``/``slug`` come off an HTTP body and become path segments.
    A ``..`` among them would make :func:`lmer_platform.workrepo.resolve_run_dir`
    look outside the mirror, and this is the first verb keyed on that identity
    that *starts something* — so the check lives at the point where the
    consequence appears rather than being assumed of the caller.
    """
    for name, value in fields.items():
        segments = value.replace("\\", "/").split("/")
        if name != "project" and len(segments) > 1:
            raise AnswerError(f"{name} must not contain a path separator: {value!r}")
        if any(segment in _UNSAFE_SEGMENTS for segment in segments):
            raise AnswerError(f"{name} is not a usable path: {value!r}")


def _require_tracked(request: AnswerRequest) -> runs.TrackedRun:
    """The tracked entry for the run, or refuse.

    Scope is the local index (spec D25): a run this orchestrator neither spawned
    nor adopted may well be a colleague's, and starting a container for it is not
    a decision to make on their behalf.
    """
    entry = runs.get_tracked(request.host, request.project, request.slug)
    if entry is None:
        raise RunNotTracked(
            f"{request.key} is not tracked by this orchestrator — adopt it "
            "first (POST /api/runs/adopt) so it is in scope, then answer it"
        )
    return entry


def _live_session_for(request: AnswerRequest, slug: str) -> Optional[dict]:
    """A live registry entry filed under this run identity, if one exists.

    The comparison itself belongs to :func:`lmer_platform.spawn.live_session_for_run`,
    which is where the one-run-one-session invariant reads it: a second matcher
    here could answer "no" to a question the spawn then answers "yes" to, and the
    two would differ over exactly the run whose identity is unusual.
    """
    return live_session_for_run(request.host, request.project, slug)


def _refuse_if_live(request: AnswerRequest, slug: str) -> None:
    """Refuse to answer a run that already has a session running.

    Not the enforcement — :func:`lmer_platform.spawn.spawn_session` refuses this
    for every spawn path and cannot be got round (see
    :func:`lmer_platform.spawn._refuse_if_run_is_live`). This is the *answer
    path's* version of the same refusal, kept for the two things it can say that
    the floor underneath cannot:

    - it fires before a container is even considered, and before anything is read
      from the mirror — the cheapest check, and the one whose answer overrides what
      the mirror says, since committed run state lags a live session (spec D24) and
      a row can still read "waiting on you" while a session works on it;
    - it is an :class:`AnswerError`, so it reaches the operator as this route's own
      409 with the answer-shaped wording (the answer is *not* delivered, type into
      the live session instead) rather than as a generic spawn refusal.

    It takes the slug to match rather than using the request's, because a session
    files itself under the run's *recorded* slug while a renamed run dir is tracked
    under its directory name. Those differ exactly when a run has been renamed, and
    a check that only knew the tracked name would let a second container start for
    one of those — the case the invariant underneath cannot see either, since it
    only knows the identity the spawn derives.
    """
    entry = _live_session_for(request, slug)
    if entry is None:
        return
    raise NotAnswerable(
        # The session's own identity, not the request's key: this refusal can be
        # about the recorded slug (see above), and it is a key either way — a
        # directory composed from it would be the wrong name for a named run.
        f"{runs.run_key(request.host, request.project, slug)} already has a live "
        f"session ({entry.get('id')}, pid {entry.get('pid')}) — answering would "
        "start a second container for one run. Type into that session instead "
        f"(POST /api/sessions/{entry.get('id')}/input), or wait for it to stop"
    )


def _require_run_dir(config: PlatformConfig, request: AnswerRequest) -> RunDirRef:
    """The run's directory in the host mirror, or refuse.

    Without it there is no way to tell whether a question is pending, and no way
    to recover the taskdef/target a respawn needs — so this is a refusal rather
    than a spawn made on assumptions.
    """
    ref = resolve_run_dir(config, request.host, request.project, request.slug)
    if ref is None:
        raise NotAnswerable(
            f"{request.key} is not in the host mirror, so the platform cannot "
            "confirm what the run is waiting for — it may not have been pushed "
            "yet, the mirror may be stale (rescan to refresh), or the run dir may "
            "have been renamed since it was tracked"
        )
    return ref


def _load_state(ref: RunDirRef) -> dict:
    """The run's recorded state, or refuse.

    Unreadable state is a refusal rather than a warning: every decision below is
    made from this file, and guessing at any of them is how an answer ends up on
    the wrong run.

    ``None`` — a state file that is not there — gets the same treatment and is not
    a theoretical case: the mirror is ``git reset --hard``ed by every pull, which
    the fleet view triggers on its poll interval, so the file genuinely can vanish
    between resolving the run dir and reading it. Without this branch that race is
    an ``AttributeError`` and a 500.
    """
    try:
        state = run_state.load_state(ref.path)
    except (run_state.RunStateError, OSError) as exc:
        raise NotAnswerable(
            f"{ref.rel_path}: run state could not be read ({exc}) — nothing about "
            "this run can be confirmed, so it is not answerable from here"
        ) from exc
    if not isinstance(state, dict):
        raise NotAnswerable(
            f"{ref.rel_path} has no readable run state — a run that never recorded "
            "any cannot have recorded a question"
        )
    return state


def _refuse_if_finished(state: dict, ref: RunDirRef) -> None:
    """Refuse to answer a run that is already over.

    A terminal status outranks a stale question stop everywhere else (that is what
    :func:`lmer_platform.inventory._derive` does with it), so this keeps the API
    from accepting what the UI would never offer. Reopening a finished run is a
    deliberate act with its own contract, not a side effect of typing an answer.
    """
    status = state.get("status")
    if status in _FINISHED_STATUSES:
        raise NotAnswerable(
            f"{ref.rel_path} is {status}, so answering it would start a session "
            "that is told not to work — reopening a finished run is its own "
            "decision, not an answer"
        )


def _require_question_stop(state: dict, ref: RunDirRef) -> Optional[str]:
    """The question the answer will be attached to, or refuse.

    One refusal: a run that is not stopped on a question has nothing an answer
    resolves, so a respawn would carry the text into a container that ignores it.

    The *text* is not required (T24). ``work session-start`` keys on the question
    stop, not on ``open_question``, so a bare ``stop_reason=question`` — the shape
    most runs stop in — is answered exactly like a recorded one, minus the question
    half of the event. ``None`` comes back for it, and travels out as the reply's
    ``question`` so a client can tell "no text was recorded" from a text.
    """
    stop_reason = state.get("stop_reason")
    if stop_reason != "question":
        raise NotAnswerable(
            f"{ref.rel_path} is not stopped on a question (stop_reason="
            f"{stop_reason!r}, status={state.get('status')!r}) — there is nothing "
            "for an answer to be applied to"
        )
    question = state.get("open_question")
    if not isinstance(question, str) or not question.strip():
        return None
    return question


def _respawn_inputs(
    state: dict, tracked: runs.TrackedRun, ref: RunDirRef
) -> tuple:
    """The ``(taskdef, target)`` a respawn has to carry, or refuse.

    The run's own state wins over the tracked index: ``run_state.seed_state``
    makes both immutable for the life of the run, and they are exactly what the
    container derives its run dir from — so the recorded pair is the one that
    lands back on this run. The index is the fallback for state that predates them
    or was hand-edited.

    An absent target is a refusal rather than a bare ``lmer <taskdef>``: the slug
    of a target-less run is the taskdef alone, which any other target-less run of
    the same taskdef shares, and :meth:`SpawnRequest.validate` refuses one anyway.
    """
    taskdef = state.get("taskdef") or tracked.taskdef
    target = state.get("target") or tracked.target
    missing = [
        name
        for name, value in (("taskdef", taskdef), ("target", target))
        if not (isinstance(value, str) and value.strip())
    ]
    if missing:
        raise NotAnswerable(
            f"{ref.rel_path} records no {' and no '.join(missing)}, so the platform "
            "cannot work out what to respawn — answer it from a shell with "
            '`lmer <taskdef> <target> --answer="…"`'
        )
    return taskdef.strip(), target.strip()


def _require_same_run(
    taskdef: str, target: str, state: dict, ref: RunDirRef
) -> str:
    """Refuse a respawn that would file itself as a different run; return the slug.

    The slug it returns is the identity the answering session will register under,
    which is not always the one the operator asked about — see
    :func:`_refuse_if_live`.

    The container computes its own run dir from ``LMER_TASK``/``LMER_TASK_TARGET``
    through ``run_state.derive_slug`` and then resolves it by **recorded slug**,
    not by directory name — which is what lets a renamed run dir still be found.
    So the comparison is against the slug in the state file, falling back to the
    directory name only when the state records none (the one case where nothing
    but the name can resolve it).

    A mismatch means the fresh session would seed or adopt some other run and
    apply the answer to a question that run never asked, i.e. not at all — while
    the run the operator answered stays blocked.
    """
    recorded = state.get("slug")
    if not (isinstance(recorded, str) and recorded.strip()):
        recorded = ref.slug
    recorded = recorded.strip()
    derived = run_state.derive_slug(taskdef, target)
    if derived != recorded:
        raise NotAnswerable(
            f"{ref.rel_path}: respawning with taskdef {taskdef!r} and target "
            f"{target!r} derives run {derived!r}, not {recorded!r} — the answer "
            "would land on a different run, so it is refused"
        )
    return recorded


def _identity_repo_url(request: AnswerRequest) -> str:
    """A URL that parses back to the run's own ``host``/``project``.

    ``repo_url`` never reaches the container (the target is what the clone is
    derived from); :func:`lmer_platform.spawn.derive_run_identity` uses it to
    predict the run's ``host``/``project``, and getting that wrong would register
    the answering session with no run identity — a second, host-less row in the
    fleet view and a detail view still pointing at the session that asked.

    A run adopted rather than spawned has no recorded URL, so the HTTPS spelling
    of the host and project it is already filed under is reconstructed. That is a
    round trip by construction rather than a guess: ``_parse_repo_url`` of it
    yields back exactly the identity it was built from.

    It is **not** a claim about how the operator clones the repo, so it travels as
    :attr:`lmer_platform.spawn.SpawnRequest.identity_repo_url` and is never
    recorded. That distinction is the whole point: the spawn writes what it is
    given into the tracked index as the run's ``repo``, a recorded URL beats a
    reconstruction everywhere afterwards, and it round-trips — so an adopted run
    answered once would pass :class:`lmer_platform.resume.RepoUrlRequired`'s
    check forever after on a URL nobody supplied, quietly undoing the decision
    that a missing repo URL is asked for rather than guessed.
    """
    return f"https://{request.host}/{request.project}"


def answer_run(config: PlatformConfig, request: AnswerRequest) -> AnswerResult:
    """Answer a question-blocked run by respawning it with the answer attached.

    Raises :class:`AnswerError` (with a status) when the answer cannot be
    delivered, and lets :class:`lmer_platform.spawn.CapacityError` /
    :class:`~lmer_platform.spawn.SpawnError` through unchanged — the concurrency
    cap belongs to the spawn path, and an answer is not a reason to exceed it.
    """
    request = request.validate()
    tracked = _require_tracked(request)
    _refuse_if_live(request, request.slug)
    ref = _require_run_dir(config, request)
    state = _load_state(ref)
    _refuse_if_finished(state, ref)
    question = _require_question_stop(state, ref)
    taskdef, target = _respawn_inputs(state, tracked, ref)
    recorded_slug = _require_same_run(taskdef, target, state, ref)
    if recorded_slug != request.slug:
        # The run was renamed: it is tracked under its directory name while any
        # session for it registers under the recorded slug, so the check above did
        # not look at the identity the new session would take.
        _refuse_if_live(request, recorded_slug)

    result = spawn_session(
        config,
        SpawnRequest(
            taskdef=taskdef,
            target=target,
            # Recorded only if the run actually has one — an adopted run that has
            # none keeps having none. The reconstruction beside it is for deriving
            # the identity and is deliberately not written down.
            repo_url=tracked.repo,
            identity_repo_url=_identity_repo_url(request),
            # The typed field, not extra_args: see the module docstring — that
            # field is where a validated answer goes, and the flag is refused in
            # the caller-supplied argv beside it.
            answer=request.answer,
        ),
    )

    # The spawn tracks the run under the identity *it* derived, which is the
    # recorded slug — the same key in the ordinary case, and a different one for a
    # run whose directory was renamed. Noting the session under the key the
    # operator answered is what keeps their detail view following the session it
    # just started instead of the one that asked.
    #
    # Best-effort from here down, and the ordering is the reason: the container
    # already has the answer, so a bookkeeping failure reported as a failed answer
    # would send the operator to answer again — which the live-session check would
    # then refuse. A stale ``last_session_id`` is a worse view, not a lost answer.
    try:
        runs.note_session(
            request.host, request.project, request.slug, result.session_id
        )
    except StoreError as exc:
        logger.warning(
            "platform_answer_index_not_updated run=%s session=%s error=%s — the "
            "answer is already on its way; the run's detail view will keep "
            "pointing at the previous session",
            request.key, result.session_id, exc,
        )

    # Length, not text: the platform's history is a debugging artifact people read
    # and paste, and an answer is the operator's content.
    append_event(
        "run_answered",
        note=request.key,
        data={
            "run": {
                "host": request.host,
                "project": request.project,
                "slug": request.slug,
            },
            "session": result.session_id,
            "answer_chars": len(request.answer),
        },
    )
    logger.info(
        "platform_run_answered run=%s session=%s answer_chars=%d",
        request.key, result.session_id, len(request.answer),
    )

    return AnswerResult(
        host=request.host,
        project=request.project,
        slug=request.slug,
        question=question,
        session=result,
    )
