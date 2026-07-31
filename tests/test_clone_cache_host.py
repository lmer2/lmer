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
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest

from lmer_cli import clone_cache
from lmer_cli.clone_cache import (
    _git_env,
    _split_credentials,
    mirror_path,
    read_cached_repo_file,
    read_cached_repo_file_status,
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

    def test_ssh_and_bare_urls_pass_through(self):
        for url in ("git@example.com:org/repo.git", "https://example.com/org/repo.git"):
            scrubbed, env = _split_credentials(url)
            assert scrubbed == url
            assert not any(k.startswith("GIT_CONFIG_KEY_") for k in env)

    def test_indices_default_to_zero_when_nothing_inherited(self, monkeypatch):
        monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
        _, env = _split_credentials("https://oauth2:sekrit@example.com/org/repo.git")
        assert env["GIT_CONFIG_COUNT"] == "2"
        assert env["GIT_CONFIG_KEY_0"].endswith(".extraHeader")
        assert env["GIT_CONFIG_KEY_1"] == "credential.helper"

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
        assert env["GIT_CONFIG_COUNT"] == "4"
        assert env["GIT_CONFIG_KEY_2"].endswith(".extraHeader")
        assert env["GIT_CONFIG_KEY_3"] == "credential.helper"
        # the inherited pairs are untouched — they survive the merge in _git_env
        assert "GIT_CONFIG_KEY_0" not in env
        assert "GIT_CONFIG_KEY_1" not in env
        merged = _git_env(env)
        assert merged["GIT_CONFIG_KEY_0"] == "user.name"
        assert merged["GIT_CONFIG_KEY_1"] == "core.sshCommand"
        assert merged["GIT_CONFIG_COUNT"] == "4"

    @pytest.mark.parametrize("bogus", ["", "  ", "not-a-number", "-3", "0"])
    def test_unparseable_inherited_count_falls_back_to_zero(self, monkeypatch, bogus):
        # Same reading git itself applies to an invalid value: nothing inherited.
        monkeypatch.setenv("GIT_CONFIG_COUNT", bogus)
        _, env = _split_credentials("https://oauth2:sekrit@example.com/org/repo.git")
        assert env["GIT_CONFIG_COUNT"] == "2"
        assert env["GIT_CONFIG_KEY_0"].endswith(".extraHeader")


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


class TestReadCachedRepoFile:
    """read_cached_repo_file: host-side, read-only, fail-soft mirror reads.

    Uses the *real* URL→mirror mapping (unlike the `cache` fixture) so the
    reader is exercised end-to-end: resolve cache root, map URL, git show.
    """

    URL = "https://git.example.com/org/repo.git"
    SOURCES = "sources:\n  - name: demo\n"

    @pytest.fixture
    def mirrored(self, tmp_path, upstream, monkeypatch):
        """A real bare mirror at the real mapping's location for URL."""
        (upstream / "sources.yaml").write_text(self.SOURCES)
        _git("add", ".", cwd=upstream)
        _git("commit", "-m", "declare sources", cwd=upstream)
        cache_root = tmp_path / "reader-cache"
        mirror = mirror_path(cache_root, self.URL)
        mirror.parent.mkdir(parents=True)
        subprocess.run(
            ["git", "clone", "--bare", "--quiet", str(upstream), str(mirror)],
            check=True,
            capture_output=True,
        )
        monkeypatch.setenv("LMER_CLONE_CACHE_DIR", str(cache_root))
        return cache_root, mirror

    def test_returns_file_content_from_real_mirror(self, mirrored):
        assert read_cached_repo_file(self.URL, "sources.yaml") == self.SOURCES

    def test_unmapped_local_url_returns_none(self, mirrored, upstream):
        # a local path never maps to a mirror — even though it is itself a
        # perfectly readable git repo, the reader must not touch it
        assert read_cached_repo_file(str(upstream), "sources.yaml") is None

    def test_absent_mirror_returns_none(self, tmp_path, monkeypatch):
        cache_root = tmp_path / "empty-cache"
        cache_root.mkdir()
        monkeypatch.setenv("LMER_CLONE_CACHE_DIR", str(cache_root))
        assert read_cached_repo_file(self.URL) is None

    def test_absent_cache_root_returns_none(self):
        # HOME is hermetic and LMER_CLONE_CACHE_DIR unset: the default cache
        # root does not exist, and the reader must not create it
        assert read_cached_repo_file(self.URL) is None
        assert not (Path(os.environ["HOME"]) / ".lmer" / "clone-cache").exists()

    def test_headless_mirror_returns_none(self, mirrored):
        _, mirror = mirrored
        (mirror / "HEAD").unlink()
        assert read_cached_repo_file(self.URL) is None

    def test_missing_file_at_head_returns_none(self, mirrored):
        assert read_cached_repo_file(self.URL, "no-such-file.yaml") is None

    def test_missing_git_binary_returns_none(self, mirrored, monkeypatch):
        def boom(*a, **kw):
            raise FileNotFoundError("git")

        monkeypatch.setattr(clone_cache.subprocess, "run", boom)
        assert read_cached_repo_file(self.URL) is None

    def test_tokenized_url_leaks_no_credential(self, mirrored):
        tokenized = "https://oauth2:sekrit@git.example.com/org/repo.git"
        content = read_cached_repo_file(tokenized, "sources.yaml")
        # mapping scrubs userinfo: same mirror as the plain URL, content back
        assert content == self.SOURCES
        assert "sekrit" not in content
        # a pure read logs nothing — and certainly never the token
        log = Path(os.environ["HOME"]) / ".lmer" / "logs" / "clone-cache.log"
        assert not log.exists() or "sekrit" not in log.read_text()

    def test_status_reports_hit_with_content(self, mirrored):
        assert read_cached_repo_file_status(self.URL, "sources.yaml") == (
            self.SOURCES,
            clone_cache.CACHE_HIT,
        )

    def test_status_distinguishes_absent_file_from_cold_cache(
        self, mirrored, tmp_path, monkeypatch
    ):
        """The distinction the None-collapsing reader cannot make: a warm
        mirror of a repo that simply lacks the file is NOT a cache miss."""
        content, reason = read_cached_repo_file_status(self.URL, "no-such-file.yaml")
        assert (content, reason) == (None, clone_cache.CACHE_FILE_ABSENT)

        cold = tmp_path / "cold-cache"
        cold.mkdir()
        monkeypatch.setenv("LMER_CLONE_CACHE_DIR", str(cold))
        assert read_cached_repo_file_status(self.URL) == (
            None,
            clone_cache.CACHE_NO_MIRROR,
        )

    def test_status_reports_headless_mirror_separately(self, mirrored):
        """A mirror whose HEAD names no commit has nothing to show yet — a
        different answer from "the file is not in this repo"."""
        _, mirror = mirrored
        (mirror / "HEAD").write_text("ref: refs/heads/no-such-branch\n")
        assert read_cached_repo_file_status(self.URL) == (
            None,
            clone_cache.CACHE_NO_HEAD,
        )

    def test_status_reports_error_when_git_is_unusable(self, mirrored, monkeypatch):
        def boom(*a, **kw):
            raise FileNotFoundError("git")

        monkeypatch.setattr(clone_cache.subprocess, "run", boom)
        assert read_cached_repo_file_status(self.URL) == (None, clone_cache.CACHE_ERROR)

    def test_read_writes_nothing_to_the_cache(self, mirrored):
        cache_root, _ = mirrored
        before = sorted(str(p.relative_to(cache_root)) for p in cache_root.rglob("*"))
        assert read_cached_repo_file(self.URL, "sources.yaml") == self.SOURCES
        after = sorted(str(p.relative_to(cache_root)) for p in cache_root.rglob("*"))
        assert after == before  # no locks, no stamps, no tmp dirs


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
