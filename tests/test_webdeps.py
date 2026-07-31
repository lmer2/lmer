"""The web-deps bootstrap: fresh containers stop failing node tests (T125).

Two live sessions on one day each burned a gate run discovering that Node
exists (mise) while ``web/node_modules`` does not (gitignored) — the skip
logic only covered Node's absence. These tests pin the bootstrap that closed
that: staleness detection, the one-shot memo, failure containment, and the
meta-guard that every ``_node_binary`` resolver actually calls it.
"""
import re
import subprocess
from pathlib import Path

import pytest

from tests import webdeps

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
            import os

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


def test_npm_absent_is_one_message_not_an_error(fake_web, monkeypatch, capsys):
    _plant(fake_web, installed=False)
    monkeypatch.setattr(webdeps.shutil, "which", lambda name: None)

    webdeps.ensure_web_deps()

    assert "npm is not on PATH" in capsys.readouterr().out


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
