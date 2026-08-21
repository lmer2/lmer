"""Spawning lmer sessions the platform owns.

What a spawn has to accomplish
------------------------------
Six things, and every one of them matters to a later slice:

1. **Run under a PTY.** ``lmer`` only passes ``-it`` to docker/podman when its own
   stdin is a TTY, and the harness inside expects a terminal. Nobody types into
   this one — it exists so a non-headless session runs normally. Same reasoning as
   :mod:`slack_chat.sessions`, whose spawn this mirrors.
2. **Tee the PTY to disk, continuously.** This is not optional bookkeeping: an
   undrained PTY blocks the child once the terminal buffer fills. The file it
   writes deliberately outlives the container, and it is the scrollback source
   (spec D16) for every session that does not write its own — see point 8, which
   is what demoted this one from *the* log to *a* log.
3. **Expose the session's control plane**, unconditionally: every
   orchestrator-spawned session runs with ``--fastapi`` (spec D8). Without it a
   running session is read-only — the PTY log can be replayed but nothing can be
   typed into it — so the platform could never answer a session's question.
4. **Register the session**, so the fleet view can find it, and clear the entry on
   a clean exit — a leftover entry with a dead PID is how a crash is detected
   (:mod:`lmer_platform.inventory`), so it must mean something.
5. **Track the run**, because the local index is what scopes the view (spec D25). A
   spawned run the platform failed to record would be invisible in its own UI.
6. **Land the harness transcript on a host path**, by mounting a per-session
   directory in as the harness's projects dir. ``lmer`` runs its container with
   ``--rm`` and nothing bind-mounts ``~/.claude``, so without this the JSONL the
   chat view reads (:mod:`lmer_platform.transcripts`) lives in the container
   overlay and dies with it — the view was built against a source that had no
   data. The same reason the PTY log is teed out, and it comes with the same
   obligation: what outlives the container gets scrubbed and locked down.
7. **Give the session a way to ask its operator something**, by mounting a
   second per-session directory as its ask channel and naming it in the child's
   environment (:mod:`lmer_platform.ask`, spec D26/D27). A mount rather than a
   URL: the platform binds on the host, which is not reachable from inside the
   container without a runtime-specific gateway address, and a URL would mean
   putting the shared secret in every session. The environment variable is also
   how an agent *tells* it was orchestrated at all.
8. **Offer the session somewhere to keep its own log**, by mounting a third
   per-session directory (:func:`container_log_dir_for`). The tee in point 2 is
   written by a thread in *this* process holding the PTY master — an fd, not a
   path — so it dies with the daemon and nothing can re-open it: every daemon
   restart used to cost the fleet its live scrollback and leave T36 recovering
   what it could over the control plane. A log written by the supervisor inside
   the container has no such dependency. It is an *offer* rather than a
   requirement because the supervisor that writes it comes from the session
   image, not from this checkout: an older image leaves the directory empty, and
   the read path takes that for the answer it is
   (:func:`lmer_platform.session_io.canonical_log`).

Predicting the run identity
---------------------------
The run dir is named by the container, but its name is *derivable*: the slug from
``run_state.derive_slug(taskdef, target)`` and the host/project from the repo URL
via ``cli._parse_repo_url``. Both are deterministic, so the platform computes the
identity up front and tracks the run before the container has committed anything.
Without that, a session's first minutes — precisely when someone is watching —
would show a row with no identity.

Which repo URL, though, is a question with four answers, and the last two are why
``lmer develop <MR-url>`` needs no repo flag on the command line: **the target
usually contains the repository**. So a spawn that was handed no URL and has no
``LMER_REPO_URL`` to fall back on uses a plain repository-URL target as the run's
repository of record, or derives identity from a resource target such as an issue
or merge request (:func:`_identity_url_from_target`). The distinction is evidence:
the former is the clone target the caller supplied, while the latter is a
reconstruction. A reconstructed URL remains *identity only* (see
:func:`_repo_urls`) so a run never acquires a clone URL nobody supplied.

The residual case survives — a target that is a branch name or a sentence names
no repository, and nothing can be derived from it — so it is said out loud at
spawn time, in the log *and* in the spawn's own result
(:func:`_untracked_run_warning`, :attr:`SpawnResult.warning`). A row that
disappears when the session exits is not a thing to leave to a log line.

Naming the run it just tracked
------------------------------
A spawn may carry the ``title`` — and ``description`` — this orchestrator files
the run under, so a fleet started on the operator's behalf does not read back as a
list of slugs. Neither is an ``lmer`` flag and neither ever reaches the child:
they are platform metadata, written *after* :func:`lmer_platform.runs.track`
through :func:`lmer_platform.meta.write`, which owns the one-line collapse and the
length bound. A second copy of that validation here would be a second opinion
about a field the operator can also edit by hand.

The ordering all follows from the container already running by the time the write
is reachable, which makes it the last thing in a spawn that can fail and the least
valuable thing in it. So it never fails the spawn: a run whose identity could not
be derived has nothing to attach a note to and says so in the warning that already
reports the lost tracking (:func:`_untracked_run_warning`), and text ``meta``
refuses comes back as a warning of its own (:func:`_write_run_meta`). A session
killed over a label is the worse outcome in every one of those cases.

Minting the control plane's port and token here
-----------------------------------------------
``lmer --fastapi`` is perfectly capable of choosing its own port and generating
its own token, but then the only place either value exists is the child's stdout,
and the platform would have to scrape the PTY log for them. That makes
reachability depend on banner text, and it makes it *late*: the registry entry is
written the moment the child starts, and an entry whose ``control`` block fills in
"eventually" is an entry the UI cannot dial. So the platform decides both up front
and hands them down through ``LMER_FASTAPI_PORT`` / ``LMER_FASTAPI_TOKEN``.

The token then has to live somewhere the entry does not: registry files get
``cat``'d into tickets, so the entry carries only a ``token_ref`` path and
:func:`registry.register` refuses an inline one outright (spec §6.2).

Threads, not asyncio
--------------------
The API handlers are synchronous (they run in Starlette's threadpool), so a
session is a ``Popen`` plus one daemon thread draining its PTY. The Slack listener
uses asyncio because it *is* an asyncio program; adopting that here would mean
running a loop inside threadpool workers for no gain.
"""

from __future__ import annotations

import json
import logging
import os
import pty
import secrets
import shutil
import subprocess
import threading
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Callable, Optional
from urllib.parse import urlparse

from lmer_cli.cli import _derive_repo_url_from_task_target, _parse_repo_url
from lmer_cli.container.clone_and_exec import _scrub_credentials
from lmer_cli.container.sources import url_has_embedded_credential
from lmer_cli.harness import HARNESSES, known_harnesses
from lmer_cli.mounts import (
    # Imported rather than restated: credential staging (there) and the
    # session-dir acceptance rule (here) must read one literal.
    CONTAINER_HOME,
    CONTAINER_MOUNT_STAGING_DIR,
    MOUNT_LINKS_ENV,
    format_mount_links,
)
from lmer_cli.supervisor import (
    CONTAINER_SESSION_LOG_DIR,
    DEFAULT_PORT_RANGE,
    SESSION_LOG_NAME,
    _pick_port,
)
from lmer_cli.service import ServiceError
from lmer_cli.user_harnesses import CONTAINER_HARNESSES_DIR
from work_repo import run_state

from . import ask, meta, registry, runs, slots
from .config import ENV_REPO_URL, PlatformConfig, configured_repo_url
from .store import StoreError, append_event, logs_dir, utc_now_iso
from . import store

logger = logging.getLogger("lmer_platform.spawn")

__all__ = [
    "SpawnError", "CapacityError", "RunAlreadyLive", "SlotOccupied",
    "live_session_for_run",
    "SpawnRequest", "SpawnResult", "ANSWER_FLAG",
    "wait_for_exit_recorded",
    "PRESET_FLAG", "AGENTS_FLAG", "MODEL_FLAG", "NO_REPO_ENV",
    "resolve_lmer_bin", "derive_run_identity", "spawn_session", "log_path_for",
    "token_file_for", "read_control_token", "transcript_dir_for", "ask_dir_for",
    "container_log_dir_for", "container_log_path_for",
]

#: How much of the PTY to read per drain iteration.
_DRAIN_CHUNK = 4096

#: Where a session's control plane is reachable from. Loopback only — the bearer
#: token is the whole authorization story, so the endpoint must not be exposed
#: beyond this host. ``lmer`` publishes the container's port to exactly this
#: address, which is why the platform can record it before the child binds.
_CONTROL_HOST = "127.0.0.1"

#: Entropy for a control token, matching what the supervisor generates when it
#: mints its own — a platform-minted token must not be the weaker one.
_CONTROL_TOKEN_BYTES = 32

#: How many times to re-draw a control port that a live session already claimed.
#: See :func:`_pick_control_port`.
_PORT_PICK_ATTEMPTS = 8

#: The flag ``lmer`` reads an answer from, spelled by :func:`_build_command` from
#: :attr:`SpawnRequest.answer`. Lives here rather than in
#: :mod:`lmer_platform.answer` because this module both emits it and refuses it in
#: ``extra_args``; that module re-exports it.
ANSWER_FLAG = "--answer"

#: The flag ``lmer`` selects a named startup preset with, spelled by
#: :func:`_build_command` from :attr:`SpawnRequest.preset`. The name is resolved
#: **host-side** against ``LMER_PRESETS_FILE`` (:mod:`lmer_cli.presets`), so what
#: it selects is a local checkout to mount, a service container to target, extra
#: environment and extra ``lmer`` flags — a launch-shaping instrument, and the one
#: the session's registry entry records under ``task.preset``.
PRESET_FLAG = "--preset"

#: The flag ``lmer`` reads the agent fan-out selection from (issue #130): a
#: comma-delimited list of *preset names* the session's agent may hand a task to
#: via ``spawn-harness``. Resolved host-side against the same presets file — the
#: file never crosses into the container, so this list is the complete set of
#: configurations the session can ever spawn a child with. Spelled by
#: :func:`_build_command` from :attr:`SpawnRequest.agents` and recorded under
#: ``task.agents``.
AGENTS_FLAG = "--agents"

#: The flag ``lmer`` takes the session's model name from (T51), spelled by
#: :func:`_build_command` from :attr:`SpawnRequest.model`. Host-side it becomes
#: the session's ``LMER_LLM_NAME``: forwarded into the container, handed to the
#: harness's own model flag by the runner scripts, and — when nothing names a
#: harness — the hint that picks which harness runs at all
#: (:func:`lmer_cli.harness.harness_for_model`). Recorded under ``task.model``,
#: which is also where the session's *own* report of the model it resolved
#: lands (:func:`absorb_ports`) — so a run this platform named no model for
#: still says what drove it, whenever the session settled on a name at all.
MODEL_FLAG = "--model"

#: The flag ``lmer`` takes the agent CLI from (``claude``, ``codex``, …), spelled
#: by :func:`_build_command` from :attr:`SpawnRequest.harness`. Named here for the
#: same reason as the three above: the platform emits it from a typed field and
#: the registry entry records what it emitted, so a second spelling appended to
#: ``extra_args`` would win on argparse's last-wins rule and leave the entry, the
#: fleet view and every post-mortem naming a harness the session never ran.
HARNESS_FLAG = "--harness"

#: How ``lmer`` is told a session deliberately has no repository (spec D17).
#: Passed in the child's environment rather than as a flag because that is where
#: ``lmer`` reads it — the host CLI skips repo resolution on it and the
#: container's ``clone_and_exec`` skips the workspace clone, so the session has
#: nothing to edit as a matter of what exists rather than of what it was told.
#: Set from :attr:`SpawnRequest.no_repo` and, when that is false, actively
#: removed: see :func:`spawn_session`.
NO_REPO_ENV = "LMER_NO_REPO"

#: ``lmer`` flags the platform reserves for itself, refused in ``extra_args``,
#: which arrives verbatim from the ``POST /api/sessions`` body — reachable input
#: rather than a caller mistake, hence a refusal rather than a note in the docs.
#: The first four would move, weaken or delete the control plane the session's
#: registry entry advertises, leaving an entry that promises a session nobody can
#: drive; the fifth belongs to a verb that validates before it spawns; the last
#: three are flags this module emits itself, from typed fields, and records.
_RESERVED_ARGS = (
    # Would replace the token minted here and written 0600, so the entry's
    # token_ref would stop opening anything.
    "--fastapi-token",
    # The container-side bind address. ``lmer`` sets 0.0.0.0 there precisely so
    # the published port reaches the endpoint; anything else maps the host port
    # onto a socket nothing is listening on.
    "--fastapi-host",
    # Names a range to pick a port from after the platform has already committed
    # to one and recorded it. Inert while LMER_FASTAPI_PORT wins, which is
    # exactly the kind of thing that stops being inert quietly.
    "--fastapi-port-range",
    # Bypasses the supervisor entirely: nothing serves /input, while the entry
    # still says the session is reachable.
    "--no-supervisor",
    # An answer is a verb, not an argument. POST /api/runs/answer checks that a
    # question is actually open, that the respawn derives the same run, and that
    # no live session is already working it (lmer_platform.answer); a raw spawn
    # checks none of that. Spelling the flag here would smuggle an answer past
    # every one of those refusals and, in the cases they name, start a second
    # container for one run or drop the answer. The answer path does not come
    # through here: it sets SpawnRequest.answer, which is a typed field rather
    # than caller-shaped argv.
    ANSWER_FLAG,
    # The last four are reserved for a different reason from the rest, and it
    # is not that they are dangerous to a session: the platform *emits* all
    # four, from typed fields, and writes what it emitted into the registry
    # entry (``task.preset`` / ``task.agents`` / ``task.model`` /
    # ``task.harness``).
    # ``extra_args`` lands after them in argv and argparse is last-wins, so a
    # second spelling does not collide with the platform's — it silently beats
    # it, and the entry then names a preset the session never applied, a fan-out
    # roster it never got, or a model that is not the one driving it. The first
    # two also select real capability out of the host's presets file (a checkout
    # to mount, a service container to target, the harnesses a spawned child may
    # run), which is the kind of thing that has to arrive as a field the
    # platform can see, validate and record rather than as text appended to a
    # command line.
    PRESET_FLAG,
    AGENTS_FLAG,
    # ``--model`` earns its place on the recording argument alone, and it is
    # sharper here than for the other two: the model is the one launch fact the
    # session reports back about *itself* (:func:`absorb_ports`), so a smuggled
    # spelling would not merely disagree with the entry — it would be overwritten
    # by the truth minutes later, leaving no trace of what the caller asked for.
    # It also steers the harness when none is named (a ``gpt-*`` model selects
    # codex), so text appended to a command line could change which agent CLI
    # runs a session whose entry says nothing about it.
    MODEL_FLAG,
    # ``--harness`` is the sharpest case of the recording argument, because it
    # needs no inference: ``{"harness": "claude", "extra_args": ["--harness",
    # "codex"]}`` runs codex while the entry, the fleet view and every
    # post-mortem say claude.
    HARNESS_FLAG,
)


class SpawnError(RuntimeError):
    """Raised when a session cannot be started."""


class CapacityError(SpawnError):
    """Raised when the concurrency cap would be exceeded."""


class RunAlreadyLive(SpawnError):
    """Raised when a live session already holds the run a spawn would start.

    A :class:`SpawnError` subclass caught ahead of it, the way
    :class:`CapacityError` is: the request was well formed and the same request
    will work once that session stops, so it is a 409 rather than the 400 a bare
    ``SpawnError`` means.
    """


class SlotOccupied(SpawnError):
    """Raised when the service slot a spawn asked for is already held.

    A 409 for the reason :class:`CapacityError` and :class:`RunAlreadyLive` are:
    nothing about the request is wrong and it works unchanged once the holding
    session ends. An *unknown* or *unusable* slot is a bare :class:`SpawnError`
    and a 400, because no amount of waiting fixes it.
    """


def _reject_positional_flag(name: str, value: str) -> None:
    """Refuse a taskdef or target ``lmer``'s parser would read as a flag.

    :func:`_build_command` emits ``[lmer, taskdef, target, "--fastapi", …]`` and
    ``lmer`` parses with argparse, so a value beginning with a dash is an
    *option* there, not a positional. A taskdef of ``--fastapi-token=known`` is
    consumed as that flag with the target sliding into the ``task`` positional —
    which is precisely the hijack :data:`_RESERVED_ARGS` exists to stop, one argv
    position earlier, where that guard does not look.

    Reachable input rather than a caller mistake: ``POST /api/sessions`` takes
    both fields verbatim from the request body. It needs the shared secret, so
    this is not an unauthenticated hole — but the guard is there to keep the
    *registry* honest about the control plane it advertises, and that argument
    does not care how the caller got in.

    :mod:`lmer_platform.resume` refuses the same shapes one level up, with its
    own status codes, because it can tell a caller-supplied taskdef from one it
    read out of the mirror. This is the floor under both.
    """
    if value.startswith("-"):
        raise SpawnError(
            f"{name} may not begin with a dash ({value!r}): it is passed to `lmer` "
            "as a positional argument, and its parser reads that as a flag — "
            f"{', '.join(_RESERVED_ARGS)} would then be set by a caller and the "
            "session's registry entry would advertise a control plane that is not "
            "the one it got"
        )


def _reject_unsafe_taskdef(taskdef: str) -> None:
    """Refuse a taskdef that would name a run dir outside ``runs/``.

    ``run_state.derive_slug`` interpolates the taskdef into the run's directory
    name as given — only the *target* passes through ``sanitize_task_target`` —
    so ``..`` in a taskdef names a directory outside ``runs/``: in this
    orchestrator's index and in the work repo the container writes to.

    Refused as a substring rather than per path segment, unlike
    :func:`lmer_platform.answer._reject_traversal`, which has to allow the
    slashes a ``project`` legitimately carries. A taskdef is a directory name in
    a taskdef search path and never legitimately contains either character; and
    by the time the slug matters it is a *concatenation*, so there are no
    segments left to reason about.
    """
    for bad in ("/", "\\", ".."):
        if bad in taskdef:
            raise SpawnError(
                f"taskdef may not contain {bad!r} ({taskdef!r}): it is interpolated "
                "into the run's directory name unsanitized, so this is not a usable "
                "path — it would name a run dir outside runs/"
            )


def _reject_option_value(name: str, value: str) -> None:
    """Refuse a typed field ``lmer``'s parser would read as the *next* option.

    :attr:`SpawnRequest.preset`, :attr:`SpawnRequest.agents`,
    :attr:`SpawnRequest.model` and :attr:`SpawnRequest.harness` are emitted as
    two tokens (``--preset <name>``),
    and ``lmer`` parses with argparse: a value beginning with a dash is not taken
    as the flag's argument at all — the child exits 2 with "expected one
    argument" before it is ever a session. The visible result is a spawn that
    looked accepted, a registry entry, and a container that never existed.

    A different failure from :func:`_reject_positional_flag`, which is about a
    *hijack* — this one cannot succeed rather than succeeding as something else —
    but the same reachability: ``POST /api/sessions`` takes every one of those
    fields verbatim from the request body.
    """
    if value.startswith("-"):
        raise SpawnError(
            f"{name} may not begin with a dash ({value!r}): it is passed to "
            "`lmer` as the argument of a flag, and argparse reads a dash-leading "
            "value as the next option instead — the child would exit 2 with "
            '"expected one argument" rather than starting a session'
        )


def _reject_empty_agent_selection(agents: str) -> None:
    """Refuse an ``--agents`` value that names nobody.

    ``lmer`` splits the selection on commas and drops blank entries
    (:func:`lmer_cli.presets.resolve_agent_presets`), so ``","`` selects no
    agents at all — and that is an *error* there rather than a no-op, because
    spawning fewer agents than were asked for is never silent. The child exits 2.
    Refusing it here means the caller hears about it while it can still fix the
    value, instead of watching a row appear in the fleet view and die before its
    first line of output.
    """
    if not [name for name in agents.split(",") if name.strip()]:
        raise SpawnError(
            f"agents names no agent ({agents!r}): the selection is split on "
            "commas and blank entries are dropped, so `lmer` would refuse it — "
            "an agent fan-out that spawns nobody is an error there, not a "
            "no-op"
        )


@dataclass(frozen=True)
class SpawnRequest:
    """What to start.

    ``answer`` is the one field no HTTP body fills: the route builds this request
    field by field, and an answer only reaches a spawn through
    :func:`lmer_platform.answer.answer_run`, which validates that there is a
    question to answer first. That is why ``--answer`` is refused in
    ``extra_args`` (see :data:`_RESERVED_ARGS`) while this field is not — the
    difference between the two is the validation, not the argv.

    ``preset``, ``agents`` and ``model`` are typed for a related but distinct
    reason. All three *are* filled from the HTTP body, so the difference is not
    who may set them; it is that the platform emits each as a flag it owns and
    records what it emitted in the session's registry entry. Arriving as fields,
    they are values the platform can validate, record and show; appended to
    ``extra_args`` they would be text that wins over the platform's own copy by
    being later in argv (see :data:`_RESERVED_ARGS`, where all three are refused
    there for exactly that).

    Three fields say something about a repository, and they are three different
    claims (see :func:`_repo_urls`):

    - ``repo_url`` is the run's repo **of record**. It is written into the
      session's registry entry and into the tracked index, where every later
      verb believes it.
    - ``identity_repo_url`` is only used to predict the run's ``host``/
      ``project`` and is never written down — for a caller that can reconstruct
      the identity but has no evidence of where the code is cloned from.
    - ``no_repo`` is the session having no repository at all (spec D17), which
      is not the same as a caller supplying no URL: it also refuses the
      ``LMER_REPO_URL`` fallback and tells ``lmer`` to skip the clone.

    A caller that fills in none of the three is the ordinary case rather than a
    mistake — ``lmer develop <MR-url>`` takes no repo flag either. A plain clone
    URL target becomes repository evidence; a resource URL target contributes
    identity only (:func:`_repo_urls`).

    ``title`` and ``description`` are the odd pair here: they are the only fields
    that say nothing about the invocation. Nothing spells them in argv, ``lmer``
    has no flag for either, and they are not checked by :meth:`validate` — they are
    this orchestrator's note about the run, written after the spawn is tracked by
    the module that owns the rules for that text (:func:`_write_run_meta`).
    """

    taskdef: str
    target: str
    repo_url: Optional[str] = None
    preset: Optional[str] = None
    #: Comma-delimited preset names the session's agent may fan out to
    #: (:data:`AGENTS_FLAG`). One string rather than a list because that is the
    #: shape ``lmer`` takes and the shape it records; splitting it here would
    #: mean two spellings of one selection that could disagree.
    agents: Optional[str] = None
    harness: Optional[str] = None
    #: The model to run (:data:`MODEL_FLAG`), verbatim — ``lmer`` does not
    #: validate model names and neither does this: the harness is the only thing
    #: that knows which ids it serves, and a platform-side allowlist would be a
    #: second, staler opinion about that. Left unset the session runs whatever
    #: its own environment, its preset or its harness settles on — and reports
    #: that back when it is a name at all (:func:`absorb_ports`).
    model: Optional[str] = None
    ports: int = 0
    extra_args: tuple = ()
    answer: Optional[str] = None
    identity_repo_url: Optional[str] = None
    no_repo: bool = False
    #: What this orchestrator files the run under, one line
    #: (:data:`lmer_platform.meta.MAX_TITLE_CHARS`). Platform metadata, not a
    #: launch fact: it is written to ``run_meta.json`` after the run is tracked and
    #: never to the tracked index — see :mod:`lmer_platform.meta` for why those are
    #: separate files, and for what makes this note local to this orchestrator.
    title: Optional[str] = None
    #: The longer form of the same note, markdown, for a spawn that has more to say
    #: about why the run exists than a label can hold.
    description: Optional[str] = None
    #: The service slot to occupy (issue #245). Unlike every other field here it
    #: names nothing in argv: the slot resolves against ``config.slots`` and it
    #: is the slot's *preset* that is emitted, which is what makes occupying a
    #: slot and running in service mode one act rather than two that must agree.
    slot: Optional[str] = None

    def validate(self) -> "SpawnRequest":
        if not isinstance(self.taskdef, str) or not self.taskdef.strip():
            raise SpawnError("taskdef is required")
        if not isinstance(self.target, str) or not self.target.strip():
            raise SpawnError("target is required")
        # Both are argv positionals, and the taskdef is also a path segment.
        # Neither is checked anywhere else on the way in: ``POST /api/sessions``
        # takes both verbatim from the request body.
        _reject_positional_flag("taskdef", self.taskdef)
        _reject_positional_flag("target", self.target)
        _reject_unsafe_taskdef(self.taskdef)
        if not isinstance(self.ports, int) or isinstance(self.ports, bool) or self.ports < 0:
            raise SpawnError(f"ports must be a non-negative integer, got {self.ports!r}")
        # Not a second copy of the answer path's own checks (length, emptiness
        # after stripping): just enough that the flag is never spelled with
        # nothing — or with a repr — behind the ``=``.
        if self.answer is not None and (
            not isinstance(self.answer, str) or not self.answer.strip()
        ):
            raise SpawnError(f"answer must be non-empty text, got {self.answer!r}")
        # The flag *arguments* the platform emits. Absent is the normal case
        # and says nothing; present and unusable is refused here rather than
        # discovered by the child, which can only report it by exiting 2 — after
        # the session exists on paper.
        #
        # ``harness`` is in this loop rather than trusted from ``body["harness"]``
        # for both halves of that sentence. A dash-leading value ("-x") is an
        # accepted spawn that never existed: the port is drawn, the token minted,
        # the entry registered and the run tracked before the child exits 2. A
        # non-str is worse — ``extra_args`` is coerced on the way into argv and
        # this is not, so a number reaches ``Popen`` and raises ``TypeError``,
        # which the ``(OSError, ValueError)`` handler around it does not catch:
        # a 500 with the master fd of the PTY leaked, once per request.
        for name in ("preset", "agents", "model", "harness"):
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                raise SpawnError(f"{name} must be non-empty text, got {value!r}")
            _reject_option_value(name, value)
        if self.agents is not None:
            _reject_empty_agent_selection(self.agents)
        if self.slot is not None:
            if not isinstance(self.slot, str) or not self.slot.strip():
                raise SpawnError(f"slot must be non-empty text, got {self.slot!r}")
            # A contradiction rather than a precedence question, like
            # ``no_repo`` beside a repo URL: dropping either one silently would
            # grant a slot against a service the session never opened.
            if self.preset is not None:
                raise SpawnError(
                    f"slot {self.slot!r} is set together with preset "
                    f"{self.preset!r}: a slot supplies its own preset, so name "
                    "one or the other"
                )
        # A contradiction rather than a precedence question: silently dropping
        # one of the two would mean a caller believing it named a repository and
        # a session that has none, or the reverse.
        if self.no_repo and (self.repo_url or self.identity_repo_url):
            raise SpawnError(
                "no_repo is set together with a repo URL "
                f"({self.repo_url or self.identity_repo_url!r}): a session either "
                "has no repository (spec D17) or belongs to one, so say which"
            )
        return self


@dataclass
class SpawnResult:
    """The session that was started.

    ``host``/``project``/``slug`` are the *run's* identity; ``control_host`` and
    ``control_port`` are where the session itself answers. The two are unrelated
    addresses that both want the word "host", hence the prefix.

    ``control_port`` is optional only so a caller can synthesize a result without
    a live session; a real spawn always fills it in.

    ``warning`` is how a spawn that *succeeded* still reports that something was
    lost: :func:`_untracked_run_warning`'s case, a session whose run has no
    identity and is therefore never recorded, and — since a spawn can name the run
    it starts — a title that was not stored (:func:`_write_run_meta`,
    :func:`_dropped_meta_warning`). Both are things the session survives, which is
    why they ride on a result rather than raising. ``None`` on the ordinary path,
    so a caller can show it unconditionally.
    """

    session_id: str
    pid: int
    log_path: Path
    host: Optional[str]
    project: Optional[str]
    slug: Optional[str]
    command: list
    control_host: str = _CONTROL_HOST
    control_port: Optional[int] = None
    warning: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "pid": self.pid,
            "log_path": str(self.log_path),
            "run": {"host": self.host, "project": self.project, "slug": self.slug},
            # Present even when empty: a client that only renders a truthy value
            # needs no knowledge of which spawns can produce one.
            "warning": self.warning,
            # Where to reach the session, and pointedly not how to authenticate
            # to it: this dict is the spawn API's response body. The bearer token
            # is available to processes on this host that can read the 0600 file
            # named by the entry's ``control.token_ref`` — see read_control_token.
            "control": {"host": self.control_host, "port": self.control_port},
            "command": self.command,
        }


def resolve_lmer_bin(config: PlatformConfig) -> str:
    """Locate the ``lmer`` executable to spawn.

    Config wins so an operator can point at a specific checkout; otherwise
    ``PATH``. Failing to find it is an error rather than a hopeful ``"lmer"``,
    because the resulting failure would otherwise surface as an opaque ENOENT
    inside a drain thread rather than at the call site.
    """
    configured = getattr(config, "lmer_bin", None)
    if configured:
        return str(configured)
    found = shutil.which("lmer")
    if not found:
        raise SpawnError(
            "cannot find the `lmer` executable on PATH — set lmer_bin in "
            "config.json to its full path"
        )
    return found


def derive_run_identity(
    request: SpawnRequest, repo_url: Optional[str]
) -> tuple:
    """Predict ``(host, project, slug)`` for the run this spawn will create.

    Mirrors what the container itself will compute: the slug from
    ``run_state.derive_slug`` and the host/project from the repo URL. Returns
    ``None`` components when the repo URL is absent or unparseable — the session
    still runs, it just cannot be joined to a run dir until its state is pushed.

    The URL is a parameter rather than ``request.repo_url`` because deriving an
    identity from one and recording one as the run's repo are separable; see
    :func:`_repo_urls` for which is which.
    """
    slug = run_state.derive_slug(request.taskdef, request.target)
    host, project = _parse_repo_url(repo_url or "")
    return host, project, slug


def _identity_url_from_target(target: str) -> Optional[str]:
    """A repo URL read out of *target*, to identify the run by. ``None`` if it isn't one.

    Reuses ``cli._derive_repo_url_from_task_target``, which is not merely "a
    parser that exists": it is the *same* helper ``lmer`` runs on the target one
    line before it resolves the repository, so for a resource URL its answer
    already **is** the URL the child ends up cloning. ``_parse_repo_url`` of it is
    therefore the very ``LMER_REPO_HOST``/``LMER_REPO_PROJECT`` pair the container
    exports and files the run directory under. A second parser here would be a
    second opinion, and the one that disagreed would name a run dir that never
    appears.

    Which is also why the derived spelling is passed through as-is rather than
    normalised to the ``https://host/project`` form
    :func:`lmer_platform.answer._identity_repo_url` builds: with no token for the
    host that helper answers in SSH shape, and the identity ``lmer`` computes from
    *that* string is what the run dir gets. The two spellings do agree now that
    the parser reads them the same way — until T48 its SSH branch stripped
    ``.git`` as a character class, so ``group/project`` read back as
    ``group/projec`` and re-spelling the URL here predicted a run directory the
    container never created — but that agreement is a property of
    ``_parse_repo_url``, not of this call, and re-spelling a URL to derive an
    identity from is the one way this can be wrong while looking right.

    Credentials come off first. The helper looks up a host token and returns
    ``https://oauth2:<token>@…`` when it finds one — which a daemon holding tokens
    for its own forge does as a matter of course — and this value goes on to be
    logged and, when the identity does not parse, quoted back to the caller. The
    scrub cannot cost the identity: ``_parse_repo_url`` takes the host from the
    netloc after the ``@`` either way. It is not enough on its own, which is why
    the value is *identity only* (see :func:`_repo_urls`) — but it is what makes
    the value safe to say out loud.
    """
    derived = _derive_repo_url_from_task_target(target or "")
    if not derived:
        return None
    scrubbed = _scrub_credentials(derived) or None
    if scrubbed:
        # Said once per spawn, because a run filed under a project the request
        # never named is otherwise a mystery in the fleet view — and because this
        # is the line that would have explained the row nobody could find. Safe to
        # log only *because* of the scrub above: platform logs are read and pasted.
        logger.info(
            "platform_spawn_identity_from_target target=%s repo=%s — identity "
            "derived from the target; not recorded as the run's repo",
            target, scrubbed,
        )
    return scrubbed


_PLAIN_REPO_SCHEMES = frozenset({"http", "https", "ssh", "git"})
_KNOWN_HTTP_FORGES = frozenset({
    "github.com", "gitlab.com", "bitbucket.org", "codeberg.org",
})


def _known_http_forge(host: str) -> bool:
    """Whether *host* makes a suffix-less HTTP project URL unambiguous."""
    host = host.lower()
    first_label = host.split(".", 1)[0]
    return (
        host in _KNOWN_HTTP_FORGES
        or first_label in {"git", "gitlab", "github", "bitbucket", "codeberg"}
    )


def _github_http_forge(host: str) -> bool:
    """Whether *host* uses GitHub's fixed ``owner/repo`` project root."""
    host = host.lower()
    return host == "github.com" or host.split(".", 1)[0] == "github"


def _plain_repo_url_from_target(target: str) -> Optional[str]:
    """Return a repository URL used directly as *target*, or ``None``.

    Parsing a host and path does not prove a URL is cloneable: ordinary GitLab
    file, pipeline and wiki pages have the same shape. This predicate therefore
    accepts only Git transports, HTTPS URLs ending in ``.git``, or suffix-less
    HTTP(S) roots on a recognisable forge host. GitLab's ``/-/`` web routes and
    GitHub's routes below ``owner/repo`` are always excluded.

    Embedded HTTP credentials are transport material, not repository identity,
    so they are stripped before the URL becomes the repository of record. Bare
    SSH userinfo (``git@``) is protocol plumbing and is preserved.
    """
    if not target or _derive_repo_url_from_task_target(target):
        return None
    if target.startswith("git@"):
        parsed = None
    else:
        try:
            parsed = urlparse(target)
            scheme = parsed.scheme.lower()
            hostname = parsed.hostname or ""
        except ValueError:
            return None
        if scheme not in _PLAIN_REPO_SCHEMES:
            return None
        if parsed.query or parsed.fragment or "/-/" in parsed.path:
            return None
        path_parts = [part for part in parsed.path.split("/") if part]
        if _github_http_forge(hostname) and len(path_parts) != 2:
            return None
        if scheme in {"http", "https"} and not (
            parsed.path.rstrip("/").endswith(".git")
            or _known_http_forge(hostname)
        ):
            return None
        if (
            scheme not in {"http", "https"}
            and url_has_embedded_credential(target)
        ):
            return None
    recorded = (
        _scrub_credentials(target)
        if parsed is not None and scheme in {"http", "https"}
        else target
    )
    host, project = _parse_repo_url(recorded)
    if not host or not project:
        return None
    logger.info(
        "platform_spawn_repo_from_target repo=%s — plain repository URL target "
        "available as repository evidence",
        recorded,
    )
    return recorded


def _repo_urls(request: SpawnRequest) -> tuple:
    """``(recorded, identity)`` — the run's repo of record, and what to derive from.

    Two values because they are two different claims, and conflating them is how
    a run acquires a URL nobody gave it. *recorded* goes into the session's
    registry entry and, through :func:`lmer_platform.runs.track`, into the
    tracked index as the run's ``repo``; from there every later verb believes it,
    which is why :class:`lmer_platform.resume.RepoUrlRequired` refuses to invent
    one. *identity* only has to parse back to the run's own host and project, so
    a caller that can reconstruct those (:mod:`lmer_platform.answer`, for an
    adopted run that recorded no URL) can be joined to its session without
    leaving a fabricated URL behind.

    The ``LMER_REPO_URL`` fallback keeps a daemon started with one exported
    working for a caller that supplies nothing, and stops at the two cases where
    it would be filing a session under a repository nobody named:

    - ``no_repo``, where there is no repository at all (spec D17) — the case the
      fallback silently broke, since the assistant sets ``repo_url=None``
      deliberately and would otherwise be recorded against whatever the daemon's
      shell happened to export;
    - a caller that supplied its own ``identity_repo_url``, which is a caller
      saying it already knows where this run belongs.

    A plain repository-URL target is also evidence: it is the clone target the
    caller supplied, so it becomes both the record and identity when no stronger
    source is present. A resource target remains the **last** source of identity
    and never a source of record, because its URL is reconstructed. The ordering
    keeps a supplied ``repo_url`` first, an exported ``LMER_REPO_URL`` next, and
    a caller-supplied ``identity_repo_url`` ahead of either target shape.

    A reconstructed resource URL is never recorded, for the reason
    :func:`lmer_platform.answer._identity_repo_url` sets out at length: a
    recorded reconstruction round-trips, so it satisfies every later identity
    check, and a run whose repo was *derived once* would pass
    :class:`lmer_platform.resume.RepoUrlRequired` forever after on a URL nobody
    supplied — quietly undoing the decision that a missing repo URL is asked for
    rather than guessed. It is also, in the ordinary GitLab case, a URL whose
    clone transport was chosen by whichever token the daemon happened to hold,
    which is no basis for the field every later verb clones from.
    """
    if request.no_repo:
        return None, None
    recorded = request.repo_url or (
        None if request.identity_repo_url
        else configured_repo_url()
    )
    if recorded is None and not request.identity_repo_url:
        recorded = _plain_repo_url_from_target(request.target)
    identity = (
        recorded
        or request.identity_repo_url
        or _identity_url_from_target(request.target)
    )
    return recorded, identity


def _untracked_run_warning(request: SpawnRequest, repo_url: Optional[str]) -> str:
    """What is lost when a spawn cannot work out the run's identity, in one line.

    Written for the person who typed the target, because that is who can fix it,
    and returned rather than only logged: it rides back on
    :attr:`SpawnResult.warning` and out through ``POST /api/sessions``. The failure
    it describes is silent by nature — the session starts, runs and looks entirely
    healthy, and the only visible symptom arrives minutes later when its row leaves
    the fleet view along with its registry entry — so a line in the daemon's log is
    not where it belongs.

    Names the cause as well as the consequence, since the two fixes are different:
    a URL that did not parse is a typo, while nothing-to-derive-from is a target
    that names no repository and needs one supplied beside it.
    """
    if repo_url:
        cause = (
            f"the repository URL {_scrub_credentials(repo_url)!r} does not parse "
            "into a host and project"
        )
    else:
        cause = (
            f"no repository URL was given, {ENV_REPO_URL} is not set, and the "
            f"target {request.target!r} is not a merge request, pull request or "
            "issue URL to read one out of"
        )
    warning = (
        f"this run has no identity ({cause}), so it is not tracked at all: the "
        "session appears in the fleet view only while it is alive, and its row "
        "disappears when it exits. Give a repository URL, or a target that names "
        "a merge request, pull request or issue."
    )
    # A spawn that named the run loses that too, and in the same breath: the note
    # is attached to a *tracked* run, and there is none.
    lost = _dropped_meta_warning(request)
    return warning if lost is None else f"{warning} Also, {lost}"


def _dropped_meta_warning(request: SpawnRequest) -> Optional[str]:
    """The note a spawn asked for and this orchestrator had nowhere to put.

    ``None`` when the spawn named neither field, which is the ordinary spawn — a
    warning that also fires when nothing was asked for is one operators learn to
    skip, the same argument the ``no_repo`` branch of :func:`spawn_session` makes
    about the untracked warning itself.

    One sentence for two callers (a run with no identity, and a session with no
    repository at all), because the caller's move is the same in both: nothing was
    lost that a later ``POST /api/runs/meta`` cannot set, if the run ever becomes
    one this orchestrator tracks.
    """
    if request.title is None and request.description is None:
        return None
    return (
        "the title and description this spawn carried were not stored: metadata is "
        "this orchestrator's note about a run it tracks, and this session has no "
        "tracked run to attach one to. Set it with POST /api/runs/meta if the run "
        "is adopted later."
    )


def _write_run_meta(
    request: SpawnRequest, host: str, project: str, slug: str
) -> Optional[str]:
    """File the run under the spawn's title and description. Returns any warning.

    After :func:`lmer_platform.runs.track` and not before: :func:`meta.write`
    refuses a run this orchestrator does not track, which one line earlier this run
    was. The text itself is ``meta``'s business — one line, control characters gone,
    bounded — and stays that way here: this passes both fields through untouched
    and lets that module refuse what it will not take.

    A refusal is a warning rather than an exception, and that is a decision about
    ordering rather than about severity. By the time this runs the container has
    started, the session is registered and the run is tracked; raising would report
    a failed spawn for a session that is working, and there is nothing to undo it
    with. So the run keeps its slug in the fleet view, and the caller is told the
    label did not take.

    ``None`` when there was nothing to write, which is every spawn that named
    neither field.
    """
    if request.title is None and request.description is None:
        return None
    try:
        meta.write(
            host, project, slug,
            title=request.title,
            description=request.description,
        )
    # StoreError beside the refusal because a snapshot that cannot be written is
    # the same outcome from the caller's side, and it must not become a traceback
    # out of a spawn that succeeded either.
    except (meta.MetaError, StoreError) as exc:
        logger.warning(
            "platform_spawn_run_meta_refused host=%s project=%s slug=%s error=%s "
            "— the run is tracked and running, without the note",
            host, project, slug, exc,
        )
        return (
            "the run was started and tracked, but this orchestrator's note about it "
            f"was not stored: {exc}. It appears in the fleet view under its slug; "
            "set a title with POST /api/runs/meta."
        )
    return None


def log_path_for(session_id: str) -> Path:
    """Where the *host-side* tee of a session's PTY lands.

    One of two logs a session can have, and no longer the log of record for a
    session whose image writes the other one — see
    :func:`container_log_path_for` for which is which and
    :func:`lmer_platform.session_io.canonical_log` for the choice between them.
    """
    return logs_dir() / f"{session_id}.log"


def container_log_dir_for(session_id: str) -> Path:
    """Host directory the session's *own* log is written into, from inside.

    Beside the PTY log and the facts file, in ``logs/``, because it is the same
    kind of thing: per-session state the platform owns and reads. It gets a
    directory rather than being a bare file for the reason the whole feature turns
    on — the platform must be able to tell "this session writes its own log" from
    "this session's image predates that" *without asking about versions*, and a
    directory it creates is a question, while the file the container's supervisor
    writes inside it is the answer. Bind-mounting a file instead would answer yes
    before anything had been written, which is the one answer that cannot be
    recovered from.
    """
    return logs_dir() / f"{session_id}.session"


def container_log_path_for(session_id: str) -> Path:
    """Host path of the log the session's own supervisor writes (issue #150).

    The transport is a mounted directory: exactly the shape the transcript (T22)
    and ask channel (T23) already use, and the same "the container writes, the host
    reads" relationship the facts file (:func:`ports_file_for`) has, differing only
    in which side of the mount the writer sits on. It exists because the host tee
    dies with the process that owns the PTY master, so a daemon restart used to
    degrade every session's scrollback to whatever the control plane could still be
    asked for (T36); this file is written by a process inside the container and
    survives anything short of the container itself.

    **Absent is a first-class answer, forever.** The file appears only if the
    session's image carries an ``lmer`` whose supervisor knows to write it
    (``lmer_cli.supervisor.SessionLog``), and the image is *not* this checkout: a
    session spawned from an older worker image never writes here, and everything
    about that session must work exactly as it did before. Nothing anywhere checks
    a version to decide; the read path probes for this path and falls back to
    :func:`log_path_for`.

    The file name comes from :data:`lmer_cli.supervisor.SESSION_LOG_NAME` rather
    than being spelled again, so the two halves of the mount cannot drift.
    """
    return container_log_dir_for(session_id) / SESSION_LOG_NAME


def ports_file_for(session_id: str) -> Path:
    """Where the spawned ``lmer`` reports what it could only learn at launch.

    The ports first, and the reason the file is named after them: the platform
    cannot know those in advance — ``lmer`` picks free ports at launch — so it
    hands the child a path to write them to (``LMER_PLATFORM_PORTS_FILE``,
    honored by ``lmer_cli.cli``). A file rather than an import keeps ``lmer_cli``
    free of any dependency on this package.

    T51 put the session-resolved model through the same channel, and #318 added
    the resolved harness (see :func:`absorb_ports`). Both have the same shape:
    known only inside the launch and unguessable here — the daemon's environment
    is not evidence about a preset/model-selected run. The name stayed as it is
    on both sides: it is an interface between independently-installed programs,
    and an older ``lmer`` writing a ports-only file into a newer platform is
    exactly the case that must keep working.
    """
    return logs_dir() / f"{session_id}.ports.json"


def _transcripts():
    """The :mod:`lmer_platform.transcripts` module, imported on demand.

    Imported inside a function rather than at module scope because
    :mod:`lmer_platform.session_io` imports *this* module and
    :mod:`lmer_platform.transcripts` imports ``session_io`` — a top-level import
    here would close that cycle and try to read this module's names before they
    exist. One named seam so the reason is stated once for every caller that
    needs it; same lazy-import shape as ``lmer_cli.harness.known_harnesses``.
    """
    from . import transcripts

    return transcripts


def transcript_dir_for(session_id: str) -> Path:
    """Host directory a session's harness transcripts land in.

    Thin pass-through to :func:`lmer_platform.transcripts.session_transcript_dir`
    — the chat view's *primary* resolution, derived from the session id and
    needing no recorded state, which is what makes this wiring a mount and
    nothing else. Kept here so the spawn side has one name for it and callers do
    not have to know which module owns the layout.
    """
    return _transcripts().session_transcript_dir(session_id)


def _prepare_transcript_dir(session_id: str) -> Optional[Path]:
    """Create the session's transcript directory 0700. ``None`` if that failed.

    0700 because the directory is about to hold everything the session said,
    quoted commands and all, and because it is mounted read-write into a
    container — the mode is what keeps other users on the host out of it. Created
    with the mode rather than chmod'ed into it (``os.mkdir`` applies
    ``mode & ~umask``, so the directory is never *wider* than 0700 for an
    instant), then chmod'ed to make the mode exact under any umask and to correct
    a directory that somehow already existed.

    Fail-soft, unlike the control token: a session with no transcript directory
    is exactly the pre-T22 status quo — the terminal is still the complete record
    — so it warns and skips the mount rather than refusing to start a session
    someone is waiting for.
    """
    directory = transcript_dir_for(session_id)
    mode = _transcripts().SESSION_DIR_MODE
    try:
        # The parent through the store: mkdir(mode=...) is leaf-only, and the
        # transcript ROOT was taking the umask around 0700 leaves (T93 finding).
        store.ensure_state_dir(directory.parent)
        directory.mkdir(mode=mode, exist_ok=True)
        directory.chmod(mode)
    except OSError as exc:
        logger.warning(
            "platform_transcript_dir_unusable id=%s path=%s error=%s — the "
            "session runs without a transcript mount; the PTY log is the "
            "complete record",
            session_id, directory, exc,
        )
        return None
    return directory


def _container_dir_key(container_dir: str) -> str:
    """*container_dir* as the runtime will compare it: no trailing slash.

    Exact equality on the normalised path and nothing else. A *nested* path is
    not a collision — the runtime mounts ``/a`` and ``/a/b`` happily — and
    guessing at nesting here would refuse mounts that work.
    """
    return str(PurePosixPath(container_dir))


def _below_destination(destination: str, ancestor: str) -> bool:
    """Whether *destination* is strictly below *ancestor*.

    Compared as path components rather than as a string prefix: ``/home/dev2``
    starts with ``/home/dev`` and is nowhere below it. *Strictly*, because an
    equality is a collision and is reported as one, with its own reason.
    """
    parts = PurePosixPath(destination).parts
    top = PurePosixPath(ancestor).parts
    return len(parts) > len(top) and parts[:len(top)] == top


def _below_container_home(destination: str) -> bool:
    """Whether *destination* is strictly below :data:`CONTAINER_HOME`.

    Named after the rule it enforces — the container home is the ancestor a
    declared session directory has to be under.
    """
    return _below_destination(destination, CONTAINER_HOME)


def _covered_destination(destination: str, *owners: dict) -> tuple:
    """The first destination in *owners* that *destination* contains, and its owner.

    ``(None, None)`` when it contains none of them. An exact match is not
    "covered" — that is the equality collision, which is reported separately and
    with its own reason. Components again, for the reason
    :func:`_below_container_home` gives.
    """
    parts = PurePosixPath(destination).parts
    for mapping in owners:
        for other, owner in mapping.items():
            other_parts = PurePosixPath(other).parts
            if len(other_parts) > len(parts) and other_parts[:len(parts)] == parts:
                return other, owner
    return None, None


def _reserved_session_destinations() -> dict:
    """``{container path: what already owns it}`` for a user harness's ``session_dir``.

    Every entry is a destination this spawn (or ``lmer`` itself) already mounts
    something at, so a drop-in manifest naming one of them is a claim on
    somebody else's mount. What that costs depends on which flag delivers the
    mount:

    * the harness session directories, the ask channel and the session log
      arrive as ``--mount-dir``, and that parser dedupes duplicate destinations
      itself — warn, last wins (:func:`lmer_cli.cli.parse_dir_mount_specs`). So
      the drop-in's mount would quietly take over what lands at a platform
      destination, the same silent redirect :func:`_reject_mount_hijack` refuses
      for a caller's own ``--mount-dir``.
    * ``/workspace`` and ``/Agents/global`` are ``lmer``'s own ``-v`` mounts,
      which no dedupe sees. The docker *client* sends both binds unmerged
      (measured on 29.6.2), so the refusal is the daemon's — one drop-in line
      would abort *every* platform spawn, on every harness, until the file was
      found and edited.

    The manifest field's own contract is warn-and-ignore
    (:func:`lmer_cli.user_harnesses._parse_session_dir`), so the collision is
    checked here, at the site that knows what the platform mounts.

    Built from the constants where there are constants and from literals where
    ``lmer``'s own layout has none. The built-in harnesses' declared directories
    are in it too: they are mounted on every spawn regardless of which harness
    the session resolves to (:func:`_transcript_mount_flags`).
    """
    reserved = {
        _container_dir_key(harness.session_dir): f"the built-in {name} harness"
        for name, harness in HARNESSES.items()
        if harness.session_dir
    }
    reserved.update({
        _container_dir_key("/workspace"): "the session's workspace",
        _container_dir_key("/Agents/global"): "the agent-files tree",
        _container_dir_key(ask.CONTAINER_ASK_DIR): "the session's ask channel",
        _container_dir_key(CONTAINER_SESSION_LOG_DIR): "the session's own log",
        _container_dir_key(CONTAINER_HARNESSES_DIR): "the user-harness drop-ins",
        _container_dir_key(CONTAINER_MOUNT_STAGING_DIR):
            "the mount staging area (lmer-internal)",
    })
    return reserved


def _within_staging_area(destination: str) -> bool:
    """Whether *destination* is the staging directory or sits below it.

    A declared path in the staging area would put a mount on top of the
    platform's own staging layout and ask the linker to link a path to itself
    or to another harness's mount. Equality is answered here (not only by the
    reserved map) because :func:`_reject_mount_hijack` calls this where no
    reserved map exists.
    """
    staging = _container_dir_key(CONTAINER_MOUNT_STAGING_DIR)
    return (
        _container_dir_key(destination) == staging
        or _below_destination(destination, CONTAINER_MOUNT_STAGING_DIR)
    )


def _harness_session_dirs() -> dict:
    """``{harness name: the container directory it writes its session JSONL in}``.

    Read out of the registry (:func:`lmer_cli.harness.known_harnesses`, which
    merges user drop-ins into the built-ins) rather than listed here, because
    where a harness writes is a fact about that harness. The platform spawns
    claude, pi and codex but mounted only claude's projects directory, so a pi
    or a codex session's transcript died with its ``--rm`` container and the
    chat view drew a blank page for a run that had said plenty (#280).

    Each name becomes a host subdirectory name below, so a name that is not a
    single path component would put a mount outside the directory this session
    owns. The user-harness loader already refuses anything but
    ``[a-z][a-z0-9_-]*`` — this is that guarantee checked where it is depended
    on rather than trusted across a module boundary.

    A *user* harness's declared directory has to earn its mount, because that
    mount is made ``rw`` on every spawn whatever harness the session resolves to
    (:func:`_transcript_mount_flags`), and the host side of it is an empty 0700
    directory. Three things are asked of it, and failing any of them costs that
    harness its mount and nothing else:

    * it is not a destination the platform already mounts at
      (:func:`_reserved_session_destinations`) or one an earlier drop-in claimed.
      What that collision would cost is in that function's docstring: a silent
      redirect for the ``--mount-dir`` destinations, an aborted launch for
      ``lmer``'s own ``-v`` ones.
    * it is *strictly below* :data:`CONTAINER_HOME`. ``"/home/developer"``
      itself, ``/etc`` or ``/`` pass the manifest parser's shape check (absolute,
      no ``..``) and the equality check above, and would bind an empty directory
      over the container's home or its system tree — hiding the harness binaries
      in ``~/.npm-global/bin`` and ``~/.local/bin``, so no session on any harness
      could start.
    * it does not *contain* another destination this spawn mounts.
      ``/home/developer/.pi`` is neither equal to nor outside the home, and
      mounting an empty directory there hides whatever the image keeps beside
      pi's session directory — its configuration and its credentials.
    * it is not inside the mount staging area (:func:`_within_staging_area`),
      which is where a user harness's mount is actually bound.

    What passes is *declared*, and stays declared here: the staging of a user
    harness's mount destination happens afterwards
    (:func:`_harness_mount_destinations`), on paths these rules have already
    accepted, so the checks keep reading the path the manifest wrote.

    The checks cover the destinations the *platform* mounts, not the image's
    own layout or ``lmer``'s ``-v`` set: a declared directory that shadows,
    say, ``~/.npm-global`` is below the home, covers nothing in the reserved
    map, and still mounts — these rules narrow the blast radius, they do not
    make an arbitrary manifest safe.

    Built-ins are exempt: their declarations are fixed in this repository, so a
    bad one is not a runtime condition but an edit here, and the
    ``CONTAINER_TRANSCRIPT_DIR`` drift check below is the one that catches that
    class.
    """
    container_transcript_dir = _transcripts().CONTAINER_TRANSCRIPT_DIR
    reserved = _reserved_session_destinations()
    claimed = {}
    dirs = {}
    for name, harness in known_harnesses().items():
        if not harness.session_dir:
            continue
        if name in (".", "..") or name != Path(name).name:
            logger.warning(
                "platform_transcript_harness_name_refused harness=%r — a name "
                "that is not one path component cannot be a transcript "
                "subdirectory; this harness's transcript is not mounted out",
                name,
            )
            continue
        if harness.source_dir is not None:
            destination = _container_dir_key(harness.session_dir)
            owner = reserved.get(destination) or claimed.get(destination)
            if owner is not None:
                logger.warning(
                    "platform_transcript_session_dir_taken harness=%s path=%s "
                    "owner=%s — a second mount at one container path would "
                    "silently take over what lands there (or, for lmer's own -v "
                    "destinations, abort every session); this harness's "
                    "transcript is not mounted out and the session starts "
                    "without it",
                    name, harness.session_dir, owner,
                )
                continue
            if _within_staging_area(destination):
                logger.warning(
                    "platform_transcript_session_dir_in_staging harness=%s "
                    "path=%s staging=%s — that directory is where the platform "
                    "stages this harness's own mount, so a declaration inside it "
                    "would collide with the staging layout and with the "
                    "entrypoint's link; this harness's transcript is not mounted "
                    "out and the session starts without it",
                    name, harness.session_dir, CONTAINER_MOUNT_STAGING_DIR,
                )
                continue
            if not _below_container_home(destination):
                logger.warning(
                    "platform_transcript_session_dir_outside_home harness=%s "
                    "path=%s home=%s — the platform mounts a session directory "
                    "rw on every spawn, and an empty host directory bound over "
                    "the container home or a system path hides what the image "
                    "put there, harness binaries included; this harness's "
                    "transcript is not mounted out and the session starts "
                    "without it",
                    name, harness.session_dir, CONTAINER_HOME,
                )
                continue
            covered, covered_owner = _covered_destination(
                destination, reserved, claimed
            )
            if covered is not None:
                logger.warning(
                    "platform_transcript_session_dir_covers harness=%s path=%s "
                    "covers=%s owner=%s — an empty directory mounted over one "
                    "the platform mounts inside of hides what the container "
                    "keeps beside it, a harness's own configuration included; "
                    "this harness's transcript is not mounted out and the "
                    "session starts without it",
                    name, harness.session_dir, covered, covered_owner,
                )
                continue
            claimed[destination] = f"the {name} harness"
        dirs[name] = harness.session_dir
    if container_transcript_dir not in dirs.values():
        # Not a runtime condition: claude's mount destination, the constant the
        # reader and the redirect refusal are written against, and claude's
        # registry entry are one fact — the only way here is an edit to one of
        # them. Loud on every spawn beats a chat view that quietly reads a
        # directory nothing was mounted at.
        raise RuntimeError(
            f"no harness declares {container_transcript_dir!r} as its session "
            "directory: lmer_platform.transcripts.CONTAINER_TRANSCRIPT_DIR and "
            "lmer_cli.harness's claude entry have drifted apart"
        )
    return dirs


def _harness_mount_destinations() -> dict:
    """``{harness name: the container path its transcript subdirectory binds at}``.

    The declared directory (:func:`_harness_session_dirs`) for a built-in; a
    staged path under :data:`lmer_cli.mounts.CONTAINER_MOUNT_STAGING_DIR` for a
    user harness, whose declared parents the image may not ship — the runtime
    would create them root-owned and the harness would EACCES writing beside
    its mount (issue #293; the why lives on that constant). The declared path
    comes back as an entrypoint symlink (:func:`_harness_session_links`).
    """
    registry_entries = known_harnesses()
    destinations = {}
    for name, declared in _harness_session_dirs().items():
        entry = registry_entries.get(name)
        if entry is not None and entry.source_dir is not None:
            destinations[name] = f"{CONTAINER_MOUNT_STAGING_DIR}/sessions/{name}"
        else:
            destinations[name] = declared
    return destinations


def _harness_session_links() -> list:
    """``[(declared, staged)]`` for every harness whose mount is staged.

    The container entrypoint turns each pair into a symlink, so a harness still
    finds its session directory where its manifest says it is. Derived from the
    same two functions the mount is built from rather than recomputed, so a link
    can only ever name a destination this spawn also mounts.
    """
    destinations = _harness_mount_destinations()
    return [
        (declared, destinations[name])
        for name, declared in _harness_session_dirs().items()
        if destinations.get(name) not in (None, declared)
    ]


def _prepare_transcript_subdirs(directory: Path) -> list:
    """One transcript subdirectory per harness under *directory*, 0700.

    Returns ``[(host_dir, container_dir)]`` for the ones that could be created.
    Named after the harness, and *under* the session's own transcript directory,
    so everything a session writes stays inside the one tree the reader scans and
    the exit scrub rewrites: :func:`lmer_platform.transcripts.locate_sources`
    walks it recursively, so a file a level down is found without teaching the
    reader this layout, and a session that predates it — files at the root, from
    the days when claude's projects dir was mounted straight in — still reads.

    Created with the mode rather than chmod'ed into it, for the reason
    :func:`_prepare_transcript_dir` gives, and safe from the leaf-only pitfall it
    names (T93) because *directory* was made through the store there and a
    harness name is a single path component.

    Fail-soft per harness rather than per session: a subdirectory that cannot be
    made costs that one harness its transcript mount, the others still mount, and
    the session starts either way.
    """
    mode = _transcripts().SESSION_DIR_MODE
    prepared = []
    for name, container_dir in _harness_mount_destinations().items():
        subdir = directory / name
        try:
            subdir.mkdir(mode=mode, exist_ok=True)
            subdir.chmod(mode)
        except OSError as exc:
            logger.warning(
                "platform_transcript_subdir_unusable harness=%s path=%s error=%s "
                "— a session on this harness leaves no transcript behind; the PTY "
                "log is its complete record",
                name, subdir, exc,
            )
            continue
        prepared.append((subdir, container_dir))
    return prepared


def _transcript_mount_flags(mounts: list) -> list:
    """``lmer`` flags that mount each *mounts* pair in where its harness writes.

    ``rw`` is not optional here: the harness writes its JSONL into its directory
    for the life of the session. The container-side destinations come from
    :func:`_harness_mount_destinations` rather than being spelled out again, so
    the paths these mount at and the paths the harnesses reach — directly for a
    built-in, through the entrypoint's symlink for a staged user harness — are
    one fact. Every declared harness is mounted, not just the session's: which
    harness a request resolves to is decided inside the child ``lmer`` (flag,
    env, preset, model hint), so the alternative to a few empty directories is
    guessing — and guessing wrong is a session whose transcript is gone.
    """
    flags: list = []
    for host_dir, container_dir in mounts:
        flags += ["--mount-dir", f"{host_dir}:{container_dir}:rw"]
    return flags


def ask_dir_for(session_id: str) -> Path:
    """Host directory a session posts its questions into (spec D26).

    Pass-through to :func:`lmer_platform.ask.session_ask_dir`, kept here so the
    spawn side has one name for every per-session directory it mounts.
    """
    return ask.session_ask_dir(session_id)


def _prepare_ask_dir(session_id: str) -> Optional[Path]:
    """Create the session's ask channel 0700. ``None`` if that failed.

    Fail-soft, and the failure is honest rather than silent: with no directory
    the environment variable is not set either, so ``lmer-ask`` inside the
    container reports "not orchestrated" and exits instead of posting questions
    into a place nobody reads.
    """
    return ask.prepare_ask_dir(session_id)


def _ask_mount_flags(directory: Path) -> list:
    """``lmer`` flags that mount *directory* in as the session's ask channel.

    ``rw`` because the session writes its questions here; the platform writes
    the answers beside them. The container-side destination comes from
    :data:`lmer_platform.ask.CONTAINER_ASK_DIR`, the same constant the
    environment variable points at, so the mount and what the CLI reads cannot
    drift apart.
    """
    return ["--mount-dir", f"{directory}:{ask.CONTAINER_ASK_DIR}:rw"]


def _prepare_container_log_dir(session_id: str) -> Optional[Path]:
    """Create the directory the session writes its own log into. 0700.

    0700 and owner-only for the same reason as the transcript directory: what
    lands in here is every byte the session drew, quoted credentials included, and
    it is mounted read-write into a container. Created with the mode rather than
    chmod'ed into it so it is never briefly wider, then chmod'ed to make the mode
    exact under any umask.

    Fail-soft, and the failure is precisely the pre-#150 status quo: with no
    directory the container's supervisor finds nothing mounted and writes no log,
    the host-side tee stays this session's only record, and the read path serves
    that — which is what it does for every session spawned from an older image
    anyway. Refusing to start a session over it would be trading a working session
    for a durability improvement.
    """
    directory = container_log_dir_for(session_id)
    mode = _transcripts().SESSION_DIR_MODE
    try:
        # Same leaf-only pitfall as the transcript root (T93 finding).
        store.ensure_state_dir(directory.parent)
        directory.mkdir(mode=mode, exist_ok=True)
        directory.chmod(mode)
    except OSError as exc:
        logger.warning(
            "platform_container_log_dir_unusable id=%s path=%s error=%s — the "
            "session writes no log of its own; the host-side PTY tee stays its "
            "record, as for a session from an older image",
            session_id, directory, exc,
        )
        return None
    return directory


def _container_log_mount_flags(directory: Path) -> list:
    """``lmer`` flags that mount *directory* in for the session's own log.

    ``rw`` because the container's supervisor creates and appends to the file in
    here for the life of the session. The destination comes from
    :data:`lmer_cli.supervisor.CONTAINER_SESSION_LOG_DIR` — the constant the writer
    itself reads — so the path this mounts at and the path that is written to are
    one fact rather than two that agree today.
    """
    return ["--mount-dir", f"{directory}:{CONTAINER_SESSION_LOG_DIR}:rw"]


def _with_host_flags(command: list, flags: list) -> list:
    """Insert platform-added ``lmer`` flags ahead of the caller's ``extra_args``.

    Position matters, and appending is wrong: ``extra_args`` may carry a bare
    ``--`` to hand a command line to the container, and anything after that
    sentinel is a token for that command rather than one of ``lmer``'s own
    arguments — the mount would silently not happen. So the flags go right after
    the ``--fastapi`` :func:`_build_command` emits unconditionally, which is the
    one anchor that always sits ahead of request-supplied argv. A caller
    restating ``--fastapi`` in ``extra_args`` cannot move the insertion point:
    ``list.index`` finds the platform's, which comes first.

    Depends on that flag being unconditional — see :func:`_build_command`, where
    it is a stated invariant rather than an option. If it ever stops being one
    this raises ``ValueError`` on every spawn, which is the failure worth having:
    loud and immediate, not a mount that quietly stopped happening.
    """
    at = command.index("--fastapi") + 1
    return [*command[:at], *flags, *command[at:]]


def token_file_for(session_id: str) -> Path:
    """Path of the file holding one session's control-plane bearer token.

    Beside the registry entry rather than in the log directory, because the two
    are one fact split by sensitivity: the entry is a debugging artifact people
    paste around, the token is a credential. The name comes from
    :func:`registry.token_path` — the registry owns that directory and unlinks the
    token along with the entry — while what goes *inside* the file is this
    module's business alone.
    """
    return registry.token_path(session_id)


def read_control_token(session_id: str) -> Optional[str]:
    """The session's bearer token, or ``None`` when there is no usable one.

    The read side of :func:`token_file_for`, for anything that needs to talk to a
    running session. Absent is a normal answer, not an error: a session that
    exited cleanly has had its token removed, and the caller's move — "this
    session cannot be driven" — is the same as for a token that cannot be read.
    """
    path = token_file_for(session_id)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning(
            "platform_control_token_unreadable id=%s error=%s", session_id, exc
        )
        return None
    return token or None


def _mint_control_token(session_id: str) -> tuple:
    """Generate this session's bearer token and persist it. Returns both.

    The file is created 0600 by :func:`os.open` rather than chmod'ed afterwards:
    a token that is world-readable for a millisecond is a token that leaked.
    ``O_EXCL`` for the same reason — ``os.open`` ignores the mode argument for a
    file that already exists, so the only way to be certain of the permissions is
    to refuse anything already there.
    """
    token = secrets.token_urlsafe(_CONTROL_TOKEN_BYTES)
    path = token_file_for(session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token)
    except OSError as exc:
        raise SpawnError(f"cannot write the control token for {session_id} ({exc})")
    return token, path


def _reported_model(payload: dict) -> Optional[str]:
    """The model a session reported for itself, or ``None`` if it reported none.

    Anything that is not a non-blank string is nothing: the file is written by
    the session's own ``lmer`` and is therefore as trustworthy as the daemon
    itself, but an older or newer one may have no such key, and a ``None``
    written for "the harness ran its default" must not become the string
    ``"None"`` on somebody's row.
    """
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        return None
    return model.strip()


def _reported_harness(payload: dict) -> Optional[str]:
    """The resolved harness a session reported for itself, if usable."""
    harness = payload.get("harness")
    if not isinstance(harness, str) or not harness.strip():
        return None
    return harness.strip()


def absorb_ports(sessions: list) -> list:
    """Fold what running sessions reported about themselves into their entries.

    Three facts today, all from :func:`ports_file_for`: the published port mapping,
    model and harness the session resolved. Called on the read path because they
    appear *after* the spawn returns — ``lmer`` publishes ports while starting the
    container and settles the model just before that, which is strictly later than
    the moment the registry entry is written.

    Each fact is folded only when the entry lacks it, which is what makes this
    converge and then stop doing work — and, for model/harness, what keeps the
    precedence right in the other direction: a spawn that *named* a model already
    recorded it, so there is nothing here to learn. Only a session that named none
    (the ordinary case, where the model comes from the daemon's environment or a
    preset) gets its ``task.model`` filled in — and a session that named none and
    resolved none reports nothing, so the field stays empty rather than claiming
    the harness's unnamed default is a model somebody chose.

    Best-effort throughout — a session with unreported ports is worth less than a
    session the fleet view refuses to show.
    """
    updated = []
    for entry in sessions:
        if not isinstance(entry, dict):
            updated.append(entry)
            continue
        session_id = entry.get("id")
        task = entry.get("task")
        wants_ports = not entry.get("ports")
        # A task block that is not a dict came from another version of this
        # daemon, and half-rewriting it here would be a worse answer than the
        # missing model: the fleet view already treats it as no metadata at all
        # (lmer_platform.inventory), and it stays exactly as its writer left it.
        wants_model = isinstance(task, dict) and not task.get("model")
        wants_harness = isinstance(task, dict) and not task.get("harness")
        # Nothing left to learn about this session: stop reading its file.
        if not session_id or not (wants_ports or wants_model or wants_harness):
            updated.append(entry)
            continue
        path = ports_file_for(session_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            updated.append(entry)
            continue
        except OSError as exc:
            logger.warning(
                "platform_ports_file_unreadable id=%s error=%s", session_id, exc
            )
            updated.append(entry)
            continue
        if not isinstance(payload, dict):
            updated.append(entry)
            continue

        changes: dict = {}
        ports = payload.get("ports")
        if wants_ports and isinstance(ports, list) and ports:
            changes["ports"] = ports
        model = _reported_model(payload)
        harness = _reported_harness(payload)
        if (wants_model and model) or (wants_harness and harness):
            # The whole block, because registry.update merges at the top level
            # only; a bare ``{"model": …}`` would drop the taskdef and target the
            # fleet view labels the row with.
            resolved = dict(task)
            if wants_model and model:
                resolved["model"] = model
            if wants_harness and harness:
                resolved["harness"] = harness
            changes["task"] = resolved
        if not changes:
            updated.append(entry)
            continue
        merged = registry.update(session_id, **changes)
        if merged is not None:
            merged["live"] = entry.get("live", registry.is_live(merged))
        updated.append(merged or {**entry, **changes})
    return updated


def _reject_reserved_args(extra_args) -> None:
    """Refuse caller args the platform reserves — see :data:`_RESERVED_ARGS`.

    Matches on prefixes rather than equality because ``lmer``'s parser accepts
    unambiguous abbreviations: ``--no-super`` disables the supervisor exactly as
    well as the full spelling, and a guard that only knew the full spelling would
    be decorative. ``--fastapi`` is left alone — it is an exact flag in its own
    right, and it is what this module passes anyway. A flag that merely *starts*
    like a reserved one (``--prompt`` beside ``--preset``) is untouched: only
    prefixes *of* a reserved flag are refused, which is what keeps
    :data:`lmer_platform.resume.DIRECTION_FLAG` riding through here.

    Scanning stops at a bare ``--``: everything after it is the container's
    command line, not ``lmer``'s, so a flag there cannot reach the parser.
    """
    args = [str(raw) for raw in extra_args]
    for index, arg in enumerate(args):
        if arg == "--":
            return
        name = arg.split("=", 1)[0]
        if not name.startswith("--") or name == "--fastapi":
            continue
        for reserved in _RESERVED_ARGS:
            if reserved.startswith(name):
                # The way through, for the one reserved flag that has one: an
                # answer is refused here because it is under-validated, not
                # because the platform will not deliver it.
                through = (
                    " — answer the run instead (POST /api/runs/answer), which "
                    "checks that there is an open question to answer and that no "
                    "session is already working the run"
                    if reserved == ANSWER_FLAG else ""
                )
                raise SpawnError(
                    f"{name} is not allowed in extra_args: the platform owns this "
                    f"session's control plane and the verbs that validate a "
                    f"respawn, so {', '.join(_RESERVED_ARGS)} cannot be set by a "
                    f"caller{through}"
                )
        _reject_mount_hijack(name, arg, args[index + 1:index + 2])


def _protected_mount_destinations() -> dict:
    """Container paths a caller's ``--mount-dir`` may not aim at, and why.

    Resolved at call time rather than at import: the transcript constant lives
    behind the lazy import that breaks this package's one import cycle (see
    :func:`_transcripts`), and the harnesses that declare a session directory
    include the user drop-ins, which are read from disk.

    Every *declared* session directory is protected, not just claude's, and the
    declared one even where the mount is staged: a caller's mount at the declared
    path lands exactly where the entrypoint wants to put the harness's symlink,
    so it would either swallow the transcript or leave the link unmade.
    :data:`lmer_cli.mounts.CONTAINER_MOUNT_STAGING_DIR` is protected too, but by
    :func:`_reject_mount_hijack` rather than by an entry here — what has to be
    refused there is the whole subtree, not one path.
    """
    protected = {
        session_dir: (
            "the platform mounts the session's transcript there and scrubs it on "
            "exit, so redirecting it would leave an unscrubbed transcript outside "
            "the directory it owns"
        )
        for session_dir in _harness_session_dirs().values()
    }
    protected.update({
        ask.CONTAINER_ASK_DIR: (
            "the platform mounts the session's ask channel there, so redirecting "
            "it would send the session's questions somewhere no operator is "
            "watching — and leave it waiting forever for answers"
        ),
        CONTAINER_SESSION_LOG_DIR: (
            "the session writes its own log there and the platform serves that "
            "log as this session's scrollback, so redirecting it would put the "
            "record of everything the session drew in a directory nobody reads "
            "back — and leave the terminal view showing an empty file"
        ),
    })
    return protected


def _reject_mount_hijack(name: str, arg: str, following: list) -> None:
    """Refuse a caller-supplied mount aimed at a destination the platform owns.

    ``--mount-dir`` is *not* reserved outright: mounting an unrelated data
    directory into a spawned session is legitimate and forbidding it would be a
    worse trade. But last-wins means a caller aiming one at a platform
    destination silently redirects what lands there — see
    :func:`_protected_mount_destinations` for what each redirect would cost.
    Rejecting exactly those destinations keeps the permission the flag exists for
    and removes the aims that lie.

    The staging area is refused as a *subtree*, for ``--mount-file`` too (it
    holds staged credential *files*): a caller's mount in there can hand the
    session's harness credentials the caller chose. The protected-destination
    list stays a ``--mount-dir`` rule — those destinations are all directories.
    """
    aims_at_dir = "--mount-dir".startswith(name)
    aims_at_file = "--mount-file".startswith(name)
    if not (aims_at_dir or aims_at_file):
        return
    spec = arg.split("=", 1)[1] if "=" in arg else (following[0] if following else "")
    # Grammar is host:container[:mode] for both flags; the destination is the
    # second field.
    parts = str(spec).split(":")
    if len(parts) < 2:
        return
    destination = parts[1].rstrip("/")
    if _within_staging_area(destination):
        raise SpawnError(
            f"{name} may not target {CONTAINER_MOUNT_STAGING_DIR} or anything "
            "below it: the platform stages its own user-harness mounts there, so a "
            "mount inside it would shadow one of them — or hand the session's "
            "harness a file or directory the caller chose"
        )
    if not aims_at_dir:
        return
    for protected, reason in _protected_mount_destinations().items():
        if destination == protected.rstrip("/"):
            raise SpawnError(f"--mount-dir may not target {protected}: {reason}")


def _build_command(config: PlatformConfig, request: SpawnRequest) -> list:
    """Assemble the ``lmer`` invocation.

    ``--fastapi`` is not conditional and has no request field turning it off: a
    platform-spawned session that cannot be written to is a session the platform
    cannot answer (spec D8). The port and token it needs travel in the child's
    environment, never as flags — this list is echoed back by the spawn API and
    lands in the event log, which is no place for a credential.

    An answer is one ``--answer=<text>`` token, and that is load-bearing: ``lmer``
    parses with argparse, so ``--answer -yes`` exits 2 with "expected one
    argument" — the value looks like an option. It also goes *before*
    ``extra_args``, which may end in a bare ``--`` handing the rest to the
    container's own command line.

    ``--preset``, ``--agents``, ``--model`` and ``--harness`` stay two tokens
    rather than following that pattern, because they carry *names* rather than
    prose: a preset, agent, model or harness name that begins with a dash is not
    a value to escape past the parser but a value :func:`_reject_option_value`
    refuses outright.
    Everything the platform emits still goes ahead of ``extra_args``, for the
    same ``--`` reason.
    """
    _reject_reserved_args(request.extra_args)
    cmd = [resolve_lmer_bin(config)]
    cmd += [request.taskdef, request.target, "--fastapi"]
    if request.preset:
        cmd += [PRESET_FLAG, request.preset]
    if request.agents:
        cmd += [AGENTS_FLAG, request.agents]
    if request.harness:
        cmd += [HARNESS_FLAG, request.harness]
    if request.model:
        cmd += [MODEL_FLAG, request.model]
    if request.ports:
        cmd += ["--ports", str(request.ports)]
    if request.answer:
        cmd += [f"{ANSWER_FLAG}={request.answer}"]
    cmd += [str(arg) for arg in request.extra_args]
    return cmd


def _live_worker_count() -> int:
    """How many *worker* sessions are up — what ``max_concurrent_sessions`` bounds.

    The orchestrating assistant is excluded, which is the whole of the operator's
    decision that it "gets its own slot" (T75): the platform starts one for itself
    at boot (:class:`lmer_platform.assistant.Supervisor`), so counting it would
    silently turn a host configured for four sessions into a host that runs three
    workers and a chat window. The cap is about how much *work* this host can bear;
    the assistant is the thing that routes the work, and it is one container
    whether the cap is set to 1 or 40.

    The exclusion is by ``kind`` and therefore runs in both directions, which is
    what keeps it one rule rather than two: a worker spawn does not see the
    assistant in the count, and an assistant spawn does not see a previous
    incarnation whose entry is still live. What stops a *second* assistant is
    :func:`lmer_platform.assistant.start`, on the strength of the registry (D11),
    and that is where the refusal belongs — a cap on workers rejecting an
    assistant would be the same setting meaning two things.

    Read through :data:`lmer_platform.registry.ASSISTANT_KIND` rather than a
    literal, and from there rather than from :mod:`lmer_platform.assistant`: this
    module is what spawns the assistant, so importing it would be a cycle.
    """
    return sum(
        1 for entry in registry.list_sessions(live_only=True)
        if entry.get("kind") != registry.ASSISTANT_KIND
    )


def live_session_for_run(
    host: Optional[str], project: Optional[str], slug: Optional[str]
) -> Optional[dict]:
    """The live registry entry filed under this run identity, or ``None``.

    The single matcher for "does this run already have a session". Callers that
    word their own refusal (:mod:`lmer_platform.answer`,
    :mod:`lmer_platform.resume`) come through here too, because two
    implementations of this comparison would be two answers to one question — and
    the one that said no would be the one that let a second container start.

    Matched as a triple of *recorded fields*, never as a composed
    ``host/project/slug`` string: :func:`lmer_platform.runs.run_key` is a display
    form, and building one to compare against would make the answer depend on how
    the key was spelled. The divergence the triple tolerates is the renamed run
    (:func:`lmer_platform.workrepo.resolve_run_dir`): a run keeps its slug as its
    identity while its *directory* may be named after it (``review-mr-172`` living
    in ``runs/review-mr-172--review-mr-172``, in an internal work repo rather than
    this one), every session registers under that slug (:func:`derive_run_identity`
    mirrors the container's ``run_state.derive_slug``), so a renamed run's session
    is found here whatever its directory is called. What this deliberately does
    not do is treat a directory name as an identity: a caller holding the run's
    *tracked* name — which for a renamed run is that directory name — checks it as
    a second candidate itself, because only the caller knows it.

    Liveness is the registry's own lazy reading (``live_only`` →
    :func:`lmer_platform.registry.is_live`, a pid probe on read), so a DEAD entry
    never matches. That is load-bearing rather than incidental: a crashed session's
    entry is kept deliberately as the crash signal, and an entry that outlived its
    process must not wedge the run it names out of ever being respawned.

    An incomplete identity matches nothing in either direction — entries with no
    run recorded carry ``{}``, and a spawn with no repository (spec D17) has no run
    to hold.
    """
    if not (host and project and slug):
        return None
    wanted = (host, project, slug)
    for entry in registry.list_sessions(live_only=True):
        run = entry.get("run") or {}
        if (run.get("host"), run.get("project"), run.get("slug")) == wanted:
            return entry
    return None


def _refuse_if_run_is_live(
    host: Optional[str], project: Optional[str], slug: Optional[str]
) -> None:
    """One run, one session — the invariant, enforced where every spawn passes.

    In :func:`spawn_session` rather than in its callers because a rule that lives
    in callers is as strong as the weakest copy of it, and the field showed what
    that costs: ``POST /api/runs/answer`` spawns by design, the two callers
    that respawn a run each carried their own version of this check, and
    ``POST /api/sessions`` — which derives the same identity from the same
    taskdef and target — carried none at all, so a duplicate session for a run
    was a plain API call away. A session appearing for a run that already had one
    reads as the platform acting on its own, and the two containers then fight
    over one run's owner claim and one run's state.

    The identity is the one the *spawn* will register and track, so this cannot
    disagree with what the new session becomes.

    Not exempt by ``kind``: the assistant is unaffected because it has no run
    (``no_repo``, spec D17, leaves host and project unset) rather than because it
    is named here — and its supervisor's respawn is unaffected for a second
    reason, that the entry a dead incarnation leaves behind is DEAD.
    """
    entry = live_session_for_run(host, project, slug)
    if entry is None:
        return
    raise RunAlreadyLive(
        # The run identity and the session id, because a caller reading a 409 has
        # to know *which* session holds the run before it can do anything about it
        # — an assistant included, since this is one of the refusals it has to act
        # on rather than retry.
        f"{runs.run_key(host, project, slug)} already has a live session "
        f"({entry.get('id')}, pid {entry.get('pid')}) — a second session for one "
        "run would fight it over the run's owner claim and its state. Type into "
        f"that session instead (POST /api/sessions/{entry.get('id')}/input), or "
        "wait for it to stop"
    )


def _claimed_control_ports() -> set:
    """Control ports already promised to live sessions.

    ``_pick_port`` releases its test bind before returning, so "free" only means
    free *at that instant*: a second spawn probing the same range moments later
    can be handed the same port, and the loser's container fails to publish it.
    The registry is the only record of a port that is claimed but not yet bound,
    so it is what the next draw avoids.

    This closes the window that lasts for the *duration of a spawn*, not the
    instant inside one: an entry can only be written once ``Popen`` has returned a
    pid, so two spawns running at the same moment can still draw the same port.
    Left that way deliberately — the alternative is a reservation file written
    before the child exists, i.e. state to reconcile when a spawn dies between
    the two. Anyone narrowing ``DEFAULT_PORT_RANGE`` (100 ports today) is making
    that residual collision proportionally more likely.
    """
    claimed = set()
    for entry in registry.list_sessions(live_only=True):
        control = entry.get("control")
        if not isinstance(control, dict):
            continue
        port = control.get("port")
        if isinstance(port, int) and not isinstance(port, bool):
            claimed.add(port)
    return claimed


def _pick_control_port() -> int:
    """Reserve the host port this session's control plane will bind.

    Failure raises :class:`SpawnError` rather than falling back to "no control
    plane": a session nobody can type into looks identical to a healthy one in
    the fleet view, and would only reveal itself when someone tried to answer it.
    Refusing to start is the honest outcome.
    """
    claimed = _claimed_control_ports()
    for _ in range(_PORT_PICK_ATTEMPTS):
        try:
            port = _pick_port(DEFAULT_PORT_RANGE, _CONTROL_HOST)
        except RuntimeError as exc:
            raise SpawnError(
                f"cannot allocate a control-plane port ({exc}) — refusing to "
                "spawn a session the platform could not reach"
            )
        if port not in claimed:
            return port
    raise SpawnError(
        f"cannot allocate a control-plane port: {_PORT_PICK_ATTEMPTS} draws from "
        f"{DEFAULT_PORT_RANGE[0]}-{DEFAULT_PORT_RANGE[1]} all landed on ports "
        "already claimed by live sessions"
    )


def _drain(master_fd: int, log_file: Path, session_id: str) -> None:
    """Copy the PTY master into *log_file* until every writer closes the slave.

    Kept, and demoted, by #150. It is no longer the log of record for a session
    whose image writes its own (:func:`container_log_path_for`) — but it is not
    redundant either, on three counts, and each one is a reason removal would lose
    something: the loop **must** keep reading whatever happens (an undrained PTY
    blocks the child, so the tee is at most optional in its *writing*); its file is
    the whole record for a session spawned from an older image; and it holds what
    the in-container log structurally cannot — everything host-side ``lmer`` printed
    before the container existed and after it went away (image pull, clone, the
    credential announce, a failure that stopped the launch before a supervisor ever
    ran).

    Runs on a daemon thread and does **only** the tee. Exit bookkeeping lives in
    :func:`_watch`, because PTY closure and child exit are different events:
    a grandchild that inherited the slave fd keeps this loop blocked long after the
    session's own process is gone. Conflating them made a ``kill -9``'d session
    keep reporting as running.

    What it must get right is never stopping while the child lives — an undrained
    PTY blocks the child once the terminal buffer fills.
    """
    try:
        with open(log_file, "ab", buffering=0) as sink:
            while True:
                try:
                    chunk = os.read(master_fd, _DRAIN_CHUNK)
                except OSError:
                    # EIO on Linux is the normal signal that the child closed
                    # the slave end — i.e. the session ended.
                    break
                if not chunk:
                    break
                try:
                    sink.write(chunk)
                except OSError as exc:
                    # Losing the log must not wedge the session: keep draining so
                    # the child never blocks, just stop persisting.
                    logger.warning(
                        "platform_session_log_write_failed id=%s error=%s",
                        session_id, exc,
                    )
                    break
    except OSError as exc:
        logger.warning(
            "platform_session_log_open_failed id=%s path=%s error=%s",
            session_id, log_file, exc,
        )
        # Still drain, or the child blocks on a full terminal buffer.
        try:
            while os.read(master_fd, _DRAIN_CHUNK):
                pass
        except OSError:
            pass
    finally:
        with _suppress_oserror():
            os.close(master_fd)


class _suppress_oserror:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _tb):
        return exc_type is not None and issubclass(exc_type, OSError)


def _scrub_transcripts(session_id: str) -> int:
    """Mask credential shapes in the transcript the finished session left behind.

    Delegates to :func:`lmer_platform.transcripts.scrub_session_transcripts` —
    the same scrub the read path applies to everything it serves, one definition
    for both directions. Returns how many files were rewritten.

    Called only from :func:`_watch`, after ``Popen.wait`` has returned, because
    an in-flight rewrite would replace a file the harness still holds open and
    lose every line it appended afterwards.

    The limit of that guarantee, since it is not obvious: ``lmer`` exiting is not
    proof the *container* is gone. A ``kill -9``'d session can leave the
    container running (the same lingering-grandchild case ``_drain``'s docstring
    describes), and such a container's later appends land in the replaced inode
    and are lost. That is accepted rather than fixed by waiting on the PTY, which
    would leave credentials on disk for as long as an orphan holds the fd — and
    the atomic replace at least means the file at that path is always a complete
    transcript, never a torn one.
    """
    return _transcripts().scrub_session_transcripts(session_id)


#: Set by :func:`_watch` once it has finished recording how a session ended.
#:
#: The reap is asynchronous — ``spawn_session`` returns while a thread is still
#: waiting on the child — so "has this session's ending been recorded yet?" had no
#: answer, and callers were left inferring it by re-reading the registry until it
#: changed. That is a poll, and a poll that is usually right on an idle machine is
#: exactly the kind of thing that fails under load: three tests raced this reap
#: before it existed, each asserting an entry the watcher was about to remove.
#:
#: Bounded, because a long-lived daemon spawns without limit and an Event per
#: session would otherwise accumulate for the life of the process. The cap is
#: generous relative to any plausible concurrent session count, and evicting the
#: oldest is safe: a caller waiting on an ending that happened thousands of
#: sessions ago has already lost the race it cared about.
_EXIT_RECORDED: "OrderedDict[str, threading.Event]" = OrderedDict()
_EXIT_RECORDED_CAP = 512
_EXIT_RECORDED_LOCK = threading.Lock()


def _exit_event(session_id: str) -> threading.Event:
    """The event for *session_id*, created on first use by either side."""
    with _EXIT_RECORDED_LOCK:
        event = _EXIT_RECORDED.get(session_id)
        if event is None:
            event = threading.Event()
            _EXIT_RECORDED[session_id] = event
            while len(_EXIT_RECORDED) > _EXIT_RECORDED_CAP:
                _EXIT_RECORDED.popitem(last=False)
        return event


def wait_for_exit_recorded(session_id: str, timeout: float = 10.0) -> bool:
    """Block until the watcher has recorded how *session_id* ended.

    Returns ``False`` on timeout rather than raising: the caller usually has
    something better to say about it than this function does.

    This is the synchronisation point for anything that needs to observe the
    *consequences* of an exit — the registry entry removed on a clean exit, or
    kept as the crash signal on a dirty one. Reading the registry without it is a
    race against a thread, not a check.
    """
    return _exit_event(session_id).wait(timeout)


def _watch(process: subprocess.Popen, session_id: str) -> None:
    """Wait for the session's own process and record how it ended.

    Separate from :func:`_drain` on purpose. ``Popen.wait`` returns the moment the
    direct child exits — and it *reaps* it, which matters because an unreaped
    zombie still answers ``kill(pid, 0)`` and would keep the session looking alive.
    Waiting on the PTY instead would delay both until every grandchild dropped the
    slave fd, which is how a ``kill -9``'d session managed to report as running.

    The registry entry is removed only on a clean exit: an entry surviving with a
    dead PID is the crash signal the inventory reads, so leaving it there is the
    point, not an oversight.

    The transcript scrub is the one piece of cleanup that runs on *both* paths.
    It happens here rather than in :func:`_drain` because it must not start while
    anything is still appending to the file, and ``Popen.wait`` returning is the
    signal that the session's own process is gone — see
    :func:`_scrub_transcripts` for the case that signal does not cover. It is also
    the *last* thing this function does: recording how a session ended is the job
    that must not be lost, and cleanup that walks a file the container wrote is
    the more surprising of the two.
    """
    try:
        code = process.wait()
    except OSError as exc:  # pragma: no cover - wait failing is pathological
        logger.warning("platform_session_wait_failed id=%s error=%s", session_id, exc)
        code = None

    clean = code == 0
    append_event(
        "session_exited",
        note=session_id,
        data={"session": session_id, "exit_code": code, "clean": clean},
    )
    if clean:
        # This takes the session's control token with it (registry.remove owns
        # that, since it owns the directory). On the crash path neither goes: the
        # entry is the crash signal, and an entry whose token_ref resolved to
        # nothing would advertise a session as controllable and then deny it.
        registry.remove(session_id)
        # The ports file has no readers once the session is gone; both *logs* do
        # (they are the scrollback source), so only the former is cleaned up.
        try:
            ports_file_for(session_id).unlink()
        except OSError:
            pass
        logger.info("platform_session_exited id=%s code=%s", session_id, code)
    else:
        logger.warning(
            "platform_session_died id=%s code=%s — registry entry kept as the "
            "crash signal", session_id, code,
        )

    # Outside the branch, because it belongs to both endings: a crashed session's
    # entry is kept deliberately, its raw credentials are not.
    _scrub_transcripts(session_id)

    # Last, and in a `finally`-like position by construction: everything above is
    # what "the ending has been recorded" means, so signalling earlier would hand
    # a waiter a half-finished answer — the entry still present, the scrub still
    # running. A waiter blocked on this is released here or by its own timeout.
    _exit_event(session_id).set()


def _claim_slot(
    config: PlatformConfig, request: SpawnRequest
) -> tuple:
    """Check the requested service slot and fill its preset in (issue #245).

    Returns ``(request, service, service_group, members)``: all ``None`` when no
    slot was asked for, otherwise a copy carrying the slot's preset and what the
    slot resolved to — the dev service, and for a group slot the compose project
    plus the member services it covers (issue #312). They are returned so they
    can be *recorded* on the session's entry: occupancy then reads what this
    session took rather than re-deriving it from a presets file that may have
    changed since.
    Nothing else is written: the claim *is* that registry entry, which is why
    there is no release path and a session that dies frees its slot unaided.

    Refusals run permanent-before-transient, so an operator hears the thing they
    have to fix rather than the one that might pass on a retry. The
    service-occupancy check is not redundant with the name-keyed one above it:
    the one-service-one-slot rule is derived from the presets file, which is hot
    by design, so a slot that was unusable when a session started can become its
    service's first resolver once the operator fixes it — at which point only
    the running sessions still say the service is taken.

    The probe is uncached here: a poll can afford a stale answer, an action
    taken on one cannot.

    This does **not** make the claim atomic. The occupancy read and the
    ``registry.register`` that records it are separated by the process start, so
    two spawns racing for one free slot can both get past — the same
    read-then-act window :func:`_refuse_if_run_is_live` and the concurrency cap
    already have, and left open the same way, because closing it means holding a
    lock across every spawn rather than changing anything about slots. The cost
    is therefore reported and not prevented:
    :func:`lmer_platform.slots._held` returns *every* holder, the row says how
    many there are, and the daemon logs ``slot_double_occupancy``.
    """
    if request.slot is None:
        return request, None, None, None

    status = slots.slot_status(config, request.slot, cached=False)
    if status is None:
        declared = [d.name for d in slots.slot_definitions(config)]
        raise SpawnError(
            f"unknown service slot {request.slot!r}"
            + (
                f" — this host declares {', '.join(sorted(declared))}"
                if declared else " — this host declares no service slots"
            )
        )
    if status.unusable_reason is not None:
        raise SpawnError(
            f"service slot {request.slot!r} is not usable: "
            f"{status.unusable_reason}"
        )
    if status.occupant is not None:
        held_by = status.occupant.get("session_id")
        raise SlotOccupied(
            f"service slot {request.slot!r} is already held by session "
            f"{held_by}"
        )
    # The slot's *name* is free but its dev service is not. Only reachable when
    # the presets file changed under a running session, which is why this is
    # measured from the sessions rather than derived from the file.
    if status.service_occupants:
        raise SlotOccupied(
            f"service slot {request.slot!r}: {status.service_busy_reason}"
        )
    if status.service_down_reason is not None:
        target = (
            f"service group {status.service_group!r}" if status.service_group
            else f"service {status.service!r}"
        )
        raise SpawnError(
            f"service slot {request.slot!r} targets {target}, which is not "
            f"running: {status.service_down_reason}"
        )

    # What a group session takes, read once at the claim. Uncached and here
    # rather than on the poll path, for the reason the probe above is uncached:
    # this is the record every later occupancy answer is derived from, so it
    # cannot afford a stale reading. A group that has gone since the probe is a
    # refusal, not an empty member list — an empty list would read as a session
    # holding nothing.
    members = None
    if status.service_group:
        try:
            members = slots.group_members(status.service_group)
        except ServiceError as exc:
            raise SpawnError(
                f"service slot {request.slot!r}: could not resolve the members "
                f"of service group {status.service_group!r}: {exc}"
            )

    # ``validate`` has already refused a request naming both, so the preset
    # field is empty and this fills it.
    return (
        replace(request, preset=status.definition.preset),
        status.service,
        status.service_group,
        members,
    )


def spawn_session(
    config: PlatformConfig,
    request: SpawnRequest,
    *,
    kind: str = "worker",
    publish_registration: Optional[
        Callable[[Callable[[], None], str, int], None]
    ] = None,
) -> SpawnResult:
    """Start a session, register it, and track its run.

    Raises :class:`CapacityError` when the configured concurrency cap is already
    met — enforced here rather than in the caller so every spawn path (the API, the
    assistant's own start) is bounded by the same check. The queue that makes
    waiting graceful is still unbuilt; until then a spawn over cap is simply
    refused with the numbers in the message.

    Raises :class:`SlotOccupied` — or a bare :class:`SpawnError` for a slot that
    is unknown or unusable — when ``request.slot`` names a service slot this
    spawn cannot have (:func:`_claim_slot`), which is also where a slot spawn
    gets its preset filled in.

    ``max_concurrent_sessions`` counts *workers*: see :func:`_live_worker_count`
    for why the assistant is not one of them.

    Raises :class:`RunAlreadyLive` when a live session already holds the run this
    spawn would land in — the one-run-one-session invariant, here for the reason
    the cap is here and with the reasoning in :func:`_refuse_if_run_is_live`.
    """
    request = request.validate()
    if kind not in registry.SESSION_KINDS:
        raise SpawnError(f"invalid session kind {kind!r}")

    live = _live_worker_count()
    if live >= config.max_concurrent_sessions:
        raise CapacityError(
            f"concurrency cap reached: {live}/{config.max_concurrent_sessions} "
            "worker sessions already running"
        )

    # After the cap, so a host that is simply full says so rather than blaming
    # the slot.
    request, slot_service, slot_group, slot_services = _claim_slot(config, request)

    # Two URLs, because "what this run's repo is" and "what its identity is
    # derived from" are separable claims — only the first is recorded anywhere.
    repo_url, identity_url = _repo_urls(request)
    host, project, slug = derive_run_identity(request, identity_url)
    # As early as the identity allows, and before the command is built: a spawn
    # refused for a run that is already running leaves nothing behind — no argv, no
    # port drawn, no directory made.
    _refuse_if_run_is_live(host, project, slug)
    command = _build_command(config, request)

    # Before anything is created on disk, so a range with no free port leaves no
    # trace of a session that was never started.
    control_port = _pick_control_port()

    session_id = registry.new_session_id()
    log_file = log_path_for(session_id)
    try:
        # Through the store so every level takes STATE_DIR_MODE — the bare
        # mkdir left logs/ at the umask around 0600 log files (T93 finding).
        store.ensure_state_dir(log_file.parent)
    except OSError as exc:
        raise SpawnError(f"cannot create log directory {log_file.parent} ({exc})")

    # The transcript's host home, with one subdirectory per harness mounted in
    # where that harness writes its session JSONL, so the file the chat view reads
    # outlives the container (`lmer` runs it --rm) whichever harness ran. Only for
    # sessions the platform spawns: persisting every lmer invocation's transcript
    # is a separate decision, and this is not the place to take it.
    transcript_dir = _prepare_transcript_dir(session_id)
    transcript_mounts = (
        _prepare_transcript_subdirs(transcript_dir)
        if transcript_dir is not None
        else []
    )
    # The session's ask channel (T23): where it posts questions for the operator
    # and reads the answers back.
    ask_dir = _prepare_ask_dir(session_id)
    # Where the session writes its own log, if its image knows how to (#150). The
    # directory is the offer; the file inside it is the session's answer, and the
    # read path takes "no file" for exactly the answer it is.
    container_log_dir = _prepare_container_log_dir(session_id)

    # One insertion, not one per mount: _with_host_flags inserts right after
    # --fastapi, so calling it twice would interleave the mounts in reverse and
    # make the command's shape depend on the order of these lines.
    host_flags: list = []
    if transcript_mounts:
        host_flags += _transcript_mount_flags(transcript_mounts)
    if ask_dir is not None:
        host_flags += _ask_mount_flags(ask_dir)
    if container_log_dir is not None:
        host_flags += _container_log_mount_flags(container_log_dir)
    if host_flags:
        command = _with_host_flags(command, host_flags)

    control_token, token_file = _mint_control_token(session_id)

    # Facts the child cannot work out for itself: where to report the ports it
    # publishes (it picks those, see ports_file_for), the port and token its
    # control plane must use so the registry entry below is true from the start,
    # and — when the channel directory exists — the container-side path of its ask
    # channel. That last one is the flag an agent tests to know it was started by
    # the orchestrator, which is why it is set here and only here: a session
    # spawned without a usable channel directory must not claim to have one.
    child_env = {
        **os.environ,
        "LMER_PLATFORM_PORTS_FILE": str(ports_file_for(session_id)),
        "LMER_FASTAPI_PORT": str(control_port),
        "LMER_FASTAPI_TOKEN": control_token,
    }
    if ask_dir is not None:
        child_env[ask.ASK_DIR_ENV] = ask.CONTAINER_ASK_DIR
    else:
        # Never inherited: the daemon's own environment carrying this variable
        # (a daemon started from inside a session, say) would otherwise tell an
        # agent it has a channel that was never mounted. Blank rather than
        # absent, because the child is `lmer` and it seeds its own environment
        # from .env files first-wins — a key that is merely absent is a key a
        # file may re-supply. Blank already reads as unset on every consumer
        # (resolve_channel_dir strips it; the runners render the ask fragment
        # only on a non-blank value).
        child_env[ask.ASK_DIR_ENV] = ""
    # The declared→staged link pairs for the entrypoint (#293), filtered to
    # mounts actually prepared. Seeded fresh, never inherited (a daemon's own
    # environment would carry another session's pairs), and blank rather than
    # absent for the reason the two variables below give.
    mounted_destinations = {container_dir for _, container_dir in transcript_mounts}
    child_env[MOUNT_LINKS_ENV] = format_mount_links(
        "",
        [
            (declared, staged)
            for declared, staged in _harness_session_links()
            if staged in mounted_destinations
        ],
    )
    if request.no_repo:
        # Spec D17, structurally: `lmer` skips repo resolution on this and the
        # container skips the workspace clone, so the session has nothing to edit
        # rather than instructions not to.
        child_env[NO_REPO_ENV] = "1"
    else:
        # Also never inherited, and for the sharper version of the same reason: a
        # daemon whose shell exported LMER_NO_REPO would otherwise hand every
        # worker an empty /workspace while its request still names a repository —
        # a session that looks like it is working on code it does not have. Blank
        # for the same reason as above: deleting the key only hides it from this
        # environment, and the child's own .env seeding would hand it straight
        # back. Blank is falsy to get_bool_env on the host and never equals "1"
        # in the container, so it reads as unset the whole way down.
        child_env[NO_REPO_ENV] = ""

    master_fd, slave_fd = pty.openpty()
    try:
        process = subprocess.Popen(
            command,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=child_env,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        os.close(master_fd)
        os.close(slave_fd)
        # The token was minted for a process that does not exist; nothing will
        # ever come back to read it, so it is litter — and litter that is a
        # credential does not get left lying around.
        with _suppress_oserror():
            token_file.unlink()
        raise SpawnError(f"cannot start {command[0]} ({exc})")
    finally:
        # The child holds its own copy of the slave end.
        with _suppress_oserror():
            os.close(slave_fd)

    # Register before starting the drain thread: the thread's cleanup path
    # removes the entry, and it must never run against an entry that was never
    # written.
    def register() -> None:
        registry.register(
            session_id,
            kind=kind,
            pid=process.pid,
            task={
                "taskdef": request.taskdef,
                "target": request.target,
                "repo": repo_url,
                "preset": request.preset,
                # Names only, which is all that was ever passed: the presets these
                # select stay on the host (their env can hold credentials), and the
                # entry is a debugging artifact people paste around.
                "agents": request.agents,
                "harness": request.harness,
                # What the spawn *asked* for, which is ``None`` for the ordinary
                # request that names no model. The session overwrites it with what
                # it actually resolved as soon as it reports back (absorb_ports), so
                # this key answers "which model is driving this run" rather than
                # only "which model was requested".
                "model": request.model,
                # Recorded rather than re-derived, because the presets file is hot:
                # a slot repointed while this session runs must not change what it
                # is understood to be holding (issue #245).
                "slot_service": slot_service,
                # A group session can retarget to any member, so it holds all of
                # them; the members are resolved once, here, rather than re-read on
                # every poll (issue #312).
                "slot_service_group": slot_group,
                "slot_services": slot_services,
            },
            run={"host": host, "project": project, "slug": slug},
            # The *only* record that the slot is taken — nothing writes an occupancy
            # file — so a session that dies takes its claim with it (issue #245).
            slot=request.slot,
            # Reachability, minus the credential: ``token_ref`` is a path, and
            # registry.register rejects an inline ``token`` outright (spec §6.2).
            control={
                "host": _CONTROL_HOST,
                "port": control_port,
                "token_ref": str(token_file),
            },
            log_path=str(log_file),
            started_at=utc_now_iso(),
        )

    # Most sessions publish only their registry entry. The assistant also has a
    # lifecycle-state pointer that must become visible with that entry, so it
    # supplies a tiny publication wrapper. It runs only after process startup and
    # receives registration as a thunk, keeping its lock around the two writes
    # rather than around the whole spawn.
    if publish_registration is None:
        register()
    else:
        publish_registration(register, session_id, process.pid)

    # Track the run so it appears in this orchestrator's fleet view (D25). Only
    # possible with a full identity; a session whose repo URL could not be parsed
    # still shows up via its registry entry — for as long as that entry exists,
    # which is the whole problem the warning below describes.
    warning: Optional[str] = None
    if host and project and slug:
        runs.track(
            host, project, slug,
            source="spawned",
            taskdef=request.taskdef,
            target=request.target,
            repo=repo_url,
            session_id=session_id,
        )
        # Only now: until the call above, this was a run meta.write would refuse.
        warning = _write_run_meta(request, host, project, slug)
    elif request.no_repo:
        # Not the failure below, and kept apart from it deliberately: a session
        # with no repository (spec D17) has no run to file anywhere, so nothing
        # was lost. A warning that also fires on the designed case is a warning
        # operators learn to skip — which is how the real one went unnoticed.
        logger.info(
            "platform_spawn_no_repo_session id=%s — no repository (spec D17), so "
            "there is no run to track", session_id,
        )
        # Nothing was lost by not tracking — but a note this spawn asked for was,
        # and that is worth saying even on the designed case.
        warning = _dropped_meta_warning(request)
    else:
        warning = _untracked_run_warning(request, repo_url)
        logger.warning(
            "platform_spawn_untracked_run id=%s — %s", session_id, warning
        )

    # Two threads, because PTY closure and child exit are different events (see
    # _drain / _watch). One tees the log, the other records the ending.
    threading.Thread(
        target=_drain,
        args=(master_fd, log_file, session_id),
        name=f"lmer-platform-drain-{session_id}",
        daemon=True,
    ).start()
    threading.Thread(
        target=_watch,
        args=(process, session_id),
        name=f"lmer-platform-watch-{session_id}",
        daemon=True,
    ).start()

    append_event(
        "session_spawned",
        note=session_id,
        data={
            "session": session_id,
            "pid": process.pid,
            "taskdef": request.taskdef,
            "target": request.target,
            "run": {"host": host, "project": project, "slug": slug},
        },
    )
    logger.info(
        "platform_session_spawned id=%s pid=%s taskdef=%s",
        session_id, process.pid, request.taskdef,
    )

    return SpawnResult(
        session_id=session_id,
        pid=process.pid,
        log_path=log_file,
        host=host,
        project=project,
        slug=slug,
        command=command,
        control_host=_CONTROL_HOST,
        control_port=control_port,
        warning=warning,
    )
