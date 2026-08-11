"""
Container runtime detection and configuration.

This module handles:
- Detecting available container runtime (Docker or Podman)
- Building base container run arguments with resource limits
- Constructing environment variable arguments
- Determining repository root path and install mode
- TTY configuration for interactive sessions
"""

import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import List

from .log import warning


class InstallMode(Enum):
    """How the lmer CLI was installed and is being run."""

    DEVELOPER = "developer"  # Running from a git checkout
    INSTALLED = "installed"  # Installed via uv tool install (no local repo)


class RuntimeErrorDetect(Exception):
    """Exception raised when no container runtime is found."""
    pass


_LMER_STATE_DIR = Path.home() / ".lmer"

# Default container PID limit. Caps the number of processes a session's
# container may spawn (a fork-bomb / runaway safety bound). Overridable via
# LMER_PIDS_LIMIT — see _resolve_pids_limit() and docs/LMER-CLI.md. Raising
# this is the host-kernel-agnostic mitigation for the cgroup-v1 pids-controller
# counter leak on older kernels (e.g. RHEL 8 / 4.18), where phantom fork
# entries accumulate and prematurely exhaust the cap.
DEFAULT_PIDS_LIMIT = "512"


#: The kernel's own answer, read rather than shelled out for. ``1`` is
#: enforcing, ``0`` permissive; the file exists only while selinuxfs is mounted,
#: which is exactly the condition under which SELinux can be enforcing at all.
SELINUX_ENFORCE_PATH = Path("/sys/fs/selinux/enforce")


@lru_cache(maxsize=1)
def _is_selinux_enforcing() -> bool:
    """Whether SELinux is enabled and enforcing.

    Reads :data:`SELINUX_ENFORCE_PATH` first and only falls back to
    ``getenforce``. The order is load-bearing rather than an optimisation: this
    is called from *inside* the platform container as well as on a host
    (``lmer platform`` spawns sessions from there), and the container image
    carries no ``selinux-utils`` — so a ``getenforce``-only probe answers "not
    enforcing" on an enforcing host, every spawned session loses
    ``--security-opt label=disable`` and the ``,z`` relabel suffix on its bind
    mounts (:func:`base_run_args`, ``mounts.selinux_opt``), and the session dies
    on AVC denials against ``/workspace`` and its mounted credentials. selinuxfs
    is visible in the container on such a host, so reading it removes the
    package dependency for every caller instead of adding one to one image.

    The shell-out stays as the fallback for a host where selinuxfs is mounted
    somewhere else. Both branches answer ``False`` when they cannot tell: a
    wrong "enforcing" adds relabel flags a non-SELinux runtime rejects outright,
    while a wrong "not enforcing" degrades to today's behaviour.
    """
    try:
        raw = SELINUX_ENFORCE_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    else:
        return raw.strip() == "1"

    try:
        result = subprocess.run(
            ["getenforce"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0 and result.stdout.strip().lower() == "enforcing"
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def _user_cgroup_controllers_path() -> Path:
    """Path to cgroup.controllers for the current user's systemd user slice."""
    uid = os.getuid()
    return Path(
        f"/sys/fs/cgroup/user.slice/user-{uid}.slice/"
        f"user@{uid}.service/cgroup.controllers"
    )


def _available_controllers() -> "set[str] | None":
    """Cgroup v2 controllers delegated to the user's systemd slice.

    Returns the (possibly empty) controller set read from the user slice's
    ``cgroup.controllers``, or ``None`` when the delegation gate does not
    apply — the file is missing or unreadable (cgroup v1 hosts, root podman
    where ``user@0.service`` typically doesn't exist, exotic layouts).

    ``None`` and ``set()`` are deliberately distinct: an *empty set* means
    "cgroup v2 user slice exists and nothing is delegated" (the crun-abort
    case the gate exists for), while ``None`` means "we cannot tell —
    keep prior behavior and pass the resource flags unchanged." Collapsing
    the two would silently drop the ``--pids-limit`` fork-bomb bound on
    hosts where the limits worked fine before.
    """
    try:
        path = _user_cgroup_controllers_path()
        if not path.exists():
            return None
        return set(path.read_text().split())
    except OSError:
        return None


def detect_runtime() -> str:
    """
    Detect available container runtime on the system.

    Checks PATH for docker and podman binaries, in that order.

    Returns:
        'docker' or 'podman'

    Raises:
        RuntimeErrorDetect: If neither Docker nor Podman is found
    """
    if shutil.which("docker"):
        return "docker"
    if shutil.which("podman"):
        return "podman"
    raise RuntimeErrorDetect("Neither Docker nor Podman found in PATH")


def tty_flags() -> List[str]:
    """
    Determine TTY flags for container.

    Returns:
        ['-it'] if stdin is a TTY, empty list otherwise
    """
    # mimic lmer: use -it if stdin is a tty
    if sys.stdin.isatty():
        return ["-it"]
    return []


def _is_lmer_pyproject(path: Path) -> bool:
    """Check if a pyproject.toml belongs to lmer."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return data.get("project", {}).get("name") in ("lmer", "lmer-cli")
    except (OSError, tomllib.TOMLDecodeError):
        return False


def detect_install_mode() -> InstallMode:
    """
    Detect whether lmer is running from a git checkout or an installed package.

    Developer mode: __file__ is inside a directory tree containing a
    pyproject.toml with project.name == "lmer" (or legacy "lmer-cli").

    Installed mode: No such pyproject.toml found (e.g. uv tool install
    places the package in an isolated venv).

    Returns:
        InstallMode.DEVELOPER or InstallMode.INSTALLED
    """
    p = Path(__file__).resolve()
    for parent in [p] + list(p.parents):
        pyproject = parent / "pyproject.toml"
        if pyproject.exists() and _is_lmer_pyproject(pyproject):
            return InstallMode.DEVELOPER
    return InstallMode.INSTALLED


def repo_root_path() -> Path | None:
    """
    Find the root path of the lmer repository.

    Searches up the directory tree from this file for a pyproject.toml
    belonging to lmer.

    Returns:
        Path to repository root in developer mode, None in installed mode.
    """
    if detect_install_mode() == InstallMode.INSTALLED:
        return None
    p = Path(__file__).resolve()
    for parent in [p] + list(p.parents):
        pyproject = parent / "pyproject.toml"
        if pyproject.exists() and _is_lmer_pyproject(pyproject):
            return parent
    return None


def lmer_state_dir() -> Path:
    """
    Get the directory for LMER runtime state (~/.lmer/).

    Used for container-home, .env, and other persistent data
    in both developer and installed modes.

    Returns:
        Path to ~/.lmer/
    """
    return _LMER_STATE_DIR


def _resolve_pids_limit() -> str:
    """Resolve the container ``--pids-limit`` value from ``LMER_PIDS_LIMIT``.

    Returns the value to hand to ``docker``/``podman`` ``--pids-limit``.

    Rules:
    - Unset/empty → :data:`DEFAULT_PIDS_LIMIT`.
    - Any positive integer → that value (raise the cap on hosts affected by
      the cgroup-v1 pids-controller leak).
    - ``-1`` → ``"-1"`` (Docker/Podman "unlimited"; an escape hatch for badly
      leaking hosts where any finite cap eventually fills with phantom
      entries).
    - Anything else — ``0``, other negatives, non-numeric — is rejected with a
      warning and falls back to the default. A misconfigured value must never
      silently weaken or disable the safety bound.
    """
    raw = os.environ.get("LMER_PIDS_LIMIT", "").strip()
    if not raw:
        return DEFAULT_PIDS_LIMIT
    try:
        value = int(raw)
    except ValueError:
        warning(
            f"⚠️  Ignoring invalid LMER_PIDS_LIMIT={raw!r} (not an integer); "
            f"using default {DEFAULT_PIDS_LIMIT}"
        )
        return DEFAULT_PIDS_LIMIT
    if value > 0 or value == -1:
        return str(value)
    warning(
        f"⚠️  Ignoring out-of-range LMER_PIDS_LIMIT={raw!r} "
        f"(must be a positive integer, or -1 for unlimited); "
        f"using default {DEFAULT_PIDS_LIMIT}"
    )
    return DEFAULT_PIDS_LIMIT


def _resource_limit_args(runtime: str) -> List[str]:
    """
    Resource-limit flags (CPU, memory, PIDs), gated where required.

    crun aborts when --cpus is set but the cpu controller isn't delegated
    to the user slice, so ROOTLESS podman on cgroup v2 gates each flag on
    delegation. The gate is scoped to exactly that case: root podman
    (controllers available at the root cgroup) and hosts where the
    user-slice controllers file is missing/unreadable (cgroup v1, no user
    manager) keep the prior always-pass behavior — dropping flags there
    would silently shed the --pids-limit fork-bomb bound on hosts where
    the limits worked fine. Docker (root daemon) always passes the flags.
    """
    flag_specs = [
        ("--cpus", "1", "cpu"),
        ("--memory", "2g", "memory"),
        ("--pids-limit", _resolve_pids_limit(), "pids"),
    ]
    controllers = None
    if runtime == "podman" and os.geteuid() != 0:
        controllers = _available_controllers()
    if controllers is None:
        # Gate does not apply (docker, root, cgroup v1, unreadable slice) —
        # pass all resource flags.
        return [arg for flag, value, _controller in flag_specs for arg in (flag, value)]

    args: List[str] = []
    dropped: list[str] = []
    for flag, value, controller in flag_specs:
        if controller in controllers:
            args += [flag, value]
        else:
            dropped.append(controller)
    if dropped:
        uid = os.getuid()
        dropped_flags = [f for f, _, c in flag_specs if c in dropped]
        warning(
            f"⚠️  cgroup controllers not delegated to user@{uid}.service: "
            f"{', '.join(dropped)} — dropping flags: {', '.join(dropped_flags)}"
        )
        warning(
            "   To enable, create /etc/systemd/system/user@.service.d/delegate.conf:"
        )
        warning("     [Service]")
        warning("     Delegate=cpu cpuset io memory pids")
        warning("   then: sudo systemctl daemon-reload")
    return args


def base_run_args(runtime: str, exec_mode: bool, user: str) -> List[str]:
    """
    Build base container run arguments.

    Includes:
    - Runtime and run command
    - TTY flags
    - Resource limits (CPU, memory, PIDs)
    - Security options
    - User and working directory

    Args:
        runtime: Container runtime ('docker' or 'podman')
        exec_mode: Whether running in exec mode
        user: Container user specification (e.g., 'developer' or '1000:1000')

    Returns:
        List of base Docker/Podman arguments
    """
    args: List[str] = [runtime, "run", "--rm"]
    # PID 1 init (tini): reap orphaned grandchildren and forward signals.
    # clone_and_exec.py runs the claude-runner as a child (session-end
    # backstop) instead of exec'ing it, so without an init the Python
    # process is PID 1 and zombies accumulate unreaped.
    args += ["--init"]
    args += tty_flags()
    args += ["--security-opt", "no-new-privileges"]
    # Disable SELinux labeling to allow SSH agent socket access
    # Container processes (container_t) cannot connect to user sockets (unconfined_t) by default
    if _is_selinux_enforcing():
        args += ["--security-opt", "label=disable"]

    # Resource limits (CPU, memory, PIDs), gated on rootless podman where
    # delegation requires it — see _resource_limit_args.
    args += _resource_limit_args(runtime)
    if runtime == "podman":
        # Podman-specific: maintain user ID mapping for SSH agent and file permissions
        args += ["--userns=keep-id"]

    # User and workdir
    args += ["--user", user, "-w", "/workspace"]
    return args


class ContainerEnvError(Exception):
    """A variable cannot be carried into the container without exposing it.

    Raised instead of falling back to an inline ``-e NAME=value``: that form
    is the exposure issue #158 exists to close, and the fallback it replaced
    was reachable with a credential straight from a ``.env`` file —
    ``dotenv_values`` accepts both dotted/dashed key names and quoted
    multi-line values, so an SSH key under a key name like ``app.signing-id``
    landed in argv.
    """


# Names the container runtime CLIs actually read from their OWN environment.
# The marker leg hands a container-side value to the client, so overriding one
# of these would change how docker/podman itself behaves — a container HOME
# redirecting the client's config lookup, a container PATH breaking the exec of
# the runtime binary.
#
# Deliberately an explicit list rather than `DOCKER_*`-style prefixes. Under
# fail-closed routing this set gates a HARD ABORT, so over-reserving costs a
# refused launch for a name no client reads (`CONTAINER_PAYLOAD` is a real
# example). Completeness here is a functional convenience, NOT a security
# boundary: a name missing from this list can only corrupt the client's own
# environment, never leak a value, because no leg falls back to argv.
_CLIENT_RESERVED_NAMES = frozenset(
    {
        # Generic process environment the client itself depends on.
        "PATH",
        "HOME",
        "TMPDIR",
        "IFS",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        # docker CLI.
        "DOCKER_HOST",
        "DOCKER_CONFIG",
        "DOCKER_CERT_PATH",
        "DOCKER_TLS",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CONTEXT",
        "DOCKER_API_VERSION",
        "DOCKER_DEFAULT_PLATFORM",
        "DOCKER_BUILDKIT",
        "DOCKER_CONTENT_TRUST",
        # podman CLI.
        "CONTAINER_HOST",
        "CONTAINER_CONNECTION",
        "CONTAINER_SSHKEY",
        "CONTAINERS_CONF",
        "CONTAINERS_STORAGE_CONF",
        "CONTAINERS_REGISTRIES_CONF",
        "PODMAN_CONNECTIONS_CONF",
        "REGISTRY_AUTH_FILE",
        "XDG_RUNTIME_DIR",
        "XDG_CONFIG_HOME",
        # Loader hooks — a container-side value here breaks the client's exec.
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
    }
)


def _client_reads_name(name: str) -> bool:
    """Whether the runtime client itself consumes ``name``."""
    if name in _CLIENT_RESERVED_NAMES:
        return True
    # Proxy settings are honored in either case by Go's http.ProxyFromEnvironment.
    return name.upper() in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY")


_NUL = "\0"

# Both runtimes read the env file with Go's default ``bufio.Scanner``, whose
# maximum token is 64 KiB — a longer LINE aborts the spawn outright
# ("bufio.Scanner: token too long", rc 125; measured in review at exactly
# 65536 bytes, with 65535 parsing fine). Measured on the encoded
# ``NAME=value`` bytes, so a multibyte value is counted as the parser counts
# it. Such a value has no newline by definition, so the marker leg carries it
# instead: execve's per-string ceiling (``MAX_ARG_STRLEN``, 128 KiB) is twice
# as generous, which is also what the pre-#158 ``-e NAME=value`` form relied
# on.
_ENV_FILE_MAX_LINE_BYTES = 64 * 1024

# Characters an env-file line cannot carry. Both runtimes read the file line
# by line, and Go's ``bufio.Scanner`` line splitter also strips a trailing
# "\r", so a value containing either would arrive truncated or altered.
_UNREPRESENTABLE_IN_ENV_FILE = ("\n", "\r")

# Exactly what docker's env-file parser rejects in an INTERIOR position of a
# NAME: ``strings.ContainsAny(name, " \t")``. Deliberately not
# ``str.isspace()``, which also covers NBSP/VT/FF — docker's parser accepts
# those in the interior (``FO\xa0O=val`` parses), so treating them as blockers
# would refuse a spawn no runtime objects to. LEADING whitespace is a separate
# case handled by ``name != name.lstrip()``; see _env_file_line_blocker.
_ENV_FILE_NAME_REJECTS = " \t"

# Basename of the env file inside its private temp directory.
_ENV_FILE_NAME = "container.env"


def _undeliverable_reason(name: str, value: str) -> str | None:
    """Why ``name``/``value`` cannot be carried by ANY leg, or None.

    These are properties of the pair alone rather than of either transport, so
    they are checked once up front rather than per leg — and before any file
    is written, so a refusal cannot strand a directory:

    - **NUL**: terminates a C string. In a value, the env-file leg has docker
      forward it and ``execve`` truncate the value inside the container, while
      the marker leg raises an uncaught ``ValueError: embedded null byte`` out
      of ``subprocess``. In a NAME it is the same story with the legs swapped:
      the marker leg refuses it (see :func:`_marker_name_blocker`) but the
      env-file leg would write the raw byte into the file. None of those is a
      delivery, so refuse here with a message that names the variable.
    - **Not UTF-8 encodable** (a lone surrogate, i.e. a non-UTF-8 byte that
      ``os.environ`` decoded with ``surrogateescape``): docker's env-file
      parser runs ``utf8.Valid`` per line and aborts the whole spawn
      ("invalid utf8 bytes at line N", rc 125), taking every other variable
      with it. The marker leg is no better — verified in review, the client
      marshals ``Config.Env`` as JSON and the byte arrives as U+FFFD, which is
      also what the pre-#158 argv form silently did. Since no leg can deliver
      the original bytes, refuse loudly rather than corrupt quietly. This
      applies identically to a NAME, which rides the same JSON marshalling and
      the same per-line ``utf8.Valid``; checking it here is also what keeps
      :func:`_env_file_line_blocker`'s length measurement from raising
      ``UnicodeEncodeError`` out of the builder.
    """
    if _NUL in value:
        return "its value contains a NUL byte, which no transport can carry"
    if _NUL in name:
        return "its name contains a NUL byte, which no transport can carry"
    for part, label in ((value, "value"), (name, "name")):
        try:
            part.encode("utf-8")
        except UnicodeEncodeError:
            return (
                f"its {label} is not valid UTF-8; docker's env-file parser "
                f"rejects the file outright and the client environment would "
                f"silently replace the byte with U+FFFD"
            )
    return None


def _env_file_line_blocker(name: str, value: str) -> str | None:
    """Why ``name=value`` cannot be a faithful env-file line, or None.

    Every branch is a shape one of the runtimes would misread, silently alter,
    or reject outright — verified against docker 29.6.2 in review, not
    inferred:

    - a name with LEADING whitespace is silently RENAMED: docker runs
      ``strings.TrimLeftFunc(line, unicode.IsSpace)`` before splitting on
      ``=``, and Go's ``unicode.IsSpace`` covers NBSP, VT, FF and U+3000 as
      well as space and tab, so ``'\xa0FOO=kept'`` arrives as ``FOO``. Harmless
      on its own, but last-wins ``Config.Env`` means a real ``GITLAB_TOKEN``
      plus a stray NBSP-prefixed copy collapse into one name and the stale
      value takes effect. Reachable from a *quoted* ``.env`` key — the loader
      normalises a bare one. Python's strip set is a superset of Go's
      ``unicode.White_Space``, so ``lstrip()`` cannot under-detect;
    - a name containing a space or tab anywhere else makes docker abort the
      spawn ("variable 'M N' contains whitespaces"), while podman's
      ``ParseFile`` validates nothing and accepts it, so the outcome is
      runtime-dependent. Interior NBSP/VT/FF are NOT blocked: docker parses
      them, so refusing would reject a spawn no runtime objects to;
    - a leading ``#`` reads as a comment and an empty name is rejected, both
      of which would silently drop the variable;
    - a line at or over 64 KiB exceeds ``bufio.Scanner``'s token limit and
      aborts the spawn on both.

    A ``=`` inside a name is deliberately NOT a blocker: ``FOO=BAR=x`` splits
    on the first ``=`` into ``FOO`` / ``BAR=x``, exactly what
    ``-e FOO=BAR=x`` has always meant.
    """
    if any(ch in value for ch in _UNREPRESENTABLE_IN_ENV_FILE):
        return "its value contains a newline"
    if not name:
        return "its name is empty"
    if name != name.lstrip():
        return "its name has leading whitespace, which docker's parser strips"
    if any(ch in name for ch in _ENV_FILE_NAME_REJECTS):
        return "its name contains a space or tab, which docker rejects outright"
    if name.startswith("#"):
        return "its name would be read as a comment"
    if any(ch in name for ch in _UNREPRESENTABLE_IN_ENV_FILE):
        return "its name contains a newline"
    encoded = len(f"{name}={value}".encode("utf-8"))
    if encoded >= _ENV_FILE_MAX_LINE_BYTES:
        return (
            f"the encoded NAME=value line is {encoded} bytes, at or over the "
            f"{_ENV_FILE_MAX_LINE_BYTES}-byte limit both runtimes' env-file "
            f"parsers impose"
        )
    return None


def _marker_name_blocker(name: str) -> str | None:
    """Why ``name`` cannot be a bare ``-e NAME`` marker, or None.

    A bare name is looked up in the client's environment, so the only real
    constraints are the ones that lookup cannot express. Verified against
    docker 29.6.2 in review: ``-e -FOO`` and ``-e "M N"`` both resolve
    correctly (pflag consumes the operand after ``-e`` verbatim, and the
    lookup does no name validation), so neither a leading ``-`` nor a space is
    blocked here — both are shapes ``dotenv_values`` produces, and blocking
    them would refuse a launch that previously worked.

    What remains: an empty name has nothing to look up, ``=`` and NUL cannot
    survive ``execve``, and podman's ``parseEnv`` glob-expands a **trailing**
    ``*`` (``strings.HasSuffix``) — an interior ``*`` is an ordinary
    character.

    The NUL branch is a tripwire rather than a live one: a NUL in a name is
    refused up front by :func:`_undeliverable_reason`, since the env-file leg
    cannot carry it either. Kept so this function stays true standalone.
    """
    if not name:
        return "its name is empty"
    if "=" in name:
        return "its name contains '='"
    if _NUL in name:
        return "its name contains a NUL byte"
    if name.endswith("*"):
        return "its name ends with '*', which podman glob-expands"
    return None


@dataclass
class ContainerEnv:
    """How a session's environment reaches its container, never via argv.

    ``args`` are appended to the ``docker``/``podman run`` command.
    ``client_env`` is the environment the runtime *client* must be started
    with — non-empty only when some value could not be written to the env
    file and rides an inheritance marker instead. ``env_file_dir`` is the
    private temp directory holding the env file; :meth:`cleanup` removes it
    and must run once the run command has returned.
    """

    args: List[str] = field(default_factory=list)
    client_env: dict = field(default_factory=dict)
    env_file_dir: Path | None = None

    def subprocess_env(self) -> dict | None:
        """``env=`` for the runtime client, or None to inherit unchanged.

        Returning None when nothing rides the marker leg keeps the common
        path byte-identical to passing no ``env=`` at all. The overlay is
        applied on top of ``os.environ`` rather than replacing it, so the
        client keeps the PATH that resolves its own binary.
        """
        if not self.client_env:
            return None
        return {**os.environ, **self.client_env}

    def cleanup(self) -> None:
        """Remove the env file and its directory. Idempotent."""
        if self.env_file_dir is not None:
            shutil.rmtree(self.env_file_dir, ignore_errors=True)
            self.env_file_dir = None


def _write_env_file(lines: List[str]) -> Path:
    """Write ``lines`` to a mode-0600 file in a fresh mode-0700 directory.

    Owns cleanup for its own failures: the caller only ever receives a
    directory it can clean via :meth:`ContainerEnv.cleanup`, so anything
    raised between ``mkdtemp`` and the return would otherwise leave a
    directory nobody holds a handle to — and on a partial write, one with
    token lines already in it.
    """
    directory = Path(tempfile.mkdtemp(prefix="lmer-env-"))
    try:
        path = directory / _ENV_FILE_NAME
        # O_EXCL with an explicit mode rather than write_text(), so the value
        # bytes are never briefly group/world-readable under a lax umask.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            # fdopen did not take ownership, so the descriptor is still ours to
            # close — otherwise this path leaks one fd per failure.
            os.close(fd)
            raise
        # Plain UTF-8: a value that cannot encode is refused up front by
        # _unsupported_value_reason, because no leg can deliver those bytes.
        with handle:
            handle.write("".join(f"{line}\n" for line in lines))
        return path
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def build_container_env(env: dict) -> ContainerEnv:
    """
    Build the runtime arguments that carry ``env`` into the container.

    Values never enter argv, where ``/proc/<pid>/cmdline`` — world-readable —
    exposed every token a session carries to any user on the host for the
    lifetime of the spawn (issue #158). Two legs:

    - Anything the env-file format carries faithfully (see
      :func:`_env_file_line_blocker`) becomes a ``NAME=value`` line in a
      mode-0600 file inside a mode-0700 temp directory, passed as
      ``--env-file``, which docker and podman both accept.
    - Anything else rides a bare ``-e NAME`` marker, which both runtimes
      document as "take the value from the client environment", with the
      value handed over in ``client_env``. ``/proc/<pid>/environ`` is mode
      0400, owner-only, unlike ``cmdline``. Overwhelmingly this is a value
      containing a newline: ``LMER_START_PROMPT`` (``--prompt``),
      ``LMER_ANSWER`` (``--answer``) and Slack thread text all carry them.

    Args:
        env: Dictionary of environment variables (None values are skipped)

    Returns:
        A :class:`ContainerEnv`. The caller owns
        :meth:`ContainerEnv.cleanup`.

    Raises:
        ContainerEnvError: when neither leg can carry a variable. There is no
            inline ``-e NAME=value`` fallback — that is the exposure being
            closed — so the spawn fails loudly, naming the variable and the
            reason with the value redacted.
    """
    result = ContainerEnv()
    lines: List[str] = []
    for key, value in env.items():
        if value is None:
            continue
        text = value if isinstance(value, str) else str(value)
        undeliverable = _undeliverable_reason(key, text)
        if undeliverable is not None:
            raise ContainerEnvError(
                f"cannot forward {key} into the container: {undeliverable}. "
                f"Its value is not shown here. Fix or drop the variable — see "
                f"'How the environment reaches the container' in "
                f"docs/CONTAINER.md."
            )
        file_blocker = _env_file_line_blocker(key, text)
        if file_blocker is None:
            lines.append(f"{key}={text}")
            continue
        marker_blocker = _marker_name_blocker(key)
        if marker_blocker is None and not _client_reads_name(key):
            result.client_env[key] = text
            result.args += ["-e", key]
            continue
        reason = (
            f"the runtime client reads {key} from its own environment"
            if marker_blocker is None
            else marker_blocker
        )
        raise ContainerEnvError(
            f"cannot forward {key} into the container without exposing its "
            f"value in the process table: it cannot go in the env file "
            f"({file_blocker}), and it cannot be passed by name because "
            f"{reason}. Its value is not shown here. Rename or drop the "
            f"variable — see 'How the environment reaches the container' in "
            f"docs/CONTAINER.md."
        )

    # Postcondition, not a comment: every -e carries a bare name. This is the
    # "no value in argv" invariant the module exists for, enforced where it
    # cannot drift out of sync with the code. Checked BEFORE the env file is
    # written: raising after the write would strand the directory, since the
    # caller never receives a ContainerEnv to clean up. Unreachable by
    # construction today (marker names cannot contain "="); it is a tripwire
    # for a future leg, not a live branch.
    #
    # Walks the EMITTED PAIRS rather than every element. Scanning element-wise
    # and reading args[index + 1] ran off the end whenever a marker NAME was
    # itself "-e" — reachable from a plain `.env`, since dotenv_values('-e=…')
    # yields the key "-e" and a multi-line value routes it to the marker leg.
    # That turned a clean ContainerEnvError into an IndexError traceback,
    # because cli.py catches only the former.
    for flag, operand in zip(result.args[::2], result.args[1::2]):
        if flag == "-e" and "=" in operand:
            raise ContainerEnvError(
                "internal error: container env transport emitted an inline "
                f"-e {operand.split('=', 1)[0]}=… argument"
            )

    if lines:
        path = _write_env_file(lines)
        result.env_file_dir = path.parent
        result.args = ["--env-file", str(path)] + result.args
    return result
