"""Tests for the container side of the persistent git clone cache (issue #112).

Architecture under test: the *host* CLI maintains the bare mirrors
(``lmer_cli.clone_cache`` — covered in test_clone_cache_host.py) and
bind-mounts the cache **read-only** at ``/clone-cache``, advertised via
``LMER_CLONE_CACHE_PATH``. The container is a pure consumer: when a mirror
exists it clones with ``--reference <mirror> --dissociate`` (origin stays
the real remote, and the workspace never depends on the cache afterwards);
when it doesn't — or anything at all goes wrong — it clones directly. The
container never creates, refreshes, or writes anything in the cache.

The mechanics tests use real tmp git repos (the suite's pattern, cf.
test_work_repo_git_ops.py). Local-path clone URLs are deliberately *not*
cacheable (``_mirror_path`` returns None), so those tests pin the URL→path
mapping via monkeypatch and exercise everything else for real. The
URL→path mapping itself is additionally cross-checked against the host
module's reuse of it in test_clone_cache_host.py::TestMirrorPathReuse.
"""

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from lmer_cli.container import clone_and_exec
from lmer_cli.container.clone_and_exec import (
    _cache_reference_flags,
    _clone_with_cache,
    _mirror_path,
    ensure_clone,
)
from lmer_cli.mounts import (
    CONTAINER_CLONE_CACHE_DIR,
    build_clone_cache_mount,
    resolve_host_clone_cache_dir,
)
from lmer_cli.runtime import _is_selinux_enforcing

REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture(autouse=True)
def _no_ambient_cache(monkeypatch, tmp_path):
    # The suite may itself run inside an lmer container where the host CLI
    # set LMER_CLONE_CACHE_PATH; each test opts in with its own tmp cache.
    monkeypatch.delenv("LMER_CLONE_CACHE_PATH", raising=False)
    monkeypatch.delenv("LMER_CLONE_CACHE_DIR", raising=False)
    # Hermeticity guarantee: no test in this module may write the real
    # ~/.lmer — Path.home()/expanduser follow $HOME on POSIX, so anything
    # reaching resolve_host_clone_cache_dir's default lands in tmp_path.
    # (A stray ~/.lmer flips hooks/start.py's taskdef search chain, #80.)
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))


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
    """An enabled clone cache with a pinned URL→mirror mapping.

    Local-path upstreams are not cacheable through _mirror_path by design
    (tested separately below), so the mapping is pinned to exercise the
    reference-clone mechanics with real git.
    """
    cache_root = tmp_path / "clone-cache"
    cache_root.mkdir()
    mirror = cache_root / "example.com" / "org" / "repo.git"
    monkeypatch.setenv("LMER_CLONE_CACHE_PATH", str(cache_root))
    monkeypatch.setattr(
        clone_and_exec, "_mirror_path", lambda root, url: mirror
    )
    return cache_root, mirror


def _build_mirror(upstream: Path, mirror: Path) -> None:
    """Stand-in for the host-side updater: a real bare mirror of *upstream*."""
    mirror.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--quiet", "--mirror", str(upstream), str(mirror)],
        check=True,
        capture_output=True,
    )


def _chmod_tree(root: Path, mode_bit: int, add: bool) -> None:
    for p in [root, *root.rglob("*")]:
        current = p.stat().st_mode
        p.chmod(current | mode_bit if add else current & ~mode_bit)


@pytest.fixture
def read_only(request):
    """Make a directory tree read-only for the test, restored afterwards
    (simulates the :ro bind mount the container actually sees)."""
    import stat

    trees = []

    def apply(root: Path):
        _chmod_tree(root, stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH, add=False)
        trees.append(root)

    yield apply
    for root in trees:
        _chmod_tree(root, stat.S_IWUSR, add=True)


class TestMirrorPath:
    """URL → <cache_root>/<host>/<project>.git mapping."""

    ROOT = Path("/clone-cache")

    def test_https_url(self):
        assert _mirror_path(self.ROOT, "https://gitlab.example.com/group/proj.git") == \
            self.ROOT / "gitlab.example.com" / "group" / "proj.git"

    def test_https_url_without_dot_git(self):
        assert _mirror_path(self.ROOT, "https://github.com/owner/repo") == \
            self.ROOT / "github.com" / "owner" / "repo.git"

    def test_credentials_never_reach_the_path(self):
        mirror = _mirror_path(
            self.ROOT, "https://oauth2:sekrit@gitlab.example.com/group/proj.git"
        )
        assert mirror == self.ROOT / "gitlab.example.com" / "group" / "proj.git"
        assert "sekrit" not in str(mirror)

    def test_scp_like_ssh_url(self):
        assert _mirror_path(self.ROOT, "git@gitlab.example.com:group/sub/proj.git") == \
            self.ROOT / "gitlab.example.com" / "group" / "sub" / "proj.git"

    def test_host_is_lowercased(self):
        assert _mirror_path(self.ROOT, "https://GitHub.com/Owner/Repo") == \
            self.ROOT / "github.com" / "Owner" / "Repo.git"

    def test_local_path_is_not_cacheable(self):
        assert _mirror_path(self.ROOT, "/host-repo") is None
        assert _mirror_path(self.ROOT, "file:///host-repo") is None

    def test_traversal_components_rejected(self):
        assert _mirror_path(self.ROOT, "https://h.example/../../evil") is None
        assert _mirror_path(self.ROOT, "git@h.example:..") is None


class TestReadOnlyConsumer:
    """The container is a pure cache consumer: flags iff a mirror exists,
    reference clones work off a read-only mount, and a stale mirror is
    never wrong. Mirror creation/refresh is host-side (see
    test_clone_cache_host.py)."""

    def test_no_mirror_means_no_flags(self, cache, upstream):
        cache_root, mirror = cache
        assert _cache_reference_flags(str(upstream)) == []
        # ...and nothing was created: the consumer never writes the cache.
        assert not mirror.exists()
        assert list(cache_root.iterdir()) == []

    def test_warm_mirror_yields_reference_flags(self, cache, upstream):
        cache_root, mirror = cache
        _build_mirror(upstream, mirror)
        assert _cache_reference_flags(str(upstream)) == [
            "--reference", str(mirror), "--dissociate",
        ]

    def test_reference_clone_from_read_only_cache(
        self, cache, upstream, tmp_path, read_only
    ):
        # The real container sees the cache through a :ro bind mount — the
        # whole clone path must work without a single write to it.
        cache_root, mirror = cache
        _build_mirror(upstream, mirror)
        read_only(cache_root)

        dest = tmp_path / "ws"
        _clone_with_cache(str(upstream), dest)

        # Working clone is intact and points at the REAL remote, not the mirror.
        assert (dest / "README.md").read_text() == "hello\n"
        assert _git("remote", "get-url", "origin", cwd=dest) == str(upstream)
        # --dissociate: the clone must not depend on the mirror afterwards.
        assert not (dest / ".git" / "objects" / "info" / "alternates").exists()

    def test_stale_mirror_is_never_wrong(self, cache, upstream, tmp_path, read_only):
        # New upstream commits missing from the mirror still arrive: the
        # clone talks to the real origin and borrows only what the mirror
        # has. Staleness costs bytes, never correctness.
        cache_root, mirror = cache
        _build_mirror(upstream, mirror)
        stale_sha = _git("rev-parse", "HEAD", cwd=upstream)
        (upstream / "new.txt").write_text("more\n")
        _git("add", ".", cwd=upstream)
        _git("commit", "-m", "second", cwd=upstream)
        new_sha = _git("rev-parse", "HEAD", cwd=upstream)
        read_only(cache_root)

        dest = tmp_path / "ws"
        _clone_with_cache(str(upstream), dest)

        assert _git("rev-parse", "HEAD", cwd=dest) == new_sha
        # The mirror itself was untouched (still at the stale head).
        assert _git("rev-parse", "refs/heads/main", cwd=mirror) == stale_sha

    def test_ensure_clone_uses_the_cache(self, cache, upstream, tmp_path):
        # The work repo (and the workspace) clone through ensure_clone; both
        # must share the cache path.
        cache_root, mirror = cache
        _build_mirror(upstream, mirror)
        ws = tmp_path / "work"
        ensure_clone(ws, str(upstream), None, None)
        assert (ws / "README.md").exists()

    def test_disabled_when_env_unset(self, monkeypatch, upstream, tmp_path):
        monkeypatch.delenv("LMER_CLONE_CACHE_PATH", raising=False)
        assert _cache_reference_flags(str(upstream)) == []
        # Empty value counts as disabled too.
        monkeypatch.setenv("LMER_CLONE_CACHE_PATH", "")
        assert _cache_reference_flags(str(upstream)) == []

        # The direct clone still works, cache-free.
        dest = tmp_path / "ws"
        _clone_with_cache(str(upstream), dest)
        assert (dest / "README.md").exists()

    def test_uncacheable_url_bypasses(self, tmp_path, monkeypatch):
        # A local-path URL maps to no mirror: no flags, nothing written.
        cache_root = tmp_path / "clone-cache"
        cache_root.mkdir()
        monkeypatch.setenv("LMER_CLONE_CACHE_PATH", str(cache_root))
        assert _cache_reference_flags("/host-repo") == []
        assert list(cache_root.iterdir()) == []


class TestFailSoft:
    """Any cache trouble must degrade to the direct clone, never fail the
    session. With the container never writing the cache, the remaining
    failure surface is the referenced clone itself."""

    def test_corrupt_mirror_retries_directly(self, cache, upstream, tmp_path, capsys):
        cache_root, mirror = cache
        # Looks like a mirror (HEAD present) but is not a git repo: the
        # pure-read flags check accepts it, the referenced clone fails, and
        # the retry-direct fallback saves the session.
        mirror.mkdir(parents=True)
        (mirror / "HEAD").write_text("garbage\n")

        dest = tmp_path / "ws"
        _clone_with_cache(str(upstream), dest)

        assert (dest / "README.md").exists()
        assert "retrying directly" in capsys.readouterr().err

    def test_failed_referenced_clone_retries_directly(
        self, monkeypatch, upstream, tmp_path, capsys
    ):
        # A mirror that vanished between the flags check and the clone
        # (e.g. pruned from under us): the clone retries without the cache.
        missing = tmp_path / "gone.git"
        monkeypatch.setattr(
            clone_and_exec,
            "_cache_reference_flags",
            lambda url: ["--reference", str(missing), "--dissociate"],
        )

        dest = tmp_path / "ws"
        _clone_with_cache(str(upstream), dest)

        assert (dest / "README.md").exists()
        assert "retrying directly" in capsys.readouterr().err

    def test_flags_check_error_degrades_to_direct(
        self, monkeypatch, upstream, tmp_path, capsys
    ):
        # A surprising fs error inside the flags check (the one remaining
        # try/except) warns and clones directly.
        monkeypatch.setenv("LMER_CLONE_CACHE_PATH", "/nonexistent-cache")

        def boom(root, url):
            raise OSError("mount fell out")

        monkeypatch.setattr(clone_and_exec, "_mirror_path", boom)
        dest = tmp_path / "ws"
        _clone_with_cache(str(upstream), dest)
        assert (dest / "README.md").exists()
        assert "clone cache unavailable" in capsys.readouterr().err


class TestHostSideMount:
    """Host-side cache-dir resolution and mount-arg construction."""

    def test_explicit_cache_dir_wins(self, monkeypatch, tmp_path):
        explicit = tmp_path / "custom-cache"
        monkeypatch.setenv("LMER_CLONE_CACHE_DIR", str(explicit))
        assert resolve_host_clone_cache_dir() == explicit

    def test_default_under_lmer_state_dir(self, monkeypatch, tmp_path):
        monkeypatch.delenv("LMER_CLONE_CACHE_DIR", raising=False)
        monkeypatch.setattr(
            "lmer_cli.mounts.Path.home", classmethod(lambda cls: tmp_path)
        )
        assert resolve_host_clone_cache_dir() == tmp_path / ".lmer" / "clone-cache"

    def test_empty_value_falls_back_to_default(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LMER_CLONE_CACHE_DIR", "  ")
        monkeypatch.setattr(
            "lmer_cli.mounts.Path.home", classmethod(lambda cls: tmp_path)
        )
        assert resolve_host_clone_cache_dir() == tmp_path / ".lmer" / "clone-cache"

    def test_expanduser_on_explicit_path(self, monkeypatch):
        monkeypatch.setenv("LMER_CLONE_CACHE_DIR", "~/my-clone-cache")
        assert not str(resolve_host_clone_cache_dir()).startswith("~")

    def test_mount_arg_shape_is_read_only(self):
        # The container only consumes the cache (--reference --dissociate);
        # all maintenance is host-side, so the mount is structurally :ro —
        # no token or corruption can ever originate from a session.
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            _is_selinux_enforcing.cache_clear()
            args = build_clone_cache_mount("docker", Path("/home/user/.lmer/clone-cache"))
        assert args == [
            "-v",
            f"/home/user/.lmer/clone-cache:{CONTAINER_CLONE_CACHE_DIR}:ro",
        ]

    def test_selinux_label_on_podman(self):
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=True):
            _is_selinux_enforcing.cache_clear()
            args = build_clone_cache_mount("podman", Path("/x"))
        assert args[-1].endswith(":ro,z")


class TestHostWiring:
    """Source-level guards on cli.py's env/mount wiring (the run path is
    exercised for real only with a container runtime — same pattern as
    tests/test_run_state_wiring.py)."""

    CLI_SRC = (REPO_ROOT / "src" / "lmer_cli" / "cli.py").read_text()

    def test_container_env_carries_cache_path(self):
        # The clone happens container-side, so the container-side mount path
        # must be in the passthrough dict.
        assert '"LMER_CLONE_CACHE_PATH": clone_cache_container_dir' in self.CLI_SRC

    def test_toggle_is_get_bool_env_default_on(self):
        assert 'get_bool_env("LMER_CLONE_CACHE", default=True)' in self.CLI_SRC

    def test_host_only_vars_stay_host_only(self):
        # The toggle and the host dir are host-side settings; neither may be
        # a passthrough dict key (LMER_CLONE_CACHE_PATH is the only container
        # input). LMER_CLONE_CACHE_DIR is resolved in mounts.py, not cli.py.
        assert '"LMER_CLONE_CACHE":' not in self.CLI_SRC
        assert '"LMER_CLONE_CACHE_DIR"' not in self.CLI_SRC

    def test_mount_built_from_resolver(self):
        assert "resolve_host_clone_cache_dir()" in self.CLI_SRC
        assert "build_clone_cache_mount(runtime, host_clone_cache)" in self.CLI_SRC

    def test_updater_spawn_gated_on_active_mount(self):
        # The background updater runs only when the cache mount is active —
        # LMER_CLONE_CACHE=0 (or an unusable dir) disables both together.
        assert "if clone_cache_container_dir is not None:" in self.CLI_SRC
        assert "_spawn_clone_cache_updater(" in self.CLI_SRC


class TestUpdaterSpawn:
    """The detached-spawn contract for the host-side cache updater: never
    block the session, never put a URL on an argv."""

    def _spawn(self, monkeypatch, urls):
        from lmer_cli import cli as cli_mod

        calls = {}

        class FakeStdin:
            def __init__(self):
                self.data = b""
                self.closed = False

            def write(self, b):
                self.data += b

            def close(self):
                self.closed = True

        class FakeProc:
            def __init__(self):
                self.stdin = FakeStdin()
                self.wait_called = False

            def wait(self, *a, **kw):
                self.wait_called = True

        proc = FakeProc()

        def fake_popen(cmd, **kwargs):
            calls["cmd"] = cmd
            calls["kwargs"] = kwargs
            return proc

        monkeypatch.setattr(cli_mod.subprocess, "Popen", fake_popen)
        cli_mod._spawn_clone_cache_updater(urls)
        return calls, proc

    def test_detached_stdin_fed_no_wait(self, monkeypatch):
        calls, proc = self._spawn(
            monkeypatch,
            ["https://oauth2:tok@git.example.com/a/b.git", "", "https://x/y.git"],
        )
        # module entrypoint, not a script path; no URL on the argv
        assert calls["cmd"][-2:] == ["-m", "lmer_cli.clone_cache"]
        assert all("tok" not in str(c) for c in calls["cmd"])
        # detached: new session, closed fds, piped stdin
        assert calls["kwargs"]["start_new_session"] is True
        assert calls["kwargs"]["close_fds"] is True
        # URLs delivered on stdin (empties dropped), stdin closed, no wait
        assert proc.stdin.data.decode().splitlines() == [
            "https://oauth2:tok@git.example.com/a/b.git",
            "https://x/y.git",
        ]
        assert proc.stdin.closed
        assert proc.wait_called is False

    def test_no_urls_spawns_nothing(self, monkeypatch):
        calls, _ = self._spawn(monkeypatch, ["", None])
        assert "cmd" not in calls

    def test_spawn_failure_is_fail_soft(self, monkeypatch):
        from lmer_cli import cli as cli_mod

        def boom(*a, **kw):
            raise OSError("no forks today")

        monkeypatch.setattr(cli_mod.subprocess, "Popen", boom)
        cli_mod._spawn_clone_cache_updater(["https://x/y.git"])  # must not raise
