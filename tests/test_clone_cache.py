"""Tests for the container side of the persistent git clone cache (issue #112).

Architecture under test: the *host* CLI maintains the bare mirrors
(``lmer_cli.clone_cache`` — covered in test_clone_cache_host.py) and
bind-mounts **read-only** the mirrors the launch itself needs, each at its
usual path under ``/clone-cache`` (issue #135), advertised via
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
    build_clone_cache_mounts,
    plan_clone_cache_mirror_mounts,
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

    def test_relative_path_refused(self, monkeypatch, tmp_path, capsys):
        """Review on !154: a relative value splits the feature in two — the
        mount string `cache:/clone-cache:ro` reads as a *named volume* to
        Docker/Podman while the host updater populates a real ./cache, so the
        container never sees the mirrors."""
        monkeypatch.setenv("LMER_CLONE_CACHE_DIR", "cache")
        monkeypatch.setattr(
            "lmer_cli.mounts.Path.home", classmethod(lambda cls: tmp_path)
        )
        resolved = resolve_host_clone_cache_dir()
        assert resolved == tmp_path / ".lmer" / "clone-cache"
        err = capsys.readouterr().err
        assert "absolute path" in err
        assert "'cache'" in err

    @pytest.mark.parametrize("broad", ["/", "~", "~/"])
    def test_broad_root_refused(self, monkeypatch, tmp_path, broad):
        # Host-side guard (the container no longer sees the root at all, #135):
        # the updater scatters `<host>/<group>/<project>.git` mirror trees
        # through this directory, which `~` — let alone `/` — must never be.
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("LMER_CLONE_CACHE_DIR", broad)
        assert resolve_host_clone_cache_dir() == home / ".lmer" / "clone-cache"

    def test_broad_root_warns(self, monkeypatch, tmp_path, capsys):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("LMER_CLONE_CACHE_DIR", "~")
        resolve_host_clone_cache_dir()
        assert "too broad to bind-mount" in capsys.readouterr().err

    def test_absolute_subdir_of_home_still_allowed(self, monkeypatch, tmp_path):
        # Only the home directory *itself* is refused — a dedicated subdir is
        # the normal configuration.
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("LMER_CLONE_CACHE_DIR", "~/mirrors")
        assert resolve_host_clone_cache_dir() == home / "mirrors"


class TestPerMirrorMounts:
    """Issue #135: a launch mounts the mirrors it will clone, one bind each —
    never the cache root, which would hand every session the full history of
    every repo the user has ever cached."""

    @staticmethod
    def _mirror(cache_root: Path, url: str) -> Path:
        """Create a mirror-shaped directory for *url* under *cache_root*."""
        mirror = _mirror_path(cache_root, url)
        assert mirror is not None
        mirror.mkdir(parents=True)
        (mirror / "HEAD").write_text("ref: refs/heads/main\n")
        return mirror

    def _build(self, cache_root: Path, urls, runtime: str = "docker"):
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            _is_selinux_enforcing.cache_clear()
            return build_clone_cache_mounts(runtime, cache_root, urls)

    def test_only_the_launch_s_mirrors_are_mounted(self, tmp_path):
        # The point of the issue: an unrelated cached repo — private, from a
        # host this session has no credentials for — must not be visible.
        cache = tmp_path / "clone-cache"
        wanted = self._mirror(cache, "https://git.example.com/grp/proj.git")
        unrelated = self._mirror(cache, "https://other.example.com/secret/repo.git")

        args, pairs = self._build(cache, ["https://git.example.com/grp/proj.git"])

        assert args == [
            "-v",
            f"{wanted}:{CONTAINER_CLONE_CACHE_DIR}/git.example.com/grp/proj.git:ro",
        ]
        assert [str(m) for m, _ in pairs] == [str(wanted)]
        assert str(unrelated) not in " ".join(args)
        assert str(cache) + ":" not in " ".join(args)  # never the root itself

    def test_container_path_matches_container_side_lookup(self, tmp_path):
        # The container resolves the mirror itself via _mirror_path against
        # CONTAINER_CLONE_CACHE_DIR; the bind has to land exactly there or the
        # mount is invisible to the clone.
        cache = tmp_path / "clone-cache"
        url = "https://git.example.com:8443/grp/sub/proj.git"
        self._mirror(cache, url)

        _, pairs = self._build(cache, [url])

        expected = _mirror_path(Path(CONTAINER_CLONE_CACHE_DIR), url)
        assert [container for _, container in pairs] == [str(expected)]

    def test_missing_mirror_produces_no_mount_and_creates_nothing(self, tmp_path):
        # Cold cache: no bind at all. Handing Docker/Podman a missing source
        # would have it create the path as a root-owned directory on the host.
        cache = tmp_path / "clone-cache"
        cache.mkdir()
        url = "https://git.example.com/grp/proj.git"

        args, pairs = self._build(cache, [url])

        assert args == []
        assert pairs == []
        assert list(cache.iterdir()) == []

    def test_directory_without_head_is_not_mounted(self, tmp_path):
        # "Exists" means usable: the container refuses to reference a mirror
        # with no HEAD, so mounting one would only widen exposure for nothing.
        cache = tmp_path / "clone-cache"
        url = "https://git.example.com/grp/proj.git"
        mirror = _mirror_path(cache, url)
        mirror.mkdir(parents=True)

        args, _ = self._build(cache, [url])

        assert args == []

    def test_repeated_urls_produce_one_mount(self, tmp_path):
        # The work repo and the napkin repo are frequently the same URL, and
        # the same repo may arrive tokenized and plain.
        cache = tmp_path / "clone-cache"
        self._mirror(cache, "https://git.example.com/grp/proj.git")
        urls = [
            "https://git.example.com/grp/proj.git",
            "https://git.example.com/grp/proj",
            "https://oauth2:tok@git.example.com/grp/proj.git",
        ]

        args, pairs = self._build(cache, urls)

        assert args.count("-v") == 1
        assert len(pairs) == 1

    def test_empty_and_unmappable_urls_are_skipped(self, tmp_path):
        cache = tmp_path / "clone-cache"
        cache.mkdir()

        args, pairs = self._build(cache, [None, "", "/local/path/repo", "not a url"])

        assert (args, pairs) == ([], [])

    def test_symlinked_mirror_escaping_the_cache_is_refused(self, tmp_path):
        # With per-mirror binds the symlink is followed HOST-side, so honoring
        # one would mount an arbitrary host directory into the container.
        cache = tmp_path / "clone-cache"
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "HEAD").write_text("ref: refs/heads/main\n")
        url = "https://git.example.com/grp/proj.git"
        mirror = _mirror_path(cache, url)
        mirror.parent.mkdir(parents=True)
        mirror.symlink_to(outside, target_is_directory=True)

        args, _ = self._build(cache, [url])

        assert args == []

    def test_symlinked_cache_root_still_mounts_its_mirrors(self, tmp_path):
        # The escape guard compares resolved paths, so a cache root that is
        # itself a symlink (~/.lmer → /data/lmer) keeps working.
        real = tmp_path / "real-cache"
        link = tmp_path / "clone-cache"
        real.mkdir()
        link.symlink_to(real, target_is_directory=True)
        url = "https://git.example.com/grp/proj.git"
        self._mirror(link, url)

        args, pairs = self._build(link, [url])

        assert len(pairs) == 1
        assert args[1].endswith(f"{CONTAINER_CLONE_CACHE_DIR}/git.example.com/grp/proj.git:ro")

    def test_planner_keeps_first_seen_order(self, tmp_path):
        cache = tmp_path / "clone-cache"
        first = "https://git.example.com/grp/one.git"
        second = "https://git.example.com/grp/two.git"
        self._mirror(cache, first)
        self._mirror(cache, second)

        pairs = plan_clone_cache_mirror_mounts(cache, [second, first, second])

        assert [container for _, container in pairs] == [
            f"{CONTAINER_CLONE_CACHE_DIR}/git.example.com/grp/two.git",
            f"{CONTAINER_CLONE_CACHE_DIR}/git.example.com/grp/one.git",
        ]

    def test_mount_args_are_read_only(self, tmp_path):
        # The container only consumes the cache (--reference --dissociate);
        # all maintenance is host-side, so every bind is structurally :ro —
        # no token or corruption can ever originate from a session.
        cache = tmp_path / "clone-cache"
        self._mirror(cache, "https://git.example.com/grp/proj.git")

        args, _ = self._build(cache, ["https://git.example.com/grp/proj.git"])

        assert args[-1].endswith(":ro")

    def test_selinux_label_on_podman(self, tmp_path):
        cache = tmp_path / "clone-cache"
        self._mirror(cache, "https://git.example.com/grp/proj.git")
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=True):
            _is_selinux_enforcing.cache_clear()
            args, _ = build_clone_cache_mounts(
                "podman", cache, ["https://git.example.com/grp/proj.git"]
            )
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
        assert (
            "build_clone_cache_mounts(\n"
            "            runtime, host_clone_cache, clone_cache_urls + secondary_repo_urls\n"
            "        )"
        ) in self.CLI_SRC

    def test_mounts_cover_every_repo_the_launch_clones(self):
        # #135: the mounts are the mirrors for exactly the repos this launch
        # clones — the updater's list plus the secondary MR/PR targets the
        # container clones too, and nothing wider.
        assert (
            "clone_cache_urls = [repo_url, work_repo_url, napkin_repo_url, taskdef_repo_url]"
            in self.CLI_SRC
        )
        assert "_derive_repo_url_from_task_target(t)\n            for t in secondary_targets" in self.CLI_SRC
        assert "_spawn_clone_cache_updater(clone_cache_urls)" in self.CLI_SRC

    def test_cache_root_is_never_mounted_whole(self):
        # The pre-#135 shape (one bind of the cache root) must not come back:
        # it exposed every cached repo to every session.
        assert "build_clone_cache_mount(" not in self.CLI_SRC
        assert f"{{host_clone_cache}}:{{CONTAINER_CLONE_CACHE_DIR}}" not in self.CLI_SRC

    def test_updater_spawn_gated_on_active_mount(self):
        # The background updater runs only when the cache feature is active —
        # LMER_CLONE_CACHE=0 (or an unusable dir) disables both together.
        assert (
            "if clone_cache_container_dir is not None and host_clone_cache is not None:"
            in self.CLI_SRC
        )
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

    def test_host_side_python_path_is_not_caller_controlled(self, monkeypatch, tmp_path):
        """Review on !154: this child runs on the HOST, so `python -m` must not
        pick code up from the launch cwd (a stray `lmer_cli/` package there) or
        from an inherited PYTHONPATH (a cwd `.env` lands in os.environ)."""
        monkeypatch.setenv("PYTHONPATH", "/tmp/attacker")
        monkeypatch.setenv("PYTHONHOME", "/tmp/attacker-home")
        monkeypatch.setenv("PYTHONSTARTUP", "/tmp/attacker-startup.py")
        calls, _ = self._spawn(monkeypatch, ["https://x/y.git"])
        # -P (PYTHONSAFEPATH) keeps the cwd off sys.path
        assert calls["cmd"][1] == "-P"
        assert calls["cmd"][-2:] == ["-m", "lmer_cli.clone_cache"]
        env = calls["kwargs"]["env"]
        # PYTHONPATH is pinned to lmer's own package dir, not the caller's
        import lmer_cli

        assert env["PYTHONPATH"] == str(Path(lmer_cli.__file__).resolve().parent.parent)
        assert env["PYTHONSAFEPATH"] == "1"
        assert "PYTHONHOME" not in env
        assert "PYTHONSTARTUP" not in env
        # cwd is a trusted directory, not whatever the caller was sitting in
        assert calls["kwargs"]["cwd"] == str(Path.home())

    def test_updater_env_keeps_git_ssh_command(self, monkeypatch):
        # ssh-URL mirrors can need the caller's key selection; .env is trusted
        # standing configuration everywhere else in lmer, so this is inherited
        # on purpose (deliberate scope of the !154 hardening).
        monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i ~/.ssh/mirror_key")
        calls, _ = self._spawn(monkeypatch, ["git@git.example.com:a/b.git"])
        assert calls["kwargs"]["env"]["GIT_SSH_COMMAND"] == "ssh -i ~/.ssh/mirror_key"

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
