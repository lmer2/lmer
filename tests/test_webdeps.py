"""The web-deps bootstrap: fresh containers stop failing node tests (T125).

Two live sessions on one day each burned a gate run discovering that Node
exists (mise) while ``web/node_modules`` does not (gitignored) — the skip
logic only covered Node's absence. These tests pin the bootstrap that closed
that: staleness detection, the one-shot memo, failure containment, and the
meta-guard that every ``_node_binary`` resolver actually calls it.
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests import webdeps
from tests.timing_budgets import SETTLE

TESTS_DIR = Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def fresh_memo(monkeypatch):
    """Each test sees an unchecked process, and none touches the real web/."""
    monkeypatch.setattr(webdeps, "_done", {"checked": False})


@pytest.fixture()
def fake_web(tmp_path, monkeypatch):
    web = tmp_path / "web"
    (web / "node_modules").mkdir(parents=True)
    monkeypatch.setattr(webdeps, "WEB_DIR", web)
    return web


def _plant(web, *, lock=True, installed=True, lock_newer=False):
    if lock:
        (web / "package-lock.json").write_text("{}", encoding="utf-8")
    if installed:
        marker = web / "node_modules" / ".package-lock.json"
        marker.write_text("{}", encoding="utf-8")
        if lock_newer:
            past = marker.stat().st_mtime - 100
            os.utime(marker, (past, past))


def test_a_current_install_costs_a_stat_and_nothing_else(fake_web, monkeypatch):
    _plant(fake_web)

    def forbidden(*a, **k):
        raise AssertionError("npm ran against a current install")

    monkeypatch.setattr(subprocess, "run", forbidden)
    webdeps.ensure_web_deps()


def test_a_missing_install_runs_npm_ci_once(fake_web, monkeypatch):
    _plant(fake_web, installed=False)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(webdeps.shutil, "which", lambda name: "/usr/bin/npm")

    webdeps.ensure_web_deps()
    webdeps.ensure_web_deps()

    assert len(calls) == 1, "the install is memoized for the process"
    assert calls[0][0][:2] == ["/usr/bin/npm", "ci"]


def test_a_lockfile_newer_than_the_install_reinstalls(fake_web, monkeypatch):
    """The branch-switch case, not just the fresh container."""
    _plant(fake_web, lock_newer=True)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(webdeps.shutil, "which", lambda name: "/usr/bin/npm")

    webdeps.ensure_web_deps()

    assert calls, "a stale install must be refreshed"


def test_a_failed_install_is_reported_and_never_raises(fake_web, monkeypatch,
                                                       capsys):
    """The calling test's own failure carries the real cause — the bootstrap
    must not replace it with its own traceback."""
    _plant(fake_web, installed=False)
    monkeypatch.setattr(webdeps.shutil, "which", lambda name: "/usr/bin/npm")

    def boom(*a, **k):
        raise subprocess.SubprocessError("no network")

    monkeypatch.setattr(subprocess, "run", boom)

    webdeps.ensure_web_deps()

    assert "npm ci failed" in capsys.readouterr().out


def test_a_read_only_web_dir_is_reported_and_never_raises(fake_web, monkeypatch,
                                                          capsys):
    """No lock file can be created, so no install is attempted either."""
    _plant(fake_web, installed=False)
    monkeypatch.setattr(webdeps.shutil, "which", lambda name: "/usr/bin/npm")

    def read_only():
        raise PermissionError(13, "Permission denied", str(fake_web / ".test-deps.lock"))

    def forbidden(*a, **k):
        raise AssertionError("npm ran without the lock")

    monkeypatch.setattr(webdeps, "web_lock", read_only)
    monkeypatch.setattr(subprocess, "run", forbidden)

    webdeps.ensure_web_deps()

    assert "cannot lock web/" in capsys.readouterr().out


def test_npm_absent_is_one_message_not_an_error(fake_web, monkeypatch, capsys):
    _plant(fake_web, installed=False)
    monkeypatch.setattr(webdeps.shutil, "which", lambda name: None)

    webdeps.ensure_web_deps()

    assert "npm is not on PATH" in capsys.readouterr().out


FAKE_NPM = """#!/bin/sh
echo "$$" >> "$NPM_LOG"
while [ ! -e "$NPM_RELEASE" ]; do sleep 0.05; done
mkdir -p node_modules
echo '{}' > node_modules/.package-lock.json
"""

WORKER = (
    "import sys; from pathlib import Path; from tests import webdeps; "
    "webdeps.WEB_DIR = Path(sys.argv[1]); "
    "webdeps.ensure_web_deps()"
)


def _until(predicate, what):
    deadline = time.monotonic() + SETTLE
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


def _blocked_on(path):
    """A process is queued for *path*'s flock (``->`` rows in /proc/locks)."""
    if not path.exists():
        return False
    st = path.stat()
    key = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino} "
    return any("-> FLOCK" in row and key in row
               for row in Path("/proc/locks").read_text().splitlines())


def test_concurrent_workers_on_an_empty_install_run_npm_ci_once(fake_web, tmp_path):
    """#479: xdist workers share one web/, and npm ci deletes node_modules first.

    The first install is held open until the second worker has either started
    its own npm ci or queued on the lock, so neither outcome depends on timing.
    """
    if not os.access("/proc/locks", os.R_OK):
        pytest.skip("needs /proc/locks to see a worker queued on the lock")
    _plant(fake_web, installed=False)
    (fake_web / "node_modules").rmdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    npm = bin_dir / "npm"
    npm.write_text(FAKE_NPM, encoding="utf-8")
    npm.chmod(0o755)
    log, release = tmp_path / "npm.log", tmp_path / "release"
    lock = fake_web / ".test-deps.lock"
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
           "NPM_LOG": str(log), "NPM_RELEASE": str(release)}
    workers = [
        subprocess.Popen([sys.executable, "-c", WORKER, str(fake_web)],
                         cwd=TESTS_DIR.parent, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(2)
    ]
    try:
        _until(lambda: log.exists() and (len(log.read_text().split()) == 2
                                         or _blocked_on(lock)),
               "a second npm ci or a worker queued on the lock")
        release.touch()
        for worker in workers:
            output = worker.communicate(timeout=SETTLE)[0].decode()
            assert worker.returncode == 0, output
    finally:
        release.touch()
        for worker in workers:
            worker.kill()
    assert len(log.read_text().split()) == 1, "each worker ran its own npm ci"
    assert (fake_web / "node_modules" / ".package-lock.json").exists()


def test_every_node_resolver_bootstraps_the_deps():
    """The chokepoint property: a resolver that skips the bootstrap recreates
    the fresh-container failure this module exists to end. Discovered, not
    enumerated, so a reintroduced copy arrives already covered.

    The five module-local ``_node_binary`` copies this originally swept have
    since collapsed into one shared ``tests.conftest.node_binary``, so the
    discovery covers conftest's public spelling as well as the private one — a
    module that grows its own copy again is still caught, and the shared
    resolver, now the only path a node-using test takes, is checked by name
    below rather than merely happening to be in the set.
    """
    resolvers = []
    for path in sorted(TESTS_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"def (_?node_binary)\(\):\n(.*?)(?=\ndef |\nclass |\Z)",
                                 text, re.S):
            resolvers.append((path.name, match.group(1), match.group(2)))
    assert resolvers, "the resolver pattern moved — update this discovery"
    assert ("conftest.py", "node_binary") in [
        (name, func) for name, func, _ in resolvers
    ], "the shared resolver left conftest — update this discovery"
    for name, func, body in resolvers:
        assert "ensure_web_deps()" in body, (
            f"{name}'s {func} does not bootstrap web deps — a fresh "
            "container will fail its node tests instead of installing"
        )
