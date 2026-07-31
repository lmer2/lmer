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
import logging
import os
import secrets
import stat
import subprocess
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

# One definition of the credential scrub, imported rather than reimplemented —
# the same trade :mod:`lmer_platform.workrepo` makes, and it says why there.
from lmer_cli.container.clone_and_exec import _scrub_credentials
from lmer_cli.runtime import RuntimeErrorDetect, detect_runtime
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
    "binding_notice", "configured_repo_url", "container_base_url",
]

CONFIG_FILENAME = "config.json"
SECRET_FILENAME = "secret"
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
    autonomous_default: bool = False
    park_idle_side: bool = False

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
    return config


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

    return _validate(PlatformConfig(**values))


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
