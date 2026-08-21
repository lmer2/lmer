"""Short-lived, project-opt-in reuse for a full pre-commit pass.

Unlike the test cache, this cache is disabled unless the repository explicitly
opts in after auditing its hooks.  A hit means the exact ``--all-files``
invocation already passed over the exact same checked content, configuration,
hook revisions, executable, version and inherited environment.  Any unknown
input is a miss, and only successful runs whose keyed inputs did not move are
recorded.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

DEFAULT_CACHE_DIR = "/tmp/lmer-precommit-cache"
CACHE_DIR_ENV = "LMER_PRECOMMIT_CACHE_DIR"
CACHE_FORMAT_VERSION = 2
MAX_ENTRY_AGE_SECONDS = 15 * 60
CACHE_DIR_MODE = 0o700
CACHE_ENTRY_MODE = 0o600
ENTRY_SUFFIX = ".json"

Runner = Callable[[List[str], bool], Tuple[int, str, str]]


@dataclass(frozen=True)
class Fingerprint:
    key: str
    content: str
    git_state: str
    config: str
    executable_path: str
    executable_digest: str
    executable_version: str
    argv: Tuple[str, ...]
    environment: str


def _digest(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _file_digest(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _environment_digest(environment: Mapping[str, str]) -> Optional[str]:
    """Digest every inherited variable without persisting any value."""
    if not all(
        isinstance(name, str) and isinstance(value, str)
        for name, value in environment.items()
    ):
        return None
    hashes = {
        name: _digest({"name": name, "value": value})
        for name, value in environment.items()
    }
    return _digest(hashes)


def _path_state(path: Path) -> Optional[Dict[str, Any]]:
    """Hash a checked path without following symlinks; missing is a real state."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"kind": "missing"}
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode):
        try:
            target = os.readlink(path)
        except OSError:
            return None
        return {"kind": "symlink", "target": os.fsencode(target).hex()}
    if stat.S_ISREG(info.st_mode):
        digest = _file_digest(path)
        if digest is None:
            return None
        return {
            "kind": "file",
            "digest": digest,
            "executable": bool(info.st_mode & 0o111),
        }
    if stat.S_ISDIR(info.st_mode):
        # Gitlinks appear as tracked directory names. Pre-commit excludes them
        # from file hooks, but their presence/type remains part of enumeration.
        return {"kind": "directory"}
    return None


def _attribute_file_state(path: Path) -> Optional[Dict[str, Any]]:
    """Hash the bytes Git reads from an attributes path, following symlinks."""
    try:
        if not path.exists():
            return {"kind": "missing"}
    except OSError:
        return None
    digest = _file_digest(path)
    return {"kind": "attributes", "digest": digest} if digest else None


def checked_content_digest(run: Runner, project_root: Path) -> Optional[str]:
    """Digest exactly the paths ``--all-files`` enumerates and their disk bytes.

    HEAD and index identities are deliberately absent. A landing train changes
    both while leaving the worktree bytes pre-commit reads unchanged; keying on
    either makes the intended post-commit reuse impossible.
    """
    code, stdout, _stderr = run(["git", "ls-files", "-z"], check=False)
    if code != 0:
        return None
    entries = []
    for name in stdout.split("\0"):
        if not name:
            continue
        state = _path_state(project_root / name)
        if state is None:
            return None
        entries.append({"path": name, "state": state})
    return _digest(entries)


def _git_path(run: Runner, project_root: Path, name: str) -> Optional[Path]:
    code, stdout, _stderr = run(
        ["git", "rev-parse", "--git-path", name], check=False
    )
    if code != 0 or not stdout.strip():
        return None
    path = Path(stdout.strip())
    return path if path.is_absolute() else project_root / path


def _git_state_digest(
    run: Runner, project_root: Path, environment: Mapping[str, str]
) -> Optional[str]:
    """Digest the bounded Git state used by this repo's audited pinned hooks."""
    # check-added-large-files computes added_files() from this exact Git
    # projection even under --all-files. Key the hook's input without restoring
    # broad HEAD/index identities that invalidate landing-train reuse when only
    # already-tracked modifications are committed.
    code, staged_added, _stderr = run(
        ["git", "diff", "--staged", "--name-only", "--diff-filter=A"],
        check=False,
    )
    if code != 0:
        return None

    paths: Dict[str, Path] = {}
    info_attributes = _git_path(run, project_root, "info/attributes")
    if info_attributes is None:
        return None
    paths["info_attributes"] = info_attributes

    code, stdout, _stderr = run(
        ["git", "config", "--path", "--get", "core.attributesFile"],
        check=False,
    )
    if code == 0 and stdout.strip():
        configured = Path(stdout.strip()).expanduser()
        paths["global_attributes"] = (
            configured if configured.is_absolute() else project_root / configured
        )
    elif code == 1:
        home = environment.get("HOME")
        xdg = environment.get("XDG_CONFIG_HOME")
        if xdg:
            paths["global_attributes"] = Path(xdg) / "git" / "attributes"
        elif home:
            paths["global_attributes"] = Path(home) / ".config" / "git" / "attributes"
        else:
            return None
    else:
        return None

    for name in ("MERGE_MSG", "MERGE_HEAD", "rebase-apply", "rebase-merge"):
        path = _git_path(run, project_root, name)
        if path is None:
            return None
        paths[name] = path

    states = []
    for name, path in sorted(paths.items()):
        state = (
            _attribute_file_state(path)
            if name in {"info_attributes", "global_attributes"}
            else _path_state(path)
        )
        if state is None:
            return None
        states.append({"name": name, "path": str(path), "state": state})
    return _digest({"paths": states, "staged_added": staged_added})


def _resolve_executable(command: Sequence[str]) -> Optional[Path]:
    if not command or not all(isinstance(part, str) and part for part in command):
        return None
    candidate = command[0]
    resolved = (
        shutil.which(candidate)
        if os.sep not in candidate
        else str(Path(candidate).expanduser().resolve())
    )
    if not resolved:
        return None
    path = Path(resolved)
    return path if path.is_file() else None


def compute_fingerprint(
    run: Runner,
    project_root: Path,
    command: Sequence[str],
    argv: Sequence[str],
    environment: Mapping[str, str],
) -> Optional[Fingerprint]:
    """Return an exact reusable identity, or ``None`` for any unknown input."""
    try:
        content = checked_content_digest(run, project_root)
        git_state = _git_state_digest(run, project_root, environment)
        config = _file_digest(project_root / ".pre-commit-config.yaml")
        executable = _resolve_executable(command)
        executable_digest = _file_digest(executable) if executable else None
        environment_digest = _environment_digest(environment)
        if not all((content, git_state, config, executable, executable_digest)):
            return None
        if environment_digest is None:
            return None
        code, stdout, stderr = run([*command, "--version"], check=False)
        if code != 0:
            return None
        version = (stdout + stderr).strip()
        if not version:
            return None
        identity = {
            "format": CACHE_FORMAT_VERSION,
            "content": content,
            "git_state": git_state,
            # The config digest necessarily covers every pinned hook revision.
            "config": config,
            "executable_path": str(executable),
            "executable_digest": executable_digest,
            "executable_version": version,
            "argv": list(argv),
            "environment": environment_digest,
        }
        return Fingerprint(
            key=_digest(identity),
            content=content,
            git_state=git_state,
            config=config,
            executable_path=str(executable),
            executable_digest=executable_digest,
            executable_version=version,
            argv=tuple(argv),
            environment=environment_digest,
        )
    except (OSError, TypeError, ValueError):
        return None


def cache_dir() -> Path:
    return Path(os.environ.get(CACHE_DIR_ENV, "").strip() or DEFAULT_CACHE_DIR)


def safe_cache_dir(create: bool = False) -> Optional[Path]:
    """Return an owner-only cache directory, or ``None`` (a cache miss)."""
    directory = cache_dir()
    try:
        if directory.is_symlink():
            return None
        if not directory.is_dir():
            if not create:
                return None
            directory.mkdir(mode=CACHE_DIR_MODE, parents=True, exist_ok=True)
            directory.chmod(CACHE_DIR_MODE)
            return directory
        info = directory.stat()
        if info.st_uid != os.geteuid():
            return None
        if stat.S_IMODE(info.st_mode) & ~CACHE_DIR_MODE:
            directory.chmod(CACHE_DIR_MODE)
        return directory
    except OSError:
        return None


def _entry_path(fingerprint: Fingerprint, directory: Path) -> Path:
    return directory / f"{fingerprint.key}{ENTRY_SUFFIX}"


def _open_no_follow(path: str, flags: int) -> int:
    return os.open(path, flags | os.O_NOFOLLOW)


def read_pass(
    fingerprint: Optional[Fingerprint], now: Optional[float] = None
) -> Optional[Dict[str, Any]]:
    """Return a current successful entry matching every identity field."""
    if fingerprint is None:
        return None
    directory = safe_cache_dir()
    if directory is None:
        return None
    try:
        with open(
            _entry_path(fingerprint, directory),
            "r",
            encoding="utf-8",
            opener=_open_no_follow,
        ) as handle:
            entry = json.load(handle)
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        return None
    if not isinstance(entry, dict):
        return None
    expected = {
        "format": CACHE_FORMAT_VERSION,
        "outcome": "pass",
        "key": fingerprint.key,
        "content": fingerprint.content,
        "git_state": fingerprint.git_state,
        "config": fingerprint.config,
        "executable_path": fingerprint.executable_path,
        "executable_digest": fingerprint.executable_digest,
        "executable_version": fingerprint.executable_version,
        "argv": list(fingerprint.argv),
        "environment": fingerprint.environment,
    }
    if any(entry.get(name) != value for name, value in expected.items()):
        return None
    try:
        age = (now if now is not None else time.time()) - float(entry["created_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return entry if 0 <= age <= MAX_ENTRY_AGE_SECONDS else None


def record_pass(
    fingerprint: Optional[Fingerprint], now: Optional[float] = None
) -> Optional[Path]:
    """Atomically record one successful, unchanged full run; fail soft."""
    if fingerprint is None:
        return None
    directory = safe_cache_dir(create=True)
    if directory is None:
        return None
    path = _entry_path(fingerprint, directory)
    entry = {
        "format": CACHE_FORMAT_VERSION,
        "outcome": "pass",
        "created_at": now if now is not None else time.time(),
        "key": fingerprint.key,
        "content": fingerprint.content,
        "git_state": fingerprint.git_state,
        "config": fingerprint.config,
        "executable_path": fingerprint.executable_path,
        "executable_digest": fingerprint.executable_digest,
        "executable_version": fingerprint.executable_version,
        "argv": list(fingerprint.argv),
        "environment": fingerprint.environment,
    }
    temp = directory / f".{fingerprint.key}.{os.getpid()}{ENTRY_SUFFIX}"
    descriptor: Optional[int] = None
    try:
        payload = (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8")
        descriptor = os.open(
            str(temp),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            CACHE_ENTRY_MODE,
        )
        os.write(descriptor, payload)
        os.close(descriptor)
        descriptor = None
        os.replace(temp, path)
        return path
    except (OSError, TypeError, ValueError):
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            temp.unlink()
        except OSError:
            pass
        return None
