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
  Legacy records only — see the schema-2 section below.
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

A schema-2 record is answered as a new execution
------------------------------------------------
A record that carries ``session.uid`` in a schema-2 tree is one execution,
addressed by ``host/project/<uid>`` (:attr:`AnswerRequest.uid`), and it is never
written in by another launch: the answering session founds its own record with
the asking one as read-only context, exactly as :mod:`lmer_platform.resume`
continues one — and the same rules are lifted for it. No landing-run check
(the identity is the uid), no live-session refusal (two answers to one source
are a fork; the spawn's concurrency note comes back as ``warning``), no
``note_session`` on the asking row (the answer is a row of its own, #229), and
an empty, ``reference`` or ``other`` target respawns on ``--repo`` with a
transport URL — the caller's ``repo_url`` or the run's recorded clone URL,
never :func:`_identity_repo_url`'s reconstruction. An entry the key transition
left unresolved is refused with its candidates. Legacy records answer exactly as
described above.

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
from .projects import run_operation, resume_selection

import logging
from dataclasses import dataclass, replace
from typing import Optional

from work_repo import run_state
from work_repo.utils import classify_target

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
from .workrepo import RunDirRef, record_session_id, resolve_run_dir

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
#: view, and the only attention it can carry is a ``live_question``, whose reply
#: goes to the live session's ask channel rather than through here. So the UI
#: never offers an answer for it and the API must not accept one either:
#: ``answer_question`` deliberately leaves ``status`` alone, so the respawned
#: session would arrive holding the answer and the completed-run directive telling
#: it not to act on anything.
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
    """Which run to answer, and with what.

    ``uid`` addresses a schema-2 execution (32 lowercase hex; spec §6.4) and,
    as for :class:`lmer_platform.resume.ResumeRequest`, makes ``slug``
    optional: the group label comes from the tracked entry the uid resolves to.

    ``repo_url`` is consulted for one case only: a schema-2 record whose target
    cannot carry the respawn (empty, ``reference`` or ``other``), where it is the
    TRANSPORT ``--repo`` clones from — checked against the run's own project
    identity, and preferred over the run's recorded URL exactly as resume
    prefers it (:func:`lmer_platform.resume._repo_url_for`). Everywhere else an
    answer respawns from what the run recorded, as it always has.
    """

    host: str
    project: str
    slug: Optional[str] = None
    answer: str = ""
    allow_unsigned_source: Optional[str] = None
    workspace_id: Optional[str] = None
    home_revision: Optional[int] = None
    confirm_project: bool = False
    slot: Optional[str] = None
    uid: Optional[str] = None
    repo_url: Optional[str] = None

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
        uid = _clean_uid(self.uid, AnswerError)
        fields = {}
        for name in ("host", "project", "slug"):
            value = getattr(self, name)
            if name == "slug" and uid is not None and (
                value is None or (isinstance(value, str) and not value.strip())
            ):
                fields[name] = None
                continue
            if not isinstance(value, str) or not value.strip():
                raise AnswerError(f"{name} is required")
            fields[name] = value.strip()
        _reject_traversal(
            {name: value for name, value in fields.items() if value is not None}
        )

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
        acknowledgement = self.allow_unsigned_source
        if acknowledgement is not None:
            if not isinstance(acknowledgement, str):
                raise AnswerError(
                    "allow_unsigned_source must be a source UID string"
                )
            acknowledgement = acknowledgement.strip() or None
        return replace(
            self,
            uid=uid,
            repo_url=_clean_optional_text("repo_url", self.repo_url),
            answer=answer,
            allow_unsigned_source=acknowledgement,
            workspace_id=_clean_optional_text("workspace_id", self.workspace_id),
            home_revision=_clean_optional_int("home_revision", self.home_revision),
            confirm_project=_clean_flag("confirm_project", self.confirm_project),
            slot=_clean_optional_text("slot", self.slot),
            **fields,
        )

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

        ``host/project/<uid>`` for a schema-2 execution, through
        :func:`lmer_platform.runs.key_parts` — the one spelling of that rule.
        """
        return runs.run_key(*runs.key_parts(
            {"host": self.host, "project": self.project,
             "slug": self.slug, "uid": self.uid}
        ))


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
    #: The schema-2 execution answered, when the run is one; the execution the
    #: answering session founds is ``session.uid``, a row of its own.
    uid: Optional[str] = None

    def to_dict(self) -> dict:
        """Wire form, deliberately **without** the spawn's ``command``.

        :meth:`lmer_platform.spawn.SpawnResult.to_dict` echoes the argv it
        launched, and this argv carries ``--answer=<text>``. An answer is whatever
        the operator was asked for, so it does not travel back out in a response
        body — same trade as keeping the control token in the environment rather
        than in argv. The *question* is echoed: the client already has it, and
        repeating it is what makes the reply readable in a log or a CLI.

        ``warning`` is the spawn's, under the key ``POST /api/sessions`` and the
        resume reply use: on a schema-2 record it carries the concurrency note
        (spec §6.3), since a second answer to one source is a fork nothing
        refuses. It, ``started`` and the ``uid`` keys appear only for a schema-2
        execution, so a schema-1 reply keeps exactly the shape it had.
        """
        payload = {"run": {"host": self.host, "project": self.project, "slug": self.slug}}
        if self.uid is not None:
            payload["run"]["uid"] = self.uid
        if self.uid is not None and self.session.uid is not None:
            payload["started"] = {
                "host": self.host,
                "project": self.project,
                "slug": self.session.slug,
                "uid": self.session.uid,
            }
        if self.uid is not None:
            payload["warning"] = self.session.warning
        return {
            **payload,
            "question": self.question,
            "session": {
                "session_id": self.session.session_id,
                "pid": self.session.pid,
                **self.session.launch_settings(),
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


def _clean_uid(value, error) -> Optional[str]:
    """A schema-2 execution uid — 32 lowercase hex — or ``None`` when absent.

    Refused rather than normalised past stripping: the uid is an address, and
    lower-casing an upper-case one would address a record the caller did not
    spell. Shared with :mod:`lmer_platform.resume`, hence *error*.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise error(f"uid must be a string, got {type(value).__name__}")
    uid = value.strip()
    if not uid:
        return None
    if not runs.UID_RE.match(uid):
        raise error(f"uid must be 32 lowercase hex characters, got {uid!r}")
    return uid


def _clean_optional_text(name: str, value) -> Optional[str]:
    """A stripped optional string, ``None`` when absent or blank.

    The project fields beside ``answer`` come off the same HTTP body, so they get
    the same treatment: a blank selection means "I did not say", not a value with
    surrounding whitespace for the project layer to match against.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise AnswerError(f"{name} must be a string, got {type(value).__name__}")
    return value.strip() or None


def _clean_optional_int(name: str, value) -> Optional[int]:
    """An optional non-negative integer, refused rather than coerced.

    ``home_revision`` is compared for equality against the revision recorded for
    the run, so a string would never match and would read as "the project moved";
    and ``bool`` is an ``int`` in Python, so ``True`` must not pass as 1.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AnswerError(f"{name} must be a non-negative integer, got {value!r}")
    return value


def _clean_flag(name: str, value) -> bool:
    """An optional boolean, refused rather than truthy-coerced.

    The route already spells this ``is True``; the only way a non-bool arrives is
    a caller that built the request directly, and ``bool("false")`` is ``True`` —
    exactly the wrong answer for a confirmation flag.
    """
    if not isinstance(value, bool):
        raise AnswerError(f"{name} must be a boolean, got {type(value).__name__}")
    return value


def _require_tracked(request: AnswerRequest) -> runs.TrackedRun:
    """The tracked entry for the run, or refuse.

    Scope is the local index (spec D25): a run this orchestrator neither spawned
    nor adopted may well be a colleague's, and starting a container for it is not
    a decision to make on their behalf.

    Looked up by uid when the request carries one. An entry the key transition
    left unresolved is refused with its candidates — the resume module's
    :class:`~lmer_platform.resume.RunUnresolved`, which this route already
    renders as ``{code, message}`` and which a picker needs to stay one shape.
    """
    # deferred: cycle — lmer_platform.resume imports this module at top
    from .resume import refuse_if_unresolved

    entry = runs.get_tracked(
        request.host, request.project, request.slug, uid=request.uid
    )
    if entry is None:
        raise RunNotTracked(
            f"{request.key} is not tracked by this orchestrator — adopt it "
            "first (POST /api/runs/adopt) so it is in scope, then answer it"
        )
    refuse_if_unresolved(entry, "answer")
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

    By uid when the request has one: exactly that record, never the group's
    newest (:func:`lmer_platform.workrepo.resolve_run_dir`).
    """
    ref = resolve_run_dir(
        config, request.host, request.project, request.slug, uid=request.uid
    )
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
    state: dict,
    tracked: runs.TrackedRun,
    ref: RunDirRef,
    *,
    config: Optional[PlatformConfig] = None,
    request: Optional[AnswerRequest] = None,
    execution: Optional[str] = None,
) -> tuple:
    """The ``(taskdef, target, repo_url)`` a respawn has to carry, or refuse.

    The run's own state wins over the tracked index: ``run_state.seed_state``
    makes both immutable for the life of the run, and they are exactly what the
    container derives its run dir from — so the recorded pair is the one that
    lands back on this run. The index is the fallback for state that predates them
    or was hand-edited.

    An absent target is a refusal rather than a bare ``lmer <taskdef>``: the slug
    of a target-less run is the taskdef alone, which any other target-less run of
    the same taskdef shares, and :meth:`SpawnRequest.validate` refuses one anyway.

    Except on a schema-2 record (*execution* is its uid, spec §6.4, #537): that
    record is addressed by its uid, so an empty, ``reference`` or ``other``
    target is answerable given a TRANSPORT URL for ``--repo`` — the caller's
    ``repo_url``, else the run's recorded clone URL, else
    :class:`~lmer_platform.resume.RepoUrlRequired`, validated against the run's
    identity by the one function resume uses for it. Elsewhere ``repo_url`` is
    the run's recorded URL, which is what an answer always carried — ``None``
    for an adopted run that has none, and :func:`_identity_repo_url` covers the
    identity there without ever becoming a clone URL.

    A schema-2 record whose target is itself a clone URL (written before spec
    §6.5 moved such a target into ``repo_url``) launches the way resume
    launches it: on ``--repo`` with no positional (:func:`lmer_platform.resume.
    _launch_target`), so the answering execution records no repository-class
    target either. Its URL is the transport rule's — the caller's, else the
    recorded one — with the target itself as the recorded clone URL of a run
    that has no other, validated against the run's identity the same way.
    """
    # deferred: cycle — lmer_platform.resume imports this module at top
    from .resume import _repo_url_for, _target_needs_transport

    taskdef = state.get("taskdef") or tracked.taskdef
    target = state.get("target") or tracked.target
    needs_transport = execution is not None and _target_needs_transport(target)
    missing = [
        name
        for name, value in (("taskdef", taskdef), ("target", target))
        if not (isinstance(value, str) and value.strip())
        and not (name == "target" and needs_transport)
    ]
    if missing:
        raise NotAnswerable(
            f"{ref.rel_path} records no {' and no '.join(missing)}, so the platform "
            "cannot work out what to respawn — answer it from a shell with "
            '`lmer <taskdef> <target> --answer="…"`'
        )
    target = target.strip() if isinstance(target, str) else ""
    if needs_transport:
        return taskdef.strip(), target, _repo_url_for(config, tracked, request, "answer")
    if execution is not None and classify_target(target) == "repository":
        recorded = replace(tracked, repo=tracked.repo or target)
        return taskdef.strip(), target, _repo_url_for(config, recorded, request, "answer")
    return taskdef.strip(), target, tracked.repo


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
    if derived != recorded and not run_state.has_vacated(state, derived):
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


@run_operation
def answer_run(config: PlatformConfig, request: AnswerRequest) -> AnswerResult:
    """Answer a question-blocked run by respawning it with the answer attached.

    Raises :class:`AnswerError` (with a status) when the answer cannot be
    delivered, and lets :class:`lmer_platform.spawn.CapacityError` /
    :class:`~lmer_platform.spawn.SpawnError` through unchanged — the concurrency
    cap belongs to the spawn path, and an answer is not a reason to exceed it.
    """
    # The schema-2 rule is shared: a prior launch is read-only context and an
    # unsigned source needs a decision bound to that exact UID.
    # deferred: cycle — lmer_platform.resume imports this module at top
    from .resume import (
        _bind_to_tracked, _launch_target, _schema_two_source, schema_two_record,
    )

    request = request.validate()
    tracked = _require_tracked(request)
    request = _bind_to_tracked(request, tracked)
    if request.uid is None:
        _refuse_if_live(request, request.slug)
    ref = _require_run_dir(config, request)
    state = _load_state(ref)
    _refuse_if_finished(state, ref)
    question = _require_question_stop(state, ref)
    # A record with ``session.uid`` is one schema-2 execution (spec §6.4): the
    # respawn founds its own record with this one as read-only context, so the
    # slug-era landing check and the live refusal do not apply — a second answer
    # to one source is a fork, reported by the spawn's concurrency note.
    execution = schema_two_record(config, state, ref, NotAnswerable)
    taskdef, target, repo_url = _respawn_inputs(
        state, tracked, ref, config=config, request=request, execution=execution
    )
    if execution is None:
        if request.uid is not None:
            # The early check above is skipped for a uid request, whose record
            # had to be read first to know whether it is a schema-2 one.
            _refuse_if_live(request, request.slug)
        recorded_slug = _require_same_run(taskdef, target, state, ref)
        if recorded_slug != request.slug:
            # The run was renamed: it is tracked under its directory name while
            # any session for it registers under the recorded slug, so the check
            # above did not look at the identity the new session would take.
            _refuse_if_live(request, recorded_slug)

    source_session_id, unsigned_source_ack = _schema_two_source(
        config, request, ref, state
    )

    result = spawn_session(
        config,
        SpawnRequest(
            **resume_selection(request.key, request.slot),
            taskdef=taskdef,
            # Absent rather than "": an empty-target schema-2 record respawns on
            # ``--repo`` and no positional, and so does one whose target is a
            # clone URL; a legacy record keeps the positional it always had.
            target=_launch_target(target) if execution is not None else (target or None),
            # Recorded only if the run actually has one — an adopted run that has
            # none keeps having none. The reconstruction beside it is for deriving
            # the identity and is deliberately not written down. For a schema-2
            # record whose target cannot carry the launch it is the transport
            # URL ``_respawn_inputs`` resolved — never the reconstruction.
            repo_url=repo_url,
            identity_repo_url=_identity_repo_url(request),
            # The typed field, not extra_args: see the module docstring — that
            # field is where a validated answer goes, and the flag is refused in
            # the caller-supplied argv beside it.
            answer=request.answer,
            # The record that ASKED, so the answer is applied in it rather than
            # in a fresh record the container would otherwise mint beside it
            # (spec D22: the session is the unit of record, and LMER_SESSION_ID
            # picks which one a launch writes in). An empty record holding an
            # answer, with the question still standing in the real one, is the
            # shape this closes.
            #
            # Unconditional, unlike the same injection in
            # :mod:`lmer_platform.resume`: ``_require_same_run`` above has already
            # refused every respawn that would derive another run, so there is no
            # sibling group for this id to be wrong in. ``None`` for a legacy tree
            # and nothing changes there.
            #
            # This is now the direct route. ``work_repo.cli._session_that_asked``
            # stays as the fallback it describes itself as — it reroutes a launch
            # carrying ``LMER_ANSWER`` to the record that asked, which is what
            # covers launchers that carry only the answer and nothing else.
            session_id=(
                None if source_session_id is not None else record_session_id(ref)
            ),
            source_session_id=source_session_id,
            allow_unsigned_source=unsigned_source_ack,
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
    #
    # Never for a schema-2 record (#229): the answering execution is its own
    # uid-keyed row, tracked by the spawn, and the row that asked keeps the
    # session that wrote it.
    if execution is None:
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
    data = {
        "run": {
            "host": request.host,
            "project": request.project,
            "slug": request.slug,
        },
        "session": result.session_id,
        "answer_chars": len(request.answer),
    }
    # Schema 2 only, so a legacy event keeps its shape: there the slug is a
    # group label, and only the uids say which execution asked and which one
    # the answering session founds — the reply's ``run`` and ``started``.
    if execution is not None:
        data["run"]["uid"] = execution
        if result.uid is not None:
            data["started"] = {
                "host": request.host,
                "project": request.project,
                "slug": result.slug,
                "uid": result.uid,
            }
    append_event("run_answered", note=request.key, data=data)
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
        uid=execution,
    )
