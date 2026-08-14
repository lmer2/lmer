"""Host-side maintenance for the persistent git clone cache (issue #112).

The cache is a host directory of bare mirrors (``<host>/<project>.git``)
that containers consume **read-only** — and, since issue #135, only the
mirrors of the repos a given launch clones: working clones are made with
``--reference <mirror> --dissociate`` (see
``lmer_cli/container/clone_and_exec.py``), so a mirror only saves network —
a stale or absent mirror is never wrong, and a damaged one is covered by
the container's retry-direct fallback.

This module is the cache's **single writer** and its single host-side
**reader**. Writing: the CLI forks it detached at launch (``python -m
lmer_cli.clone_cache``, repo URLs on stdin) so a session never waits on
mirror maintenance; the same entrypoint serves external schedulers
(systemd timer, k8s CronJob) that want to warm a cache on a cadence.
Reading: :func:`read_cached_repo_file` lets the host see a file at a
mirror's HEAD without a checkout (e.g. a work repo's declared sources for
``--show-env``) — reads are **local-only** (no network, no fetch, no lock,
no writes to the cache) and fail-soft: any miss or trouble yields None,
never an exception.

Credential rules (the reason this code lives host-side at all):

- The tokenized URL is **never written to disk**: mirrors are created with
  ``git init --bare`` storing only the scrubbed URL as origin, and every
  fetch passes the URL explicitly.
- The token is **never on any argv**: git children fetch the *scrubbed*
  URL, with credentials injected via ephemeral ``GIT_CONFIG_*`` process
  env (an ``http.<url>.extraHeader`` Authorization header).
- **Exactly one Authorization header goes out, and it is ours**: a credentialed
  fetch cancels any header the operator's git config or the inherited
  ``GIT_CONFIG_*`` pairs would add before appending its own, and resets
  ``credential.helper`` so nothing can substitute other credentials or block a
  detached updater on an interactive unlock.
- A credential lmer injected may still be one the remote rejects (a
  ``GITLAB_TOKEN`` fallback used to reach github.com URLs; since #161 the
  generic token is scoped to its issuing host, so what remains is an
  explicitly mis-scoped or per-host token the target refuses). A failed
  attempt that carried credentials is therefore retried **once without
  lmer's own injection**, so a public repo mirrors regardless (#157). The retry
  undoes what this process attached and nothing else: it runs the same empty
  env a URL lmer could not credential gets, so the operator's own git config
  — headers and credential helper included — is what remains, exactly as
  before.
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
SHOW_TIMEOUT_S = 10  # read of one blob from a local mirror


def mirror_path(cache_root: Path, repo_url: str) -> "Path | None":
    """The container's URL→``<host>/<project>.git`` mapping, reused verbatim."""
    return _mirror_path(Path(cache_root), repo_url)


# Why read_cached_repo_file_status came back without content. The one
# distinction a caller actually needs to phrase a message: FILE_ABSENT means
# the mirror is fine and the repo simply does not carry that file (the normal
# state for a work repo that declares nothing), while the other reasons mean
# the cache has no usable answer yet.
CACHE_HIT = "hit"
CACHE_NO_MIRROR = "no-mirror"  # no cache dir, no mapping, or no mirror/HEAD
CACHE_NO_HEAD = "no-head"  # mirror present but HEAD resolves to no commit
CACHE_FILE_ABSENT = "absent"  # HEAD is a commit; the file is not in it
CACHE_ERROR = "error"  # git missing, timeout, undecodable blob, …


def read_cached_repo_file_status(
    repo_url: str, path: str = "sources.yaml"
) -> "tuple[str | None, str]":
    """:func:`read_cached_repo_file`, with the reason for a miss preserved.

    Returns ``(content, reason)`` where *reason* is one of the ``CACHE_*``
    constants above and *content* is non-None only for ``CACHE_HIT``. Same
    contract as the plain reader in every other respect — purely local,
    fail-soft, never raises — it just does not throw the cause away, so a
    caller can tell "this repo declares nothing" apart from "the cache is
    cold". Resolving HEAD before the read is what makes that distinction
    trustworthy: with a commit at HEAD, a failing ``git show`` means the
    file is not in the tree rather than that there was nothing to show.
    """
    try:
        cache_root = resolve_host_clone_cache_dir()
        mirror = mirror_path(cache_root, repo_url)
        if mirror is None or not (mirror / "HEAD").exists():
            return None, CACHE_NO_MIRROR
        head = subprocess.run(
            ["git", "-C", str(mirror), "rev-parse", "--verify", "--quiet",
             "HEAD^{commit}"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=SHOW_TIMEOUT_S,
        )
        if head.returncode != 0:
            return None, CACHE_NO_HEAD
        result = subprocess.run(
            ["git", "-C", str(mirror), "show", f"HEAD:{path}"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=SHOW_TIMEOUT_S,
        )
    except Exception:
        return None, CACHE_ERROR
    if result.returncode != 0:
        return None, CACHE_FILE_ABSENT
    return result.stdout, CACHE_HIT


def read_cached_repo_file(repo_url: str, path: str = "sources.yaml") -> "str | None":
    """Best-effort read of one file at HEAD of *repo_url*'s cached mirror.

    Purely local: no network, no fetch, no flock, no writes to the cache —
    just ``git show HEAD:<path>`` against whatever mirror the updater last
    left behind (which may be stale; a stale answer is still useful and a
    cold cache is a normal miss). Every failure mode returns None — no
    cache dir, no mirror, mirror without HEAD, file absent at HEAD, git
    binary missing, timeout, undecodable blob — so a caller like
    ``--show-env`` can never be broken by cache state. The credentialed
    URL is never logged and never part of the return value (the mapping
    scrubs userinfo, and this function emits nothing).

    Deliberately collapses every one of those causes into None; a caller
    that must distinguish them (to say "no sources.yaml" rather than "not
    cached") uses :func:`read_cached_repo_file_status` instead.
    """
    return read_cached_repo_file_status(repo_url, path)[0]


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


def _inherited_git_config_count() -> int:
    """How many ``GIT_CONFIG_KEY_n`` entries the environment already carries.

    The updater's own entries are appended *after* these (see
    :func:`_split_credentials`): ``_git_env`` merges over ``os.environ``, so
    hardcoding ``GIT_CONFIG_COUNT=2`` would silently drop a caller's numbered
    git config (CI, a loaded ``.env``) by overwriting indices 0 and 1. A
    missing or unparseable count means "nothing inherited" — same reading git
    itself would apply to an invalid value.
    """
    raw = (os.environ.get("GIT_CONFIG_COUNT") or "").strip()
    try:
        count = int(raw)
    except ValueError:
        return 0
    return count if count > 0 else 0


def _config_env(entries: "list[tuple[str, str]]") -> "dict[str, str]":
    """Numbered ``GIT_CONFIG_*`` env carrying *entries*, in order.

    The entries land at the next free indices so inherited numbered git config
    survives (see :func:`_inherited_git_config_count`), and git applies them
    *after* what it inherited — which is what lets an entry here override or
    cancel an inherited one.
    """
    base = _inherited_git_config_count()
    env = {"GIT_CONFIG_COUNT": str(base + len(entries))}
    for offset, (key, value) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{base + offset}"] = key
        env[f"GIT_CONFIG_VALUE_{base + offset}"] = value
    return env


def _credential_sources_off(scrubbed_url: str) -> "list[tuple[str, str]]":
    """Config entries clearing the way for **our own** credential on *url*.

    One caller only — the credentialed branch of :func:`_split_credentials`,
    which appends its ``Authorization`` header after these. The anonymous retry
    deliberately does *not* use this list; see :func:`_attempt_with_fallback`.

    - ``credential.helper=`` — no helper may answer a challenge in place of the
      token we were given, and none may block a detached updater holding a
      flock on an interactive unlock.
    - ``http.<url>.extraHeader=`` — an empty value **resets** git's
      multi-valued header list, cancelling an ``Authorization`` header the
      operator configured globally (the corporate-proxy/Artifactory pattern)
      or one arriving in an inherited ``GIT_CONFIG_KEY_n`` pair. Without it git
      sends *both*, ours second: measured against git 2.52.0 by recording what
      git puts on the wire, the bare ``[http]``, matching-URL-prefix and
      inherited-env-pair forms all produced ``['Basic INHERITED', 'Basic
      OURS']``, and the empty reset at the exact URL cancelled all three.

    **Deliberate cost, and it is not free** (review on !178): the reset clears
    git's *whole* header list for the URL, not only ``Authorization`` entries,
    so an operator using ``http.extraHeader`` for a required non-auth header
    (proxy routing, a tenant id) loses it on a URL lmer credentials — measured,
    and a regression against the behaviour before the reset existed. It is
    accepted because git offers no narrower instrument: the reset is
    all-or-nothing, and the header list cannot be read back and re-emitted
    selectively either, since ``git config --get-urlmatch http <url>`` reports
    only the single best-matching value rather than the accumulated list git
    actually sends (measured on the same git). The alternative is two
    ``Authorization`` headers on the wire with the remote choosing between
    them, which is worse for every operator rather than for a narrow one.
    Tracked as issue #179 so the loss stays visible and revisitable rather than
    folded into #157.

    The claim is bounded, deliberately: this covers the credential sources
    reachable through process env, which is why the caller says "no auth header
    and no credential helper" rather than "no credentials".
    """
    return [
        ("credential.helper", ""),
        (f"http.{scrubbed_url}.extraHeader", ""),
    ]


def _carries_credentials(cred_env: "dict[str, str]") -> bool:
    """Whether *cred_env* attaches an Authorization header to git's requests.

    A fact the updater owns — it built the dict — which is why the retry in
    :func:`_attempt_with_fallback` keys on this and never on git's error text:
    "could not read Username" covers a wrong credential, a private repo and a
    nonexistent path alike, and git never promised that wording.

    An ``extraHeader`` entry with an **empty** value cancels headers rather
    than sending one (:func:`_credential_sources_off`), so the paired *value*
    is what decides, not the key name. Today's credentialed env carries a real
    entry alongside its reset and would read as credentialed either way; the
    value check keeps the predicate true to its name for any env that carries
    resets alone.
    """
    prefix = "GIT_CONFIG_KEY_"
    for key, name in cred_env.items():
        if not key.startswith(prefix) or not name.endswith(".extraHeader"):
            continue
        if cred_env.get(f"GIT_CONFIG_VALUE_{key[len(prefix):]}", ""):
            return True
    return False


def _split_credentials(repo_url: str) -> "tuple[str, dict[str, str]]":
    """Split an http(s) URL into (scrubbed URL, ephemeral git-config env).

    The env entries carry an ``http.<url>.extraHeader`` Authorization header
    so the token reaches git without ever appearing on an argv (host ``ps``
    shows every command line for the full duration of an initial mirror
    build), preceded by :func:`_credential_sources_off` so exactly one
    ``Authorization`` header goes out — ours. Without that reset an inherited
    global header is sent *alongside* it (measured: git 2.52.0 puts both on
    the wire).

    URLs without userinfo — and URLs with no scheme or an unparseable one —
    pass through with an **empty** env: the operator's own git config, credential
    helper included, is then what authenticates, exactly as it did before this
    function existed.

    Note the userinfo branch is taken for *any* scheme, so an
    ``ssh://user@host/…`` URL loses its login and gets a meaningless
    ``http.ssh://….extraHeader``. That mangling predates this function and is
    tracked separately — untangling it needs a store-URL/fetch-URL split, since
    simply passing such URLs through puts their userinfo in the mirror's
    ``remote.origin.url`` on disk, which the module's credential rules forbid.
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
    entries = _credential_sources_off(scrubbed) + [
        (f"http.{scrubbed}.extraHeader", f"Authorization: Basic {token}"),
    ]
    return scrubbed, _config_env(entries)


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


def _fetch_existing(mirror: Path, scrubbed_url: str, cred_env: "dict[str, str]") -> None:
    """Refresh an existing mirror from *scrubbed_url*."""
    _run_git(
        ["-C", str(mirror), "fetch", "--quiet", "--prune", scrubbed_url,
         "+refs/*:refs/*"],
        timeout=FETCH_TIMEOUT_S,
        cred_env=cred_env,
    )


def _attempt_with_fallback(
    operation, mirror: Path, cred_env: "dict[str, str]"
) -> None:
    """Run *operation(cred_env)*, retrying once anonymously if it failed (#157).

    A credential lmer injected is not necessarily one the remote accepts:
    a host with ``GITLAB_TOKEN`` set and no GitHub token *used to* get that
    PAT injected into github.com URLs by the generic token fallback, and
    GitHub challenges it even for a **public** repo — which is how a mirror
    of a public repo failed with "could not read Username" while an anonymous
    fetch of the same URL would have succeeded (issue #157, seen in the #150
    spike). Since #161 the generic token is scoped to its issuing host, so
    that exact path is closed and the retry now mainly covers explicitly
    mis-scoped (``LMER_GITLAB_TOKEN_HOST``) or per-host tokens the target
    rejects — the failure mode is unchanged, only its sources are fewer.

    So a failed attempt that *carried* credentials is retried once **with
    lmer's own injection dropped and nothing else changed** — an empty env,
    byte-identical to what a URL lmer could not credential already gets. The
    decision reads only what this process knows it sent
    (:func:`_carries_credentials`) — never git's error text, which cannot tell a
    rejected credential from a private or nonexistent repo.

    The retry deliberately does *not* apply :func:`_credential_sources_off`
    (review on !178). What #157 needs undone is the credential lmer chose, not
    the operator's configuration: cancelling their headers and helper too made
    the retry *stricter* than the tokenless path, discarding — measured — an
    ``Authorization`` header of their own that would have worked, and any
    non-auth header the request needs. Dropping to ``{}`` keeps the retry
    symmetric with the rule stated for tokenless URLs: a request lmer cannot
    credential is left entirely to the operator's git config. The cost is the
    converse case — an operator whose own global header is the *wrong* one for
    this host keeps sending it into the retry, so that mirror still fails — but
    that was true before this retry existed and is not a regression.

    **Only a non-zero git exit earns a retry.** ``RuntimeError`` is what
    :func:`_run_git` raises for that, so a ``subprocess.TimeoutExpired`` or a
    local ``OSError`` deliberately propagates untouched: retrying a create that
    hit :data:`CREATE_TIMEOUT_S` would start a second *from-scratch* transfer
    (``_create_mirror`` removes its ``.tmp``, so nothing resumes), doubling a
    window in which this mirror's flock is held — and ``update_mirrors`` skips a
    held lock rather than queueing, so every launch in that window silently gets
    no warming for the repo. Certain to fail again anyway when the repo genuinely
    needs credentials (review on !178).

    A URL with no credentials makes exactly one attempt — there is nothing to
    drop — and a working token still succeeds on the first, so
    token-authenticated flows are untouched. If the retry fails too, the
    *credentialed* failure is what propagates, since an anonymous failure on a
    repo that really does need credentials says nothing useful; the retry's own
    error is logged before that, because ``update_mirrors`` stringifies only the
    exception it catches and the cause would otherwise be lost.
    """
    try:
        operation(cred_env)
        return
    except RuntimeError as e:
        if not _carries_credentials(cred_env):
            raise
        credentialed_failure = e
    _log(f"{mirror.name}: update with credentials failed ({credentialed_failure}); "
         f"retrying without the credential lmer injected")
    try:
        operation({})
    except Exception as anon_failure:
        _log(f"{mirror.name}: retry without the injected credential also "
             f"failed: {anon_failure}")
        raise credentialed_failure


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
        _attempt_with_fallback(
            lambda env: _fetch_existing(mirror, scrubbed_url, env),
            mirror, cred_env,
        )
    else:
        if mirror.is_dir():
            # HEAD-less non-empty dir (partial manual deletion, external
            # damage): _create_mirror's tmp.rename would fail with ENOTEMPTY
            # after paying the full transfer, on every run — clear it first.
            # Safe: we hold this mirror's flock.
            _log(f"{mirror.name}: removing damaged mirror (no HEAD)")
            shutil.rmtree(mirror)
        _attempt_with_fallback(
            lambda env: _create_mirror(mirror, scrubbed_url, env),
            mirror, cred_env,
        )
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
    cache root resolved exactly like the launch's mounts (LMER_CLONE_CACHE_DIR or
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
