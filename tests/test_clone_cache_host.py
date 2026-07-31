"""Tests for the host-side clone-cache updater (issue #112, MR !145 rework).

Architecture under test: the *host* CLI owns all mirror maintenance — a
detached background updater (``lmer_cli.clone_cache``) creates/refreshes one
bare mirror per repo under the cache root, while containers consume the cache
read-only. The token-bearing URL must never be written to disk, never appear
on any process argv (URLs arrive on stdin; credentials travel as ephemeral
``GIT_CONFIG_*`` env), and never leak into the updater log (which lives
*outside* the cache root and is credential-scrubbed).

Same conventions as test_clone_cache.py: real tmp git repos, and a pinned
URL→mirror mapping (local-path URLs are deliberately not cacheable through
``mirror_path``), so the lock/create/fetch mechanics run against real git.
"""

import fcntl
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

from lmer_cli import clone_cache
from lmer_cli.clone_cache import (
    _carries_credentials,
    _config_env,
    _credential_sources_off,
    _git_env,
    _split_credentials,
    mirror_path,
    update_mirrors,
)
from lmer_cli.container.clone_and_exec import _mirror_path


@pytest.fixture(autouse=True)
def _hermetic_home(monkeypatch, tmp_path):
    # The log path and default cache root derive from HOME; no test in this
    # module may touch the real ~/.lmer (cf. test_clone_cache.py, #80).
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.delenv("LMER_CLONE_CACHE_DIR", raising=False)


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


@pytest.fixture
def upstream(tmp_path):
    """A real local git repo standing in for the remote."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test User", cwd=repo)
    (repo / "README.md").write_text("hello\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-m", "init", cwd=repo)
    return repo


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """An updater cache root with a pinned URL→mirror mapping."""
    cache_root = tmp_path / "clone-cache"
    cache_root.mkdir()
    mirror = cache_root / "example.com" / "org" / "repo.git"
    monkeypatch.setattr(clone_cache, "mirror_path", lambda root, url: mirror)
    return cache_root, mirror


def _scan_for(needle: str, root: Path) -> list[Path]:
    """Every file under *root* whose bytes contain *needle*."""
    hits = []
    for p in root.rglob("*"):
        if p.is_file() and needle.encode() in p.read_bytes():
            hits.append(p)
    return hits


class TestMirrorPathReuse:
    """mirror_path must be the container's mapping — same module, no drift."""

    VECTORS = [
        ("https://git.example.com/group/project.git", "git.example.com/group/project.git"),
        ("https://git.example.com/group/project", "git.example.com/group/project.git"),
        ("https://oauth2:tok@git.example.com/group/project.git", "git.example.com/group/project.git"),
        ("git@git.example.com:group/project.git", "git.example.com/group/project.git"),
        ("HTTPS://GIT.Example.COM/group/project.git", "git.example.com/group/project.git"),
    ]

    @pytest.mark.parametrize("url,rel", VECTORS)
    def test_vectors_match_container_mapping(self, tmp_path, url, rel):
        assert mirror_path(tmp_path, url) == tmp_path / rel
        assert mirror_path(tmp_path, url) == _mirror_path(tmp_path, url)

    @pytest.mark.parametrize(
        "url",
        [
            "/local/path/repo.git",
            "https://example.com/../../etc",
            "https://example.com/a/../b",
            "",
        ],
    )
    def test_uncacheable_vectors_match_container(self, tmp_path, url):
        assert mirror_path(tmp_path, url) is None
        assert _mirror_path(tmp_path, url) is None

    def test_distinct_ports_get_distinct_mirrors(self, tmp_path):
        """Review on !154: two servers can share a hostname on different ports.
        Collapsing them into one mirror crossed their stamps and let a clone
        borrow objects from the wrong server."""
        plain = mirror_path(tmp_path, "https://git.example.com/grp/proj")
        ported = mirror_path(tmp_path, "https://git.example.com:8443/grp/proj")
        assert plain == tmp_path / "git.example.com/grp/proj.git"
        assert ported == tmp_path / "git.example.com_8443/grp/proj.git"
        assert plain != ported
        # host and container sides must agree, or the container looks in the
        # wrong place for what the updater built
        assert ported == _mirror_path(tmp_path, "https://git.example.com:8443/grp/proj")

    @pytest.mark.parametrize(
        "url",
        [
            "https://git.example.com:443/grp/proj",
            "http://git.example.com:80/grp/proj",
            "ssh://git@git.example.com:22/grp/proj",
        ],
    )
    def test_default_ports_do_not_split_the_namespace(self, tmp_path, url):
        # An explicit default port names the same server as the bare form, so
        # mirrors built before the port distinction keep being found.
        scheme_host = urlparse(url).hostname
        assert mirror_path(tmp_path, url) == tmp_path / scheme_host / "grp/proj.git"
        assert mirror_path(tmp_path, url) == _mirror_path(tmp_path, url)


class TestSplitCredentials:
    def test_https_userinfo_becomes_auth_header_env(self):
        scrubbed, env = _split_credentials("https://oauth2:sekrit@example.com/org/repo.git")
        assert scrubbed == "https://example.com/org/repo.git"
        assert "sekrit" not in scrubbed
        joined = json.dumps(env)
        assert "sekrit" not in joined  # header value is base64, never raw
        # env carries a git-config Authorization header scoped to the URL
        keys = {v for k, v in env.items() if k.startswith("GIT_CONFIG_KEY_")}
        assert any(k.lower() == f"http.{scrubbed}.extraheader".lower() for k in keys)

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/org/repo.git",  # no userinfo
            "git@example.com:org/repo.git",  # scp-like form, no scheme
            "https://exa mple.com:99999/x",  # unparseable port
        ],
    )
    def test_uncredentialable_urls_pass_through_untouched(self, url):
        """A URL lmer cannot credential gets an EMPTY env: the operator's own git
        config — credential helper included — stays the authenticating path, as
        it was before this function existed.

        Review on !178: an earlier iteration reset `credential.helper` here too,
        which silently removed a working auth path (a `gh auth login`/`store`
        helper is how a tokenless private repo mirrored) in exchange for nothing
        #157 asked for.
        """
        scrubbed, env = _split_credentials(url)
        assert scrubbed == url
        assert env == {}
        assert not _carries_credentials(env)

    def test_credentialed_env_cancels_inherited_headers_before_adding_ours(
        self, monkeypatch
    ):
        """Without the reset git sends the inherited header AND ours — measured
        on the wire with git 2.52.0 (review on !178). Order matters: the empty
        value resets the list, so it has to precede our own entry."""
        monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
        scrubbed, env = _split_credentials("https://oauth2:sekrit@example.com/o/r.git")
        assert env["GIT_CONFIG_COUNT"] == "3"
        assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
        assert env["GIT_CONFIG_VALUE_0"] == ""
        assert env["GIT_CONFIG_KEY_1"] == f"http.{scrubbed}.extraHeader"
        assert env["GIT_CONFIG_VALUE_1"] == ""  # cancels anything inherited
        assert env["GIT_CONFIG_KEY_2"] == f"http.{scrubbed}.extraHeader"
        assert env["GIT_CONFIG_VALUE_2"].startswith("Authorization: Basic ")

    def test_carries_credentials_reads_the_header_value_not_the_key(self):
        _, with_token = _split_credentials("https://oauth2:sekrit@example.com/o/r.git")
        _, without = _split_credentials("https://example.com/o/r.git")
        assert _carries_credentials(with_token)
        assert not _carries_credentials(without)
        # a reset-only env names extraHeader too, but with an empty value: that
        # cancels a header rather than sending one
        resets = _config_env(_credential_sources_off("https://example.com/o/r.git"))
        assert not _carries_credentials(resets)

    def test_indices_default_to_zero_when_nothing_inherited(self, monkeypatch):
        monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
        _, env = _split_credentials("https://oauth2:sekrit@example.com/org/repo.git")
        assert env["GIT_CONFIG_COUNT"] == "3"
        assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
        assert env["GIT_CONFIG_KEY_1"].endswith(".extraHeader")  # cancels inherited
        assert env["GIT_CONFIG_KEY_2"].endswith(".extraHeader")  # ours

    def test_inherited_numbered_config_is_appended_to(self, monkeypatch):
        """Review on !154: _git_env merges over os.environ, so hardcoding
        GIT_CONFIG_COUNT=2 silently dropped a caller's numbered git config (CI,
        a loaded .env) by overwriting indices 0 and 1."""
        monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "CI Bot")
        monkeypatch.setenv("GIT_CONFIG_KEY_1", "core.sshCommand")
        monkeypatch.setenv("GIT_CONFIG_VALUE_1", "ssh -i /keys/ci")
        _, env = _split_credentials("https://oauth2:sekrit@example.com/org/repo.git")
        assert env["GIT_CONFIG_COUNT"] == "5"
        assert env["GIT_CONFIG_KEY_2"] == "credential.helper"
        assert env["GIT_CONFIG_KEY_3"].endswith(".extraHeader")
        assert env["GIT_CONFIG_KEY_4"].endswith(".extraHeader")
        # the inherited pairs are untouched — they survive the merge in _git_env
        assert "GIT_CONFIG_KEY_0" not in env
        assert "GIT_CONFIG_KEY_1" not in env
        merged = _git_env(env)
        assert merged["GIT_CONFIG_KEY_0"] == "user.name"
        assert merged["GIT_CONFIG_KEY_1"] == "core.sshCommand"
        assert merged["GIT_CONFIG_COUNT"] == "5"

    @pytest.mark.parametrize("bogus", ["", "  ", "not-a-number", "-3", "0"])
    def test_unparseable_inherited_count_falls_back_to_zero(self, monkeypatch, bogus):
        # Same reading git itself applies to an invalid value: nothing inherited.
        monkeypatch.setenv("GIT_CONFIG_COUNT", bogus)
        _, env = _split_credentials("https://oauth2:sekrit@example.com/org/repo.git")
        assert env["GIT_CONFIG_COUNT"] == "3"
        assert env["GIT_CONFIG_KEY_0"] == "credential.helper"


class TestHeadersOnTheWire:
    """What git *actually sends*, recorded off a real request (review on !178).

    This has to go to the wire. ``git config --get-urlmatch http <url>`` reports
    only the single best-matching value, while git sends the accumulated
    multi-valued list — so an assertion built on ``--get-urlmatch`` stays green
    whether the empty value resets the list or merely substitutes a more
    specific entry, which is exactly the property that was wrong in iteration 1.
    Measured on git 2.52.0; the class already drives real git elsewhere.
    """

    @pytest.fixture
    def recorder(self):
        """A server that records request headers and 404s, plus its base URL."""
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers)
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"no")

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}", seen
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    @staticmethod
    def _sent(url, cred_env, monkeypatch, seen, operator_config):
        """Headers git put on the wire fetching *url* under *cred_env*."""
        # the operator's own git config, in the hermetic HOME
        gitconfig = Path(os.environ["HOME"]) / ".gitconfig"
        gitconfig.parent.mkdir(parents=True, exist_ok=True)
        gitconfig.write_text(operator_config)
        for var in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"):
            monkeypatch.delenv(var, raising=False)
        seen.clear()
        subprocess.run(
            ["git", "ls-remote", url],
            env=_git_env(cred_env), capture_output=True, text=True, timeout=60,
        )
        assert seen, "git made no request"
        return seen[0]

    @staticmethod
    def _auth(headers):
        return headers.get_all("Authorization") or []

    def test_credentialed_fetch_sends_exactly_one_auth_header_and_it_is_ours(
        self, recorder, monkeypatch
    ):
        """Without the reset git sends the operator's header *and* ours."""
        base, seen = recorder
        monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
        operator = f'[http "{base}/"]\n\textraHeader = Authorization: Basic INHERITED\n'
        scrubbed, cred_env = _split_credentials(f"http://oauth2:OURS@{base[7:]}/o/r.git")

        ours = [
            v for k, v in cred_env.items()
            if k.startswith("GIT_CONFIG_VALUE_") and v.startswith("Authorization: ")
        ]
        assert len(ours) == 1
        ours_value = ours[0].removeprefix("Authorization: ")

        # baseline: the operator's header is genuinely in effect for this URL
        assert self._auth(self._sent(scrubbed, {}, monkeypatch, seen, operator)) == [
            "Basic INHERITED"
        ]
        # and the credentialed env leaves exactly one — ours
        assert self._auth(
            self._sent(scrubbed, cred_env, monkeypatch, seen, operator)
        ) == [ours_value]

    def test_retry_leaves_the_operators_own_config_alone(
        self, recorder, monkeypatch
    ):
        """The retry drops lmer's injection only — it is the tokenless env.

        Cancelling the operator's headers too made the retry stricter than the
        tokenless path and discarded a credential of theirs that works.
        """
        base, seen = recorder
        monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
        operator = (
            f'[http "{base}/"]\n'
            f'\textraHeader = Authorization: Basic INHERITED\n'
            f'\textraHeader = X-Required: tenant-a\n'
        )
        scrubbed, _ = _split_credentials(f"http://oauth2:OURS@{base[7:]}/o/r.git")

        # {} is what _attempt_with_fallback hands the retry
        headers = self._sent(scrubbed, {}, monkeypatch, seen, operator)
        assert self._auth(headers) == ["Basic INHERITED"]
        assert headers.get_all("X-Required") == ["tenant-a"]

    def test_credentialed_reset_also_drops_non_auth_headers(
        self, recorder, monkeypatch
    ):
        """The accepted cost of the credentialed reset, pinned so it is visible.

        An empty `http.<url>.extraHeader` resets git's whole header list, not
        just Authorization entries, and git offers no narrower instrument. If a
        future git grows one, this test fails and the trade can be revisited.
        """
        base, seen = recorder
        monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
        operator = f'[http "{base}/"]\n\textraHeader = X-Required: tenant-a\n'
        scrubbed, cred_env = _split_credentials(f"http://oauth2:OURS@{base[7:]}/o/r.git")

        # in effect without our env ...
        assert self._sent(scrubbed, {}, monkeypatch, seen, operator).get_all(
            "X-Required"
        ) == ["tenant-a"]
        # ... and cancelled by the credentialed reset
        assert self._sent(
            scrubbed, cred_env, monkeypatch, seen, operator
        ).get_all("X-Required") is None


class TestMirrorCreate:
    def test_first_run_builds_mirror_equivalent(self, cache, upstream):
        cache_root, mirror = cache
        update_mirrors([str(upstream)], cache_root)
        # mirror exists, is bare, HEAD points at the fetched default branch
        assert (mirror / "HEAD").exists()
        assert _git("symbolic-ref", "HEAD", cwd=mirror) == "refs/heads/main"
        assert _git("rev-parse", "refs/heads/main", cwd=mirror) == _git(
            "rev-parse", "main", cwd=upstream
        )
        # clone --mirror equivalence + reader-safety config
        assert _git("config", "remote.origin.mirror", cwd=mirror) == "true"
        assert _git("config", "gc.auto", cwd=mirror) == "0"
        # no leftover tmp; a staleness stamp was written
        assert not mirror.with_name(mirror.name + ".tmp").exists()
        assert mirror.with_name(mirror.name + ".stamp").exists()

    def test_failed_create_leaves_no_tmp_no_token_and_logs_outside_cache(
        self, cache, monkeypatch
    ):
        cache_root, mirror = cache
        # Unreachable host, credentialed URL: create must fail fast, clean up,
        # and never let the token touch disk or argv.
        seen_argv: list[list[str]] = []
        real_run = subprocess.run

        def spy(cmd, *a, **kw):
            if isinstance(cmd, (list, tuple)) and cmd and "git" in str(cmd[0]):
                seen_argv.append([str(c) for c in cmd])
            return real_run(cmd, *a, **kw)

        monkeypatch.setattr(clone_cache.subprocess, "run", spy)
        url = "https://oauth2:sekrit@127.0.0.1:1/org/repo.git"
        update_mirrors([url], cache_root)  # fail-soft: must not raise
        assert not (mirror / "HEAD").exists()
        assert not mirror.with_name(mirror.name + ".tmp").exists()
        assert _scan_for("sekrit", cache_root) == []
        for argv in seen_argv:
            assert all("sekrit" not in arg for arg in argv), argv
        # the failure is logged, scrubbed, outside the cache root
        log = Path(os.environ["HOME"]) / ".lmer" / "logs" / "clone-cache.log"
        assert log.exists()
        content = log.read_text()
        assert "sekrit" not in content
        assert content.strip() != ""

    def test_damaged_headless_mirror_dir_is_cleared_and_rebuilt(self, cache, upstream):
        # A non-empty mirror dir without HEAD (partial manual deletion,
        # external damage) must not wedge _create_mirror's tmp.rename with
        # ENOTEMPTY on every run — the updater clears it and rebuilds.
        cache_root, mirror = cache
        mirror.mkdir(parents=True)
        (mirror / "objects").mkdir()
        (mirror / "objects" / "junk").write_text("damaged\n")
        update_mirrors([str(upstream)], cache_root)
        assert (mirror / "HEAD").exists()
        assert not (mirror / "objects" / "junk").exists()
        assert _git("rev-parse", "refs/heads/main", cwd=mirror) == _git(
            "rev-parse", "main", cwd=upstream
        )

    def test_stale_tmp_swept_at_start(self, cache):
        cache_root, mirror = cache
        # a killed !145-era create left a tokenized tmp behind
        stale = cache_root / "example.com" / "org" / "old.git.tmp"
        stale.mkdir(parents=True)
        (stale / "config").write_text("url = https://oauth2:sekrit@x/y.git\n")
        update_mirrors([], cache_root)
        assert not stale.exists()


class TestMirrorFetch:
    def _age_stamp(self, mirror):
        stamp = mirror.with_name(mirror.name + ".stamp")
        old = time.time() - 3600
        os.utime(stamp, (old, old))

    def test_second_run_fetches_new_commits(self, cache, upstream):
        cache_root, mirror = cache
        update_mirrors([str(upstream)], cache_root)
        (upstream / "new.txt").write_text("more\n")
        _git("add", ".", cwd=upstream)
        _git("commit", "-m", "second", cwd=upstream)
        self._age_stamp(mirror)
        update_mirrors([str(upstream)], cache_root)
        assert _git("rev-parse", "refs/heads/main", cwd=mirror) == _git(
            "rev-parse", "main", cwd=upstream
        )

    def test_fresh_stamp_skips_fetch(self, cache, upstream):
        cache_root, mirror = cache
        update_mirrors([str(upstream)], cache_root)
        before = _git("rev-parse", "refs/heads/main", cwd=mirror)
        (upstream / "new.txt").write_text("more\n")
        _git("add", ".", cwd=upstream)
        _git("commit", "-m", "second", cwd=upstream)
        update_mirrors([str(upstream)], cache_root)  # stamp is fresh
        assert _git("rev-parse", "refs/heads/main", cwd=mirror) == before

    def test_stale_lock_droppings_cleaned_before_fetch(self, cache, upstream):
        cache_root, mirror = cache
        update_mirrors([str(upstream)], cache_root)
        dropping = mirror / "packed-refs.lock"
        dropping.write_text("")
        old = time.time() - 7200
        os.utime(dropping, (old, old))
        self._age_stamp(mirror)
        update_mirrors([str(upstream)], cache_root)
        assert not dropping.exists()

    def test_recent_lock_dropping_kept(self, cache, upstream):
        cache_root, mirror = cache
        update_mirrors([str(upstream)], cache_root)
        dropping = mirror / "packed-refs.lock"
        dropping.write_text("")  # fresh: another process may hold it
        self._age_stamp(mirror)
        update_mirrors([str(upstream)], cache_root)
        assert dropping.exists()


class TestAnonymousFallback:
    """#157: a credential lmer injected is not necessarily one the remote
    accepts. A host with GITLAB_TOKEN set and no GitHub token gets that PAT
    injected into github.com URLs, and GitHub challenges it even for a *public*
    repo — so the mirror of a public repo failed with "could not read Username"
    where an anonymous fetch of the same URL succeeds. A failed attempt that
    carried credentials is retried once without them.

    The retry keys on what the updater knows it sent, never on git's error
    text: that text ("could not read Username") is identical for a rejected
    credential, a private repo and a nonexistent path.
    """

    def _credentialed(self, upstream) -> str:
        """A credential-bearing URL whose scrubbed form is the local upstream.

        ``file://oauth2:sekrit@/path`` scrubs to ``file:///path``, so the
        anonymous retry reaches real git while the first attempt looks exactly
        like a tokenized https URL to the updater.
        """
        return f"file://oauth2:sekrit@{upstream}"

    def _start_rejecting(self, monkeypatch) -> list:
        """Make every git call that carries an Authorization header fail the way
        GitHub does, and let credential-free calls through to real git."""
        real = clone_cache._run_git
        seen = []

        def fake(args, timeout, cred_env):
            seen.append(dict(cred_env))
            if _carries_credentials(cred_env):
                raise RuntimeError(
                    "git fetch failed (exit 128): fatal: could not read Username "
                    "for 'https://github.com': terminal prompts disabled"
                )
            return real(args, timeout=timeout, cred_env=cred_env)

        monkeypatch.setattr(clone_cache, "_run_git", fake)
        return seen

    @pytest.fixture
    def reject_credentialed_fetches(self, monkeypatch):
        return self._start_rejecting(monkeypatch)

    @pytest.fixture
    def count_creates(self, monkeypatch):
        """Count _create_mirror attempts and the env each one ran with."""
        attempts = []
        real = clone_cache._create_mirror

        def counting(mirror, scrubbed_url, cred_env):
            attempts.append(dict(cred_env))
            return real(mirror, scrubbed_url, cred_env)

        monkeypatch.setattr(clone_cache, "_create_mirror", counting)
        return attempts

    def _log_text(self) -> str:
        log = Path.home() / ".lmer" / "logs" / "clone-cache.log"
        return log.read_text() if log.exists() else ""

    def test_rejected_credentials_still_build_the_mirror(
        self, cache, upstream, reject_credentialed_fetches
    ):
        cache_root, mirror = cache
        update_mirrors([self._credentialed(upstream)], cache_root)
        # the public repo mirrored anyway — issue #157's acceptance criterion
        assert (mirror / "HEAD").exists()
        assert _git("rev-parse", "refs/heads/main", cwd=mirror) == _git(
            "rev-parse", "main", cwd=upstream
        )
        assert "retrying without the credential lmer injected" in self._log_text()

    def test_retry_runs_with_the_tokenless_env_not_a_stricter_one(
        self, cache, upstream, reject_credentialed_fetches, count_creates
    ):
        """The retry drops lmer's injection and changes nothing else (!178).

        An earlier iteration handed the retry `credential.helper=` plus an empty
        `http.<url>.extraHeader`, cancelling the operator's own header and
        helper too — stricter than the tokenless path, and it discarded a
        credential of theirs that would have worked. `{}` is the tokenless env.
        """
        cache_root, _ = cache
        update_mirrors([self._credentialed(upstream)], cache_root)
        assert len(count_creates) == 2
        assert _carries_credentials(count_creates[0])
        assert count_creates[1] == {}

    def test_rejected_credentials_on_an_existing_mirror_still_fetch(
        self, cache, upstream, monkeypatch
    ):
        cache_root, mirror = cache
        update_mirrors([str(upstream)], cache_root)  # build it credential-free
        (upstream / "new.txt").write_text("more\n")
        _git("add", ".", cwd=upstream)
        _git("commit", "-m", "second", cwd=upstream)
        stamp = mirror.with_name(mirror.name + ".stamp")
        old = time.time() - 3600
        os.utime(stamp, (old, old))
        # only now start rejecting credentialed calls: the refresh path retries too
        self._start_rejecting(monkeypatch)
        update_mirrors([self._credentialed(upstream)], cache_root)
        assert _git("rev-parse", "refs/heads/main", cwd=mirror) == _git(
            "rev-parse", "main", cwd=upstream
        )

    def test_working_credentials_are_not_retried(self, cache, upstream, count_creates):
        """A token the remote accepts must cost exactly one attempt — existing
        token-authenticated flows are unchanged in behavior and timing."""
        cache_root, mirror = cache
        update_mirrors([self._credentialed(upstream)], cache_root)
        assert (mirror / "HEAD").exists()
        assert len(count_creates) == 1
        assert _carries_credentials(count_creates[0])
        assert "retrying without the credential" not in self._log_text()

    def test_no_retry_when_no_credentials_were_sent(self, cache, upstream, monkeypatch):
        """Nothing to drop: a tokenless failure is reported, not retried."""
        attempts = []

        def boom(mirror, scrubbed_url, cred_env):
            attempts.append(dict(cred_env))
            raise RuntimeError("upstream unreachable")

        cache_root, mirror = cache
        monkeypatch.setattr(clone_cache, "_create_mirror", boom)
        update_mirrors([str(upstream)], cache_root)
        assert len(attempts) == 1
        assert "retrying without the credential" not in self._log_text()
        assert "upstream unreachable" in self._log_text()
        assert not (mirror / "HEAD").exists()
        assert list(cache_root.rglob("*.git.tmp")) == []

    def test_both_attempts_failing_reports_the_credentialed_error(
        self, cache, upstream, monkeypatch
    ):
        """The anonymous failure of a repo that really does need credentials
        says nothing useful — the credentialed error is the informative one."""
        attempts = []

        def boom(mirror, scrubbed_url, cred_env):
            attempts.append(dict(cred_env))
            raise RuntimeError(
                "CRED-FAILURE" if _carries_credentials(cred_env) else "ANON-FAILURE"
            )

        cache_root, _ = cache
        monkeypatch.setattr(clone_cache, "_create_mirror", boom)
        update_mirrors([self._credentialed(upstream)], cache_root)
        assert len(attempts) == 2
        assert _carries_credentials(attempts[0])
        assert not _carries_credentials(attempts[1])  # retry drops the header
        log = self._log_text()
        assert "update failed: CRED-FAILURE" in log
        assert "update failed: ANON-FAILURE" not in log

    def test_timeout_is_not_treated_as_a_rejected_credential(
        self, cache, upstream, monkeypatch
    ):
        """Review on !178: retrying a timed-out create starts a second
        from-scratch transfer (the .tmp is gone, nothing resumes), doubling a
        window in which this mirror's flock is held and every launch skips its
        warming — and it is certain to fail again on a repo that genuinely needs
        credentials. A timeout says nothing about credentials."""
        cache_root, mirror = cache
        attempts = []

        def timeout(mirror_path_, scrubbed_url, cred_env):
            attempts.append(dict(cred_env))
            raise subprocess.TimeoutExpired(cmd=["git", "fetch"], timeout=900)

        monkeypatch.setattr(clone_cache, "_create_mirror", timeout)
        update_mirrors([self._credentialed(upstream)], cache_root)  # fail-soft
        assert len(attempts) == 1
        assert "retrying without the credential" not in self._log_text()
        assert not (mirror / "HEAD").exists()

    def test_local_oserror_is_not_treated_as_a_rejected_credential(
        self, cache, upstream, monkeypatch
    ):
        cache_root, _ = cache
        attempts = []

        def enospc(mirror_path_, scrubbed_url, cred_env):
            attempts.append(dict(cred_env))
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(clone_cache, "_create_mirror", enospc)
        update_mirrors([self._credentialed(upstream)], cache_root)
        assert len(attempts) == 1
        assert "No space left on device" in self._log_text()

    def test_retrys_own_failure_is_logged_not_swallowed(
        self, cache, upstream, monkeypatch
    ):
        """update_mirrors stringifies only the exception it catches, so without
        this line a retry that died of ENOSPC is reported as the auth failure
        (review on !178)."""
        cache_root, _ = cache

        def boom(mirror_path_, scrubbed_url, cred_env):
            if _carries_credentials(cred_env):
                raise RuntimeError("CRED-FAILURE")
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(clone_cache, "_create_mirror", boom)
        update_mirrors([self._credentialed(upstream)], cache_root)
        log = self._log_text()
        assert "retry without the injected credential also failed" in log
        assert "No space left on device" in log  # the retry's own cause survives
        assert "update failed: CRED-FAILURE" in log  # and the reported error

    def test_failed_retry_leaves_no_tmp_and_no_mirror(self, cache, tmp_path):
        """Both attempts fail for real (the upstream does not exist), through
        the real create path: no half-built mirror, no leftover build dir."""
        cache_root, mirror = cache
        update_mirrors([f"file://oauth2:sekrit@{tmp_path / 'does-not-exist'}"], cache_root)
        assert not mirror.exists()
        assert list(cache_root.rglob("*.git.tmp")) == []

    def test_retry_carries_no_token_into_the_cache_or_the_log(
        self, cache, upstream, reject_credentialed_fetches
    ):
        cache_root, mirror = cache
        update_mirrors([self._credentialed(upstream)], cache_root)
        assert _scan_for("sekrit", cache_root) == []
        assert "sekrit" not in self._log_text()


class TestFailSoft:
    def test_held_lock_skips_without_corrupting(self, cache, upstream):
        cache_root, mirror = cache
        lock_path = mirror.with_name(mirror.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            update_mirrors([str(upstream)], cache_root)  # must not block/raise
            assert not (mirror / "HEAD").exists()  # skipped, not half-built
        finally:
            os.close(fd)

    def test_missing_git_is_a_quiet_noop(self, cache, upstream, monkeypatch):
        cache_root, mirror = cache
        monkeypatch.setattr(clone_cache.shutil, "which", lambda _: None)
        update_mirrors([str(upstream)], cache_root)
        assert not (mirror / "HEAD").exists()

    def test_uncacheable_url_skipped(self, tmp_path):
        # real mirror_path: a local path maps to no mirror — nothing happens
        cache_root = tmp_path / "clone-cache"
        cache_root.mkdir()
        update_mirrors(["/some/local/path"], cache_root)
        assert list(cache_root.rglob("*.git")) == []

    def test_log_is_size_capped(self, cache):
        cache_root, _ = cache
        log = Path(os.environ["HOME"]) / ".lmer" / "logs" / "clone-cache.log"
        log.parent.mkdir(parents=True)
        log.write_text("x" * (clone_cache.LOG_MAX_BYTES + 1))
        update_mirrors(["https://oauth2:sekrit@127.0.0.1:1/org/repo.git"], cache_root)
        assert log.stat().st_size <= clone_cache.LOG_MAX_BYTES


class TestEntrypoint:
    def test_stdin_urls_drive_update(self, cache, upstream, monkeypatch):
        cache_root, mirror = cache
        monkeypatch.setenv("LMER_CLONE_CACHE_DIR", str(cache_root))
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO(f"{upstream}\n\n"))
        assert clone_cache.main() == 0
        assert (mirror / "HEAD").exists()

    def test_module_is_runnable(self):
        # python -m lmer_cli.clone_cache with empty stdin exits 0 quickly
        result = subprocess.run(
            ["python3", "-m", "lmer_cli.clone_cache"],
            input="",
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent / "src")},
        )
        assert result.returncode == 0
