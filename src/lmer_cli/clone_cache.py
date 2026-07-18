"""Host-side maintenance for the persistent git clone cache (issue #112).

The cache is a host directory of bare mirrors (``<host>/<project>.git``)
that containers consume **read-only**: working clones are made with
``--reference <mirror> --dissociate`` (see
``lmer_cli/container/clone_and_exec.py``), so a mirror only saves network —
a stale or absent mirror is never wrong, and a damaged one is covered by
the container's retry-direct fallback.

This module is the cache's **single writer**. The CLI forks it detached at
launch (``python -m lmer_cli.clone_cache``, repo URLs on stdin) so a
session never waits on mirror maintenance; the same entrypoint serves
external schedulers (systemd timer, k8s CronJob) that want to warm a cache
on a cadence.

Credential rules (the reason this code lives host-side at all):

- The tokenized URL is **never written to disk**: mirrors are created with
  ``git init --bare`` storing only the scrubbed URL as origin, and every
  fetch passes the URL explicitly.
- The token is **never on any argv**: git children fetch the *scrubbed*
  URL, with credentials injected via ephemeral ``GIT_CONFIG_*`` process
  env (an ``http.<url>.extraHeader`` Authorization header).
- Updater output is credential-scrubbed and size-capped, and the log lives
  **outside the cache root** so containers can never read it.

Everything is fail-soft: any per-mirror trouble is logged and skipped; the
updater must never break or delay an lmer run.
"""

from __future__ import annotations

import base64
import fcntl
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

# Host→container imports are allowed (the container script is the side that
# must stay standalone); reusing the mapping + scrub keeps the two sides
# from ever drifting.
from lmer_cli.container.clone_and_exec import _mirror_path, _scrub_credentials
from lmer_cli.mounts import resolve_host_clone_cache_dir

STALENESS_WINDOW_S = 15 * 60  # skip a fetch when the stamp is this fresh
FETCH_TIMEOUT_S = 300  # refresh of an existing mirror
CREATE_TIMEOUT_S = 900  # initial build of a large repo's mirror
LOCK_DROPPING_MAX_AGE_S = 3600  # git *.lock files older than this are litter
LOG_MAX_BYTES = 1_000_000  # clone-cache.log cap (truncate-and-restart)


def mirror_path(cache_root: Path, repo_url: str) -> "Path | None":
    """The container's URL→``<host>/<project>.git`` mapping, reused verbatim."""
    return _mirror_path(Path(cache_root), repo_url)


def _log_path() -> Path:
    return Path.home() / ".lmer" / "logs" / "clone-cache.log"


def _log(message: str) -> None:
    """Append a scrubbed, timestamped line; never raise, never grow unbounded."""
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            path.write_text("")  # cap by restart: this is a diagnostic log
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with path.open("a") as f:
            f.write(f"{stamp} {_scrub_credentials(message)}\n")
    except OSError:
        pass


def _split_credentials(repo_url: str) -> "tuple[str, dict[str, str]]":
    """Split an http(s) URL into (scrubbed URL, ephemeral git-config env).

    The env entries carry an ``http.<url>.extraHeader`` Authorization header
    so the token reaches git without ever appearing on an argv (host ``ps``
    shows every command line for the full duration of an initial mirror
    build). Non-http URLs and URLs without userinfo pass through untouched.
    """
    if "://" not in repo_url:
        return repo_url, {}
    try:
        parsed = urlparse(repo_url)
    except Exception:
        return repo_url, {}
    if not parsed.username and not parsed.password:
        return repo_url, {}
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    scrubbed = f"{parsed.scheme}://{host}{parsed.path or ''}"
    userinfo = f"{unquote(parsed.username or '')}:{unquote(parsed.password or '')}"
    token = base64.b64encode(userinfo.encode()).decode()
    env = {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": f"http.{scrubbed}.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {token}",
        # reset any inherited credential helpers: nothing may prompt or
        # substitute other credentials under a detached updater
        "GIT_CONFIG_KEY_1": "credential.helper",
        "GIT_CONFIG_VALUE_1": "",
    }
    return scrubbed, env


def _git_env(cred_env: "dict[str, str]") -> "dict[str, str]":
    """Non-interactive git environment: a detached updater holding a flock
    must never hang on a credential prompt or an ssh host-key question."""
    env = dict(os.environ)
    env.update(cred_env)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env["SSH_ASKPASS"] = ""
    env["GIT_SSH_COMMAND"] = env.get("GIT_SSH_COMMAND", "ssh") + " -oBatchMode=yes"
    return env


def _run_git(args: "list[str]", timeout: float, cred_env: "dict[str, str]") -> None:
    """Run git non-interactively; raise on failure with scrubbed output.

    stdout/stderr are captured (never inherited — the caller may have
    redirected output somewhere containers can read) and surface only via
    the scrubbed exception message.
    """
    result = subprocess.run(
        ["git", *args],
        env=_git_env(cred_env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {args[0]} failed (exit {result.returncode}): "
            f"{_scrub_credentials((result.stderr or result.stdout or '').strip())}"
        )


def _stamp_path(mirror: Path) -> Path:
    return mirror.with_name(mirror.name + ".stamp")


def _clean_lock_droppings(mirror: Path) -> None:
    """Remove git lock files a killed/suspended fetch left behind.

    git never cleans these up itself, so one SIGKILL mid-fetch would
    otherwise wedge the mirror forever — with fail-soft logging hiding it.
    Only locks older than :data:`LOCK_DROPPING_MAX_AGE_S` are removed; a
    fresh lock may belong to a live git process.
    """
    cutoff = time.time() - LOCK_DROPPING_MAX_AGE_S
    candidates = [mirror / "packed-refs.lock", mirror / "config.lock"]
    refs = mirror / "refs"
    if refs.is_dir():
        candidates.extend(refs.rglob("*.lock"))
    for lock in candidates:
        try:
            if lock.is_file() and lock.stat().st_mtime < cutoff:
                lock.unlink()
        except OSError:
            continue


def _sweep_stale_tmps(cache_root: Path) -> None:
    """Delete leftover ``*.git.tmp`` build dirs (a killed create — including
    the pre-rework code's crash window — leaves one, possibly with a
    tokenized URL in its config). Skips any tmp whose sibling lock is held:
    that build is live, not stale."""
    for tmp in cache_root.rglob("*.git.tmp"):
        if not tmp.is_dir():
            continue
        lock_path = tmp.with_name(tmp.name[: -len(".tmp")] + ".lock")
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            continue  # held: a live updater owns this tmp
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        finally:
            os.close(fd)


def _default_branch(url: str, cred_env: "dict[str, str]") -> "str | None":
    """The remote's HEAD target (``refs/heads/<branch>``), or None."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--symref", url, "HEAD"],
            env=_git_env(cred_env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("ref:") and "\tHEAD" in line:
            return line.split()[1]
    return None


def _create_mirror(mirror: Path, scrubbed_url: str, cred_env: "dict[str, str]") -> None:
    """Build a ``clone --mirror``-equivalent bare repo without the token ever
    touching disk: init + scrubbed origin + explicit-URL fetch, in a sibling
    ``.tmp`` renamed into place. Any failure removes the tmp before
    propagating — there is no state in which a half-built dir survives."""
    tmp = mirror.with_name(mirror.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    mirror.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run_git(["init", "--bare", "--quiet", str(tmp)], timeout=30, cred_env={})
        for key, value in (
            ("remote.origin.url", scrubbed_url),
            ("remote.origin.mirror", "true"),
            ("remote.origin.fetch", "+refs/*:refs/*"),
            # fetch-triggered auto-gc deletes packfiles, which would yank
            # borrowed objects out from under a container mid-clone
            ("gc.auto", "0"),
            ("maintenance.auto", "false"),
        ):
            _run_git(["-C", str(tmp), "config", key, value], timeout=30, cred_env={})
        _run_git(
            ["-C", str(tmp), "fetch", "--quiet", scrubbed_url, "+refs/*:refs/*"],
            timeout=CREATE_TIMEOUT_S,
            cred_env=cred_env,
        )
        head = _default_branch(scrubbed_url, cred_env)
        if head:
            _run_git(["-C", str(tmp), "symbolic-ref", "HEAD", head], timeout=30, cred_env={})
        tmp.rename(mirror)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _update_one(mirror: Path, repo_url: str) -> None:
    """Create or refresh one mirror. Must be called under its flock."""
    scrubbed_url, cred_env = _split_credentials(repo_url)
    stamp = _stamp_path(mirror)
    if (mirror / "HEAD").exists():
        try:
            if stamp.stat().st_mtime > time.time() - STALENESS_WINDOW_S:
                return
        except OSError:
            pass  # no stamp: treat as stale
        _clean_lock_droppings(mirror)
        _run_git(
            ["-C", str(mirror), "fetch", "--quiet", "--prune", scrubbed_url,
             "+refs/*:refs/*"],
            timeout=FETCH_TIMEOUT_S,
            cred_env=cred_env,
        )
    else:
        if mirror.is_dir():
            # HEAD-less non-empty dir (partial manual deletion, external
            # damage): _create_mirror's tmp.rename would fail with ENOTEMPTY
            # after paying the full transfer, on every run — clear it first.
            # Safe: we hold this mirror's flock.
            _log(f"{mirror.name}: removing damaged mirror (no HEAD)")
            shutil.rmtree(mirror)
        _create_mirror(mirror, scrubbed_url, cred_env)
    stamp.touch()


def update_mirrors(urls: "list[str]", cache_root: Path) -> None:
    """Create/refresh the mirrors for *urls* under *cache_root*. Fail-soft:
    every failure is logged and skipped; this function never raises and
    never blocks on another updater's lock."""
    if shutil.which("git") is None:
        _log("git not found on host; skipping cache update")
        return
    cache_root = Path(cache_root)
    if not cache_root.is_dir():
        return
    _sweep_stale_tmps(cache_root)
    for repo_url in urls:
        repo_url = (repo_url or "").strip()
        if not repo_url:
            continue
        mirror = mirror_path(cache_root, repo_url)
        if mirror is None:
            continue
        lock_path = mirror.with_name(mirror.name + ".lock")
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as e:
            _log(f"{mirror.name}: cannot open lock: {e}")
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue  # another updater owns this mirror right now
            _update_one(mirror, repo_url)
        except Exception as e:
            _log(f"{mirror.name}: update failed: {e}")
        finally:
            os.close(fd)


def main() -> int:
    """Entrypoint for ``python -m lmer_cli.clone_cache``: one repo URL per
    stdin line (credentialed URLs allowed — stdin is not visible in ``ps``),
    cache root resolved exactly like the mount (LMER_CLONE_CACHE_DIR or
    ``~/.lmer/clone-cache``). Always exits 0: a cache warmer has no caller
    that could act on failure."""
    urls = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
    cache_root = resolve_host_clone_cache_dir()
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
        update_mirrors(urls, cache_root)
    except Exception as e:  # belt and braces: fail-soft even for bugs here
        _log(f"updater crashed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
