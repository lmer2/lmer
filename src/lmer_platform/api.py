"""The platform's HTTP control plane.

The fleet view is read-only (spec §5.4): what runs exist, which are alive, which
need a human. The verbs that change a session are narrow and deliberate — spawn
one, and read or write the terminal of one (:mod:`lmer_platform.session_io`); the
queue and slots arrive with M3.

Authentication
--------------
One shared secret (spec D9/§10.5), accepted two ways:

- ``Authorization: Bearer <secret>`` — for CLI and API clients.
- ``Authorization: Basic <base64>`` — so a **browser** can reach the platform
  before the SPA exists. The 401 carries ``WWW-Authenticate: Basic``, which makes
  the browser show its native credential prompt; the username is ignored and
  either field may carry the secret. Without this, the first live test would need
  a JavaScript client to send a header, which is a silly prerequisite for
  "does the daemon work".

The comparison is constant-time (:func:`secrets.compare_digest`). There is
deliberately **no artificial delay** on failure: the secret is 256 bits of
``token_urlsafe``, so online guessing is not the threat, while a sleep-per-failure
would hand an attacker an amplification primitive — many cheap connections each
pinning a threadpool worker. Failures are logged instead.

The one route that does *not* take the secret is the tty WebSocket, because a
browser cannot reliably put a header on a handshake. It takes a single-use ticket
minted by an authenticated ``POST`` instead; :mod:`lmer_platform.session_io`
documents why that is safer than the obvious ``?secret=…``.

Why the handlers are synchronous
--------------------------------
``build_state`` may run a throttled ``git fetch``. Declaring the routes with
``def`` rather than ``async def`` makes Starlette run them in a threadpool, so a
slow fetch cannot stall the event loop for every other request. This is the same
choice ``lmer_cli.supervisor`` made for its endpoints. The tty socket is the
exception that has to be ``async def`` — it holds a connection for the life of a
session, which is precisely what a threadpool worker must not do — so every
blocking call it makes goes through ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import os
import secrets as secrets_mod
from pathlib import Path
from typing import Callable, Optional

# Discovery for the spawn form's selects. Private helpers on purpose, the same
# trade :mod:`lmer_platform.spawn` makes with ``_parse_repo_url``: these are the
# functions ``lmer`` itself resolves a taskdef with, and a second copy here would
# offer the operator a menu the CLI disagrees with. Nothing here is imported
# lazily — ``lmer_cli.cli`` is already on this module's import path through
# :mod:`lmer_platform.spawn`, and it imports both of the others itself.
from lmer_cli.cli import _discover_all_tasks, _discover_tasks, _get_taskdef_paths
from lmer_cli.presets import load_presets
from lmer_cli.runtime import repo_root_path

# The forge URL mapping, imported rather than rebuilt (T66). ``web_url_for``
# cannot be called from here — it resolves paths under the *container's* ``/work``
# checkout, which this host does not have — but the mapping underneath it is the
# same question, and a second copy of "which forge puts blob where" is how one
# copy keeps a bug the other one fixed. ``_web_base_from_remote`` is private and
# reused deliberately, the trade :mod:`lmer_platform.workrepo` makes with
# ``_scrub_credentials``: stripping credentials out of a git URL must have exactly
# one definition, because everything it misses becomes a link in a browser.
from work_repo.git_ops import (
    FORGE_GITLAB,
    _web_base_from_remote,
    checkout_branch,
    forge_web_url,
)

from . import SCHEMA_VERSION
from . import ask
from . import assistant
from . import lifecycle
from . import meta
from . import reattach
from . import relations
from . import session_io
from . import transcripts
from .answer import AnswerError, AnswerRequest, answer_run
from .ask import AskChannelError
from .assistant import AssistantError
from .config import (
    ASSISTANT_SETTING_KEYS,
    ConfigError,
    PlatformConfig,
    assistant_settings,
    configured_repo_url,
    update_stored,
    validate_assistant_override,
)
from .inventory import build_inventory
from .lifecycle import LifecycleError
from .meta import MetaError
from .registry import list_sessions, prune_dead, read_session
from .relations import RelationError
from .resume import ResumeError, ResumeRequest, resume_run
from .runs import RunIndexError, forget, list_tracked, run_key, track
from .session_io import ControlPlaneError, SessionIOError
# ``_repo_urls`` and ``derive_run_identity`` are the pair that predicts a run's
# host and project, and the work-repo project taskdef tier is filed under exactly
# that pair (T73). Private and reused deliberately, the same trade this module
# already makes with ``lmer_cli.cli``'s discovery helpers: a second reading of a
# repo URL here would offer the operator a tier the container never searches.
from .spawn import (
    CapacityError,
    RunAlreadyLive,
    SpawnError,
    SpawnRequest,
    _repo_urls,
    absorb_ports,
    derive_run_identity,
    spawn_session,
)
from .store import utc_now_iso
from .ui_build import dist_dir
from .workrepo import mirror_status, pull, resolve_run_dir, run_dirs

logger = logging.getLogger("lmer_platform.api")

__all__ = [
    "build_state", "create_app", "discover_spawn_options", "REALM",
    "WS_POLICY_VIOLATION",
]

REALM = "lmer platform"

#: Close code for a tty socket the platform will not serve. 1008 (policy
#: violation) rather than a private code because a browser can read neither: a
#: handshake refused before ``accept`` reaches it as a bare connection failure.
#: Hence the client contract — mint a fresh ticket and retry once.
WS_POLICY_VIOLATION = 1008

#: RFC 6455 caps a close reason at 123 bytes; the reasons here are explanations,
#: so they are truncated rather than assumed short.
_WS_REASON_LIMIT = 120

#: What a starting assistant is told when its predecessor left it nothing (T60).
#: A fact about the host rather than an error, and the ``orchestrate`` taskdef
#: turns on being able to tell it apart from an absent route: it says to read the
#: handover before saying anything, and to state plainly that it was not briefed
#: instead of pretending it was.
_NO_HANDOFF_NOTE = (
    "no handover note has been written on this host — you are the first "
    "incarnation, or the one before you was stopped before it wrote one. Start "
    "from GET /api/state and say you were not briefed, rather than inventing what "
    "you were told."
)

#: What a starting assistant is told when the operator has set no standing orders
#: (T87). The ordinary state of a fresh host, and it has to be *said* for the same
#: reason :data:`_NO_HANDOFF_NOTE` does: the taskdef tells the assistant to fetch
#: this before it says anything, and "nobody has told me anything yet" must be
#: distinguishable from "this build has no such route" without inferring either
#: from an empty string.
_NO_INSTRUCTIONS_NOTE = (
    "the operator has set no standing instructions on this host. That is the "
    "ordinary state and not a missing briefing — behave as the taskdef says. When "
    "they state a standing preference in chat, confirm the wording back and POST "
    "the whole document here so every later incarnation reads it too."
)

#: How many of one run's files a listing names. A run dir is a handful of files
#: plus a ``reports/`` directory that grows one per report, so this is a bound on
#: the pathological case rather than a limit anyone meets — and past it the reply
#: says it was truncated and still links the directory itself.
_MAX_RUN_FILES = 200

#: What the file listing tells a reader that the list itself cannot (T66). Both
#: halves are load-bearing: the mirror is force-reset to the remote on every pull
#: (spec D24), which is *why* these links are safe — anything present in it is by
#: definition pushed, so a link cannot point at content the forge has never seen —
#: and a missing ``url`` is a work repo with no links to build rather than a
#: missing file.
_RUN_FILES_NOTE = (
    "the files in this daemon's mirror of the run's directory. The mirror is "
    "force-reset to the remote on every pull, so everything listed here is "
    "already pushed and its link cannot point at content the forge does not have. "
    "A file whose url is null belongs to a work repo this daemon builds no links "
    "for — no work repo is configured, or work_repo_forge is set to none — and the "
    "name is still the name."
)


def _query_int(params, name: str, default: int) -> int:
    """One integer query parameter, falling back rather than failing.

    The tty socket is a plain Starlette route (see :func:`create_app`), so nothing
    validates its query string for it. A malformed offset is not worth refusing a
    terminal over — the default is the tail, which is what a client that could not
    say where to start wanted anyway.
    """
    try:
        return int(params[name])
    except (KeyError, TypeError, ValueError):
        return default


def _positive_int(value: object) -> bool:
    """Whether *value* is an int a terminal dimension could be.

    ``bool`` is an ``int`` subclass, so ``{"rows": true}`` would otherwise arrive
    as a one-row terminal — the same check :mod:`lmer_platform.registry` makes of
    a pid, for the same reason.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _config_summary(config: PlatformConfig, mirror: dict) -> dict:
    """Operator-visible configuration. Carries no secret and no credentialed URL.

    The work repo URL is taken from the mirror status rather than the config,
    because that copy has already been scrubbed of any embedded credentials.

    Every key here is a setting something reads. ``max_concurrent_assistant_spawns``
    was served from this block while nothing enforced it, which made this payload a
    place a cap could *look* real — see :mod:`lmer_platform.config` for why it was
    deleted rather than wired up. ``max_concurrent_sessions`` is the one cap in
    force, and it bounds workers: the assistant holds its own slot
    (:func:`lmer_platform.spawn._live_worker_count`).
    """
    return {
        "bind_address": config.bind_address,
        "bind_port": config.bind_port,
        "base_url": config.base_url,
        "work_repo_url": mirror.get("url"),
        "work_repo_pull_interval": config.work_repo_pull_interval,
        "max_concurrent_sessions": config.max_concurrent_sessions,
        "max_followup_rounds": config.max_followup_rounds,
        "autonomous_default": config.autonomous_default,
        "park_idle_side": config.park_idle_side,
    }


def _meta_reply(host: str, project: str, slug: str, record) -> dict:
    """The body both metadata routes answer with (T52).

    One shape for the read and the write, so a client that just set a title does
    not have to re-read to learn what was stored — the reply *is* what was
    stored, after collapsing and bounding.

    Two fields here are not the metadata and are the reason this helper exists.
    ``limits`` is the daemon's own bounds, so a form can size its counter from
    the number that will actually refuse it rather than from a copy that drifts.
    ``local`` and its ``note`` say the thing nobody would otherwise be told: this
    is platform state (spec D3), so it belongs to this orchestrator and not to
    the run — see :mod:`lmer_platform.meta`.
    """
    return {
        "run": {"host": host, "project": project, "slug": slug},
        "meta": record.to_dict(),
        "limits": {
            "title": meta.MAX_TITLE_CHARS,
            "description": meta.MAX_DESCRIPTION_CHARS,
        },
        "local": True,
        "note": meta.LOCALITY_NOTE,
    }


def _related_ref(body: dict) -> tuple:
    """The *other* run named by a relate/unrelate body, as a triple.

    One nested object rather than three more top-level fields, and that is the
    decision worth stating: the thing being related is a **run**, so it carries a
    run's three field names — ``related_host``/``related_project``/``related_slug``
    would be a second spelling of the identity every other route on this API
    already has one spelling for. The refusal names the shape because the mistake
    it catches (a bare string, or the composite key) is the natural one.

    Raises :class:`lmer_platform.relations.RelationError` rather than an
    ``HTTPException`` so the two routes keep one handler and one status source —
    ``HTTPException`` is not in scope out here anyway (see :func:`create_app`).
    """
    related = body.get("related")
    if not isinstance(related, dict):
        raise RelationError(
            'related must be an object naming the other run — {"host": ..., '
            '"project": ..., "slug": ...}, the same three fields this body names '
            f"the first run with — got {type(related).__name__}"
        )
    return related.get("host"), related.get("project"), related.get("slug")


def _run_ref_dict(run: tuple) -> dict:
    """A ``(host, project, slug)`` triple as the object every reply names a run with.

    Echoed back on a write so a caller reading the reply alone can see which two
    runs it acted on, in the shape ``run`` already uses on the same body.
    """
    return {"host": run[0], "project": run[1], "slug": run[2]}


def _relations_reply(
    host: str, project: str, slug: str, extra: Optional[dict] = None
) -> dict:
    """The body all three relation routes answer with (T53).

    One shape for the read and both writes, for :func:`_meta_reply`'s reason: a
    caller that just related two runs holds the run's relations as they now are and
    does not have to re-read to learn what happened. *extra* is what only a write
    can say — which run it acted on, and whether anything changed.

    Each relation carries the title this orchestrator has for that run when there
    is one. That is a decoration added here rather than in
    :mod:`lmer_platform.relations`, deliberately: the relation itself is about run
    keys and nothing else, while a *listing* names a run by its title when it has
    one (T65) — and a switcher showing two slugs that differ by one word is the
    hunt this feature exists to end. Read from one snapshot, not one read per
    relation.

    ``local`` and its ``note`` say the thing nobody would otherwise be told: this
    is platform state (spec D3), so these relations belong to this orchestrator
    and not to the runs — see :mod:`lmer_platform.relations`.
    """
    titles = meta.load_all()
    payload = {
        "run": {"host": host, "project": project, "slug": slug},
        "relations": [
            {
                **entry.to_dict(),
                "title": meta.RunMeta.from_dict(
                    entry.key, titles.get(entry.key)
                ).title,
            }
            for entry in relations.list_for(host, project, slug)
        ],
        # The daemon's own cap, so a UI can say how many are left from the number
        # that will actually refuse the next one rather than from a copy of it.
        "limits": {"relations": relations.MAX_RELATIONS_PER_RUN},
        "local": True,
        "note": relations.LOCALITY_NOTE,
    }
    payload.update(extra or {})
    return payload


def _handoff_reply(state) -> dict:
    """The body both assistant-handoff routes answer with (T60).

    One shape for the read and the write, for :func:`_meta_reply`'s reason: an
    assistant that has just written its note does not have to re-read to learn
    what was stored, and what comes back *is* what was stored — stripped, since
    :mod:`lmer_platform.assistant` bounds and strips it on the way in.

    ``limit`` is the daemon's own ceiling rather than a copy of it, so a client
    counts against the number that will actually refuse the text. ``generation``
    rides along because it is what makes the note interpretable: a note from
    generation 4 read by generation 5 is a handover, and one read by generation 4
    is its own draft.

    ``note`` is present even when there is nothing to say (``None`` then), and
    carries the one distinction a starting assistant has to be able to make —
    see :data:`_NO_HANDOFF_NOTE`.
    """
    return {
        "handoff": state.handoff,
        "handoff_at": state.handoff_at,
        "generation": state.generation,
        "limit": assistant.MAX_HANDOFF_CHARS,
        "note": None if state.handoff else _NO_HANDOFF_NOTE,
    }


def _instructions_reply(state) -> dict:
    """The body both standing-instruction routes answer with (T87).

    Shaped like :func:`_handoff_reply` on purpose — one read/write shape, the
    daemon's own ``limit`` rather than a copy of it, and a ``note`` that is present
    even when there is nothing to say — because the two documents are read by the
    same agent in the same startup breath and a second shape would be a second
    thing to get wrong. What differs is what the fields *mean*: this document is
    never consumed, so there is no generation to interpret it against; it is the
    operator's standing orders and it is as true for generation 12 as it was for
    generation 1.

    Scrubbed here as well as in :func:`lmer_platform.assistant.set_instructions`,
    which is the same both-directions rule
    :func:`lmer_platform.transcripts._scrub` already serves and the same shape as
    the digest spool being bounded on write and trimmed on read: this route also
    serves a *browser*, and the file underneath is a plain hand-editable one, so
    "it was clean when we stored it" is not a property this end can assume.
    """
    document = state.instructions
    return {
        "instructions": transcripts._scrub(document) if document else None,
        "instructions_at": state.instructions_at,
        "limit": assistant.MAX_INSTRUCTIONS_CHARS,
        "note": None if document else _NO_INSTRUCTIONS_NOTE,
    }


#: What both assistant-config routes have to say out loud, every time (issue
#: #234): the two facts about this surface that read as bugs when discovered by
#: experiment. A changed setting that does nothing *visible* looks broken unless
#: the reply says the running incarnation is deliberately untouched, and a
#: persisted value that never takes effect looks broken unless the reply says an
#: export is standing in front of it.
_CONFIG_SCOPE_NOTE = (
    "settings apply to the NEXT incarnation: the running assistant keeps its "
    "context window until it is stopped or rotated. A value whose source is "
    "'env' is an export shadowing config.json — what POST persists here has no "
    "effect until that export is removed."
)


def _assistant_config_reply(extra: Optional[dict] = None) -> dict:
    """The body both assistant-config routes answer with (issue #234).

    One shape for the read and the write, for :func:`_meta_reply`'s reason: the
    reply to a write is the effective configuration as it now stands, re-resolved
    through the same chain the next start will use — which is what lets a client
    see, without a second read, that the value it just persisted is (or is not)
    the one in effect. Each key carries its ``source`` because the chain's one
    trap is an export shadowing the file; see :data:`_CONFIG_SCOPE_NOTE`.

    *extra* is what only a write can say — which keys it changed.
    """
    payload = {
        "settings": {
            key: setting.to_dict()
            for key, setting in assistant_settings().items()
        },
        "note": _CONFIG_SCOPE_NOTE,
    }
    payload.update(extra or {})
    return payload


def _body_launch_settings(payload: dict) -> Optional[dict]:
    """The per-call launch overrides in a start/rotate body, or ``None``.

    Everything except ``handoff`` — which travels beside them in the same body
    and is not a launch setting — is passed through, unknown keys included,
    so an unknown key gets :func:`lmer_platform.assistant._launch_settings`'s
    own refusal. Lifting only the known keys here would silently drop a typo
    (``{"modle": "gpt-x"}``): a 200, a session running the standing settings,
    and an operator who believes the override took. Values are not coerced
    either, the rule every assistant route follows: an unusable one keeps the
    module's refusal (400, naming the field) instead of arriving as ``None``
    and reading as "leave it alone".
    """
    named = {key: value for key, value in payload.items() if key != "handoff"}
    return named or None


def _run_dir_files(root: Path) -> tuple[list[str], bool]:
    """Run-dir-relative paths of the readable files under *root*, sorted.

    Read off disk rather than from a list of the names a run is *supposed* to
    hold: what a taskdef writes changes with the taskdef, and a hardcoded list
    would hide the one file somebody came here to read. Directories are descended
    into so ``reports/`` and its contents appear as ``reports/<name>``; dot
    entries are skipped, since nothing a run writes starts with one and anything
    that does belongs to git or an editor; an entry this daemon cannot read is
    dropped rather than linked, because a link to a file that is not there is
    worse than its absence.

    Returns the paths and whether :data:`_MAX_RUN_FILES` truncated them. Never
    raises — the listing is a convenience, and an unwalkable directory is a thing
    to report as empty rather than a reason to fail the request.
    """
    found: list[str] = []
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if not name.startswith(".")]
            here = Path(dirpath)
            for name in filenames:
                if name.startswith("."):
                    continue
                entry = here / name
                if not entry.is_file() or not os.access(entry, os.R_OK):
                    continue
                found.append(entry.relative_to(root).as_posix())
    except OSError as exc:
        logger.warning("platform_run_files_unwalkable path=%s error=%s", root, exc)
    found.sort()
    return found[:_MAX_RUN_FILES], len(found) > _MAX_RUN_FILES


def _run_files_reply(
    config: PlatformConfig, host: str, project: str, slug: str
) -> dict:
    """One run's files and the forge URL of each (T66).

    The operator asked: "the detail view should have a list of run files, clicking
    takes the user to the work repo (gitlab, github etc.,) to view the file". The
    run dir is where a run's whole record lives — the spec, the plan, the ledger,
    the events, the reports — and the UI showed none of it.

    Two things here are not what the container-side :func:`work_repo.git_ops.
    web_url_for` does, and both are forced by where this runs. The paths are
    resolved against the daemon's **mirror** (:func:`resolve_run_dir`), because
    there is no ``/work`` on the host; and the web base comes from
    ``config.work_repo_url`` rather than a checkout's ``origin``, because the
    mirror's origin is deliberately rewritten to a credential-free URL and the
    configuration is the copy that is always there. That configured URL is
    routinely **tokenised** (``LMER_WORK_REPO`` normally is), so it goes through
    ``_web_base_from_remote``, which keeps only host and project path — this is
    the one property in here that must not regress, since what comes out is
    rendered as a link in a browser and a link is a URL somebody copies.

    A run absent from the mirror answers ``present: false`` with no files, which
    is a state and not an error: a freshly spawned run has pushed nothing yet.

    The forge is resolved exactly as ``web_url_for`` resolves it — the operator's
    ``work_repo_forge`` when set, then the host, then GitLab. T66 shipped this call
    with no default on the reasoning that a guessed layout 404s, and that was the
    wrong place to be honest: the deployed work repo is a self-hosted GitLab at
    ``git.<domain>``, which detection cannot name, so the same run dir came back
    linked from ``work`` and as plain names here — one surface contradicting the
    other about a URL either could have built. The opt-out moved to the knob, where
    an operator who really is on something else can say ``none`` once.
    """
    ref = resolve_run_dir(config, host, project, slug)
    base = _web_base_from_remote((config.work_repo_url or "").strip()) or ""
    branch = checkout_branch(config.mirror_path)
    forge = config.work_repo_forge

    files: list[dict] = []
    truncated = False
    if ref is not None:
        names, truncated = _run_dir_files(ref.path)
        files = [
            {
                "name": name,
                "url": forge_web_url(
                    base,
                    branch,
                    f"{ref.rel_path}/{name}",
                    forge=forge,
                    default_forge=FORGE_GITLAB,
                ),
            }
            for name in names
        ]

    return {
        "run": {"host": host, "project": project, "slug": slug},
        "rel_path": ref.rel_path if ref is not None else None,
        "present": ref is not None,
        "files": files,
        "truncated": truncated,
        "run_dir_url": (
            forge_web_url(
                base,
                branch,
                ref.rel_path,
                is_dir=True,
                forge=forge,
                default_forge=FORGE_GITLAB,
            )
            if ref is not None
            else None
        ),
        "note": _RUN_FILES_NOTE,
    }


def build_state(config: PlatformConfig, *, force_pull: bool = False) -> dict:
    """Assemble the fleet view for the runs **this orchestrator tracks**.

    Scope comes from the local run index, never from the shared work repo (spec
    D25): the repo is shared across devs, so enumerating it would report other
    people's blocked runs as needing this operator's input. A fresh orchestrator
    tracks nothing and returns an empty view — correct, not broken.

    Sessions are read with ``live_only=False`` on purpose: a stale entry is the
    evidence that a session crashed, and filtering it out here would silently
    drop crashed runs from the attention list (see
    :mod:`lmer_platform.inventory`).
    """
    mirror = pull(config, force=force_pull).to_dict()
    # Ports are reported by the child after the spawn returned, so folding them in
    # happens on the read path (see spawn.absorb_ports).
    sessions = absorb_ports(list_sessions(live_only=False))
    tracked = list_tracked()
    refs = [
        ref
        for ref in (
            resolve_run_dir(config, entry.host, entry.project, entry.slug)
            for entry in tracked
        )
        if ref is not None
    ]
    # A live session's open questions ride on the fleet payload rather than
    # costing a request per run: the view is polled from a phone, and "which of
    # these is waiting on me" is the question it exists to answer.
    inventory = build_inventory(
        refs, sessions, tracked=tracked, questions=ask.pending_by_session(sessions)
    )

    payload = {
        "schema": SCHEMA_VERSION,
        "generated_at": utc_now_iso(),
        "config": _config_summary(config, mirror),
        "mirror": mirror,
        "tracked": {
            "count": len(tracked),
            "runs": [entry.to_dict() for entry in tracked],
        },
    }
    payload.update(inventory.to_dict())
    if not tracked:
        payload["hint"] = (
            "No runs are tracked yet. The view is scoped to runs this "
            "orchestrator spawned or adopted — never the whole shared work "
            "repo. Adopt one with POST /api/runs/adopt or "
            "`lmer platform adopt <host>/<project>/<slug>`."
        )
    return payload


#: What a root has to call the directory its taskdefs live in.
_TASKDEF_DIRNAME = "taskdef"


def _package_taskdef_root() -> Path:
    """The root beside the installed package: …/<root>/src/lmer_platform → …/<root>.

    Its own function so a test can neutralise this one candidate and exercise the
    others — in a checkout it always resolves, which would otherwise mask them.
    """
    return Path(__file__).resolve().parents[2]


def _holds_taskdefs(root: Path) -> bool:
    """Whether *root* has a ``taskdef/`` with at least one taskdef in it.

    ``lmer``'s own :func:`lmer_cli.cli._discover_tasks` decides what counts, so
    "is this a taskdef directory" is answered in one place. Requiring a hit also
    keeps a candidate that would contribute nothing from shadowing a later one
    that would, and keeps an unrelated project's ``taskdef/`` — reachable through
    the cwd probe below — from being read as lmer's.
    """
    return bool(_discover_tasks(root / _TASKDEF_DIRNAME))


def _taskdef_root_candidates() -> list:
    """Every place the built-in taskdefs might be, in precedence order."""
    candidates: list = [_package_taskdef_root()]

    root = repo_root_path()
    if root is not None:
        candidates.append(root)

    candidates.append(Path.cwd())
    return candidates


def _builtin_taskdef_root() -> Optional[Path]:
    """The root whose ``taskdef/`` holds the built-in taskdefs, or ``None``.

    This asks "where are the taskdefs" rather than "is this an installed
    package", for the reason :func:`lmer_platform.ui_build.web_source_dir` sets
    out at length — it is the same bug a second time. Discovery used to hand
    :func:`lmer_cli.runtime.repo_root_path` straight to
    :func:`lmer_cli.cli._get_taskdef_paths`, and that is ``None`` whenever
    install mode says INSTALLED; the paths list came back empty, discovery
    enumerated nothing, and the run dialog offered only the single name its own
    form defaults to. Install mode was never the question.

    Resolution order, first hit wins:

    1. Beside the installed package (``…/src/lmer_platform`` → ``…/taskdef``),
       which covers an editable install and a plain checkout.
    2. The repo root, for developer mode.
    3. ``./taskdef`` under the current directory — a daemon started from a
       checkout it was not installed from.

    ``$LMER_TASKDEF_PATHS`` is deliberately not a candidate here: it is not a
    fallback but an addition, and :func:`lmer_cli.cli._get_taskdef_paths` already
    appends every directory it names *on top of* whatever this returns. So a host
    with no checkout anywhere still has an explicit way in, and it keeps working
    once this does find something.
    """
    for candidate in _taskdef_root_candidates():
        if _holds_taskdefs(candidate):
            return candidate
    return None


def _mirror_project_dir(mirror: Path, host: str, project: str) -> Optional[Path]:
    """``{mirror}/{host}/{project}``, or ``None`` when that is not inside the mirror.

    The containment re-check is the one in :func:`_safe_asset`, for the same
    reason: ``host``/``project`` come out of a repo URL a caller supplied, and
    ``_parse_repo_url`` hands back whatever path that URL had — ``..`` segments
    included. Without this, a crafted repo URL would aim the taskdef search at a
    directory outside the mirror and the menu would report what it found there.
    """
    if not (host and project):
        return None
    root = mirror.resolve()
    try:
        candidate = (root / host / project).resolve()
        candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate


def _work_repo_taskdef_dirs(
    config: Optional[PlatformConfig],
    *,
    target: str = "",
    repo_url: Optional[str] = None,
) -> list:
    """The work repo's taskdef tiers, read off this daemon's mirror (T73).

    The container searches ``{work}/{host}/{project}/taskdef`` and then
    ``{work}/taskdef`` before anything else
    (:func:`lmer_cli.container.taskdefs.work_repo_taskdef_dirs`), and the mirror
    (spec D24) is a full checkout of that same repo — so the tiers the operator
    keeps their own taskdefs in were never invisible from the host, only unwired.
    Returned in the container's order, existing directories only, so the search
    below reads as the precedence it implements.

    The project tier needs a project, which is why *target* and *repo_url* are
    here: they go through the same pair the spawn itself would
    (:func:`_repo_urls`, :func:`derive_run_identity`), so the tier listed is the
    tier *that* spawn will search. Given neither, that pair falls back to the
    daemon's own ``$LMER_REPO_URL`` — which is also what a spawn naming no
    repository uses, so the default answer stays truthful for the default spawn.

    **The mirror is read exactly as it stands.** No pull, not even a throttled
    one: the fleet poll keeps it current, and a form's select is no reason to put
    a ``git fetch`` on a request path (the same call ``GET /api/runs/files``
    makes, T66). A tier that has not been pulled yet is a completion missing for
    one interval; the pencil override is what makes that affordable.

    ``[]`` for a host with no mirror configured or cloned, and for a mirror that
    cannot be read — both degrade the menu to exactly what it listed before this
    existed, rather than costing it the tiers that *are* readable.
    """
    if config is None:
        return []
    try:
        mirror = config.mirror_path
        if not mirror.is_dir():
            return []

        request = SpawnRequest(
            taskdef="", target=target or "", repo_url=repo_url or None
        )
        # The slug is discarded: a taskdef tier is a property of the repository,
        # not of the run. This call is here for the host/project pair only.
        host, project, _slug = derive_run_identity(request, _repo_urls(request)[1])

        dirs = []
        project_root = _mirror_project_dir(mirror, host or "", project or "")
        if project_root is not None:
            dirs.append(project_root / _TASKDEF_DIRNAME)
        dirs.append(mirror / _TASKDEF_DIRNAME)
        return [path for path in dirs if path.is_dir()]
    # Deliberately everything, and scoped tighter than the caller's catch: a
    # mirror that is a broken symlink, or a repo URL nothing can be parsed out
    # of, must cost the menu the work repo's tiers and nothing else.
    except Exception as exc:
        logger.warning("platform_work_repo_taskdefs_unreadable error=%r", exc)
        return []


def _taskdef_search_paths(
    config: Optional[PlatformConfig],
    *,
    target: str = "",
    repo_url: Optional[str] = None,
) -> list:
    """Every directory the menu enumerates, in the container's precedence order.

    Work-repo project, work-repo global, ``$LMER_TASKDEF_PATHS``, built-ins —
    :func:`lmer_cli.container.taskdefs.taskdef_search_dirs`' order, because that
    is the search a spawned session actually performs. Only ids come back out of
    it, so the order is not what decides the answer; it is what keeps this
    readable as the same search rather than a set of places that happen to be
    looked at.
    """
    return _work_repo_taskdef_dirs(config, target=target, repo_url=repo_url) + (
        _get_taskdef_paths(_builtin_taskdef_root())
    )


def _discovered_taskdefs(
    config: Optional[PlatformConfig] = None,
    *,
    target: str = "",
    repo_url: Optional[str] = None,
) -> list:
    """Taskdef ids this host can enumerate, sorted and deduplicated. Never raises.

    **Advisory**, and that word carries the whole design. Only ``chat`` lives in
    the code repo; ``LMER_TASKDEF_PATHS`` adds arbitrary directories; the work
    repo's own tiers are read off a mirror that is allowed to be stale, absent,
    or missing the project this spawn names. ``lmer`` treats its own copy of this
    list the same way — an unrecognised task is a warning and the session starts
    anyway, because the container is the one that can settle it. So this is a set
    of suggestions, never a vocabulary, and a caller that turns it into the only
    permitted values has made the host's partial knowledge into a rule the host
    cannot support.

    One entry per id. An id present in two tiers is one taskdef the session will
    resolve once, by the precedence :func:`_taskdef_search_paths` records, and
    naming it twice in a menu would only invite the operator to wonder which one
    they picked.

    Failure is an empty list rather than an exception because of what the one
    caller does with it: it offers these beside a field the operator can always
    type into. An empty list costs a completion; a raised exception costs the
    form — and an operator who knows the taskdef's name would then have no way
    to spawn at all.
    """
    try:
        # The assistant's taskdef is excluded, in every tier: it is spawned by the
        # daemon as kind='assistant' (with no repo checkout, refusing to write
        # code), so offering it on the *run* form invites a worker session that
        # cannot work — and a work repo is free to carry its own copy. The pencil
        # override still accepts it — advisory, not vocabulary.
        return sorted(
            task
            for task in _discover_all_tasks(
                _taskdef_search_paths(config, target=target, repo_url=repo_url)
            )
            if task != assistant.TASKDEF
        )
    # Deliberately everything: the value of this list is a convenience and the
    # cost of it raising is the spawn form. Discovery walks the filesystem
    # through helpers that answer to $LMER_TASKDEF_PATHS, so the failure modes
    # are the host's, not this module's.
    except Exception as exc:
        logger.warning("platform_taskdef_discovery_failed error=%r", exc)
        return []


def _discovered_presets() -> list:
    """Preset **names** the host has configured, sorted. Never raises.

    Names only, and that is not a summary — it is the entire safe projection. A
    preset body carries ``env`` (``LMER_*`` values, and operators do put tokens
    there), a ``checkout`` path and a ``service`` container name; the presets
    file is deliberately host-side and does not cross into a container
    (:mod:`lmer_cli.presets`). Only the name is ever selected by, so only the
    name goes out.

    :func:`lmer_cli.presets.load_presets` already degrades to ``{}`` on a
    missing, unreadable or malformed file, so this wrapper is for the cases it
    does not own — an unreadable directory, a surprising ``LMER_PRESETS_FILE``.
    Same reasoning as :func:`_discovered_taskdefs`: an empty menu, never a dead
    form.
    """
    try:
        return sorted(load_presets())
    except Exception as exc:
        logger.warning("platform_preset_discovery_failed error=%r", exc)
        return []


def discover_spawn_options(
    config: Optional[PlatformConfig] = None,
    *,
    target: str = "",
    repo_url: Optional[str] = None,
) -> dict:
    """What this host can offer a spawn form, and how far to trust it.

    ``advisory`` is a field rather than a line in the docs because it is the
    contract the UI has to honour: both lists are things the daemon *happens to
    be able to see*, so the fields they back stay free text (a combobox, not a
    select). A host with no taskdef search path at all answers with two empty
    lists, and the form is exactly as usable as it was before this route
    existed — which is the property that makes an empty answer safe to return
    instead of an error.

    *target* and *repo_url* are the spawn the caller is about to make, not a
    filter: they decide which work-repo project tier the taskdef list can include
    (:func:`_work_repo_taskdef_dirs`), so the menu describes what *that* spawn
    will be able to resolve. A caller that supplies neither gets the daemon's own
    repository, which is the repository that spawn would have used. *config* is
    optional for the same reason it is the mirror's only route in: without one
    there is no mirror to read, and the answer is the pre-T73 one.

    ``repo_url`` in the reply is the odd one out: not a suggestion but the value
    the spawn path would *actually* use for a request that names no repository
    (:func:`lmer_platform.config.configured_repo_url`, credentials already
    stripped), so the form can prefill the field with its own effective default
    instead of showing an empty box that behaves as if something were in it.
    ``None`` when the daemon was started without one, which is the case where
    leaving the field blank costs the run its identity.
    """
    return {
        "taskdefs": _discovered_taskdefs(config, target=target, repo_url=repo_url),
        "presets": _discovered_presets(),
        "repo_url": configured_repo_url(),
        "advisory": True,
        "note": (
            "suggestions, not a vocabulary: the work repo's taskdefs are read "
            "from this host's mirror, which may be stale or hold no tier for "
            "the project this spawn names, so any name may be typed. Presets "
            "are names only — their env, checkout and service stay on the host."
        ),
    }


def _index_file():
    """The built SPA entry point, or ``None`` when the UI has not been built."""
    dist = dist_dir()
    if dist is None:
        return None
    index = dist / "index.html"
    return index if index.is_file() else None


def _safe_asset(asset_path: str):
    """Resolve a request under ``/assets/`` to a real file inside the bundle.

    Resolves and then re-checks containment, so ``..`` segments and symlinks alike
    cannot reach outside ``dist/assets`` — the daemon is reachable from a phone on
    a LAN, and path traversal in a static handler is the classic way that becomes
    "reachable from a phone on a LAN, reading /etc".
    """
    dist = dist_dir()
    if dist is None:
        return None
    assets_root = (dist / "assets").resolve()
    try:
        candidate = (assets_root / asset_path).resolve()
    # RuntimeError, not just OSError: on 3.12 a symlink loop makes resolve() raise
    # RuntimeError, so catching only OSError turned a traversal attempt into a 500
    # instead of the 404 it is. Found by a mutation test on the identical pattern
    # in transcripts.py.
    except (OSError, RuntimeError):
        return None
    if not str(candidate).startswith(str(assets_root) + os.sep):
        return None
    return candidate if candidate.is_file() else None


def _unbuilt_message() -> str:
    """What to show instead of the UI when the bundle is absent.

    A plain, specific instruction rather than a 404 or a stack trace: the most
    likely visitor is an operator who just started the daemon and has not run the
    build step yet, and the answer is one command.
    """
    return (
        "lmer platform — the control UI has not been built yet.\n\n"
        "  build it:  lmer platform setup-ui\n"
        "             (fetches a pinned Node into the platform state dir;\n"
        "              nothing is installed system-wide)\n\n"
        "The JSON API is available meanwhile:\n"
        "  GET /api            route list\n"
        "  GET /api/state      fleet view\n"
        "  GET /api/health     liveness\n"
    )


def _presented_secret(authorization: Optional[str]) -> Optional[str]:
    """Pull the candidate secret out of an ``Authorization`` header."""
    if not authorization:
        return None
    scheme, _, rest = authorization.partition(" ")
    rest = rest.strip()
    scheme = scheme.lower()
    if scheme == "bearer":
        return rest or None
    if scheme == "basic":
        try:
            decoded = base64.b64decode(rest, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return None
        username, sep, password = decoded.partition(":")
        if sep and password:
            return password
        return username or None
    return None


def create_app(
    config: PlatformConfig,
    secret: str,
    *,
    state_builder: Optional[Callable[..., dict]] = None,
):
    """Build the FastAPI app.

    *state_builder* is injectable so tests can exercise routing and auth without
    a work repo or a network.
    """
    import anyio
    from fastapi import Depends, FastAPI, Header, HTTPException, Query
    from fastapi.responses import FileResponse, PlainTextResponse
    from starlette.websockets import WebSocketDisconnect

    if not secret:
        raise ValueError("refusing to serve without a shared secret")

    builder = state_builder or build_state
    app = FastAPI(title="lmer platform", version=str(SCHEMA_VERSION))

    # The UI bundle is ~550 kB uncompressed and ~130 kB gzipped, almost all of it
    # Vuetify's precompiled utilities sheet, which does not tree-shake. That
    # difference is paid by a phone on whatever link it happens to have, on every
    # cold load — so compress. `minimum_size` keeps it off the small JSON
    # responses, where the CPU is not worth the handful of bytes.
    #
    # GZipMiddleware only touches `http` scopes, so the tty WebSocket is
    # unaffected — which is what we want: its frames are already base64 of raw
    # terminal bytes and are latency-sensitive, not bandwidth-bound.
    from starlette.middleware.gzip import GZipMiddleware

    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # Per app rather than per module so tests get a fresh store and a second app
    # in one process cannot redeem the first one's tickets. In production there is
    # exactly one app in exactly one process (see session_io on why that matters).
    tickets = session_io.TicketStore()

    def require_secret(authorization: Optional[str] = Header(default=None)) -> None:
        presented = _presented_secret(authorization)
        # Encoded bytes on both sides, not the strings: ``compare_digest``
        # refuses two ``str`` arguments unless both are ASCII-only, and
        # :func:`_presented_secret` returns whatever the header decoded to — for
        # Basic, ``b64decode(...).decode("utf-8")``. So one non-ASCII character
        # typed into a browser's credential prompt raised ``TypeError`` here,
        # which nothing on the dependency path catches: a 500 with no
        # ``WWW-Authenticate`` (so the browser never re-prompts) and no
        # ``platform_auth_rejected`` line (so the attempt was invisible in the
        # daemon's own record of refused auth).
        #
        # Deliberately NOT a validation rule refusing non-ASCII input: that
        # resolves the crash into a different wrong answer — it would make a
        # non-ASCII secret unusable — and a constant-time comparison is the last
        # place a shape check belongs. Encoding is total, so every credential
        # gets compared rather than sorted into "comparable" and "rejected".
        if presented is not None and secrets_mod.compare_digest(
            presented.encode("utf-8"), secret.encode("utf-8")
        ):
            return
        logger.warning(
            "platform_auth_rejected scheme=%s",
            (authorization or "").partition(" ")[0].lower() or "none",
        )
        raise HTTPException(
            status_code=401,
            detail="invalid credentials",
            # Prompts a browser for credentials instead of showing a bare 401.
            headers={"WWW-Authenticate": f'Basic realm="{REALM}"'},
        )

    guard = [Depends(require_secret)]

    @app.get("/", dependencies=guard)
    def root():
        """The control UI, or instructions for building it.

        Serving the app behind the *same* guard as the API matters: an unbuilt or
        unauthenticated visit must not leak a shell of the UI, and the browser's
        Basic prompt is what the SPA relies on for credentials.
        """
        index = _index_file()
        if index is not None:
            # no-store: the bundle's filenames are stable across builds
            # (assets/app.js), so a cached index would keep serving a stale app
            # after setup-ui.
            return FileResponse(index, headers={"Cache-Control": "no-store"})
        return PlainTextResponse(_unbuilt_message())

    @app.get("/assets/{asset_path:path}", dependencies=guard)
    def asset(asset_path: str):
        """Serve a built asset, refusing anything outside the bundle."""
        resolved = _safe_asset(asset_path)
        if resolved is None:
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(resolved, headers={"Cache-Control": "no-store"})

    @app.get("/api", response_class=PlainTextResponse, dependencies=guard)
    def api_index() -> str:
        """Plain-text route list — useful from a terminal, and stable for scripts."""
        return (
            "lmer platform\n\n"
            "  GET  /api/health            liveness and mirror presence (cheap)\n"
            "  GET  /api/state             fleet view: runs THIS orchestrator\n"
            "                              tracks. A run's session block is\n"
            "                              narrowed to the fields a client reads,\n"
            "                              not the whole registry entry\n"
            "  POST /api/sessions          spawn {taskdef,target,repo_url,\n"
            "                              preset,agents,harness,ports,\n"
            "                              title?,description?,...} — title names\n"
            "                              the run in the fleet view, exactly as\n"
            "                              POST /api/runs/meta would afterwards;\n"
            "                              it is written once the run is tracked,\n"
            "                              and a 201 says so in warning if it\n"
            "                              could not be\n"
            "  GET  /api/spawn-options     taskdefs and preset names this host\n"
            "                              can see — suggestions, not a\n"
            "                              vocabulary: any taskdef may be named;\n"
            "                              plus repo_url, the default a spawn\n"
            "                              would use when none is given.\n"
            "                              ?target=&repo_url= name the spawn being\n"
            "                              composed, which is what lets the work\n"
            "                              repo's project taskdef tier be listed;\n"
            "                              read off the mirror, no pull forced\n"
            "  POST /api/rescan            force a work-repo pull, then re-read\n"
            "  POST /api/prune             forget dead session entries\n"
            "  GET  /api/runs/candidates   every run in the shared work repo\n"
            "  POST /api/runs/adopt        track an existing run {host,project,slug}\n"
            "  POST /api/runs/forget       stop tracking a run\n"
            "  GET  /api/runs/meta         what a run is about: this\n"
            "                              orchestrator's title and description\n"
            "                              for it ?host=&project=&slug=\n"
            "  POST /api/runs/meta         set them {host,project,slug,title?,\n"
            "                              description?} — an omitted field is\n"
            "                              left alone, \"\" clears it. This is\n"
            "                              PLATFORM state, never the work repo\n"
            "                              (D3): local to this orchestrator,\n"
            "                              invisible to anyone else's fleet view,\n"
            "                              and dropped when the run is forgotten\n"
            "  GET  /api/runs/relations    runs this orchestrator considers related\n"
            "                              to this one ?host=&project=&slug=, each\n"
            "                              with tracked: false meaning it is not in\n"
            "                              this fleet — a key to show, not a run to\n"
            "                              open\n"
            "  POST /api/runs/relate       relate two runs {host,project,slug,\n"
            "                              related:{host,project,slug}} so each is\n"
            "                              one tap from the other. SYMMETRIC and\n"
            "                              stored once: either order is the same\n"
            "                              relation, both runs show it, and doing it\n"
            "                              twice answers created: false. Neither run\n"
            "                              need be tracked. PLATFORM state, never\n"
            "                              the work repo (D3): local to this\n"
            "                              orchestrator, and NOT removed when a run\n"
            "                              is forgotten\n"
            "  POST /api/runs/unrelate     remove one, same body, either order —\n"
            "                              one entry, so both directions go at once\n"
            "  GET  /api/runs/files        the run's own files in this daemon's\n"
            "                              mirror ?host=&project=&slug=, each with\n"
            "                              its forge URL. A null url means no\n"
            "                              links are built for this work repo\n"
            "                              (none configured, or work_repo_forge=\n"
            "                              none), not a missing file; no pull is\n"
            "                              forced, so a just-pushed file can lag\n"
            "                              one interval\n"
            "  POST /api/runs/answer       answer a question-blocked run\n"
            "                              {host,project,slug,answer} — starts a\n"
            "                              fresh session carrying the answer\n"
            "  POST /api/runs/resume       continue a tracked run by starting its\n"
            "                              next session {host,project,slug,\n"
            "                              taskdef?,repo_url?,direction?}. The\n"
            "                              taskdef defaults to the run's recorded\n"
            "                              one; naming another starts a sibling run\n"
            "                              against the same target. A refusal here\n"
            "                              carries {code,message} — two of them ask\n"
            "                              for one more field rather than failing\n\n"
            "One session's terminal:\n"
            "  GET  /api/sessions/{id}/log         scrollback: base64 bytes from\n"
            "                                      ?offset (negative = tail), ?limit,\n"
            "                                      ?source=host|container (default:\n"
            "                                      whichever is the log of record)\n"
            "  POST /api/sessions/{id}/input        type into it {data,append_newline}\n"
            "  POST /api/sessions/{id}/tty-ticket   mint a single-use socket ticket\n"
            "  WS   /api/sessions/{id}/tty?ticket=  live terminal, both directions\n\n"
            "The log survives the session, so scrollback stays readable after a\n"
            "crash; input needs a live one. The socket takes a ticket instead of\n"
            "the shared secret — browsers cannot header a WebSocket handshake.\n\n"
            "One run's conversation:\n"
            "  GET  /api/sessions/{id}/messages    normalised harness transcript,\n"
            "                                      every session of the run, from\n"
            "                                      ?since (negative = tail), ?limit\n\n"
            "The terminal is the faithful view; this is the readable one. No\n"
            "transcript on disk is an empty page with a note, not an error.\n\n"
            "One session's ask channel (a LIVE session asking you something):\n"
            "  GET  /api/sessions/{id}/ask         questions, notes and answers\n"
            "  POST /api/sessions/{id}/ask/{qid}/answer   reply {answer}\n\n"
            "Not the same as POST /api/runs/answer: that respawns a run that\n"
            "stopped on a question, while this drops a reply into a directory a\n"
            "still-running session is polling. No container is started.\n\n"
            "Ending one session — two verbs, never interchangeable:\n"
            "  POST /api/sessions/{id}/wind-down  ask the agent to wrap up and end\n"
            "                                     itself {note} — 202, nothing has\n"
            "                                     ended yet\n"
            "  POST /api/sessions/{id}/exit       signal it now; the agent commits,\n"
            "                                     pushes and reports nothing\n\n"
            "Sessions end only when a human asks. wind-down is a prompt over the\n"
            "control plane, so it reaches a session whose host terminal died with a\n"
            "previous daemon; exit refuses that case (409) rather than signalling a\n"
            "pid this daemon no longer owns.\n\n"
            "The orchestrating assistant — the one session this platform runs for\n"
            "itself, and the thing the operator chats with:\n"
            "  GET  /api/assistant          whether one is running, which session,\n"
            "                               how old, how many digests are spooled\n"
            "                               for it, and the handover note it starts\n"
            "                               from. A read: it starts nothing. The\n"
            "                               pending count here is the only\n"
            "                               non-consuming signal that something is\n"
            "                               waiting — watch it, do not drain to ask\n"
            "  POST /api/assistant/start    start it {handoff?, model?, harness?,\n"
            "                               preset?, agents?} — 409 when one is\n"
            "                               already up, which is how the incumbent\n"
            "                               keeps its context window. 200, not 202:\n"
            "                               the session is up when this returns\n"
            "  POST /api/assistant/stop     stop it {reason?,handoff?}. NEVER\n"
            "                               /api/sessions/{id}/exit — that verb\n"
            "                               refuses an assistant (409), because a\n"
            "                               stop also has to clear the pointer and\n"
            "                               record why it ended\n"
            "  POST /api/assistant/rotate   replace it with a fresh context window,\n"
            "                               carrying {handoff?} over — one call, so\n"
            "                               no start lands in the gap. Takes the\n"
            "                               same launch overrides as start\n"
            "  GET  /api/assistant/config   how the NEXT incarnation will be run —\n"
            "                               model/harness/preset/agents, each with\n"
            "                               the layer that decided it (env /\n"
            "                               config.json / default). What the running\n"
            "                               one uses is on GET /api/assistant\n"
            "  POST /api/assistant/config   persist launch settings to config.json —\n"
            "                               a patch of the keys named; null clears\n"
            "                               one. Changes nothing about the running\n"
            "                               incarnation: rotate to apply\n"
            "  GET  /api/assistant/handoff  the note the last incarnation left. A\n"
            "                               null one with a note means nobody left\n"
            "                               you anything, which is not the same as\n"
            "                               this route being absent\n"
            "  POST /api/assistant/handoff  write it {handoff} — what an assistant\n"
            "                               tells its successor before a rotation\n"
            "  GET  /api/assistant/instructions   the operator's standing orders.\n"
            "                               Read at the start of every incarnation\n"
            "                               and never consumed; a null one with a\n"
            "                               note means none have been set\n"
            "  POST /api/assistant/instructions   replace the whole document\n"
            "                               {instructions} — what an assistant\n"
            "                               writes after the operator states a\n"
            "                               standing preference in chat. Re-read\n"
            "                               first; this is not a patch\n"
            "  POST /api/assistant/pending  take the digests the daemon spooled and\n"
            "                               clear them. DESTRUCTIVE, hence not a\n"
            "                               GET; the status carries the count, so\n"
            "                               watch that instead. Nothing pushes a\n"
            "                               digest anywhere — it waits here\n\n"
            "The fleet view is scoped to runs this orchestrator spawned or\n"
            "adopted — never the whole shared work repo.\n"
        )

    @app.get("/api/health", dependencies=guard)
    def health() -> dict:
        """Cheap liveness — deliberately does not pull the mirror.

        Health checks run often; making one depend on a network fetch would turn
        a slow remote into a false "unhealthy".
        """
        status = mirror_status(config)
        return {
            "ok": True,
            "schema": SCHEMA_VERSION,
            "at": utc_now_iso(),
            "bind": config.base_url,
            "mirror": {
                "present": status.present,
                "healthy": status.healthy,
                "last_pull_at": status.last_pull_at,
            },
        }

    @app.get("/api/state", dependencies=guard)
    def state() -> dict:
        return builder(config)

    @app.post("/api/rescan", dependencies=guard)
    def rescan() -> dict:
        """Re-read from ground truth, skipping the pull throttle."""
        return builder(config, force_pull=True)

    @app.get("/api/spawn-options", dependencies=guard)
    def spawn_options(
        target: str = Query(default=""),
        repo_url: Optional[str] = Query(default=None),
    ) -> dict:
        """Taskdefs and presets this host can see, for the spawn form's selects.

        Behind the guard like everything else: preset names are the operator's
        configuration, and the taskdef list is a description of this machine.

        ``target`` and ``repo_url`` are the spawn being composed. They are what
        lets the work repo's *project* taskdef tier appear — it is filed under the
        run's host and project, which nothing knows until the form has a
        repository (T73) — so a client that re-asks as those fields settle gets a
        menu for the spawn it is about to make. Both are optional and neither is
        validated here: an unparseable one costs the project tier, never the
        answer.

        Always 200, even when it can enumerate nothing — see
        :func:`discover_spawn_options`. A client is expected to use both lists as
        *completions* and to keep sending whatever the operator actually typed.
        """
        return discover_spawn_options(config, target=target, repo_url=repo_url)

    @app.post("/api/sessions", dependencies=guard, status_code=201)
    def create_session(body: dict) -> dict:
        """Spawn a session and start tracking its run.

        Returns 429 at the concurrency cap rather than queueing: the queue and
        slots that make waiting graceful land in M3, and silently dropping a spawn
        request would be worse than refusing it with the numbers.

        A 201 can still carry a ``warning``: a spawn whose run could not be
        identified starts a perfectly healthy session that is never recorded, so
        the reply says so rather than leaving the caller to notice the row
        vanishing later (:func:`lmer_platform.spawn._untracked_run_warning`). A
        ``title`` that was refused is reported the same way and for the same
        reason — the session is already running by the time it is written.
        """
        try:
            request = SpawnRequest(
                taskdef=body.get("taskdef") or "",
                target=body.get("target") or "",
                repo_url=body.get("repo_url"),
                # Typed fields, never appended to extra_args: the platform emits
                # each flag itself and records what it emitted, so a second
                # spelling in argv would beat the entry (see spawn._RESERVED_ARGS,
                # which refuses exactly that). Not coerced, so an unusable value
                # keeps its own refusal instead of arriving as ``None``.
                preset=body.get("preset"),
                agents=body.get("agents"),
                harness=body.get("harness"),
                model=body.get("model"),
                ports=body.get("ports") or 0,
                extra_args=tuple(body.get("extra_args") or ()),
                # The run's name in this fleet, and pointedly not part of the
                # invocation: no flag spells either of these and the child never
                # sees them. They are written after the run is tracked, by
                # :mod:`lmer_platform.meta` — the same write ``POST /api/runs/meta``
                # performs, so a spawn that names the run and an operator renaming
                # it afterwards go through one validation and one file.
                title=body.get("title"),
                description=body.get("description"),
            )
            result = spawn_session(config, request)
        except CapacityError as exc:
            raise HTTPException(status_code=429, detail=str(exc))
        # 409, and before SpawnError, which this subclasses: the request is well
        # formed and would work once the session holding that run stops. This route
        # derives the same run identity from the same taskdef and target as the
        # answer and resume routes, so it is one of the ways a second session for
        # one run used to be a plain API call away.
        except RunAlreadyLive as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except SpawnError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return result.to_dict()

    # --- one session's terminal (spec D16, T16) ------------------------------
    #
    # Statuses come off the exception (session_io.SessionIOError.status) rather
    # than being decided per route, so a new failure mode arrives with a code
    # instead of falling through to a 500 with a traceback.

    @app.get("/api/sessions/{session_id}/log", dependencies=guard)
    def session_log(
        session_id: str,
        offset: int = 0,
        limit: int = Query(
            default=session_io.DEFAULT_LOG_LIMIT,
            ge=1,
            le=session_io.MAX_LOG_LIMIT,
        ),
        source: Optional[str] = None,
    ) -> dict:
        """Scrollback for one session — the terminal's history (spec D16).

        Works for a session that is dead, crashed, or long since forgotten by the
        registry: the PTY log outlives the container on purpose, and that is what
        makes a crash investigable. ``live`` says which case this is.

        A negative *offset* reads the last ``|offset|`` bytes, so a client can
        attach to the end of a huge log without first asking how big it is.

        ``detached`` is present when this log has a seam in it: the daemon that
        owned the session's host PTY was restarted, so everything after that
        point is the container's own output recovered over the control plane, and
        the host-side ``lmer`` output during the gap is not in the file and never
        will be (T36). A terminal replaying these bytes should be able to say so
        rather than presenting one continuous stream.

        ``source`` reads one of the session's two logs by name instead of the one
        that is canonical, and the reply always says which it served. It exists
        for one thing: the host-side launch of a session that records itself —
        the pull, the clone and lmer's own lines, printed before the container had
        a log to write into, and therefore behind the first byte of the log this
        route otherwise serves. Read-only and one file at a time; the offsets in
        the reply belong to the file named, so a cursor from a named read must not
        be handed back to the socket, which resolves the canonical log for itself.
        """
        try:
            chunk = session_io.read_log(
                session_id, offset=offset, limit=limit, source=source
            )
            live = session_io.session_is_live(session_id)
            detached = reattach.detached_record(read_session(session_id))
        except SessionIOError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        return {
            "session": session_id,
            "live": live,
            "detached": detached,
            **chunk.to_dict(),
        }

    @app.post("/api/sessions/{session_id}/input", dependencies=guard)
    def session_input(session_id: str, body: dict) -> dict:
        """Type into a running session, via its own control plane.

        This is the route that makes the platform an orchestrator rather than a
        dashboard: it is how a session that stopped to ask a question gets its
        answer. Failure is always an error status and never a 200 — see
        :func:`lmer_platform.session_io.send_input`.
        """
        data = body.get("data")
        if not isinstance(data, str):
            raise HTTPException(
                status_code=400,
                detail="input needs a string 'data' field (\"\" is allowed)",
            )
        try:
            reply = session_io.send_input(
                session_id, data, append_newline=bool(body.get("append_newline"))
            )
        except SessionIOError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        answer = {"session": session_id, "bytes_written": reply.get("bytes_written")}
        # Only when Enter was asked for, and only from the control plane's own
        # answer: the supervisor writes the CR and cannot observe whether the TUI
        # took it as a submit, so the caller is told that rather than left to
        # read a 200 as "delivered and sent". A keystroke (append_newline off)
        # presses no Enter and so has nothing to be unsure about — no field.
        if "submit_confirmed" in reply:
            answer["submit_confirmed"] = reply.get("submit_confirmed")
            answer["note"] = reply.get("note")
        # The half of the delivery the supervisor *can* see: whether the harness was
        # observed taking the text before Enter was pressed (#210) — ``read``,
        # ``unread`` or ``unknown``, relayed as given rather than reduced to a flag,
        # since "not observed" is not "observed to have failed". Only forwarded when
        # the control plane reported it, so an older session image's reply keeps its
        # exact shape.
        if "submit_text" in reply:
            answer["submit_text"] = reply.get("submit_text")
        return answer

    @app.get("/api/sessions/{session_id}/messages", dependencies=guard)
    def session_messages(
        session_id: str,
        since: int = 0,
        limit: int = Query(
            default=transcripts.DEFAULT_MESSAGE_LIMIT,
            ge=1,
            le=transcripts.MAX_MESSAGE_LIMIT,
        ),
    ) -> dict:
        """The readable view of one run: its harness transcript, normalised (D6).

        Sibling of ``/log``, not a replacement: that route serves the terminal's
        bytes, which are the faithful record, while this one serves messages a
        phone can read. Both outlive the session.

        Spans **every session of the run** the given session belongs to, so a run
        that was respawned to answer a question still reads as one conversation
        (spec §10.4). :mod:`lmer_platform.transcripts` documents which sessions
        can actually be joined and which drop out.

        A negative *since* reads the last ``|since|`` messages — the log route's
        negative-offset convention, for the same reason: a cold open wants the end
        of a long conversation without first asking how long it is. ``cursor`` is
        what the next poll passes as *since*, and it neither skips nor repeats.

        The page also carries the exchanges from each session's ask channel, which
        the transcript cannot: an answer given through ``POST …/ask/{id}/answer``
        is a file the session polls for, so nothing about it reaches the harness's
        own record. :mod:`lmer_platform.transcripts` merges the two on this read
        path and never into the transcript file.

        No transcript is an empty page with a ``note``, never an error: the
        session existing and its transcript being readable are different facts,
        and today the second is usually false.
        """
        try:
            session_io.require_session(session_id)
            page = transcripts.read_messages(session_id, since=since, limit=limit)
            live = session_io.session_is_live(session_id)
        except SessionIOError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        return {"session": session_id, "live": live, **page.to_dict()}

    # --- one session's ask channel (spec D26/D27, T23) -----------------------
    #
    # The live-session counterpart of POST /api/runs/answer. Keyed on the session
    # rather than the run because the channel is a directory mounted into one
    # container: only that session is polling it, and only while it is up.

    @app.get("/api/sessions/{session_id}/ask", dependencies=guard)
    def session_ask(session_id: str) -> dict:
        """Everything on one session's ask channel, oldest first.

        Questions, progress notes and the answers already given, in the order the
        session wrote them. A session that never asked anything returns an empty
        list — not an error, since that is almost every session.

        ``live`` matters more here than on the other session routes: an
        unanswered question from a session that has exited is a record, not a
        request, because nothing is polling for the reply any more.
        """
        try:
            session_io.require_session(session_id)
            entries = ask.read_entries(session_id)
            live = session_io.session_is_live(session_id)
        except SessionIOError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        except AskChannelError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        return {
            "session": session_id,
            "live": live,
            "entries": [entry.to_dict() for entry in entries],
        }

    @app.post(
        "/api/sessions/{session_id}/ask/{question_id}/answer", dependencies=guard
    )
    def session_ask_answer(session_id: str, question_id: str, body: dict) -> dict:
        """Answer one question a live session is waiting on.

        The question id is in the path, not the body, and that is the point: an
        answer is bound to the question it answers at the URL, so it cannot be
        applied to whatever happens to be open when it arrives. The channel
        checks the binding again on both sides (see
        :mod:`ask_channel.protocol`).

        200, unlike ``/api/runs/answer``'s 202: when this returns the answer is
        on disk and the session's next poll — seconds away — picks it up. Nothing
        is spawned, so there is no "started, will apply later" to report.

        The answer text is not echoed back. The operator already has it, and the
        one thing a reply body would add is another copy of it in a log.
        """
        text = body.get("answer")
        if not isinstance(text, str):
            raise HTTPException(
                status_code=400, detail="answer must be text"
            )
        try:
            session_io.require_session(session_id)
            recorded = ask.answer_question(session_id, question_id, text)
        except SessionIOError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        except AskChannelError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        return {
            "session": session_id,
            "question_id": recorded["question_id"],
            "answered_at": recorded["answered_at"],
        }

    # --- ending one session (spec §7.5 / D22, T27) ---------------------------
    #
    # Two routes because they are two verbs, and the difference between them is the
    # whole feature: one asks the agent and waits, the other signals now. Nothing
    # here lets either become the other — a wind-down never escalates to a signal,
    # which is what makes it safe to offer as the ordinary action, and an exit never
    # claims to have asked first. See :mod:`lmer_platform.lifecycle`.

    @app.post(
        "/api/sessions/{session_id}/wind-down", dependencies=guard, status_code=202
    )
    def session_wind_down(session_id: str, body: Optional[dict] = None) -> dict:
        """Ask a session's agent to wrap up and end itself. Signals nothing.

        **202, not 200.** Nothing has ended when this returns: an agent has been
        handed a prompt and is expected to commit, push, report and then exit on its
        own schedule. 200 would imply the session is over, which is exactly the
        wrong thing for an operator to believe about a container that is still
        holding a slot — and the run's row is what reports the ending, whenever it
        comes.

        Reaches a session whose host terminal died with a previous daemon (T36),
        because it travels the control plane rather than the PTY. That is why this is
        the verb the UI leads with.

        An optional ``note`` rides on the end of the prompt, for the "and don't
        bother with the MR" an operator will want to add. The prompt that was sent
        comes back in the reply: the operator interrupted a working agent, and what
        it was told should not be something only the source knows.
        """
        try:
            report = lifecycle.wind_down(session_id, note=(body or {}).get("note"))
        except (SessionIOError, LifecycleError) as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        return report.to_dict()

    @app.post("/api/sessions/{session_id}/exit", dependencies=guard)
    def session_exit(session_id: str) -> dict:
        """End a session now, by signal. The agent gets no chance to wrap up.

        200 rather than 202, and that is the honest difference from the route above:
        this one does not return until the process is gone, so the answer is a fact
        rather than an acknowledgement. It answers 409 for a session it will not
        signal — one that survived a daemon restart, or that some other process
        started — with the reason and the pid in the message, because the operator's
        next move (wind it down instead) differs from what they asked for.

        Takes no body. There is nothing to configure about ending something now, and
        a body would only invite a ``force`` flag that this verb already is.
        """
        try:
            report = lifecycle.exit_session(session_id)
        except (SessionIOError, LifecycleError) as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        return report.to_dict()

    @app.post("/api/sessions/{session_id}/tty-ticket", dependencies=guard)
    def tty_ticket(session_id: str) -> dict:
        """Mint the single-use credential the tty socket requires.

        Authenticated with the shared secret like every other route — the ticket
        exists precisely so the *socket* never has to be.
        """
        try:
            session_io.require_session(session_id)
        except SessionIOError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        return {
            "session": session_id,
            "ticket": tickets.mint(session_id),
            "expires_in": tickets.ttl,
        }

    def _session_preamble(session_id: str) -> bool:
        """Confirm the session exists and report liveness, in one threadpool hop.

        Both touch disk, and both are needed before the socket is accepted.
        """
        session_io.require_session(session_id)
        return session_io.session_is_live(session_id)

    async def send_status(websocket, event: str, message: str) -> None:
        """Tell the client something happened without taking its socket away.

        Every non-fatal problem on the socket becomes one of these. The
        alternative — closing — loses the scrollback the client has already
        rendered and gives it nothing to display about why.
        """
        await websocket.send_json(
            {"type": "status", "event": event, "message": message}
        )

    async def stream_log(websocket, session_id: str, offset: int) -> None:
        """Push the backlog and then every new byte, until the session ends."""
        try:
            async for chunk in session_io.follow_log(session_id, offset=offset):
                await websocket.send_json({"type": "data", **chunk.to_dict()})
        except SessionIOError as exc:
            await send_status(websocket, "log_failed", str(exc))
            return
        await send_status(
            websocket,
            "ended",
            "the session has exited — its scrollback stays readable",
        )

    async def handle_frame(websocket, session_id: str, frame: dict) -> None:
        """Apply one client frame. Bad frames are reported, never fatal."""
        kind = frame.get("type")
        if kind == "input":
            data = frame.get("data")
            if not isinstance(data, str):
                await send_status(
                    websocket, "bad_frame", "an input frame needs a string 'data'"
                )
                return
            try:
                await asyncio.to_thread(
                    session_io.send_input,
                    session_id,
                    data,
                    append_newline=bool(frame.get("append_newline")),
                )
            except SessionIOError as exc:
                await send_status(websocket, "input_failed", str(exc))
            return

        if kind == "resize":
            rows, cols = frame.get("rows"), frame.get("cols")
            if not _positive_int(rows) or not _positive_int(cols):
                await send_status(
                    websocket,
                    "bad_frame",
                    "a resize frame needs positive integer 'rows' and 'cols'",
                )
                return
            try:
                report = await asyncio.to_thread(
                    session_io.apply_resize, session_id, rows, cols
                )
            except ControlPlaneError as exc:
                # Not the same as a refused resize, and collapsing the two cost an
                # operator a working terminal: a session whose harness has not
                # finished starting has no listener on its control port yet, so the
                # very first resize — which fit-to-screen now sends the moment the
                # terminal opens — gets a connection reset. Reported as
                # ``resize_failed`` the client read that as "the PTY is gone",
                # switched fitting off and left a sticky error that only a reload
                # cleared, on a session that was about to be perfectly fine.
                await send_status(websocket, "resize_deferred", str(exc))
                return
            except SessionIOError as exc:
                await send_status(websocket, "resize_failed", str(exc))
                return
            if not report.applied:
                await send_status(websocket, report.event, report.message)
            return

        await send_status(websocket, "bad_frame", f"unknown frame type {kind!r}")

    async def drive_session(websocket, session_id: str) -> None:
        """Read client frames until it hangs up."""
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            text = message.get("text")
            if text is None:
                await send_status(
                    websocket,
                    "bad_frame",
                    "binary frames are not accepted; send JSON text frames",
                )
                continue
            try:
                frame = json.loads(text)
            except ValueError:
                await send_status(websocket, "bad_frame", "frame is not JSON")
                continue
            if not isinstance(frame, dict):
                await send_status(websocket, "bad_frame", "expected a JSON object")
                continue
            await handle_frame(websocket, session_id, frame)

    async def relay(websocket, session_id: str, offset: int) -> None:
        """Run both directions until either finishes, then stop the other.

        A task group rather than two loose tasks: the two directions block on
        unrelated things (one on the log, one on the client) but they have to die
        together, *including* when the cancellation arrives from outside — uvicorn
        shutting down, or the connection torn down under us. Loose tasks let a
        follower outlive the socket it writes to and go on polling a log nobody is
        reading; structured concurrency makes that unrepresentable.
        """
        async with anyio.create_task_group() as group:

            async def once(direction, *args) -> None:
                """One direction, then end the socket. A hang-up is not a failure."""
                try:
                    await direction(*args)
                except WebSocketDisconnect:
                    pass
                group.cancel_scope.cancel()

            group.start_soon(once, stream_log, websocket, session_id, offset)
            group.start_soon(once, drive_session, websocket, session_id)

    @app.websocket_route("/api/sessions/{session_id}/tty")
    async def session_tty(websocket) -> None:
        """A live terminal: log bytes out, input and resize in.

        Ticket first, existence second — an unauthenticated peer must not be able
        to learn which session ids exist from the way it is turned away.

        Registered as a plain Starlette route rather than with FastAPI's
        ``@app.websocket``, because the two decisions above it collide: fastapi is
        imported inside this function (half a second of import time that
        ``lmer platform status`` should not pay), while FastAPI resolves a
        handler's annotations against *module* globals — where ``WebSocket``
        therefore is not, so it would treat the socket itself as a query
        parameter. Reading two values off the request is cheaper than the
        workarounds.
        """
        session_id = websocket.path_params["session_id"]
        ticket = websocket.query_params.get("ticket", "")
        offset = _query_int(
            websocket.query_params, "offset", -session_io.DEFAULT_TAIL_BYTES
        )
        if not tickets.redeem(ticket, session_id):
            logger.warning("platform_tty_ticket_rejected session=%s", session_id)
            await websocket.close(
                code=WS_POLICY_VIOLATION,
                reason="invalid, expired or already-used ticket",
            )
            return
        try:
            live = await asyncio.to_thread(_session_preamble, session_id)
        except SessionIOError as exc:
            await websocket.close(
                code=WS_POLICY_VIOLATION, reason=str(exc)[:_WS_REASON_LIMIT]
            )
            return

        await websocket.accept()
        await websocket.send_json(
            {"type": "open", "session": session_id, "live": live}
        )

        try:
            await relay(websocket, session_id, offset)
        except Exception as failure:
            # An ExceptionGroup from the task group. Nothing here is worth losing
            # the daemon over, but a terminal that stopped working is worth a line
            # saying which half broke.
            logger.warning(
                "platform_tty_failed session=%s error=%r", session_id, failure
            )
        # Already-closed is the common case (the client hung up first).
        with contextlib.suppress(RuntimeError):
            await websocket.close()

    @app.get("/api/runs/candidates", dependencies=guard)
    def candidates() -> dict:
        """Runs present in the shared work repo, for choosing what to adopt.

        Explicitly **not** the fleet view: this lists everyone's runs, which is
        why it is a separate route with a name that says so. Tracked runs are
        flagged so the list is usable as a picker.
        """
        tracked_keys = {(e.host, e.project, e.slug) for e in list_tracked()}
        return {
            "candidates": [
                {
                    "host": ref.host,
                    "project": ref.project,
                    "slug": ref.slug,
                    "rel_path": ref.rel_path,
                    "tracked": (ref.host, ref.project, ref.slug) in tracked_keys,
                }
                for ref in run_dirs(config)
            ],
            "note": "every run in the shared work repo, including other people's",
        }

    @app.post("/api/runs/adopt", dependencies=guard)
    def adopt(body: dict) -> dict:
        """Start tracking an existing run so it appears in the fleet view."""
        try:
            entry = track(
                body.get("host"),
                body.get("project"),
                body.get("slug"),
                source="adopted",
                note=body.get("note"),
            )
        except RunIndexError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"tracked": entry.to_dict()}

    @app.post("/api/runs/forget", dependencies=guard)
    def forget_run(body: dict) -> dict:
        """Stop tracking a run. Its work-repo state is untouched."""
        try:
            removed = forget(
                body.get("host"), body.get("project"), body.get("slug")
            )
        except RunIndexError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"forgotten": removed}

    # --- what a run is about (T52) -------------------------------------------
    #
    # Platform state, never the work repo. The daemon's mirror is force-reset on
    # every pull (spec D3), so a title stored beside the run's own state would be
    # destroyed by the next fetch with nothing to show for it —
    # :mod:`lmer_platform.meta` carries the whole argument, including what this
    # design costs: the metadata is local to this orchestrator.
    #
    # Keyed on the run, like adopt/forget/answer/resume, and by query string on
    # the read because a project is ``group/subgroup`` — a path parameter would
    # have to be escaped by every caller to survive the slash it contains.

    @app.get("/api/runs/meta", dependencies=guard)
    def run_meta(host: str = "", project: str = "", slug: str = "") -> dict:
        """This orchestrator's title and description for one run.

        Empty for a run that has none, which is most of them, and empty rather
        than 404 for a run that is not tracked at all: the caller asked what is
        recorded about a run and "nothing" is the true answer, while a refusal
        would turn a run forgotten in another tab into an error page.

        The three parts default to empty strings rather than being required by
        FastAPI, so a caller that leaves one out gets :func:`run_key`'s refusal —
        which names the field — instead of a validation error in a different
        shape from every other refusal here.
        """
        try:
            record = meta.read(host, project, slug)
        except RunIndexError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _meta_reply(host, project, slug, record)

    @app.post("/api/runs/meta", dependencies=guard)
    def set_run_meta(body: dict) -> dict:
        """Set a run's title, description, or both.

        Reachable by anything holding the shared secret, which is the point: the
        orchestrator agent sets these over this route with the same ``Bearer``
        credential as any other client (T30), so nothing needed a channel of its
        own. That also makes the text agent-authored and therefore untrusted —
        it is bounded and stripped of control characters here, and rendered
        through the UI's one sanitising renderer there.

        An omitted field is left as it is and ``""`` clears it, so an agent
        setting a title cannot silently delete a description an operator wrote.

        200, not 202 or 201: when this returns the value is on disk, and the body
        is what was stored rather than what was sent — the title comes back
        collapsed to the single line it will be shown as. Statuses ride on
        :class:`lmer_platform.meta.MetaError` so a refusal added later arrives
        with its own code instead of a 500.
        """
        host, project, slug = body.get("host"), body.get("project"), body.get("slug")
        try:
            record = meta.write(
                host, project, slug,
                # Not coerced: a non-string keeps its own refusal rather than
                # arriving as ``None`` and reading as "leave this field alone".
                title=body.get("title"),
                description=body.get("description"),
            )
        except RunIndexError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except MetaError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        return _meta_reply(host, project, slug, record)

    # --- what a run wrote (T66) ----------------------------------------------
    #
    # A route of its own rather than more fields on the fleet payload, and the
    # asymmetry is the reason: /api/state carries every tracked run and is polled
    # on a timer, so a file list on it would cost a directory walk per run per poll
    # to serve something only the open detail view ever renders. This is read once,
    # when a run is opened. Keyed by query string like GET /api/runs/meta, for that
    # route's reason: a project is ``group/subgroup``.

    @app.get("/api/runs/files", dependencies=guard)
    def run_files(host: str = "", project: str = "", slug: str = "") -> dict:
        """The run's own files in the mirror, each with its forge URL.

        Deliberately does **not** pull: this is read while an operator is looking
        at one run, the fleet poll is what keeps the mirror current, and a fetch
        per detail view would put a remote's latency in front of opening a page.
        So a file pushed seconds ago can be missing for one pull interval, which is
        the same eventual-consistency the rest of the view already has (spec D24).

        The three parts default to empty strings rather than being required, so a
        caller that omits one gets :func:`lmer_platform.runs.run_key`'s refusal —
        which names the field — instead of a validation error in a shape no other
        refusal here uses.
        """
        try:
            run_key(host, project, slug)
        except RunIndexError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _run_files_reply(config, host, project, slug)

    # --- which runs belong together (T53) -------------------------------------
    #
    # Platform state, never the work repo, for the reason the metadata routes give
    # (spec D3): the daemon's mirror is force-reset on every pull, so a relation
    # stored beside a run's own state would be destroyed by the next fetch with
    # nothing to show for it. :mod:`lmer_platform.relations` carries the whole
    # argument, including what this costs — relations are local to this
    # orchestrator and do not travel with the runs.
    #
    # Three routes rather than a GET and a PUT of a list: adding and removing one
    # relation are the two things anyone does, and a whole-list PUT would make the
    # UI and the assistant race each other for a set they both edit — last writer
    # wins, silently dropping the other's relation. Named verbs, like
    # adopt/forget, which is this API's grammar for a change to the run index.
    #
    # The write path is the API precisely so the orchestrating assistant and the
    # browser use the same one (T30): the assistant relates a develop run to the
    # review run it just started, over REST, with the same bearer credential as
    # any other client.
    #
    # Keyed on the run like adopt/forget/meta, and by query string on the read for
    # that route's reason: a project is ``group/subgroup``, and a path parameter
    # would have to be escaped by every caller to survive the slash it contains.

    @app.get("/api/runs/relations", dependencies=guard)
    def run_relations(host: str = "", project: str = "", slug: str = "") -> dict:
        """The runs this orchestrator considers related to this one.

        Empty for most runs, and empty rather than 404 for a run that is not
        tracked at all — the same quiet read ``GET /api/runs/meta`` performs, for
        the same reason: the caller asked which runs this one is related to, and
        "none" is the true answer.

        Each relation says whether this orchestrator tracks the run it names.
        ``tracked: false`` is an ordinary state, not an error: relating a run
        before adopting it is allowed, and forgetting a run does not remove its
        relations. A client shows the key with a hint rather than a way through —
        there is no run in this fleet to switch to.

        The three parts default to empty strings rather than being required by
        FastAPI, so a caller that leaves one out gets
        :func:`lmer_platform.runs.run_key`'s refusal — which names the field —
        instead of a validation error in a different shape from every other
        refusal here.
        """
        try:
            return _relations_reply(host, project, slug)
        except RunIndexError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/runs/relate", dependencies=guard)
    def relate_runs(body: dict) -> dict:
        """Relate two runs, so each is one tap from the other.

        Body is ``/api/runs/adopt``'s plus ``related``, the other run as an object
        of the same three fields — see :func:`_related_ref` for why it is not three
        new field names.

        The relation is **symmetric and stored once**: relating A to B is the same
        act as relating B to A, both runs' pages show it afterwards, and doing it
        twice changes nothing and answers 200 with ``created: false``. Neither run
        has to be tracked, because relating a run ahead of adopting it is
        legitimate.

        200, not 201 or 202: when this returns the relation is on disk, and the
        body carries the subject run's relations as they now are. Statuses ride on
        :class:`lmer_platform.relations.RelationError` so a refusal added later
        arrives with its own code instead of a 500.
        """
        host, project, slug = body.get("host"), body.get("project"), body.get("slug")
        try:
            related = _related_ref(body)
            created = relations.relate((host, project, slug), related)
            return _relations_reply(
                host, project, slug,
                {"related": _run_ref_dict(related), "created": created},
            )
        except RunIndexError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RelationError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    @app.post("/api/runs/unrelate", dependencies=guard)
    def unrelate_runs(body: dict) -> dict:
        """Remove a relation between two runs, in either direction.

        Same body as ``/api/runs/relate``. One entry *is* the relation, so this
        removes both directions at once — there is no state in which one run still
        links to the other — and the order the two runs are named in does not
        matter.

        ``removed: false`` rather than a 404 for a pair that was not related: the
        caller asked for the relation to be gone, and it is. This is also the way
        to clear a relation naming a run nobody tracks any more, which is why it
        requires no adoption.
        """
        host, project, slug = body.get("host"), body.get("project"), body.get("slug")
        try:
            related = _related_ref(body)
            removed = relations.unrelate((host, project, slug), related)
            return _relations_reply(
                host, project, slug,
                {"related": _run_ref_dict(related), "removed": removed},
            )
        except RunIndexError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RelationError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    @app.post("/api/runs/answer", dependencies=guard, status_code=202)
    def answer(body: dict) -> dict:
        """Answer a run that stopped to ask a question, by respawning it (T19).

        Keyed on the **run**, and the body shape is ``/api/runs/adopt``'s plus the
        answer, because the run is the entity that has a question — the session
        that asked has exited, so there is frequently no session id to key on at
        all (spec D15). T18 flagged that ``/api/sessions/{id}/messages`` is keyed
        on a session while answering *for a run*; this route deliberately does not
        inherit that shape.

        **202, not 201 or 200.** Nothing is recorded by the time this returns: a
        session has been started, and it applies the answer at its own session
        start a moment later, in the container. 200 would imply the run's state
        already carries the answer, and 201 would name a resource this URL does not
        create. The run's row catches up on the next poll, which is also how the UI
        learns it worked.

        Statuses come off :class:`lmer_platform.answer.AnswerError` so a refusal
        added later arrives with its own code rather than as a 500, and the
        concurrency cap answers 429 exactly as ``POST /api/sessions`` does — an
        answer is not a reason to exceed it.
        """
        try:
            request = AnswerRequest(
                host=body.get("host") or "",
                project=body.get("project") or "",
                slug=body.get("slug") or "",
                # Not coerced: a non-string answer keeps its own refusal message
                # rather than being reported as an empty one.
                answer=body.get("answer") or "",
            )
            result = answer_run(config, request)
        except AnswerError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        # The same 409 ``NotAnswerable`` gives this route for a run that is already
        # running, for the residual case the answer path's own check cannot see: the
        # spawn's invariant matches the identity it is about to register, so it
        # catches a live session the recorded slug did not lead to.
        except RunAlreadyLive as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CapacityError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except SpawnError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result.to_dict()

    @app.post("/api/runs/resume", dependencies=guard, status_code=202)
    def resume(body: dict) -> dict:
        """Continue a tracked run by starting its next session (T25, T41).

        Keyed on the **run** and shaped like ``/api/runs/answer`` for the same
        reason: the session that stopped has exited (spec D15), so there is nothing
        to type into and continuing means spawning. The body is
        ``/api/runs/adopt``'s plus the three things a caller may say about a resume
        — ``taskdef`` (the override that starts a sibling run against the same
        target), ``repo_url`` and ``direction``. All three are optional, and none is
        coerced: a non-string keeps its own refusal instead of arriving as ``None``.

        **202, not 200**, exactly as the answer route is: when this returns a
        session has been started and nothing else has happened yet. The run claims
        itself and prints its own resume brief in the container a moment later,
        which is what the next fleet poll sees.

        The one way this route differs from every other here is its **error body**:
        ``{"code": …, "message": …}`` (:meth:`lmer_platform.resume.ResumeError.to_dict`)
        rather than a bare string. Two of these refusals — ``repo_url_required`` and
        ``direction_required`` — are requests for one more field rather than
        failures, and a client that had to match an English sentence to tell them
        apart would break the first time the sentence was improved. The status still
        rides on the exception, so a refusal added later arrives with a code instead
        of falling through to a 500.

        The direction is not echoed back (see
        :meth:`lmer_platform.resume.ResumeResult.to_dict`): it is the operator's
        content and it travels in the spawn's argv.
        """
        try:
            request = ResumeRequest(
                host=body.get("host") or "",
                project=body.get("project") or "",
                slug=body.get("slug") or "",
                taskdef=body.get("taskdef"),
                repo_url=body.get("repo_url"),
                direction=body.get("direction"),
            )
            result = resume_run(config, request)
        except ResumeError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.to_dict()) from exc
        # The residual case of ``RunIsLive`` above: the spawn's invariant matches the
        # identity it is about to register, so it sees a live session the recorded
        # slug did not lead to. A sentence rather than this route's {code, message} —
        # the same shape the cap answers with, and one a client reads as a failure
        # (no code) rather than as a request for another field, which is right.
        except RunAlreadyLive as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # Before SpawnError, which CapacityError subclasses: the cap is a 429 with
        # the numbers in it, and continuing a run is not a reason to exceed it.
        except CapacityError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except SpawnError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result.to_dict()

    # --- the orchestrating assistant (spec §8, T60) ---------------------------
    #
    # :mod:`lmer_platform.assistant` had the whole lifecycle and nothing here
    # imported it, so ``status``, ``set_handoff`` and ``take_pending`` were
    # callable only in-process — while the ``orchestrate`` taskdef tells the
    # assistant to *ask this API* for the handover note its predecessor left. A
    # prompt that says "ask for it" pointed at an API that cannot answer is the
    # gap these routes close, and the entry in ``GET /api`` above is half of the
    # fix: the taskdef makes that list the authority on what exists, so a route
    # missing from it is invisible to the assistant even when it works.
    #
    # Statuses ride on :class:`lmer_platform.assistant.AssistantError` as
    # everywhere else here — 409 for a second assistant, 429 at the concurrency
    # cap, 503 for a host that cannot see the taskdef — so a refusal added later
    # arrives with its own code instead of a 500.
    #
    # Stopping goes through ``assistant.stop`` and never through
    # ``POST /api/sessions/{id}/exit``: that verb refuses a ``kind="assistant"``
    # session outright (:func:`lmer_platform.lifecycle._signallable_pid`), because
    # the signal is the easy half — clearing the pointer and recording
    # ``stopped_at``/``stop_reason`` belongs to the module that owns the state
    # file, and the fleet view lists the assistant as a row like any other, so
    # the wrong verb is one tap away rather than hypothetical.

    @app.get("/api/assistant", dependencies=guard)
    def assistant_status() -> dict:
        """Is the assistant running, and what does it know (spec §8.1).

        A read, and pointedly not
        :func:`lmer_platform.assistant.ensure_running`: this is what a UI polls to
        draw the chat's state, and a read that started a container would make
        opening a page cost a session slot.

        200 for a host that has never had one — ``running: false`` at
        ``generation: 0`` is the fresh-host answer, not an error (D11: started on
        demand). ``stale`` means state names a session nothing is running under
        and ``tracked: false`` means an assistant is up that this state file never
        recorded; both are reported rather than repaired here, so a status poll
        can never race a start.

        The same body start, stop and rotate answer with, so a client renders one
        shape however it arrived at it.
        """
        return assistant.status().to_dict()

    @app.post("/api/assistant/start", dependencies=guard)
    def assistant_start(body: Optional[dict] = None) -> dict:
        """Start the assistant. One at a time (D11), so a second is a 409.

        **200, not 201 or 202.** The fact the caller asked for is true when this
        returns: the session is spawned and in the registry, and the body is the
        reconciled status rather than an acknowledgement — which is the honest
        difference from ``/api/runs/answer``, where the work happens in a
        container afterwards.

        The 409 is the useful answer to two operators tapping "open chat": the
        incumbent keeps its context window, which is why this refuses instead of
        replacing. A client should read it as "one is running" and re-read the
        status; ``rotate`` is the verb for wanting a fresh one.

        ``handoff`` is optional and replaces what this incarnation is told;
        omitting it carries the recorded note forward, which is what makes a
        respawn after a crash as informed as a planned rotation. Not coerced — a
        non-string keeps its own refusal rather than arriving as ``None`` and
        reading as "leave it alone".

        **This is also where supervision is re-armed** (T75). The daemon's
        supervisor stops looping when it gives up, and this route is what its
        give-up message tells the operator to call — so a start here that only
        started a container would hand back an assistant nothing was watching, and
        the next crash would be silent. :func:`lmer_platform.assistant.resume_supervision`
        is called *after* the start rather than before, so a refusal re-arms
        nothing, and its answer is deliberately not in the reply: whether this
        daemon supervises at all is not a fact about the request, and a client that
        branched on it would be reading the daemon's wiring out of an assistant's
        status.

        ``model``/``harness``/``preset``/``agents`` are the per-call launch
        overrides (issue #234), beating the standing chain the way an explicit
        override beats an export everywhere else in the config; omitted keys fall
        through to it (:func:`lmer_platform.assistant._launch_settings`).
        """
        payload = body or {}
        try:
            started = assistant.start(
                config,
                handoff=payload.get("handoff"),
                settings=_body_launch_settings(payload),
            )
        except AssistantError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        assistant.resume_supervision()
        return started.to_dict()

    @app.post("/api/assistant/stop", dependencies=guard)
    def assistant_stop(body: Optional[dict] = None) -> dict:
        """Stop the assistant, with the bookkeeping only this path does.

        Not ``POST /api/sessions/{id}/exit`` — see the note above this block for
        why that route refuses an assistant rather than trusting anyone to
        remember.

        ``stopped: false`` is a normal answer rather than a failure. Usually it
        means nothing was running, and the stale pointer has been cleared as part
        of answering; it also covers the process that would not die to SIGTERM or
        SIGKILL, which ``running: true`` in the same body distinguishes.

        ``reason`` comes back because it is what was *recorded* — the field is
        optional and defaults to ``operator``, so a caller that omitted it cannot
        otherwise know what a later "why did it end" question will read. An
        unknown one is a 400: ``rotation`` has to stay distinguishable from an
        operator stop, or the next context-pressure question is unanswerable from
        state alone.

        ``handoff`` is optional and is recorded even when nothing was running,
        which is what lets an assistant write its successor's note and stop in
        one call.
        """
        payload = body or {}
        # Defaulted on absence rather than on falsiness: ``{"reason": ""}`` is a
        # caller mistake and gets the module's refusal, instead of being quietly
        # recorded as an operator stop.
        reason = payload.get("reason", "operator")
        try:
            stopped = assistant.stop(reason=reason, handoff=payload.get("handoff"))
        except AssistantError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        return {
            "stopped": stopped,
            "reason": reason,
            **assistant.status().to_dict(),
        }

    @app.post("/api/assistant/rotate", dependencies=guard)
    def assistant_rotate(body: Optional[dict] = None) -> dict:
        """Replace the assistant with a fresh window, carrying a note over (§8.3).

        One call rather than a stop and a start, because two would leave a window
        where an operator's ``start`` lands between them and wins the generation
        counter. The *trigger* is not here — an age or context-pressure policy is
        the daemon's to run, reading ``age_seconds`` off the status — this is the
        transition it would drive.

        Starts one when nothing is running, deliberately: the case a rotation
        fires in is often an assistant that has already died, and refusing there
        would leave the operator with a 409 and no chat.

        Takes the same per-call launch overrides as start (issue #234) — though
        the ordinary way a settings change lands is a rotate with *none*: the
        replacement resolves the standing chain fresh, so "persist, then
        rotate" already runs the new configuration.
        """
        payload = body or {}
        try:
            rotated = assistant.rotate(
                config,
                handoff=payload.get("handoff"),
                settings=_body_launch_settings(payload),
            )
        except AssistantError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        return rotated.to_dict()

    @app.get("/api/assistant/handoff", dependencies=guard)
    def assistant_handoff() -> dict:
        """The note the previous incarnation left for this one (§8.3).

        The route the ``orchestrate`` taskdef sends a starting assistant to: it is
        told to read its handover before it says anything to the operator, and to
        say plainly that it was not briefed when there is none. Both halves need
        an answer — ``handoff: null`` with a ``note`` is "nobody left you
        anything", which is a different fact from a 404, and the instruction turns
        on being able to tell them apart.

        The note travels through platform state rather than argv on purpose: an
        ``lmer --prompt`` carrying a fleet summary would put it in ``ps``, in the
        command list ``POST /api/sessions`` echoes back, and in ``events.jsonl``.
        """
        return _handoff_reply(assistant.read_state())

    @app.post("/api/assistant/handoff", dependencies=guard)
    def set_assistant_handoff(body: dict) -> dict:
        """Record what the next incarnation should be told.

        Written by the assistant itself, over this route, with the same bearer
        credential as any other client (T30) — which also makes the text
        agent-authored and therefore untrusted: it is bounded here, and the
        ``limit`` in the reply is the number that will actually refuse it, so a
        composer counts against the daemon rather than against a copy that drifts.

        Recorded whether or not an assistant is running: the note is for the
        *next* one, and the case it exists for is writing it while the current one
        still has the context to write it from.
        """
        try:
            # Not coerced, as elsewhere: a non-string keeps the module's own
            # refusal instead of being reported as an empty one.
            state = assistant.set_handoff(body.get("handoff"))
        except AssistantError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        return _handoff_reply(state)

    @app.get("/api/assistant/instructions", dependencies=guard)
    def assistant_instructions() -> dict:
        """The operator's standing orders, read at every incarnation start (T87).

        The handoff's sibling, and the difference is the whole feature: a handoff
        is a baton one successor reads once, while this is a *standing* document —
        nothing consumes it, a rotation carries it forward, and it is fetched again
        by every incarnation because the operator's preferences did not stop being
        true when a context window filled.

        A GET, with no destructive twin anywhere: the only writer is
        ``POST /api/assistant/instructions``, and this is safe to poll, safe to
        prefetch and safe to render in a browser. ``instructions: null`` with a
        ``note`` is the ordinary answer on a host where the operator has never
        stated a preference, which the taskdef has to be able to tell apart from a
        route this build does not serve.
        """
        return _instructions_reply(assistant.read_state())

    @app.post("/api/assistant/instructions", dependencies=guard)
    def set_assistant_instructions(body: dict) -> dict:
        """Replace the standing-orders document. The chat is the write path (T87).

        Written by the assistant, in the operator's words, after it has confirmed
        the wording back to them — the operator asked for exactly that instead of a
        settings screen, so the UI shows this read-only and the conversation is the
        only thing that edits it. Which makes the text agent-authored and therefore
        untrusted twice over: it is scrubbed and bounded in
        :func:`lmer_platform.assistant.set_instructions`, and the ``limit`` in the
        reply is the number that will actually refuse it.

        Whole-document, not a patch, and the reply is what was stored: an assistant
        composing a rewrite has to re-read first, and being handed back the result
        is what lets it check that the rule it just promised the operator is the one
        in the file.

        Recorded whether or not an assistant is running, like the handoff — a
        preference stated to an incarnation that then dies is still the operator's
        preference.
        """
        try:
            # Uncoerced, as with the handoff: a non-string keeps the module's own
            # refusal rather than being stored as ``str()`` of whatever arrived.
            state = assistant.set_instructions(body.get("instructions"))
        except AssistantError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        return _instructions_reply(state)

    @app.post("/api/assistant/pending", dependencies=guard)
    def assistant_pending() -> dict:
        """Take the digests the daemon spooled for the assistant, and clear them.

        A POST because it is **destructive**, and draining is what the module
        does: a cursor would need a reader identity, and the reader is a session
        that gets replaced on rotation (§8.3). Spelled as a safe method, a browser
        prefetch or a double-poll would silently eat the operator's digests.

        Nothing is lost by never calling it — the attention list the daemon
        computes mechanically is in ``/api/state`` and the history is in
        ``events.jsonl`` — and the *count* is on the status, so a UI can show that
        something is waiting without consuming it. An empty spool is ``[]`` and a
        200, which is most calls.

        **Nothing pushes this to the assistant** (T89). The spool waits here until
        somebody takes it, and an idle session has no turn in which to call this —
        which is how a finished review once sat unread until it was evicted. The
        non-consuming signal is ``pending`` on ``GET /api/assistant``, and what the
        ``orchestrate`` taskdef does with it is arm a background watch on that
        count; this route is what the watch's wake-up calls, never what it polls.
        """
        notes = assistant.take_pending()
        return {"pending": notes, "count": len(notes)}

    @app.get("/api/assistant/config", dependencies=guard)
    def assistant_config() -> dict:
        """How the next incarnation will be run, and who decided (issue #234).

        Effective values, resolved through the standing chain **fresh** — the
        environment and ``config.json`` as they are now, never this app's
        boot-time config — because fresh is what the next start actually reads
        (:func:`lmer_platform.assistant._launch_settings`). Each key carries its
        ``source`` (``env`` / ``config.json`` / ``default``), which is the fact a
        settings screen needs to stop lying: an export shadows what the POST
        below persists, and a screen that appears to have no effect is a bad
        afternoon (the same reason ``binding_notice`` names its source).

        What the *running* incarnation was launched with is deliberately not
        here — it is on ``GET /api/assistant`` (``settings``), because it is a
        fact about the session, not about the configuration. The two differing
        is the normal state between a settings change and the next rotate.
        """
        return _assistant_config_reply()

    @app.post("/api/assistant/config", dependencies=guard)
    def set_assistant_config(body: dict) -> dict:
        """Persist assistant launch settings into ``config.json`` (issue #234).

        The stored layer and only it: an export stays an export (and keeps
        shadowing the file — the reply's ``source`` says so rather than letting
        the write look effective), and what lands in the file is what was sent,
        never an env-resolved value baked in where it would outlive the export
        (:func:`lmer_platform.config.update_stored`).

        Keys are the four setting names; omitted keys are left alone, so this is
        a patch of exactly what the caller named. ``null`` — and the emptied
        text field a browser sends as ``""`` — clears the key, letting the layer
        below show through. Anything else must be usable text: the caller is
        asking for a change, so an unusable value is refused with the field
        named (400), the posture every explicit ask gets here, while the
        warn-and-fall-back treatment is reserved for the standing layers read at
        start time.

        Nothing restarts on a write, deliberately: the running incarnation keeps
        its context window, the reply says so (``note``), and the rotate verb is
        one call away when the operator wants the new settings live.
        """
        if not isinstance(body, dict) or not body:
            raise HTTPException(
                status_code=400,
                detail=(
                    "name at least one setting to change — "
                    f"{', '.join(sorted(ASSISTANT_SETTING_KEYS))}"
                ),
            )
        changes: dict = {}
        for key, value in body.items():
            if key not in ASSISTANT_SETTING_KEYS:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"unknown assistant setting {key!r}: expected one of "
                        f"{', '.join(sorted(ASSISTANT_SETTING_KEYS))}"
                    ),
                )
            field = ASSISTANT_SETTING_KEYS[key][0]
            # Clearing is this route's own semantic — null, and the emptied
            # text field a browser sends as "" — everything else goes through
            # the ONE definition of a usable explicit value, shared with the
            # start/rotate overrides. The rules living in one place is the
            # point: a value this route persists is a value every later start
            # resolves, so a rule missed here would come back as a warning (or
            # worse) on every incarnation.
            if value is None or (isinstance(value, str) and not value.strip()):
                changes[field] = None
                continue
            try:
                changes[field] = validate_assistant_override(key, value)
            except ConfigError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            update_stored(changes)
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _assistant_config_reply({"changed": sorted(body)})

    @app.post("/api/prune", dependencies=guard)
    def prune() -> dict:
        """Forget registry entries whose session process is gone.

        Separate from ``rescan`` on purpose: a stale entry is how a crashed run
        stays visible, so discarding it is an acknowledgement the operator makes
        explicitly rather than a side effect of refreshing the view.
        """
        removed = prune_dead()
        return {"removed": removed, "count": len(removed)}

    return app
