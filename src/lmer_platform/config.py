"""Platform configuration and the shared secret.

Configuration is a single ``config.json`` under the platform state dir, editable
by hand or through the UI, plus environment overrides for the values an operator
wants to set per-invocation. Resolution order is the one the rest of lmer uses —
**explicit override > ``LMER_PLATFORM_*`` environment > ``config.json`` >
default** — so a flag beats an export beats the persisted file. The one wrinkle
worth knowing is that an export therefore *shadows* what the UI wrote, which is
why :func:`binding_notice` names where the effective bind values came from: a
config screen that appears to have no effect is a bad afternoon.

Two things deliberately do not live in ``config.json``:

- **The shared secret.** It lives in its own mode-0600 file, because
  ``config.json`` is the file an operator opens, screenshots, and pastes into a
  ticket. :func:`save` writes only known fields, so a secret cannot be smuggled
  in by a caller passing extra keys.
- **Derived paths.** ``secret_file`` and ``work_repo_mirror`` default to ``None``
  meaning "derive from the platform state dir at call time", rather than being
  frozen into the file at first write. That keeps a config written on one host
  valid on another, and keeps tests honest — they can repoint the state dir
  without a stale absolute path leaking through.

Bind defaults to loopback (spec D14). The operator sets address and port; the
daemon terminates no TLS and makes no assumptions about a proxy (D9), so a
non-loopback bind serves plaintext and :func:`binding_notice` says so out loud.

A setting nothing can enforce is not configuration (T75)
--------------------------------------------------------
``max_concurrent_assistant_spawns`` was a field here — spec §6.4/§8.2's bound on
the sessions the *assistant* initiates, as distinct from
:attr:`PlatformConfig.max_concurrent_sessions`' bound on the host. It was loaded,
validated, persisted and served under ``GET /api/state``, and no code path ever
read it, so what an operator lowered was a number in a JSON file.

It is deleted rather than wired up, because wiring it up is not currently
possible: enforcement needs the daemon to know that *this* spawn came from the
assistant, and there is exactly one shared secret for the whole API (see
:func:`ensure_secret` and ``api.require_secret``) with no per-caller identity
behind it. Every request looks like the operator's, which is also why the
assistant holds the operator's own key (see
:func:`lmer_platform.assistant._assistant_environment`). A cap that silently
counted the operator's own spawns against the assistant's allowance would be
worse than the absent one: it would refuse the browser and blame the chat window.

What would bring it back, to exactly the spot in :class:`PlatformConfig` where a
comment now marks it: callers become attributable. A second credential minted per
assistant incarnation, or a per-session token the spawn route can read an identity
off — either makes "sessions this assistant started" a countable thing, at which
point the field, its validation, its ``GET /api/state`` entry and its enforcement
in :func:`lmer_platform.spawn.spawn_session` all land together instead of the
first three arriving years early. A ``config.json`` still carrying the retired key
loads unchanged: :func:`load` keeps only known fields, which is the same
tolerance that lets a file written by a *newer* build stay loadable.

A bind address is not an address a container can dial
-----------------------------------------------------
:func:`container_base_url` answers a different question from :attr:`base_url`:
where the platform is reachable *from inside a container this host starts*. The
two answers differ for the default configuration and for the common one, so
handing the bind address through would be handing out a URL that fails —
``127.0.0.1`` inside a container is the container's own loopback, and
``0.0.0.0`` is not a destination anywhere. See that function for the derivation
and for the case where the honest answer is "there isn't one".
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import secrets
import stat
import subprocess
import threading
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

# One definition of the credential scrub, imported rather than reimplemented —
# the same trade :mod:`lmer_platform.workrepo` makes, and it says why there.
from lmer_cli.container.clone_and_exec import _scrub_credentials
from lmer_cli.runtime import RuntimeErrorDetect, detect_runtime
# The token lookup's own host parser: the issuing-host default seeded below has
# to agree with what :func:`lmer_cli.tokens._gitlab_token_issuing_host` reads.
from lmer_cli.tokens import _host_from_git_url
from lmer_cli.util import get_bool_env
# The forge names are the URL builder's, imported rather than spelled again here:
# what ``work_repo_forge`` accepts has to be exactly what
# :func:`work_repo.git_ops.forge_web_url` knows how to build paths for, and a
# second copy of the strings is how "gitlab" starts meaning two things.
from work_repo.git_ops import FORGE_GITHUB, FORGE_GITLAB

from .store import (
    StoreError, ensure_state_dir, read_json, platform_dir, snapshot_path,
    write_json,
)

logger = logging.getLogger("lmer_platform.config")

__all__ = [
    "ConfigError", "PlatformConfig", "CONFIG_FILENAME", "SECRET_FILENAME",
    "DEFAULT_BIND_ADDRESS", "DEFAULT_BIND_PORT", "ENV_REPO_URL",
    "ENV_WORK_REPO_FORGE", "WORK_REPO_FORGE_NONE", "WORK_REPO_FORGE_VALUES",
    "ENV_CONTAINER_URL", "PODMAN_HOST_ALIAS", "DOCKER_BRIDGE_NETWORK",
    "DOCKER_BRIDGE_INTERFACE", "ContainerReach", "config_path",
    "load", "save", "read_secret", "ensure_secret", "active_secret",
    "ASSISTANT_CREDENTIAL_FILENAME", "assistant_credential_path",
    "mint_assistant_credential", "active_assistant_credential",
    "revoke_assistant_credential",
    "binding_notice", "configured_repo_url", "container_base_url",
    "ASSISTANT_SETTING_KEYS", "AssistantSetting", "assistant_settings",
    "update_stored", "validate_assistant_override",
    "CHECKIN_SETTING_KEYS", "DEFAULT_CHECKIN_WINDOW_SECONDS",
    "ENV_CHECKIN_WINDOW", "checkin_settings", "validate_checkin_window",
    "INT_SETTINGS", "NUDGE_SETTING_KEYS", "DEFAULT_NUDGE_AFTER_SECONDS",
    "DEFAULT_NUDGE_PENDING_THRESHOLD", "ENV_NUDGE_AFTER_SECONDS",
    "ENV_NUDGE_PENDING_THRESHOLD", "nudge_settings", "validate_int_setting",
    "DEFAULT_STALL_IDLE_SECONDS", "DEFAULT_STALL_BACKSTOP_SECONDS",
    "ENV_STALL_IDLE_SECONDS", "ENV_STALL_BACKSTOP_SECONDS",
]

CONFIG_FILENAME = "config.json"
SECRET_FILENAME = "secret"
#: The *current* assistant incarnation's own credential (issue #244). Its own
#: file for ``config.json``'s reason, and deliberately **not** relocatable the way
#: ``secret_file`` is: the daemon mints, revokes and re-mints this one, and a
#: second location to keep in step is a second way to leave a live key behind.
ASSISTANT_CREDENTIAL_FILENAME = "assistant-credential"
MIRROR_DIRNAME = "work"

#: Loopback by default (D14). The operator opts into a reachable bind.
DEFAULT_BIND_ADDRESS = "127.0.0.1"
#: Chosen to sit clear of the ports lmer already allocates: the supervisor's
#: FastAPI range is 8700-8799 and general port passthrough uses 8800-8899.
DEFAULT_BIND_PORT = 8600
DEFAULT_PULL_INTERVAL_SECONDS = 30
#: What ``max_concurrent_sessions`` bounds is *worker* sessions
#: (:func:`lmer_platform.spawn._live_worker_count`): the orchestrating assistant
#: holds its own slot beside them, so a host left at this default runs eight
#: workers and the chat window rather than seven and a chat window.
#:
#: Eight rather than the four this shipped with, from the operator reading a live
#: fleet: four was a queue on a host that was not otherwise busy. The number is a
#: claim about how much work a machine can bear and nothing here can know that, so
#: what the raise buys is a default that stops refusing first — and the fleet view
#: now shows the occupancy against this cap (``web/src/App.vue``), so a host
#: running out of room says so before a spawn is refused for it.
DEFAULT_MAX_CONCURRENT_SESSIONS = 8
DEFAULT_MAX_FOLLOWUP_ROUNDS = 5
#: How long a run may go unchecked before the daemon says so (issue #244). The
#: operator's number, and a reminder interval rather than a deadline — a run past
#: it costs one line in one digest. ``0`` turns check-in digests off.
DEFAULT_CHECKIN_WINDOW_SECONDS = 3600

#: How long an unretrieved digest spool waits — and the assistant stays quiet —
#: before the daemon types a reminder into its session (issue #317). The
#: operator's number. ``0`` turns the nudge off, and is the only off-switch.
#: Full mechanics: ``docs/PLATFORM-QUICKSTART.md``, "Digest nudges".
DEFAULT_NUDGE_AFTER_SECONDS = 180
#: How many digests have to be waiting. 1 because everything in the spool is
#: material by construction, so "there is one" is already the condition.
DEFAULT_NUDGE_PENDING_THRESHOLD = 1
#: Ceiling on ``nudge_pending_threshold``: the spool it counts is bounded, so a
#: higher threshold could never be met and would be an undocumented off-switch.
#: :data:`lmer_platform.assistant.MAX_PENDING`, copied because that module imports
#: this one; ``tests/test_platform_nudge.py`` pins the copy.
MAX_NUDGE_PENDING_THRESHOLD = 50

#: How long a live session may produce nothing before halt detection considers
#: it (#243). An operator's number, not a derived one; silence alone does not
#: raise anything (:func:`lmer_platform.inventory._stalled`).
DEFAULT_STALL_IDLE_SECONDS = 600
#: Silence past this raises the flag whatever the transcript says, so a halt the
#: precise paths cannot recognise is found an hour late rather than never.
DEFAULT_STALL_BACKSTOP_SECONDS = 3600

ENV_BIND_ADDRESS = "LMER_PLATFORM_BIND_ADDRESS"
ENV_BIND_PORT = "LMER_PLATFORM_BIND_PORT"
ENV_SECRET_FILE = "LMER_PLATFORM_SECRET_FILE"
ENV_WORK_REPO_MIRROR = "LMER_PLATFORM_WORK_REPO_MIRROR"
ENV_AUTONOMOUS = "LMER_PLATFORM_AUTONOMOUS"
#: Not platform-scoped: the work repo URL is the same one the rest of lmer uses.
ENV_WORK_REPO = "LMER_WORK_REPO"
#: Also not platform-scoped, and deliberately the variable ``lmer`` itself reads:
#: the repository a daemon was started against. It is not a
#: :class:`PlatformConfig` field because it is not the platform's setting to own —
#: it is an export the whole tool honours — and because a copy in ``config.json``
#: would be a second answer to a question the spawn path has to get right.
#: Read through :func:`configured_repo_url`, never directly.
ENV_REPO_URL = "LMER_REPO_URL"
#: Which forge the **work** repo runs, when the operator has to say so: hostname
#: detection knows ``gitlab.com``, GitHub's names and the ``gitlab.<domain>``
#: convention, and knows nothing about ``git.<domain>`` or a GitHub Enterprise
#: Server on a custom name. See :attr:`PlatformConfig.work_repo_forge`.
ENV_WORK_REPO_FORGE = "LMER_PLATFORM_WORK_REPO_FORGE"
#: The operator's escape hatch from :func:`container_base_url`'s derivation: a
#: complete URL that reaches this platform from inside a container it starts.
#: Exists because the derivation reasons about a bind address and a runtime
#: name, and a reverse proxy, a custom network or a runtime this build does not
#: know about are all cases where an operator knows an answer the code cannot.
ENV_CONTAINER_URL = "LMER_PLATFORM_CONTAINER_URL"

#: The two halt-detection thresholds (#243). Both accept ``0``, which turns the
#: path they govern off — hence validated apart from their positive-integer
#: neighbours.
ENV_STALL_IDLE_SECONDS = "LMER_PLATFORM_STALL_IDLE_SECONDS"
ENV_STALL_BACKSTOP_SECONDS = "LMER_PLATFORM_STALL_BACKSTOP_SECONDS"

#: How the orchestrating assistant's session is *run*, per platform instance
#: (issue #234): the model, harness, preset and agents fan-out its ``lmer``
#: invocation is given. Exactly the four launch facts a worker spawn already
#: takes (:class:`lmer_platform.spawn.SpawnRequest`), because they are the four
#: that travel as flags — a flag beats the container's environment and a
#: preset's env, so what is configured here is what actually runs. Reasoning
#: effort is deliberately NOT among them: ``lmer`` has no effort flag, the
#: variable would have to ride ``--env-file``, and ``--env-file`` values lose
#: to the daemon's own exported environment — a setting that can be silently
#: shadowed is the failure mode this surface exists to avoid.
#:
#: Each defaults to ``None`` — today's behaviour, the session running whatever
#: its environment and harness settle on — and resolves by the module's usual
#: chain. The keys are the *request-body* spelling (``model``, not
#: ``assistant_model``): one vocabulary for the routes, the resolver and the
#: spawn fields they end up in.
ENV_ASSISTANT_MODEL = "LMER_PLATFORM_ASSISTANT_MODEL"
ENV_ASSISTANT_HARNESS = "LMER_PLATFORM_ASSISTANT_HARNESS"
ENV_ASSISTANT_PRESET = "LMER_PLATFORM_ASSISTANT_PRESET"
ENV_ASSISTANT_AGENTS = "LMER_PLATFORM_ASSISTANT_AGENTS"

#: The check-in window, per platform instance (issue #244) — how often to sweep
#: what has gone quiet is a property of a host's fleet, not of a build.
#:
#: Deliberately **not** in :data:`ASSISTANT_SETTING_KEYS`: those four become argv
#: tokens and are validated as such, while this is an integer the daemon reads on
#: its own tick and hands to nobody. Served beside them in its own group, so one
#: settings surface covers both without pretending they are one kind of thing.
ENV_CHECKIN_WINDOW = "LMER_PLATFORM_CHECKIN_WINDOW_SECONDS"

#: setting key -> (PlatformConfig field, env var). One entry today; a table so a
#: second knob costs one line, as in the launch table above.
CHECKIN_SETTING_KEYS = {
    "window_seconds": ("checkin_window_seconds", ENV_CHECKIN_WINDOW),
}

#: The digest nudge's two knobs, per platform instance (issue #317).
#: :data:`CHECKIN_SETTING_KEYS`' group rather than
#: :data:`ASSISTANT_SETTING_KEYS`', for that table's reason: integers the daemon
#: reads on its own tick, not argv tokens.
ENV_NUDGE_AFTER_SECONDS = "LMER_PLATFORM_NUDGE_AFTER_SECONDS"
ENV_NUDGE_PENDING_THRESHOLD = "LMER_PLATFORM_NUDGE_PENDING_THRESHOLD"

#: setting key -> (PlatformConfig field, env var), as above.
NUDGE_SETTING_KEYS = {
    "after_seconds": ("nudge_after_seconds", ENV_NUDGE_AFTER_SECONDS),
    "pending_threshold": (
        "nudge_pending_threshold", ENV_NUDGE_PENDING_THRESHOLD
    ),
}


@dataclass(frozen=True)
class _IntSettingRule:
    """What is true of one integer setting, wherever it is being read.

    The three postures this module takes on a value — warn-and-default inside
    ``load()``, warn-and-fall-through per layer, refuse an explicit write — all
    need the same three facts, and the alternative to naming them once is one
    copy of each rule per setting. That is how the copies start to disagree:
    ``checkin_window_seconds`` had its own set, and a second knob would have
    arrived with a second.
    """

    #: The default the warn-and-default posture falls back to.
    default: int
    #: Smallest usable value. ``0`` where zero is the off-switch, ``1`` where an
    #: off-switch lives on a neighbouring setting instead.
    minimum: int
    #: Why a value below :attr:`minimum` is refused, in the operator's terms —
    #: the sentence tells them what zero (or one) would have meant.
    floor_reason: str
    #: The log key the warn paths use. Per setting rather than one shared key,
    #: because a grep for a specific misconfiguration is how these are found.
    log_key: str
    #: What the value counts, so the shared warn message keeps its unit.
    unit: str
    #: The one-count spelling, declared rather than guessed from the plural.
    singular_unit: str
    #: Largest usable value, or ``None`` where a larger number is merely slower
    #: rather than unreachable. Required wherever the setting is measured against
    #: something bounded: an unsatisfiable value is an undocumented off-switch.
    maximum: Optional[int] = None
    #: Why a value above :attr:`maximum` is refused; names the real off-switch.
    ceiling_reason: Optional[str] = None


#: PlatformConfig field -> its rule, for every integer setting resolved by the
#: warn-don't-refuse path. The strict ones (``bind_port``) keep :func:`_env_int`,
#: which raises: a daemon bound where nobody expects it beats one that will not
#: start.
INT_SETTINGS = {
    "checkin_window_seconds": _IntSettingRule(
        default=DEFAULT_CHECKIN_WINDOW_SECONDS,
        minimum=0,
        floor_reason=(
            "a window cannot be negative — 0 disables check-in digests"
        ),
        log_key="platform_checkin_window_invalid",
        unit="seconds",
        singular_unit="second",
    ),
    "nudge_after_seconds": _IntSettingRule(
        default=DEFAULT_NUDGE_AFTER_SECONDS,
        minimum=0,
        floor_reason=(
            "an interval cannot be negative — 0 disables the digest nudge"
        ),
        log_key="platform_nudge_after_invalid",
        unit="seconds",
        singular_unit="second",
    ),
    "nudge_pending_threshold": _IntSettingRule(
        default=DEFAULT_NUDGE_PENDING_THRESHOLD,
        minimum=1,
        floor_reason=(
            "at least one digest has to be waiting for a nudge to be about "
            "anything — disable the nudge with nudge_after_seconds=0 instead"
        ),
        log_key="platform_nudge_threshold_invalid",
        unit="digests",
        singular_unit="digest",
        maximum=MAX_NUDGE_PENDING_THRESHOLD,
        ceiling_reason=(
            f"the digest spool holds at most {MAX_NUDGE_PENDING_THRESHOLD}, so a "
            "higher threshold could never be met — disable the nudge with "
            "nudge_after_seconds=0 instead"
        ),
    ),
}

#: setting key -> (PlatformConfig field, env var). The one table the loader,
#: the resolver and the API routes all read, so a fifth setting is added in
#: exactly one place.
ASSISTANT_SETTING_KEYS = {
    "model": ("assistant_model", ENV_ASSISTANT_MODEL),
    "harness": ("assistant_harness", ENV_ASSISTANT_HARNESS),
    "preset": ("assistant_preset", ENV_ASSISTANT_PRESET),
    "agents": ("assistant_agents", ENV_ASSISTANT_AGENTS),
}

#: ``work_repo_forge``'s third value, which is not a forge: the operator saying
#: "build no links for this work repo". The only way to get unlinked run files,
#: since an undetectable host now defaults to GitLab's layout.
WORK_REPO_FORGE_NONE = "none"
#: Everything ``work_repo_forge`` accepts. Unset is not in here — that is the
#: default, and it means detect the host and fall back to GitLab.
WORK_REPO_FORGE_VALUES = (FORGE_GITLAB, FORGE_GITHUB, WORK_REPO_FORGE_NONE)

#: The name podman puts in every container's ``/etc/hosts`` for the machine it
#: runs on, under both root and rootless. Podman-only on purpose: docker
#: resolves ``host.docker.internal`` on Docker Desktop but not on Linux unless
#: the run is given ``--add-host=host.docker.internal:host-gateway``, and
#: :func:`lmer_cli.runtime.base_run_args` passes no such flag — so on a docker
#: host there is no *name* for the host. There is an address, and
#: :func:`_docker_bridge_gateway` derives it; what neither does is invent a
#: hostname and hope it resolves.
PODMAN_HOST_ALIAS = "host.containers.internal"

#: The docker network whose gateway *is* this host, seen from inside a container:
#: ``lmer_cli.runtime.base_run_args`` passes no ``--network``, so every session
#: docker starts for us lands on the default bridge.
DOCKER_BRIDGE_NETWORK = "bridge"
#: The interface that bridge is normally built on, read only when the daemon
#: itself cannot be asked — see :func:`_docker_bridge_gateway` for why that is
#: the second probe and not the first.
DOCKER_BRIDGE_INTERFACE = "docker0"
#: ``--format`` for ``docker network inspect``: one gateway per IPAM entry, which
#: is one line on a v4-only bridge and two when the daemon has ip6tables on.
DOCKER_GATEWAY_FORMAT = "{{range .IPAM.Config}}{{.Gateway}}\n{{end}}"
#: Long enough for a round-trip to a local daemon socket, short enough that a
#: wedged docker cannot stall an assistant start. Waiting longer buys nothing:
#: the probe answering nothing is a supported outcome, not an error to avoid.
DOCKER_PROBE_TIMEOUT_SECONDS = 5


class ConfigError(RuntimeError):
    """Raised when configuration is present but unusable."""


@dataclass(frozen=True)
class PlatformConfig:
    """Resolved platform configuration.

    ``secret_file`` and ``work_repo_mirror`` are ``None`` when they should be
    derived from the platform state dir; use :attr:`secret_path` and
    :attr:`mirror_path` rather than the raw fields.
    """

    bind_address: str = DEFAULT_BIND_ADDRESS
    bind_port: int = DEFAULT_BIND_PORT
    secret_file: Optional[str] = None
    #: Full path to the ``lmer`` executable to spawn. ``None`` means resolve from
    #: ``PATH``; set it to pin a specific checkout.
    lmer_bin: Optional[str] = None
    work_repo_url: Optional[str] = None
    work_repo_mirror: Optional[str] = None
    #: Which forge builds the run-file links: :data:`WORK_REPO_FORGE_VALUES`, or
    #: ``None`` (the default) for "detect the work repo's host, and treat a host
    #: that cannot be told as GitLab". Set, it **beats** detection — it is the
    #: operator answering a question the hostname cannot, for a GitHub Enterprise
    #: Server on a custom name as much as for a self-hosted GitLab — and
    #: :data:`WORK_REPO_FORGE_NONE` turns the links off altogether.
    work_repo_forge: Optional[str] = None
    work_repo_pull_interval: int = DEFAULT_PULL_INTERVAL_SECONDS
    #: How many *worker* sessions this host may run at once. The orchestrating
    #: assistant is not one of them — see
    #: :func:`lmer_platform.spawn._live_worker_count`.
    max_concurrent_sessions: int = DEFAULT_MAX_CONCURRENT_SESSIONS
    # ``max_concurrent_assistant_spawns`` lived here, between the two caps it is
    # not. Retired by T75 rather than left standing: see the module docstring for
    # why an unattributable cap cannot be enforced, and what would have to become
    # true for this field to come back to exactly this spot.
    max_followup_rounds: int = DEFAULT_MAX_FOLLOWUP_ROUNDS
    #: Halt detection (#243), in seconds of silence: when the precise paths may
    #: fire, and when silence alone is enough. ``0`` disables each.
    stall_idle_seconds: int = DEFAULT_STALL_IDLE_SECONDS
    stall_backstop_seconds: int = DEFAULT_STALL_BACKSTOP_SECONDS
    autonomous_default: bool = False
    park_idle_side: bool = False
    #: How the orchestrating assistant's session is run (issue #234) — see
    #: :data:`ASSISTANT_SETTING_KEYS`. ``None`` is today's behaviour: the
    #: session runs whatever its environment, its preset or its harness settles
    #: on. Values are names handed to ``lmer``'s own flags verbatim — the model
    #: name in particular is not validated here, because the harness is the
    #: only thing that knows which ids it serves and a platform-side allowlist
    #: would be a second, staler opinion (the same stance
    #: :class:`lmer_platform.spawn.SpawnRequest` takes).
    assistant_model: Optional[str] = None
    assistant_harness: Optional[str] = None
    assistant_preset: Optional[str] = None
    assistant_agents: Optional[str] = None
    #: Service slots this host declares (issue #245) — the raw ``config.json``
    #: entries, deliberately unparsed here. :mod:`lmer_platform.slots` turns
    #: them into definitions and skips a malformed one with a warning; parsing
    #: at this layer would make a typo in one slot either raise at boot — and a
    #: daemon that will not start cannot be reconfigured through its own UI — or
    #: leave this field holding a shape the file does not have.
    #:
    #: No environment override, unlike every scalar above it: a JSON list in an
    #: env var is a footgun, and declaring slots is once-per-host file work.
    slots: tuple = ()
    #: How long a run may go unchecked before the daemon spools a digest naming
    #: it (issue #244), in seconds. ``0`` disables check-in digests; see
    #: :data:`ENV_CHECKIN_WINDOW` for why this sits apart from the four above.
    checkin_window_seconds: int = DEFAULT_CHECKIN_WINDOW_SECONDS
    #: The digest nudge's interval and threshold (issue #317), in seconds and in
    #: digests. See :data:`NUDGE_SETTING_KEYS` and :data:`INT_SETTINGS`.
    nudge_after_seconds: int = DEFAULT_NUDGE_AFTER_SECONDS
    nudge_pending_threshold: int = DEFAULT_NUDGE_PENDING_THRESHOLD
    #: The Matrix bridge's settings (issue #327) — the raw ``config.json``
    #: mapping, deliberately unparsed here for the reason :attr:`slots` gives:
    #: :mod:`matrix_bridge.config` is the only module that knows what a
    #: capability is, and a typo in an allowlist must refuse *the bridge*
    #: rather than a daemon that would then be unreconfigurable through its own
    #: UI. Validated here only as far as "it is a mapping".
    #:
    #: It is a field rather than an unknown key the loader tolerates because the
    #: bridge writes back: D5 records the room id after creating the room, and
    #: :func:`update_stored` refuses unknown fields — while :func:`save` would
    #: *delete* an unknown key outright, taking the operator's allowlist with
    #: it. No environment override, for :attr:`slots`' second reason: a JSON
    #: mapping in an env var is a footgun.
    matrix: Optional[dict] = None

    @property
    def secret_path(self) -> Path:
        """Where the shared secret lives."""
        if self.secret_file:
            return Path(self.secret_file).expanduser()
        return platform_dir() / SECRET_FILENAME

    @property
    def mirror_path(self) -> Path:
        """Where the host-side work-repo mirror clone lives (spec D24)."""
        if self.work_repo_mirror:
            return Path(self.work_repo_mirror).expanduser()
        return platform_dir() / MIRROR_DIRNAME

    @property
    def is_loopback(self) -> bool:
        return self.bind_address in ("127.0.0.1", "::1", "localhost")

    @property
    def base_url(self) -> str:
        host = f"[{self.bind_address}]" if ":" in self.bind_address else self.bind_address
        return f"http://{host}:{self.bind_port}"

    def to_dict(self) -> dict:
        return asdict(self)


def config_path() -> Path:
    return snapshot_path(CONFIG_FILENAME)


def _known_fields() -> set:
    return {f.name for f in fields(PlatformConfig)}


def _env_str(name: str) -> Optional[str]:
    value = os.environ.get(name, "").strip()
    return value or None


def _env_int(name: str) -> Optional[int]:
    """Read an integer env var, raising on a bad value.

    Deliberately *not* the "warn and fall back" treatment
    ``slack_chat.sessions`` gives its tuning knobs: a mistyped port would bind
    somewhere other than where the operator believes, and quietly-wrong is the
    worst failure mode a bind address can have.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not an integer")


def _validate(config: PlatformConfig) -> PlatformConfig:
    if not isinstance(config.bind_address, str) or not config.bind_address.strip():
        raise ConfigError("bind_address must be a non-empty string")
    if not isinstance(config.bind_port, int) or isinstance(config.bind_port, bool):
        raise ConfigError(f"bind_port must be an integer, got {config.bind_port!r}")
    if not 1 <= config.bind_port <= 65535:
        raise ConfigError(f"bind_port {config.bind_port} is outside 1-65535")
    for name in (
        "work_repo_pull_interval",
        "max_concurrent_sessions",
        "max_followup_rounds",
    ):
        value = getattr(config, name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ConfigError(f"{name} must be a positive integer, got {value!r}")
    # Checked apart from the loop above because 0 is meaningful here: it is how
    # an operator turns a path off.
    for name in ("stall_idle_seconds", "stall_backstop_seconds"):
        value = getattr(config, name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConfigError(
                f"{name} must be a non-negative integer (0 disables it), "
                f"got {value!r}"
            )
    # Refused rather than reordered: a backstop that fired first would make the
    # precise paths unreachable, which nobody means to configure.
    if 0 < config.stall_backstop_seconds < config.stall_idle_seconds:
        raise ConfigError(
            "stall_backstop_seconds "
            f"({config.stall_backstop_seconds}) must not be below "
            f"stall_idle_seconds ({config.stall_idle_seconds}): the backstop "
            "fires on silence alone and is meant to come after the precise "
            "paths, not instead of them (0 disables it)"
        )
    # Refused rather than ignored, and case-folded rather than refused for case:
    # a misspelled forge would silently fall back to detection and leave the
    # operator looking at the links they set this to change.
    forge = config.work_repo_forge
    normalized = (
        (forge.strip().lower() or None) if isinstance(forge, str) else forge
    )
    if normalized is not None and normalized not in WORK_REPO_FORGE_VALUES:
        raise ConfigError(
            f"work_repo_forge must be one of {', '.join(WORK_REPO_FORGE_VALUES)} "
            f"(or unset to detect it from the work repo's host), got {forge!r}"
        )
    if normalized != forge:
        config = replace(config, work_repo_forge=normalized)
    for field_name in (field for field, _ in ASSISTANT_SETTING_KEYS.values()):
        value = getattr(config, field_name)
        cleaned = _assistant_value(value, field=field_name)
        if cleaned != value:
            config = replace(config, **{field_name: cleaned})
    slots = _slots_value(config.slots)
    if slots != config.slots:
        config = replace(config, slots=slots)
    matrix = _matrix_value(config.matrix)
    if matrix != config.matrix:
        config = replace(config, matrix=matrix)
    for field_name in INT_SETTINGS:
        usable = _int_setting_value(getattr(config, field_name), field=field_name)
        if usable != getattr(config, field_name):
            config = replace(config, **{field_name: usable})
    return config


def _slots_value(value: object) -> tuple:
    """The service-slot entries as a tuple, or ``()`` with a warning.

    The :func:`_assistant_value` house rule rather than :func:`_validate`'s
    refusals: ``slots`` is read long after boot, and a daemon that refuses to
    start over a mistyped slot cannot be used to fix the slot. Only the
    container is checked here; what an entry must look like is
    :mod:`lmer_platform.slots`' rule.
    """
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(value)
    logger.warning(
        "platform_config_unusable field=slots value=%r — expected a list of "
        "slot entries; no service slots are declared", value,
    )
    return ()


def _matrix_value(value: object) -> Optional[dict]:
    """The Matrix bridge's mapping, or ``None`` with a warning.

    :func:`_slots_value`'s house rule, for :func:`_slots_value`'s reason: this
    daemon does not read the mapping, and refusing to boot over the bridge's
    config would take the UI down with it. What is *inside* the mapping is
    :mod:`matrix_bridge.config`'s to refuse — loudly, because the bridge has no
    UI to be fixed through and an allowlist is the only authorization it has.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    logger.warning(
        "platform_config_unusable field=matrix value=%r — expected a mapping "
        "of the bridge's settings; the Matrix bridge will refuse to start", value,
    )
    return None


def _int_setting_value(value: object, *, field: str) -> int:
    """A usable value for one integer setting, or its default with a warning.

    :func:`_assistant_value`'s posture: this resolves inside ``load()``, so
    refusing would be a host that will not boot over a number whose only effect
    is how often a reminder is spooled or a line is typed. An *explicit* write
    gets the opposite treatment (:func:`validate_int_setting`).
    """
    rule = INT_SETTINGS[field]
    coerced = _coerced_int(value)
    reason = _int_setting_reason(coerced, field=field)
    if reason is None:
        return int(coerced)
    unit = rule.singular_unit if rule.default == 1 else rule.unit
    logger.warning(
        "%s value=%r — %s; using the default of %d %s instead",
        rule.log_key, value, reason, rule.default, unit,
    )
    return rule.default


def _coerced_int(value: object) -> object:
    """*value* as an int when it is one written down, else unchanged.

    Every layer that carries these values can carry text — an export, a form
    field, a hand-edited file. Anything that is not a number keeps its own
    refusal below.
    """
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return value
    return value


def _int_setting_reason(value: object, *, field: str) -> Optional[str]:
    """Why *value* cannot be *field* — ``None`` when it can.

    One rule set behind both postures, as :func:`_unusable_reason` is for the
    launch settings. Call it on a coerced value.
    """
    rule = INT_SETTINGS[field]
    if isinstance(value, bool) or not isinstance(value, int):
        return f"it is not a whole number ({type(value).__name__})"
    if value < rule.minimum:
        return rule.floor_reason
    if rule.maximum is not None and value > rule.maximum:
        return rule.ceiling_reason
    return None


def validate_int_setting(field: str, value: object) -> int:
    """A usable value for *field* from an explicit ask, or a refusal.

    The caller is attached and asking for exactly this value, so an unusable one
    is a :class:`ConfigError` for the route to answer 400 with: a value silently
    normalised to the default is a setting the operator believes they changed.
    """
    coerced = _coerced_int(value)
    reason = _int_setting_reason(coerced, field=field)
    if reason is not None:
        raise ConfigError(f"{field} is unusable ({value!r}): {reason}")
    return int(coerced)


def validate_checkin_window(value: object) -> int:
    """:func:`validate_int_setting` for the check-in window.

    Kept as its own name because the route that answers 400 with it reads better
    for saying which setting it is validating, and because it was the first of
    these and is what callers already import.
    """
    return validate_int_setting("checkin_window_seconds", value)


def _assistant_value(value: object, *, field: str) -> Optional[str]:
    """A usable assistant launch setting, or ``None`` with a warning.

    The ``_resolve_pids_limit`` house rule rather than :func:`_validate`'s
    refusals, and the difference is what a bad value would cost: a mistyped
    bind port must not boot a daemon somewhere the operator does not believe it
    is, while a mistyped assistant setting read at *start time* would otherwise
    take down the one session the operator talks to — refusing here would turn
    a typo in ``config.json`` into an assistant that cannot start at all. So
    anything that is not usable text warns, names the field, and reads as
    unset: the assistant starts the way it did before the setting existed,
    which is a session the operator can be told about the typo through.

    "Usable" is deliberately the same set of shape rules
    :meth:`lmer_platform.spawn.SpawnRequest.validate` enforces for these four
    fields (:func:`_unusable_reason`), and this is the ONE definition of it for
    the standing layers — ``load()``, :func:`assistant_settings` and the start
    path all resolve through here. The rules living in one place is the fix for
    a real bug: an ``agents`` that named nobody once passed every hand-copied
    validation layer and was refused only inside the spawn, as a 500, on every
    start — a stored value that made the assistant unstartable, which is
    exactly what this posture exists to prevent.

    What is *not* checked is the value's meaning — a model or harness name is
    handed to ``lmer`` verbatim, per :attr:`PlatformConfig.assistant_model`.
    """
    if value is None:
        return None
    reason = _unusable_reason(field, value)
    if reason is not None:
        logger.warning(
            "platform_assistant_setting_invalid field=%s value=%r — %s; "
            "ignoring it and resolving as if this layer were unset",
            field, value, reason,
        )
        return None
    return str(value).strip()


#: Ceiling on one launch-setting value. Names are a few dozen characters; the
#: bound exists because each value becomes one argv token, and a token past the
#: kernel's ``MAX_ARG_STRLEN`` (~128 KiB) makes ``Popen`` raise ``E2BIG`` — a
#: spawn that cannot even fail as a session, surfacing as a 500 on every start
#: for as long as the value is stored. Far above any name, far below the cliff.
MAX_ASSISTANT_SETTING_CHARS = 4096


def _authority_names(kind: str) -> Optional[set]:
    """The names this host would accept for *kind* — or ``None`` for "cannot say".

    The authorities already exist host-side and are the ones the spawned
    ``lmer`` itself consults: ``known_harnesses()`` (built-ins plus
    ``~/.lmer/harnesses``) and ``load_presets()`` (``LMER_PRESETS_FILE``, read
    from this daemon's environment — which is the environment the spawn
    inherits). An *empty* preset catalog is an authoritative answer for the
    ``preset`` field, not an unavailable one: ``--preset`` has no fallback
    route, so with the feature off ``lmer`` exits 2 for every preset name.
    (``agents`` is deliberately NOT answered from this catalog — see
    :func:`_agents_selection_reason` for the fallback that makes an empty
    catalog mean something different there.)

    The user-harness view is **refreshed** before the harness read
    (:func:`lmer_cli.user_harnesses.refresh_user_harnesses`): the load cache
    lives for the process — including a cached "no directory" from before a
    first drop-in was installed — while every spawned ``lmer`` is a fresh
    process that would see the new harness. A long-lived daemon refusing a
    name every fresh child accepts is this check diverging from the authority
    it quotes, which is the one failure it must not have. A refresh rather
    than a cache *clear*, deliberately: the clear raced concurrent loads in
    the server's threadpool (and the race failed open, downgrading the check
    at exactly the wrong moment), and it re-emitted every malformed drop-in's
    warning per validation — the refresh assigns in place and the loader
    dedups its warnings, so neither failure exists to have.

    ``None`` — the authority itself failing — downgrades the check to
    shape-only rather than refusing everything, the same "warns only when it
    has some host-side view" posture ``assistant._require_taskdef`` takes. And
    the same residual applies: this answers for *this* package's checkout,
    while the process spawned is ``config.lmer_bin`` — point that at a
    different tree and the two can disagree, as everything imported from
    ``lmer_cli`` already can.
    """
    try:
        if kind == "harness":
            from lmer_cli.harness import known_harnesses
            from lmer_cli.user_harnesses import refresh_user_harnesses

            refresh_user_harnesses()
            return set(known_harnesses())
        from lmer_cli.presets import load_presets

        return set(load_presets())
    except Exception as exc:  # noqa: BLE001 - validation must not break a start
        logger.warning(
            "platform_assistant_authority_unavailable kind=%s error=%s — "
            "launch-setting names are checked for shape only", kind, exc,
        )
        return None


def _unknown_name_reason(kind: str, names: list) -> Optional[str]:
    """Why these *names* would make ``lmer`` exit 2 — ``None`` when they would not.

    Membership is checked the way the child checks it, which differs by kind:
    harness resolution lowercases its input (``resolve_harness_selection``, so
    ``LMER_HARNESS=Codex`` works and ``Codex`` must pass here too), while
    preset lookup is exact-case (``presets.get`` — a case-variant is a miss
    the child refuses).
    """
    known = _authority_names(kind)
    if known is None:
        return None
    if kind == "harness":
        missing = sorted(
            name for name in names if name.lower() not in known
        )
    else:
        missing = sorted(name for name in names if name not in known)
    if not missing:
        return None
    if known:
        catalog = f"this host knows: {', '.join(sorted(known))}"
    elif kind == "preset":
        catalog = (
            "this host has no presets at all (LMER_PRESETS_FILE is unset, "
            "empty or unreadable)"
        )
    else:
        catalog = f"this host knows no {kind} names at all"
    return (
        f"no {kind} named {', '.join(repr(name) for name in missing)} exists "
        f"on this host — `lmer` would exit 2 after the session was already "
        f"registered; {catalog}"
    )


def _agents_selection_reason(selection: str) -> Optional[str]:
    """Why ``lmer`` would refuse this ``--agents`` selection — ``None`` if it won't.

    Asked of ``resolve_agent_presets`` itself rather than re-derived as
    catalog membership, because the resolver accepts more than the catalog:
    a member matching no preset falls back to the **model route**
    (``harness_for_model`` — ``fable`` resolves as a synthesized model agent),
    which is the documented form ``--agents=fable,sol-review`` and, on a host
    with no presets file, the only usable form of ``--agents`` at all. A
    membership check here once refused exactly that and silently stripped a
    working fan-out from the standing layers. The resolver is side-effect-free
    and also encodes the refusals a re-derivation would miss — the
    case-variant-of-a-preset trap, and unknown-name-with-catalog wording.

    The user-harness view is refreshed first, exactly as in
    :func:`_authority_names` and for the same divergence: the resolver reaches
    ``known_harnesses()`` itself — a preset's ``--harness`` arg is validated
    there, and ``harness_for_model`` consults user ``model_hints`` — so
    without the refresh this path answered from whatever a previous read had
    cached while the harness field beside it answered fresh.

    The resolver's *warnings* are logged rather than dropped, because one of
    them is the signal that makes the model route survivable: a typo'd preset
    name containing a model word ("sonnet-reviw") resolves via the model route
    with no error, and only fails hours later inside the session — the child
    prints this note where its log can be read, and the daemon holding the
    same note must not discard it.

    Failure of the resolver itself downgrades to "no answer" (the caller's
    shape checks have already run), the same posture as
    :func:`_authority_names`.
    """
    try:
        from lmer_cli.presets import load_presets, resolve_agent_presets
        from lmer_cli.user_harnesses import refresh_user_harnesses

        refresh_user_harnesses()
        _resolved, warnings, error = resolve_agent_presets(
            selection, load_presets()
        )
    except Exception as exc:  # noqa: BLE001 - validation must not break a start
        logger.warning(
            "platform_assistant_authority_unavailable kind=agents error=%s — "
            "the agents selection is checked for shape only", exc,
        )
        return None
    for note in warnings:
        logger.warning(
            "platform_assistant_agents_note selection=%r: %s", selection, note
        )
    if error is None:
        return None
    return (
        f"`lmer` would refuse this agents selection and exit 2 after the "
        f"session was already registered: {error}"
    )


def _unusable_reason(field: str, value: object) -> Optional[str]:
    """Why *value* cannot be a launch setting for *field* — ``None`` when it can.

    One rule set, shared by the two postures built on top: the standing layers
    warn and fall through (:func:`_assistant_value`), an explicit ask is
    refused with the reason (:func:`validate_assistant_override`).

    Two kinds of rule, and both matter. The **shape** rules mirror
    ``spawn._reject_option_value`` and ``spawn._reject_empty_agent_selection``:
    a value those would refuse becomes a session that exists on paper and exits
    2 before its first line of output. The **name** rules ask the host-side
    authorities the spawned ``lmer`` itself consults (:func:`_authority_names`)
    — because a harness, preset or agent name the host does not know passes
    every shape check, the spawn *succeeds*, and the child exits 2 after the
    route already answered 200: a one-letter typo persisted through the
    settings surface would otherwise stop the incumbent, crash-loop the
    supervisor's respawns, and take down the one session the operator talks to,
    all behind successful-looking responses. ``model`` deliberately gets no
    name rule — no host-side authority exists (the harness is the only thing
    that knows its own ids), so the verbatim stance stays.

    *field* accepts both spellings — the setting key (``agents``) and the
    config field (``assistant_agents``) — so the callers do not translate.
    """
    if not isinstance(value, str) or not value.strip():
        return "not non-empty text"
    text = value.strip()
    if text.startswith("-"):
        return (
            "it begins with a dash, which `lmer`'s parser reads as the next "
            "option rather than a name"
        )
    if len(text) > MAX_ASSISTANT_SETTING_CHARS:
        return (
            f"it is {len(text)} characters long, over the "
            f"{MAX_ASSISTANT_SETTING_CHARS} limit — a launch setting is one "
            "argv token, and an oversized one breaks the spawn itself rather "
            "than the session"
        )
    if field in ("harness", "assistant_harness"):
        return _unknown_name_reason("harness", [text])
    if field in ("preset", "assistant_preset"):
        return _unknown_name_reason("preset", [text])
    if field in ("agents", "assistant_agents"):
        if not [name for name in text.split(",") if name.strip()]:
            return (
                "it names no agent — the selection is split on commas and "
                "blank entries are dropped, and `lmer` refuses a fan-out that "
                "spawns nobody"
            )
        # NOT the preset catalog: agents members that match no preset take
        # the model route, so the child's own resolver is the only honest
        # authority for a selection.
        return _agents_selection_reason(text)
    return None


def validate_assistant_override(key: str, value: object) -> str:
    """A usable *explicit* launch-setting value, or a refusal naming *key*.

    The other posture over :func:`_unusable_reason`'s one rule set: the caller
    is attached and asking for exactly this value, so an unusable one is a
    :class:`ConfigError` to answer with (the routes translate it to a 400)
    rather than something to quietly start without — starting *something other
    than what was asked for* is the failure the settings surface exists to
    avoid. Assumes *key* is one of :data:`ASSISTANT_SETTING_KEYS`; unknown keys
    are each surface's own refusal, since each names its own allowed set.
    """
    reason = _unusable_reason(key, value)
    if reason is not None:
        raise ConfigError(f"{key} is unusable ({value!r}): {reason}")
    return str(value).strip()


def load(overrides: Optional[dict] = None) -> PlatformConfig:
    """Resolve configuration: *overrides* > environment > ``config.json`` > defaults.

    A corrupt ``config.json`` is not fatal — ``store.read_json`` has already moved
    it aside, and starting on defaults beats refusing to start at all, since a
    daemon that will not boot cannot be reconfigured through its own UI. The
    failure is logged loudly instead.

    Unknown keys in the file are ignored rather than rejected, so a config
    written by a newer version stays loadable (its ``schema`` guard in
    ``store.read_json`` is what catches genuine incompatibility).
    """
    known = _known_fields()
    values: dict[str, Any] = {}

    try:
        stored = read_json(config_path())
    except StoreError as exc:
        logger.error("platform_config_unreadable error=%s — using defaults", exc)
        stored = None
    if stored:
        values.update({k: v for k, v in stored.items() if k in known})

    env_values = {
        "bind_address": _env_str(ENV_BIND_ADDRESS),
        "bind_port": _env_int(ENV_BIND_PORT),
        "secret_file": _env_str(ENV_SECRET_FILE),
        "work_repo_mirror": _env_str(ENV_WORK_REPO_MIRROR),
        "work_repo_forge": _env_str(ENV_WORK_REPO_FORGE),
        "stall_idle_seconds": _env_int(ENV_STALL_IDLE_SECONDS),
        "stall_backstop_seconds": _env_int(ENV_STALL_BACKSTOP_SECONDS),
        # Not `_env_int`: it raises, which is right for a bind port and wrong
        # for a reminder interval. Cleaned here rather than in ``_validate`` so
        # an unusable export costs its own layer and lets ``config.json`` show
        # through — which is what :func:`checkin_settings` and
        # :func:`nudge_settings` answer, and the two readers must not disagree.
        **{
            field: _layer_int(_env_str(env_var), layer="env", field=field)
            for field, env_var in (
                *CHECKIN_SETTING_KEYS.values(), *NUDGE_SETTING_KEYS.values(),
            )
        },
        **{
            field: _env_str(env_var)
            for field, env_var in ASSISTANT_SETTING_KEYS.values()
        },
    }
    values.update({k: v for k, v in env_values.items() if v is not None})

    # The work repo URL has no platform-scoped variable: it is the same repo the
    # rest of lmer already knows about, so the existing var is the fallback when
    # config.json does not name one.
    if not values.get("work_repo_url"):
        env_url = _env_str(ENV_WORK_REPO)
        if env_url:
            values["work_repo_url"] = env_url

    if os.environ.get(ENV_AUTONOMOUS, "").strip():
        values["autonomous_default"] = get_bool_env(ENV_AUTONOMOUS)

    if overrides:
        unknown = set(overrides) - known
        if unknown:
            raise ConfigError(
                f"unknown config override(s): {', '.join(sorted(unknown))}"
            )
        values.update({k: v for k, v in overrides.items() if v is not None})

    config = _validate(PlatformConfig(**values))
    _seed_gitlab_token_host(config.work_repo_url)
    return config


def _seed_gitlab_token_host(work_repo_url: Optional[str]) -> None:
    """Default the generic token's issuing host to the resolved work repo.

    ``lmer_cli.tokens`` scopes a generic ``GITLAB_TOKEN`` to the host that
    issued it, and its default for that host reads ``LMER_WORK_REPO`` — which
    a platform deployment configured through ``config.json`` legitimately never
    exports, so the token would be refused for the work repo's own host
    (issue #161 review finding). Seeding the variable here, at the single point
    where the work-repo URL is resolved, also carries the answer to spawned
    children through the environment.

    ``setdefault``: an explicit ``LMER_GITLAB_TOKEN_HOST`` is the operator's
    decision and always wins, and repeated :func:`load` calls stay idempotent.
    ``LMER_WORK_REPO`` itself is deliberately NOT seeded — other code branches
    on whether it is set at all.
    """
    host = _host_from_git_url(work_repo_url or "")
    if not host:
        return
    os.environ.setdefault("LMER_GITLAB_TOKEN_HOST", host)


def save(config: PlatformConfig) -> None:
    """Persist configuration, writing only known fields.

    Writing the dataclass rather than a caller-supplied mapping is what keeps the
    secret out of this file structurally: there is no field for it, so there is
    no way to pass one through.
    """
    if not isinstance(config, PlatformConfig):
        raise ConfigError(
            f"save expects a PlatformConfig, got {type(config).__name__}"
        )
    write_json(config_path(), config.to_dict())


@dataclass(frozen=True)
class AssistantSetting:
    """One effective assistant launch setting, and where it came from.

    ``source`` names which layer of the chain produced the value — ``"env"``,
    ``"config.json"`` or ``"default"`` — because the chain's one wrinkle is that
    an export *shadows* what a settings screen wrote (module docstring), and a
    screen that appears to have no effect is a bad afternoon. The per-call
    override layer is deliberately not a source here: it exists only inside a
    single start request and is never part of the standing answer this
    describes.

    ``stored`` is the ``config.json`` layer's own text whichever layer won —
    what a settings screen has to edit. With an export standing in front of the
    file the two differ, and a screen prefilled from ``value`` would show the
    export's text in a field that writes the file: saving it would copy the env
    value into ``config.json``, which is exactly the baking-in
    :func:`update_stored` exists to prevent. It is the **raw** stored text,
    deliberately not the cleaned value the resolution uses: an unusable stored
    value served as ``null`` would prefill the field empty, make clearing it a
    no-op diff, and leave a value nothing can see warning on every start — the
    screen has to show what is actually in the file to be able to remove it.

    Since issue #244 one setting served through these routes is not a launch flag
    — the check-in window (:func:`checkin_settings`). It shares this shape because
    every word above is as true of it, which is also why ``value`` is not
    annotated ``str``.
    """

    value: Optional[object] = None
    source: str = "default"
    stored: Optional[object] = None

    def to_dict(self) -> dict:
        return {"value": self.value, "source": self.source, "stored": self.stored}


def assistant_settings() -> dict:
    """The assistant's effective launch settings, one :class:`AssistantSetting` per key.

    Read **fresh** — the environment and ``config.json`` as they are *now* —
    rather than off a :class:`PlatformConfig` a caller is holding, and that is
    the point of the function: the daemon loads its config once at boot, while
    these settings are honoured at every assistant start and rotate (issue
    #234), so a value persisted through ``POST /api/assistant/config`` must
    reach the next incarnation without a daemon restart. Nothing else in the
    config gets this treatment; a bind address that moved under a running
    server would not be a feature.

    Unusable values fall through with a warning (:func:`_assistant_value`) to
    the next layer down — an export of ``"-x"`` resolves to what ``config.json``
    says, and a typo there to the default; either way it costs that one layer,
    never the assistant. That the *effective* answer here goes through the same
    cleaner the start path reads is a promise, not a convenience: this is what
    the settings screen shows as "how the next incarnation will be run", and a
    screen reporting a value the start would have discarded is the screen lying.
    A corrupt ``config.json`` has already been moved aside by ``store.read_json``
    and reads as empty, the same tolerance :func:`load` has.
    """
    try:
        stored = read_json(config_path())
    except StoreError as exc:
        logger.error(
            "platform_config_unreadable error=%s — assistant settings resolve "
            "from the environment and defaults only", exc,
        )
        stored = None
    if not isinstance(stored, dict):
        stored = {}
    resolved = {}
    for key, (field, env_var) in ASSISTANT_SETTING_KEYS.items():
        raw = stored.get(field)
        # The file's own content, usable or not — what the settings screen
        # edits (see the dataclass); the cleaned value below is what
        # resolution uses. A non-string a human hand-wrote into the JSON (a
        # list where the schema wants a comma-string, a bare number) is
        # serialized rather than dropped: served as null it would prefill the
        # field empty, make clearing a no-op diff, and warn on every resolve
        # with nothing on any screen to remove.
        if isinstance(raw, str):
            stored_text = raw.strip() or None
        elif raw is None:
            stored_text = None
        else:
            stored_text = json.dumps(raw)
        file_value = _assistant_value(raw, field=field)
        env_value = _assistant_value(_env_str(env_var), field=field)
        if env_value is not None:
            resolved[key] = AssistantSetting(
                value=env_value, source="env", stored=stored_text
            )
            continue
        if file_value is not None:
            resolved[key] = AssistantSetting(
                value=file_value, source="config.json", stored=stored_text
            )
            continue
        resolved[key] = AssistantSetting(stored=stored_text)
    return resolved


def checkin_settings() -> dict:
    """The check-in group's effective settings, one per :data:`CHECKIN_SETTING_KEYS`.

    :func:`assistant_settings`' sibling, read **fresh** for a stronger version of
    its reason: the detector reads this every tick, so an operator who widens the
    window expects the next sweep to use it, not the next restart.
    """
    return _int_group_settings(CHECKIN_SETTING_KEYS, group="check-in")


def nudge_settings() -> dict:
    """The nudge group's effective settings, one per :data:`NUDGE_SETTING_KEYS`.

    :func:`checkin_settings`' sibling on the same terms and for the same reason:
    the detector reads both every tick, so a threshold an operator raises applies
    to the next tick rather than the next restart.
    """
    return _int_group_settings(NUDGE_SETTING_KEYS, group="nudge")


def _int_group_settings(keys: dict, *, group: str) -> dict:
    """One integer setting group, resolved layer by layer.

    An unusable layer falls through with a warning, so the effective answer is
    the one the tick will read — a screen reporting a value the daemon would have
    discarded is the screen lying.
    """
    try:
        stored = read_json(config_path())
    except StoreError as exc:
        logger.error(
            "platform_config_unreadable error=%s — %s settings resolve "
            "from the environment and defaults only", exc, group,
        )
        stored = None
    if not isinstance(stored, dict):
        stored = {}
    resolved = {}
    for key, (field, env_var) in keys.items():
        raw = stored.get(field)
        env_value = _layer_int(_env_str(env_var), layer="env", field=field)
        file_value = _layer_int(raw, layer="config.json", field=field)
        if env_value is not None:
            resolved[key] = AssistantSetting(
                value=env_value, source="env", stored=raw
            )
            continue
        if file_value is not None:
            resolved[key] = AssistantSetting(
                value=file_value, source="config.json", stored=raw
            )
            continue
        resolved[key] = AssistantSetting(
            value=INT_SETTINGS[field].default, source="default", stored=raw
        )
    return resolved


#: Bad ``(field, layer, value)`` triples already warned about. This resolution
#: runs on every fleet read, so one typo in an export would otherwise warn every
#: few seconds for the life of the daemon. Keyed on the value too, so a
#: corrected-then-broken-again one is announced again.
_WARNED_INT_SETTINGS: set = set()


def _layer_int(raw: object, *, layer: str, field: str) -> Optional[int]:
    """One layer's value for *field*, or ``None`` when it says nothing usable.

    ``_assistant_value``'s shape for the integer settings: a bad value costs
    *that layer*, so an export of ``"soon"`` resolves to what ``config.json``
    says.
    """
    if raw is None:
        return None
    coerced = _coerced_int(raw)
    reason = _int_setting_reason(coerced, field=field)
    if reason is None:
        return int(coerced)
    seen = (field, layer, repr(raw))
    if seen not in _WARNED_INT_SETTINGS:
        _WARNED_INT_SETTINGS.add(seen)
        logger.warning(
            "%s layer=%s value=%r — %s; resolving as if this layer were unset "
            "(said once per value)",
            INT_SETTINGS[field].log_key, layer, raw, reason,
        )
    return None


#: Serializes :func:`update_stored`'s read-modify-write: the API handlers run in
#: a threadpool, and two settings writes interleaving would silently drop one
#: operator's change. Per-write atomicity is ``store.write_json``'s; this is the
#: consistency across the cycle, the same distinction ``assistant._LOCK`` states.
_STORED_LOCK = threading.Lock()


def update_stored(changes: dict) -> dict:
    """Persist *changes* into ``config.json`` — the stored layer and only it.

    Not :func:`save`, deliberately: ``save`` writes a whole resolved
    :class:`PlatformConfig`, and a resolved config has the environment baked
    into it — persisting one from a daemon whose operator has
    ``LMER_PLATFORM_ASSISTANT_MODEL`` exported would copy the export into the
    file, where it would outlive the export and read as a choice nobody made.
    This edits the stored mapping itself: unknown keys in the file survive
    untouched (the same tolerance :func:`load` has for a file written by a
    newer build), a value of ``None`` removes the key so the layer below shows
    through, and everything else is stored as given.

    The merged file is validated as a whole before it is written — built into a
    :class:`PlatformConfig` exactly as :func:`load` would build it — so a write
    that would make the next boot refuse the file is refused here instead, with
    the caller still attached to hear it.

    Returns the stored mapping as written.
    """
    known = _known_fields()
    unknown = set(changes) - known
    if unknown:
        raise ConfigError(
            f"unknown config field(s): {', '.join(sorted(unknown))}"
        )
    with _STORED_LOCK:
        try:
            stored = read_json(config_path())
        except StoreError as exc:
            logger.error(
                "platform_config_unreadable error=%s — the update starts from "
                "an empty stored config", exc,
            )
            stored = None
        current = dict(stored) if isinstance(stored, dict) else {}
        # The store's own bookkeeping, not configuration: ``write_json`` stamps
        # both on every write, so carrying the old pair forward would only
        # persist a stale ``updated``.
        current.pop("schema", None)
        current.pop("updated", None)
        for field, value in changes.items():
            if value is None:
                current.pop(field, None)
            else:
                current[field] = value
        _validate(PlatformConfig(**{
            k: v for k, v in current.items() if k in known
        }))
        write_json(config_path(), current)
    return current


def configured_repo_url() -> Optional[str]:
    """The repository URL this daemon was started against, credentials removed.

    One definition for two readers that must agree. The spawn path files a run
    under this when the caller named no repository
    (:func:`lmer_platform.spawn._repo_urls`), and ``GET /api/spawn-options`` offers
    the same string to the run dialog so the field's prefill *is* what leaving it
    blank would have used — a prefill that differed from the fallback would be a
    form lying about its own default.

    Scrubbed on the way out, and that is not defensive: ``lmer`` bakes a host
    token into the URL it hands the container
    (:func:`lmer_cli.tokens._inject_gitlab_token_if_available`), so an exported
    ``LMER_REPO_URL`` routinely reads ``https://oauth2:<token>@…`` — and both
    readers put the value somewhere it must not be. The spawn writes it into the
    session's registry entry and the tracked index, files that get pasted into
    tickets; the route puts it in an HTTP response body and from there into the
    DOM. Stripping the credential costs nothing that matters:
    ``cli._parse_repo_url`` reads the host out of the netloc *after* the ``@``, so
    the run identity derived from it is unchanged.
    """
    return _scrub_credentials(_env_str(ENV_REPO_URL) or "") or None


def _warn_if_permissive(path: Path) -> None:
    """Log when a secret file is readable beyond its owner."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        logger.warning(
            "platform_secret_permissive path=%s mode=%o — should be 0600",
            path, stat.S_IMODE(mode),
        )


def read_secret(config: Optional[PlatformConfig] = None) -> Optional[str]:
    """Read the shared secret, or ``None`` when it has not been created yet.

    Whitespace is stripped, so a secret file with a trailing newline (which is
    what every editor and ``echo`` produces) works.
    """
    path = (config or PlatformConfig()).secret_path
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigError(f"cannot read secret file {path} ({exc})")
    _warn_if_permissive(path)
    return raw.strip() or None


def ensure_secret(config: Optional[PlatformConfig] = None) -> str:
    """Return the shared secret, generating a strong one on first run.

    Created with mode 0600 before anything is written to it — the file is opened
    with the restrictive mode rather than chmod'ed afterwards, so the secret is
    never briefly world-readable on disk.
    """
    resolved = config or PlatformConfig()
    existing = read_secret(resolved)
    if existing:
        return existing

    path = resolved.secret_path
    token = secrets.token_urlsafe(32)
    try:
        # Through the store so the platform root is 0700 from first boot — this
        # is the earliest creator of the tree, ahead of any snapshot (T93).
        ensure_state_dir(path.parent)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, (token + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(path, 0o600)  # in case the file pre-existed with looser bits
    except OSError as exc:
        raise ConfigError(f"cannot create secret file {path} ({exc})")
    logger.info("platform_secret_created path=%s", path)
    return token


#: Memo for :func:`active_secret`, in two levels because the answer depends on
#: two files: ``config.json`` names where the secret lives, and the secret file
#: holds it. Each level is keyed on a stat fingerprint, so a hand-edited config
#: or a rewritten secret is picked up on the next call and nothing has to be
#: invalidated by hand — including between tests, which repoint the platform
#: state dir per test.
_SECRET_PATH_MEMO: tuple = (None, None)
_SECRET_MEMO: tuple = (None, None)


def _stat_key(path: Path):
    """A cheap fingerprint of a file, or ``None`` when there is no file."""
    try:
        info = path.stat()
    except OSError:
        return None
    return (str(path), info.st_mtime_ns, info.st_size)


def _resolved_secret_path() -> Path:
    """Where the secret lives, resolved as :func:`load` would, memoized.

    Same precedence as everything else — environment over ``config.json`` over
    the derived default — but reached without building a whole
    :class:`PlatformConfig`, because :func:`active_secret` is called from the
    transcript scrub once per string and a JSON read per string is not a thing
    that can happen.
    """
    global _SECRET_PATH_MEMO
    config_file = config_path()
    key = (_env_str(ENV_SECRET_FILE), str(config_file), _stat_key(config_file))
    cached_key, cached = _SECRET_PATH_MEMO
    if cached_key == key and cached is not None:
        return cached

    override = key[0]
    if override:
        resolved = Path(override).expanduser()
    else:
        try:
            stored = read_json(config_file)
        except StoreError:
            stored = None
        named = (stored or {}).get("secret_file") if isinstance(stored, dict) else None
        resolved = (
            Path(named).expanduser() if isinstance(named, str) and named
            else platform_dir() / SECRET_FILENAME
        )
    _SECRET_PATH_MEMO = (key, resolved)
    return resolved


def active_secret() -> Optional[str]:
    """The shared secret this host serves with, or ``None`` when there is none.

    :func:`read_secret` with two properties its callers do not need and the
    transcript scrub does: it resolves the path itself (so a host that moved the
    secret with :data:`ENV_SECRET_FILE` is still covered), and it never raises —
    an unreadable secret must not turn the chat view into a 500.

    Memoized on the file's stat, which costs two ``stat`` calls per call and no
    reads. That matters because :func:`lmer_platform.transcripts._scrub` calls
    this once for every string it emits, thousands of times for one page of a
    long conversation.

    One limit worth stating: this is the secret **on disk right now**. A daemon
    keeps serving with the one it read at startup, so between a rotation and a
    restart the scrub covers the new value and not the one actually in use.
    """
    global _SECRET_MEMO
    path = _resolved_secret_path()
    key = _stat_key(path)
    if key is None:
        return None
    cached_key, cached = _SECRET_MEMO
    if cached_key == key:
        return cached
    try:
        secret = path.read_text(encoding="utf-8").strip() or None
    except OSError as exc:
        logger.warning(
            "platform_secret_unreadable path=%s error=%s — anything that masks "
            "it (the transcript scrub) cannot do so on this host", path, exc,
        )
        return None
    _SECRET_MEMO = (key, secret)
    return secret


#: Memo for :func:`active_assistant_credential`, keyed on the file's stat exactly
#: as :data:`_SECRET_MEMO` is. One level rather than two: this file's location is
#: not configurable, so there is no second file whose change could move it.
_ASSISTANT_CREDENTIAL_MEMO: tuple = (None, None)


def assistant_credential_path() -> Path:
    """Where the running assistant's minted credential lives."""
    return platform_dir() / ASSISTANT_CREDENTIAL_FILENAME


def mint_assistant_credential() -> str:
    """Generate the next incarnation's credential, replacing any current one.

    Minting rather than handing over the shared secret is what makes the
    assistant's calls *attributable*: before issue #244 every request looked like
    the operator's, so "has anyone checked this run?" had no answer.

    Two properties, and they are why this is not a second shared secret:

    - **Per incarnation.** A fresh value on every start and rotate, revoked with
      the session — where the shared secret in a stopped assistant's
      ``assistant.env`` stays valid as long as that file exists.
    - **Not a scope.** It opens exactly what the shared secret opens. It adds
      identity, not a boundary.

    Created 0600 by ``os.open`` rather than chmod'ed after, like
    :func:`ensure_secret`: a credential that is world-readable for a millisecond
    has leaked. Raises :class:`ConfigError` for the caller to fall back on.
    """
    path = assistant_credential_path()
    credential = secrets.token_urlsafe(32)
    try:
        ensure_state_dir(path.parent)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, (credential + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(path, 0o600)  # in case the file pre-existed with looser bits
    except OSError as exc:
        raise ConfigError(f"cannot write assistant credential {path} ({exc})")
    logger.info("platform_assistant_credential_minted path=%s", path)
    return credential


def active_assistant_credential() -> Optional[str]:
    """The credential the running assistant holds, or ``None`` when there is none.

    From disk rather than memory because a daemon outlives the incarnations it
    starts, and one adopted at boot was never minted a credential by this
    process. Memoized on the file's stat, for :func:`active_secret`'s reason.

    Never raises: ``None`` means nothing is attributed to the assistant, which
    costs an extra reminder rather than an authentication failure.
    """
    global _ASSISTANT_CREDENTIAL_MEMO
    path = assistant_credential_path()
    key = _stat_key(path)
    if key is None:
        return None
    cached_key, cached = _ASSISTANT_CREDENTIAL_MEMO
    if cached_key == key:
        return cached
    try:
        credential = path.read_text(encoding="utf-8").strip() or None
    except OSError as exc:
        logger.warning(
            "platform_assistant_credential_unreadable path=%s error=%s — the "
            "assistant's calls cannot be told from the operator's until it is "
            "restarted", path, exc,
        )
        return None
    _warn_if_permissive(path)
    _ASSISTANT_CREDENTIAL_MEMO = (key, credential)
    return credential


def revoke_assistant_credential() -> bool:
    """Remove the current assistant credential. Returns whether one was there.

    Called by :func:`lmer_platform.assistant.stop`, rotation included — there the
    successor is minted under the same lock, so the gap is between two calls.

    Absorbs its failures loudly: refusing to stop the assistant over an undeleted
    file would leave the container running *and* the credential live.
    """
    path = assistant_credential_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.error(
            "platform_assistant_credential_unrevoked path=%s error=%s — it stays "
            "valid until the file is removed by hand", path, exc,
        )
        return False
    logger.info("platform_assistant_credential_revoked path=%s", path)
    return True


def binding_notice(config: PlatformConfig) -> str:
    """The one-line startup notice about where the daemon bound (spec D14).

    Names the *source* of the bind values, because an export shadowing what the
    UI wrote is otherwise invisible, and states plainly that a non-loopback bind
    serves plaintext — the daemon terminates no TLS (D9), and an operator who
    wants HTTPS puts a reverse proxy in front.
    """
    source = "environment" if os.environ.get(ENV_BIND_ADDRESS, "").strip() else (
        "config.json" if config_path().exists() else "default"
    )
    where = f"🛰  Platform listening on {config.base_url} (bind from {source})"
    if config.is_loopback:
        return f"{where} — loopback only; put a reverse proxy in front to reach it remotely"
    return f"{where} — reachable over the network in PLAINTEXT (no TLS); front it with nginx for HTTPS"


@dataclass(frozen=True)
class ContainerReach:
    """Where a container this host starts can reach the platform — or why it cannot.

    Exactly one of :attr:`url` and :attr:`reason` is set. Two fields rather than
    an optional URL because the caller's job when there is none is to *say so* —
    the assistant is told in its own environment why it has no platform to talk
    to (spec §8.2), which is the difference between an agent that reports a
    misconfiguration and one that retries a URL that was never going to answer.

    :attr:`source` names which rule produced the URL, for the log line and for
    an operator asking why they got the address they got.
    """

    url: Optional[str] = None
    reason: Optional[str] = None
    source: str = "none"

    @property
    def reachable(self) -> bool:
        return self.url is not None


def _address_kind(address: str) -> str:
    """Classify a bind address as ``wildcard``, ``loopback`` or ``routable``.

    Parsed rather than compared against a list of three spellings the way
    :attr:`PlatformConfig.is_loopback` does, because the question here is
    load-bearing in a way that one is not: ``is_loopback`` decides the wording of
    a startup notice, while this decides whether a container is handed a URL.
    ``127.0.0.2`` and ``::1`` are loopback; ``0.0.0.0`` and ``::`` are not
    destinations at all.

    A name that is not an IP address is called routable, and that is the right
    default rather than a shrug: nothing on this side can tell whether a
    hostname resolves inside a container, and an operator who configured one
    chose it. The residual mistake — a name that resolves to loopback in the
    container — fails loudly at the first request, which is the same failure any
    wrong hostname has.
    """
    text = (address or "").strip()
    if not text or text == "*":
        return "wildcard"
    if text.lower() in ("localhost", "localhost.localdomain"):
        return "loopback"
    try:
        parsed = ipaddress.ip_address(text.strip("[]"))
    except ValueError:
        return "routable"
    if parsed.is_unspecified:
        return "wildcard"
    if parsed.is_loopback:
        return "loopback"
    return "routable"


def _detected_runtime() -> Optional[str]:
    """The container runtime ``lmer`` will use, or ``None`` when there is none.

    Through :func:`lmer_cli.runtime.detect_runtime` so this cannot disagree with
    the process that is about to start the container — including its preference
    order, which is what makes "docker is installed too" answer docker here as
    well as there.
    """
    try:
        return detect_runtime()
    except RuntimeErrorDetect:
        return None


def _probe_output(args: list[str]) -> str:
    """The stdout of a short host command, or ``""`` when it could not be run.

    Never raises and never logs above debug: every caller has a supported
    fall-through for "no answer", and a host with no docker CLI is not a fault
    worth a warning — it is the ordinary case on a podman box.
    """
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=DOCKER_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("platform_gateway_probe_unavailable cmd=%s error=%s", args[0], exc)
        return ""
    if result.returncode != 0:
        logger.debug(
            "platform_gateway_probe_refused cmd=%s rc=%d stderr=%s",
            args[0], result.returncode, (result.stderr or "").strip()[:200],
        )
        return ""
    return result.stdout or ""


def _usable_gateway(text: str) -> Optional[str]:
    """The first token in *text* that is an address a container could dial.

    Reuses :func:`_address_kind` rather than re-deciding what counts, so the
    address this hands to a container is filtered by the same rule that refuses
    to hand over a bind address: ``0.0.0.0`` and ``127.0.0.1`` are rejected here
    for the reasons stated there.

    IPv4 only, and that is a statement about what is being derived rather than a
    limitation to apologise for: docker's default bridge is v4 unless the daemon
    has ip6tables enabled, so a v6 gateway read out of an IPAM config is exactly
    the address-with-no-route this module exists not to invent.
    """
    for token in text.split():
        try:
            parsed = ipaddress.ip_address(token.strip().strip(",").strip("[]"))
        except ValueError:
            continue
        if parsed.version == 4 and _address_kind(str(parsed)) == "routable":
            return str(parsed)
    return None


def _interface_gateway(text: str) -> Optional[str]:
    """The address out of ``ip -4 -oneline addr show`` output.

    Not the same scan as an IPAM config gets: this line also carries the
    interface's *broadcast* address, which parses as a perfectly good routable
    IPv4 and is not a destination. So the ``inet`` token is located and the one
    after it taken, rather than the first thing on the line that looks like an
    address.
    """
    tokens = text.split()
    for index, token in enumerate(tokens[:-1]):
        if token == "inet":
            return _usable_gateway(tokens[index + 1].split("/")[0])
    return None


def _docker_bridge_gateway(*, run=None) -> Optional[str]:
    """The default bridge's gateway — this host, as a container of it can dial it.

    Two probes, in this order, first usable answer winning:

    1. ``docker network inspect bridge`` for the gateway in its IPAM config. The
       daemon's own answer, so it holds on a host whose ``daemon.json`` moved the
       subnet (``bip``, ``default-address-pools``) where a hardcoded
       ``172.17.0.1`` would be confidently wrong — and it is the same daemon that
       will start the session. A zero exit also proves the daemon is *running*,
       which the CLI being on ``PATH`` does not.
    2. ``ip -4 -oneline addr show docker0`` for the interface's own address.
       Consulted only when (1) answered nothing, and the order is not
       interchangeable: a daemon told to use a different bridge (``-b br0``) can
       leave a docker0 whose address is not the gateway, so the authoritative
       answer has to be asked first. What this second probe covers is the reverse
       case — the bridge is up and the CLI cannot reach the daemon's socket (the
       caller is not in the ``docker`` group, ``DOCKER_HOST`` points elsewhere).

    ``None`` when neither answers, and that is a supported outcome rather than a
    failure: :func:`container_base_url` falls through to saying it has no URL. A
    derivation that guessed here would hand a session an address nothing is
    listening on, which is worse than the honest "there isn't one".

    Not memoized, deliberately. Both call sites are cold — the assistant's
    environment is written once per start
    (``assistant._prepare_environment``), and the supervision report is built at
    daemon boot and once per start attempt, never per tick, because the loop's
    steady state blocks on the assistant's exit and builds no report. That is a
    handful of subprocesses per daemon lifetime, while a memo would survive a
    ``systemctl restart docker`` that moved the subnet and keep serving an
    address that had stopped working.

    *run* is a seam for tests, for the same reason *runtime* is one in
    :func:`container_base_url`: what this derives has to be assertable on a host
    with no docker at all, which is most CI.
    """
    execute = run or _probe_output
    for args, parse in (
        (
            ["docker", "network", "inspect", DOCKER_BRIDGE_NETWORK,
             "--format", DOCKER_GATEWAY_FORMAT],
            _usable_gateway,
        ),
        (
            ["ip", "-4", "-oneline", "addr", "show", DOCKER_BRIDGE_INTERFACE],
            _interface_gateway,
        ),
    ):
        found = parse(execute(args) or "")
        if found:
            logger.debug(
                "platform_bridge_gateway address=%s via=%s", found, args[0]
            )
            return found
    logger.warning(
        "platform_bridge_gateway_unknown network=%s interface=%s — a wildcard "
        "bind could not be turned into a URL a container can dial",
        DOCKER_BRIDGE_NETWORK, DOCKER_BRIDGE_INTERFACE,
    )
    return None


def _unreachable_reason(
    config: PlatformConfig, kind: str, runtime: Optional[str]
) -> str:
    """One sentence naming what cannot be reached, why, and the two ways out.

    Written for whoever reads it in a session's environment, which is an agent
    relaying it to an operator on a phone — so it states the fix rather than only
    the fault.
    """
    if kind == "wildcard":
        # The port is named here and not in the loopback branch because it is
        # actionable: a wildcard socket *is* listening on every address this host
        # has, so whoever reads this reason may be able to find one the
        # derivation could not (taskdef/orchestrate/instructions.txt does).
        bind = (
            f"the platform is bound to {config.bind_address!r} port "
            f"{config.bind_port}, which is where a socket listens and not an "
            "address anything can dial"
        )
    else:
        bind = (
            f"the platform is bound to {config.bind_address!r}, which inside a "
            "container is the container's own loopback rather than this host"
        )
    if runtime == "docker" and kind == "wildcard":
        # The bind is fine; only the derivation failed. Saying "docker resolves
        # no host-gateway name" here would send an operator after the wrong
        # thing — a wildcard bind on docker is the case that now works.
        gateway = (
            "this host's container runtime is docker and the default "
            f"{DOCKER_BRIDGE_NETWORK} network's gateway could not be determined "
            f"(neither docker network inspect nor the {DOCKER_BRIDGE_INTERFACE} "
            "interface named an address), so there is nothing to hand over"
        )
    elif runtime == "docker":
        gateway = (
            "this host's container runtime is docker, which resolves no "
            "host-gateway name on Linux unless the run is given "
            "--add-host=host.docker.internal:host-gateway — and lmer passes none"
        )
    elif runtime:
        gateway = (
            f"this host's container runtime is {runtime}, which this build knows "
            f"no host-gateway name for (only podman's {PODMAN_HOST_ALIAS})"
        )
    else:
        gateway = "no container runtime could be detected on this host"
    return (
        f"{bind}, and {gateway}. Bind the platform to an address the container "
        f"can route to ({ENV_BIND_ADDRESS}, or bind_address in config.json), or "
        f"set {ENV_CONTAINER_URL} to a URL that reaches it."
    )


def _override_reach(override: str) -> ContainerReach:
    """Validate the operator's own URL just far enough to catch the two that lie.

    An override is trusted — it exists for the setups this cannot derive — so the
    only refusals are the shapes that cannot work anywhere. A wildcard host is
    the whole point of this function existing (``0.0.0.0`` is not a destination),
    and a value with no scheme is not a URL a client can be handed at all.

    A *loopback* override is deliberately allowed through: it is wrong under
    every runtime lmer starts today, but it is what an operator running the
    container in the host's network namespace would correctly write, and second-
    guessing an explicit setting is how an escape hatch stops being one.
    """
    parts = urlsplit(override)
    if not parts.scheme or not parts.netloc:
        return ContainerReach(reason=(
            f"{ENV_CONTAINER_URL}={override!r} is not a URL — it needs a scheme "
            "and a host, e.g. http://host.containers.internal:8600"
        ))
    if _address_kind(parts.hostname or "") == "wildcard":
        return ContainerReach(reason=(
            f"{ENV_CONTAINER_URL}={override!r} names a wildcard address, which is "
            "where a socket listens and not an address anything can dial — give "
            "the address a container should connect to"
        ))
    return ContainerReach(url=override.rstrip("/"), source="override")


def container_base_url(
    config: PlatformConfig, *, runtime: Optional[str] = None
) -> ContainerReach:
    """Where a container this host starts can reach this platform.

    Not :attr:`PlatformConfig.base_url` with a different name. That is where the
    daemon *listens*, and passing it through to a container would be wrong in
    both of the configurations that actually occur: the default bind is
    ``127.0.0.1``, which inside a container is the container's own loopback, and
    the usual alternative is ``0.0.0.0``, which is not a destination at all.

    Four rules, in order:

    1. :data:`ENV_CONTAINER_URL`, when the operator set one. Their network beats
       any derivation — see :func:`_override_reach` for the two shapes still
       refused.
    2. A **routable** bind address, used as-is. If the daemon is on a LAN address
       the container is on the same network and needs no gateway.
    3. A **wildcard** bind on docker: the default bridge's gateway, read from the
       runtime by :func:`_docker_bridge_gateway`. A socket on ``0.0.0.0`` is
       listening on that gateway address too, so this is a route that exists —
       verified from a stock container on a stock bridge host, where the gateway
       answered ``401`` from ``/api/health`` (2026-07-28). Only the knowledge was
       missing, so a docker host with the usual bind now needs no configuration
       at all.
    4. Otherwise the runtime's host-gateway name, which exists for podman
       (:data:`PODMAN_HOST_ALIAS`) and not for docker as ``lmer`` runs it.

    Rule 3 is deliberately **not** extended to a loopback bind, which is the
    default: the gateway address reaches this host, and a socket bound to ``lo``
    is not on it, so a URL derived there would be a connection refused rather
    than a service. That is the failure an operator hits first, and it stays a
    stated reason instead of becoming a URL.

    Rule 4 is a claim about *naming* the host, not proof of a route: podman's
    alias is present in every container it starts, but whether it reaches a
    service bound to the host's **loopback** depends on the network mode
    (rootless pasta maps the gateway to host loopback; a rootful bridge does
    not). A wildcard bind removes that question, which is why the reason text
    for a docker host leads with the bind rather than with the runtime.

    *runtime* is a parameter so a caller — and a test — can ask about a runtime
    other than this host's. Left unset it is detected, so the answer describes
    the container that would actually start.
    """
    override = _env_str(ENV_CONTAINER_URL)
    if override:
        return _override_reach(override)

    kind = _address_kind(config.bind_address)
    if kind == "routable":
        return ContainerReach(url=config.base_url, source="bind")

    detected = runtime if runtime is not None else _detected_runtime()
    if detected == "docker" and kind == "wildcard":
        gateway = _docker_bridge_gateway()
        if gateway:
            return ContainerReach(
                url=f"http://{gateway}:{config.bind_port}",
                source="bridge-gateway",
            )
    if detected == "podman":
        return ContainerReach(
            url=f"http://{PODMAN_HOST_ALIAS}:{config.bind_port}",
            source="host-alias",
        )
    return ContainerReach(reason=_unreachable_reason(config, kind, detected))


def with_overrides(config: PlatformConfig, **changes) -> PlatformConfig:
    """Return a copy of *config* with *changes* applied and re-validated."""
    return _validate(replace(config, **{k: v for k, v in changes.items() if v is not None}))
