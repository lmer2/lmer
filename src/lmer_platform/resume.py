"""Continuing a run this orchestrator already tracks (issue #141, slice M2 / T25).

Adoption tracks a run; something has to be able to act on one
-------------------------------------------------------------
Adopting puts a run in the local index so its state reaches the fleet view (spec
D25). That is all it does — it spawns nothing and attaches to nothing. Which is
how an operator could adopt a run, watch the row say "requires your review", and
find no verb that moved it forward: the list was spawn a new session, answer a
question-blocked run, adopt, forget, prune, rescan. A view that shows you work
needing you and offers no way in is worse than one that never showed it. This
module is the missing verb.

Resuming is a spawn, like answering
-----------------------------------
The same shape as :mod:`lmer_platform.answer`, minus the answer. A run that
stopped has *exited* (spec D15), so there is no session to type into, and the
platform never writes run state (spec D3) — least of all inside the mirror, which
is a read-only clone the daemon force-resets on every pull. Everything here is a
read plus one spawn: the session resolves the run, claims it and prints its own
resume brief, exactly as it does when a human types ``lmer <taskdef> <target>``.
What this module owns is deciding whether that spawn will land where the operator
thinks it will, and refusing in plain words when it would not.

Continuing a run and starting its sibling are both spelled here
---------------------------------------------------------------
The taskdef defaults to the one the run recorded, because continuing is usually
"the same thing again" and has to be one tap. It is overridable because the
workflow that motivated this verb is ``develop`` → ``review`` → ``followup``
against one target.

Those are not the same operation and the reply says which one happened rather
than leaving the caller to work it out. The container derives its run dir from
``taskdef``/``target`` (``run_state.derive_slug``), so a different taskdef derives
a different slug and the session lands on a **sibling run against the same
target**, not on the run the operator was looking at.
:attr:`ResumeResult.continued` is that distinction and
:attr:`ResumeResult.started_slug` is where the session actually went.
:mod:`~lmer_platform.answer` refuses exactly this mismatch
(``_require_same_run``), because an answer applied to another run is an answer
lost; here it is the point of the override — but only when the caller *asked* for
it. A mismatch with no override means the index and the run's own state disagree,
which is the hand-edited case ``answer`` refuses, and it is refused here too.

Which record a resume reads, now that a slug groups several
------------------------------------------------------------
In schema form the unit of record is the SESSION (spec D22): every launch owns a
complete, independent ``<project>/_runs/<slug>/<session-id>[--<name>]/``, and
``_runs/<slug>/`` is a grouping level nothing is written at. So the identity a
resume arrives with — ``host/project/slug``, unchanged, because the session id is
part of the address and never the key — can name several records, and "read the
run's state file" stops being one directory by itself.

The rule for picking one is deliberately **not** this module's.
:func:`lmer_platform.workrepo._sessions_in` is the single definition of which
children of a grouping dir are records and in what order (newest last), and both
the adoption picker and :func:`~lmer_platform.workrepo.resolve_run_dir` use it. A
resume that opened a different session than the picker offered — refusing on the
newest launch's finished status while the fleet row quoted an older one, or the
reverse — is exactly the divergence that shared rule exists to prevent, so a
plain slug resume takes what ``resolve_run_dir`` hands it and *names* it:
:attr:`ResumeResult.rel_path` is the address the checks were made against, and
every refusal has quoted :attr:`~lmer_platform.workrepo.RunDirRef.rel_path` since
a named run made the composed ``runs/<slug>`` a lie.

:attr:`ResumeRequest.session_id` is how a caller means a particular launch
instead. It is resolved against the sessions of the group the run resolved into,
by the session dir's own name or by the id the container's one name-bearing
rename appended to (``<session-id>--<name>``) — and an id that names none of
them, or two of them, is refused with the list rather than resolved to a
neighbour. A whole record read off the wrong launch is the failure this field
exists to make impossible, so guessing here would defeat it.

Two different things are called a session in this file, and only one of them is
an index entry: :attr:`ResumeResult.session` is the *container* session the spawn
started (``s-…``, registered by :mod:`lmer_platform.registry`, recorded by
``runs.note_session``), while ``session_id`` here is the work repo's record dir
(``20260824-165230-2-31245``). They are different namespaces and neither is
derived from the other.

Every state check is about the run the session will land in
-----------------------------------------------------------
Not about the run the operator pointed at, and the difference is not academic: a
``develop`` run is normally ``complete`` when you want to ``review`` its target, so
checking the *source* run's status would refuse the override's main use case. The
source run is read for two things only — the taskdef and target a respawn needs —
and everything else (a live session, an open question, a finished status) is
asked of the landing run, which for the ordinary continue *is* the source run and
for an override is the sibling, if it exists at all.

A missing repo URL is asked for, not guessed
--------------------------------------------
``repo_url`` never reaches the container; it is what
:func:`lmer_platform.spawn.derive_run_identity` predicts the run's host/project
from, and what the spawn then **records** — in the session's registry entry and,
via ``runs.track``, in the tracked index as that run's ``repo``.
:mod:`~lmer_platform.answer` reconstructs ``https://<host>/<project>`` when a run
has none, and that is fine where it is: the value round-trips to the identity it
was built from, and the run it names already exists.

Neither of those holds here. A resume with an overridden taskdef *founds* a run —
the index gains a brand-new entry whose ``repo`` is whatever this module passed —
and once any entry carries a URL it wins over every later reconstruction, so a
guess is not used once and discarded, it is durable and becomes the run's repo of
record. Guessing the one field an adopted run genuinely does not know, and then
writing the guess down, is worse than asking for it: hence
:class:`RepoUrlRequired`, a refusal with a machine-recognisable
:attr:`~ResumeError.code` the UI turns into "give me the repo URL", and
:attr:`ResumeRequest.repo_url` to satisfy it.

A schema-2 record is one execution, addressed by its uid
-------------------------------------------------------
In a schema-2 tree a record's identity is ``host/project/<uid>`` and the slug is
only its group label (spec D1, §6.4). :attr:`ResumeRequest.uid` addresses the
tracked entry and the record by that uid, and an entry the key transition
(:mod:`lmer_platform.run_transition`) could not bind to one record is refused
with its candidates (:class:`RunUnresolved`) rather than read as the group's
newest. Continuing such a record never writes in it: the spawn mints a fresh
uid and the source travels as read-only context (``--source-session``), so
four slug-era rules stop applying to a record that carries ``session.uid``:

- the landing-run prediction (:func:`_landing_slug`) — the identity is the uid,
  and the new record's group is whatever ``derive_slug`` says;
- the live-session refusal — two continuations of one source are a fork, two
  rows with one label (D8). The spawn's concurrency note
  (:func:`lmer_platform.spawn.concurrent_sessions_note`) rides back as the
  reply's ``warning`` instead;
- ``runs.note_session`` on the source row — the continuation is its own
  uid-keyed row, so the source keeps its own ``last_session_id`` and reads its
  state from its own record (#229);
- the "records no target" refusal. An empty, ``reference`` or ``other``
  target cannot carry a launch by itself, so the launch names its repository
  with ``--repo`` — a TRANSPORT URL, which is the caller's ``repo_url`` or the
  run's recorded ``repo`` and, with neither, :class:`RepoUrlRequired` (#537).
  The registry's scheme-less ``repo:`` and the primary-remote fold are identity
  and only ever validate that URL; neither is cloned from.

Legacy records keep every one of those rules exactly as described above.

A finished run reopens on a direction, not on a click
-----------------------------------------------------
``complete``/``archived`` is neither a hard refusal here nor a silent reopen —
both are wrong, in opposite directions. The run-state contract already decides it
(docs/RUN-STATE.md §3, issue #96): a session that resolves a finished run does not
re-seed "and never silently resumes either". In a legacy tree, *with* a seed
direction (``LMER_START_PROMPT``, which is what ``lmer --prompt`` sets) it records
the seed as the goal, reopens with ``work state set --status=in-progress``, and
proceeds on it; *without* one it asks the user whether this is a new target or a
continuation, and ends the session if the question goes unanswered. On schema 2 a
finished record is never reopened in place (spec D14, #437) — continue it as a
new execution: ``lmer <taskdef> [<target>] --source-session <uid>`` (or the
platform's continue, which is this module), with the direction seeding the new
record instead.

So a finished run needs :attr:`ResumeRequest.direction`, and it travels as
``--prompt=<text>``. That makes the reopen the operator's explicit direction in
the contract's own terms rather than a side effect of clicking "continue", and it
is the difference between a session that gets to work and a session that spends a
slot to ask whether it should — the same outcome ``answer._refuse_if_finished``
declines to create. A flat refusal would instead leave the operator with no way to
say yes, which is the gap this module exists to close. A direction is accepted for
an unfinished run too: it is the next session's seed either way, and refusing it
where it merely is not *required* would be an API answering "why not?" with
"because you did not have to".

What is refused, and why refusing beats a hopeful spawn
------------------------------------------------------
Each of these costs a container slot and leaves the operator believing they acted:

- **A run this orchestrator does not track.** Scope is the local index (spec D25);
  a colleague's run is not ours to start.
- **A live session on the landing run.** Liveness outranks committed run state
  (spec D24), so a row can read "waiting on you" while a session is already
  working it. Two containers for one run then fight over its owner claim — and
  this is also what makes a double-tapped button harmless. Legacy records only:
  a schema-2 continuation is a fork, not a second writer (above).
- **An entry the key transition left unresolved.** Its slug names several
  executions and nothing proves which one it meant; the refusal lists them so
  the operator can re-adopt by uid.
- **A run stopped on a question.** A plain respawn reads the question back with no
  answer and leaves the stop in place, so the run is exactly as blocked as before.
  That one has its own verb (``POST /api/runs/answer``) and the refusal names it.
- **A run whose dir is missing from the mirror, or whose state cannot be read.**
  The taskdef and target come from there; guessing them is how a resume lands on
  another run.
- **A respawn that would derive a different run with no override to explain it**,
  and identity, taskdef or target values that would not survive being a path
  segment or an argv positional.

The direction is content, not a credential channel
--------------------------------------------------
It is argv on the host and an environment variable in the container, readable via
``ps`` by any local user — inherent to ``lmer --prompt``, and not something this
module can improve on. What it can do is not add copies, so the text is never
logged, never put in the platform's event history, and dropped from the API's
reply. Same trade as the answer text, and as the control token that stays out of
argv in :mod:`lmer_platform.spawn`.
"""

from __future__ import annotations
from .projects import run_operation, resume_selection

import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from lmer_cli.container.clone_and_exec import _scrub_credentials
from work_repo import run_state
from work_repo import session_record
from work_repo.utils import classify_target

from . import mute, runs
# Deliberate reuse of two private helpers rather than two second copies of
# them, the way `workrepo` imports `_scrub_credentials` from `clone_and_exec`:
# `_reject_traversal` is an identical decision with identical inputs,
# `_load_state` carries a non-obvious branch (the mirror is force-reset
# mid-request, so the state file can vanish between resolving the run dir and
# reading it), and `_FINISHED_STATUSES` is one fact about run state. Their
# refusals are worded for the answer path, so the one that raises is re-raised
# here with resume's own words and exception type. All three want a shared home;
# this import is the honest interim, not the intended shape. "Which live session
# holds this run" already has that home and it is not here — `spawn`, below,
# where the invariant that refusal enforces lives.
from .answer import (
    MAX_ANSWER_CHARS,
    AnswerError,
    NotAnswerable,
    _FINISHED_STATUSES,
    _clean_uid,
    _load_state as _load_state_or_refuse,
    _reject_traversal,
)
from .config import PlatformConfig
from .spawn import (
    SpawnError,
    SpawnRequest,
    SpawnResult,
    derive_repo_identity,
    live_session_for_run,
    schema_two,
    spawn_session,
)
from .store import StoreError, append_event
# The same trade as the `answer` imports above, for the same reason: `_sessions_in`
# is THE definition of which children of a `_runs/<slug>/` group are records and
# in what order, shared with the adoption picker and with `resolve_run_dir` on
# purpose (module header) — a second copy here is precisely how a resume would
# come to open a session the picker never offered. `_ref_for` and `_address` are
# the two ways that module turns a found directory into an honest address, and
# `_SCHEMA_RUNS_DIR` is one fact about the layout. All four want to be public;
# this import is the honest interim rather than a reimplementation.
from .workrepo import (
    _SCHEMA_RUNS_DIR,
    RunDirRef,
    _address,
    _ref_for,
    _sessions_in,
    folded_identity,
    record_session_id,
    resolve_run_dir,
)

logger = logging.getLogger("lmer_platform.resume")

__all__ = [
    "ResumeError", "RunNotTracked", "NotResumable", "RunIsLive", "QuestionOpen",
    "SessionNotFound", "RunUnresolved", "RepoUrlRequired", "DirectionRequired",
    "UnsignedSessionSource", "DIRECTION_FLAG",
    "MAX_DIRECTION_CHARS", "ResumeRequest", "ResumeResult", "resume_run",
]

#: The ``lmer`` flag a direction rides on, spelled as one ``--prompt=<text>``
#: token for the reason ``--answer=`` is: ``lmer`` parses with argparse, so a
#: two-token ``--prompt -f`` exits 2 with "expected one argument" — the value
#: looks like an option, and a direction beginning with a dash is ordinary
#: English. It is set inside the container as ``LMER_START_PROMPT``, which
#: ``work session-start`` threads into the resume brief as the run's seed
#: (``run_state.format_brief``) — the mechanism the completed-run direction
#: contract is written in.
#:
#: Unlike ``--answer`` this is *not* reserved in
#: :data:`lmer_platform.spawn._RESERVED_ARGS`, so it rides in ``extra_args``
#: rather than in a typed field. The distinction is validation, not argv: an
#: answer spelled by a caller would bypass checks this package owns, while a
#: prompt on ``POST /api/sessions`` is the same thing a shell user gets from
#: ``lmer --prompt`` and grants no privilege the caller lacked.
DIRECTION_FLAG = "--prompt"

#: Ceiling on one direction, in characters — the same number and the same reason
#: as :data:`lmer_platform.answer.MAX_ANSWER_CHARS`, aliased rather than restated
#: so the two cannot drift: both are a single ``execve`` argument, and the
#: alternative to a refusal here is "Argument list too long" from a spawn that
#: already looked accepted.
MAX_DIRECTION_CHARS = MAX_ANSWER_CHARS


class ResumeError(RuntimeError):
    """Base for every refusal here, carrying the status *and code* a route answers.

    The status rides on the exception as it does in :mod:`lmer_platform.answer`,
    so a route needs one handler and a refusal added later arrives with a code
    rather than a 500. The ``code`` is the addition: two of these refusals are
    *requests for one more field*, and a UI that has to regex an English sentence
    to tell them apart will get it wrong the first time the sentence is improved.

    The base itself is the request-is-unusable case (400).
    """

    status = 400
    code = "resume_refused"

    def to_dict(self) -> dict:
        """Wire form for a route's error body: the stable token plus the prose."""
        return {"code": self.code, "message": str(self)}


class RunNotTracked(ResumeError):
    """The run is not in this orchestrator's index, so it is not ours to resume."""

    status = 404
    code = "run_not_tracked"


class RepoUrlRequired(ResumeError):
    """The run has no usable repo URL and the platform will not invent one.

    400 rather than 409: the same request will never succeed, because nothing the
    platform can wait for supplies this. It needs a field added — which is exactly
    what the code is for.
    """

    code = "repo_url_required"


class DirectionRequired(ResumeError):
    """A finished run was resumed with nothing telling the session what to do.

    400 for the same reason as :class:`RepoUrlRequired`: the run is not going to
    stop being complete, so this is a request to resend with a direction rather
    than a state that resolves itself.
    """

    code = "direction_required"


class NotResumable(ResumeError):
    """The run exists but its state does not permit a resume right now.

    409 rather than 400: the request is well-formed and the same request may
    succeed later — once the mirror catches up, or the live session stops.
    """

    status = 409
    code = "not_resumable"


class UnsignedSessionSource(NotResumable):
    """The selected source has no final sign-out and needs an operator decision."""

    code = "unsigned_session"

    def __init__(self, message: str, source: dict):
        super().__init__(message)
        self.source = source

    def to_dict(self) -> dict:
        return {
            **super().to_dict(),
            "source": self.source,
            "decisions": ["continue_fresh", "cancel"],
        }


class RunIsLive(NotResumable):
    """A session is already working the run a resume would land on."""

    code = "live_session"


class QuestionOpen(NotResumable):
    """The run is stopped on a question, which is the answer verb's job."""

    code = "question_open"


class SessionNotFound(NotResumable):
    """The named session id is not one record of this run — see the docstring.

    409 and a :class:`NotResumable` because the run itself is fine and the request
    is well-formed: what conflicts is the record this identity actually has. The
    commonest arm — an id whose launch is not in the mirror yet — is one a repeat
    resolves once the push lands. The other two (a legacy tree, which addresses
    nothing by launch, and an id that names two directories) need the request
    changed, and share the code because the answer has the same shape: here is
    what this run's record does hold. That is why the code is its own rather than
    a bare ``not_resumable`` — a UI turns it into a picker, not into "cannot".
    """

    code = "session_not_found"


class RunUnresolved(NotResumable):
    """The tracked entry names a slug group whose executions it cannot be bound to.

    The key transition (:mod:`lmer_platform.run_transition`) marks a slug-keyed
    entry unresolved when its group holds several schema-2 records and nothing
    proves which one the row meant. Continuing "the newest" of them is the
    slug-era guess that transition exists to retire, so this refuses and carries
    the candidates — ``{uid, started_at, name}`` each — for a picker that
    re-adopts by uid (``POST /api/runs/adopt {uid}``). Its own code because the
    remedy is a different request, not a wait. Raised for the answer verb too,
    whose route already renders this module's ``{code, message}`` bodies.
    """

    code = "run_unresolved"

    def __init__(self, message: str, candidates: Optional[list] = None):
        super().__init__(message)
        self.candidates = list(candidates or [])

    def to_dict(self) -> dict:
        return {**super().to_dict(), "candidates": self.candidates}


@dataclass(frozen=True)
class ResumeRequest:
    """Which run to continue, and the four things a caller may say about it.

    ``taskdef`` is the override: ``None`` (or blank) means the run's recorded one,
    which is the one-tap continue. Anything else starts the sibling run described
    in the module docstring.

    ``repo_url`` and ``direction`` exist to satisfy :class:`RepoUrlRequired` and
    :class:`DirectionRequired` — the two refusals whose remedy is a second request
    with one more field, which is why they are fields rather than flags.

    ``session_id`` is the work repo's record dir (``20260824-165230-2-31245``,
    optionally with the ``--<name>`` suffix the naming rename appends), not the
    container session the spawn starts — see the module docstring on the two
    namespaces. ``None`` means "the record ``resolve_run_dir`` hands back", which
    is the newest launch of the group; it is optional because in a legacy tree
    there is nothing to choose between, and because the ordinary continue is one
    tap on a row that has already named its address.

    ``uid`` addresses a schema-2 execution (32 lowercase hex; spec §6.4). Two
    decisions ride on it:

    - **A uid-addressed request needs no slug.** The slug is the group label,
      and the tracked entry the uid resolves to already records it — so it is
      taken from there (:func:`_bind_to_tracked`), and a slug sent beside a uid
      is replaced by the entry's rather than compared with it: the uid is the
      address, and a label is not a second one.
    - **A uid with a ``session_id`` naming another record is refused.** Both
      would pick a record, and quietly preferring either reads a whole record
      off a launch the caller did not mean. The same holds when the uid comes
      from the tracked entry rather than the request.
    """

    host: str
    project: str
    slug: Optional[str] = None
    uid: Optional[str] = None
    taskdef: Optional[str] = None
    repo_url: Optional[str] = None
    direction: Optional[str] = None
    session_id: Optional[str] = None
    allow_unsigned_source: Optional[str] = None
    workspace_id: Optional[str] = None
    home_revision: Optional[int] = None
    confirm_project: bool = False
    slot: Optional[str] = None

    def validate(self) -> "ResumeRequest":
        """Return a normalised copy, or raise :class:`ResumeError`.

        Normalising rather than merely checking, because the identity is used
        twice with different tolerances — :func:`lmer_platform.runs.run_key`
        strips, a filesystem path does not — and a value that matched the index
        but not the mirror would surface as a baffling "not in the mirror".

        Blank optional fields normalise to ``None`` rather than to a refusal: all
        three are text inputs in a form the ordinary continue submits empty, and
        an empty box means "I did not say", not "I said nothing".

        ``slug`` is optional only beside a ``uid`` — see the class docstring.
        """
        uid = _clean_uid(self.uid, ResumeError)
        fields = {}
        for name in ("host", "project", "slug"):
            value = getattr(self, name)
            if name == "slug" and uid is not None and (
                value is None or (isinstance(value, str) and not value.strip())
            ):
                fields[name] = None
                continue
            if not isinstance(value, str) or not value.strip():
                raise ResumeError(f"{name} is required")
            fields[name] = value.strip()
        _reject_traversal_or_refuse(
            {name: value for name, value in fields.items() if value is not None},
            ResumeError,
        )
        session_id = _clean_session_id(self.session_id)
        _refuse_mismatched_session(uid, session_id)

        return replace(
            self,
            uid=uid,
            taskdef=_clean_taskdef(self.taskdef),
            repo_url=_clean_text("repo_url", self.repo_url),
            direction=_clean_direction(self.direction),
            session_id=session_id,
            allow_unsigned_source=_clean_session_id(self.allow_unsigned_source),
            workspace_id=_clean_text("workspace_id", self.workspace_id),
            home_revision=_clean_optional_int("home_revision", self.home_revision),
            confirm_project=_clean_flag("confirm_project", self.confirm_project),
            slot=_clean_text("slot", self.slot),
            **fields,
        )

    @property
    def key(self) -> str:
        """The run's identity, ``host/project/slug``, for messages and event notes.

        Not a path, for the reason given on :attr:`lmer_platform.answer.
        AnswerRequest.key`: a named run's directory is ``runs/<slug>--<name>``, so
        a refusal composed from the request alone would name a directory that does
        not exist. Refusals that have a resolved run in hand name its
        :attr:`~lmer_platform.workrepo.RunDirRef.rel_path` instead.

        ``host/project/<uid>`` for a schema-2 execution, through
        :func:`lmer_platform.runs.key_parts` — the one spelling of that rule.
        """
        return runs.run_key(*runs.key_parts(
            {"host": self.host, "project": self.project,
             "slug": self.slug, "uid": self.uid}
        ))


@dataclass(frozen=True)
class ResumeResult:
    """The session started to carry one run forward.

    Two identities, deliberately both present. ``host``/``project``/``slug`` are
    the run the operator pointed at; ``started_slug`` is the run the session will
    file itself under, and ``continued`` says whether those are the same run. They
    differ exactly when the caller overrode the taskdef, and a client that shows
    only the first would tell an operator their run is running when a sibling is.
    """

    host: str
    project: str
    slug: str
    taskdef: str
    target: str
    continued: bool
    started_slug: str
    session: SpawnResult
    #: The mirror-relative address of the record every check above was made
    #: against — in schema form the session dir, which is what says WHICH of a
    #: slug's launches was read (module docstring). ``None`` when there is no such
    #: record: an override that founds a run has nothing to address until the
    #: session it starts pushes something, the same absence
    #: :mod:`lmer_platform.inventory` renders as a row with no path.
    #:
    #: For a continue it is also the record the new session will WRITE in: that
    #: record's id is what the spawn carries (:func:`_landing_session_id`), which
    #: is what makes "the record that was checked" and "the record that was
    #: continued" one directory rather than two. The reply names no session id of
    #: its own — ``session`` here is the container session, a different namespace
    #: (module docstring), and an override injects nothing to name anyway.
    rel_path: Optional[str] = None
    #: The schema-2 execution the operator pointed at, when the run is one. The
    #: execution the session founds is ``session.uid`` — a different row.
    uid: Optional[str] = None

    def to_dict(self) -> dict:
        """Wire form, deliberately **without** the spawn's ``command``.

        :meth:`lmer_platform.spawn.SpawnResult.to_dict` echoes the argv it
        launched, and that argv can carry ``--prompt=<direction>``. A direction is
        the operator's content, so it does not travel back out in a response body
        — the same trade :meth:`lmer_platform.answer.AnswerResult.to_dict` makes
        for the answer text, and the reason both keep the shape rather than
        reusing ``SpawnResult``'s.

        ``warning`` is carried through from the spawn, the same key ``POST
        /api/sessions`` publishes, so a client can render it unconditionally on
        either reply. On a legacy record it is ``None`` on every path a resume
        can reach, and that is not an accident: :func:`_repo_url_for` refuses
        unless the URL derives back to *this* run's host and project — through
        the spawn's own :func:`lmer_platform.spawn.derive_repo_identity`, which
        is precisely the derivation :func:`lmer_platform.spawn._untracked_run_warning`
        fires on failing. On a schema-2 record it is where the concurrency note
        (:func:`lmer_platform.spawn.concurrent_sessions_note`) arrives: a second
        continuation of one source is a fork that nothing refuses, so the reply
        is how the operator hears about the first (spec §6.3).

        ``uid`` keys appear in ``run`` and ``started`` only for a schema-2
        execution, so a legacy reply keeps exactly the shape it had.
        """
        run = {"host": self.host, "project": self.project, "slug": self.slug}
        if self.uid is not None:
            run["uid"] = self.uid
        started = {
            "host": self.host,
            "project": self.project,
            "slug": self.started_slug,
        }
        if self.session.uid is not None:
            started["uid"] = self.session.uid
        return {
            "run": run,
            "started": started,
            "continued": self.continued,
            "taskdef": self.taskdef,
            "target": self.target,
            # Top-level rather than inside ``run``: it is the address of the
            # record the checks were made against, which for an override is the
            # SIBLING's — and ``run`` is the identity the operator pointed at.
            "rel_path": self.rel_path,
            "session": {
                "session_id": self.session.session_id,
                "pid": self.session.pid,
                **self.session.launch_settings(),
                "control": {
                    "host": self.session.control_host,
                    "port": self.session.control_port,
                },
            },
            "warning": self.session.warning,
            "note": (
                f"a fresh execution was started with {self.uid} as its read-only "
                "source — it is a row of its own, and the source keeps the state "
                "it stopped in"
                if self.uid is not None and self.continued else
                "a fresh session was started on this run — it claims the run and "
                "prints its own resume brief at session start, so the run's state "
                "catches up a moment later"
                if self.continued else
                f"a fresh session was started on {self.started_slug}, a different "
                f"run against the same target — {self.slug} is untouched and keeps "
                "the state it stopped in"
            ),
        }


def _reject_traversal_or_refuse(fields: dict, error) -> None:
    """:func:`lmer_platform.answer._reject_traversal`, as a resume refusal.

    The check is identical and stays in one place: these values become path
    segments under the mirror, and this is a verb that *starts something* with
    them. Only the exception type is translated — the wording is about paths, not
    about answering, so it survives the move intact.
    """
    try:
        _reject_traversal(fields)
    except AnswerError as exc:
        raise error(str(exc)) from exc


def _reject_argv_flag(name: str, value: str, error) -> None:
    """Refuse a value argparse would read as an option instead of a positional.

    ``taskdef`` and ``target`` are the first two words of the ``lmer`` command
    line (:func:`lmer_platform.spawn._build_command`) and ``lmer`` parses with
    argparse, so a value beginning with a dash is a *flag* there. A taskdef of
    ``--fastapi-token=…`` would be parsed as the flag
    :data:`lmer_platform.spawn._RESERVED_ARGS` refuses in ``extra_args``, with the
    target sliding into the taskdef's place — the same hijack that guard exists to
    stop, one position earlier where it does not look.
    """
    if value.startswith("-"):
        raise error(
            f"{name} may not begin with a dash ({value!r}): it is passed to `lmer` "
            "as a positional argument, and its parser reads that as a flag"
        )


def _reject_unsafe_taskdef(taskdef: str, error) -> None:
    """Both checks a taskdef needs: it is a path segment *and* an argv positional.

    ``run_state.derive_slug`` interpolates the taskdef into the run's directory
    name without sanitizing it (only the *target* goes through
    ``sanitize_task_target``), so a taskdef carrying ``..`` names a run dir outside
    ``runs/`` — in the mirror this module reads, and in the work repo the container
    writes.
    """
    _reject_traversal_or_refuse({"taskdef": taskdef}, error)
    _reject_argv_flag("taskdef", taskdef, error)


def _clean_text(name: str, value: Optional[str]) -> Optional[str]:
    """A stripped optional string, ``None`` when absent or blank."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ResumeError(f"{name} must be a string, got {type(value).__name__}")
    return value.strip() or None


def _clean_taskdef(value: Optional[str]) -> Optional[str]:
    """The taskdef override, validated as far as it can be without the run."""
    taskdef = _clean_text("taskdef", value)
    if taskdef is not None:
        _reject_unsafe_taskdef(taskdef, ResumeError)
    return taskdef


def _clean_session_id(value: Optional[str]) -> Optional[str]:
    """The session dir to continue, validated as a path segment.

    It is joined under the run's grouping dir exactly as the identity fields are
    joined under the mirror, so it gets the identity fields' check —
    :func:`lmer_platform.answer._reject_traversal`, once more at the point where
    the consequence appears. No grammar is imposed beyond that: the ``--<name>``
    half of a session dir is deliberately unconstrained (``docs/SCHEMA.md``), and
    an id that matches nothing is a :class:`SessionNotFound` that lists what is
    there, which is a better answer than a regex refusal.
    """
    session_id = _clean_text("session_id", value)
    if session_id is not None:
        _reject_traversal_or_refuse({"session_id": session_id}, ResumeError)
    return session_id


def _refuse_mismatched_session(uid: Optional[str], session_id: Optional[str]) -> None:
    """Refuse a ``session_id`` that names a different record than ``uid``.

    Compared through ``run_state.session_id_of`` so the ``<uid>--<name>`` dir a
    fleet row shows still agrees with its own uid. See :class:`ResumeRequest`.
    """
    if uid is None or session_id is None:
        return
    if run_state.session_id_of(session_id) != uid:
        raise ResumeError(
            f"session_id {session_id!r} names a different record than uid {uid} "
            "— each picks the record a resume reads, so send one of them, or two "
            "that agree"
        )


def _clean_direction(value: Optional[str]) -> Optional[str]:
    """The seed direction, stripped and bounded.

    Stripped because the container's brief renders it as one line either way, and
    bounded because it is one ``execve`` argument: past the limit the spawn itself
    fails with "Argument list too long", which is an unreadable way to learn that
    a pasted log file is not a direction.
    """
    direction = _clean_text("direction", value)
    if direction is not None and len(direction) > MAX_DIRECTION_CHARS:
        raise ResumeError(
            f"direction is {len(direction)} characters, over the "
            f"{MAX_DIRECTION_CHARS} limit — it is passed to the session as one "
            "command-line argument, and a longer one fails the spawn itself"
        )
    return direction


def _clean_optional_int(name: str, value) -> Optional[int]:
    """An optional non-negative integer, refused rather than coerced.

    ``home_revision`` is compared for equality against the revision recorded for
    the run, so a string would never match and would read as "the project moved";
    and ``bool`` is an ``int`` in Python, so ``True`` must not pass as 1.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ResumeError(f"{name} must be a non-negative integer, got {value!r}")
    return value


def _clean_flag(name: str, value) -> bool:
    """An optional boolean, refused rather than truthy-coerced.

    The route already spells this ``is True``; the only way a non-bool arrives is
    a caller that built the request directly, and ``bool("false")`` is ``True`` —
    exactly the wrong answer for a confirmation flag.
    """
    if not isinstance(value, bool):
        raise ResumeError(f"{name} must be a boolean, got {type(value).__name__}")
    return value


def _require_tracked(request: ResumeRequest) -> runs.TrackedRun:
    """The tracked entry for the run, or refuse.

    Scope is the local index (spec D25). A run this orchestrator neither spawned
    nor adopted may well be a colleague's, and starting a container for it is not
    a decision to make on their behalf — adopting it first is how an operator says
    it is theirs.

    Looked up by uid when the request carries one (spec §6.4). An entry the key
    transition left unresolved is refused here, before anything is read off the
    mirror: :func:`refuse_if_unresolved`.
    """
    entry = runs.get_tracked(
        request.host, request.project, request.slug, uid=request.uid
    )
    if entry is None:
        raise RunNotTracked(
            f"{request.key} is not tracked by this orchestrator — adopt it "
            "first (POST /api/runs/adopt) so it is in scope, then resume it"
        )
    refuse_if_unresolved(entry, "resume")
    return entry


def refuse_if_unresolved(entry: runs.TrackedRun, verb: str) -> None:
    """Raise :class:`RunUnresolved` when *entry* awaits a re-adopt by uid.

    The mark is the index's own (:func:`lmer_platform.runs.readopt_marks`), read
    rather than re-derived: :mod:`lmer_platform.run_transition` is what decides
    an entry is unresolved, and a second opinion here could continue a record
    that decision declined to pick. A *conflict* mark is not a refusal — that
    entry is bound to one record and only its carried platform state clashed.
    Shared with :mod:`lmer_platform.answer`, hence *verb*.
    """
    mark = runs.readopt_marks().get(entry.key)
    if not mark or "candidates" not in mark:
        return
    candidates = mark["candidates"]
    listed = ", ".join(
        str(candidate.get("uid")) for candidate in candidates
        if isinstance(candidate, dict)
    ) or "none listed"
    raise RunUnresolved(
        f"{entry.key} names a slug group with several executions and nothing "
        f"proves which one this row meant ({listed}) — re-adopt it by uid "
        f"(POST /api/runs/adopt with one of those uids), then {verb} that",
        candidates,
    )


def _bind_to_tracked(request, tracked: runs.TrackedRun):
    """*request* addressed the way *tracked* is keyed.

    A uid-addressed request takes its slug — the group label — from the entry
    (:class:`ResumeRequest`). A slug-addressed request that reached a uid-keyed
    entry (through the alias a key transition left) takes the entry's uid, so
    the record is resolved by uid and never as "the newest in the group", which
    is what that transition exists to stop. Then a ``session_id`` is held to the
    uid. A legacy entry has no uid and the request comes back unchanged.

    Duck-typed over the two request classes: :mod:`lmer_platform.answer` binds
    the same way and has no ``session_id``.
    """
    if tracked.uid is None:
        return request
    _refuse_mismatched_session(tracked.uid, getattr(request, "session_id", None))
    return replace(
        request,
        uid=tracked.uid,
        slug=tracked.slug if request.uid is not None else request.slug,
    )


def _require_run_dir(config: PlatformConfig, request: ResumeRequest) -> RunDirRef:
    """The record this resume reads, in the host mirror, or refuse.

    A resume is defined by what the run already recorded — its taskdef and target
    — so without the dir there is nothing to continue *from*, and a spawn made on
    assumptions is how one lands on a different run.

    ``resolve_run_dir`` answers with one record per identity in either shape, and
    in schema form that is the newest session of ``_runs/<slug>/`` — the launch
    the adoption picker offers, by the same rule (module docstring). A caller that
    means another one says so, and then the group that resolved is where it is
    looked for: the resolve is what proves this identity lives in this project dir
    at all, and re-deriving the project dir from the request would be a second
    answer to a question :mod:`~lmer_platform.workrepo` already owns.

    A uid names exactly one record, wherever its group is, and an unknown one is
    absent rather than the group's newest (:func:`lmer_platform.workrepo.
    resolve_run_dir`); a ``session_id`` beside it was already held to agree.
    """
    ref = resolve_run_dir(
        config, request.host, request.project, request.slug, uid=request.uid
    )
    if ref is None:
        raise NotResumable(
            f"{request.key} is not in the host mirror, so the platform cannot "
            "tell what resuming it would continue — it may not have been pushed "
            "yet, the mirror may be stale (rescan to refresh), or the run dir may "
            "have been renamed since it was tracked"
        )
    if request.session_id is None or request.uid is not None:
        return ref
    return _require_session(config, request, ref)


def _session_group(ref: RunDirRef) -> Optional[Path]:
    """The ``_runs/<slug>/`` group *ref* is one session of, or ``None``.

    Read off the resolved path rather than composed, so it names the group the
    record was actually found in — which for a registered project is a flat short
    name the identity could not have produced (D7). The test is the layout's own:
    a session dir's grandparent is the schema record area, while a legacy run dir
    is a direct child of ``runs/`` and has no launches under it at all.
    """
    group = ref.path.parent
    if group.parent.name != _SCHEMA_RUNS_DIR:
        return None
    return group


def _require_session(
    config: PlatformConfig, request: ResumeRequest, ref: RunDirRef
) -> RunDirRef:
    """The named launch's record, or refuse — never a neighbouring one.

    Matched on the session dir's own name first and on the id the naming rename
    suffixed second (``<session-id>--<name>``), because the id is what an operator
    wrote down and the suffix arrives later, mid-life. Both spellings resolve, so
    the address a fleet row shows can be pasted back verbatim.

    Two matches are a refusal rather than a choice: the name half of a session dir
    has no charset in any document, so one id really can end up on two directories,
    and reading a whole record off the wrong launch is what the field was added to
    prevent. No match is the same refusal carrying the list, because the remedy is
    a different id and the operator cannot see this tree.
    """
    root = config.mirror_path.resolve()
    wanted = request.session_id
    group = _session_group(ref)
    if group is None:
        raise SessionNotFound(
            f"{ref.rel_path} is a legacy run dir — one shared record per target, "
            f"read in place and never restructured — so it has no launch "
            f"{wanted!r} to continue: the session is the unit of record only in a "
            "tree that has moved to the new layout. Resume it without a session id"
        )
    sessions = _sessions_in(group)
    found = [
        session for session in sessions if session.name == wanted
    ] or [
        session for session in sessions if session.name.startswith(f"{wanted}--")
    ]
    if len(found) > 1:
        raise SessionNotFound(
            f"{_address(root, group)}: the session id {wanted!r} names more than "
            f"one record here ({', '.join(session.name for session in found)}) "
            "and a whole run record is not worth guessing at — name the directory "
            "in full"
        )
    if not found:
        launches = ", ".join(session.name for session in sessions) or "none"
        raise SessionNotFound(
            f"{_address(root, group)} holds no session {wanted!r} — this run's "
            f"launches are: {launches}. Name one of those, or resume with no "
            "session id to continue the newest (which is the one the fleet view "
            "offers)"
        )
    session = _ref_for(root, request.host, request.project, ref.slug, found[0])
    if session is None:
        # The mirror is force-reset under this path (see :func:`_load_state`), so
        # a record listed a moment ago can stop being one before it is addressed.
        raise NotResumable(
            f"{_address(root, found[0])} stopped being a run record while it was "
            "being resolved — the mirror is re-pulled under this request; try again"
        )
    return session


def _load_state(ref: RunDirRef) -> dict:
    """The run's recorded state, or refuse.

    Delegates to :func:`lmer_platform.answer._load_state` — same read, same
    tolerances, and in particular the same ``None`` branch, which is not a
    theoretical case: every fleet poll ``git reset --hard``s the mirror, so the
    state file can genuinely vanish between resolving the run dir and reading it,
    and without that branch the race is an ``AttributeError`` and a 500.

    Only the refusal is this module's: the cause is recovered from the chained
    exception rather than by re-reading, because that helper raises ``from`` the
    original by contract and re-wording is the whole reason this wrapper exists.
    """
    try:
        return _load_state_or_refuse(ref)
    except NotAnswerable as exc:
        cause = exc.__cause__
        detail = f" ({cause})" if cause is not None else ""
        raise NotResumable(
            f"{ref.rel_path}: run state could not be read{detail} — a resume "
            "derives its taskdef and target from that file, so there is nothing "
            "to continue from"
        ) from exc


def _resume_inputs(
    config: PlatformConfig,
    state: dict,
    tracked: runs.TrackedRun,
    ref: RunDirRef,
    request: ResumeRequest,
    execution: Optional[str] = None,
) -> tuple:
    """The ``(taskdef, target, repo_url)`` the spawn carries, or refuse.

    Precedence for the taskdef is override, then the run's own state, then the
    index. The run's state beats the index for the same reason as in
    :mod:`~lmer_platform.answer`: ``run_state.seed_state`` makes both fields
    immutable for the life of the run and they are what the container derives its
    run dir from, so the recorded pair is the one that lands back on this run. The
    override beats both because it is the operator saying, now, what they want
    started — see the module docstring on where that lands.

    A missing taskdef is therefore only fatal without an override, which is what
    makes a bare adopted run (no recorded taskdef, nothing in the index) resumable
    by naming one. A missing target is fatal either way: the slug of a target-less
    run is the taskdef alone, which every other target-less run of that taskdef
    shares, and :meth:`SpawnRequest.validate` refuses one anyway.

    Both are re-checked here rather than only at :meth:`ResumeRequest.validate`,
    because the value that reaches this point may have come from the mirror — a
    repo every dev writes to, which the platform reads and must not simply trust.

    *execution* is the schema-2 record's uid (:func:`schema_two_record`), and it
    is what lifts the target refusal (spec §6.4, #537): that record is addressed
    by its uid, not by a slug its target would have to make unique, so an empty,
    ``reference`` or ``other`` target is resumable — given a transport URL for
    ``--repo`` to name the repository with, since that target cannot carry the
    launch itself. The URL is :func:`_repo_url_for`'s, whose order is the one
    the transport needs: the caller's, then the recorded clone URL, then
    :class:`RepoUrlRequired`. It is the third value, and ``None`` wherever the
    target carries the launch on its own — every legacy record among them —
    so the repo URL there is resolved after the landing checks, as it always was.
    """
    taskdef = request.taskdef or state.get("taskdef") or tracked.taskdef
    target = state.get("target") or tracked.target
    needs_transport = execution is not None and _target_needs_transport(target)
    missing = [
        name
        for name, value in (("taskdef", taskdef), ("target", target))
        if not (isinstance(value, str) and value.strip())
        and not (name == "target" and needs_transport)
    ]
    if missing:
        raise NotResumable(
            f"{ref.rel_path} records no {' and no '.join(missing)}, so the platform "
            "cannot work out what to start"
            + (
                " — name a taskdef to resume it with"
                if "target" not in missing else
                " — resume it from a shell with `lmer <taskdef> <target>`"
            )
        )
    taskdef = taskdef.strip()
    target = target.strip() if isinstance(target, str) else ""
    _reject_unsafe_taskdef(taskdef, NotResumable)
    _reject_argv_flag("target", target, NotResumable)
    repo_url = _repo_url_for(config, tracked, request) if needs_transport else None
    return taskdef, target, repo_url


def _target_needs_transport(target) -> bool:
    """Whether *target* cannot carry a launch without ``--repo`` (spec §6.5).

    The classes :func:`lmer_platform.spawn._needs_repo_flag` emits the flag for —
    no target, a document URL whose clone must come from the repository (#523),
    or an ``other`` URL — read through the one classifier
    (:func:`work_repo.utils.classify_target`), so this and the spawn cannot
    disagree about which targets those are.
    """
    return classify_target(target if isinstance(target, str) else None) in (
        "none", "reference", "other",
    )


def _launch_target(target: str) -> Optional[str]:
    """The target positional the spawn carries, or ``None`` for none.

    ``None`` for an empty target — an empty positional is a value argparse
    would take — and for a ``repository``-class one: a record written before
    spec §6.5 can carry a bare clone URL as its target, and that beside the
    ``repo_url`` every resume passes is "two repositories named" to
    :meth:`lmer_platform.spawn.SpawnRequest.validate`, so such a record could
    never be resumed. The repository is then the validated ``repo_url`` alone,
    on ``--repo``; ``derive_slug`` gives the bare taskdef for a repository
    target and for none, so the run lands where it did.
    """
    if classify_target(target or None) in ("none", "repository"):
        return None
    return target


def schema_two_record(
    config: PlatformConfig, state: dict, ref: RunDirRef, error=NotResumable
) -> Optional[str]:
    """The uid of a schema-2 execution record, or ``None`` for anything else.

    "A record with ``session.uid``" in spec §6.4's terms, in a mirror whose
    manifest is schema 2 — the manifest is read only once the record claims a
    uid, so a legacy record never reaches it and its refusals keep their order.
    Every rule the module docstring lists as lifted for schema 2 keys on this.

    The uid must be the one the record's directory is addressed by
    (:func:`lmer_platform.workrepo.record_session_id`), or the record is
    refused with *error* (answer passes its own): a record copied under
    another uid would be launched as ``--source-session <directory>`` while
    the reply and the event named the uid its state claims. A directory
    that is no session record at all is left to the refusal
    :func:`_schema_two_source` gives it.
    """
    session = state.get("session")
    uid = session.get("uid") if isinstance(session, dict) else None
    if not (isinstance(uid, str) and runs.UID_RE.match(uid)):
        return None
    if not _schema_two(config):
        return None
    addressed = record_session_id(ref)
    if addressed is not None and addressed != uid:
        raise error(
            f"{ref.rel_path} records session uid {uid}, but its directory is "
            f"{addressed} — a copied or hand-edited record, which the platform "
            "will not continue under either uid"
        )
    return uid


def _recorded_slug(state: dict, ref: RunDirRef) -> str:
    """The slug the run is resolved by: its recorded one, else its directory name.

    ``work name`` renames a run's directory while the state file keeps the original
    slug, and the container resolves runs by the **recorded** slug — so this, not
    the directory name, is what a derived slug has to be compared against. The
    directory name is the fallback for state that records no slug, the one case
    where nothing else can resolve it.
    """
    recorded = state.get("slug")
    if not (isinstance(recorded, str) and recorded.strip()):
        return ref.slug
    return recorded.strip()


def _landing_slug(
    taskdef: str, target: str, state: dict, ref: RunDirRef, request: ResumeRequest
) -> tuple:
    """``(slug, continued)`` for the run the session will file itself under.

    The container computes its own run dir from ``LMER_TASK``/``LMER_TASK_TARGET``
    through ``run_state.derive_slug``, so this is a prediction of a deterministic
    function rather than an intention — which is why the pair can be checked
    before anything is started.

    Equal to the recorded slug means this continues the run in front of the
    operator. Different means the session will seed or adopt *another* run against
    the same target, which is legitimate only as the thing the caller asked for. A
    mismatch with no override says the index and the run's state disagree — a
    hand-edited or mis-adopted entry — and silently starting a run nobody named is
    the surprise ``answer._require_same_run`` exists to prevent.
    """
    recorded = _recorded_slug(state, ref)
    derived = run_state.derive_slug(taskdef, target)
    if derived == recorded:
        return derived, True
    if run_state.has_vacated(state, derived):
        return derived, True
    if request.taskdef is None:
        raise NotResumable(
            f"{ref.rel_path}: resuming with its recorded taskdef {taskdef!r} and "
            f"target {target!r} derives run {derived!r}, not {recorded!r} — the "
            "session would land on a different run than the one being resumed, so "
            "it is refused. Pass a taskdef explicitly if starting another run "
            "against this target is what you meant"
        )
    return derived, False


def _execution_landing(
    taskdef: str, target: str, state: dict, ref: RunDirRef, request: ResumeRequest
) -> tuple:
    """``(slug, continued)`` for a schema-2 continuation — never a refusal.

    :func:`_landing_slug`'s prediction exists because a legacy session writes in
    whatever run its slug resolves to. A schema-2 one writes in a record of its
    own (the uid the spawn mints), so there is no run to land on by mistake:
    the group is whatever ``derive_slug`` says, which after the port's fold is
    the source's own (spec §6.4). ``continued`` still separates the ordinary
    continue from an override that starts different work — it is what decides
    whether the source's state governs the checks, and what the reply says.
    """
    derived = run_state.derive_slug(taskdef, target)
    continued = request.taskdef is None or derived == _recorded_slug(state, ref)
    return derived, continued


def _refuse_if_live(request: ResumeRequest, slug: str) -> None:
    """Refuse to start a second session for a run that already has one.

    Not the enforcement: :func:`lmer_platform.spawn.spawn_session` holds the
    one-run-one-session invariant for every spawn path, so deleting this would
    not open the hole back up. It is kept because it is a :class:`RunIsLive`,
    which is this route's error *body* rather than only its status — clients
    match on ``code`` (``live_session``) precisely so they need not read English —
    and because it fires before the mirror reads and the sibling resolution below.

    Liveness outranks committed run state (spec D24): the mirror can still say
    "yielded" while a session is already working, so this is the check that has to
    win over what the state file says. It also makes a double-tapped continue
    button harmless.

    Unlike :mod:`~lmer_platform.answer` this cannot run first — the run it is
    about is *derived*, from a taskdef and target that come out of the state file
    — so it runs as soon as the landing identity is known and before anything is
    spawned. The candidate slugs are the landing run's, plus the tracked name when
    a renamed run makes those differ: a session files itself under the recorded
    slug, and a check that knew only the tracked name would let a second container
    start for the run it names.
    """
    entry = live_session_for_run(request.host, request.project, slug)
    if entry is None:
        return
    raise RunIsLive(
        # The landing run's key, not the request's: those differ for an override
        # and for a renamed run, and neither is a directory name — see
        # :attr:`ResumeRequest.key`.
        f"{runs.run_key(request.host, request.project, slug)} already has a live "
        f"session ({entry.get('id')}, pid {entry.get('pid')}) — resuming would start a "
        "second container for one run, and the two would fight over its owner "
        f"claim. Type into that session instead (POST /api/sessions/"
        f"{entry.get('id')}/input), or wait for it to stop"
    )


def _landing_state(
    config: PlatformConfig,
    request: ResumeRequest,
    state: dict,
    ref: RunDirRef,
    started_slug: str,
    continued: bool,
) -> tuple:
    """``(state, ref)`` of the run the session lands in — ``(None, None)`` if new.

    For a continue that is the state already read. For an override it is the
    sibling's, when the sibling exists: a run that has never been seeded has
    recorded nothing that could refuse anything, and that is the ordinary case for
    ``develop`` → ``review``. When it *does* exist its state governs, because from
    the container's point of view this spawn is a resume of that run.

    The limit of that, since it is not obvious: a sibling whose *directory* has
    been renamed is not found here. The platform resolves a run dir by directory
    name (:func:`lmer_platform.workrepo.resolve_run_dir`) while the container
    resolves by recorded slug, so a renamed sibling reaches the container's own
    completed-run contract with no direction — which asks the operator rather than
    guessing. Worth knowing, not worth a mirror-wide scan on every resume.
    """
    if continued:
        return state, ref
    sibling = resolve_run_dir(config, request.host, request.project, started_slug)
    if sibling is None:
        return None, None
    return _load_state(sibling), sibling


def _refuse_if_question(state: dict, ref: RunDirRef) -> None:
    """Send a question-blocked run to the verb that can actually unblock it.

    Respawning it plain starts a session that reads the question back with no
    answer to it and leaves ``stop_reason=question`` in place — a slot spent, and
    the run as blocked as it was, while the operator believes they acted. The
    answer path exists precisely because the delivery has to ride along with the
    spawn (``--answer`` → ``LMER_ANSWER`` → ``work session-start``), and it cannot
    be bolted on afterwards from here.
    """
    if state.get("stop_reason") != "question":
        return
    raise QuestionOpen(
        f"{ref.rel_path} is stopped on a question, and a plain resume would leave "
        "it that way — the session would re-read the question with no answer and "
        "the stop would still be there. Answer it instead (POST /api/runs/answer), "
        "which clears the question as the session starts; or pass a taskdef to "
        "start different work against the same target"
    )


def _require_direction_if_finished(
    state: dict,
    ref: RunDirRef,
    request: ResumeRequest,
    continuation: Optional[tuple] = None,
) -> None:
    """A finished run reopens only on an explicit direction — see the docstring.

    Keyed on ``status`` rather than on the stop reason because that is what the
    container keys on (``run_state.decide`` → ``completed_run``), and because a
    terminal status outranks a stale stop everywhere else in the platform
    (``inventory._derive``).

    *continuation* is ``(uid, taskdef, target)`` for a schema-2 record, which is
    never reopened in place (spec D14, #437): the refusal is the same, and its
    words name the route that does work there — a new execution with the record
    as its source — instead of a reopen the record's writer rule would refuse.
    """
    status = state.get("status")
    if status not in _FINISHED_STATUSES or request.direction:
        return
    if continuation is not None:
        uid, taskdef, target = continuation
        # Scrubbed: this message reaches the UI and the assistant's transcript,
        # and a recorded target can carry a token.
        launch = " ".join(
            part for part in ("lmer", taskdef, _scrub_credentials(target or "")) if part
        )
        raise DirectionRequired(
            f"{ref.rel_path} is {status}, and a finished schema-2 record is never "
            "reopened in place — continue it as a new execution: "
            f"`{launch} --source-session {uid}` (or the platform's continue). "
            "Say what the new execution should do and it is passed as its seed; "
            "without one the session would only start up to ask whether you "
            "meant to"
        )
    raise DirectionRequired(
        f"{ref.rel_path} is {status}, and a finished run is only reopened on an "
        "explicit direction (docs/RUN-STATE.md §3) — say what the next session "
        "should do and it is passed as the run's seed: the session records it as "
        "the goal, reopens the run and works on it. Without one the session would "
        "only start up to ask whether you meant to"
    )


def _repo_url_for(
    config: PlatformConfig,
    tracked: runs.TrackedRun,
    request: ResumeRequest,
    verb: str = "resume",
) -> str:
    """The repo URL the spawn derives its run identity from, or refuse.

    Supplied beats recorded, which is the opposite of
    :func:`lmer_platform.answer._repo_url_for` and deliberate: this is the only
    path by which a wrong recorded URL can be corrected, since the spawn records
    what it was given and the corrected value then sticks.

    Whichever it is, it has to name the run's own host and project.
    ``derive_run_identity`` predicts the session's run from this URL, so one
    naming a different project files the session under a different run — a second
    row in the fleet view, the operator's row still pointing at the session that
    stopped, and (for an override) a sibling founded in the wrong project. The
    comparison is exact: this is not the place to guess that two spellings of a
    host are the same host.

    It is made through :func:`lmer_platform.spawn.derive_repo_identity` — the
    same function the spawn below will use, and *config* is here to carry the
    work repo's registry to it. For a project registered with ``remotes:`` the
    recorded URL and the tracked identity differ by design: one project is
    reachable at several URLs, its record is placed and listed under the primary
    (D8), so a run launched through a secondary is tracked under the primary
    while its ``repo`` stays the URL that was actually cloned
    (:func:`lmer_platform.workrepo.primary_remote_identity`). Comparing the URL's
    bare parse refused exactly those runs, and refused them by telling the
    operator their own repo named a different project. That is believing the
    registry where it SAYS two URLs are one project, not guessing that they are:
    a URL no entry claims, or any URL at all when the mirror cannot be read, is
    still held to naming the run's host and project exactly.

    Both sides go through the fold, not just the URL
    (:func:`lmer_platform.workrepo.folded_identity`). A row keyed on a SECONDARY
    — adopted by hand under the spelling the operator cloned from, or written by
    a spawn whose fold no-opped on a cold mirror — otherwise lost this comparison
    permanently, and lost it with a message telling the operator to supply a repo
    URL that does not exist.

    For a schema-2 record whose target cannot carry the launch, this URL is also
    the TRANSPORT — the one ``--repo`` clones from (:func:`_resume_inputs`) — and
    that is why only these two sources are ever consulted. The registry's
    ``repo:`` and the primary-remote fold are scheme-less identity; they sit on
    the right-hand side of the comparison below and nowhere else, and neither is
    a clone URL — so a URL that passes the comparison must also be shaped like
    one (:func:`_is_clone_url`), or the registry's own spelling, supplied or
    recorded, would pass as the transport by folding to the run's identity.
    A transport URL may carry credentials, so any refusal quotes it
    scrubbed (``_scrub_credentials``), and nothing here logs it.

    *verb* words the refusal for :mod:`lmer_platform.answer`, which resolves the
    same transport through here.
    """
    url = request.repo_url or tracked.repo
    if not url:
        raise RepoUrlRequired(
            f"{request.key} has no repo URL recorded — an adopted run knows "
            "its host, project and slug but not where its code is cloned from, and "
            "the platform will not invent one: the value is written into the "
            "tracked index as this run's repo and every later verb believes it. "
            f"Supply repo_url with the {verb}"
        )
    # BOTH sides folded, or the comparison is one a secondary-keyed row can
    # never win: the URL side moves to the primary and the tracked side does
    # not, so the resume is refused with advice — "supply a repo_url for
    # <secondary>" — that no URL can satisfy, because every spelling of that
    # project folds to the same primary. A no-op for a row already keyed on
    # the primary, which is every row a post-fold spawn writes.
    host, project = derive_repo_identity(url, config)
    if (host, project) != folded_identity(config, request.host, request.project):
        source = "supplied" if request.repo_url else "recorded for this run"
        raise RepoUrlRequired(
            f"the {source} repo URL {_scrub_credentials(url)!r} names "
            f"{host}/{project}, but the run is "
            f"{request.host}/{request.project} — the session would be filed under a "
            f"different run than the one being {verb.rstrip('e')}ed. Supply a "
            f"repo_url for {request.host}/{request.project}"
        )
    if not _is_clone_url(url):
        # After the comparison, so every refusal it gave keeps its words; this
        # one is for a value it lets through. The registry's scheme-less
        # identity spelling folds to the run's own project, and would then be
        # spawned as a ``--repo`` the host CLI cannot clone and recorded as the
        # run's repo for every later verb to trust.
        source = "supplied" if request.repo_url else "recorded for this run"
        raise RepoUrlRequired(
            f"the {source} repo URL {_scrub_credentials(url)!r} is not a clone URL "
            "— it has no scheme and is not scp form (user@host:path), so nothing "
            f"could clone from it. Supply a clone URL for {request.host}/"
            f"{request.project} as repo_url with the {verb}"
        )
    return url


#: A URL with a scheme, and git's scp form ``user@host:path``.
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_SCP_RE = re.compile(r"^[^\s/@:]+@[^\s/:]+:")


def _is_clone_url(url: str) -> bool:
    """Whether *url* is shaped like something git clones from: a scheme, or scp
    form — what ``resolve.is_likely_url`` and the classifier's ``repository``
    row accept as a URL. A scheme-less ``host/group/project`` is an identity
    spelling (the registry's ``repo:``), never a clone URL; which project a
    clone URL names is :func:`_repo_url_for`'s comparison, not this."""
    return bool(_SCHEME_RE.match(url) or _SCP_RE.match(url))


def _direction_args(direction: Optional[str]) -> tuple:
    """The ``extra_args`` carrying a direction, as one token.

    See :data:`DIRECTION_FLAG` for why it is one token and why it rides here
    rather than in a typed field.
    """
    if not direction:
        return ()
    return (f"{DIRECTION_FLAG}={direction}",)


def _landing_session_id(
    landing_ref: Optional[RunDirRef], continued: bool
) -> Optional[str]:
    """The record the session must write in, or ``None`` to let it mint one.

    ``LMER_SESSION_ID`` decides which ``_runs/<slug>/<session-id>/`` a launch
    writes its record in (spec D22), and the container mints one at startup only
    when the host injected none. So without this a resume READS one record and
    LANDS in a brand-new one beside it: every refusal above measured against a
    launch the session then never touched, and a second record in the group with
    nothing in it but a fresh brief. The id passed is therefore the id of the ref
    the checks were made against — ``landing_ref``, which for a continue is the
    source ``ref`` — and not, say, the newest launch re-resolved here, which is
    the very divergence :attr:`ResumeRequest.session_id` exists to close.

    **Only for a continue.** With an overridden taskdef the session files itself
    under a different slug (the module docstring's "continue vs sibling"), and the
    source record's id names nothing in that other group: injecting it would open
    a record in group B addressed by a launch of group A — a worse lie than the
    one this fixes, and one no later reader could untangle. There the container
    mints its own id, exactly as it did before this existed.

    ``None`` also for a landing ref that is not a session record at all — a legacy
    run dir, or an unmigrated record at the grouping level. Both are addressed by
    their slug rather than by a launch (:func:`lmer_platform.workrepo.
    record_session_id`), and a legacy resume is byte-identical to what it was.
    """
    if not continued or landing_ref is None:
        return None
    return record_session_id(landing_ref)


def _schema_two(config: PlatformConfig) -> bool:
    """Whether the mirror uses fresh execution records.

    :func:`lmer_platform.spawn.schema_two` is the one manifest test — the spawn
    below asks it the same question to decide whether to mint a uid, and two
    readers could answer differently. Only the refusal is this module's: an
    unreadable manifest is a :class:`NotResumable` (409, the mirror may be fixed
    by the next pull), worded for the record being selected.
    """
    try:
        return schema_two(config)
    except SpawnError as exc:
        cause = exc.__cause__ if exc.__cause__ is not None else exc
        raise NotResumable(
            f"the host mirror manifest cannot be read ({cause}); refusing to "
            "guess whether the selected session is a source or a writer"
        ) from exc


def _source_provenance(
    ref: RunDirRef, state: dict, source_uid: str
) -> tuple[dict, bool]:
    """Return visible launch facts and whether the source is stopped.

    Stopped is :func:`work_repo.session_record.is_stopped`: signed out, or a
    record ``work seed`` left that nothing ever started in (spec D13). No
    session could be writing it, so there is nothing for an unsigned
    acknowledgement to accept. It is that module's one shared rule, called
    rather than restated.
    """
    events = run_state.read_events(ref.path, last_n=0)
    metadata = state.get("session") if isinstance(state.get("session"), dict) else {}
    def stamp(value) -> str:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return str(value)

    provenance = {
        "uid": source_uid,
        "operator": metadata.get("operator", "?"),
        "hostname": metadata.get("hostname", "?"),
        "started_at": stamp(metadata.get("started_at", state.get("created", "?"))),
        "last_activity": stamp(
            events[-1].get("ts", "?") if events else state.get("updated", "?")
        ),
    }
    return provenance, session_record.is_stopped(state, events)


def _schema_two_source(
    config: PlatformConfig, request: ResumeRequest, ref: RunDirRef, state: dict
) -> tuple[Optional[str], Optional[str]]:
    """Validate and return ``(source UID, exact unsigned acknowledgement)``.

    A seeded source needs no acknowledgement (spec §5.2): it is a valid
    continuation source the moment ``work seed`` publishes it, and the launch
    line that consumes it carries no ``--allow-unsigned-source``.
    """
    if not _schema_two(config):
        return None, None
    source_uid = record_session_id(ref)
    if not source_uid:
        raise NotResumable(
            f"{ref.rel_path} is not an execution record, so it cannot be selected "
            "as schema-2 read-only context"
        )
    if (
        request.allow_unsigned_source is not None
        and request.allow_unsigned_source != source_uid
    ):
        raise ResumeError(
            "allow_unsigned_source must exactly match the selected source UID "
            f"{source_uid}"
        )
    provenance, stopped = _source_provenance(ref, state, source_uid)
    if not stopped and request.allow_unsigned_source != source_uid:
        raise UnsignedSessionSource(
            f"source session {source_uid} has no recorded final session_end "
            f"(operator={provenance['operator']}, host={provenance['hostname']}, "
            f"started={provenance['started_at']}, "
            f"last_activity={provenance['last_activity']}). Choose cancel/wait, "
            "inspect the prior session separately, or explicitly continue fresh; "
            "the source will remain read-only and will not be signed out or rewritten.",
            provenance,
        )
    acknowledgement = source_uid if not stopped else None
    return source_uid, acknowledgement


@run_operation
def resume_run(config: PlatformConfig, request: ResumeRequest) -> ResumeResult:
    """Continue a tracked run by starting its next session.

    Raises :class:`ResumeError` (with a status and a code) when the session must
    not be started, and lets :class:`lmer_platform.spawn.CapacityError` /
    :class:`~lmer_platform.spawn.SpawnError` through unchanged — the concurrency
    cap belongs to the spawn path, and continuing a run is not a reason to exceed
    it.

    The order is forced by what has to be known before what: the landing run is
    *derived* from the source run's recorded taskdef and target, so the mirror is
    read before any check about liveness or status can be made at all. Nothing is
    started until every one of them has passed.
    """
    request = request.validate()
    tracked = _require_tracked(request)
    request = _bind_to_tracked(request, tracked)
    ref = _require_run_dir(config, request)
    state = _load_state(ref)
    source_session_id, unsigned_source_ack = _schema_two_source(
        config, request, ref, state
    )
    # The schema-2 execution this record is, when it is one — the switch for
    # every slug-era rule the module docstring lists as lifted.
    execution = (
        schema_two_record(config, state, ref) if source_session_id is not None else None
    )
    taskdef, target, transport = _resume_inputs(
        config, state, tracked, ref, request, execution
    )

    if execution is not None:
        started_slug, continued = _execution_landing(taskdef, target, state, ref, request)
        # No live-session refusal: a second continuation of this source is a
        # fork, and the spawn's reply says who else is on the project instead.
        landing_state, landing_ref = (state, ref) if continued else (None, None)
    else:
        started_slug, continued = _landing_slug(taskdef, target, state, ref, request)

        _refuse_if_live(request, started_slug)
        if continued and started_slug != request.slug:
            # The run was renamed: it is tracked under its directory name while
            # any session for it registers under the recorded slug, so the check
            # above did not look at the identity the operator named.
            _refuse_if_live(request, request.slug)

        landing_state, landing_ref = _landing_state(
            config, request, state, ref, started_slug, continued
        )
    if landing_state is not None:
        _refuse_if_question(landing_state, landing_ref)
        _require_direction_if_finished(
            landing_state, landing_ref, request,
            (execution, taskdef, target) if execution is not None else None,
        )

    repo_url = transport or _repo_url_for(config, tracked, request)

    result = spawn_session(
        config,
        SpawnRequest(
            **resume_selection(request.key, request.slot),
            taskdef=taskdef,
            # An empty- or repository-target record launches on ``--repo``.
            target=_launch_target(target),
            repo_url=repo_url,
            # The record every refusal above was measured against, so a continue
            # lands in the launch the operator asked to continue rather than in a
            # fresh one beside it — and nothing for an override, which lands in
            # another group entirely. See :func:`_landing_session_id`.
            session_id=(
                None if source_session_id is not None
                else _landing_session_id(landing_ref, continued)
            ),
            source_session_id=source_session_id,
            allow_unsigned_source=unsigned_source_ack,
            extra_args=_direction_args(request.direction),
            # A continue founds a new uid-keyed run, or for a renamed legacy run
            # files its session under the recorded slug; the spawn mutes that
            # key before it is published (#458). Never an override, a different
            # run.
            mute_from=(
                None if not continued
                else mute.source_of(request.host, request.project, execution)
                if execution is not None
                else mute.source_of(request.host, request.project, request.slug)
                if started_slug != request.slug else None
            ),
        ),
    )

    if continued and execution is None:
        # Best-effort, and the ordering is the reason: the session is already
        # running, so a bookkeeping failure reported as a failed resume would send
        # the operator to resume again — which the live-session check would then
        # refuse. A stale ``last_session_id`` is a worse view, not a lost session.
        #
        # Only for a continue. The spawn has already tracked the run it derived
        # (``runs.track``, source ``spawned``), which for an override is the
        # sibling; pointing the *source* run's detail view at a session belonging
        # to another run would be a worse lie than the one this fixes.
        #
        # And never for a schema-2 record (#229): the continuation is its own
        # uid-keyed row, tracked by the spawn under the uid it minted, and the
        # source row keeps the session that wrote it.
        try:
            runs.note_session(
                request.host, request.project, request.slug, result.session_id
            )
        except StoreError as exc:
            logger.warning(
                "platform_resume_index_not_updated run=%s session=%s error=%s — the "
                "session is already running; the run's detail view will keep "
                "pointing at the previous session",
                request.key, result.session_id, exc,
            )

    # Length, not text: the platform's history is a debugging artifact people read
    # and paste, and a direction is the operator's content.
    run = {"host": request.host, "project": request.project, "slug": request.slug}
    started = {"host": request.host, "project": request.project, "slug": started_slug}
    # Schema 2 only, so a legacy event keeps its shape: there the slug is a
    # group label, and only the uids say which execution was read and which
    # one the session founds.
    if execution is not None:
        run["uid"] = execution
        if result.uid is not None:
            started["uid"] = result.uid
    append_event(
        "run_resumed",
        note=request.key,
        data={
            "run": run,
            "started": started,
            "continued": continued,
            "taskdef": taskdef,
            "session": result.session_id,
            "direction_chars": len(request.direction or ""),
        },
    )
    logger.info(
        "platform_run_resumed run=%s started=%s continued=%s session=%s "
        "direction_chars=%d",
        request.key, started_slug, continued, result.session_id,
        len(request.direction or ""),
    )

    return ResumeResult(
        host=request.host,
        project=request.project,
        slug=request.slug,
        taskdef=taskdef,
        target=target,
        continued=continued,
        started_slug=started_slug,
        session=result,
        # The landing run's record, which is the one every check above was made
        # against: the source run for a continue, the sibling for an override, and
        # nothing at all for an override that founds a run.
        rel_path=landing_ref.rel_path if landing_ref is not None else None,
        uid=execution,
    )
