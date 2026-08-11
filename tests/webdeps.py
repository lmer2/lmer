"""Web test dependencies bootstrap themselves (one ``npm ci``, at the chokepoint).

The node-dependent tests skip when Node is absent — but every lmer session
container HAS Node (mise ships it), while ``web/node_modules`` is gitignored and
therefore absent from every fresh checkout. That half was unhandled: the tests
ran, and failed on missing packages (``@vue/compiler-sfc``, the bundle build),
and each fresh container's agent re-discovered ``npm ci`` the hard way during a
gate run. Two sessions paid that toll on the same day before this existed.

So the resolver every node-using test already goes through calls
:func:`ensure_web_deps` first: packages present and current → a stat and
nothing else; missing or stale against the lockfile → one loud ``npm ci``,
memoized for the process. The deliberate trade: a test that only pipes a
snippet through ``node -e`` (no packages) still triggers the install in a
fresh container — accepted, because the alternative is per-test knowledge of
which tests need packages, which is exactly the kind of distinction the next
test author gets wrong silently. ``npm`` absent while Node exists is reported
once and left to the test's own failure, which now names the real problem.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

#: How long one cold ``npm ci`` may take on a loaded 1-CPU container before the
#: bootstrap is the thing that broke: observed cold installs run ~50s.
INSTALL_TIMEOUT_SECONDS = 600

_done: dict = {"checked": False}


def _stale() -> bool:
    """True when node_modules is absent or older than the lockfile.

    ``npm ci`` writes ``node_modules/.package-lock.json``; comparing it to the
    repo lockfile catches a checkout whose lock moved under an old install
    (the branch-switch case), not just the fresh-container case.
    """
    lock = WEB_DIR / "package-lock.json"
    installed = WEB_DIR / "node_modules" / ".package-lock.json"
    if not installed.exists():
        return True
    if not lock.exists():
        return False
    return lock.stat().st_mtime > installed.stat().st_mtime


def ensure_web_deps() -> None:
    """Install web/node_modules if a node-using test would otherwise fail.

    Called from every ``_node_binary`` resolver, so no test file needs to know
    it exists. Never raises: a failed install is reported and left to the
    calling test, whose own failure output then carries the real cause instead
    of a mystery about missing packages.
    """
    if _done["checked"]:
        return
    _done["checked"] = True
    if not _stale():
        return
    npm = shutil.which("npm")
    if npm is None:
        print("webdeps: web/node_modules is missing and npm is not on PATH — "
              "node-dependent tests will fail; install Node+npm or run npm ci "
              "in web/ yourself")
        return
    print("webdeps: web/node_modules missing or stale — running npm ci once "
          "(first node-dependent test in a fresh container pays ~1 minute)")
    try:
        subprocess.run(
            [npm, "ci", "--no-audit", "--no-fund"],
            cwd=str(WEB_DIR),
            check=True,
            timeout=INSTALL_TIMEOUT_SECONDS,
            capture_output=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"webdeps: npm ci failed ({exc}) — node-dependent tests will "
              "fail; run npm ci in web/ yourself to see the full output")
