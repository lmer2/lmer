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

A finished run reopens on a direction, not on a click
-----------------------------------------------------
``complete``/``archived`` is neither a hard refusal here nor a silent reopen —
both are wrong, in opposite directions. The run-state contract already decides it
(docs/RUN-STATE.md §3, issue #96): a session that resolves a finished run does not
re-seed "and never silently resumes either". *With* a seed direction
(``LMER_START_PROMPT``, which is what ``lmer --prompt`` sets) it records the seed
as the goal, reopens with ``work state set --status=in-progress``, and proceeds on
it; *without* one it asks the user whether this is a new target or a continuation,
and ends the session if the question goes unanswered.

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
  this is also what makes a double-tapped button harmless.
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

import logging
from dataclasses import dataclass, replace
from typing import Optional

from lmer_cli.cli import _parse_repo_url
from work_repo import run_state

from . import runs
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
    _load_state as _load_state_or_refuse,
    _reject_traversal,
)
from .config import PlatformConfig
from .spawn import (
    SpawnRequest,
    SpawnResult,
    live_session_for_run,
    spawn_session,
)
from .store import StoreError, append_event
from .workrepo import RunDirRef, resolve_run_dir

logger = logging.getLogger("lmer_platform.resume")

__all__ = [
    "ResumeError", "RunNotTracked", "NotResumable", "RunIsLive", "QuestionOpen",
    "RepoUrlRequired", "DirectionRequired", "DIRECTION_FLAG",
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


class RunIsLive(NotResumable):
    """A session is already working the run a resume would land on."""

    code = "live_session"


class QuestionOpen(NotResumable):
    """The run is stopped on a question, which is the answer verb's job."""

    code = "question_open"


@dataclass(frozen=True)
class ResumeRequest:
    """Which run to continue, and the three things a caller may say about it.

    ``taskdef`` is the override: ``None`` (or blank) means the run's recorded one,
    which is the one-tap continue. Anything else starts the sibling run described
    in the module docstring.

    ``repo_url`` and ``direction`` exist to satisfy :class:`RepoUrlRequired` and
    :class:`DirectionRequired` — the two refusals whose remedy is a second request
    with one more field, which is why they are fields rather than flags.
    """

    host: str
    project: str
    slug: str
    taskdef: Optional[str] = None
    repo_url: Optional[str] = None
    direction: Optional[str] = None

    def validate(self) -> "ResumeRequest":
        """Return a normalised copy, or raise :class:`ResumeError`.

        Normalising rather than merely checking, because the identity is used
        twice with different tolerances — :func:`lmer_platform.runs.run_key`
        strips, a filesystem path does not — and a value that matched the index
        but not the mirror would surface as a baffling "not in the mirror".

        Blank optional fields normalise to ``None`` rather than to a refusal: all
        three are text inputs in a form the ordinary continue submits empty, and
        an empty box means "I did not say", not "I said nothing".
        """
        fields = {}
        for name in ("host", "project", "slug"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ResumeError(f"{name} is required")
            fields[name] = value.strip()
        _reject_traversal_or_refuse(fields, ResumeError)

        return replace(
            self,
            taskdef=_clean_taskdef(self.taskdef),
            repo_url=_clean_text("repo_url", self.repo_url),
            direction=_clean_direction(self.direction),
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
        """
        return runs.run_key(self.host, self.project, self.slug)


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
        either reply. It is ``None`` on every path a resume can reach today and
        that is not an accident: :func:`_repo_url_for` refuses unless the URL parses
        back to *this* run's host and project, which is precisely the derivation
        :func:`lmer_platform.spawn._untracked_run_warning` fires on failing. Present
        anyway, because the alternative is a field that silently stops existing on
        one of two sibling routes the day that guarantee is loosened.
        """
        return {
            "run": {"host": self.host, "project": self.project, "slug": self.slug},
            "started": {
                "host": self.host,
                "project": self.project,
                "slug": self.started_slug,
            },
            "continued": self.continued,
            "taskdef": self.taskdef,
            "target": self.target,
            "session": {
                "session_id": self.session.session_id,
                "pid": self.session.pid,
                "control": {
                    "host": self.session.control_host,
                    "port": self.session.control_port,
                },
            },
            "warning": self.session.warning,
            "note": (
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


def _require_tracked(request: ResumeRequest) -> runs.TrackedRun:
    """The tracked entry for the run, or refuse.

    Scope is the local index (spec D25). A run this orchestrator neither spawned
    nor adopted may well be a colleague's, and starting a container for it is not
    a decision to make on their behalf — adopting it first is how an operator says
    it is theirs.
    """
    entry = runs.get_tracked(request.host, request.project, request.slug)
    if entry is None:
        raise RunNotTracked(
            f"{request.key} is not tracked by this orchestrator — adopt it "
            "first (POST /api/runs/adopt) so it is in scope, then resume it"
        )
    return entry


def _require_run_dir(config: PlatformConfig, request: ResumeRequest) -> RunDirRef:
    """The run's directory in the host mirror, or refuse.

    A resume is defined by what the run already recorded — its taskdef and target
    — so without the dir there is nothing to continue *from*, and a spawn made on
    assumptions is how one lands on a different run.
    """
    ref = resolve_run_dir(config, request.host, request.project, request.slug)
    if ref is None:
        raise NotResumable(
            f"{request.key} is not in the host mirror, so the platform cannot "
            "tell what resuming it would continue — it may not have been pushed "
            "yet, the mirror may be stale (rescan to refresh), or the run dir may "
            "have been renamed since it was tracked"
        )
    return ref


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
    state: dict, tracked: runs.TrackedRun, ref: RunDirRef, request: ResumeRequest
) -> tuple:
    """The ``(taskdef, target)`` the spawn carries, or refuse.

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
    """
    taskdef = request.taskdef or state.get("taskdef") or tracked.taskdef
    target = state.get("target") or tracked.target
    missing = [
        name
        for name, value in (("taskdef", taskdef), ("target", target))
        if not (isinstance(value, str) and value.strip())
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
    taskdef, target = taskdef.strip(), target.strip()
    _reject_unsafe_taskdef(taskdef, NotResumable)
    _reject_argv_flag("target", target, NotResumable)
    return taskdef, target


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
    state: dict, ref: RunDirRef, request: ResumeRequest
) -> None:
    """A finished run reopens only on an explicit direction — see the docstring.

    Keyed on ``status`` rather than on the stop reason because that is what the
    container keys on (``run_state.decide`` → ``completed_run``), and because a
    terminal status outranks a stale stop everywhere else in the platform
    (``inventory._derive``).
    """
    status = state.get("status")
    if status not in _FINISHED_STATUSES or request.direction:
        return
    raise DirectionRequired(
        f"{ref.rel_path} is {status}, and a finished run is only reopened on an "
        "explicit direction (docs/RUN-STATE.md §3) — say what the next session "
        "should do and it is passed as the run's seed: the session records it as "
        "the goal, reopens the run and works on it. Without one the session would "
        "only start up to ask whether you meant to"
    )


def _repo_url_for(tracked: runs.TrackedRun, request: ResumeRequest) -> str:
    """The repo URL the spawn derives its run identity from, or refuse.

    Supplied beats recorded, which is the opposite of
    :func:`lmer_platform.answer._repo_url_for` and deliberate: this is the only
    path by which a wrong recorded URL can be corrected, since the spawn records
    what it was given and the corrected value then sticks.

    Whichever it is, it has to parse back to the run's own host and project.
    ``derive_run_identity`` predicts the session's run from this URL, so one
    naming a different project files the session under a different run — a second
    row in the fleet view, the operator's row still pointing at the session that
    stopped, and (for an override) a sibling founded in the wrong project. The
    comparison is exact: this is not the place to guess that two spellings of a
    host are the same host.
    """
    url = request.repo_url or tracked.repo
    if not url:
        raise RepoUrlRequired(
            f"{request.key} has no repo URL recorded — an adopted run knows "
            "its host, project and slug but not where its code is cloned from, and "
            "the platform will not invent one: the value is written into the "
            "tracked index as this run's repo and every later verb believes it. "
            "Supply repo_url with the resume"
        )
    host, project = _parse_repo_url(url)
    if (host, project) != (request.host, request.project):
        source = "supplied" if request.repo_url else "recorded for this run"
        raise RepoUrlRequired(
            f"the {source} repo URL {url!r} names {host}/{project}, but the run is "
            f"{request.host}/{request.project} — the session would be filed under a "
            "different run than the one being resumed. Supply a repo_url for "
            f"{request.host}/{request.project}"
        )
    return url


def _direction_args(direction: Optional[str]) -> tuple:
    """The ``extra_args`` carrying a direction, as one token.

    See :data:`DIRECTION_FLAG` for why it is one token and why it rides here
    rather than in a typed field.
    """
    if not direction:
        return ()
    return (f"{DIRECTION_FLAG}={direction}",)


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
    ref = _require_run_dir(config, request)
    state = _load_state(ref)
    taskdef, target = _resume_inputs(state, tracked, ref, request)
    started_slug, continued = _landing_slug(taskdef, target, state, ref, request)

    _refuse_if_live(request, started_slug)
    if continued and started_slug != request.slug:
        # The run was renamed: it is tracked under its directory name while any
        # session for it registers under the recorded slug, so the check above did
        # not look at the identity the operator named.
        _refuse_if_live(request, request.slug)

    landing_state, landing_ref = _landing_state(
        config, request, state, ref, started_slug, continued
    )
    if landing_state is not None:
        _refuse_if_question(landing_state, landing_ref)
        _require_direction_if_finished(landing_state, landing_ref, request)

    repo_url = _repo_url_for(tracked, request)

    result = spawn_session(
        config,
        SpawnRequest(
            taskdef=taskdef,
            target=target,
            repo_url=repo_url,
            extra_args=_direction_args(request.direction),
        ),
    )

    if continued:
        # Best-effort, and the ordering is the reason: the session is already
        # running, so a bookkeeping failure reported as a failed resume would send
        # the operator to resume again — which the live-session check would then
        # refuse. A stale ``last_session_id`` is a worse view, not a lost session.
        #
        # Only for a continue. The spawn has already tracked the run it derived
        # (``runs.track``, source ``spawned``), which for an override is the
        # sibling; pointing the *source* run's detail view at a session belonging
        # to another run would be a worse lie than the one this fixes.
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
    append_event(
        "run_resumed",
        note=request.key,
        data={
            "run": {
                "host": request.host,
                "project": request.project,
                "slug": request.slug,
            },
            "started": {
                "host": request.host,
                "project": request.project,
                "slug": started_slug,
            },
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
    )
