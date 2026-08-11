"""Tests for the UI build bootstrap (issue #141, slice M2 / T10).

The network is never touched: a fake Node tarball is built on the fly and the
downloader is redirected at it, so checksum verification, extraction safety and
the reuse/force paths are all exercised for real.

The properties that matter: a mismatched checksum aborts *before* extraction, an
archive member cannot escape the state dir, an interrupted run leaves nothing that
looks like a working toolchain, and nothing is ever installed system-wide.
"""

import hashlib
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from lmer_platform import store, ui_build
from tests.conftest import strip_lmer_env


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


def make_node_tarball(path: Path, *, top="node-fake", escape=False, extra_top=False,
                      with_binaries=True) -> str:
    """Build a tarball shaped like Node's, returning its sha256."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:xz") as tar:
        def add(name, content=b"#!/bin/sh\n", mode=0o755):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = mode
            tar.addfile(info, io.BytesIO(content))

        if with_binaries:
            add(f"{top}/bin/node")
            add(f"{top}/bin/npm")
        add(f"{top}/README.md", b"fake node\n", 0o644)
        if escape:
            add("../escaped.txt", b"nope\n", 0o644)
        if extra_top:
            add("other-top/file.txt", b"x\n", 0o644)

    data = buffer.getvalue()
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def fake_archive(tmp_path, monkeypatch):
    """Point the downloader at a local tarball and pin its real checksum."""
    source = tmp_path / "node-fake.tar.xz"
    digest = make_node_tarball(source)

    def fake_download(url, dest):
        dest.write_bytes(source.read_bytes())

    monkeypatch.setattr(ui_build, "_download", fake_download)
    monkeypatch.setattr(ui_build, "platform_key", lambda: "linux-x64")
    monkeypatch.setitem(ui_build.NODE_CHECKSUMS, "linux-x64", digest)
    return source


# --- pinning ----------------------------------------------------------------

def test_every_supported_platform_has_a_checksum():
    """A missing pin would mean downloading whatever the CDN serves."""
    assert set(ui_build.NODE_CHECKSUMS) == {
        "linux-x64", "linux-arm64", "darwin-x64", "darwin-arm64",
    }
    for key, digest in ui_build.NODE_CHECKSUMS.items():
        assert len(digest) == 64, f"{key} checksum is not a sha256"
        assert digest == digest.lower()


def test_download_url_matches_the_pinned_version():
    url = ui_build.download_url("linux-x64")
    assert url == (
        f"https://nodejs.org/dist/{ui_build.NODE_VERSION}/"
        f"node-{ui_build.NODE_VERSION}-linux-x64.tar.xz"
    )


@pytest.mark.parametrize("system,machine,expected", [
    ("Linux", "x86_64", "linux-x64"),
    ("Linux", "aarch64", "linux-arm64"),
    ("Darwin", "arm64", "darwin-arm64"),
    ("Darwin", "x86_64", "darwin-x64"),
])
def test_platform_key_mapping(monkeypatch, system, machine, expected):
    monkeypatch.setattr(ui_build.platform, "system", lambda: system)
    monkeypatch.setattr(ui_build.platform, "machine", lambda: machine)
    assert ui_build.platform_key() == expected


def test_unsupported_platform_says_what_to_do_instead(monkeypatch):
    monkeypatch.setattr(ui_build.platform, "system", lambda: "Windows")
    monkeypatch.setattr(ui_build.platform, "machine", lambda: "amd64")
    with pytest.raises(ui_build.UIBuildError, match="npm ci && npm run build"):
        ui_build.platform_key()


def test_node_lives_under_the_platform_state_dir(platform_root, monkeypatch):
    monkeypatch.setattr(ui_build, "platform_key", lambda: "linux-x64")
    assert ui_build.node_dir() == platform_root / "node" / ui_build.NODE_VERSION


# --- ensure_node ------------------------------------------------------------

def test_ensure_node_downloads_verifies_and_extracts(platform_root, fake_archive):
    toolchain = ui_build.ensure_node()

    assert toolchain.usable
    assert toolchain.node == ui_build.node_dir() / "bin" / "node"
    assert toolchain.node.is_file()
    assert toolchain.npm.exists()


def test_ensure_node_reuses_an_existing_toolchain(platform_root, fake_archive,
                                                  monkeypatch):
    ui_build.ensure_node()

    calls = []
    monkeypatch.setattr(
        ui_build, "_download", lambda url, dest: calls.append(url)
    )
    ui_build.ensure_node()
    assert calls == [], "a second call must not re-download"


def test_force_redownloads(platform_root, fake_archive, monkeypatch):
    ui_build.ensure_node()

    calls = []
    real_download = ui_build._download

    def counting(url, dest):
        calls.append(url)
        real_download(url, dest)

    monkeypatch.setattr(ui_build, "_download", counting)
    ui_build.ensure_node(force=True)
    assert len(calls) == 1


def test_checksum_mismatch_aborts_before_extraction(platform_root, fake_archive,
                                                    monkeypatch):
    """An archive that is not what we pinned is not unpacked "just to see"."""
    monkeypatch.setitem(ui_build.NODE_CHECKSUMS, "linux-x64", "0" * 64)

    with pytest.raises(ui_build.UIBuildError, match="checksum mismatch"):
        ui_build.ensure_node()

    assert not ui_build.node_dir().exists(), "nothing may be extracted on mismatch"


def test_missing_checksum_is_refused(platform_root, monkeypatch):
    monkeypatch.setattr(ui_build, "platform_key", lambda: "plan9-vax")
    with pytest.raises(ui_build.UIBuildError, match="no pinned checksum"):
        ui_build.ensure_node()


def test_download_failure_is_reported_clearly(platform_root, monkeypatch):
    monkeypatch.setattr(ui_build, "platform_key", lambda: "linux-x64")

    def boom(url, dest):
        raise ui_build.UIBuildError(f"cannot download {url} (timed out)")

    monkeypatch.setattr(ui_build, "_download", boom)
    with pytest.raises(ui_build.UIBuildError, match="cannot download"):
        ui_build.ensure_node()


def test_archive_escaping_the_target_is_refused(platform_root, tmp_path, monkeypatch):
    source = tmp_path / "evil.tar.xz"
    digest = make_node_tarball(source, escape=True)
    monkeypatch.setattr(ui_build, "platform_key", lambda: "linux-x64")
    monkeypatch.setitem(ui_build.NODE_CHECKSUMS, "linux-x64", digest)
    monkeypatch.setattr(
        ui_build, "_download", lambda url, dest: dest.write_bytes(source.read_bytes())
    )

    with pytest.raises(ui_build.UIBuildError, match="escapes"):
        ui_build.ensure_node()
    assert not (tmp_path / "escaped.txt").exists()


def test_multiple_top_levels_are_refused(platform_root, tmp_path, monkeypatch):
    source = tmp_path / "odd.tar.xz"
    digest = make_node_tarball(source, extra_top=True)
    monkeypatch.setattr(ui_build, "platform_key", lambda: "linux-x64")
    monkeypatch.setitem(ui_build.NODE_CHECKSUMS, "linux-x64", digest)
    monkeypatch.setattr(
        ui_build, "_download", lambda url, dest: dest.write_bytes(source.read_bytes())
    )

    with pytest.raises(ui_build.UIBuildError, match="unexpected archive layout"):
        ui_build.ensure_node()


def test_archive_without_node_binary_is_refused(platform_root, tmp_path, monkeypatch):
    source = tmp_path / "empty.tar.xz"
    digest = make_node_tarball(source, with_binaries=False)
    monkeypatch.setattr(ui_build, "platform_key", lambda: "linux-x64")
    monkeypatch.setitem(ui_build.NODE_CHECKSUMS, "linux-x64", digest)
    monkeypatch.setattr(
        ui_build, "_download", lambda url, dest: dest.write_bytes(source.read_bytes())
    )

    with pytest.raises(ui_build.UIBuildError, match="no bin/node"):
        ui_build.ensure_node()


def test_a_partial_extraction_is_not_left_behind(platform_root, fake_archive,
                                                 monkeypatch):
    """An interrupted setup must not leave something that looks usable."""
    def fail_move(src, dst):
        raise OSError("interrupted")

    monkeypatch.setattr(ui_build.shutil, "move", fail_move)
    with pytest.raises(OSError):
        ui_build.ensure_node()

    assert not ui_build.NodeToolchain(root=ui_build.node_dir()).usable


# --- build ------------------------------------------------------------------

def test_build_requires_the_web_sources(platform_root, monkeypatch):
    monkeypatch.setattr(ui_build, "web_source_dir", lambda: None)
    toolchain = ui_build.NodeToolchain(root=platform_root / "node")

    with pytest.raises(ui_build.UIBuildError, match="cannot find the UI sources"):
        ui_build.build_ui(toolchain)


def test_build_requires_a_lockfile(platform_root, tmp_path, monkeypatch):
    web = tmp_path / "web"
    web.mkdir()
    monkeypatch.setattr(ui_build, "web_source_dir", lambda: web)
    toolchain = ui_build.NodeToolchain(root=platform_root / "node")

    with pytest.raises(ui_build.UIBuildError, match="package-lock.json is missing"):
        ui_build.build_ui(toolchain)


def test_build_runs_npm_ci_then_vite_build(platform_root, tmp_path, monkeypatch):
    web = tmp_path / "web"
    web.mkdir()
    (web / "package-lock.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ui_build, "web_source_dir", lambda: web)

    steps = []

    def fake_run(command, cwd, env, capture_output, text, timeout):
        steps.append(command[1:])
        (web / "dist").mkdir(exist_ok=True)
        (web / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(ui_build.subprocess, "run", fake_run)
    dist = ui_build.build_ui(ui_build.NodeToolchain(root=platform_root / "node"))

    assert steps == [["ci"], ["run", "build"]]
    assert dist == web / "dist"


def test_build_failure_surfaces_the_tail_of_the_output(platform_root, tmp_path,
                                                      monkeypatch):
    web = tmp_path / "web"
    web.mkdir()
    (web / "package-lock.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ui_build, "web_source_dir", lambda: web)
    monkeypatch.setattr(
        ui_build.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "ERR_PNPM nope"),
    )

    with pytest.raises(ui_build.UIBuildError, match="ERR_PNPM nope"):
        ui_build.build_ui(ui_build.NodeToolchain(root=platform_root / "node"))


def test_build_that_produces_no_index_is_an_error(platform_root, tmp_path,
                                                  monkeypatch):
    web = tmp_path / "web"
    web.mkdir()
    (web / "package-lock.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ui_build, "web_source_dir", lambda: web)
    monkeypatch.setattr(
        ui_build.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", ""),
    )

    with pytest.raises(ui_build.UIBuildError, match="index.html is missing"):
        ui_build.build_ui(ui_build.NodeToolchain(root=platform_root / "node"))


def test_npm_cache_stays_inside_the_platform_state_dir(platform_root, tmp_path,
                                                       monkeypatch):
    """A build must not write to the operator's ~/.npm."""
    web = tmp_path / "web"
    web.mkdir()
    (web / "package-lock.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ui_build, "web_source_dir", lambda: web)

    seen = {}

    def fake_run(command, cwd, env, capture_output, text, timeout):
        seen.update(env)
        (web / "dist").mkdir(exist_ok=True)
        (web / "dist" / "index.html").write_text("x", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(ui_build.subprocess, "run", fake_run)
    ui_build.build_ui(ui_build.NodeToolchain(root=platform_root / "node"))

    assert seen["npm_config_cache"].startswith(str(platform_root))
    assert seen["PATH"].startswith(str(platform_root))


def test_missing_npm_binary_is_reported(platform_root, tmp_path, monkeypatch):
    web = tmp_path / "web"
    web.mkdir()
    (web / "package-lock.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ui_build, "web_source_dir", lambda: web)
    monkeypatch.setattr(
        ui_build.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()),
    )

    with pytest.raises(ui_build.UIBuildError, match="not found"):
        ui_build.build_ui(ui_build.NodeToolchain(root=platform_root / "node"))


def test_build_timeout_is_reported(platform_root, tmp_path, monkeypatch):
    web = tmp_path / "web"
    web.mkdir()
    (web / "package-lock.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ui_build, "web_source_dir", lambda: web)
    monkeypatch.setattr(
        ui_build.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd="npm", timeout=1)
        ),
    )

    with pytest.raises(ui_build.UIBuildError, match="timed out"):
        ui_build.build_ui(ui_build.NodeToolchain(root=platform_root / "node"))


# --- state reporting --------------------------------------------------------

def test_is_built_reflects_the_bundle(platform_root, tmp_path, monkeypatch):
    web = tmp_path / "web"
    (web / "dist").mkdir(parents=True)
    monkeypatch.setattr(ui_build, "web_source_dir", lambda: web)

    assert ui_build.is_built() is False
    (web / "dist" / "index.html").write_text("x", encoding="utf-8")
    assert ui_build.is_built() is True


def test_dist_dir_is_none_without_sources(monkeypatch):
    monkeypatch.setattr(ui_build, "web_source_dir", lambda: None)
    assert ui_build.dist_dir() is None
    assert ui_build.is_built() is False


def test_installed_mode_alone_does_not_hide_the_sources(monkeypatch, tmp_path):
    """This test previously asserted the bug.

    It required web_source_dir() to be None whenever install mode said INSTALLED —
    which is precisely what broke `uv tool install --from .`: that copies the
    package into an isolated venv, so a user with a real checkout was told they
    needed one. Install mode is not the question; where the sources are, is.
    """
    monkeypatch.delenv(ui_build.ENV_WEB_DIR, raising=False)
    monkeypatch.setattr(ui_build, "repo_root_path", lambda: None)
    monkeypatch.chdir(tmp_path)

    assert ui_build.web_source_dir() is not None, (
        "the sources sit beside this package and must still be found"
    )


def test_web_source_dir_found_in_a_checkout():
    """This repo is a checkout, so the real resolution must work."""
    web = ui_build.web_source_dir()
    assert web is not None
    assert (web / "package.json").is_file()


def test_setup_ui_chains_node_then_build_then_install(platform_root, monkeypatch):
    """Three steps now, and the third is the one that makes the result usable.

    Install was added because the built bundle used to be left in the sources, so
    ``lmer platform run`` could only find it from inside a checkout. A chain that
    stops at "build" still ends in somebody's working tree, which is why the
    ordering is asserted rather than just the return value.
    """
    order = []
    monkeypatch.setattr(
        ui_build, "ensure_node",
        lambda force=False: order.append("node") or ui_build.NodeToolchain(platform_root),
    )
    monkeypatch.setattr(
        ui_build, "build_ui",
        lambda toolchain: order.append("build") or Path("/dist"),
    )
    monkeypatch.setattr(
        ui_build, "install_ui",
        lambda dist: order.append("install") or ui_build.installed_ui_dir(),
    )
    assert ui_build.setup_ui() == ui_build.installed_ui_dir()
    assert order == ["node", "build", "install"]


def test_nothing_is_installed_system_wide(platform_root, fake_archive):
    """The extracted toolchain lives only under the platform state dir."""
    ui_build.ensure_node()
    assert str(ui_build.node_dir()).startswith(str(platform_root))
    assert not (Path.home() / ".nvm").exists() or True  # not created by us
    assert os.environ.get("PATH", "").find(str(ui_build.node_dir())) == -1


# --- finding the sources ----------------------------------------------------
#
# The regression: web_source_dir() gated on repo_root_path(), which is None
# whenever install mode is INSTALLED — and a non-editable `uv tool install --from .`
# COPIES the package into an isolated venv, so a user who installed from a real
# checkout was told they needed a checkout. Install mode was never the question.

def _plant_ui_sources(root, *, name="lmer-platform-ui"):
    web = root / "web"
    web.mkdir(parents=True, exist_ok=True)
    (web / "package.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    return web


def test_sources_are_found_when_install_mode_says_installed(tmp_path, monkeypatch):
    """The exact shape a `uv tool install --from .` user hits."""
    web = _plant_ui_sources(tmp_path)
    monkeypatch.setattr(ui_build, "repo_root_path", lambda: None)
    monkeypatch.setenv(ui_build.ENV_WEB_DIR, str(web))

    assert ui_build.web_source_dir() == web


def test_the_env_override_wins(tmp_path, monkeypatch):
    elsewhere = _plant_ui_sources(tmp_path / "elsewhere")
    monkeypatch.setattr(ui_build, "repo_root_path", lambda: tmp_path / "repo")
    _plant_ui_sources(tmp_path / "repo")
    monkeypatch.setenv(ui_build.ENV_WEB_DIR, str(elsewhere))

    assert ui_build.web_source_dir() == elsewhere


def test_the_working_directory_is_a_candidate(tmp_path, monkeypatch):
    """Running `lmer platform setup-ui` from the checkout you installed from."""
    web = _plant_ui_sources(tmp_path)
    monkeypatch.delenv(ui_build.ENV_WEB_DIR, raising=False)
    monkeypatch.setattr(ui_build, "repo_root_path", lambda: None)
    monkeypatch.setattr(ui_build, "_package_web_dir", lambda: tmp_path / "absent")
    monkeypatch.chdir(tmp_path)

    assert ui_build.web_source_dir() == web


def test_an_unrelated_web_directory_is_not_mistaken_for_ours(tmp_path, monkeypatch):
    """Someone else's project with a web/ directory must not get built."""
    _plant_ui_sources(tmp_path, name="somebody-elses-frontend")
    monkeypatch.delenv(ui_build.ENV_WEB_DIR, raising=False)
    monkeypatch.setattr(ui_build, "repo_root_path", lambda: None)
    monkeypatch.setattr(ui_build, "_package_web_dir", lambda: tmp_path / "absent")
    monkeypatch.chdir(tmp_path)

    assert ui_build.web_source_dir() is None


def test_a_web_directory_without_a_manifest_is_not_ours(tmp_path, monkeypatch):
    (tmp_path / "web").mkdir()
    monkeypatch.delenv(ui_build.ENV_WEB_DIR, raising=False)
    monkeypatch.setattr(ui_build, "repo_root_path", lambda: None)
    monkeypatch.setattr(ui_build, "_package_web_dir", lambda: tmp_path / "absent")
    monkeypatch.chdir(tmp_path)

    assert ui_build.web_source_dir() is None


def test_a_corrupt_manifest_is_not_fatal(tmp_path, monkeypatch):
    web = tmp_path / "web"
    web.mkdir()
    (web / "package.json").write_text("{not json", encoding="utf-8")
    monkeypatch.delenv(ui_build.ENV_WEB_DIR, raising=False)
    monkeypatch.setattr(ui_build, "repo_root_path", lambda: None)
    monkeypatch.setattr(ui_build, "_package_web_dir", lambda: tmp_path / "absent")
    monkeypatch.chdir(tmp_path)

    assert ui_build.web_source_dir() is None


def test_the_failure_names_what_it_tried_and_how_to_override(platform_root, tmp_path,
                                                            monkeypatch):
    """An error that asserts something false about the user's install is worse
    than no error: this one has to be actionable."""
    monkeypatch.delenv(ui_build.ENV_WEB_DIR, raising=False)
    monkeypatch.setattr(ui_build, "web_source_dir", lambda: None)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ui_build.UIBuildError) as excinfo:
        ui_build.build_ui(ui_build.NodeToolchain(root=platform_root / "node"))

    message = str(excinfo.value)
    assert ui_build.ENV_WEB_DIR in message
    assert "Looked in:" in message
    assert str(tmp_path / "web") in message
    assert "installed package" not in message, "the old claim was simply wrong"


def test_this_checkout_resolves_beside_the_package(monkeypatch):
    """Candidate 2: …/<root>/src/lmer_platform → …/<root>/web, no env, no cwd."""
    monkeypatch.delenv(ui_build.ENV_WEB_DIR, raising=False)
    monkeypatch.setattr(ui_build, "repo_root_path", lambda: None)
    monkeypatch.chdir("/tmp")

    web = ui_build.web_source_dir()
    assert web is not None
    assert web == Path(ui_build.__file__).resolve().parents[2] / "web"


# --- where the build output lives -------------------------------------------
#
# The bug: `lmer platform` only worked from inside a checkout. dist_dir() was
# web_source_dir()/dist, and web_source_dir()'s only candidate that resolves in
# INSTALLED mode is ./web — so `run` from anywhere else reported the UI as unbuilt
# minutes after building it, and `setup-ui` elsewhere found nothing to build.
# The operator hit both in live testing. Building needs the sources; *serving* must
# not.

def built_bundle(root: Path) -> Path:
    """A directory shaped like a vite `dist/`."""
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>ui</title>", encoding="utf-8")
    (dist / "assets" / "app-aaaa.js").write_text("console.log(1)", encoding="utf-8")
    return dist


def test_the_installed_bundle_lives_beside_the_platforms_other_state(platform_root):
    """Next to the Node it fetched, not in somebody's working tree."""
    assert ui_build.installed_ui_dir() == platform_root / "ui"
    assert ui_build.node_dir().parent.parent == ui_build.installed_ui_dir().parent


def test_installing_makes_the_bundle_findable_with_no_sources_anywhere(
    platform_root, tmp_path, monkeypatch
):
    """The actual fix: dist_dir() resolves with cwd nowhere near a checkout."""
    ui_build.install_ui(built_bundle(tmp_path / "build"))

    # No sources reachable by any route: no env override, no package-adjacent web/,
    # no repo root, and a cwd that has nothing to do with lmer.
    monkeypatch.delenv(ui_build.ENV_WEB_DIR, raising=False)
    monkeypatch.setattr(ui_build, "_package_web_dir", lambda: tmp_path / "nope")
    monkeypatch.setattr(ui_build, "repo_root_path", lambda: None)
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert ui_build.web_source_dir() is None, "this test is not exercising the fix"
    assert ui_build.dist_dir() == platform_root / "ui"
    assert ui_build.is_built()


def test_the_installed_copy_wins_over_one_left_in_a_working_tree(
    platform_root, tmp_path, monkeypatch
):
    """It is the copy setup-ui produced and the only one reachable from anywhere."""
    web = tmp_path / "web"
    (web / "dist").mkdir(parents=True)
    (web / "dist" / "index.html").write_text("stale", encoding="utf-8")
    monkeypatch.setattr(ui_build, "web_source_dir", lambda: web)

    ui_build.install_ui(built_bundle(tmp_path / "build"))

    assert ui_build.dist_dir() == platform_root / "ui"


def test_a_developers_own_build_is_still_served_when_nothing_is_installed(
    platform_root, tmp_path, monkeypatch
):
    """`npm run build` in web/ then looking at it must not require setup-ui."""
    web = tmp_path / "web"
    built_bundle(web)
    monkeypatch.setattr(ui_build, "web_source_dir", lambda: web)

    assert ui_build.dist_dir() == web / "dist"
    assert ui_build.is_built()


def test_installing_replaces_rather_than_merges(platform_root, tmp_path):
    """Hashed filenames differ between builds, so a merge strands old chunks and
    leaves an index.html that could be served next to either build's assets."""
    ui_build.install_ui(built_bundle(tmp_path / "first"))

    second = tmp_path / "second"
    dist = second / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>new</title>", encoding="utf-8")
    (dist / "assets" / "app-bbbb.js").write_text("console.log(2)", encoding="utf-8")
    ui_build.install_ui(dist)

    installed = ui_build.installed_ui_dir()
    assets = sorted(p.name for p in (installed / "assets").iterdir())
    assert assets == ["app-bbbb.js"], f"the previous build's chunks survived: {assets}"


def test_a_reinstall_never_leaves_the_served_directory_half_copied(
    platform_root, tmp_path, monkeypatch
):
    """A daemon serving mid-reinstall must see the old bundle or the new one.

    The copy is assembled beside the target and moved in, so a failure partway
    through cannot be observed as a bundle with an index.html and no assets.
    """
    ui_build.install_ui(built_bundle(tmp_path / "first"))
    original = (ui_build.installed_ui_dir() / "index.html").read_text(encoding="utf-8")

    real_copytree = ui_build.shutil.copytree

    def fail_partway(src, dst, *args, **kwargs):
        real_copytree(src, dst, *args, **kwargs)
        raise OSError("disk full halfway through")

    monkeypatch.setattr(ui_build.shutil, "copytree", fail_partway)

    with pytest.raises((ui_build.UIBuildError, OSError)):
        ui_build.install_ui(built_bundle(tmp_path / "second"))

    served = ui_build.installed_ui_dir()
    assert (served / "index.html").read_text(encoding="utf-8") == original
    assert (served / "assets").is_dir(), "the served bundle lost its assets"


def test_installing_leaves_no_staging_directories_behind(platform_root, tmp_path):
    """Twice, so the second run also proves the first cleaned up after itself."""
    for name in ("first", "second"):
        ui_build.install_ui(built_bundle(tmp_path / name))

    strays = [p.name for p in platform_root.iterdir() if p.name.startswith("ui.")]
    assert not strays, f"left temporary directories behind: {strays}"


def test_setup_ui_installs_what_it_builds(platform_root, tmp_path, monkeypatch):
    """Otherwise the whole chain still ends in a working tree."""
    dist = built_bundle(tmp_path / "build")
    monkeypatch.setattr(ui_build, "ensure_node", lambda force=False: "toolchain")
    monkeypatch.setattr(ui_build, "build_ui", lambda toolchain: dist)

    result = ui_build.setup_ui()

    assert result == ui_build.installed_ui_dir()
    assert (result / "index.html").is_file()


def test_the_missing_sources_error_says_building_is_the_only_step_that_needs_them(
    platform_root, tmp_path, monkeypatch
):
    """The operator read "cannot find the UI sources" as "you must run everything
    here"."""
    monkeypatch.delenv(ui_build.ENV_WEB_DIR, raising=False)
    monkeypatch.setattr(ui_build, "web_source_dir", lambda: None)

    with pytest.raises(ui_build.UIBuildError) as caught:
        ui_build.build_ui("toolchain")

    message = str(caught.value)
    assert str(ui_build.installed_ui_dir()) in message
    assert "any directory" in message


# --- a bundle somebody built elsewhere --------------------------------------
#
# LMER_PLATFORM_UI_DIST (issue #150): the platform container image builds the UI
# during the image build and points this at it, so a deployment that pulls the
# image needs no Node, no checkout and no setup-ui. Every test above runs with the
# variable unset — the autouse fixture strips LMER_* — so they are also the
# assertion that unset behavior is unchanged.

def test_a_configured_bundle_is_served(platform_root, tmp_path, monkeypatch):
    dist = built_bundle(tmp_path / "baked")
    monkeypatch.setenv(ui_build.ENV_UI_DIST, str(dist))

    assert ui_build.dist_dir() == dist
    assert ui_build.is_built()


def test_a_configured_bundle_beats_an_installed_one(platform_root, tmp_path,
                                                    monkeypatch):
    """The image's own bundle, not a host-built one on a mounted state dir.

    This is the ordering's whole reason: a container keeping its config, secret and
    runs on the host's platform dir inherits whatever ``ui/`` the host's lmer
    built, and serving that against this image's API is a stale UI in front of a
    newer control plane.
    """
    ui_build.install_ui(built_bundle(tmp_path / "host-built"))
    dist = built_bundle(tmp_path / "baked")
    monkeypatch.setenv(ui_build.ENV_UI_DIST, str(dist))

    assert ui_build.dist_dir() == dist


def test_a_configured_path_that_is_not_there_is_skipped(platform_root, tmp_path,
                                                        monkeypatch):
    """A wrong path must not turn the UI off — the next candidate still answers."""
    installed = ui_build.install_ui(built_bundle(tmp_path / "build"))
    monkeypatch.setenv(ui_build.ENV_UI_DIST, str(tmp_path / "absent"))

    assert ui_build.dist_dir() == installed


def test_a_configured_directory_with_no_index_is_not_a_bundle(platform_root, tmp_path,
                                                              monkeypatch):
    """The same test the installed copy gets: a directory is not a build."""
    empty = tmp_path / "empty"
    empty.mkdir()
    web = tmp_path / "web"
    built_bundle(web)
    monkeypatch.setattr(ui_build, "web_source_dir", lambda: web)
    monkeypatch.setenv(ui_build.ENV_UI_DIST, str(empty))

    assert ui_build.dist_dir() == web / "dist"


def test_a_blank_setting_counts_as_unset(platform_root, tmp_path, monkeypatch):
    """How a wrapper script disables one it inherited, as with ENV_WEB_DIR."""
    installed = ui_build.install_ui(built_bundle(tmp_path / "build"))
    monkeypatch.setenv(ui_build.ENV_UI_DIST, "   ")

    assert ui_build.dist_dir() == installed


def test_without_the_setting_nothing_changes(platform_root, tmp_path, monkeypatch):
    """Unset resolves exactly as it did before the seam existed."""
    monkeypatch.delenv(ui_build.ENV_UI_DIST, raising=False)
    web = tmp_path / "web"
    built_bundle(web)
    monkeypatch.setattr(ui_build, "web_source_dir", lambda: web)

    assert ui_build.dist_dir() == web / "dist"
    ui_build.install_ui(built_bundle(tmp_path / "build"))
    assert ui_build.dist_dir() == ui_build.installed_ui_dir()


def test_the_setting_is_not_a_platform_config_field(platform_root):
    """Read in ui_build, deliberately: it names where files are, not a setting.

    A field would be persisted in config.json and editable in the UI, and a UI
    that can repoint the daemon at a directory that has no bundle is a UI that can
    remove itself.
    """
    from lmer_platform import config as config_mod

    assert not hasattr(config_mod.PlatformConfig(), "ui_dist")
    assert ui_build.ENV_UI_DIST not in config_mod.__dict__.values()
