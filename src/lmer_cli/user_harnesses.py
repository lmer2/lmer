"""User-installed harness definitions (issue #132).

A *user harness* is an agent harness added to lmer without touching the lmer
codebase: a drop-in directory holding a declarative manifest plus the runner
script that launches the harness inside the session container::

    ~/.lmer/harnesses/<name>/
    ├── harness.json     # serialized registry entry (schema 1)
    ├── runner.sh        # in-container runner; installs its CLI if missing
    └── agent-files/     # optional base config the runner provisions

The directory name is the harness name. ``LMER_HARNESSES_DIR`` overrides the
location; the host CLI mounts the directory read-only into the container at
``/lmer-harnesses`` and forwards that path in the same variable, so the host
CLI, ``lmer-supervisor``, and ``spawn-harness`` all resolve the same
definitions from the same files (``clone_and_exec.py`` — which runs without
the ``lmer_cli`` import path — dispatches user runner tokens purely by file
existence against the same directory).

Trust model — identical to presets (:mod:`lmer_cli.presets`): the directory
lives on the host and is writable only by whoever runs lmer. lmer already
executes operator-controlled code inside the container (the target repo, the
runner scripts, preset-selected checkouts); a user-authored runner adds no
new exposure class, and the container remains the security boundary. One
carve-out survives from the built-in registry: a manifest's
``permission_bypass_args`` still only apply when a caller passes
``build_exec_argv(..., unattended=True)``.

Loading degrades gracefully, mirroring presets: a missing directory yields no
harnesses, and a broken entry (malformed JSON, unsupported schema, invalid
name, built-in collision) is warned about and skipped so it cannot break
sessions that don't select it — while *selecting* a skipped harness fails
with the normal unknown-harness error. Warnings print once per process per
directory (the load is cached).

Unlike the built-ins, a user harness has no Containerfile install layer: its
``runner.sh`` owns CLI availability (install-if-missing at session start),
with a persistent cache volume (``LMER_HARNESS_CACHE``) mounted by the host
so later sessions skip the install. The user-facing guide is
``docs/HARNESSES.md`` § "User-installed harnesses".
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

from .harness import (
    HARNESSES,
    CredentialMount,
    ExecProfile,
    GENERIC_START_COMMAND,
    Harness,
    SupervisorProfile,
)
from .util import decode_escape_bytes

#: Env var overriding the user-harness directory. Host-side it names the host
#: directory to load and mount; container-side the host CLI sets it to the
#: mount point (:data:`CONTAINER_HARNESSES_DIR`) so in-container code resolves
#: the same definitions. Also consumed (via the environment, not this module)
#: by ``clone_and_exec.py``, which runs without the lmer_cli import path.
HARNESSES_DIR_ENV = "LMER_HARNESSES_DIR"

#: Default host location of user-harness definitions.
DEFAULT_HARNESSES_DIR = Path.home() / ".lmer" / "harnesses"

#: Where the host mounts the user-harness directory inside the container.
#: Deliberately NOT under ``~/.lmer``: creating that directory in the
#: container would flip ``harness_find_global_dir`` (harness-common.sh) away
#: from ``/Agents/global`` and break agent-files provisioning.
CONTAINER_HARNESSES_DIR = "/lmer-harnesses"

#: Env var carrying the session harness's persistent install-cache directory
#: (inside the container). The runner owns the layout — typical use is an npm
#: prefix with ``$LMER_HARNESS_CACHE/bin`` prepended to PATH.
HARNESS_CACHE_ENV = "LMER_HARNESS_CACHE"

#: Default host location of the per-harness install caches.
DEFAULT_HARNESS_CACHE_DIR = Path.home() / ".lmer" / "harness-cache"

#: Where the host mounts the install-cache directory inside the container.
CONTAINER_HARNESS_CACHE_DIR = "/lmer-harness-cache"

#: The runner script a user-harness directory must carry.
RUNNER_FILENAME = "runner.sh"

#: The manifest filename.
MANIFEST_FILENAME = "harness.json"

#: Manifest schema versions this loader understands.
SUPPORTED_SCHEMAS = (1,)

#: Harness names must be usable as a directory name, an env value, and a
#: ``<name>-runner`` dispatch token; resolution lowercases names, so the
#: manifest side is lowercase-only from the start.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

#: extra_env keys a manifest may NOT set: HOME/PATH would corrupt the
#: container layout, and LMER_* would let a manifest silently override
#: lmer's own contract vars (e.g. LMER_HARNESS re-routing the supervisor
#: away from the runner actually launched). extra_env is for the harness's
#: own variables (the built-in precedent: PI_SKIP_VERSION_CHECK).
_RESERVED_EXTRA_ENV = ("HOME", "PATH")

#: Keys a manifest may define at the top level; unknown keys warn (typo
#: signal) without invalidating the entry, mirroring presets.
_KNOWN_KEYS = frozenset(
    {
        "schema",
        "description",
        "binary",
        "credential_mounts",
        "supervisor",
        "exec",
        "model_hints",
        "extra_env",
    }
)

# Load cache, keyed by resolved directory path so tests pointing
# LMER_HARNESSES_DIR elsewhere never see stale results, while a session's
# repeated resolutions neither re-read files nor repeat warnings.
_cache: Dict[str, Dict[str, Harness]] = {}


def _warn(message: str) -> None:
    print(f"⚠️  {message}", file=sys.stderr)


def user_harnesses_dir(environ: Optional[dict] = None) -> Path:
    """The user-harness directory for this process.

    ``LMER_HARNESSES_DIR`` wins (host override, and how the container is told
    the mount point); the default is the host-side ``~/.lmer/harnesses``.
    """
    env = environ if environ is not None else os.environ
    raw = (env.get(HARNESSES_DIR_ENV) or "").strip()
    return Path(raw) if raw else DEFAULT_HARNESSES_DIR


def _decode_escape_bytes(value: str, what: str, name: str) -> Optional[bytes]:
    """Decode a manifest string into bytes (shared :func:`decode_escape_bytes`
    semantics — same encoding as the supervisor's env overrides). Returns
    ``None`` (with a warning) on an undecodable value."""
    try:
        return decode_escape_bytes(value)
    except UnicodeDecodeError as exc:
        _warn(f"User harness {name!r}: cannot decode {what} {value!r}: {exc}")
        return None


def _str_tuple(value, what: str, name: str) -> Optional[Tuple[str, ...]]:
    """Validate a manifest list-of-strings field."""
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        _warn(f"User harness {name!r}: {what} must be a list of strings")
        return None
    return tuple(value)


def _parse_credential_mounts(raw, name: str) -> Optional[Tuple[CredentialMount, ...]]:
    if not isinstance(raw, list):
        _warn(f"User harness {name!r}: credential_mounts must be a list")
        return None
    mounts = []
    for entry in raw:
        if not isinstance(entry, dict):
            _warn(f"User harness {name!r}: credential_mounts entries must be objects")
            return None
        host_path = entry.get("host_path")
        container_path = entry.get("container_path")
        mode = entry.get("mode", "rw")
        if not isinstance(host_path, str) or not host_path:
            _warn(f"User harness {name!r}: credential mount needs a host_path")
            return None
        if not isinstance(container_path, str) or not container_path.startswith("/"):
            _warn(
                f"User harness {name!r}: credential mount needs an absolute container_path"
            )
            return None
        # ':'/','/ whitespace would smuggle extra fields or options into the
        # runtime's `-v host:container:mode` string, bypassing the mode gate.
        if any(re.search(r"[:,\s]", p) for p in (host_path, container_path)):
            _warn(
                f"User harness {name!r}: credential mount paths must not "
                "contain ':', ',' or whitespace"
            )
            return None
        # Path() normalizes "." away, so parts == () catches host_path="."
        # (which would mount the entire host home).
        parts = Path(host_path).parts
        if Path(host_path).is_absolute() or not parts or ".." in parts:
            _warn(
                f"User harness {name!r}: credential mount host_path must be "
                f"a plain path relative to the host home directory (got {host_path!r})"
            )
            return None
        if mode not in ("rw", "ro"):
            _warn(f"User harness {name!r}: credential mount mode must be 'rw' or 'ro'")
            return None
        mounts.append(CredentialMount(host_path, container_path, mode))
    return tuple(mounts)


def _parse_supervisor(raw, name: str) -> Optional[SupervisorProfile]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        _warn(f"User harness {name!r}: supervisor must be an object")
        return None
    # Defaults are the safest possible TUI profile: no ready-marker gating
    # (the supervisor falls back to its fixed delays), the generic start
    # instruction, and no quit chord (shutdown escalates straight to
    # SIGTERM). Every field remains env-overridable at runtime like the
    # built-ins (LMER_AUTO_START_READY_MARKER etc.).
    marker_raw = raw.get("ready_marker", "")
    if not isinstance(marker_raw, str):
        _warn(f"User harness {name!r}: supervisor.ready_marker must be a string")
        return None
    ready_marker = _decode_escape_bytes(marker_raw, "supervisor.ready_marker", name)
    if ready_marker is None:
        return None

    # Type-check the RAW value before applying the empty default — `false`,
    # `0`, `[]` are wrong types, not requests for the generic instruction.
    start_command = raw.get("start_command", "")
    if not isinstance(start_command, str):
        _warn(f"User harness {name!r}: supervisor.start_command must be a string")
        return None
    start_command = start_command or GENERIC_START_COMMAND

    quit_raw = raw.get("quit_sequence", [])
    if not isinstance(quit_raw, list) or not all(isinstance(s, str) for s in quit_raw):
        _warn(f"User harness {name!r}: supervisor.quit_sequence must be a list of strings")
        return None
    quit_sequence = []
    for step in quit_raw:
        decoded = _decode_escape_bytes(step, "supervisor.quit_sequence step", name)
        if decoded is None:
            return None
        if decoded:
            quit_sequence.append(decoded)

    ready_timeout = raw.get("ready_timeout")
    if ready_timeout is not None and (
        isinstance(ready_timeout, bool) or not isinstance(ready_timeout, (int, float))
    ):
        _warn(f"User harness {name!r}: supervisor.ready_timeout must be a number")
        return None

    return SupervisorProfile(
        ready_marker=ready_marker,
        start_command=start_command,
        quit_sequence=tuple(quit_sequence),
        ready_timeout=float(ready_timeout) if ready_timeout is not None else None,
    )


def _parse_exec_profile(raw, name: str) -> Optional[ExecProfile]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        _warn(f"User harness {name!r}: exec must be an object")
        return None
    fields = {}
    for key in ("base_args", "permission_bypass_args", "model_args", "effort_args"):
        value = _str_tuple(raw.get(key, []), f"exec.{key}", name)
        if value is None:
            return None
        fields[key] = value
    effort_max_value = raw.get("effort_max_value", "max")
    if not isinstance(effort_max_value, str) or not effort_max_value:
        _warn(f"User harness {name!r}: exec.effort_max_value must be a string")
        return None
    dashdash = raw.get("dashdash_before_prompt", False)
    if not isinstance(dashdash, bool):
        _warn(f"User harness {name!r}: exec.dashdash_before_prompt must be a boolean")
        return None
    return ExecProfile(
        base_args=fields["base_args"],
        permission_bypass_args=fields["permission_bypass_args"],
        model_args=fields["model_args"],
        effort_args=fields["effort_args"],
        effort_max_value=effort_max_value,
        dashdash_before_prompt=dashdash,
    )


def _parse_manifest(name: str, harness_dir: Path, manifest: dict) -> Optional[Harness]:
    """Build a :class:`Harness` from one validated manifest, or ``None``."""
    schema = manifest.get("schema")
    # Exact int only: bool is an int subclass (`true` == 1) and `1.0 == 1`,
    # so both would otherwise pass as schema 1 — neither is a valid schema.
    if type(schema) is not int or schema not in SUPPORTED_SCHEMAS:
        _warn(
            f"User harness {name!r}: unsupported schema {schema!r} "
            f"(supported: {', '.join(str(s) for s in SUPPORTED_SCHEMAS)}) — skipped"
        )
        return None

    unknown = set(manifest) - _KNOWN_KEYS
    if unknown:
        _warn(
            f"User harness {name!r}: ignoring unknown manifest key(s): "
            f"{', '.join(sorted(unknown))}"
        )

    binary = manifest.get("binary")
    if not isinstance(binary, str) or not binary:
        _warn(f"User harness {name!r}: manifest needs a non-empty 'binary' — skipped")
        return None

    description = manifest.get("description", "")
    if not isinstance(description, str):
        _warn(f"User harness {name!r}: description must be a string — skipped")
        return None

    credential_mounts = _parse_credential_mounts(
        manifest.get("credential_mounts", []), name
    )
    supervisor = _parse_supervisor(manifest.get("supervisor"), name)
    exec_profile = _parse_exec_profile(manifest.get("exec"), name)
    if credential_mounts is None or supervisor is None or exec_profile is None:
        return None

    hints = _str_tuple(manifest.get("model_hints", []), "model_hints", name)
    if hints is None:
        return None
    if any(not re.search(r"\w", hint) for hint in hints):
        # A hint with no word character produces a catch-all pattern in
        # harness_for_model: "" compiles to \b\b (matches every model name),
        # "-" to \b\-\b (matches every hyphenated id) — silently rerouting
        # otherwise-unhinted models to this harness.
        _warn(
            f"User harness {name!r}: every model_hints entry must contain "
            "a word character — skipped"
        )
        return None

    extra_env_raw = manifest.get("extra_env", {})
    if not isinstance(extra_env_raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in extra_env_raw.items()
    ):
        _warn(f"User harness {name!r}: extra_env must be an object of strings — skipped")
        return None
    reserved = sorted(
        k for k in extra_env_raw if k in _RESERVED_EXTRA_ENV or k.startswith("LMER_")
    )
    if reserved:
        _warn(
            f"User harness {name!r}: extra_env must not set reserved key(s) "
            f"{', '.join(reserved)} (HOME, PATH, LMER_*) — skipped"
        )
        return None

    return Harness(
        name=name,
        binary=binary,
        runner_command=f"{name}-runner",
        # The runner lives in the harness directory itself, not libexec/;
        # clone_and_exec locates it by convention (<dir>/<name>/runner.sh).
        runner_script=RUNNER_FILENAME,
        credential_mounts=credential_mounts,
        supervisor=supervisor,
        # No Containerfile install layer to bust — runner-owned install.
        cache_bust_arg="",
        exec_profile=exec_profile,
        description=description or f"{name} (user-installed harness)",
        extra_env=tuple(sorted(extra_env_raw.items())),
        source_dir=str(harness_dir),
        model_hints=hints,
    )


def _load_dir(root: Path) -> Dict[str, Harness]:
    loaded: Dict[str, Harness] = {}
    try:
        entries = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return loaded
    for harness_dir in entries:
        name = harness_dir.name
        manifest_path = harness_dir / MANIFEST_FILENAME
        if not manifest_path.is_file():
            # A stray directory (editor droppings, the cache dir a user
            # misplaced) is not necessarily a broken harness — stay quiet.
            continue
        if not _NAME_RE.match(name):
            _warn(
                f"User harness directory {name!r} has an invalid name "
                "(expected lowercase [a-z][a-z0-9_-]*, max 64 chars) — skipped"
            )
            continue
        if name in HARNESSES:
            _warn(
                f"User harness {name!r} would shadow the built-in harness of "
                "the same name — skipped (built-ins cannot be overridden)"
            )
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            # UnicodeDecodeError: a non-UTF-8 manifest must be skipped like
            # any other broken entry — read_text() raises it before json ever
            # runs, and letting it propagate would crash EVERY registry
            # resolution (including sessions on built-in harnesses).
            _warn(f"User harness {name!r}: cannot read {manifest_path}: {exc} — skipped")
            continue
        if not isinstance(manifest, dict):
            _warn(f"User harness {name!r}: manifest must be a JSON object — skipped")
            continue
        harness = _parse_manifest(name, harness_dir, manifest)
        if harness is None:
            continue
        if not (harness_dir / RUNNER_FILENAME).is_file():
            # Still loaded: the host needs the entry for credential mounts
            # and validation; the missing runner fails loudly at dispatch.
            _warn(
                f"User harness {name!r}: no {RUNNER_FILENAME} in {harness_dir} — "
                "the session container will fail to start it"
            )
        loaded[name] = harness
    return loaded


def load_user_harnesses(root: Optional[Path] = None) -> Dict[str, Harness]:
    """Load user-harness definitions from *root* (default: the resolved dir).

    Returns ``{name: Harness}``. Results are cached per directory path for
    the life of the process; :func:`clear_user_harness_cache` resets (tests).
    """
    directory = root if root is not None else user_harnesses_dir()
    key = str(directory)
    if key not in _cache:
        _cache[key] = _load_dir(directory) if directory.is_dir() else {}
    return _cache[key]


def clear_user_harness_cache() -> None:
    """Drop the per-directory load cache (test isolation)."""
    _cache.clear()
