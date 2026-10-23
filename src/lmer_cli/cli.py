"""
LMER CLI main entry point.

This module implements the command-line interface for lmerpy, which orchestrates
running containerized development environments. It handles:
- Argument parsing for repository URLs, branches, and execution modes
- Container runtime detection (Docker/Podman)
- Volume mount configuration for workspaces, repos, and user configs
- SSH agent forwarding for authenticated git operations
- Command execution within containers

The CLI supports both interactive Claude Code sessions and arbitrary command execution.
"""

import argparse
import hashlib
import json
import os
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
import re
from typing import Iterable, Mapping
from dotenv import dotenv_values
from work_repo import session_record
from work_repo.run_state import derive_slug
from work_repo.utils import classify_target, is_secret_env_name, repository_of, strip_url_credentials

from . import resolve
from .clone_cache import (
    CACHE_ERROR,
    CACHE_FILE_ABSENT,
    read_cached_repo_file_status,
)
from .container import sources as container_sources
from .container_home import ensure_container_home
from .env_sources import DEFAULT_SOURCES_ENV, consume_env_defaults
from .log import error, info, success, warning
from .mounts import (
    CONTAINER_CLONE_CACHE_DIR,
    CONTAINER_GPG_AGENT_SOCKET_PATH,
    CONTAINER_RELEASE_SIGNING_KEY_PATH,
    MOUNT_LINKS_ENV,
    DirMountSpec,
    FileMountSpec,
    PlannedCredentialMount,
    credential_mount_links,
    format_mount_links,
    build_checkout_mount,
    build_clone_cache_mounts,
    build_gpg_agent_mounts,
    build_release_signing_key_mount,
    build_container_home_mounts,
    build_dir_mounts,
    build_external_taskdef_mounts,
    build_file_mounts,
    build_global_mount,
    build_host_repo_ro_mount,
    build_host_uv_cache_mount,
    build_lmer_docs_mount,
    build_user_harness_mounts,
    build_user_mounts,
    build_workspace_mount,
    container_gpg_pubring_path,
    plan_credential_mounts,
    resolve_gpg_extra_socket,
    resolve_gpg_pubring,
    build_service_mode_mounts,
    resolve_host_clone_cache_dir,
    resolve_host_uv_cache_dir,
)
from .build import DEFAULT_IMAGE, checkout_commit, ensure_image, build_image, resolve_image_tag
from .keep_id import image_id as remap_image_id, record_remap, remap_recorded
from .harness import (
    CODEX_SEED_ERROR_ENV,
    HARNESS_ENV,
    HARNESSES,
    LLM_NAME_ENV,
    Harness,
    UnknownHarnessError,
    get_harness,
    implied_harness_name,
    known_harnesses,
    missing_credential_mounts,
    resolve_harness_selection,
)
from .user_harnesses import (
    CONTAINER_HARNESS_CACHE_DIR,
    CONTAINER_HARNESSES_DIR,
    DEFAULT_HARNESS_CACHE_DIR,
    load_user_harnesses,
)
# The lane→agent map belongs to the renderer; the host derives the variable names
# from it rather than restating a list that could fall out of step.
from .container.dispatch_agents import ENV_PREFIX as DISPATCH_ENV_PREFIX, LANE_AGENTS
from .model_aliases import (
    MODEL_ALIASES_ENV,
    expand_dispatch_value,
    expand_model_alias,
    load_model_aliases,
)
from .pi_providers import (
    PROVIDERS_ENV,
    ProviderConfigError,
    container_provider_env,
    load_providers,
    validate_host_registry_file,
)
from .presets import (
    AGENTS_CONFIG_ENV,
    AGENTS_ENV,
    PRESET_ENV,
    PRESETS_FILE_ENV,
    SPAWN_AGENTS_CONFIG_ENV,
    SPAWN_AGENTS_ENV,
    load_presets,
    preset_env_value,
    resolve_agent_presets,
    select_preset_name,
    task_preset_env_name,
)
from .container_follow import (
    create as create_container,
    follow as follow_container,
    main as follow_main,
    read_container_id,
    remove as remove_container,
)
from .runtime import (
    ambient_container_user,
    apply_env_file_defaults,
    base_run_args,
    build_container_env,
    container_labels,
    container_name,
    ContainerEnvError,
    CPUS_HINT,
    default_env_file_candidates,
    detect_runtime,
    gpu_grant_plan,
    gpu_remediation,
    engine_is_rootless,
    is_root_container_user,
    keep_id_applies,
    LaunchScope,
    lmer_state_dir,
    MEMORY_HINT,
    repo_root_path,
    resolve_gate_ci_baseline,
    resource_limit_plan,
    RUNTIME_ENV,
    _valid_cpus,
    _valid_memory,
    ROOT_CONTAINER_USER,
)
from .service import (
    ServiceError,
    inspect_container_workdir,
    member_of,
    resolve_container,
    resolve_group,
)
from .container.target_switch import DEFAULT_TARGET_FILE as CONTAINER_SERVICE_TARGET_FILE
from .tokens import (
    EVIDENCE_TOKEN_PREFIX,
    _get_gitlab_token,
    _prefer_ssh,
    _convert_ssh_to_https_if_token_available,
    _inject_gitlab_token_if_available,
)
from .util import get_bool_env, resolve_human_identity
from .targets import SlackThreadTargets, partition_targets, special_target_env

# Default range the --fastapi endpoint's host port is picked from. Spelled as a
# string because that is the shape --fastapi-port-range / LMER_FASTAPI_PORT_RANGE
# arrive in; it is the same span as supervisor.DEFAULT_PORT_RANGE, which is what
# the in-container side falls back to.
DEFAULT_FASTAPI_PORT_RANGE = "8700-8799"

# Default pool for general port passthrough (--ports / --port-pool). Kept
# distinct from the FastAPI range (8700-8799) so both features can be used in
# the same session without colliding on default flags.
DEFAULT_PORT_POOL = "8800-8899"

# Default host bind address for --ports passthrough. Loopback-only so the
# published mapping is reachable from the host but not network-exposed.
# Override with --port-bind / LMER_PORT_BIND (e.g. "0.0.0.0" for LAN access).
DEFAULT_PORT_BIND = "127.0.0.1"

# The five per-lane dispatch variables (LMER_DISPATCH_<LANE>), derived from the
# renderer's own lane map so a sixth lane is covered the day it is added.
DISPATCH_LANE_VARS = tuple(DISPATCH_ENV_PREFIX + lane for lane in sorted(LANE_AGENTS))

# --- Release credential provisioning (release-flow spec §4/§5) --------------
#
# Gate mechanism decision: the production release credentials are scoped by
# the **resolved task id** (the LMER_TASK value `_selected_task_id` returns).
# The three mechanisms the codebase offers were weighed:
#
# - `task.yaml` manifest, read host-side from `resolved_taskdef_dir`: the
#   dir is None whenever the taskdef resolves in-container (work-repo
#   taskdefs are invisible host-side), and any taskdef in any tier could
#   declare the flag — a self-claimed authorization is weaker than the
#   thing it gates.
# - Operator preset as authorization (`presets.py` env/args): routes the
#   secret through `preset_applied_env`, which is one of the two leak paths
#   this gate exists to close, and since any session may carry any preset
#   the negative guarantee ("no other taskdef receives the credentials")
#   would be structurally untestable.
# - Resolved task id: the value the operator explicitly selected at launch,
#   available host-side unconditionally, and literally the spec's own
#   scoping unit ("release-taskdef sessions only"). Chosen.
RELEASE_TASK_ID = "release"

# Frozen production credential env var names — later release-flow tasks
# (taskdef instructions, docs, wave-2 scoping tests) cite these:
#
# - LMER_RELEASE_GITHUB_TOKEN — the fine-grained GitHub PAT
#   (`contents:write` + `workflows:write` on the single mirror repo,
#   spec §5). Host value forwarded verbatim, release-taskdef sessions only.
# - LMER_RELEASE_SIGNING_KEY — host side: path to the release SSH signing
#   private key; container side: the fixed read-only bind path
#   CONTAINER_RELEASE_SIGNING_KEY_PATH (the path is remapped, so the host
#   location never leaks into the container env).
RELEASE_GITHUB_TOKEN_ENV = "LMER_RELEASE_GITHUB_TOKEN"
RELEASE_SIGNING_KEY_ENV = "LMER_RELEASE_SIGNING_KEY"


# --- gpg-agent forwarding (issue #355) -------------------------------------
#
# Same gate mechanism as the release credentials above, for the same reason:
# the resolved task id is the value the operator selected at launch, is
# available host-side unconditionally, and is the scoping unit the operator
# chose ("gpg access is gated by taskdef"). It keys on the id, NOT on the
# taskdef resolving host-side — the `ansible` taskdef ships from the work
# repo, which is invisible from here, so an unresolved `ansible` session
# still gets gpg while a `develop` session never does.
ANSIBLE_TASK_ID = "ansible"

# - LMER_ANSIBLE_GPG_AGENT — host side: `auto` (ask gpgconf for the
#   agent-extra-socket) or the socket path; container side: the fixed
#   read-only bind path CONTAINER_GPG_AGENT_SOCKET_PATH. Unset = off.
# - LMER_ANSIBLE_GPG_PUBRING — host side: optional explicit public keyring,
#   otherwise derived from the host gpg home; container side: the remapped
#   bind path. Both are path remaps, so no host location enters the
#   container env.
ANSIBLE_GPG_AGENT_ENV = "LMER_ANSIBLE_GPG_AGENT"
ANSIBLE_GPG_PUBRING_ENV = "LMER_ANSIBLE_GPG_PUBRING"


def _resolve_requested_ports(
    cli_count: int | None,
    cli_pool: str | None,
    env: Mapping[str, str],
) -> tuple[int, str]:
    """Resolve the requested port count and pool from CLI args + env vars.

    CLI values take precedence over the ``LMER_PORT_COUNT`` / ``LMER_PORT_POOL``
    environment variables. Returns ``(count, pool_spec)`` where ``count`` is 0
    when port passthrough is off. Raises :class:`ValueError` if the count is
    non-numeric or negative.
    """
    if cli_count is not None:
        count = cli_count
    else:
        raw = (env.get("LMER_PORT_COUNT") or "").strip()
        if not raw:
            count = 0
        else:
            try:
                count = int(raw)
            except ValueError:
                raise ValueError(f"LMER_PORT_COUNT must be an integer, got {raw!r}")
    if count < 0:
        raise ValueError(f"port count must be >= 0, got {count}")
    pool = cli_pool or env.get("LMER_PORT_POOL") or DEFAULT_PORT_POOL
    return count, pool


def _resolve_port_bind(cli_bind: str | None, env: Mapping[str, str]) -> str:
    """Resolve the host bind address for --ports passthrough.

    Precedence: ``--port-bind`` > ``LMER_PORT_BIND`` env > ``DEFAULT_PORT_BIND``.
    Empty strings (from blank env values) are treated as unset and fall through
    to the next source.
    """
    if cli_bind:
        return cli_bind
    env_bind = (env.get("LMER_PORT_BIND") or "").strip()
    return env_bind or DEFAULT_PORT_BIND


def _resolve_fastapi_host_port(
    cli_range: str | None,
    env: Mapping[str, str],
) -> int:
    """Resolve the host port to publish the ``--fastapi`` endpoint on.

    ``LMER_FASTAPI_PORT`` wins over picking one, because a caller that already
    set it has *committed* to that port: the platform orchestrator picks a port,
    writes it into the session's registry entry, and only then spawns lmer
    (issue #141). Quietly binding somewhere else would leave a session whose
    recorded address is wrong — reachable, but not where anyone is looking.

    With nothing preset, a free port is picked from ``--fastapi-port-range``
    (default ``DEFAULT_FASTAPI_PORT_RANGE``) on loopback, exactly as before.

    Raises :class:`ValueError` for a non-integer or out-of-range
    ``LMER_FASTAPI_PORT`` and for an unparseable range, and :class:`RuntimeError`
    (from ``_pick_port``) when the range has no free port. A preset value is
    *not* probed for availability: the caller chose it, and a container that
    cannot publish it fails loudly at start rather than silently landing
    elsewhere.
    """
    raw = (env.get("LMER_FASTAPI_PORT") or "").strip()
    if raw:
        try:
            port = int(raw)
        except ValueError:
            raise ValueError(f"LMER_FASTAPI_PORT must be an integer, got {raw!r}")
        if not 1 <= port <= 65535:
            raise ValueError(f"LMER_FASTAPI_PORT must be 1-65535, got {port}")
        return port

    # deferred: import cost — only the no-preset-port branch picks one; supervisor + pydantic add ~140 ms
    from .supervisor import _parse_port_range, _pick_port

    port_range = _parse_port_range(cli_range or DEFAULT_FASTAPI_PORT_RANGE)
    return _pick_port(port_range, "127.0.0.1")


def _publish_host_ports(
    run: list[str], ports: list[int], bind: str = DEFAULT_PORT_BIND
) -> None:
    """Append container ``-p`` args publishing each port on ``bind``.

    The same port number is used inside and outside the container. By default
    ``bind`` is loopback (``127.0.0.1``) so the mapping is reachable from the
    host but not network-exposed; pass a different address (e.g. ``0.0.0.0``)
    to expose the ports more widely. Shared by the FastAPI endpoint (always
    bound to loopback by its caller) and the general port-passthrough
    publishing site.
    """
    for port in ports:
        run += ["-p", f"{bind}:{port}:{port}"]


def _make_sigterm_cleanup_handler(container_env):
    """SIGTERM handler that deletes the env file, then dies as SIGTERM would.

    Python's default SIGTERM disposition terminates without unwinding, which
    strands the session's mode-0600 env file in ``$TMPDIR`` holding the
    tokenized work-repo URL and every forwarded token (issue #158).

    Active while the container is being created; once it exists,
    :func:`lmer_cli.container_follow.follow` installs its own handler, which
    stops the container instead.

    It deliberately does NOT raise. ``subprocess.call`` is::

        with Popen(...) as p:
            try:
                return p.wait(timeout=timeout)
            except:          # bare — catches SystemExit too
                p.kill()
                raise

    so raising ``SystemExit`` here would unwind through that bare ``except``
    and SIGKILL the runtime client mid-create. That is why this restores the
    default disposition and re-raises the signal at itself instead: process
    semantics stay identical to having no handler at all, and the file still
    gets deleted.

    "Identical to having no handler" is exact for the CLI, whose SIGTERM
    disposition IS the default. It is not exact for a hypothetical in-process
    caller that had installed ``SIG_IGN`` or a handler of its own before
    calling ``main()``: this restores ``SIG_DFL``, so such a parent would die
    where it previously ignored the signal. Nothing calls ``main()`` that way
    today, and preserving the caller's disposition would mean either returning
    from the handler (the file survives a second signal) or raising (the
    unwinding this exists to avoid).
    """

    def _handler(signum, frame) -> None:
        container_env.cleanup()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    return _handler


def _thread_post_label(ns, task_id: str | None) -> str:
    """How a thread is told which session posted into it: a shell session has no
    run, so it says the session id it was given, else its task."""
    session = (getattr(ns, "session", None) or "").strip()
    if session:
        return f"lmer session {session}"
    return f"lmer {task_id}" if task_id else "an lmer session"


def _start_thread_servicer(
    ns, host_port, control_token: str, uploads_host, uploads_container, task_id
):
    """This process's answer to `lmer-signal read` / `post --thread` (docs/COMMS.md §12).

    A servicer that will not start costs the session those two verbs and nothing
    else, so it is a warning rather than a failed launch.
    """
    if not host_port or not control_token:
        return None
    try:
        # deferred: import cost — thread_access (~10 ms) loads only for a launch with a control port and token
        from lmer_cli.thread_access import CLI_ENVIRONMENT, ThreadServicer, UploadStore

        servicer = ThreadServicer(
            supervisor_url=f"http://127.0.0.1:{host_port}",
            token=control_token,
            label=_thread_post_label(ns, task_id),
            # The run's own name, not the label above: that one already says
            # "lmer" and `Poster.author` adds it (#427, review of !300).
            run_name=task_id or "",
            store=UploadStore.of(
                str(uploads_host) if uploads_host else None, uploads_container
            ),
            where=CLI_ENVIRONMENT,
            assistant_session=os.environ.get("LMER_SESSION_KIND") == "assistant",
        )
        servicer.start()
        return servicer
    except Exception as exc:
        warning(
            f"Thread servicer did not start ({exc}) — this session cannot read or "
            "post threads it is not connected to"
        )
        return None


def _apply_port_passthrough(
    ns: argparse.Namespace, env: dict, run: list[str]
) -> int | None:
    """Resolve, allocate, and publish general port-passthrough ports.

    Allocates the requested number of free ports from the pool on the host
    (before container start, so parallel sessions get disjoint ports),
    publishes each on the resolved bind address (loopback by default), and
    exports the list to the container via ``LMER_PORTS`` so Claude can bind
    services to ports reachable from the host. CLI flags win over the
    ``LMER_PORT_COUNT`` / ``LMER_PORT_POOL`` / ``LMER_PORT_BIND`` env vars.

    Mutates ``env`` and ``run`` in place. Returns ``None`` on success
    (including when no ports are requested) or a process exit code on a fatal
    error, so the caller can ``return`` it directly.
    """
    try:
        port_count, port_pool_spec = _resolve_requested_ports(
            ns.ports, ns.port_pool, os.environ
        )
    except ValueError as exc:
        error(f"❌ {exc}")
        return 2
    if port_count <= 0:
        return None

    port_bind = _resolve_port_bind(getattr(ns, "port_bind", None), os.environ)

    # deferred: import cost — only a launch that requests ports picks them; supervisor + pydantic add ~150 ms
    from .supervisor import _parse_port_range, _pick_ports

    try:
        port_pool = _parse_port_range(port_pool_spec)
    except ValueError as exc:
        error(f"❌ Invalid port pool {port_pool_spec!r}: {exc}")
        return 2
    try:
        picked_ports = _pick_ports(port_pool, port_bind, port_count)
    except RuntimeError as exc:
        error(f"❌ {exc}")
        return 2

    env["LMER_PORTS"] = ",".join(str(p) for p in picked_ports)
    _publish_host_ports(run, picked_ports, bind=port_bind)
    published = ", ".join(str(p) for p in picked_ports)
    info(f"🔌 Publishing {len(picked_ports)} port(s) on {port_bind}: {published}")
    info("   (exposed inside the container via LMER_PORTS; bind services to 0.0.0.0)")
    _record_published_ports(picked_ports, port_bind)
    return None


def _record_platform_facts(**facts) -> None:
    """Merge *facts* into ``$LMER_PLATFORM_PORTS_FILE`` when the platform set it.

    The orchestrator platform (issue #141) hands a spawned ``lmer`` this path to
    report back things it can only learn at launch — the ports it published (free
    ports are picked *here*, so the spawning platform cannot know them in advance)
    plus the model and harness actually driving the session, which are resolved
    after preset application and would be guesses anywhere else (T50/T51). The platform folds
    whatever it finds into the session's registry entry
    (``lmer_platform.spawn.absorb_ports``).

    A file rather than an import keeps the dependency pointing the right way:
    ``lmer_platform`` depends on ``lmer_cli``, never the reverse. Merged rather
    than overwritten because the two facts are recorded at different points in a
    launch — model/harness before the container is built, ports while it starts —
    and the second writer must not erase the first. Best-effort by design:
    failing to record an annotation must never fail the launch it annotates, so a
    file that cannot be read is treated as empty rather than as an error.

    Keeps its ``…PORTS_FILE`` name: the variable is set by whatever platform
    spawned this process, which may be an older daemon, and a name is not worth a
    launch that reports nothing.
    """
    target = os.environ.get("LMER_PLATFORM_PORTS_FILE", "").strip()
    if not target:
        return
    path = Path(target)
    payload: dict = {}
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(existing, dict):
            payload = existing
    except (OSError, ValueError):
        # Absent is the normal first write; corrupt or unreadable is worth no
        # more than starting over, since every fact in here is re-derivable.
        payload = {}
    payload.update(facts)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # pid *and* thread id: two writers sharing a temp path is worse than
        # losing a write (see lmer_platform.store.write_json), and the launch
        # records more than one fact at more than one point.
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        warning(f"⚠️  Could not record session facts to {target}: {exc}")


def _record_published_ports(ports: list[int], bind: str) -> None:
    """Report the published port mapping to the spawning platform.

    The mapping is what lets the platform's UI link "what this session is
    serving"; see :func:`_record_platform_facts` for the channel and why it
    merges.
    """
    _record_platform_facts(
        bind=bind,
        # Host and container port are the same number (see _publish_host_ports).
        ports=[{"host": p, "container": p} for p in ports],
    )


def _record_session_model(
    model: str | None, harness: str | None = None, effort: str | None = None,
) -> None:
    """Report resolved launch settings; absent values leave harness defaults unnamed."""
    facts = {}
    if model:
        facts["model"] = model
    if harness:
        facts["harness"] = harness
    if effort:
        facts["effort"] = effort
    if facts:
        _record_platform_facts(**facts)


class ParseRefused(Exception):
    """Raised instead of exiting when a *quiet* parse rejects its argv.

    argparse prints usage and raises ``SystemExit``, which is right for a
    command line and wrong for a caller that parses on a server path:
    ``lmer_platform.slots`` asks "would these preset args rebind the slot?" on
    every fleet-view poll, where the usage block would land in the daemon's log
    and a swapped ``sys.stderr`` would be a process-global mutation on a
    threadpool route.
    """


class _QuietParser(argparse.ArgumentParser):
    """An ``ArgumentParser`` that raises rather than printing and exiting.

    Every output path is closed, not just ``error()``: ``-h`` reaches
    :meth:`exit` through ``print_help``, and both write through
    ``_print_message``.
    """

    def error(self, message: str):  # pragma: no cover - exercised via parse_args
        raise ParseRefused(message)

    def exit(self, status: int = 0, message: str | None = None):
        raise ParseRefused(message or f"parser exited with status {status}")

    def _print_message(self, message, file=None):
        return


def parse_args(
    argv: list[str], *, quiet: bool = False
) -> tuple[argparse.Namespace, list[str]]:
    """
    Parse command-line arguments for lmerpy.

    Args:
        argv: List of command-line arguments to parse
        quiet: Raise :class:`ParseRefused` instead of printing usage and
            exiting, for callers that parse on a server path. Off by default, so
            the command line behaves exactly as it did.

    Returns:
        Tuple of (parsed namespace, remaining args after known args)
        Remaining args are used for commands after --exec
    """
    # Handle -- separator manually: everything after -- goes to rest
    # This is needed because argparse's parse_known_args doesn't stop
    # consuming positional arguments at --
    rest: list[str] = []
    if "--" in argv:
        idx = argv.index("--")
        rest = argv[idx + 1:]
        argv = argv[:idx]

    parser_class = _QuietParser if quiet else argparse.ArgumentParser
    parser = parser_class(prog="lmerpy", add_help=True)
    # Task is optional when --no-task is provided; otherwise required
    parser.add_argument("task", nargs="?", help="Task type (e.g., chat, review, develop, modernize)")
    parser.add_argument("target", nargs="*", help="Repository URL/path or PR/MR/issue URL. First target is primary (sets env vars), additional targets are cloned but don't override env vars.")
    parser.add_argument("--workspace-volume", dest="workspace_volume")
    parser.add_argument("--workspace-bind", dest="workspace_bind")
    parser.add_argument("--branch", dest="branch")
    parser.add_argument("--ref", dest="ref")
    parser.add_argument("--remote", dest="remote", help="Git remote name to use (required when local repo has multiple remotes)")
    parser.add_argument("--repo-url", dest="repo_url", help="Repository URL to clone, independent of the target. The primary target is then only the task's target (LMER_TASK_TARGET and the run slug): a label, an issue/MR URL, or a Slack/matrix thread permalink, which lets a non-chat task run per thread. A bare word is never probed as a local checkout; a target spelled as a path (/x, ./x, ../x) or a repository URL is refused as a second repository, as is --repo beside --repo-url.")
    parser.add_argument("--exec", dest="exec_mode", action="store_true", help="Run an arbitrary command in the container")
    parser.add_argument("--no-clone", dest="no_clone", action="store_true", help="Skip git clone, just run command (requires --exec)")
    parser.add_argument("--service", dest="service", help="Docker service/container name to exec into (service mode)")
    parser.add_argument("--service-group", dest="service_group", help="Compose project whose running services this session may target (service-group mode); the agent retargets with the in-container target-switch")
    parser.add_argument("--checkout", dest="checkout", help="Path to existing local source checkout (mounted as /workspace)")
    parser.add_argument("--repo", dest="repo", metavar="URL_OR_PATH", help="Explicit repository for this launch (URL or local path). It beats a repository derived from a work-item, reference or thread target; refused as 'two repositories named' beside a repository positional, and also beside --checkout, LMER_NO_REPO, --no-clone or --no-task. `lmer <taskdef> --repo <url>` with no positional is the plain project session, launched from anywhere. Nothing new reaches the container.")
    parser.add_argument("--user", dest="user", help="Container user (e.g., developer or 0:0)")
    parser.add_argument("--match-uid", dest="match_uid", action="store_true", help="Run container as your host UID:GID (fixes SSH agent permissions)")
    parser.add_argument("--memory", dest="memory", help="Container memory cap for this launch, overriding every environment tier (LMER_<TASK>__<REPO>__MEMORY, LMER_<TASK>_MEMORY, LMER_MEMORY). Same grammar as LMER_MEMORY — an integer with an optional b/k/m/g suffix, at least 6m. Unlike the environment tiers, an invalid value here aborts the run rather than falling through: an explicit request is not something to silently substitute.")
    parser.add_argument("--cpus", dest="cpus", help="Container CPU quota for this launch, overriding every environment tier (LMER_<TASK>__<REPO>__CPUS, LMER_<TASK>_CPUS, LMER_CPUS). Same grammar as LMER_CPUS — a positive number of cores, integer or fractional. Invalid values abort the run, as with --memory.")
    parser.add_argument("--root-userns", dest="root_userns", action="store_true", help="Run as container root under the default rootless-podman map (skips the keep-id UID remap for a fast cold start; the agent is root inside the container). Env: LMER_ROOT_USERNS")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable verbose logging (same as LMER_VERBOSE=1).")
    parser.add_argument("--debug", action="store_true",
                        help="Alias for --verbose (sets LMER_VERBOSE=1).")
    parser.add_argument("--show-env", dest="show_env", action="store_true", help="Display LMER environment variable configuration on startup")
    parser.add_argument("--mount-file", dest="mount_file", action="append", metavar="HOST:CONTAINER[:MODE]", help="Mount a single host file into the container at an explicit destination (repeatable). HOST supports ~ and $VAR expansion and must be an existing file; CONTAINER must be an absolute path; MODE is ro (default) or rw. Invalid entries abort the run. (env: LMER_MOUNT_FILES, comma-separated entries)")
    parser.add_argument("--mount-dir", dest="mount_dir", action="append", metavar="HOST:CONTAINER[:MODE]", help="Mount a host directory into the container at an explicit destination (repeatable). Same grammar and strictness as --mount-file, but HOST must be an existing directory; MODE is ro (default) or rw. The mount shadows whatever the image has at CONTAINER. Invalid entries abort the run. (env: LMER_MOUNT_DIRS, comma-separated entries)")
    parser.add_argument("--env-file", dest="env_file", help="Additional .env file to load (highest precedence among .env files; below already-exported environment variables). Its variables are forwarded into the container alongside cwd/.env and ~/.lmer/.env, which still load. Useful when lmer is spawned from a directory without the relevant .env (e.g. the Slack listener).")
    parser.add_argument("--preset", dest="preset", help="Named startup preset from LMER_PRESETS_FILE to apply (env: LMER_<TASK>_PRESET for one taskdef, e.g. LMER_REVIEW_PRESET, then LMER_PRESET for any task; the flag wins over both). Flags and explicit exported variables override the preset; preset env overrides .env defaults, including defaults loaded by the platform.")
    parser.add_argument("--list-presets", dest="list_presets", action="store_true", help="List the presets available from LMER_PRESETS_FILE and exit")
    parser.add_argument("--agents", dest="agents", help="Comma-delimited preset names the session's agent may fan a task out to via spawn-harness (env: LMER_AGENTS; the flag wins). Names are resolved against LMER_PRESETS_FILE host-side (unknown names fail fast); each preset contributes its env overlay plus --harness/--prompt folded from its args — the remaining launch-shaping fields (checkout/service/other args) are ignored with a warning — and only that resolved config is forwarded into the container.")

    # Use parse_known_args so we can capture the command after --exec
    parser.add_argument("--no-task", dest="no_task", action="store_true", help="Run without selecting a task (exec mode only)")
    parser.add_argument("--skip-build", dest="skip_build", action="store_true", help="Do not auto-build or pull the container image if missing")
    # Supervisor / FastAPI controls (consumed inside the container by lmer-supervisor)
    parser.add_argument("--fastapi", dest="fastapi", action="store_true", help="Expose a FastAPI endpoint to read/write the claude process (POST /input, GET /output)")
    parser.add_argument("--no-thread-connectors", dest="no_thread_connectors", action="store_true", help="This session's host side belongs to another process: set the container's chat context for any Slack/matrix thread targets, but run no connector for them, claim no thread in the host registry, prepare no chat upload store, and run no thread servicer (issue #414). The platform daemon passes this for every session it spawns, because it owns all of those itself — the connectors so they can be added and removed while the session runs (issue #412). Nothing else should: without a connector the thread neither hears the session nor reaches it, and without a servicer the session cannot read or post a thread at all.")
    parser.add_argument("--manual-start", dest="manual_start", action="store_true", help="Do not auto-inject /start into claude on launch")
    parser.add_argument("--prompt", dest="prompt", help="Follow-up prompt injected immediately after the auto-/start (e.g. --prompt='research X online first'). Ignored under --manual-start since nothing is auto-injected then.")
    parser.add_argument("--answer", dest="answer", help="Answer to the run's recorded open question, exported to the container as LMER_ANSWER. The fresh session applies it at session start (question_answered event, question stop cleared) and its resume brief leads with the question+answer pair. This flag is the only supported source: a host/.env LMER_ANSWER is deliberately NOT read, so a stale value can never silently auto-answer a future question-stop.")
    parser.add_argument("--name", dest="run_name", help="kebab-case run name recorded at session start, exported to the container as LMER_RUN_NAME and applied once by `work session-start` (on the call that opens the record's session). A name is a LABEL: on schema 2 the launch name replaces a default or inherited name; only a legacy tree enforces per-project uniqueness, where a held name is reported in the session's start output and the run stays unnamed. This flag is the only source: a host-exported or .env LMER_RUN_NAME is never forwarded. Refused with --no-task (no run context to record it in); with --exec it applies only if the command runs `work session-start`.")
    parser.add_argument("--goal", dest="run_goal", help="Run goal recorded at session start, exported to the container as LMER_RUN_GOAL and applied once by `work session-start` (it replaces a goal inherited from a continuation source). This flag is the only source: a host-exported or .env LMER_RUN_GOAL is never forwarded. Refused with --no-task; with --exec it applies only if the command runs `work session-start`.")
    parser.add_argument("--session", dest="session", help="Session id selected for continuation. The host marks it as a source candidate: schema 2 reads it through LMER_SOURCE_SESSION_ID and mints a fresh writer UID, while schema 1 keeps the established LMER_SESSION_ID behavior. The new execution inherits the source's name label; a never-started `work seed` record is a valid source and needs no --allow-unsigned-source. This flag is the only supported source; an inherited host LMER_SESSION_ID is never read.")
    parser.add_argument("--source-session", help="Read-only schema-2 predecessor UID, passed explicitly by the platform. The new execution always gets its own UID and inherits the source's name label; a never-started `work seed` record is a valid source without --allow-unsigned-source.")
    parser.add_argument("--launch-uid", dest="launch_uid", metavar="UID", help="The fresh execution uid this launch records under; a caller that mints one never reuses it. Exported to the container as LMER_LAUNCH_UID, where a uid that already names a record is refused. This flag is the only source: a host-exported or .env LMER_LAUNCH_UID is never forwarded, and a child session mints its own.")
    parser.add_argument("--allow-unsigned-source", dest="allow_unsigned_source", metavar="SESSION_UID", help="Explicitly continue fresh when the selected schema-2 source has no final session_end. Must exactly equal --session, so the decision authorizes only that source; omit it to cancel/wait after a refusal.")
    parser.add_argument("--no-supervisor", dest="no_supervisor", action="store_true", help="Bypass lmer-supervisor and exec the harness directly (debug aid for rendering issues)")
    parser.add_argument("--harness", dest="harness", help=f"Agent harness to run in the container (default: claude, or LMER_HARNESS; when neither is set, LMER_LLM_NAME can imply one, e.g. gpt-* selects codex). Known: {', '.join(sorted(known_harnesses()))} (incl. any user-installed harnesses from ~/.lmer/harnesses; see docs/HARNESSES.md)")
    parser.add_argument("--model", dest="model", help="Model the harness runs, exported to the container as LMER_LLM_NAME (env: LMER_LLM_NAME; the flag wins over it, and over a preset's env). Forwarded verbatim to the harness's own --model flag by the runner scripts — lmer never validates it, the harness rejects what it does not know. Also implies the harness when neither --harness nor LMER_HARNESS is set (e.g. --model gpt-5.5 selects codex).")
    parser.add_argument("--fastapi-port-range", dest="fastapi_port_range", help="Port range LOW-HIGH the FastAPI endpoint may bind to (default 8700-8799)")
    parser.add_argument("--fastapi-host", dest="fastapi_host", help="Host for the FastAPI endpoint to bind (default 127.0.0.1)")
    parser.add_argument("--fastapi-token", dest="fastapi_token", help="Bearer token for the FastAPI endpoint (auto-generated if omitted)")
    # General port passthrough: allocate N free ports from a pool and publish
    # them so a service Claude runs inside the container is reachable on the host.
    parser.add_argument("--ports", dest="ports", type=int, help="Number of host ports to allocate from --port-pool and publish to the container (env: LMER_PORT_COUNT)")
    parser.add_argument("--port-pool", dest="port_pool", help=f"Port pool LOW-HIGH to allocate --ports from (default {DEFAULT_PORT_POOL}; env: LMER_PORT_POOL)")
    parser.add_argument("--port-bind", dest="port_bind", help=f"Host bind address for --ports publishing (default {DEFAULT_PORT_BIND}; pass 0.0.0.0 to expose ports beyond loopback; env: LMER_PORT_BIND)")

    ns, extra = parser.parse_known_args(argv)
    # argparse leaves a positional that follows an option in `extra`, where only
    # --exec reads it; a value after an unknown flag is that flag's, not a target.
    ns.stray_positionals = [
        token for index, token in enumerate(extra)
        if not token.startswith("-")
        and not (index and extra[index - 1].startswith("-") and "=" not in extra[index - 1])
    ]
    # Combine any extra unknown args with the -- separated rest
    rest = extra + rest
    return ns, rest


def _parse_gitlab_mr_url(target: str) -> tuple[str | None, str | None, str | None]:
    """
    Parse a GitLab merge request URL and extract host, project, and MR ID.

    Args:
        target: GitLab MR URL (e.g., https://gitlab.example.com/group/project/-/merge_requests/756)

    Returns:
        Tuple of (host, project, mr_id) or (None, None, None) if not a GitLab MR URL
    """
    try:
        parsed = urlparse(target)
    except Exception:
        return None, None, None

    if not parsed.scheme or not parsed.netloc or not parsed.path:
        return None, None, None

    # Check if this is a GitLab merge request URL
    # GitLab URLs use /-/ separator: group/project/-/merge_requests/123
    if '/-/merge_requests/' not in parsed.path.lower():
        return None, None, None

    # Extract host (hostname only, strip any credentials)
    host = parsed.hostname
    if not host:
        return None, None, None

    # Extract project path (everything before /-/)
    if '/-/' in parsed.path:
        project_path = parsed.path.split('/-/')[0].strip('/')
        if not project_path:
            return None, None, None
    else:
        return None, None, None

    # Extract MR ID (number after merge_requests/)
    path_after_separator = parsed.path.split('/-/')[1]
    # Look for merge_requests/ followed by a number
    match = re.search(r'merge_requests/(\d+)', path_after_separator, re.IGNORECASE)
    if not match:
        return None, None, None

    mr_id = match.group(1)

    return host, project_path, mr_id


def _parse_repo_url(repo_url: str) -> tuple[str | None, str | None]:
    """
    Parse a repository URL and extract host and project path.

    Supports both SSH and HTTPS formats:
    - SSH: git@gitlab.example.com:group/project -> (gitlab.example.com, group/project)
    - HTTPS: https://gitlab.example.com/group/project -> (gitlab.example.com, group/project)
    - GitHub SSH: git@github.com:owner/repo -> (github.com, owner/repo)
    - GitHub HTTPS: https://github.com/owner/repo -> (github.com, owner/repo)

    Args:
        repo_url: Repository URL in SSH or HTTPS format

    Returns:
        Tuple of (host, project_path) or (None, None) if parsing fails

    What is parsed is the repository the URL belongs to
    (``work_repo.utils.repository_of``, spec §2), so a work-item or reference
    URL — ``.../group/project/-/issues``, ``.../owner/repo/pull/9`` — yields
    the project and not the route: ``LMER_REPO_PROJECT`` never contains
    ``/-/`` (#391). A repository URL is its own answer and parses as before.
    """
    if not repo_url:
        return None, None
    repo_url = repository_of(repo_url) or repo_url

    # Handle SSH format: git@host:path
    if repo_url.startswith("git@"):
        # Format: git@host:path
        parts = repo_url.split(":", 1)
        if len(parts) != 2:
            return None, None
        host = parts[0].replace("git@", "")
        # removesuffix, not rstrip: rstrip(".git") strips a character *class*, so
        # it ate any trailing run of `.`, `g`, `i` or `t` — `group/project` came
        # back as `group/projec`, and LMER_REPO_PROJECT (hence the run directory,
        # the memory dir and the log path) was filed under the mangled name.
        project_path = parts[1].removesuffix(".git")
        return host, project_path

    # Handle HTTPS format: https://host/path
    try:
        parsed = urlparse(repo_url)
    except Exception:
        return None, None

    if not parsed.netloc:
        return None, None

    host = parsed.hostname
    if not host:
        return None, None
    # Extract project path from URL path
    path = parsed.path.strip("/")
    # Remove .git suffix if present
    path = re.sub(r"\.git$", "", path)

    if not path:
        return None, None

    return host, path


def container_task_target(primary_target: str | None, klass: str) -> str:
    """The ``LMER_TASK_TARGET`` a launch exports for its primary target.

    The exported target is the session's *task context*, never its repository
    (spec §4/§2, A1-wave0.host-export). A ``repository`` target — a local
    filesystem path (``.``, ``./``, ``../x``, an absolute checkout path) or a
    bare repository URL — is a plain project session: the repository comes
    from the checkout's git origin or the URL, and the spelling carries no
    identity worth a slug, so the export is the **empty string** (delivered
    empty, never absent, because every container reader treats ``""`` as "no
    target" while an absent variable falls back to whatever the environment
    holds). ``none`` (no positional at all) is the same empty export. A
    ``work-item``, ``reference`` or ``other`` target is exported verbatim: a
    work item is the session's target, a document URL is context, and an
    ``other`` URL reached here only because ``--repo`` named the repository.
    ``primary_target`` itself — the value the GitLab MR parsing, the
    host-repo mount and the secondary-target list read — is never touched.
    Pure: no filesystem, no environment.
    """
    if klass in ("repository", "none"):
        return ""
    return primary_target


def _derive_repo_url_from_task_target(target: str) -> str | None:
    """
    Best-effort derivation of a base repository URL from a task target URL
    such as PR/MR/issue links — and, since A1-wave0.repository-of, from a
    reference URL inside a repository (a file, a tree, a wiki page, a commit
    listing, an id-less list page), so a document target derives the
    repository its clone comes from (#523).

    For GitLab hosts with available API tokens, returns HTTPS URL with token auth.
    Otherwise returns SSH-format URL for git cloning. A repository URL, a thread
    permalink, a non-forge URL and a non-URL target derive None.

    Supports:
    - GitHub: https://github.com/owner/repo/pull/123 -> git@github.com:owner/repo
    - GitLab: https://gitlab.com/group/project/-/merge_requests/123 -> https://oauth2:TOKEN@gitlab.com/group/project.git (if token available)
    - GitLab: https://gitlab.example.com/group/subgroup/project/-/issues/456 -> git@gitlab.example.com:group/subgroup/project (if no token)
    - GitLab: https://gitlab.com/group/project/-/work_items/70 (newer issue URL form) -> same as /-/issues/
    - GitLab: https://gitlab.com/group/project/-/blob/main/docs/x.md -> same as /-/issues/ (#523)
    - GitLab: https://gitlab.com/group/project/-/issues (a list page) -> same as /-/issues/N (#391)
    """
    # The repository is repository_of's answer — the one definition behind
    # this helper in both its copies and behind _parse_repo_url (#391) — for
    # a work item or a reference and for nothing else. A repository URL
    # derives None on purpose: spawn._plain_repo_url_from_target,
    # clone_secondary_mr and _resolve_clone_url fall back to the target
    # itself as the clone URL: the one class repository_of answers for that
    # this helper must not, hence the class check ahead of it.
    if classify_target(target) not in ("work-item", "reference"):
        return None
    repository = repository_of(target)
    if repository is None:
        return None
    parsed = urlparse(repository)
    host = parsed.hostname
    project_path = parsed.path.strip("/")
    if not host or not project_path:
        return None

    # GitLab-shaped (a /-/ route): HTTPS with the host's token when there is
    # one, SSH otherwise. The GitHub shape never looks a token up — it never
    # did, and _get_gitlab_token reads GH_TOKEN for github.com and refuses a
    # generic GITLAB_TOKEN with a notice, both of which would be new.
    if "/-/" in urlparse(target).path:
        token = _get_gitlab_token(host)
        if token:
            # Preserve the repository's authority: a non-default HTTPS port
            # (gitlab.example.com:8443) must survive the credential insert.
            try:
                authority = host if parsed.port is None else f"{host}:{parsed.port}"
            except ValueError:
                authority = host
            return f"https://oauth2:{token}@{authority}/{project_path}.git"
    return f"git@{host}:{project_path}"


def _get_taskdef_paths(repo_root: Path | None) -> list[Path]:
    """
    Get ordered list of taskdef directories to search.

    Checks:
    1. Built-in taskdef dir (repo_root/taskdef if available)
    2. LMER_TASKDEF_PATHS env var (colon-separated list of additional paths)

    Returns paths in search order (first match wins).
    """
    paths: list[Path] = []

    # Built-in taskdef dir first
    if repo_root is not None:
        builtin = repo_root / "taskdef"
        if builtin.exists() and builtin.is_dir():
            paths.append(builtin)

    # Additional paths from env var
    paths += _get_external_taskdef_paths()

    return paths


def _get_external_taskdef_paths() -> list[Path]:
    """
    Parse LMER_TASKDEF_PATHS into a list of existing directory Paths.
    """
    paths: list[Path] = []
    extra_paths = os.environ.get("LMER_TASKDEF_PATHS", "")
    if extra_paths:
        for p in extra_paths.split(":"):
            p = p.strip()
            if p:
                path = Path(p)
                if path.exists() and path.is_dir():
                    paths.append(path)
    return paths


def _discover_tasks(taskdef_root: Path) -> set[str]:
    """
    Discover available tasks from a single taskdef directory.

    A task is any subdirectory of taskdef_root containing an instructions.txt file.
    """
    tasks: set[str] = set()
    if not taskdef_root.exists() or not taskdef_root.is_dir():
        return tasks
    for child in taskdef_root.iterdir():
        if child.is_dir() and (child / "instructions.txt").exists():
            tasks.add(child.name)
    return tasks


def _discover_all_tasks(taskdef_paths: list[Path]) -> set[str]:
    """
    Discover available tasks from all taskdef directories.
    """
    tasks: set[str] = set()
    for path in taskdef_paths:
        tasks |= _discover_tasks(path)
    return tasks


def _resolve_taskdef_dir(task_id: str, taskdef_paths: list[Path]) -> Path | None:
    """
    Find the taskdef directory for a given task ID.

    Searches paths in order, returning the first match.
    """
    for path in taskdef_paths:
        candidate = path / task_id
        if candidate.is_dir() and (candidate / "instructions.txt").exists():
            return candidate
    return None


def _parse_mount_specs(
    flag_values: list[str], env_value: str, *, flag: str, env_name: str, want_dir: bool
) -> list:
    """Shared parser for the ``host:container[:mode]`` mount grammar.

    One body for ``--mount-file`` and ``--mount-dir`` because they are one
    grammar: the only differences are which host predicate must hold, which
    spec type comes out, and the names quoted back in an error. Duplicating it
    would let the two flags drift apart — on `$VAR` expansion, on the
    env-before-flags order, on the duplicate-destination rule — in ways a user
    would read as a bug in whichever half they tried second.

    Each entry is ``host:container[:mode]``: ``host`` gets ``~`` and ``$VAR``
    expansion and must resolve to an existing file (or directory, per
    *want_dir*); ``container`` must be an absolute path; ``mode`` is ``ro``
    (default) or ``rw``.

    ``env_value`` is split on commas (the entry separator — ``:`` is taken by
    the field grammar, cf. LMER_TASKDEF_PATHS choosing its own separator) and
    its entries are ordered BEFORE the flags, so a persistent ``.env`` sets a
    baseline an ad-hoc flag can override. When two entries target the same
    container destination, last wins and a warning is emitted.

    Fail-fast by design — deliberately stricter than the skip-and-warn mount
    builders (external taskdefs, uv cache): these entries are credentials the
    user explicitly asked for, and silently launching without a kubeconfig
    would only surface as confusing downstream auth failures. Raises
    ``ValueError`` naming the offending entry on any invalid input.
    """
    entries: list[tuple[str, str]] = []  # (source, raw entry)
    for raw in env_value.split(","):
        raw = raw.strip()
        if raw:
            entries.append((env_name, raw))
    for raw in flag_values:
        raw = raw.strip()
        if raw:
            entries.append((flag, raw))

    by_container: dict[str, object] = {}
    for source, raw in entries:
        parts = raw.split(":")
        if len(parts) == 2:
            host_raw, container = parts
            mode = "ro"
        elif len(parts) == 3:
            host_raw, container, mode = parts
        else:
            raise ValueError(
                f"{source} entry {raw!r} is not of the form host:container[:mode]"
            )

        host = Path(os.path.expandvars(host_raw)).expanduser()
        if want_dir and not host.is_dir():
            raise ValueError(
                f"{source} entry {raw!r}: host path {str(host)!r} is not an "
                f"existing directory"
            )
        if not want_dir and not host.is_file():
            raise ValueError(
                f"{source} entry {raw!r}: host path {str(host)!r} is not an "
                f"existing file"
            )
        if not container.startswith("/"):
            raise ValueError(
                f"{source} entry {raw!r}: container path {container!r} must be "
                f"absolute"
            )
        if mode not in ("ro", "rw"):
            raise ValueError(
                f"{source} entry {raw!r}: mode {mode!r} must be 'ro' or 'rw'"
            )

        if container in by_container:
            warning(
                f"⚠️  Duplicate mount destination {container} — "
                f"{source} entry {raw!r} overrides the earlier one (last wins)"
            )
        spec_type = DirMountSpec if want_dir else FileMountSpec
        by_container[container] = spec_type(host=host, container=container, mode=mode)

    return list(by_container.values())


def parse_file_mount_specs(
    flag_values: list[str], env_value: str
) -> list[FileMountSpec]:
    """Parse and validate --mount-file / LMER_MOUNT_FILES entries.

    Grammar, ordering, dedup and fail-fast rules: :func:`_parse_mount_specs`.
    ``host`` must be an existing **file** — a directory is refused here so it
    cannot arrive at a destination the user meant to hold one file.
    """
    return _parse_mount_specs(
        flag_values,
        env_value,
        flag="--mount-file",
        env_name="LMER_MOUNT_FILES",
        want_dir=False,
    )


def parse_dir_mount_specs(
    flag_values: list[str], env_value: str
) -> list[DirMountSpec]:
    """Parse and validate --mount-dir / LMER_MOUNT_DIRS entries.

    Grammar, ordering, dedup and fail-fast rules: :func:`_parse_mount_specs`.
    ``host`` must be an existing **directory** — lmer does not create it,
    because a typo would otherwise be answered with an empty directory the
    container happily mounts instead of an error naming the typo.

    The default mode is ``ro``, the same as ``--mount-file``: one rule to
    remember for one grammar, and the narrow default is the right one for a
    flag whose blast radius is a whole subtree rather than a single file. The
    cases that need to write — the platform's transcript mount is the first —
    say ``:rw`` out loud.
    """
    return _parse_mount_specs(
        flag_values,
        env_value,
        flag="--mount-dir",
        env_name="LMER_MOUNT_DIRS",
        want_dir=True,
    )


def conflicting_mount_destinations(file_specs: list, dir_specs: list) -> list[str]:
    """Container destinations claimed by both a file mount and a dir mount.

    The two parsers dedupe within themselves (last wins), but neither can see
    the other, and Docker/Podman refuse a duplicate ``-v`` destination
    outright — so without this check the only symptom of ``--mount-file
    a:/x`` plus ``--mount-dir b:/x`` is the runtime aborting the launch with
    its own message about a destination the user has to trace back to two
    different flags. Returns the clashing destinations, sorted; the caller
    turns that into the same fail-fast refusal an invalid entry gets.
    """
    return sorted(
        {spec.container for spec in file_specs} & {spec.container for spec in dir_specs}
    )


def _check_ssh_setup(container_home: Path, ssh_agent_enabled: bool) -> None:
    """
    Check SSH configuration and warn users if git operations may fail.

    Checks for:
    1. SSH agent forwarding (preferred method)
    2. SSH keys in container-home/.ssh/
    3. SSH keys in ~/.ssh/ that could be copied

    Args:
        container_home: Path to container-home directory
        ssh_agent_enabled: Whether SSH agent forwarding is enabled
    """
    if ssh_agent_enabled:
        # SSH agent is available, no warning needed
        return

    # Check for SSH keys in container-home
    container_ssh_dir = container_home / ".ssh"
    has_container_keys = False
    if container_ssh_dir.exists():
        for key_file in ["id_rsa", "id_ed25519", "id_ecdsa"]:
            if (container_ssh_dir / key_file).exists():
                has_container_keys = True
                break

    if has_container_keys:
        # Keys exist in container-home, should work
        success("✅ SSH keys found in container-home/.ssh/")
        return

    # No SSH agent and no keys in container-home - warn the user
    home_ssh_dir = Path.home() / ".ssh"
    has_host_keys = False
    if home_ssh_dir.exists():
        for key_file in ["id_rsa", "id_ed25519", "id_ecdsa"]:
            if (home_ssh_dir / key_file).exists():
                has_host_keys = True
                break

    warning("")
    warning("─" * 72)
    warning("⚠️  SSH not configured - git operations requiring authentication may fail")
    warning("─" * 72)
    warning("")
    warning("  Option 1: Use SSH agent (recommended)")
    warning("    eval $(ssh-agent)")
    warning("    ssh-add ~/.ssh/id_ed25519")
    warning("")
    warning("  Option 2: Copy SSH keys to container-home")
    if has_host_keys:
        warning("    cp -r ~/.ssh ~/.lmer/container-home/")
        warning("    chmod 700 ~/.lmer/container-home/.ssh")
        warning("    chmod 600 ~/.lmer/container-home/.ssh/id_*")
    else:
        warning("    (No SSH keys found - generate one first:")
        warning("      ssh-keygen -t ed25519")
        warning("    Then add the public key to your Git hosting service)")
    warning("")
    warning("─" * 72)
    warning("")


def _remap_taskdef_to_container(
    resolved_taskdef_dir: Path,
    external_pairs: list[tuple[Path, str]],
    repo_root: Path | None,
) -> tuple[str, str, str]:
    """Translate a host-resolved taskdef dir into container paths.

    Returns (taskdef_root, taskdef_dir, instructions_path) as they must
    appear INSIDE the container. Three cases:

    1. Under an external LMER_TASKDEF_PATHS entry -> the corresponding
       /Agents/taskdefs/<n> mount.
    2. Under the developer checkout's built-in taskdef/ -> the
       /Agents/global/taskdef mount. (Previously passed through as the raw
       host path, e.g. /home/<user>/.../taskdef — which doesn't exist in the
       container, so start.py silently dropped the built-in root from the
       include search path and shared fragments failed to resolve; issue #80.)
    3. Anything else passes through unchanged (already a container path).
    """
    for host_path, cpath in external_pairs:
        try:
            rel = resolved_taskdef_dir.relative_to(host_path)
            return cpath, f"{cpath}/{rel}", f"{cpath}/{rel}/instructions.txt"
        except ValueError:
            continue

    if repo_root is not None:
        try:
            rel = resolved_taskdef_dir.relative_to(repo_root / "taskdef")
            croot = "/Agents/global/taskdef"
            return croot, f"{croot}/{rel}", f"{croot}/{rel}/instructions.txt"
        except ValueError:
            pass

    return (
        str(resolved_taskdef_dir.parent),
        str(resolved_taskdef_dir),
        str(resolved_taskdef_dir / "instructions.txt"),
    )


def _redact_env_value(name: str, value: str) -> str:
    """Redact sensitive env var values, showing only a hint.

    Name rule and URL-credential strip come from work_repo.utils (issue
    #285); only the ``value[:4] + "***"`` hint for name-matched vars is
    display behavior of this surface.
    """
    if is_secret_env_name(name):
        if len(value) <= 4:
            return "***"
        return value[:4] + "***"
    return strip_url_credentials(value)


def _display_env_config_cli(
    host_lmer_vars: set[str],
    env_file_sources: dict[str, str],
) -> None:
    """Display LMER environment variables with their sources and exit.

    Shows all LMER_* vars from the host environment and .env files in a
    formatted table.
    """
    lmer_vars: dict[str, str] = {}
    for key, value in sorted(os.environ.items()):
        if key.startswith("LMER_"):
            lmer_vars[key] = value

    if not lmer_vars:
        info("No LMER_* environment variables set.")
        return

    name_width = max(len(k) for k in lmer_vars)
    rows = []
    for name, value in lmer_vars.items():
        redacted = _redact_env_value(name, value)
        if name in env_file_sources:
            source = env_file_sources[name]
        elif name in host_lmer_vars:
            source = "environment"
        else:
            source = "environment"
        rows.append((name, redacted, source))

    value_width = max(len(r[1]) for r in rows)

    print("---")
    print("⚙️  LMER Environment Configuration:\n")
    print(f"  {'Variable':<{name_width}}  {'Value':<{value_width}}  Source")
    print(f"  {'─' * name_width}  {'─' * value_width}  {'─' * 20}")
    for name, value, source in rows:
        print(f"  {name:<{name_width}}  {value:<{value_width}}  {source}")
    print("---")


def _announce_resource_limits(limits: "list", gpu=None, ci_baseline=None) -> None:
    """Say what bounds this container gets and what set each one (issue #453).

    Rendered from the plan the run arguments are built from, so the line and
    the arguments cannot disagree — including a flag the rootless-podman
    delegation gate dropped, which is absent from both. The source is the
    variable that answered, ``--memory``/``--cpus`` when a flag overrode the
    ladder, or ``default``; with three tiers per knob, which one won is the
    thing an operator cannot infer from the value alone.
    """
    if not limits and gpu is None and ci_baseline is None:
        return
    shown = [
        f"{limit.flag.lstrip('-')}={limit.value} ({limit.source})" for limit in limits
    ]
    if gpu is not None:
        shown.append(f"gpu={gpu.value or 'none'} ({gpu.source})")
    if ci_baseline is not None:
        value, source = ci_baseline
        shown.append(f"gate-ci-baseline={'on' if value == '1' else 'off'} ({source})")
    info(f"📐 Limits: {', '.join(shown)}")


def _display_sources_config_cli() -> None:
    """Render the canonical-sources block (sources.yaml, issue #105) for --show-env.

    Placement decisions (recorded for plan task 11):

    (a) Sibling renderer called from BOTH --show-env call sites (the normal
        path and the UnknownHarnessError fail-fast branch), NOT from inside
        _display_env_config_cli: that function returns early when no LMER_*
        vars are set, which would drop this block exactly when it is most
        useful (nothing overridden means the declared/unset picture IS the
        answer), and folding it in would break that function's tested
        no-LMER-vars no-output contract.

    (b) A source with neither a declaration nor an env var prints an explicit
        `unset-fallback` row rather than being omitted — issue #105's "loud
        when unset" requirement is satisfied HERE, in the host-side display.

    (c) A cached sources.yaml that fails validation is surfaced as
        `declared: invalid (<scrubbed reason>)` and the rows fall back to
        env-only/unset-fallback; nothing raises and the exit code is
        untouched — this renderer is strictly display-only. The recoverable
        half of the same channel — load_sources' warnings list — prints as
        `⚠️` lines under the `declared:` line, matching what bin/doctor and
        container startup surface for the same file.

    Declared side: the work repo's sources.yaml is read from the local clone
    cache (read_cached_repo_file_status — no network, fail-soft) against the
    RAW LMER_WORK_REPO env value, because the work-repo URL derivation further
    down main() has not run yet at either call site. A cache hit is labeled
    `(cached, possibly stale)`. A miss is phrased from the reason the reader
    reports rather than from the bare None the plain reader returns: a warm
    mirror whose repo carries no sources.yaml renders `declared: none (no
    sources.yaml in the work repo)` — the expected state until the cutover —
    and only a genuinely cold/unusable cache renders `unknown (work repo not
    cached)`. Parse/validate/normalize/redact all come from the core
    library (lmer_cli.container.sources) — nothing is reimplemented here;
    the normalizer decides declared-vs-env equality so an env var that
    silently matches the declaration is labeled `env-match` (the same origin
    the container banner prints for that row), never `env-override`.
    """
    work_repo_url = os.environ.get("LMER_WORK_REPO", "")
    config: "dict | None" = None
    # Recoverable findings from load_sources (unknown keys, reserved keys,
    # forward-compat notices). bin/doctor and container startup both print
    # them; dropping them here would render a typo'd declaration as a clean
    # `declared:` line on the one surface an operator uses to understand
    # what their declaration is doing (review finding).
    load_warnings: "list[str]" = []
    # A cached `_lmer.yaml` names the tier of every declared row (issue
    # #370 point 4): the manifest tier is read here from the same clone
    # cache, through the ONE manifest parser, and handed to E.loader's
    # tiered `load_sources` so precedence, the stale-legacy WARN and every
    # trust rule are the container's, not a re-implementation.
    manifest_tiered = False
    if not work_repo_url:
        declared_status = "unknown (LMER_WORK_REPO not set)"
    else:
        cached, cache_reason = read_cached_repo_file_status(
            work_repo_url, container_sources.SOURCES_FILENAME
        )
        cached_manifest, _manifest_reason = read_cached_repo_file_status(
            work_repo_url, container_sources.MANIFEST_FILENAME
        )
        manifest = None
        manifest_error: "Exception | None" = None
        if cached_manifest is not None:
            try:
                # Display-only, so a broken import renders rather than raises.
                # deferred: optional dependency — only a cached manifest needs the core; no manifest, no import
                from lmer_schema.manifest import parse_manifest

                manifest = parse_manifest(
                    cached_manifest, path=container_sources.MANIFEST_FILENAME
                )
            except Exception as exc:  # ManifestError, ImportError, ...
                manifest_error = exc
        if manifest_error is not None:
            # Decision (c) over the manifest tier: render, never raise. The
            # rows fall back to env-only/unset-fallback.
            declared_status = (
                f"invalid ({container_sources.scrub_credentials(str(manifest_error))})"
            )
        elif cached is None and manifest is None:
            if cache_reason == CACHE_FILE_ABSENT:
                # Warm mirror, no declaration — the expected state for every
                # work repo until the cutover lands, so it must not read as a
                # clone-cache problem the operator should go debug.
                declared_status = (
                    f"none (no {container_sources.SOURCES_FILENAME} in the work repo)"
                )
            elif cache_reason == CACHE_ERROR:
                declared_status = "unknown (work-repo cache unreadable)"
            else:
                declared_status = "unknown (work repo not cached)"
        else:
            tmp: "Path | None" = None
            try:
                # load_sources reads a file path; hand the cached bytes to it
                # as a throwaway sources.yaml so schema/credential/trust-rule
                # validation runs exactly as the container will run it. A miss
                # on sources.yaml leaves the temp path unwritten — the legacy
                # tier is then absent, exactly as on disk.
                with tempfile.TemporaryDirectory() as td:
                    tmp = Path(td) / container_sources.SOURCES_FILENAME
                    if cached is not None:
                        tmp.write_text(cached, encoding="utf-8")
                    config, raw_warnings = container_sources.load_sources(
                        tmp,
                        work_repo_url=work_repo_url,
                        manifest_sources=(
                            manifest.sources if manifest is not None else None
                        ),
                        manifest_path=container_sources.MANIFEST_FILENAME,
                    )
                # Warnings name the file by the path load_sources was given
                # — the throwaway copy. Rewrite it to the real filename, the
                # same substitution the SourcesConfigError branch does.
                load_warnings = [
                    w.replace(str(tmp), container_sources.SOURCES_FILENAME)
                    for w in raw_warnings
                ]
                manifest_tiered = manifest is not None
                tiers = set((config or {}).get("tiers", {}).values())
                if config is None:
                    # The loader found nothing declared in either tier (a
                    # cached manifest without a `sources:` section, and no
                    # legacy file): the same "none" wording the pre-#370
                    # output used, never a manifest declaration that is not
                    # there.
                    if cached is not None:
                        declared_status = "none (no sources declared in the work repo)"
                    elif cache_reason == CACHE_FILE_ABSENT:
                        declared_status = (
                            f"none (no {container_sources.SOURCES_FILENAME} in the work repo)"
                        )
                    elif cache_reason == CACHE_ERROR:
                        declared_status = "unknown (work-repo cache unreadable)"
                    else:
                        declared_status = "unknown (work repo not cached)"
                elif (container_sources.TIER_MANIFEST in tiers
                      and container_sources.TIER_LEGACY in tiers):
                    declared_status = (
                        "manifest and sources.yaml from work-repo cache "
                        "(cached, possibly stale)"
                    )
                elif container_sources.TIER_MANIFEST in tiers:
                    declared_status = (
                        "manifest from work-repo cache (cached, possibly stale)"
                    )
                else:
                    declared_status = (
                        "sources.yaml from work-repo cache (cached, possibly stale)"
                    )
            except container_sources.SourcesConfigError as exc:
                # Decision (c): render, never raise. Strip the throwaway temp
                # path so the message names the real file, and scrub before
                # printing — the reason may echo (scrubbed) repo URLs.
                reason = str(exc)
                if tmp is not None:
                    reason = reason.replace(str(tmp), container_sources.SOURCES_FILENAME)
                declared_status = f"invalid ({container_sources.scrub_credentials(reason)})"
                config = None
            except Exception as exc:  # display-only: never break --show-env
                declared_status = f"unknown (cache read failed: {type(exc).__name__})"
                config = None

    declared_sources = (config or {}).get("sources") or {}
    declared_tiers = (config or {}).get("tiers") or {}
    rows = []
    for label, name, key, env_var in (
        ("taskdef repo", "taskdef", "repo", "LMER_TASKDEF_REPO"),
        ("taskdef ref", "taskdef", "ref", "LMER_TASKDEF_REF"),
        ("napkin repo", "napkin", "repo", "LMER_NAPKIN_REPO"),
    ):
        declared_value = (declared_sources.get(name) or {}).get(key)
        env_value = (os.environ.get(env_var) or "").strip() or None
        if declared_value and env_value:
            if key == "repo":
                # Core predicate, not string equality: `git@h:o/x.git` and
                # `https://h/o/x` are the same source, not an override — and
                # so is the URL the container would derive from the
                # declaration, which is what an SSH-on-a-custom-port work
                # repo produces. Shared with the container's matrix so both
                # surfaces call this row env-match.
                same = container_sources.env_matches_declared(
                    env_value, declared_value, work_repo_url
                )
            else:
                same = env_value == declared_value  # refs are opaque strings
            if same:
                value, origin = declared_value, "env-match (declaration cached, possibly stale)"
            else:
                value, origin = env_value, "env-override"
        elif declared_value:
            value = declared_value
            if manifest_tiered:
                # The tier is named whenever a manifest is present (spec §8):
                # the loader's ONE origin spelling, with the cache caveat.
                origin = container_sources.declared_origin_label(
                    declared_tiers.get(name, container_sources.TIER_LEGACY),
                    "cached",
                    "possibly stale",
                )
            else:
                origin = "declared (cached, possibly stale)"
        elif env_value:
            value, origin = env_value, "env-only"
        else:
            # Decision (b): loud, explicit unset row.
            value, origin = "(unset)", "unset-fallback"
        # A valid config never carries credentials (load_sources refuses),
        # but env values can — scrub every rendered value unconditionally
        # (core-library redactor; _redact_env_value is the same host-side
        # pattern but misses scp-form credentials).
        rows.append((label, container_sources.scrub_credentials(value), origin))

    name_width = max(len(r[0]) for r in rows)
    value_width = max(len(r[1]) for r in rows)
    print("---")
    print("📚 Canonical Sources:\n")
    print(f"  declared: {declared_status}\n")
    for warning in load_warnings:
        # Scrub unconditionally, the same rule the rows below follow.
        print(f"  ⚠️  {container_sources.scrub_credentials(warning)}")
    if load_warnings:
        print()
    print(f"  {'Source':<{name_width}}  {'Value':<{value_width}}  Origin")
    print(f"  {'─' * name_width}  {'─' * value_width}  {'─' * 20}")
    for label, value, origin in rows:
        print(f"  {label:<{name_width}}  {value:<{value_width}}  {origin}")
    print("---")


#: A session id names ONE directory inside a run's group, so it is a path
#: SEGMENT: no separator, no empty/`.`/`..` segment, and no character
#: ``Path.glob`` would expand rather than compare. Same rule as
#: ``work_repo.run_state._usable_session_id`` and its container-side twin
#: ``clone_and_exec._usable_session_id`` — deliberately, because a value those
#: two refuse opens no record at all in the container, and the operator is
#: HERE. No grammar beyond it: the ``--<name>`` half of a session dir is
#: unconstrained (docs/SCHEMA.md), so a regex would refuse ids that are on
#: disk right now. The agreement is pinned by tests/test_lmer_cli_args.py,
#: this module having no work_repo import to share the rule through.
_UNSAFE_SESSION_SEGMENTS = frozenset({"", ".", ".."})
_SESSION_PATTERN_CHARS = "*?["


def _session_id_refusal(session: str) -> str | None:
    """Why *session* cannot name a session directory, or None when it can.

    Whitespace is refused rather than stripped: the value becomes a directory
    name, and silently rewriting an id sends the launch to a directory the
    operator did not type. (The platform strips its own field before it
    reaches this flag, so this refusal answers a hand-typed value.)
    """
    if not session.strip():
        return "it is empty"
    if session != session.strip():
        return "it is padded with whitespace"
    if "/" in session or "\\" in session:
        return "it contains a path separator"
    if session in _UNSAFE_SESSION_SEGMENTS:
        return "it is not a name"
    if session.startswith("."):
        # Reserved, not merely unsafe — and the only shape here that opens a
        # record instead of failing to. Every walk over a group skips
        # dot-children (a dot-dir there is a `.new-*` seed mid-rename), so a
        # `.hidden` id writes a complete record that `work sessions`, the
        # fleet view and every later resume are blind to, while the session
        # itself sees nothing wrong. Refused in all three copies of this rule
        # rather than only here: `work session-start --session .hidden`
        # reaches the same address by the kernel's own explicit route.
        return "it starts with a dot, which names a seed directory"
    if any(char in session for char in _SESSION_PATTERN_CHARS):
        return "it contains a wildcard character"
    return None


def _continuation_env(ns: argparse.Namespace) -> dict[str, str | None]:
    """Environment handshake for schema-aware direct CLI continuation."""
    selected = ns.session if ns.session else None
    return {
        "LMER_SESSION_ID": selected,
        "LMER_SESSION_IS_SOURCE": "1" if selected else None,
        "LMER_SOURCE_SESSION_ID": ns.source_session or None,
        "LMER_ALLOW_UNSIGNED_SOURCE": (
            ns.allow_unsigned_source if ns.allow_unsigned_source else None
        ),
        # A pre-minted execution uid, from the flag ONLY — the same flag-only
        # rule as the entries above: a stale exported uid would make every
        # launch of a container reuse one record's identity.
        "LMER_LAUNCH_UID": getattr(ns, "launch_uid", None) or None,
    }


def _launch_session_metadata(
    *, harness: str, model: str | None, effort: str | None, config: dict
) -> dict:
    """Compose host metadata through the shared session-record policy."""
    return session_record.compose_launch_metadata(
        session_record.provided_metadata(),
        harness=harness,
        model=model,
        effort=effort,
        config=config,
    )


def _validate_parsed_args(ns: argparse.Namespace) -> int | None:
    """Validate mutually exclusive / dependent options on a parsed namespace.

    Returns an exit code on failure, or None when the namespace is valid.
    Runs after preset application so preset-supplied args are validated the
    same as explicit ones.
    """
    if ns.branch and ns.ref:
        error("Cannot specify both --branch and --ref")
        return 2
    if ns.workspace_volume and ns.workspace_bind:
        error("Cannot specify both --workspace-volume and --workspace-bind")
        return 2
    if ns.no_clone and not ns.exec_mode:
        error("--no-clone requires --exec mode")
        return 2
    if ns.user and ns.match_uid:
        error("Cannot specify both --user and --match-uid")
        return 2
    if ns.root_userns and (ns.user or ns.match_uid):
        error("Cannot combine --root-userns with --user or --match-uid")
        return 2
    if ns.no_task and not ns.exec_mode:
        error("--no-task requires --exec mode")
        return 2
    # --repo names the repository explicitly (A1-wave0.host-export). The
    # refusals that need the positional classified ('two repositories named')
    # live in main(), where the classifier runs; these need only the flags.
    if ns.repo is not None:
        if not ns.repo.strip():
            error("--repo requires a non-empty URL or path")
            return 2
        if ns.checkout:
            error("Cannot specify both --repo and --checkout")
            return 2
        if ns.no_clone:
            error("Cannot specify both --repo and --no-clone")
            return 2
        if ns.no_task:
            error("--repo needs a task and is refused with --no-task")
            return 2
        if get_bool_env("LMER_NO_REPO"):
            error("Cannot specify both --repo and LMER_NO_REPO")
            return 2
    if not ns.no_task and not ns.task and not ns.show_env:
        error("Task type is required unless --no-task is specified")
        return 2
    # --name / --goal are recorded by `work session-start` inside the
    # container; the host refuses only what the container could not record
    # (normalisation itself stays in the container, with `work name`).
    if ns.run_name is not None:
        if not ns.run_name.strip():
            error("--name requires a non-empty value")
            return 2
        if "\n" in ns.run_name or "\r" in ns.run_name:
            error("--name must be a single line (a run name is a kebab-case label)")
            return 2
    if ns.run_goal is not None and not ns.run_goal.strip():
        error("--goal requires a non-empty value")
        return 2
    if ns.run_name is not None or ns.run_goal is not None:
        if ns.no_task:
            error("--name/--goal need a run to record them in and are refused with --no-task")
            return 2
        if ns.exec_mode:
            warning(
                "ℹ️  --name/--goal are recorded by work session-start; "
                "this --exec command must run it for them to apply"
            )
    if ns.service and not ns.checkout:
        error("--service requires --checkout (path to local source checkout)")
        return 2
    if ns.service_group and not ns.checkout:
        error(
            "--service-group requires --checkout (path to local source checkout)"
        )
        return 2
    for flag, dest, validator, hint in (
        ("--memory", "memory", _valid_memory, MEMORY_HINT),
        ("--cpus", "cpus", _valid_cpus, CPUS_HINT),
    ):
        # The other posture over the same grammar (issue #453): the environment
        # tiers warn and fall through, because a typo in a file must never stop
        # a session starting, while a flag is an attached caller saying what
        # they want — and running with some other bound is the failure.
        #
        # Blank is the exception, and it is the same rule the tiers follow: a
        # wrapper that passes --memory "$OVERRIDE" with the variable unset has
        # expressed no preference, and refusing to launch over an empty string
        # would be answering a question nobody asked.
        raw = (getattr(ns, dest) or "").strip()
        if not raw:
            setattr(ns, dest, None)
            continue
        accepted = validator(raw)
        if accepted is None:
            error(f"{flag} {getattr(ns, dest)!r} is not a usable value: it {hint}")
            return 2
        # The accepted spelling, not what was typed: the validators may
        # normalise (as _valid_pids_limit does), and the env path already
        # passes what the validator returned.
        setattr(ns, dest, accepted)
    if ns.session is not None:
        # Refused here, where the operator can read it. The value becomes a
        # directory name in the work repo (D22), and every layer below this
        # one degrades quietly instead of refusing: the container-side guards
        # treat an unusable id as NO launch id, so the session would start,
        # open no record, and look normal until nothing could be resumed.
        refusal = _session_id_refusal(ns.session)
        if refusal is not None:
            error(
                f"--session {ns.session!r} is not a usable session id: {refusal}. "
                "A session id names ONE launch inside the run's group "
                "(<project>/_runs/<slug>/<session-id>), never a path or a "
                "pattern — copy one from `work sessions` or from the fleet "
                "view's row for the run."
            )
            return 2
    if getattr(ns, "launch_uid", None) is not None:
        if ns.session:
            error(
                "--launch-uid cannot be combined with --session: a schema-1 "
                "continuation already names the record it writes"
            )
            return 2
        if session_record.UID_RE.fullmatch(ns.launch_uid or "") is None:
            error(
                f"--launch-uid {ns.launch_uid!r} is not a usable execution uid: "
                "it must be 32 lowercase hex characters"
            )
            return 2
    if ns.source_session is not None:
        if ns.session:
            error("--session and --source-session are mutually exclusive")
            return 2
        refusal = _session_id_refusal(ns.source_session)
        if refusal is not None:
            error(f"--source-session is not a usable session id: {refusal}")
            return 2
    if ns.allow_unsigned_source is not None:
        selected_source = ns.source_session or ns.session
        if not selected_source:
            error("--allow-unsigned-source requires --session or --source-session")
            return 2
        if ns.allow_unsigned_source != selected_source:
            error(
                "--allow-unsigned-source must exactly match --session or --source-session; use the "
                "source UID shown in the unsigned-session refusal"
            )
            return 2
    return None


def resolve_service_target(
    runtime: str, service: str | None, service_group: str | None
) -> tuple[str | None, str | None, str | None]:
    """``(container id, workdir, service name)`` a service-mode launch starts on.

    A group is resolved here even when nothing is targeted yet, so a project the
    host cannot see fails at launch rather than at the agent's first
    ``target-switch``. Its members are deliberately *not* forwarded to the
    container: ``target-switch`` re-reads them, which is what keeps a member
    that restarted under a new container id reachable.

    ``(None, None, None)`` is the group-without-``--service`` answer — a
    targetless start, which ``target-exec`` reports by naming the switch rather
    than by picking a member nobody chose.
    """
    if service_group:
        members = resolve_group(runtime, service_group)
        if not service:
            return None, None, None
        member = member_of(members, service)
        return member.container_id, member.workdir, member.service
    container_id = resolve_container(runtime, service)
    return container_id, inspect_container_workdir(runtime, container_id), service


def _display_presets_cli() -> int:
    """Handle --list-presets: print the configured presets and exit code.

    Env values are shown as key names only — a preset may carry credentials
    (same reason --show-env redacts), and the keys are what identify it.
    """
    presets_file = os.environ.get(PRESETS_FILE_ENV, "").strip()
    presets = load_presets()
    # Direct print (not the verbose-gated info()): the listing IS the
    # command's output, mirroring _display_env_config_cli.
    if not presets:
        if presets_file:
            print(f"No presets loaded from {presets_file} (missing file or no valid entries).")
        else:
            print(f"No presets configured ({PRESETS_FILE_ENV} is not set).")
        return 0
    print(f"🎛️  Presets ({presets_file}):")
    for name in sorted(presets):
        preset = presets[name]
        parts = []
        if preset.checkout:
            parts.append(f"checkout={preset.checkout}")
        if preset.service:
            parts.append(f"service={preset.service}")
        if preset.service_group:
            parts.append(f"service_group={preset.service_group}")
        if preset.env:
            parts.append("env=" + ",".join(sorted(preset.env)))
        if preset.args:
            parts.append("args=" + shlex.join(preset.args))
        print(f"  {name}" + (f"  [{' '.join(parts)}]" if parts else ""))
    return 0


def _selected_task_id(ns: argparse.Namespace) -> str | None:
    """The taskdef id this invocation selected, or ``None`` under --no-task.

    One home for the ``ns.task`` / ``ns.no_task`` pairing: under ``--no-task``
    argparse still binds a positional to ``ns.task`` (with ``--exec true`` the
    task is literally ``"true"``), so every consumer must gate on ``no_task``
    rather than reading ``ns.task`` alone.
    """
    return ns.task if not ns.no_task else None


def _release_credential_env(
    runtime: str, task_id: str | None
) -> tuple[dict[str, str | None], list[str], str | None]:
    """The release credential scoping gate: env entries + signing-key mount.

    Returns ``(env_entries, mount_args, fatal)``.

    ``env_entries`` ALWAYS carries both production keys. For a release
    session (``task_id == RELEASE_TASK_ID``) they hold the host PAT value
    and the container key path; for every other session they are seeded
    ``None``, which is load-bearing: a key already present in the container
    env dict is skipped by both the .env merge and the preset seeding loop
    in main() (the LMER_NAPKIN_TOKEN precedent), so a PAT sitting in a cwd
    ``.env`` / ``~/.lmer/.env`` / ``--env-file`` or a preset's env can never
    leak into a non-release container.

    ``fatal`` is a reason string when a *configured* signing key is
    rejected by the mount guard — the caller must abort (a release session
    that cannot sign must not start, the --mount-file fail-fast precedent).
    Unconfigured credentials are not fatal: leg-1 release work (version
    bump, MR) needs neither.
    """
    entries: dict[str, str | None] = {
        RELEASE_GITHUB_TOKEN_ENV: None,
        RELEASE_SIGNING_KEY_ENV: None,
    }
    if task_id != RELEASE_TASK_ID:
        return entries, [], None
    entries[RELEASE_GITHUB_TOKEN_ENV] = os.environ.get(RELEASE_GITHUB_TOKEN_ENV) or None
    host_key = (os.environ.get(RELEASE_SIGNING_KEY_ENV) or "").strip()
    if not host_key:
        return entries, [], None
    mount_args, skip_reason = build_release_signing_key_mount(runtime, Path(host_key))
    if skip_reason:
        return entries, [], skip_reason
    entries[RELEASE_SIGNING_KEY_ENV] = CONTAINER_RELEASE_SIGNING_KEY_PATH
    return entries, mount_args, None


def _ansible_gpg_env(
    runtime: str, task_id: str | None
) -> tuple[dict[str, str | None], list[str], str | None]:
    """The gpg-agent forwarding gate: env entries + socket/keyring mounts.

    Returns ``(env_entries, mount_args, fatal)`` — the shape
    :func:`_release_credential_env` established, for the same reasons.

    ``env_entries`` ALWAYS carries both keys. For an ansible session with
    the feature configured they hold the **container** paths; everywhere
    else they are seeded ``None``, which is what blocks the leak: a key
    already in the container env dict is skipped by both the .env merge and
    the preset seeding loop in main(), so neither a ``.env`` nor a preset
    can hand another taskdef a socket to sign with.

    ``fatal`` is a reason string when the feature is *configured* but
    unusable. Aborting is the point: a vault session that cannot decrypt
    must not start, and a gpg failure deep inside a playbook run is worse
    to diagnose than a refusal at the door (the release signing key's
    fail-fast precedent). An unset variable is not an error — the ansible
    session simply launches without gpg — and no other taskdef ever
    evaluates the guard.
    """
    entries: dict[str, str | None] = {
        ANSIBLE_GPG_AGENT_ENV: None,
        ANSIBLE_GPG_PUBRING_ENV: None,
    }
    if task_id != ANSIBLE_TASK_ID:
        return entries, [], None
    configured = (os.environ.get(ANSIBLE_GPG_AGENT_ENV) or "").strip()
    if not configured:
        return entries, [], None
    socket_path, reason = resolve_gpg_extra_socket(configured)
    if reason:
        return entries, [], reason
    pubring_path, reason = resolve_gpg_pubring(os.environ.get(ANSIBLE_GPG_PUBRING_ENV))
    if reason:
        return entries, [], reason
    entries[ANSIBLE_GPG_AGENT_ENV] = CONTAINER_GPG_AGENT_SOCKET_PATH
    entries[ANSIBLE_GPG_PUBRING_ENV] = container_gpg_pubring_path(pubring_path)
    return entries, build_gpg_agent_mounts(runtime, socket_path, pubring_path), None


def _warn_on_scoped_preset_tier_override(
    preset_source: str | None,
    task_id: str | None,
    early_env_file_sources: dict[str, str],
) -> None:
    """Warn when a file-sourced scoped var outranks an *exported* LMER_PRESET.

    Selection is by specificity, not by source tier: ``LMER_<TASK>_PRESET``
    beats ``LMER_PRESET`` wherever each came from. That is the intended
    contract (a per-task override in ``~/.lmer/.env`` is the feature), but it
    has one genuinely surprising case — a scoped var sitting in a ``.env``
    file silently outranking a ``LMER_PRESET`` the operator exported on this
    very command line. Everywhere else lmer resolves file-vs-export in the
    export's favor, so that case gets a warning naming both sides and the
    per-invocation escape hatch.

    Deliberately narrow: nothing is said when both selectors come from the
    same tier (the documented "global default + per-task override in one
    .env" setup would otherwise warn on every run), nor when the scoped var
    is itself exported (an export outranking an export is unsurprising).
    """
    scoped_var = task_preset_env_name(task_id)
    if not scoped_var or preset_source != scoped_var:
        return
    # Via preset_env_value so the blank-is-unset rule has exactly one home —
    # a whitespace-only LMER_PRESET must not warn about being "overridden"
    # when the selector itself reads it as unset.
    generic = preset_env_value(PRESET_ENV)
    if not generic:
        return
    scoped_from_file = early_env_file_sources.get(scoped_var)
    generic_from_file = PRESET_ENV in early_env_file_sources
    if not scoped_from_file or generic_from_file:
        return
    warning(
        f"⚠️  {scoped_var} (from {scoped_from_file}) overrides the exported "
        f"{PRESET_ENV}={generic} for this task — taskdef-scoped selection wins "
        f"regardless of where each value came from. Use --preset to choose "
        f"per invocation."
    )


def _resolve_and_apply_preset(
    ns: argparse.Namespace,
    rest: list[str],
    argv: list[str],
    early_env_file_sources: dict[str, str],
) -> tuple[argparse.Namespace, list[str], dict[str, str]] | int:
    """Resolve the selected startup preset and apply it to the invocation.

    Selection is ``--preset`` > ``LMER_<TASK>_PRESET`` > ``LMER_PRESET``
    (flag-beats-env matching the --harness/LMER_HARNESS convention; the
    taskdef-scoped var beats the generic one because it is the more specific
    selector — issue #140). Specificity, not source tier: a .env-sourced
    LMER_<TASK>_PRESET outranks even an exported LMER_PRESET, which is the
    point of a per-task default but is warned about (see
    _warn_on_scoped_preset_tier_override) because it is the one case where
    file-beats-export contradicts the rest of the CLI. Must run after the
    early .env load (so
    LMER_PRESET/LMER_<TASK>_PRESET/LMER_PRESETS_FILE from .env files work)
    and before argument validation / harness resolution (so preset args and
    env take full effect). ``ns.task`` is already parsed here and a preset
    cannot change it (preset args reject positional tokens below), so the
    taskdef id the lookup uses is stable. The explicit invocation always
    wins over the preset: preset tokens are PREPENDED to argv (argparse
    last-wins). Preset env replaces only absent or file-default values,
    including defaults attributed by the platform daemon; exports stay protected.

    Returns the (possibly re-parsed) ``(ns, rest, applied_env)`` on success —
    ``applied_env`` being the preset env entries that actually won host-side,
    for the caller to seed into the container env dict — or an exit code on
    failure. No preset selected returns the inputs unchanged.
    """
    # Report only this invocation's resolved preset, never an inherited label.
    os.environ.pop("LMER_SESSION_PRESET", None)
    # --no-task runs have no taskdef id, so only the flag and LMER_PRESET can
    # select for them.
    task_id = _selected_task_id(ns)
    preset_name, preset_source = select_preset_name(ns.preset, task_id)
    applied_env: dict[str, str] = {}
    if not preset_name:
        return ns, rest, applied_env

    _warn_on_scoped_preset_tier_override(
        preset_source, task_id, early_env_file_sources
    )

    presets = load_presets()
    preset = presets.get(preset_name)
    if preset is None:
        available = ", ".join(sorted(presets)) or "(none)"
        # Name the selector: a stale LMER_REVIEW_PRESET in ~/.lmer/.env is
        # otherwise indistinguishable from a mistyped flag.
        error(
            f"❌ Unknown preset '{preset_name}' (selected by {preset_source}). "
            f"Available presets: {available}"
        )
        if not os.environ.get(PRESETS_FILE_ENV, "").strip():
            error(f"   ({PRESETS_FILE_ENV} is not set, so no presets can load)")
        return 2

    # Preset args must be tokens the lmer parser fully recognizes as flags on
    # a CLI invocation: a bare positional would silently bind as the task
    # (demoting the user's own task to a target), a literal `--` would
    # truncate parsing there and dump the rest of the command line into exec
    # args, and an unknown/typo'd flag would fall through to `rest` — silently
    # dropped, or prepended to the exec command under --exec. (The Slack spawn
    # path instead appends args to a fixed `lmer chat <permalink>` command,
    # where extra tokens are meaningful.)
    if "--" in preset.args:
        error(f"❌ Preset '{preset.name}' args may not contain '--' on a CLI invocation")
        return 2
    probe_ns, probe_rest = parse_args(list(preset.args))
    # Unknown-token check first: a typo'd flag's value binds as a positional
    # too, and the unknown flag is the actionable half of that message.
    if probe_rest:
        error(
            f"❌ Preset '{preset.name}' args contain tokens lmer does not "
            f"recognize ({shlex.join(probe_rest)}) — preset args must be "
            f"known lmer flags on a CLI invocation"
        )
        return 2
    leaked = ([probe_ns.task] if probe_ns.task else []) + list(probe_ns.target)
    if leaked:
        error(
            f"❌ Preset '{preset.name}' args contain positional tokens "
            f"({shlex.join(leaked)}) — preset args must be flags on a CLI invocation"
        )
        return 2

    preset_tokens = preset.cli_tokens()
    if preset_tokens:
        original_env_file = ns.env_file
        ns, rest = parse_args(preset_tokens + argv)
        if ns.verbose or ns.debug:
            os.environ["LMER_VERBOSE"] = "1"
        # The caller's early .env load already ran off the command line's
        # --env-file; honoring one smuggled in via preset args would require
        # re-running it with murky precedence, so reject it.
        if ns.env_file != original_env_file:
            warning(
                "⚠️  --env-file inside preset args is ignored — "
                "pass it on the lmer command line instead"
            )
            ns.env_file = original_env_file

    for key, value in preset.env.items():
        if key not in os.environ or key in early_env_file_sources:
            os.environ[key] = value
            early_env_file_sources[key] = f"preset ({preset.name})"
            applied_env[key] = value
    os.environ["LMER_SESSION_PRESET"] = preset.name
    applied_env.pop("LMER_SESSION_PRESET", None)
    details = []
    # The flag is visible in the command line the operator just typed; an env
    # var (especially a .env-sourced one) is not, so attribute those.
    if preset_source and preset_source != "--preset":
        details.append(f"via {preset_source}")
    if applied_env:
        details.append(f"env: {', '.join(sorted(applied_env))}")
    info(
        f"🎛️  Preset: {preset.name}"
        + (f" ({'; '.join(details)})" if details else "")
    )
    return ns, rest, applied_env


def _load_aliases() -> dict[str, str]:
    """The operator's alias table, complaining once about a malformed entry.

    Unprefixed: the warning names ``LMER_MODEL_ALIASES`` itself, and tagging it
    with whichever reader loaded first said the typo was an ``--agents`` problem.
    """
    aliases, notes = load_model_aliases(os.environ)
    for note in notes:
        warning(f"⚠️  {note}")
    return aliases


def _resolve_agents_cli(ns: argparse.Namespace) -> dict[str, dict] | None | int:
    """Resolve the ``--agents``/``LMER_AGENTS`` fan-out selection (issue #130).

    Selection is ``--agents`` > ``LMER_AGENTS`` (matching --preset/
    LMER_PRESET). Must run after the early .env load and preset application
    so an LMER_AGENTS from a .env file or a preset's env is honored. Names
    are resolved host-side against LMER_PRESETS_FILE — the trust model keeps
    the presets file itself out of the container; the session's agent can
    only spawn what was named at launch.

    Returns the resolved ``{name: {"env": {...}, "prompt": ...?}}`` mapping
    (the ``LMER_SPAWN_AGENTS_CONFIG`` shape, selection-ordered), ``None`` when no
    selection was made, or an exit code on failure (unknown name / empty
    selection — spawning fewer agents than asked is never silent).
    """
    selection = ns.agents or os.environ.get(AGENTS_ENV) or ""
    if not selection.strip():
        return None
    resolved, agent_warnings, agents_error = resolve_agent_presets(selection, load_presets())
    for note in agent_warnings:
        warning(f"⚠️  --agents: {note}")
    if agents_error is not None:
        error(f"❌ --agents: {agents_error}")
        if not os.environ.get(PRESETS_FILE_ENV, "").strip():
            error(f"   ({PRESETS_FILE_ENV} is not set, so no presets can load)")
        return 2
    # A child's model too (issue #309), expanded where the preset stops being a
    # file and becomes this launch's config — what the credential-mount union and
    # spawn-harness both read.
    aliases = _load_aliases()
    if aliases:
        for agent_name, entry in resolved.items():
            overlay = entry.get("env") or {}
            named = overlay.get(LLM_NAME_ENV)
            expanded = expand_model_alias(named, aliases)
            if expanded and expanded != named:
                overlay[LLM_NAME_ENV] = expanded
                info(f"🎛️  Model alias ({agent_name}): {named} → {expanded}")
    info(f"🤖 Agents: {','.join(resolved)}")
    return resolved


def _agents_child_harnesses(
    resolved: dict[str, dict], session_harness: Harness
) -> list[Harness]:
    """The extra harnesses the ``--agents`` fan-out children imply (issue #131).

    Mirrors spawn-harness's in-container selection host-side, before the
    container starts (the same fail-early rationale as
    ``resolve_agent_presets``): for each resolved agent, the overlay merged
    over the session's effective ``LMER_HARNESS``/``LMER_LLM_NAME`` feeds
    :func:`lmer_cli.harness.implied_harness_name`. Credential mounts are
    keyed to the session harness only, so a child routed elsewhere needs its
    harness's credential files mounted too — the returned (deduplicated,
    selection-ordered) non-session harnesses union into
    ``build_user_mounts``.

    Also warns when an implied non-session child harness has no mountable
    credential file on the host at all — the child may fail to authenticate.
    A warning, never an error: a mount is no promise of working auth, and
    keys-via-env harnesses (pi) can authenticate without one. The session's
    own harness is exempt, mirroring ``warn_missing_credentials``'s
    spawn-time exemption: its credential state IS the session's (a plain
    launch never warns about it, and a same-harness child authenticates
    exactly as well as the session).
    """
    inherited = {
        HARNESS_ENV: session_harness.name,
        LLM_NAME_ENV: os.environ.get(LLM_NAME_ENV) or "",
    }
    home = Path.home()
    extra: dict[str, Harness] = {}
    registry = known_harnesses()
    for agent_name, entry in resolved.items():
        overlay = entry.get("env") or {}
        name = implied_harness_name(overlay, {**inherited, **overlay})
        if name not in registry:
            # resolve_agent_presets already rejects unknown explicit names;
            # unreachable in practice, but a mount union must never crash.
            continue
        if name == session_harness.name:
            continue
        child = registry[name]
        missing = missing_credential_mounts(
            child, lambda cred: (home / cred.host_path).exists()
        )
        if missing:
            paths = " / ".join(f"~/{cred.host_path}" for cred in missing)
            warning(
                f"⚠️  --agents: agent '{agent_name}' routes to {name} but "
                f"{paths} does not exist on the host — "
                "the child may fail to authenticate"
            )
        extra.setdefault(name, child)
    return list(extra.values())


def _apply_model_selection(
    explicit: str | None, env_sources: dict[str, str]
) -> str | None:
    """Make ``--model`` the session's ``LMER_LLM_NAME``. Returns the effective model.

    The flag spelling of a variable that until now could only be exported. It
    wins over the environment, matching the ``--harness``/``LMER_HARNESS``
    convention, and it is applied to ``os.environ`` rather than carried in the
    namespace because that is where every consumer already reads it: the harness
    autoselection hint (:func:`lmer_cli.harness.resolve_harness_selection`), the
    fan-out children's implied harnesses (:func:`_agents_child_harnesses`) and
    the container env dict all read the variable, so a second channel would be a
    second answer.

    Runs after preset application so the flag wins, and before harness
    resolution so the model can select the harness that serves it.

    Blank is not a value: ``--model ''`` leaves the environment alone, the same
    way an empty ``--harness`` falls through to ``LMER_HARNESS``. Surrounding
    whitespace is stripped (the value ends up as a container env var and then as
    a harness's ``--model`` argument, where a stray space is a model name nobody
    has); the case is not touched — model ids are case-sensitive to the harness
    that resolves them.

    *env_sources* is the ``--show-env`` attribution map: without an entry the
    table would report a flag-set value as coming from the environment, which is
    the one thing that table exists to tell apart.

    An operator alias (``LMER_MODEL_ALIASES``, issue #309) is expanded here rather
    than at the flag, because a preset's or a ``.env``'s ``sol`` has to expand
    too — and before harness resolution reads it. The expanded value lands in
    ``os.environ``, so nothing downstream re-derives it.
    """
    aliases = _load_aliases()
    model = (explicit or "").strip()
    if not model:
        inherited = os.environ.get(LLM_NAME_ENV) or None
        expanded = expand_model_alias(inherited, aliases)
        if expanded and expanded != inherited:
            os.environ[LLM_NAME_ENV] = expanded
            prior = env_sources.get(LLM_NAME_ENV, "environment")
            env_sources[LLM_NAME_ENV] = f"{prior} → alias {inherited}"
            info(f"🎛️  Model alias: {inherited} → {expanded}")
        return expanded
    expanded = expand_model_alias(model, aliases) or model
    if expanded != model:
        info(f"🎛️  Model alias: {model} → {expanded}")
    os.environ[LLM_NAME_ENV] = expanded
    env_sources[LLM_NAME_ENV] = (
        "--model flag" if expanded == model else f"--model flag → alias {model}"
    )
    return expanded


def _apply_dispatch_aliases(env_sources: dict[str, str]) -> None:
    """Expand an alias in each ``LMER_DISPATCH_<LANE>`` value (issue #309).

    Into ``os.environ``, not into the container env dict: the variable is what
    every reader consults (the ``--show-env`` table, the in-container lane
    renderer), and it keeps those five entries the plain passthroughs
    ``tests/test_dispatch_env.py`` guards.
    """
    aliases = _load_aliases()
    if not aliases:
        return
    for lane in DISPATCH_LANE_VARS:
        current = os.environ.get(lane)
        expanded = expand_dispatch_value(current, aliases)
        if expanded and expanded != current:
            os.environ[lane] = expanded
            prior = env_sources.get(lane, "environment")
            env_sources[lane] = f"{prior} → alias {current}"
            info(f"🎛️  Model alias ({lane}): {current} → {expanded}")


def _resolve_afk_timeout_ms(explicit_value: str | None, slack_bridged: bool) -> str | None:
    """Resolve the CLAUDE_AFK_TIMEOUT_MS value forwarded into the container.

    An explicit host-side value always wins. When it is unset and the
    session is Slack-bridged, default to 5 minutes (300000 ms) so an idle
    session pings the thread instead of sitting silent; plain terminal
    sessions stay untouched (None).
    """
    if explicit_value is not None:
        return explicit_value
    return "300000" if slack_bridged else None


def _handle_build(argv: list[str]) -> int:
    """
    Handle the 'lmer build' subcommand.

    Args:
        argv: Arguments after 'build' (e.g., ['--no-pull', '--force'])

    Returns:
        Exit code (0 for success, non-zero for errors)
    """
    parser = argparse.ArgumentParser(prog="lmer build", description="Build the container image")
    parser.add_argument("--no-pull", action="store_true", help="Don't pass --pull to docker build (skip refreshing base image layers)")
    parser.add_argument("--force", action="store_true", help="Delete existing image before building")
    parser.add_argument("--local", metavar="PATH", type=Path, help="Path to local repo checkout to build from (useful when installed via pip/uv)")
    parser.add_argument("--update-claude", action="store_true", help="Force re-install of Claude Code CLI (bust Docker cache for that layer); alias for --update-harness claude")
    parser.add_argument("--update-harness", action="append", metavar="NAME", dest="update_harness", help=f"Force re-install of a harness CLI (bust Docker cache for its install layer). Repeatable. Known: {', '.join(sorted(HARNESSES))}, or 'all'")
    args = parser.parse_args(argv)

    # Validate the names as REQUESTED before expanding 'all' — otherwise
    # `--update-harness all --update-harness <typo-or-user-name>` would be
    # silently swallowed by the expansion instead of rejected.
    update_harnesses = set(args.update_harness or [])
    unknown = update_harnesses - set(HARNESSES) - {"all"}
    if unknown:
        # User-installed harnesses have no Containerfile install layer to
        # bust — their runner owns the CLI install (wipe the cache instead).
        user_named = unknown & set(load_user_harnesses())
        if user_named:
            error(
                f"❌ --update-harness does not apply to user-installed harness(es) "
                f"{', '.join(sorted(user_named))}: their runner installs the CLI at "
                f"session start — clear {DEFAULT_HARNESS_CACHE_DIR}/<name> to force a reinstall"
            )
            return 2
        error(f"❌ Unknown harness(es) for --update-harness: {', '.join(sorted(unknown))} (known: {', '.join(sorted(HARNESSES))}, or 'all')")
        return 2
    if "all" in update_harnesses:
        update_harnesses = set(HARNESSES)
    if args.update_claude:
        update_harnesses.add("claude")

    # `build` is dispatched before main()'s .env load, so without this the
    # settings that steer a build — the runtime (LMER_CONTAINER_RUNTIME, which
    # decides the image store), the keep-id pre-warm toggles
    # (LMER_ROOT_USERNS / LMER_CONTAINER_USER, LMER_NO_PREWARM,
    # LMER_PREWARM_TIMEOUT) and LMER_IMAGE — would be honoured from an exported
    # shell variable but silently ignored from the very ~/.lmer/.env the docs
    # point users at (!309 adversarial review, F4; the runtime key is
    # prep-release's LMER_CONTAINER_RUNTIME override). Same candidates and same
    # first-wins precedence as a launch, so an exported variable still beats
    # the file. An explicit allow-list rather than a full load: only variables
    # the build path actually reads are seeded, so an unrelated .env key cannot
    # steer it.
    apply_env_file_defaults(
        default_env_file_candidates(state_dir=lmer_state_dir(), cwd=Path.cwd()),
        only=(
            RUNTIME_ENV,
            "LMER_IMAGE",
            "LMER_ROOT_USERNS",
            "LMER_CONTAINER_USER",
            "LMER_NO_PREWARM",
            "LMER_PREWARM_TIMEOUT",
        ),
    )
    try:
        runtime = detect_runtime()
    except Exception as e:
        error(f"❌ {e}")
        return 2

    repo_root = repo_root_path()
    build_root = args.local or repo_root
    # Resolve image tag from the install method (not the --local checkout)
    # so the built image matches what 'lmer chat' will look for.
    image = os.environ.get("LMER_IMAGE") or resolve_image_tag(repo_root)
    if not image:
        error("Could not determine image version. Set LMER_IMAGE or fix your installation.")
        return 2

    success(f"Building image {image}...")
    if build_image(runtime, image, build_root, force=args.force, pull=not args.no_pull, update_harnesses=sorted(update_harnesses)):
        return 0
    return 1


def _resolve_container_user(ns) -> tuple[str, bool]:
    """Resolve the container user spec, and whether --root-userns selected it.

    --root-userns (env: LMER_ROOT_USERNS) opts into container root under the
    default rootless-podman map — a fast cold start, because base_run_args
    then skips --userns=keep-id and podman never chowns the rootfs
    (issue #351). An explicit --user/--match-uid beats the ambient env
    toggle; combining one with the FLAG is rejected in _validate_parsed_args.
    """
    if ns.match_uid:
        return f"{os.getuid()}:{os.getgid()}", False
    if ns.user:
        return ns.user, False
    if ns.root_userns or get_bool_env("LMER_ROOT_USERNS"):
        return ROOT_CONTAINER_USER, True
    return ambient_container_user(), False


def _resolve_napkin_path(napkin_repo_url: str, work_repo_path: str) -> str:
    """Container path agents write napkin notes to.

    Separate-repo mode (a napkin repo URL is configured) -> ``/napkin``.
    Subdir mode -> ``{work_repo_path}/napkin``. Agents always reference
    ``$LMER_NAPKIN_PATH``, so the mode is transparent to them.
    """
    if napkin_repo_url:
        return "/napkin"
    return f"{work_repo_path}/napkin"


def _announce_user_credential_mounts(planned: "Iterable[PlannedCredentialMount]") -> None:
    """Announce every user-harness credential mount at launch.

    Manifests are operator-authored config, so an unexpected entry (say, a
    private key file) must be visible at launch, not discovered inside the
    container — which means the notice cannot be verbosity-gated: it goes
    through ``warning()`` (unconditional), not ``info()`` (LMER_VERBOSE only).
    Built-in harnesses are skipped: their credential lists are fixed in-tree,
    so there is nothing an operator could be surprised by.
    """
    for m in planned:
        if m.is_user:
            warning(f"🔑 User harness {m.harness_name}: mounting ~/{m.host_path} ({m.mode})")


def _updater_child_env() -> "dict[str, str]":
    """Environment for the detached host-side clone-cache updater.

    Unlike every other execution vector in lmer, this child runs **on the
    host**, so its Python import path must not be attacker-controllable:
    ``PYTHONPATH`` is pinned to the directory lmer's own package was imported
    from (keeps ``-m lmer_cli.clone_cache`` working for venv *and*
    source-checkout installs) and ``PYTHONSAFEPATH`` keeps the cwd off
    ``sys.path``, so neither a stray ``lmer_cli/`` directory in the launch cwd
    nor a ``PYTHONPATH`` picked up from a cwd ``.env`` can inject host code.
    ``PYTHONHOME``/``PYTHONSTARTUP`` are dropped for the same reason.

    The rest of the environment is inherited deliberately: git needs it (an
    ssh-URL mirror can require the caller's ``GIT_SSH_COMMAND``), and .env is
    trusted standing configuration everywhere else in lmer.
    """
    env = dict(os.environ)
    # Directory holding the `lmer_cli` package (…/src for a checkout,
    # …/site-packages for an installed venv).
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    env["PYTHONSAFEPATH"] = "1"
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONSTARTUP", None)
    return env


def _spawn_clone_cache_updater(urls: "list[str | None]") -> None:
    """Fork the detached host-side clone-cache updater (lmer_cli.clone_cache).

    The updater creates/refreshes the bare mirrors the container consumes
    read-only. Detached (new session, no wait) so the launch never blocks on
    cache maintenance, and stdin-fed so a tokenized URL never appears on a
    host-visible argv. Fail-soft: a spawn problem is reported and ignored —
    the session just runs with whatever mirrors already exist.
    """
    to_send = [u for u in urls if u]
    if not to_send:
        return
    try:
        proc = subprocess.Popen(
            # -P (PYTHONSAFEPATH): never prepend the cwd to sys.path. This
            # child runs on the *host*, so a `lmer_cli/` directory in whatever
            # cwd lmer was launched from must not become importable code.
            [sys.executable, "-P", "-m", "lmer_cli.clone_cache"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            # A trusted, always-present cwd instead of the caller's (which may
            # be untrusted, or deleted out from under a detached child).
            cwd=str(Path.home()),
            env=_updater_child_env(),
        )
        proc.stdin.write(("\n".join(to_send) + "\n").encode())
        proc.stdin.close()
        # deliberately no wait(): the updater outlives this launch path
    except OSError as e:
        info(f"⚠️  clone-cache updater not started: {e}")


def main(argv: list[str] | None = None) -> int:
    """
    Main entry point for the lmerpy CLI.

    Orchestrates the complete workflow:
    1. Parse arguments and validate configuration
    2. Resolve repository URL (from args or git origin)
    3. Detect container runtime (Docker/Podman)
    4. Build container run arguments with mounts and environment
    5. Execute container with either Claude Code or custom command

    Args:
        argv: Command-line arguments, defaults to sys.argv[1:]

    Returns:
        Exit code (0 for success, non-zero for errors)
    """
    argv = argv if argv is not None else sys.argv[1:]

    # Handle build subcommand early (before normal arg parsing)
    if argv and argv[0] == "build":
        return _handle_build(argv[1:])
    if argv and argv[0] == "follow":
        return follow_main(argv[1:])
    if argv and argv[0] == "rebuild":
        error("'lmer rebuild' has been renamed to 'lmer build'. Please use 'lmer build' instead.")
        return 1
    # The orchestrator platform (issue #141) owns its own argument grammar, so it
    # is dispatched before task parsing like `build` is.
    if argv and argv[0] == "platform":
        # deferred: cycle — lmer_platform.daemon imports this module at top (via lmer_platform.api); only `lmer platform` needs it
        from lmer_platform.daemon import main as platform_main

        return platform_main(argv[1:])

    ns, rest = parse_args(argv)

    inherited_env_sources = consume_env_defaults()
    if ns.verbose or ns.debug:
        os.environ["LMER_VERBOSE"] = "1"
        inherited_env_sources.pop("LMER_VERBOSE", None)

    # Discover tasks from filesystem and validate provided task
    repo_root = repo_root_path()  # None in installed mode
    state_dir = lmer_state_dir()  # Always ~/.lmer/

    taskdef_paths = _get_taskdef_paths(repo_root)
    if taskdef_paths:
        known_tasks = _discover_all_tasks(taskdef_paths)
    else:
        # Installed mode with no local or configured taskdef directories.
        # Accept any task name; the container validates via baked-in taskdef/.
        known_tasks = set()

    # Snapshot host LMER_* env vars before .env loading (for source tracking)
    host_lmer_vars = {k for k in os.environ if k.startswith("LMER_")}

    # Load .env files early so GitLab tokens are available for URL derivation
    # Check state dir (~/.lmer/) and current working directory
    # Later entries take priority, so load in reverse order (highest priority first)
    # since we use a first-wins pattern (if key not in os.environ).
    # Also track sources for --show-env.
    cwd_env_file = Path.cwd() / ".env"
    # Optional explicit --env-file: highest-priority .env source (still below
    # already-exported environment variables, which the first-wins loop never
    # overwrites). Lets a caller forward a .env that lives neither in cwd nor
    # the state dir — e.g. the Slack listener spawning `lmer chat` from a
    # scratch cwd (issue #75). Warn (non-fatal) when an explicitly named file
    # is missing so a typo'd path is visible rather than silently ignored.
    explicit_env_file = Path(ns.env_file).expanduser() if ns.env_file else None
    if explicit_env_file is not None and not explicit_env_file.is_file():
        warning(f"⚠️  --env-file not found, skipping: {explicit_env_file}")
    env_file_candidates = []
    # Gate on .is_file() (not apply_env_file_defaults' .exists()) so a path that
    # exists but isn't a regular file — e.g. a directory — is skipped here too,
    # keeping this consistent with the warning above and the container-merge
    # guard below (all three agree, and the "skipping" warning stays accurate).
    if explicit_env_file is not None and explicit_env_file.is_file():
        env_file_candidates.append(("--env-file", explicit_env_file))
    env_file_candidates += default_env_file_candidates(
        state_dir=state_dir, cwd=cwd_env_file.parent
    )
    early_env_file_sources = {
        **inherited_env_sources,
        **apply_env_file_defaults(env_file_candidates),
    }

    # Handle --list-presets: a pure query, so it runs before any preset is
    # resolved or a task is required — but after the early .env load so an
    # LMER_PRESETS_FILE defined in a .env file is honored.
    if ns.list_presets:
        return _display_presets_cli()

    # Resolve and apply a named startup preset (--preset > LMER_PRESET;
    # issue #127). Runs after the early .env load so LMER_PRESET/
    # LMER_PRESETS_FILE from .env files work, and before argument validation
    # and harness resolution so preset args and env take full effect.
    preset_result = _resolve_and_apply_preset(ns, rest, argv, early_env_file_sources)
    if isinstance(preset_result, int):
        return preset_result
    ns, rest, preset_applied_env = preset_result

    # Resolve the --agents/LMER_AGENTS fan-out selection (issue #130). Runs
    # after preset application so a preset's env can carry LMER_AGENTS, and
    # early enough that an unknown name fails fast before any container work.
    agents_result = _resolve_agents_cli(ns)
    if isinstance(agents_result, int):
        return agents_result
    resolved_agents = agents_result
    agents_csv = ",".join(resolved_agents) if resolved_agents else None
    agents_config_json = json.dumps(resolved_agents) if resolved_agents else None

    # Validate the (possibly preset-augmented) arguments
    validation_error = _validate_parsed_args(ns)
    if validation_error is not None:
        return validation_error

    # Determine container user (see _resolve_container_user). The
    # --root-userns premise only holds under rootless podman, which is
    # checked once the runtime is detected below.
    container_user, root_userns_active = _resolve_container_user(ns)

    # The flag wins over preset env and can select the harness that serves it.
    session_model = _apply_model_selection(ns.model, early_env_file_sources)
    _apply_dispatch_aliases(early_env_file_sources)

    # Resolve the agent harness (--harness > LMER_HARNESS > LMER_LLM_NAME
    # model hint > claude). Must run AFTER the early .env load above so
    # LMER_HARNESS/LMER_LLM_NAME from a .env file are honored (docs promise
    # ~/.lmer/.env works); still early enough that a typo fails fast, before
    # any image/container work.
    try:
        harness_name, harness_source = resolve_harness_selection(ns.harness)
        harness = get_harness(harness_name)
    except UnknownHarnessError as e:
        # Fail fast — but under --show-env render the env table first: that
        # table is exactly the diagnostic for finding where a typo'd
        # LMER_HARNESS value comes from (host export vs. which .env file).
        # The sources block is a sibling call (not inside the env renderer)
        # so it still prints when no LMER_* vars are set — see
        # _display_sources_config_cli's decision record.
        if ns.show_env:
            _display_env_config_cli(host_lmer_vars, early_env_file_sources)
            _display_sources_config_cli()
        error(f"❌ {e}")
        return 2
    # The platform daemon could not read its Codex seed and left the call to us,
    # since only this point knows the harness (#544 item 7). Dropped either way so
    # it never reaches the container.
    seed_error = os.environ.pop(CODEX_SEED_ERROR_ENV, "").strip()
    if seed_error and harness.name == "codex":
        error(f"❌ {seed_error}; this session resolved the codex harness, which needs it")
        return 2
    user_tag = " [user-installed]" if harness.source_dir is not None else ""
    if harness_source == "model":
        info(f"🤖 Harness: {harness.name}{user_tag} (auto-selected from LMER_LLM_NAME={os.environ.get('LMER_LLM_NAME')})")
    elif harness.name != "claude":
        info(f"🤖 Harness: {harness.name}{user_tag}")
    if harness.source_dir is not None:
        info(f"   (definition: {harness.source_dir}; runner-owned CLI install)")
    elif harness.name != "claude":
        info(
            f"   (requires an image that ships {harness.runner_script}; "
            f"if the session fails with '{harness.runner_command}: command not found', "
            f"rebuild with: lmer build)"
        )

    # Tell the spawning platform which model this session settled on (T51). Here
    # rather than anywhere earlier because this is the first point the answer is
    # final: the flag, the preset's env and the ambient environment have all had
    # their say. A plain terminal launch sets no ports file and writes nothing.
    _record_session_model(
        session_model, harness.name, os.environ.get("LMER_REASONING_EFFORT"),
    )

    # Harnesses the --agents fan-out children imply beyond the session's
    # (issue #131): their credential files union into build_user_mounts
    # below, and a child harness with no host credential file warns here,
    # at launch — not hours later when spawn-harness runs.
    agent_extra_harnesses = (
        _agents_child_harnesses(resolved_agents, harness) if resolved_agents else []
    )

    # Handle --show-env: display env config table, then the canonical-sources
    # block (sibling call so it renders even when the env table early-returns
    # with no LMER_* vars set — see _display_sources_config_cli).
    if ns.show_env:
        _display_env_config_cli(host_lmer_vars, early_env_file_sources)
        _display_sources_config_cli()
        # Without a task, just show env and exit
        if not ns.task and not ns.no_task:
            return 0

    task_id = _selected_task_id(ns)
    # Handle multiple targets: first is primary, rest are secondary.
    # ns.target is always a list with nargs="*" (possibly empty).
    # Partition special target types (e.g. Slack thread URLs) out before
    # primary/secondary selection so the clone path (~repo resolution)
    # never sees them. Each claimed type is represented by a handler that
    # owns its type-specific behavior (see lmer_cli.targets).
    if ns.stray_positionals and not ns.exec_mode:
        error(f"❌ {len(ns.stray_positionals)} argument(s) after an option would be ignored: "
              "put the task and every target before the options")
        return 2
    _raw_targets: list[str] = ns.target if ns.target else []
    targets, special_targets = partition_targets(_raw_targets)
    if ns.repo_url and not resolve.is_likely_url(ns.repo_url):
        error("❌ --repo-url needs a repository URL (http(s)://, ssh://, git@ or file:///)")
        return 2
    if ns.repo_url and not targets and _raw_targets:
        # The thread is then the target too; its handler still connects it.
        targets = [_raw_targets[0]]
    primary_target: str | None = targets[0] if targets else None
    secondary_targets: list[str] = targets[1:] if len(targets) > 1 else []

    # Credential gate: each special target type validates its own required
    # credentials (e.g. Slack needs SLACK_BOT_TOKEN); fail fast before any
    # container work.
    for handler in special_targets:
        env_error = handler.validate_environment()
        if env_error:
            error(f"❌ {env_error}")
            return 1

    # A chat-thread target implies the supervisor control plane (issue #407):
    # the connector that carries the thread runs in this process against the
    # session's bus, so the plane is on and its token is minted here when the
    # environment did not supply one — otherwise the supervisor would mint its
    # own and this process would never learn it.
    chat_connected = any(handler.needs_control_plane for handler in special_targets)
    # The control plane is still on, and its token still minted here: the
    # connectors need it whoever runs them, and under --no-thread-connectors the
    # daemon reads it from the file it wrote for this session (issue #412).
    for handler in special_targets:
        handler.hosts_connectors = not ns.no_thread_connectors
    if chat_connected:
        ns.fastapi = True
    control_token = ns.fastapi_token or os.environ.get("LMER_FASTAPI_TOKEN")
    # Any session with a control plane: whoever holds this token is the session's
    # host side. Minted here because a token this process never learns is one it
    # can attach nothing to.
    if ns.fastapi and not control_token:
        control_token = secrets.token_urlsafe(32)
    # What crosses into the container is the hash, never the token: the harness
    # can read the container's initial environment (`/proc/1/environ`, same user)
    # and the control credential is what says who a record came from (#407 §3.2).
    # The supervisor verifies against this and mints the session's own token.
    control_token_digest = (
        hashlib.sha256(control_token.encode("utf-8")).hexdigest() if control_token else None
    )

    # Resolve taskdef directory for the selected task
    resolved_taskdef_dir: Path | None = None
    if not ns.no_task:
        # Host-side known_tasks is advisory: work-repo taskdefs (project-scoped
        # and globally-scoped) live inside the container at /work/... and are
        # not visible here. If the task isn't in the host's known set, warn but
        # continue — the container's start hook is authoritative. The container
        # may still fail to find the task (e.g. for a genuine typo); that error
        # surfaces after start-up.
        if task_id and known_tasks and task_id not in known_tasks:
            warning(
                "Task '" + str(task_id) + "' not found in host-side taskdef directories ("
                + ", ".join(sorted(known_tasks))
                + "). Will attempt to resolve it via the container's work-repo taskdefs; "
                + "if not found there either, the task will fail to start."
            )
        if task_id and taskdef_paths:
            resolved_taskdef_dir = _resolve_taskdef_dir(task_id, taskdef_paths)
        # Announce selected task early
        if primary_target:
            if secondary_targets:
                success(f"✅ Selected task: {task_id} (primary target: {primary_target}, secondary targets: {', '.join(secondary_targets)})")
            else:
                success(f"✅ Selected task: {task_id} (target: {primary_target})")
        else:
            success(f"✅ Selected task: {task_id}")
        if resolved_taskdef_dir:
            info(f"📂 Task definition: {resolved_taskdef_dir}")
    else:
        success("✅ Selected no-task (exec mode)")

    # Service mode: resolve checkout path early. A group attaches the session to
    # every running member of a compose project, so it puts the session into
    # service mode exactly as a single --service does; what differs is only
    # which container target-exec starts on (issue #312).
    service_mode = bool(ns.service or ns.service_group)
    checkout_path: Path | None = None
    if ns.checkout:
        checkout_path = Path(ns.checkout).resolve()
        if not checkout_path.exists():
            error(f"❌ --checkout path does not exist: {checkout_path}")
            return 2
        if not checkout_path.is_dir():
            error(f"❌ --checkout path is not a directory: {checkout_path}")
            return 2

    # Repo-less session as a *host input*: LMER_NO_REPO in the environment (a
    # preset's env and the early .env load both land in os.environ before this
    # point, so either can set it). The caller this exists for is the
    # orchestrating assistant (issue #141, D17), which must run with no checkout
    # so that it structurally cannot edit code — a prompt saying "don't" is not
    # the same guarantee. Read once and used twice, below and in the container
    # env dict, so the two cannot disagree about what was asked for.
    no_repo_env = get_bool_env("LMER_NO_REPO")

    # Skip repo resolution when --no-clone or --no-task, or when the session
    # deliberately has no repository: there is nothing to resolve, and resolving
    # would fail on a target that is not a repo (which is exactly the shape a
    # repo-less session's target has).
    # Note: --checkout skips cloning but still resolves repo URL for MR/git metadata
    skip_repo_resolve = bool(ns.no_clone or ns.no_task or no_repo_env)
    repo_url: str | None = None
    host_repo_path = None
    # Session without a repository (set when the only targets are special
    # targets whose handler allows repo-less mode for the selected task,
    # and no git origin can be inferred from cwd).
    no_repo_session = False
    # What kind of target this session was given (spec §2, D2). `none` when
    # there is no positional; a `special` target was partitioned out above.
    target_class = classify_target(primary_target)
    if not skip_repo_resolve:
        cwd = Path.cwd()

        # --repo (spec §6.5) and --repo-url (#545) each name the repository;
        # both at once name two.
        if ns.repo and ns.repo_url:
            error(
                "❌ two repositories named: --repo "
                f"{strip_url_credentials(ns.repo)} and --repo-url "
                f"{strip_url_credentials(ns.repo_url)}"
            )
            return 2

        # A non-URL positional that names an existing local checkout is the
        # repository, exactly as before. The probe returns None for a target
        # that names no filesystem entry, so a branch, a SHA, a shorthand or a
        # document URL reaches the classifier instead of failing as a missing
        # path; a target that names an existing entry which is not a checkout
        # (a plain directory, a file) is refused, never read as a branch.
        # Beside --repo-url the positional is only the task's target and is
        # never probed as a local checkout (#545); one spelled as a path still
        # classifies as a repository and is refused below.
        if (
            primary_target
            and not ns.repo_url
            and not resolve.is_likely_url(primary_target)
        ):
            try:
                local_checkout = resolve.probe_local_repo(primary_target, cwd)
            except resolve.NotACheckoutError as exc:
                error(f"❌ {strip_url_credentials(str(exc))}")
                return 2
            if local_checkout is not None:
                target_class = "repository"

        # --repo names the repository explicitly, so it must BE one: a local
        # path goes through the same probe, anything else through the shared
        # classifier, and a non-repository value is refused rather than used
        # as a clone URL that fails inside the container.
        if ns.repo:
            if not resolve.is_likely_url(ns.repo):
                try:
                    repo_checkout = resolve.probe_local_repo(ns.repo, cwd)
                except resolve.NotACheckoutError as exc:
                    error(f"❌ --repo {strip_url_credentials(str(exc))}")
                    return 2
                if repo_checkout is None:
                    error(
                        "❌ --repo "
                        f"{strip_url_credentials(ns.repo)} names no existing "
                        "path or repository URL"
                    )
                    return 2
            elif classify_target(ns.repo) != "repository":
                error(
                    "❌ --repo "
                    f"{strip_url_credentials(ns.repo)} is not a repository URL"
                )
                return 2

        # --repo names the repository explicitly. It is refused beside a
        # repository-class positional ('two repositories named'); the flag
        # refusals that need no classification are in _validate_parsed_args.
        named_repo = ns.repo or ns.repo_url
        if named_repo and target_class == "repository":
            flag = "--repo" if ns.repo else "--repo-url"
            error(
                "❌ two repositories named: the positional "
                f"{strip_url_credentials(primary_target)} and {flag} "
                f"{strip_url_credentials(named_repo)}"
            )
            return 2

        # `other` is a URL that is neither a repository, a work item nor a
        # known thread. Without --repo there is nothing to clone; with it the
        # URL stays the session's target as context.
        if target_class == "other" and not named_repo:
            error(
                "❌ not a repository, a work item or a known thread: "
                f"{strip_url_credentials(primary_target)}. "
                "Name its repository with --repo."
            )
            return 2

        # If the task provides a PR/MR/issue URL, try to derive base repo URL.
        # Only the primary target takes part in repo resolution.
        derived_repo_url = None
        if primary_target and target_class in ("work-item", "reference"):
            derived_repo_url = _derive_repo_url_from_task_target(primary_target)

        try:
            if ns.repo:
                target_to_resolve = ns.repo
            elif ns.repo_url:
                # The target keeps its own identity; the clone is the named
                # repository, not one derived from the target (#545).
                target_to_resolve = ns.repo_url
            elif target_class == "repository":
                target_to_resolve = primary_target or ""
            elif target_class in ("work-item", "reference"):
                # The cwd-origin fallback is for a branch, a SHA or a shorthand
                # (a non-URL work item). A URL that derives no repository is
                # refused rather than silently cloning the cwd's origin.
                if derived_repo_url is None and resolve.is_likely_url(primary_target):
                    error(
                        "❌ cannot derive a repository from "
                        f"{strip_url_credentials(primary_target)}; "
                        "name it with --repo"
                    )
                    return 2
                target_to_resolve = derived_repo_url or ""
            else:  # none
                target_to_resolve = ""
            # With --checkout, target is optional (may not have a repo URL)
            if checkout_path and not target_to_resolve:
                # No target and using checkout — resolve from the checkout's git remote
                repo_url, host_repo_path = resolve.normalize_repo_url("", checkout_path, ns.remote)
            else:
                repo_url, host_repo_path = resolve.normalize_repo_url(
                    target_to_resolve, cwd, ns.remote
                )
            # Inject GitLab token into repo URL if available (for HTTPS or SSH URLs)
            if repo_url:
                repo_url = _inject_gitlab_token_if_available(repo_url)
        except resolve.ResolveError as e:
            if ns.repo:
                # An explicit repository is never substituted: the repo-less
                # fallback exists for an INFERRED repository, not a named one.
                error(f"❌ {e}")
                return 2
            if checkout_path:
                # With --checkout, repo URL resolution failure is non-fatal
                warning(f"⚠️  Could not resolve repo URL: {e}")
            elif special_targets and not targets:
                # Special targets (e.g. a Slack thread) are the only targets
                # and cwd is not a git repo: a repo-less session is allowed
                # only when every claimed handler supports it for this task
                # (conservative when multiple types are mixed). If allowed,
                # the container is told to skip the workspace clone via
                # LMER_NO_REPO; otherwise fail fast with the refusing
                # handler's reason.
                refusing = next(
                    (
                        h
                        for h in special_targets
                        if not h.supports_repoless_session(task_id)
                    ),
                    None,
                )
                if refusing is None:
                    info(special_targets[0].repoless_start_message())
                    no_repo_session = True
                else:
                    error(f"❌ {e}. {refusing.repoless_unsupported_reason(task_id)}")
                    return 2
            else:
                error(f"❌ {e}")
                return 2

    try:
        runtime = detect_runtime()
    except Exception as e:
        error(f"❌ {e}")
        return 2

    if root_userns_active:
        # The whole premise — container root IS the invoking host uid — only
        # holds under the default rootless-podman user namespace. Under
        # docker (root daemon) or a rootful podman, "--user 0:0" would be
        # actual root against the host, so refuse rather than degrade.
        #
        # The question is about the ENGINE, not this process: a non-root
        # client can be pointed at a rootful podman service (CONTAINER_HOST,
        # CONTAINER_CONNECTION, an ssh connection), and a local euid check
        # cannot see that (!309 adversarial review, F1). So ask the engine,
        # and refuse when it will not answer — an unestablished premise is
        # not a satisfied one, and IS_SANDBOX=1 hangs off this branch.
        if runtime != "podman":
            error("❌ --root-userns/LMER_ROOT_USERNS requires rootless podman")
            return 2
        rootless = engine_is_rootless(runtime)
        if rootless is False:
            error(
                "❌ --root-userns/LMER_ROOT_USERNS requires rootless podman: "
                "the podman engine this client is talking to is rootful, so "
                "container root would be host root"
            )
            return 2
        if rootless is None:
            error(
                "❌ --root-userns/LMER_ROOT_USERNS: could not establish that "
                "the podman engine is rootless (`podman info` did not answer), "
                "so refusing to run the agent as container root"
            )
            return 2
        warning(
            "⚠️  --root-userns: the agent runs as root inside the container "
            "(no non-root 'developer' guardrail; it can write anywhere in the "
            "container rootfs). Host blast radius unchanged: still your uid, "
            "no-new-privileges. keep-id UID remap skipped."
        )
    elif is_root_container_user(container_user):
        warning("⚠️  Container user is root inside the container")

    # Resolve image name and ensure it exists (auto-build or pull if missing)
    image = os.environ.get("LMER_IMAGE") or resolve_image_tag(repo_root)
    if not image:
        error("Could not determine image version. Set LMER_IMAGE or fix your installation.")
        return 1
    image_readiness = ensure_image(
        runtime, image, repo_root,
        skip_build=ns.skip_build,
        # --root-userns skips keep-id, so a build on this launch has no remap
        # to pre-warm and must not spend minutes paying one (issue #351).
        container_root=is_root_container_user(container_user),
    )
    if not image_readiness.available:
        return 1

    # Build docker/podman run args
    exec_mode = bool(ns.exec_mode)
    run: list[str] = []
    # Resolved once and both announced and passed (issue #453), so the
    # diagnostic below cannot describe a launch other than the one that
    # happens. The scope is what the ladder reads: this session's taskdef and
    # the repository it works on, with the host dropped.
    scope = LaunchScope(
        task_id=task_id,
        repo_project=_parse_repo_url(repo_url)[1] if repo_url else None,
    )
    limits = resource_limit_plan(
        runtime,
        scope,
        overrides={
            flag: value
            for flag, value in (("--memory", ns.memory), ("--cpus", ns.cpus))
            if value
        },
    )
    gpu = gpu_grant_plan(runtime, scope)
    # Resolved on the host for the same scope (#452): the session's gate only
    # ever sees this one value, never the scoped spellings.
    ci_baseline = resolve_gate_ci_baseline(scope)
    _announce_resource_limits(limits, gpu, ci_baseline)
    run += base_run_args(runtime, exec_mode, container_user, limits, gpu)

    # In developer mode, mount local repo dirs into the container to override
    # baked-in assets (for live development). In installed mode, the container
    # image already has everything at /Agents/global/.
    if repo_root is not None:
        run += build_global_mount(runtime, repo_root)
        run += build_lmer_docs_mount(runtime, repo_root)

    # Ensure container-home exists and mount it
    container_home_base = repo_root if repo_root is not None else state_dir
    container_home = ensure_container_home(container_home_base)
    run += build_container_home_mounts(runtime, container_home)

    # Build user mounts and check for SSH agent. Compute the credential plan
    # ONCE and thread it through both the mounter and the 🔑 announce below,
    # so what is shown is literally what was bound (one evaluation, not two).
    cred_plan = plan_credential_mounts(harness, agent_extra_harnesses)
    user_mounts, ssh_agent_enabled = build_user_mounts(
        runtime, harness, agent_extra_harnesses, plan=cred_plan
    )
    run += user_mounts
    if ssh_agent_enabled:
        success("✅ SSH agent forwarding enabled")

    # User-installed harness definitions (~/.lmer/harnesses, issue #132) plus
    # the install-cache volume when a user harness is active this session.
    harness_dir_mounts, harness_cache_mounted = build_user_harness_mounts(
        runtime, harness, agent_extra_harnesses
    )
    run += harness_dir_mounts

    # Announce every user-harness credential mount from the SAME computed plan
    # build_user_mounts just bound from (cred_plan above), so what is shown is
    # exactly what was bound.
    planned_creds, _ = cred_plan
    _announce_user_credential_mounts(planned_creds)

    # Check SSH setup and warn if not configured
    _check_ssh_setup(container_home, ssh_agent_enabled)

    # Explicit per-file mounts: --mount-file flags + LMER_MOUNT_FILES env.
    # Invalid entries abort the run (fail-fast; see parse_file_mount_specs).
    try:
        file_mount_specs = parse_file_mount_specs(
            ns.mount_file or [], os.environ.get("LMER_MOUNT_FILES", "")
        )
    except ValueError as exc:
        error(f"❌ Invalid file mount: {exc}")
        return 1

    # Explicit per-directory mounts: --mount-dir flags + LMER_MOUNT_DIRS env.
    # Same grammar and same fail-fast rule (see parse_dir_mount_specs).
    try:
        dir_mount_specs = parse_dir_mount_specs(
            ns.mount_dir or [], os.environ.get("LMER_MOUNT_DIRS", "")
        )
    except ValueError as exc:
        error(f"❌ Invalid directory mount: {exc}")
        return 1

    # A chat-thread session gets an upload store the way a platform-spawned one
    # does (issue #407, #246): the connector copies files from the thread into
    # it and the agent's `lmer-signal --attach` copies files out through it.
    # Per session, under the state dir, owner-only.
    # Not under --no-thread-connectors: the daemon has already prepared its own
    # store for this session and passed a --mount-dir for it, so a second one
    # here puts two bind mounts on one container path — and the pointer in the
    # registry entry, which is what the upload routes read, names the daemon's.
    chat_uploads_container: str | None = None
    chat_uploads_host: Path | None = None
    if chat_connected and not ns.no_thread_connectors:
        # deferred: import cost — only a chat-connected launch needs it; lmer_platform.uploads adds ~12 ms to every lmer launch
        from lmer_platform.uploads import CONTAINER_UPLOADS_DIR

        store_id = ns.session or f"chat-{os.getpid()}-{int(time.time())}"
        chat_uploads_host = lmer_state_dir() / "uploads" / store_id
        try:
            chat_uploads_host.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(chat_uploads_host, 0o700)
        except OSError as exc:
            error(f"❌ Cannot create the session's upload store {chat_uploads_host}: {exc}")
            return 1
        chat_uploads_container = CONTAINER_UPLOADS_DIR
        dir_mount_specs = [
            *dir_mount_specs,
            DirMountSpec(host=chat_uploads_host, container=CONTAINER_UPLOADS_DIR, mode="rw"),
        ]
        for handler in special_targets:
            if handler.needs_control_plane:
                handler.attach_uploads(str(chat_uploads_host), CONTAINER_UPLOADS_DIR)

    clashes = conflicting_mount_destinations(file_mount_specs, dir_mount_specs)
    if clashes:
        error(
            "❌ Invalid mount: a file mount and a directory mount both claim "
            f"{', '.join(clashes)} — the container runtime refuses a duplicate "
            "mount destination, so drop one of them"
        )
        return 1

    if file_mount_specs:
        run += build_file_mounts(runtime, file_mount_specs)
        for spec in file_mount_specs:
            success(f"✅ Mounting file: {spec.host} → {spec.container} ({spec.mode})")
    if dir_mount_specs:
        run += build_dir_mounts(runtime, dir_mount_specs)
        for spec in dir_mount_specs:
            success(f"✅ Mounting directory: {spec.host} → {spec.container} ({spec.mode})")

    # Release credential gate (release-flow spec §4/§5): production release
    # credentials — the fine-grained GitHub PAT and the release SSH signing
    # key — reach release-taskdef sessions ONLY, keyed on the resolved task
    # id (see _release_credential_env for the mechanism decision). Every
    # other session gets None seeds in the env dict below, closing both the
    # .env-merge and preset-seeding leak paths. A configured key the mount
    # guard rejects is fatal: a release session that cannot sign must not
    # start (the --mount-file fail-fast precedent above).
    release_env_entries, release_key_mount, release_fatal = _release_credential_env(
        runtime, task_id
    )
    if release_fatal:
        error(f"❌ {release_fatal}")
        return 1
    if release_key_mount:
        run += release_key_mount
        success(
            f"✅ Mounting release signing key → {CONTAINER_RELEASE_SIGNING_KEY_PATH} (ro)"
        )
    if release_env_entries.get(RELEASE_GITHUB_TOKEN_ENV):
        success("🔑 Release GitHub PAT provisioned (release-taskdef session)")

    # gpg-agent forwarding (#355): the host agent's EXTRA socket — the one
    # designed for forwarding, where a client can sign and decrypt but never
    # export or reconfigure a key — reaches ansible-taskdef sessions only,
    # keyed on the resolved task id exactly like the release credentials.
    # Configured-but-unusable is fatal for that session and only that one: a
    # vault session that cannot decrypt must not start.
    gpg_env_entries, gpg_mounts, gpg_fatal = _ansible_gpg_env(runtime, task_id)
    if gpg_fatal:
        error(f"❌ {gpg_fatal}")
        return 1
    if gpg_mounts:
        run += gpg_mounts
        success(
            f"✅ Forwarding gpg-agent → {CONTAINER_GPG_AGENT_SOCKET_PATH} (ro), "
            f"public keyring → {gpg_env_entries[ANSIBLE_GPG_PUBRING_ENV]} (ro)"
        )

    # Optional: mount the host's uv cache so target-repo `uv sync` reuses
    # already-downloaded packages instead of re-fetching them each session.
    # Off by default; opt-in via LMER_MOUNT_UV_CACHE.
    if get_bool_env("LMER_MOUNT_UV_CACHE"):
        host_uv_cache = resolve_host_uv_cache_dir()
        if host_uv_cache.exists() and host_uv_cache.is_dir():
            run += build_host_uv_cache_mount(runtime, host_uv_cache)
            success(f"✅ Mounting host uv cache: {host_uv_cache} → /home/developer/.cache/uv")
        else:
            info(f"⚠️  Host uv cache not found at {host_uv_cache}, skipping mount")

    # Persistent git clone cache (#112): the mirrors themselves are mounted
    # further down, once every repo URL this launch will clone is known —
    # only those mirrors are mounted (#135), never the whole cache root.
    # On by default; LMER_CLONE_CACHE=0 disables. LMER_CLONE_CACHE_DIR
    # overrides the host location (default ~/.lmer/clone-cache). Fail-soft:
    # an unusable cache dir disables the feature and the container clones
    # directly.
    clone_cache_container_dir: str | None = None
    host_clone_cache: Path | None = None
    if get_bool_env("LMER_CLONE_CACHE", default=True):
        candidate_clone_cache = resolve_host_clone_cache_dir()
        try:
            candidate_clone_cache.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            info(f"⚠️  Clone cache dir unusable at {candidate_clone_cache} ({e}), skipping mount")
        else:
            host_clone_cache = candidate_clone_cache
            clone_cache_container_dir = CONTAINER_CLONE_CACHE_DIR

    # Workspace mount removed - using /workspace directory from image instead
    # This avoids root:root ownership issues with Docker/Podman mounts
    # run += build_workspace_mount(
    #     runtime,
    #     ns.workspace_volume,
    #     Path(ns.workspace_bind).resolve() if ns.workspace_bind else None,
    # )
    if host_repo_path is not None:
        run += build_host_repo_ro_mount(runtime, host_repo_path)

    # Service mode: resolve container and add mounts
    service_container_id: str | None = None
    service_workdir: str | None = None
    service_start_name: str | None = ns.service
    if service_mode:
        try:
            service_container_id, service_workdir, service_start_name = (
                resolve_service_target(runtime, ns.service, ns.service_group)
            )
        except ServiceError as e:
            error(f"❌ {e}")
            return 2
        run += build_service_mode_mounts(runtime, checkout_path)  # type: ignore[arg-type]
    elif checkout_path:
        # --checkout without --service: just mount the checkout as workspace
        # (no Docker socket needed since we're not doing container exec)
        run += build_checkout_mount(runtime, checkout_path)

    # Mount external taskdef directories into the container and build
    # container-side LMER_TASKDEF_PATHS so start.py can find instructions.
    container_taskdef_paths: list[str] = []
    external_taskdef_host_paths = _get_external_taskdef_paths()
    if external_taskdef_host_paths:
        taskdef_mount_args, container_taskdef_paths = build_external_taskdef_mounts(
            runtime, external_taskdef_host_paths
        )
        run += taskdef_mount_args
        info(f"📂 External taskdef paths: {', '.join(str(p) for p in external_taskdef_host_paths)}")

    # Parse GitLab MR URL if primary_target is a GitLab merge request URL
    # Only primary target sets GitLab env vars
    gitlab_host = None
    gitlab_project = None
    gitlab_mr_id = None
    if primary_target and isinstance(primary_target, str):
        parsed_host, parsed_project, parsed_mr_id = _parse_gitlab_mr_url(primary_target)
        if parsed_host and parsed_project and parsed_mr_id:
            gitlab_host = parsed_host
            gitlab_project = parsed_project
            gitlab_mr_id = parsed_mr_id

    # Parse repository URL to extract host and project for work repo directory structure
    repo_host = None
    repo_project = None
    if repo_url:
        repo_host, repo_project = _parse_repo_url(repo_url)

    # Get work repo URL, convert to HTTPS if token available
    work_repo_url_original = os.environ.get("LMER_WORK_REPO", "")
    if not work_repo_url_original:
        error("❌ LMER_WORK_REPO is required but not set. Configure it in your .env file.")
        return 2
    work_repo_url = _convert_ssh_to_https_if_token_available(work_repo_url_original, for_work_repo=True)

    # Debug: show token lookup for work repo
    if work_repo_url_original.startswith("git@"):
        work_host = work_repo_url_original.split("@")[1].split(":")[0]
        if _prefer_ssh():
            info(f"🔑 REPO_AUTH_PREFER_SSH set (using SSH for work repo)")
        else:
            work_token = _get_gitlab_token(work_host, for_work_repo=True)
            if work_token:
                success(f"🔑 Found GitLab token for {work_host} (using HTTPS auth for work repo)")
            else:
                info(f"🔑 No GitLab token found for {work_host} (work repo will use SSH)")
        # work_repo_url may be an https URL carrying an `oauth2:<token>@` prefix
        # (see _convert_ssh_to_https_if_token_available). _redact_env_value
        # rebuilds the URL from host/port only, so the embedded token never
        # reaches the log sink (lmer_cli.log.info -> print).
        info(f"📦 Work repo URL: {_redact_env_value('LMER_WORK_REPO', work_repo_url)}")

    # Resolve optional napkin/taskdef repo URLs host-side, baking auth into the
    # URL so the standalone container clone script can clone them as-is. The raw
    # *_TOKEN vars are consumed here and never forwarded into the container.
    napkin_repo_url = os.environ.get("LMER_NAPKIN_REPO", "")
    if napkin_repo_url:
        napkin_repo_url = _inject_gitlab_token_if_available(napkin_repo_url, dedicated_env="LMER_NAPKIN_TOKEN")

    taskdef_repo_url = os.environ.get("LMER_TASKDEF_REPO", "")
    if taskdef_repo_url:
        taskdef_repo_url = _inject_gitlab_token_if_available(taskdef_repo_url, dedicated_env="LMER_TASKDEF_TOKEN")

    # Host-side cache maintenance (#112): freshen/build the mirrors for every
    # repo this session will clone, in a detached background updater. The
    # session never waits on it; the container consumes the cache read-only.
    #
    # ACCEPTED GAP (#105): only the env-var forms are warmed. A taskdef/napkin
    # source configured purely in the work repo's sources.yaml has no
    # LMER_TASKDEF_REPO/LMER_NAPKIN_REPO here, so no mirror is ever built for
    # it and the container's cache lookup misses every session — declaring a
    # source is slower than exporting one. Deliberate, not an oversight: the
    # declaration is only knowable host-side through the best-effort clone
    # cache (the same possibly-stale read --show-env labels as such), and
    # resolution is authoritative container-side, after this point. Documented
    # in docs/LMER-CLI.md under Canonical Source Declarations.
    if clone_cache_container_dir is not None and host_clone_cache is not None:
        clone_cache_urls = [repo_url, work_repo_url, napkin_repo_url, taskdef_repo_url]

        # Mount only the mirrors for the repos this launch clones (#135), each
        # at the path the container's own lookup expects. A repo with no mirror
        # yet contributes no mount: the container clones it directly while the
        # updater below builds the mirror for next time.
        #
        # A secondary MR/PR target is cloned container-side as well
        # (clone_secondary_mr, from a repo URL derived from the target), so its
        # mirror joins the mount list — otherwise narrowing the mount would
        # quietly take away borrowing the whole-root mount used to give it.
        # It stays out of the updater list below: warming is for the repos the
        # session works in, and a secondary repo's mirror exists only when some
        # other session had it as its own project.
        secondary_repo_urls = [
            _derive_repo_url_from_task_target(t)
            for t in secondary_targets
            if isinstance(t, str)
        ]
        cache_mount_args, cache_mounted = build_clone_cache_mounts(
            runtime, host_clone_cache, clone_cache_urls + secondary_repo_urls
        )
        run += cache_mount_args
        if cache_mounted:
            success(
                f"✅ Mounting git clone cache mirrors ({len(cache_mounted)}): "
                + ", ".join(container_path for _, container_path in cache_mounted)
            )
        else:
            info(
                f"📦 No clone-cache mirrors yet for this session's repos "
                f"({host_clone_cache}); cloning directly and warming the cache"
            )

        _spawn_clone_cache_updater(clone_cache_urls)

    # Compute the in-container napkin path (separate repo -> /napkin, else a
    # subdir of the work repo). Always injected so agents can use it in any mode.
    container_work_repo_path = os.environ.get("LMER_WORK_REPO_PATH", "/work")
    napkin_path = _resolve_napkin_path(napkin_repo_url, container_work_repo_path)

    # Remap resolved_taskdef_dir to container paths when it lives in an
    # external taskdef directory (host path won't exist inside the container).
    container_taskdef_root: str | None = None
    container_taskdef_dir: str | None = None
    container_task_instructions: str | None = None
    if resolved_taskdef_dir and not ns.no_task:
        container_taskdef_root, container_taskdef_dir, container_task_instructions = (
            _remap_taskdef_to_container(
                resolved_taskdef_dir,
                list(zip(external_taskdef_host_paths, container_taskdef_paths)),
                repo_root_path(),
            )
        )
    elif not ns.no_task:
        container_taskdef_root = "/Agents/global/taskdef"
        container_taskdef_dir = f"/Agents/global/taskdef/{task_id}" if task_id else None
        container_task_instructions = f"/Agents/global/taskdef/{task_id}/instructions.txt" if task_id else None

    try:
        launch_metadata = _launch_session_metadata(
            harness=harness.name,
            model=session_model,
            effort=os.environ.get("LMER_REASONING_EFFORT"),
            config={
                "preset": os.environ.get("LMER_SESSION_PRESET"),
                "agents": list(resolved_agents or {}),
                "taskdef": task_id if not ns.no_task else None,
                "service": service_start_name if service_mode else None,
                "service_group": ns.service_group if service_mode else None,
            },
        )
    except ValueError as exc:
        error(f"❌ {exc}")
        return 2

    # When a shared taskdef repo is configured it is cloned to /taskdef inside
    # the container; append it AFTER any external taskdef mounts so it lands
    # between the work-repo taskdefs and the lmer built-in (taskdef_search_dirs
    # in hooks/start.py already orders LMER_TASKDEF_PATHS in that slot).
    if taskdef_repo_url:
        container_taskdef_paths.append("/taskdef")

    env = {
        "HOME": "/home/developer",
        "LMER_GPU_GRANTED": gpu.value,
        # --root-userns only (issue #351): Claude Code refuses
        # --dangerously-skip-permissions as root; IS_SANDBOX=1 is its
        # documented sandbox escape, asserted here ONLY for the explicit
        # opt-in whose rootless-podman premise was checked above — never
        # for an ad-hoc --user 0:0, which established no such sandbox.
        # None (dropped) otherwise.
        "IS_SANDBOX": "1" if root_userns_active else None,
        # Which agent harness the container should run (resolved above from
        # --harness/LMER_HARNESS/LMER_LLM_NAME model hint; default claude).
        # Consumed by clone_and_exec.py (runner selection) and lmer-supervisor
        # (TUI profile), and available to the per-harness runner scripts.
        # Always the resolved name — the container never re-derives it from
        # the model hint.
        "LMER_HARNESS": harness.name,
        # Harness-specific fixed environment (registry defaults; a host-
        # exported value wins, matching the other passthrough vars here).
        **{k: os.environ.get(k, v) for k, v in harness.extra_env},
        "CLAUDE_CODE_ENTRYPOINT": os.environ.get("CLAUDE_CODE_ENTRYPOINT", "cli"),
        # Claude Code's own AFK-timeout variable (no LMER_ prefix, like
        # GITLAB_HOST). Host value passes through; when unset, Slack-bridged
        # sessions get a 5-minute default applied just below the dict.
        "CLAUDE_AFK_TIMEOUT_MS": os.environ.get("CLAUDE_AFK_TIMEOUT_MS"),
        "LMER_GLOBAL_DIR": os.environ.get("LMER_GLOBAL_DIR", "/home/developer/.lmer"),
        "LMER_DANGER_ZONE": os.environ.get("LMER_DANGER_ZONE"),
        "LMER_REASONING_EFFORT": os.environ.get("LMER_REASONING_EFFORT"),
        # Claude output style for the session (issue #257). Read in-container by
        # claude-runner.sh, so a host-side value alone would do nothing.
        "LMER_CLAUDE_OUTPUT_STYLE": os.environ.get("LMER_CLAUDE_OUTPUT_STYLE"),
        # Which host the generic GITLAB_TOKEN was issued for (issue #161).
        # The container's own token lookup (clone_and_exec.py) scopes the
        # generic fallback to that host, so without this passthrough an
        # explicit host setting would only apply on the host side.
        "LMER_GITLAB_TOKEN_HOST": os.environ.get("LMER_GITLAB_TOKEN_HOST"),
        "LMER_LLM_NAME": os.environ.get("LMER_LLM_NAME"),
        "LMER_SESSION_PRESET": os.environ.get("LMER_SESSION_PRESET"),
        session_record.METADATA_ENV: json.dumps(
            launch_metadata, ensure_ascii=False, separators=(",", ":")
        ),
        # No human attached (cron/scheduled launches): the AGENTS.md gates
        # report instead of asking when this is set. Never inferred from the
        # launch shape — only an explicit host value crosses over, while
        # spawn-harness sets it on its own children (issue #137).
        "LMER_NONINTERACTIVE": os.environ.get("LMER_NONINTERACTIVE"),
        # Per-lane model+effort dispatch for Claude subagent defs
        # (model[:effort] per lane; parsed in-container by
        # lmer_cli.container.dispatch_agents via claude-agent-files.sh).
        # Any alias in a lane value was expanded in the environment before this
        # dict was built (_apply_dispatch_aliases).
        "LMER_DISPATCH_REVIEW": os.environ.get("LMER_DISPATCH_REVIEW"),
        "LMER_DISPATCH_DESIGN": os.environ.get("LMER_DISPATCH_DESIGN"),
        "LMER_DISPATCH_CODE": os.environ.get("LMER_DISPATCH_CODE"),
        "LMER_DISPATCH_MECHANICAL": os.environ.get("LMER_DISPATCH_MECHANICAL"),
        "LMER_DISPATCH_EXPLORE": os.environ.get("LMER_DISPATCH_EXPLORE"),
        # The table itself, so a nested `lmer` and a spawn-harness child resolve
        # the same names. The session's own model is already expanded by now.
        MODEL_ALIASES_ENV: os.environ.get(MODEL_ALIASES_ENV),
        # Agent fan-out (issue #130): the resolved --agents/LMER_AGENTS preset
        # names and their env overlays (JSON {name: {"env": {...}}}), consumed
        # in-container by spawn-harness. Host-resolved — the presets file
        # itself never enters the container.
        #
        # Deliberately NOT forwarded under the host input names (issue #283):
        # in the container the pair is ambient, so a session launched with
        # --agents handed its selection to every nested `lmer` and every test
        # run inside it, which then tried to resolve those names against a
        # presets file that is not there. The scoped names have exactly one
        # reader, spawn-harness.
        SPAWN_AGENTS_ENV: agents_csv,
        SPAWN_AGENTS_CONFIG_ENV: agents_config_json,
        # The host *input* names must never cross the boundary under any
        # route. Seeded None (the LMER_NAPKIN_TOKEN precedent above, dropped
        # by build_container_env) because presence in this dict — not the
        # value — is what stops the two fill-in loops below: the preset-env
        # seeding (`if key not in env`) and the .env merge (`if key not in
        # env or key in env_file_keys`). Without the seeds an LMER_AGENTS
        # from ~/.lmer/.env or a preset's env would be forwarded verbatim,
        # which is exactly the ambient value the scoped pair exists to avoid.
        AGENTS_ENV: None,
        AGENTS_CONFIG_ENV: None,
        # pi provider metadata (issue #490) is host-generated only: seed None so
        # a .env value cannot inject a registry. The pi block below fills it in
        # for a pi session; for every other harness it is dropped.
        PROVIDERS_ENV: None,
        # DM permissions are decided by host-side listeners and servicers.
        # Block .env and preset seeding from carrying these lists into a run.
        "LMER_SLACK_DM_ALLOWED_USERS": None,
        "LMER_MATRIX_ALLOWED_USERS": None,
        # Names the *host's* runtime; the session image ships only the docker
        # client. Seeded None so neither .env nor preset seeding forwards it.
        "LMER_CONTAINER_RUNTIME": None,
        DEFAULT_SOURCES_ENV: None,
        "LMER_QUICK_GATE_COMMIT": os.environ.get("LMER_QUICK_GATE_COMMIT"),
        "LMER_GATE_CI_BASELINE": ci_baseline[0],
        # User-installed harnesses (issue #132): where the host mounted
        # ~/.lmer/harnesses inside the container (consumed by
        # clone_and_exec.py runner dispatch, lmer-supervisor, and
        # spawn-harness — deliberately the container path, never the host
        # one), and — when the session harness is user-installed — the
        # runner's persistent install-cache directory on the rw cache mount.
        "LMER_HARNESSES_DIR": CONTAINER_HARNESSES_DIR if harness_dir_mounts else None,
        "LMER_HARNESS_CACHE": (
            f"{CONTAINER_HARNESS_CACHE_DIR}/{harness.name}"
            if harness.source_dir is not None and harness_cache_mounted
            else None
        ),
        # Staged mounts (#293/#290): inherited pairs are concatenated with this
        # launch's credential pairs, never replaced. Blank means none — a
        # deleted key would be re-supplied by the .env merge below. Inherited
        # pairs are harmless because the per-plan token in every staged
        # credential path (mounts.plan_credential_mounts) means they can never
        # name a path this launch mounts, and the linker skips unmounted
        # staged paths.
        MOUNT_LINKS_ENV: format_mount_links(
            os.environ.get(MOUNT_LINKS_ENV, ""),
            credential_mount_links(planned_creds),
        ),
        # Statusline segment list (issue #121), consumed in-container by
        # hooks/statusline.py; unset keeps the default repo,branch,task,ctx.
        "LMER_STATUSLINE": os.environ.get("LMER_STATUSLINE"),
        "LMER_PERSIST_AGENT_MEMORY": os.environ.get("LMER_PERSIST_AGENT_MEMORY"),
        # Source provenance for the live-mounted dirs (dev mode): the commit
        # of the host checkout at session launch, "-dirty" when uncommitted.
        # The image's own commit is baked as LMER_BUILD_COMMIT/BUILD_INFO;
        # together a session can answer "what code am I actually running?".
        "LMER_SOURCE_COMMIT": checkout_commit(repo_root_path()),
        "LMER_RUN_STATE_GUARD": os.environ.get("LMER_RUN_STATE_GUARD"),
        # Kill switch for the signal-reminder Stop hook (issue #289). The hook
        # runs in the container, so a host-side value that stopped at the
        # boundary would leave the operator unable to turn the nudge off.
        "LMER_SIGNAL_GUARD": os.environ.get("LMER_SIGNAL_GUARD"),
        # Gate-in-flight coordination (issue #201): the kill switch for the
        # work-repo commit deferral and the Stop hook's matching nudge
        # suppression, and the marker directory both sides read. Both must
        # cross into the container — the gates, the work CLI and the hook all
        # run there, and a host-side value that stopped at the boundary would
        # silently leave the deferral on.
        "LMER_GATE_INFLIGHT_GUARD": os.environ.get("LMER_GATE_INFLIGHT_GUARD"),
        "LMER_GATE_LOCK_DIR": os.environ.get("LMER_GATE_LOCK_DIR"),
        # Kill switch for the gate's text-only fast path (#269). The gates run
        # in the container, so a host-side value that stopped at the boundary
        # would leave the operator unable to demand a full suite.
        "LMER_GATE_NO_FASTPATH": os.environ.get("LMER_GATE_NO_FASTPATH"),
        # Same reasoning for the gate's test-result cache (#269): the kill
        # switch that forces a re-run, and the directory the recorded passes
        # live in. Both are read where the gates run — in the container.
        "LMER_GATE_NO_CACHE": os.environ.get("LMER_GATE_NO_CACHE"),
        "LMER_GATE_NO_PARALLEL": os.environ.get("LMER_GATE_NO_PARALLEL"),
        "LMER_TESTS_WAIT_SCALE": os.environ.get("LMER_TESTS_WAIT_SCALE"),
        "LMER_GATE_CACHE_DIR": os.environ.get("LMER_GATE_CACHE_DIR"),
        "LMER_PRECOMMIT_CACHE_DIR": os.environ.get("LMER_PRECOMMIT_CACHE_DIR"),
        # Opt-in to the masterplan workflow. Truthy (get_bool_env) turns on the
        # session-start plugin provisioning in claude-runner.sh; LMER_TASK=masterplan
        # implies it. MASTERPLAN_RUNS_DIR is computed in-container from the run
        # state (not passed through here), so bundles nest inside the run dir.
        "LMER_MASTERPLAN": os.environ.get("LMER_MASTERPLAN"),
        # Mirror-directory search order for the masterplan plugin, read
        # in-container by libexec/masterplan-enable.sh.
        "LMER_MASTERPLAN_MIRROR_CANDIDATES": os.environ.get("LMER_MASTERPLAN_MIRROR_CANDIDATES"),
        "LMER_HUMAN_IDENTITY": resolve_human_identity(),
        # Optional git identity overrides for commits made inside the container.
        # When set, entrypoint.sh exports them as GIT_AUTHOR_*/GIT_COMMITTER_*
        # (session-scoped; the mounted ~/.gitconfig is left untouched). Either
        # may be set independently; the unset half falls back to gitconfig.
        "LMER_GIT_USER_NAME": os.environ.get("LMER_GIT_USER_NAME"),
        "LMER_GIT_USER_EMAIL": os.environ.get("LMER_GIT_USER_EMAIL"),
        "LMER_REPO_URL": repo_url,
        "LMER_WORK_REPO": work_repo_url,
        "LMER_WORK_REPO_PATH": os.environ.get("LMER_WORK_REPO_PATH", "/work"),
        # Container-side path of the persistent clone-cache mount, read by
        # clone_and_exec.py (#112). None — i.e. not forwarded, and not
        # overridable from a .env — when LMER_CLONE_CACHE=0 or the host
        # cache dir is unusable. The host-side LMER_CLONE_CACHE /
        # LMER_CLONE_CACHE_DIR settings themselves stay host-only.
        "LMER_CLONE_CACHE_PATH": clone_cache_container_dir,
        # Optional napkin/taskdef auxiliary repos. The *credentialed* URLs are
        # forwarded (they carry their own auth); LMER_NAPKIN_PATH is always set
        # so agents can write in any mode. The raw *_TOKEN vars are seeded None
        # so the .env merge below cannot leak them into the container.
        "LMER_NAPKIN_REPO": napkin_repo_url or None,
        "LMER_NAPKIN_PATH": napkin_path,
        "LMER_NAPKIN_TOKEN": None,
        "LMER_TASKDEF_REPO": taskdef_repo_url or None,
        "LMER_TASKDEF_REF": os.environ.get("LMER_TASKDEF_REF"),
        "LMER_TASKDEF_TOKEN": None,
        # Release-flow credentials (spec §5), resolved by the release gate
        # above. LMER_RELEASE_GITHUB_TOKEN / LMER_RELEASE_SIGNING_KEY carry
        # real values only in release-taskdef sessions; every other session
        # carries None seeds so neither the .env merge below nor the preset
        # seeding loop can forward them (the LMER_NAPKIN_TOKEN precedent
        # above).
        **release_env_entries,
        # gpg-agent forwarding (#355), resolved by the ansible gate above.
        # Container paths only — the host socket/keyring locations never
        # enter the container env. Every non-ansible session carries None
        # seeds, so neither the .env merge below nor the preset seeding loop
        # can forward them (the release pair's precedent, same mechanism).
        **gpg_env_entries,
        # Task routing env
        "LMER_TASK": task_id if not ns.no_task else None,
        # Empty (delivered as `LMER_TASK_TARGET=`, never absent) for a
        # repository target (a local path or a bare repository URL) and for no
        # target at all, so every slug copy derives the bare taskdef; the
        # work-item, reference or `other` URL spelling otherwise. The class is
        # the shared classifier's answer; `primary_target` itself is untouched.
        "LMER_TASK_TARGET": (
            container_task_target(primary_target, target_class)
            if not ns.no_task
            else None
        ),
        "LMER_SESSION_KIND": os.environ.get("LMER_SESSION_KIND"),
        "LMER_WORKSPACE_ID": os.environ.get("LMER_WORKSPACE_ID"),
        "LMER_SECONDARY_TARGETS": ",".join(secondary_targets) if secondary_targets else None,
        "LMER_CORE_TASKS": ",".join(sorted(known_tasks)) if (known_tasks and not ns.no_task) else None,
        # Container paths for task definitions (remapped from host paths).
        "LMER_TASKDEF_ROOT": container_taskdef_root,
        "LMER_TASKDEF_DIR": container_taskdef_dir,
        "LMER_TASK_INSTRUCTIONS": container_task_instructions,
        # External taskdef search paths (container-side) for Jinja2 includes
        "LMER_TASKDEF_PATHS": ":".join(container_taskdef_paths) if container_taskdef_paths else None,
        "LMER_CHECKOUT_BRANCH": ns.branch if ns.branch else None,
        "LMER_CHECKOUT_REF": ns.ref if ns.ref else None,
        "LMER_GIT_REMOTE": ns.remote if ns.remote else None,
        # GitLab MR environment variables (set when task_target is a GitLab MR URL)
        "GITLAB_HOST": gitlab_host,
        "GITLAB_PROJECT": gitlab_project,
        "GITLAB_MR_ID": gitlab_mr_id,
        "GITLAB_REVIEW_FILE": "review.json",
        # Repository parsing for work repo directory structure
        "LMER_REPO_HOST": repo_host,
        "LMER_REPO_PROJECT": repo_project,
        # Service mode environment variables
        "LMER_SERVICE_MODE": "1" if service_mode else None,
        "LMER_SERVICE_CONTAINER": service_container_id,
        "LMER_SERVICE_NAME": service_start_name if service_mode else None,
        "LMER_SERVICE_WORKDIR": service_workdir,
        # Group mode (#312). The group is what target-switch resolves members
        # from; the target file is where it records the pick, and is what makes
        # a switch visible to shells that were already running.
        "LMER_SERVICE_GROUP": ns.service_group if service_mode else None,
        "LMER_SERVICE_TARGET_FILE": (
            CONTAINER_SERVICE_TARGET_FILE if ns.service_group else None
        ),
        # Supervisor / FastAPI controls (consumed inside the container by lmer-supervisor)
        "LMER_FASTAPI": "1" if ns.fastapi else None,
        "LMER_MANUAL_START": "1" if ns.manual_start else None,
        # Follow-up prompt the in-container supervisor injects immediately
        # after the auto-/start. Sourced from --prompt; no-op under
        # --manual-start (the supervisor only injects it as part of auto-start).
        "LMER_START_PROMPT": ns.prompt if ns.prompt else None,
        # Answer to the run's recorded open question (issue #98). Sourced
        # from --answer ONLY — no os.environ fallback, same deliberate
        # flag-only pattern as LMER_START_PROMPT above: an answer is one-shot
        # data, while .env is standing configuration, so a stale LMER_ANSWER
        # left in a .env must never silently auto-answer every future
        # question-stop. Applied in-container by `work session-start` before
        # the brief prints.
        "LMER_ANSWER": ns.answer if ns.answer else None,
        # A run's name and goal from the launch (`--name` / `--goal`), applied
        # once in-container by `work session-start` on the call that opens
        # the record's session. Sourced from the flags ONLY — no os.environ
        # fallback, the LMER_ANSWER rule above: a name is one-shot launch
        # data, and a stale export would name (or re-goal) every future run
        # on the project. The None seeds block the .env merge below. A
        # multi-line goal rides the `-e NAME` marker leg on its own.
        "LMER_RUN_NAME": ns.run_name or None,
        "LMER_RUN_GOAL": ns.run_goal or None,
        # The record this launch continues (issue #353 D22). Sourced from
        # --session ONLY — no os.environ fallback, and the reason is stronger
        # here than for the two flag-only entries above. LMER_SESSION_ID is
        # exported inside EVERY lmer container (libexec/harness-common.sh,
        # libexec/claude-runner.sh), so an agent that runs `lmer` to spawn a
        # child session always has one in its own environment; a fallback
        # would route every such child into its PARENT's record, leaving two
        # containers writing one state file — the collision the session-level
        # record exists to prevent. Absent (None, not "") without the flag,
        # because clone_and_exec.mint_session_id mints with setdefault: an
        # empty string is a value it would NOT replace, and the launch would
        # run with no usable id and open no record at all.
        **_continuation_env(ns),
        "LMER_DISABLE_SUPERVISOR": "1" if ns.no_supervisor else None,
        # Forward the initial auto-/start delay so a host-set value reaches
        # the supervisor running inside the container.
        "LMER_AUTO_START_DELAY": os.environ.get("LMER_AUTO_START_DELAY"),
        # Forward the auto-/start CR-nudge delay so a host-set value reaches
        # the supervisor running inside the container.
        "LMER_AUTO_START_NUDGE_DELAY": os.environ.get("LMER_AUTO_START_NUDGE_DELAY"),
        # Forward the prompt-ready marker wait timeout (default 15s in-container).
        "LMER_AUTO_START_READY_TIMEOUT": os.environ.get("LMER_AUTO_START_READY_TIMEOUT"),
        # Forward the prompt-ready marker bytes (UTF-8) so the in-container
        # supervisor can be re-tuned without a release if claude changes the
        # input-prompt glyph.
        "LMER_AUTO_START_READY_MARKER": os.environ.get("LMER_AUTO_START_READY_MARKER"),
        # Forward the post-marker settle delay and the winsize recheck delay
        # so host-set values reach the supervisor running inside the container.
        "LMER_AUTO_START_SETTLE_DELAY": os.environ.get("LMER_AUTO_START_SETTLE_DELAY"),
        "LMER_WINSIZE_RECHECK_DELAY": os.environ.get("LMER_WINSIZE_RECHECK_DELAY"),
        # The Codex delivery adapter runs inside the container (issue #460).
        "LMER_CODEX_RPC_TIMEOUT": os.environ.get("LMER_CODEX_RPC_TIMEOUT"),
        # Forward the harness-profile overrides for the injected start command
        # and the self-shutdown quit sequence — HARNESSES.md promises every
        # profile field can be patched via env without a release, which needs
        # a host-exported value to reach the in-container supervisor.
        "LMER_START_COMMAND": os.environ.get("LMER_START_COMMAND"),
        "LMER_QUIT_SEQUENCE": os.environ.get("LMER_QUIT_SEQUENCE"),
        # Forward the gap between the auto-/start and the follow-up prompt so a
        # host-set value reaches the supervisor running inside the container.
        # Without this delay /start can fail to register before the prompt is
        # typed on slow systems, landing both on one input line (issue #65).
        "LMER_START_PROMPT_DELAY": os.environ.get("LMER_START_PROMPT_DELAY"),
        # The margin between a submitted message's text and its Enter, covering
        # what the kernel cannot answer once the harness has read the text — a
        # harness still coalescing input in userspace (issue #210). It has to reach
        # the in-container supervisor, or retuning that window needs a release.
        "LMER_SUBMIT_ENTER_DELAY": os.environ.get("LMER_SUBMIT_ENTER_DELAY"),
        # Read by the supervisor, inside the container, so they have to cross:
        # how long it holds its control plane open after the harness exits, and
        # whether it mirrors what a human types at the terminal (issue #407).
        "LMER_COMMS_DRAIN_GRACE": os.environ.get("LMER_COMMS_DRAIN_GRACE"),
        "LMER_PLATFORM_TERMINAL_CONFIRM_SECONDS": os.environ.get("LMER_PLATFORM_TERMINAL_CONFIRM_SECONDS"),
        "LMER_PLATFORM_ASSISTANT_NAME": os.environ.get("LMER_PLATFORM_ASSISTANT_NAME"),
        # The worker's launch-time delegation to the assistant (issue #594),
        # read by the orchestrator-ask prompt fragment.
        "LMER_SUPERVISOR_AUTHORITY": os.environ.get("LMER_SUPERVISOR_AUTHORITY"),
        "LMER_COMMS_TERMINAL_MIRROR": os.environ.get("LMER_COMMS_TERMINAL_MIRROR"),
        "LMER_FASTAPI_PORT_RANGE": ns.fastapi_port_range,
        # When --fastapi is on we publish the port range to the host, which
        # only works if the container-side bind is 0.0.0.0. The host CLI
        # publishes to 127.0.0.1 on the host, so it is not network-exposed.
        "LMER_FASTAPI_HOST": ns.fastapi_host or ("0.0.0.0" if ns.fastapi else None),
        # Seed None to block the control preimage from the later .env merge;
        # the supervisor exports a separate session token under this name.
        "LMER_FASTAPI_TOKEN": None,
        "LMER_FASTAPI_TOKEN_SHA256": control_token_digest,
        # Container-side path of the orchestrator's ask channel (issue #141):
        # the directory the platform bind-mounts so a session can ask its
        # operator a question through `lmer-ask` instead of posing it in a
        # terminal nobody is watching. Set by lmer_platform.spawn on the host
        # process, and forwarded here because the value is a *container* path the
        # in-container CLI and the prompt fragment both read. Deliberately no
        # flag: it is not an operator control, it is one half of a mount the
        # spawner made, and a value without the matching mount is a session that
        # blocks forever on answers nobody can write.
        "LMER_ASK_DIR": os.environ.get("LMER_ASK_DIR"),
        # Container-side path of the operator's chat upload store (issue #246),
        # forwarded for exactly the reasons above: it is a *container* path, the
        # in-container prompt fragment is gated on it, and it is one half of a
        # mount the spawner made rather than an operator control — a value
        # without the matching mount is an agent told to read files out of an
        # empty directory. No flag, for the same reason.
        "LMER_UPLOADS_DIR": chat_uploads_container or os.environ.get("LMER_UPLOADS_DIR"),
        # Container-side path of this session's status document (issue #248),
        # forwarded for the same reasons as the two above. No flag either.
        "LMER_STATUS_DIR": os.environ.get("LMER_STATUS_DIR"),
        # Env contributed by special target types (Slack tokens + parsed
        # thread context today). Keys of types with no matching target are
        # seeded with None so the .env merge below cannot forward them.
        **special_target_env(special_targets),
        # Repo-less session: tells clone_and_exec to skip the workspace clone
        # instead of failing on the missing LMER_REPO_URL. Either inferred
        # (special targets were the only targets and no git origin could be
        # found) or asked for by the host — see no_repo_env above.
        "LMER_NO_REPO": "1" if (no_repo_session or no_repo_env) else None,
    }

    # Chat-bridged sessions (a Slack or matrix thread target) default the
    # Claude Code AFK timeout to 5 minutes when the host left it unset;
    # terminal sessions keep None.
    env["CLAUDE_AFK_TIMEOUT_MS"] = _resolve_afk_timeout_ms(
        env["CLAUDE_AFK_TIMEOUT_MS"], chat_connected,
    )

    # Preset env entries applied host-side are seeded into the container env
    # dict so keys with no hardcoded passthrough above still reach the
    # container, keeping the documented precedence over .env-file values (the
    # merge below never overrides a key already present unless a previous
    # .env set it). Skip keys the dict already carries: hardcoded passthrough
    # keys read the preset's value from os.environ above, and the
    # deliberately-None token guards (e.g. LMER_NAPKIN_TOKEN) must stay
    # unforwardable.
    for key, value in preset_applied_env.items():
        if key not in env:
            env[key] = value

    # Merge all variables from .env file into container env dict
    # Check state dir (~/.lmer/) and cwd for .env files
    # Working directory takes highest priority — load in reverse order
    # (highest priority first) since we use a first-wins pattern (if key not in env).
    cwd_env_file = Path.cwd() / ".env"

    env_files_to_load = []

    # Load from cwd first (highest priority)
    if cwd_env_file.exists():
        env_files_to_load.append(("working directory", cwd_env_file))

    # Load from state dir (~/.lmer/)
    state_env_file = state_dir / ".env"
    if state_env_file.exists():
        already_listed = any(f == state_env_file for _, f in env_files_to_load)
        if not already_listed and state_env_file != cwd_env_file:
            env_files_to_load.append(("lmer state dir", state_env_file))

    # An explicit --env-file (resolved above) is appended LAST so it wins: the
    # merge below lets later .env files override earlier ones, so last == the
    # highest precedence among .env files. This is what carries a forwarded
    # .env into the container when cwd has none — e.g. the Slack listener
    # passing its deployment .env to the spawned `lmer chat` (issue #75). Skip
    # if it duplicates a path already queued (harmless, just avoids a re-merge).
    if explicit_env_file is not None and explicit_env_file.is_file():
        already_listed = any(f == explicit_env_file for _, f in env_files_to_load)
        if not already_listed:
            env_files_to_load.append(("--env-file", explicit_env_file))

    # Track which keys came from .env files (not hardcoded in env dict above)
    env_file_keys: set[str] = set()

    if env_files_to_load:
        for location, env_file in env_files_to_load:
            success(f"✅ Found .env file at {location}: {env_file}")
            dotenv_vars = dotenv_values(dotenv_path=str(env_file))
            success(f"📋 Loaded {len(dotenv_vars)} variables from .env file in {location}")
            # Merge .env variables into env dict
            # - Hardcoded env values (set above) take precedence over .env
            # - Later .env files (cwd) override earlier ones (repo root)
            merged_count = 0
            for key, value in dotenv_vars.items():
                if value is None:
                    continue
                # Allow override if key came from a previous .env file
                # but not if it was hardcoded in the env dict
                if key not in env or key in env_file_keys:
                    env[key] = value
                    env_file_keys.add(key)
                    merged_count += 1
            success(f"✅ Merged {merged_count} variables from {location} .env into container environment")
    else:
        searched = [str(cwd_env_file), str(state_env_file)]
        success(f"⚠️  .env file not found at: {' or '.join(searched)}")

    # Publish the FastAPI port so the endpoint is reachable from the host.
    # Only that one port is published (rather than the whole range) so multiple
    # `lmer ... --fastapi` sessions can coexist on the same host with default
    # flags. The resolved port is passed into the container via
    # LMER_FASTAPI_PORT so the supervisor inside binds to it.
    if ns.fastapi:
        try:
            host_port = _resolve_fastapi_host_port(ns.fastapi_port_range, os.environ)
        except (ValueError, RuntimeError) as exc:
            error(f"❌ {exc}")
            return 2
        env["LMER_FASTAPI_PORT"] = str(host_port)
        _publish_host_ports(run, [host_port])
        info(f"🛰  FastAPI endpoint will be published on http://127.0.0.1:{host_port}")
        for handler in special_targets:
            if handler.needs_control_plane and control_token:
                handler.attach_control_plane(
                    f"http://127.0.0.1:{host_port}", control_token
                )

    # General port passthrough (--ports / --port-pool): allocate N free ports
    # and publish them so a service Claude runs inside is reachable on the host.
    port_rc = _apply_port_passthrough(ns, env, run)
    if port_rc is not None:
        return port_rc

    # pi provider model discovery (issue #490). Only the pi harness reads these
    # variables, so the definitions and the operator's key ride along whenever
    # pi runs: the session harness itself, or a ``--agents`` child routed to pi
    # (which execs the binary directly and cannot render its own registry). The
    # host file is validated here — before the container starts — so an obvious
    # apiKey placeholder aborts with the file named instead of producing a
    # session that sends the placeholder as a bearer token.
    pi_in_play = harness.name == "pi" or any(
        child.name == "pi" for child in agent_extra_harnesses
    )
    if pi_in_play:
        try:
            # The key may be exported or live in a .env file; the exported
            # value wins, matching the CLI's normal precedence.
            env.update(container_provider_env(load_providers(), {**env, **os.environ}))
            validate_host_registry_file(environ={**env, **os.environ})
        except ProviderConfigError as exc:
            error(f"❌ {exc}")
            return 2

    # The CI-baseline evidence token reads as a Maintainer, CI/CD variable
    # values included, so only a session whose baseline is on receives it.
    if ci_baseline[0] != "1":
        for name in [key for key in env if key.startswith(EVIDENCE_TOKEN_PREFIX)]:
            del env[name]

    # Container environment. Values ride a mode-0600 --env-file (plus bare
    # -e NAME inheritance markers for the few that a line-oriented file
    # cannot carry), never argv — /proc/<pid>/cmdline is world-readable and
    # used to expose every token a session carries (issue #158). A variable
    # neither leg can carry aborts the launch rather than falling back to an
    # inline -e NAME=value, so the invariant cannot be quietly violated.
    try:
        container_env = build_container_env(env)
    except ContainerEnvError as exc:
        error(f"❌ {exc}")
        return 2
    # Installed immediately after the file exists — before the args are even
    # appended — so the create→install window a signal could slip through is as
    # narrow as possible. SIGTERM here is routine rather than an operator
    # mishap: a reaped session (systemd KillMode=control-group, an orchestrator,
    # the Slack reaper's ladder) gets one before SIGKILL. Unavailable off the
    # main thread, where an embedded caller owns signal disposition anyway.
    try:
        previous_sigterm = signal.signal(
            signal.SIGTERM, _make_sigterm_cleanup_handler(container_env)
        )
    except ValueError:
        previous_sigterm = None
    run += container_env.args
    # The platform names the session and run it spawned; a plain CLI run has
    # neither (#444).
    platform_session = os.environ.get("LMER_PLATFORM_SESSION_ID") or None
    run += container_labels(
        repo_host=repo_host,
        repo_project=repo_project,
        run_slug=os.environ.get("LMER_PLATFORM_RUN_SLUG") or None,
        platform_session=platform_session,
        platform_instance=os.environ.get("LMER_PLATFORM_INSTANCE") or None,
    )
    name_slug = os.environ.get("LMER_PLATFORM_RUN_SLUG") or _cli_run_slug(
        None if ns.no_task else task_id,
        container_task_target(primary_target, target_class),
    )
    name = container_name(name_slug, platform_session)
    run += ["--name", name]
    cid_dir = None
    try:
        cid_dir = Path(tempfile.mkdtemp(prefix="lmer-cid-"))
        cid_file = cid_dir / "cid"
        run += ["--cidfile", str(cid_file)]
        # Image name was resolved earlier (before ensure_image). Remember where
        # it lands: under keep-id the launch re-resolves it to an ID below and
        # substitutes it here, so the marker and the container name the same
        # image (!309 adversarial review, F6).
        image_argv_index = len(run)
        run += [image]

        # Container command
        # Service mode and checkout mode still use clone_and_exec.py for work repo
        # and MR branch handling — only --no-clone/--no-task skip it entirely.
        use_clone_script = not (ns.no_clone or ns.no_task)
        if not use_clone_script:
            # Skip clone script entirely, run command directly
            cmd_tokens = rest if rest else ["bash"]
            run += cmd_tokens
        else:
            # Call the container clone+exec script from mounted repo
            clone_script = "/Agents/global/src/lmer_cli/container/clone_and_exec.py"

            if exec_mode:
                # Remaining tokens after --exec are the command to run; if empty, default to bash
                cmd_tokens = rest if rest else ["bash"]
                run += [
                    "python3",
                    clone_script,
                    "--",
                    *cmd_tokens,
                ]
            else:
                # Default to the harness runner (historically the literal
                # "claude-runner" token, kept for claude so a new host CLI still
                # works against older images).
                run += [
                    "python3",
                    clone_script,
                    "--",
                    harness.runner_command,
                ]

        # No environment VALUE appears here: build_container_env emits only
        # --env-file plus bare -e NAME markers (enforced by its postcondition).
        # The one inline -e in the command comes from mounts.py's
        # SSH_AUTH_SOCK=/ssh-agent, a fixed container-side socket path.
        info("Running: " + shlex.join(run))

        # A real interactive session (the default claude-runner path) attached to a
        # special target announces itself via its handler's lifecycle hooks while it
        # runs, so peers can tell the target is already taken. For a Slack thread
        # this records the attachment in the host-side registry the listener
        # consults, so it won't connect a second lmer to a thread that already has
        # one — including this session if it was started manually (issue #74).
        # --exec / --no-task one-shots are not interactive sessions, so they don't
        # announce.
        announce_session = use_clone_script and not exec_mode
        thread_servicer = None
        if announce_session:
            for handler in special_targets:
                handler.on_session_start()
            # The host side of `lmer-signal read` / `post --thread`, keyed on the
            # control plane rather than on a chat target (docs/COMMS.md §12).
            if ns.fastapi and control_token and not ns.no_thread_connectors:
                thread_servicer = _start_thread_servicer(
                    ns, env.get("LMER_FASTAPI_PORT"), control_token,
                    chat_uploads_host, chat_uploads_container, task_id,
                )

        success(f"🚀 Launching container {name} ({runtime} create {image})")
        # Warn when this launch faces the remap, which is a question about the
        # IMAGE and not about this process: `lmer build` then a separate spawn
        # is cold with materialized=False, and that is the longest wait there
        # is (issue #384). The marker survives across processes; absent means
        # warn, because a missing notice reads as a hang.
        keep_id = keep_id_applies(runtime, container_user)
        # Bind the notice, the container and the marker to ONE image ID.
        # ensure_image resolved an ID at the top of session setup, but the
        # argv above names the mutable TAG — so a rebuild or pull retagging
        # inside the setup window meant the notice was decided about A,
        # podman ran B, and A was then recorded warm on the strength of a run
        # of B. A retained A is silently cold after that: one multi-minute
        # wait with no notice, which is #384's own symptom
        # (!309 adversarial review, F6 — the launch half of it; `prewarm`
        # already binds this way). Re-resolving as late as possible and
        # putting the ID into argv closes the window rather than narrowing
        # it: whatever the tag becomes, podman runs the image we recorded.
        # One extra `image inspect`, and only where keep-id applies.
        # No late answer means there is nothing to bind to, so leave both the
        # argv and the marker exactly where they were — the same fallback
        # `prewarm` takes, and no worse than the behaviour this replaces.
        launch_image_id = image_readiness.image_id
        if keep_id:
            late_image_id = remap_image_id(runtime, image)
            if late_image_id:
                launch_image_id = late_image_id
                run[image_argv_index] = late_image_id
        if keep_id and not remap_recorded(launch_image_id):
            success(
                "   (this image has not been remapped yet; podman remaps UIDs "
                "for --userns=keep-id on first use, which can take several "
                "minutes)"
            )
        try:
            try:
                rc, container, name = create_container(
                    runtime, run, cid_file,
                    rename=lambda: container_name(name_slug, platform_session, retry=True),
                    env=container_env.subprocess_env(),
                )
            finally:
                # The runtime has read the env file once create returns, so the
                # secrets in it need not outlive this call (#444).
                container_env.cleanup()
            # podman pays the keep-id remap while create builds the container's
            # storage, so a successful create is the evidence the marker needs.
            if keep_id and rc == 0:
                record_remap(launch_image_id)
            if rc != 0:
                if container:
                    remove_container(runtime, container)
            elif not container:
                error(f"❌ {runtime} create reported success but no container id")
                return 125
            else:
                _record_platform_facts(container={"id": container, "name": name})
                # 125 from here means the start never ran — where a runtime
                # rejects a GPU it cannot grant.
                rc = follow_container(
                    runtime, container, started=False, interactive=sys.stdin.isatty(),
                    detach_note=_detach_note(announce_session, special_targets, thread_servicer),
                    name=name,
                )
            if gpu.value and rc == 125:
                error(
                    "❌ The GPU-enabled container invocation failed before its "
                    "command started (runtime exit 125). Lmer will not retry "
                    "without the requested GPU."
                )
                error(f"   To fix GPU support, {gpu_remediation(runtime)}.")
            return rc
        except KeyboardInterrupt:
            # Interrupted mid-create, or a Ctrl-C that escaped the follow: a
            # container may exist that nothing will follow any more.
            container = read_container_id(cid_file)
            if container:
                remove_container(runtime, container)
            return 130
        finally:
            if announce_session:
                for handler in special_targets:
                    handler.on_session_end()
                if thread_servicer is not None:
                    thread_servicer.stop()
    finally:
        # Cleanup BEFORE restoring the disposition: in the other order a
        # SIGTERM landing between the two statements gets the default
        # disposition and strands the file — in the one window where it is
        # guaranteed to still exist, and the reaper's ladder makes a second
        # SIGTERM unexceptional.
        container_env.cleanup()
        if cid_dir is not None:
            shutil.rmtree(cid_dir, ignore_errors=True)
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)


def _detach_note(announce_session, special_targets, thread_servicer) -> str | None:
    """What a detach costs a session whose thread services this process hosts:
    they stop with it, and `lmer follow` does not restart them."""
    hosting = announce_session and (
        thread_servicer is not None
        or any(getattr(h, "hosts_connectors", False) for h in special_targets)
    )
    if not hosting:
        return None
    return (
        "this session's Slack/Matrix thread connection and `lmer-signal` thread "
        "service end with this process; the agent keeps running without them"
    )


def _cli_run_slug(task: str | None, target: str | None) -> str | None:
    """The run slug the container will derive for itself, for a CLI run's
    container name; ``None`` for a run with no task."""
    if not task:
        return None

    return derive_slug(task, target or "")


if __name__ == "__main__":
    raise SystemExit(main())
