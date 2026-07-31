"""Building the control UI without asking the host for a Node toolchain.

Why this exists
---------------
The UI is a Vite + Vue app (spec D10), which needs Node to build — but requiring a
preinstalled Node on every host that runs the platform is a poor trade, and
committing ``dist/`` to git is worse (build output in diffs, on a public mirror).
So the platform fetches its **own** pinned Node into its state dir, builds with
it, and never touches anything system-wide. Nothing is added to ``PATH``; the
absolute path to the extracted binary is used directly.

Supply chain
------------
The Node archive's SHA-256 is **pinned in this file** and verified before anything
is extracted. That makes bumping Node a deliberate, reviewable act rather than a
silent "whatever nodejs.org serves today", and it is the same instinct behind the
project's one-week-old-package policy for Python dependencies. A checksum mismatch
aborts before extraction — an archive that is not what we pinned is not unpacked
"just to see".

JS dependencies are pinned by ``web/package-lock.json`` and installed with
``npm ci``, which fails rather than resolving something new.

Where the sources have to be
----------------------------
The build needs the ``web/`` directory, and finding it asks "where are the
sources", never "is this an installed package" — see :func:`web_source_dir` for
the resolution order and for the bug that distinction fixes. A non-editable
``uv tool install --from .`` copies the package into an isolated venv, so install
mode says INSTALLED for a user who installed from a perfectly good checkout; a
gate on install mode refused them.

What remains genuinely unsupported is an install with no checkout anywhere —
``uv tool install lmer --from git+…`` on a machine that never cloned the repo.
Shipping the web sources inside the distribution, or prebuilt assets, is the
follow-up that would lift that; until then the failure names every path it tried
and the variable that overrides them, rather than asserting something false about
the user's install.

Where the *build output* goes
----------------------------
Not into the sources, which is where it used to go and why ``lmer platform`` only
worked from inside a checkout: ``dist_dir()`` was ``web_source_dir()/dist``, and in
INSTALLED mode the only candidate that resolves is ``./web``. So ``run`` from any
other directory reported the UI as unbuilt even though it had been built minutes
earlier. Serving a live working tree was the underlying mistake — a ``git clean``
in the checkout could take the running UI with it.

``setup-ui`` now *installs* what it builds into :func:`installed_ui_dir`
(``platform_dir()/ui``), beside the Node it fetched, and :func:`dist_dir` prefers
that copy. Building still needs the sources — inherently; a bundler needs input —
so ``setup-ui`` remains a command you run where the checkout is, once. Everything
after it is location-independent.

Where a bundle can come from instead
------------------------------------
The follow-up named above — shipping prebuilt assets — arrives as an image rather
than as a wheel: ``Dockerfile.platform`` builds the UI during the image build and
points :data:`ENV_UI_DIST` at the result (issue #150), so a deployment that pulls
that image needs neither Node nor a checkout nor ``setup-ui``. The variable is
read here and not in :mod:`lmer_platform.config`, for the same reason
:data:`ENV_WEB_DIR` is: it names *where files are* for one function, not a setting
the operator configures through the UI and persists in ``config.json``.

It is the **first** candidate :func:`dist_dir` tries, which is deliberate and is
the one place this ordering differs from what the installed copy usually wins.
Two reasons. It matches :func:`web_source_dir`, where the explicit environment
override also comes first — a variable an operator (or an image) set on purpose
beating something found by convention is the rule everywhere else in this module.
And the case where both exist is a container mounting the host's platform state
dir to keep its config, secret and runs: that dir may carry a ``ui/`` built by
whatever lmer the *host* had, and serving it against this image's API would be a
stale UI in front of a newer control plane. To serve something else from inside
such an image, point the variable at it or unset it; nothing else about it
changes, and with the variable unset :func:`dist_dir` behaves exactly as before.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from lmer_cli.runtime import repo_root_path
from lmer_cli.tls import ensure_ca_bundle

from .store import platform_dir

logger = logging.getLogger("lmer_platform.ui_build")

__all__ = [
    "UIBuildError", "NODE_VERSION", "NODE_CHECKSUMS", "NodeToolchain", "ENV_WEB_DIR",
    "ENV_UI_DIST", "node_dir", "dist_dir", "web_source_dir", "platform_key",
    "archive_name", "download_url", "ensure_node", "build_ui", "setup_ui",
    "is_built", "installed_ui_dir", "install_ui",
]

#: Pinned Node LTS. Bumping this requires updating NODE_CHECKSUMS in the same
#: commit — the whole point is that the pair is reviewed together.
NODE_VERSION = "v24.18.0"

#: SHA-256 of each official archive, from https://nodejs.org/dist/<ver>/SHASUMS256.txt
NODE_CHECKSUMS = {
    "linux-x64": "55aa7153f9d88f28d765fcdad5ae6945b5c0f98a36881703817e4c450fa76742",
    "linux-arm64": "58c9520501f6ae2b52d5b210444e24b9d0c029a58c5011b797bc1fe7105886f6",
    "darwin-x64": "4a3b6bc81542154430825128d9a279e8b364e8d90581544e506ef7579fd1ab6f",
    "darwin-arm64": "4477b9f78efb77744cf5eb57a0e9594dba66466b38b4e93fa9f35cb907a095a6",
}

NODE_BASE_URL = "https://nodejs.org/dist"
DOWNLOAD_TIMEOUT_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 900
_CHUNK = 256 * 1024


class UIBuildError(RuntimeError):
    """Raised when the UI cannot be built."""


@dataclass(frozen=True)
class NodeToolchain:
    """An extracted Node the platform owns."""

    root: Path

    @property
    def node(self) -> Path:
        return self.root / "bin" / "node"

    @property
    def npm(self) -> Path:
        return self.root / "bin" / "npm"

    @property
    def usable(self) -> bool:
        return self.node.is_file() and self.npm.exists()


def platform_key() -> str:
    """``<os>-<arch>`` in Node's own naming, e.g. ``linux-x64``."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    os_name = {"linux": "linux", "darwin": "darwin"}.get(system)
    arch = {
        "x86_64": "x64", "amd64": "x64",
        "aarch64": "arm64", "arm64": "arm64",
    }.get(machine)
    if not os_name or not arch:
        raise UIBuildError(
            f"no pinned Node build for {system}/{machine} — install Node "
            "yourself and build web/ with `npm ci && npm run build`"
        )
    return f"{os_name}-{arch}"


def archive_name(key: Optional[str] = None) -> str:
    return f"node-{NODE_VERSION}-{key or platform_key()}.tar.xz"


def download_url(key: Optional[str] = None) -> str:
    return f"{NODE_BASE_URL}/{NODE_VERSION}/{archive_name(key)}"


def node_dir() -> Path:
    """Where the platform keeps its own Node, versioned so a bump is additive."""
    return platform_dir() / "node" / NODE_VERSION


#: Set to point the UI build at the ``web/`` sources explicitly. The escape hatch
#: for any install shape this cannot work out on its own.
ENV_WEB_DIR = "LMER_PLATFORM_WEB_DIR"

#: Absolute path to an already-built bundle to serve, for a deployment that has
#: one and no Node toolchain: the platform container image builds the UI at image
#: build time and sets this to it (``Dockerfile.platform``, issue #150). Read
#: here rather than mapped into :class:`lmer_platform.config.PlatformConfig` —
#: see the module docstring for that and for why it is the first candidate
#: :func:`dist_dir` tries. Unset changes nothing.
ENV_UI_DIST = "LMER_PLATFORM_UI_DIST"

#: What a directory must call itself to be these sources. Guards the cwd probe
#: below: a project of your own with a ``web/`` directory must not be mistaken for
#: the platform's UI and built by it.
_UI_PACKAGE_NAME = "lmer-platform-ui"


def _is_ui_source_dir(candidate: Optional[Path]) -> bool:
    """Whether *candidate* really is the platform's UI sources."""
    if candidate is None or not candidate.is_dir():
        return False
    manifest = candidate / "package.json"
    if not manifest.is_file():
        return False
    try:
        return json.loads(manifest.read_text(encoding="utf-8")).get("name") == _UI_PACKAGE_NAME
    except (OSError, ValueError):
        return False


def web_source_dir() -> Optional[Path]:
    """Locate the ``web/`` sources, or ``None`` when they are genuinely absent.

    This deliberately asks "where are the sources" rather than "is this an
    installed package". The earlier version gated on ``repo_root_path()``, which is
    ``None`` whenever :func:`lmer_cli.runtime.detect_install_mode` says INSTALLED —
    and a non-editable ``uv tool install --from .`` *copies* the package into an
    isolated venv, so a user who installed from a perfectly good checkout was told
    they needed a checkout. Install mode was never the question.

    Resolution order, first hit wins:

    1. ``$LMER_PLATFORM_WEB_DIR`` — explicit, works for any layout.
    2. Beside the installed package (``…/src/lmer_platform`` → ``…/web``), which
       covers an editable install and a plain checkout.
    3. The repo root, for developer mode.
    4. ``./web`` under the current directory — the common case of running
       ``lmer platform setup-ui`` from the checkout you installed from.

    Every candidate must identify itself as the UI package (:func:`_is_ui_source_dir`),
    so a stray ``web/`` directory in an unrelated project is never built by mistake.
    """
    for candidate in _web_source_candidates():
        if _is_ui_source_dir(candidate):
            return candidate
    return None


def _package_web_dir() -> Path:
    """``web/`` beside the installed package: …/<root>/src/lmer_platform → …/<root>/web.

    Its own function so a test can neutralise this one candidate and exercise the
    others — in a checkout it always resolves, which would otherwise mask them.
    """
    return Path(__file__).resolve().parents[2] / "web"


def _web_source_candidates() -> list:
    """Every place the UI sources might be, in precedence order."""
    candidates: list = []

    configured = os.environ.get(ENV_WEB_DIR, "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())

    candidates.append(_package_web_dir())

    root = repo_root_path()
    if root is not None:
        candidates.append(root / "web")

    candidates.append(Path.cwd() / "web")
    return candidates


def installed_ui_dir() -> Path:
    """Where a built bundle is *installed*, independent of any checkout.

    The bug this exists to fix: the built UI used to live only inside the sources,
    and the sources are found partly by :func:`lmer_cli.runtime.repo_root_path`,
    which returns ``None`` in INSTALLED mode — so for a ``uv tool install``ed lmer
    the only candidate that ever resolved was ``Path.cwd() / "web"``. That made
    ``lmer platform`` a command you had to run *from inside the checkout*:
    elsewhere, ``run`` reported the UI as unbuilt and ``setup-ui`` could not find
    anything to build.

    Serving assets out of a working tree was the mistake underneath that. A build
    artifact belongs beside the other machine-local state this platform installs
    for itself — the pinned Node already lives in ``platform_dir()/node`` — so the
    daemon can find it from any working directory, and a stray ``git clean`` in the
    checkout cannot take the running UI with it.
    """
    return platform_dir() / "ui"


def _configured_dist() -> Optional[Path]:
    """The bundle :data:`ENV_UI_DIST` names, or ``None`` when it names none.

    Blank counts as unset, the same tolerance :func:`_web_source_candidates` gives
    :data:`ENV_WEB_DIR`: an exported-but-empty variable is how a wrapper script
    disables one it inherited, and treating it as the path ``""`` would be a
    directory nobody meant.
    """
    configured = os.environ.get(ENV_UI_DIST, "").strip()
    return Path(configured).expanduser() if configured else None


def dist_dir() -> Optional[Path]:
    """The bundle to serve: a configured one, the installed copy, else the sources'.

    Order matters and is deliberate. :data:`ENV_UI_DIST` comes first because it is
    somebody saying explicitly which bundle this deployment serves — the module
    docstring has the two reasons, and the case that decides it is a container
    image whose baked UI must not be shadowed by a host-built one on a mounted
    state dir. The installed copy is next: it is the one ``setup-ui`` produced and
    the only one reachable from an arbitrary directory. The in-source ``web/dist``
    stays as a fallback for the developer loop — someone who just ran
    ``npm run build`` by hand in ``web/`` expects to see that, and making them
    re-run ``setup-ui`` to look at their own build would be silly.

    A candidate has to hold an ``index.html`` to be chosen, so a path that is not
    there — or a state dir mounted empty — falls through to the next one instead of
    turning the UI off. Only ``web/dist`` is returned unchecked, which is what
    lets :func:`is_built` and the daemon's startup notice say "not built" about a
    checkout rather than "no UI anywhere".
    """
    for candidate in (_configured_dist(), installed_ui_dir()):
        if candidate is not None and (candidate / "index.html").is_file():
            return candidate

    web = web_source_dir()
    return None if web is None else web / "dist"


def install_ui(dist: Path) -> Path:
    """Copy a freshly built bundle into :func:`installed_ui_dir`.

    Replacement rather than overlay: an old bundle's hashed asset filenames differ
    from a new one's, so merging two builds leaves the previous chunks lying around
    and an ``index.html`` that could be served alongside assets from either. The
    swap is also ordered so the live directory is never a half-copied tree — the
    new bundle is assembled beside it and only then moved into place.
    """
    target = installed_ui_dir()
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(f"{target.name}.new-{os.getpid()}")
    previous = target.with_name(f"{target.name}.old-{os.getpid()}")

    for leftover in (staging, previous):
        if leftover.exists():
            shutil.rmtree(leftover)

    shutil.copytree(dist, staging)
    try:
        if target.exists():
            target.rename(previous)
        staging.rename(target)
    except OSError as exc:
        # Put back whatever was serving before failing: a daemon mid-request is
        # better off with the old bundle than with no bundle.
        if previous.exists() and not target.exists():
            previous.rename(target)
        raise UIBuildError(f"cannot install the built UI into {target} ({exc})")
    finally:
        if previous.exists():
            shutil.rmtree(previous, ignore_errors=True)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    logger.info("platform_ui_installed dir=%s", target)
    return target


def is_built() -> bool:
    dist = dist_dir()
    return bool(dist and (dist / "index.html").is_file())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, dest: Path) -> None:
    logger.info("platform_node_download url=%s", url)
    # Before the first TLS handshake, not after: the standalone CPython that
    # `uv tool install` fetches can be compiled with a CA path that does not exist
    # on the host, and then this download fails with CERTIFICATE_VERIFY_FAILED
    # ("unable to get local issuer certificate") even though nodejs.org is fine.
    # Setting it later would not help — OpenSSL reads SSL_CERT_FILE when the
    # context loads its default certs.
    ensure_ca_bundle()
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            with open(dest, "wb") as sink:
                shutil.copyfileobj(response, sink, _CHUNK)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise UIBuildError(
            f"cannot download {url} ({exc})"
            + (
                # The bare error reads as a problem at the far end, which sends the
                # operator looking in the wrong place. Say what it usually is.
                "\nIf this is a certificate verification failure, the host's CA"
                " bundle could not be found rather than nodejs.org being"
                " untrusted. Set SSL_CERT_FILE to your CA bundle (a corporate CA"
                " if you are behind a TLS-inspecting proxy) and retry."
                if "CERTIFICATE_VERIFY_FAILED" in str(exc)
                else ""
            )
        )


def _safe_extract(archive: Path, into: Path) -> Path:
    """Extract *archive*, refusing any member that escapes *into*.

    Tarballs are attacker-controlled input in general. This one is checksum-pinned,
    so a malicious member would mean nodejs.org itself was compromised *and* the
    pin updated — but the check costs nothing and the failure mode it prevents is
    writing outside the state dir.
    """
    into.mkdir(parents=True, exist_ok=True)
    resolved_root = into.resolve()
    try:
        with tarfile.open(archive, "r:xz") as tar:
            members = tar.getmembers()
            for member in members:
                target = (resolved_root / member.name).resolve()
                if not str(target).startswith(str(resolved_root)):
                    raise UIBuildError(
                        f"refusing to extract {member.name}: escapes {into}"
                    )
                if member.issym() or member.islnk():
                    link_target = (target.parent / member.linkname).resolve()
                    if not str(link_target).startswith(str(resolved_root)):
                        raise UIBuildError(
                            f"refusing to extract link {member.name}: escapes {into}"
                        )
            # filter="data" is the strict extraction policy (Python 3.12+): it
            # refuses absolute and parent paths, links pointing outside the tree,
            # device/special files, and setuid/setgid bits — while keeping the
            # executable bit bin/node needs. The explicit member walk above stays
            # because it gives a named, actionable error instead of a generic
            # tarfile refusal, and catches the multi-top-level case this cannot.
            tar.extractall(into, filter="data")
            top_levels = {Path(m.name).parts[0] for m in members if m.name}
    except tarfile.TarError as exc:
        raise UIBuildError(f"cannot extract {archive.name} ({exc})")

    if len(top_levels) != 1:
        raise UIBuildError(
            f"unexpected archive layout in {archive.name}: {sorted(top_levels)}"
        )
    return into / top_levels.pop()


def ensure_node(*, force: bool = False) -> NodeToolchain:
    """Return a usable Node, downloading the pinned build if needed.

    Reuses an existing extraction unless *force*, so a rebuild costs nothing after
    the first run. The download lands in a temp file and is checksum-verified
    before extraction, and the extracted tree is moved into place only once
    complete — an interrupted setup never leaves a half-populated toolchain that
    looks usable.
    """
    target = node_dir()
    existing = NodeToolchain(root=target)
    if existing.usable and not force:
        logger.debug("platform_node_reused root=%s", target)
        return existing

    key = platform_key()
    expected = NODE_CHECKSUMS.get(key)
    if not expected:
        raise UIBuildError(f"no pinned checksum for {key}")

    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="lmer-node-") as tmpdir:
        tmp = Path(tmpdir)
        archive = tmp / archive_name(key)
        _download(download_url(key), archive)

        actual = _sha256(archive)
        if actual != expected:
            raise UIBuildError(
                f"checksum mismatch for {archive.name}: expected {expected}, "
                f"got {actual} — refusing to extract"
            )

        extracted = _safe_extract(archive, tmp / "unpacked")
        # Move into place last: an interrupted run leaves nothing to mistake for
        # a complete toolchain.
        shutil.move(str(extracted), str(target))

    toolchain = NodeToolchain(root=target)
    if not toolchain.usable:
        raise UIBuildError(
            f"extracted Node at {target} has no bin/node — unexpected archive layout"
        )
    logger.info("platform_node_ready version=%s root=%s", NODE_VERSION, target)
    return toolchain


def _run(command: list, *, cwd: Path, toolchain: NodeToolchain, label: str) -> None:
    """Run a Node command with the platform's own toolchain on PATH.

    ``PATH`` is prefixed rather than replaced because npm shells out to git and a
    system shell; the prefix ensures *our* node wins without hiding the rest of
    the environment.
    """
    env = {
        **os.environ,
        "PATH": f"{toolchain.root / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
        # Keep npm's cache and any config inside the platform's state dir so a
        # build never writes to the operator's ~/.npm.
        "npm_config_cache": str(platform_dir() / "npm-cache"),
        "npm_config_update_notifier": "false",
        "npm_config_fund": "false",
        "npm_config_audit": "false",
        "CI": "1",
    }
    try:
        result = subprocess.run(
            command, cwd=str(cwd), env=env, capture_output=True, text=True,
            timeout=BUILD_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        raise UIBuildError(f"{label}: {command[0]} not found")
    except subprocess.TimeoutExpired:
        raise UIBuildError(f"{label} timed out after {BUILD_TIMEOUT_SECONDS}s")

    if result.returncode != 0:
        tail = ((result.stderr or "") + (result.stdout or "")).strip()
        raise UIBuildError(f"{label} failed:\n{tail[-2000:]}")
    logger.info("platform_ui_step_ok step=%s", label)


def build_ui(toolchain: NodeToolchain) -> Path:
    """Install pinned dependencies and build the bundle. Returns the dist dir."""
    web = web_source_dir()
    if web is None:
        raise UIBuildError(
            "cannot find the UI sources (a web/ directory whose package.json is "
            f"named {_UI_PACKAGE_NAME}). Looked in:\n"
            + "\n".join(f"  {path}" for path in _web_source_candidates())
            + f"\nRun this from your lmer checkout, or set {ENV_WEB_DIR} to its "
            "web/ directory.\nOnly *building* needs the sources: the built bundle "
            f"is installed to {installed_ui_dir()}, so once this has succeeded "
            "once, `lmer platform run` works from any directory."
        )
    if not (web / "package-lock.json").is_file():
        raise UIBuildError(f"{web}/package-lock.json is missing; cannot run npm ci")

    _run([str(toolchain.npm), "ci"], cwd=web, toolchain=toolchain, label="npm ci")
    _run([str(toolchain.npm), "run", "build"], cwd=web, toolchain=toolchain,
         label="vite build")

    dist = web / "dist"
    if not (dist / "index.html").is_file():
        raise UIBuildError(f"build finished but {dist}/index.html is missing")
    return dist


def setup_ui(*, force_node: bool = False) -> Path:
    """Full bootstrap: pinned Node, pinned deps, built bundle, installed.

    Installing is the step that makes the result usable from anywhere. Building
    still needs the sources — that is inherent, a bundler needs something to bundle
    — so this remains a command you run once where the checkout is. Everything
    afterwards, ``lmer platform run`` included, reads the installed copy and does
    not care what directory it is in.
    """
    toolchain = ensure_node(force=force_node)
    return install_ui(build_ui(toolchain))
