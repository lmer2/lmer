"""Tests for the session-scoped chrome-agent teardown (issue #410).

The sweep exists because a forgotten browser is a few hundred MiB against a
2 GiB cgroup. What the tests are mostly about, though, is the other side of
that: it must stop ONLY what this session launched.

Two mechanisms carry that, and both are exercised here rather than asserted
about:

* **Scope.** captivus's registry is container-global and its instance names
  come from the working-directory basename, so it cannot say who owns what.
  The sweep reads a ledger the session wrote itself, keyed by the session's own
  harness process — ancestry over our processes, which are still children,
  rather than over Chrome, which is reparented to init.
* **Identity.** captivus REUSES names: it hands out the lowest free
  ``<dir>-NN`` and ``stop`` frees it again. A name a session recorded can later
  belong to somebody else's browser, so the ledger records ``name pid
  starttime`` and the sweep re-confirms all three before it stops anything.

Anything unconfirmable is left alone: these tests pin that direction, because
a leaked browser is bounded and visible while a browser stopped out from under
another session is neither.

**These tests establish their own preconditions.** An earlier version derived
the session key by running the helper as a child of pytest, which meant it only
passed where pytest happened to have a harness ancestor — green locally, 13
failures on a bare CI runner. Everything below runs under a fake harness
process the test creates, so the ancestry logic is genuinely exercised and the
result does not depend on who invoked the suite.
"""
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SWEEP = REPO_ROOT / "hooks" / "chrome_agent_sweep.sh"
LAUNCH = REPO_ROOT / "bin" / "chrome-agent-launch"
HELPER = REPO_ROOT / "libexec" / "chrome-agent-session.sh"

FAKE_CLI = """#!/bin/bash
echo "$@" >> "$CHROME_AGENT_CALLS"
[ "$1" = "stop" ] && echo "Stopped $2"
[ "$1" = "launch" ] && cat "$CHROME_AGENT_LAUNCH_JSON"
exit 0
"""

REAPING_CLI = """#!/bin/bash
echo "$@" >> "$CHROME_AGENT_CALLS"
[ "$1" = "stop" ] && echo "$2 was already dead, cleaned up"
exit 0
"""

HANGING_CLI = """#!/bin/bash
echo "$@" >> "$CHROME_AGENT_CALLS"
[ "$1" = "stop" ] && sleep 600
exit 0
"""


def _starttime(pid: int) -> str:
    """Field 22 of /proc/<pid>/stat, parsed past the parenthesised comm."""
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    return raw[raw.rindex(") ") + 2 :].split()[19]


@pytest.fixture
def live_pid():
    """A real process to pin an instance to, so identity checks can pass."""
    proc = subprocess.Popen(["sleep", "300"])
    try:
        yield proc.pid
    finally:
        proc.kill()
        proc.wait()


class Session:
    """A fake harness process tree: what makes these tests hermetic.

    Commands run as children of a process whose `comm` is `claude`, so
    `chrome_agent_session_key` resolves the same way it does in a real session
    — and two `Session`s are two different sessions, which is the property the
    cross-session tests need.
    """

    def __init__(self, tmp_path: Path, name: str):
        self.root = tmp_path / name
        self.root.mkdir(parents=True, exist_ok=True)
        self.harness = self.root / "claude"
        shutil.copy(shutil.which("bash"), self.harness)
        self.calls = self.root / "calls.txt"
        self.calls.touch()
        self.bindir = self.root / "bin"
        self.bindir.mkdir(exist_ok=True)
        self.launch_json = self.root / "launch.json"
        self.launch_json.write_text(
            json.dumps({"name": "workspace-01", "port": 9222, "pid": 4242}) + "\n",
            encoding="utf-8",
        )
        self._env: dict = {}

    def with_cli(self, cli: str | None) -> "Session":
        if cli is not None:
            fake = self.bindir / "chrome-agent"
            fake.write_text(cli, encoding="utf-8")
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        return self

    def env(self, ledger_dir: Path, registry: Path, **extra) -> dict:
        return {
            **os.environ,
            "PATH": f"{self.bindir}:{os.environ['PATH']}",
            "CHROME_AGENT_CALLS": str(self.calls),
            "CHROME_AGENT_LAUNCH_JSON": str(self.launch_json),
            "LMER_CHROME_AGENT_LEDGER_DIR": str(ledger_dir),
            "LMER_CHROME_AGENT_REGISTRY": str(registry),
            **self._env,
            **extra,
        }

    def run(self, script: str, env: dict, stdin: str = "", timeout: int = 120):
        """Run one script inside ONE harness process.

        Every operation a session performs must go through a single call:
        each invocation starts a NEW `claude` process, and the session key is
        that process's identity, so plant-here-sweep-there would silently use
        two different keys. (It did, in the first draft — some tests then
        passed only by pid coincidence.)

        `; true` at the end defeats bash's last-command exec optimisation.
        Without it the harness process is REPLACED by the script and the
        ancestry walk finds whatever invoked pytest instead, which is the way
        these tests pass for the wrong reason.
        """
        return subprocess.run(
            [str(self.harness), "-c", f"{script}\ntrue"],
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )

    @staticmethod
    def plant_part(lines: list[str]) -> str:
        body = "".join(f"{line}\n" for line in lines)
        return (
            f'. "{HELPER}"\n'
            'L="$(chrome_agent_ledger_path)"\n'
            '[ -n "$L" ] || { echo "NO_KEY" >&2; exit 3; }\n'
            'mkdir -p "$(dirname "$L")"\n'
            f"cat > \"$L\" <<'LEDGER'\n{body}LEDGER"
        )

    @staticmethod
    def sweep_part(payload: str | None = None) -> str:
        if payload is None:
            return f'bash "{SWEEP}" </dev/null'
        return f'bash "{SWEEP}" <<\'PAYLOAD\'\n{payload}\nPAYLOAD'

    @staticmethod
    def launch_part() -> str:
        # The harness script ends in `true`, so its exit code says nothing
        # about the wrapper. Report the wrapper's own status instead.
        return f'bash "{LAUNCH}" --headless\necho "LAUNCH_EXIT=$?"'

    def do(self, ledger_dir: Path, registry: Path, *parts: str, timeout: int = 120):
        """Run the given parts as one session, and return (proc, calls seen)."""
        proc = self.run(
            "\n".join(parts), self.env(ledger_dir, registry), timeout=timeout
        )
        return proc, self.calls_seen()

    def key(self, ledger_dir: Path, registry: Path) -> str:
        script = f'. "{HELPER}"\nchrome_agent_session_key'
        return self.run(script, self.env(ledger_dir, registry)).stdout.strip()

    def calls_seen(self) -> list[str]:
        return [l for l in self.calls.read_text(encoding="utf-8").splitlines() if l]


@pytest.fixture
def paths(tmp_path):
    ledger_dir = tmp_path / "ledgers"
    registry = tmp_path / "registry.json"
    registry.write_text("{}", encoding="utf-8")
    return ledger_dir, registry


def _registry(registry: Path, entries: dict) -> None:
    registry.write_text(
        json.dumps({n: {"port": 9222, "pid": p} for n, p in entries.items()}),
        encoding="utf-8",
    )


class TestSessionKeyIsSelfEstablished:
    def test_a_session_resolves_a_key(self, tmp_path, paths):
        """The precondition the old suite borrowed from its caller."""
        assert Session(tmp_path, "a").key(*paths)

    def test_two_sessions_get_different_keys(self, tmp_path, paths):
        """The cross-session property, at its root."""
        a = Session(tmp_path, "a").key(*paths)
        b = Session(tmp_path, "b").key(*paths)
        assert a and b and a != b


class TestIdentityNotJustName:
    """captivus reuses instance names; the ledger must pin a process."""

    def test_a_recycled_name_is_not_stopped(self, tmp_path, paths, live_pid):
        """The !289 iteration-2 finding, end to end.

        Session A records `workspace-01` and stops it promptly — which is what
        the skill instructs, and what frees the name. Session B is then handed
        the same name. A's SessionEnd must not touch B's browser.
        """
        ledger_dir, registry = paths
        a = Session(tmp_path, "a").with_cli(FAKE_CLI)
        stale_pid = live_pid + 1_000_000  # a pid that is not the live one
        _registry(registry, {"workspace-01": live_pid})  # B's browser now

        proc, seen = a.do(
            ledger_dir,
            registry,
            a.plant_part([f"workspace-01 {stale_pid} 12345"]),
            a.sweep_part(),
        )

        assert proc.returncode == 0
        assert not [c for c in seen if c.startswith("stop")], (
            f"A stopped a browser it no longer owned: {seen}"
        )
        assert "stopped" not in proc.stdout

    def test_a_recycled_pid_is_not_stopped(self, tmp_path, paths, live_pid):
        """Same name, same pid — but the pid has been reused since."""
        ledger_dir, registry = paths
        a = Session(tmp_path, "a").with_cli(FAKE_CLI)
        wrong_start = str(int(_starttime(live_pid)) + 1)
        _registry(registry, {"workspace-01": live_pid})

        proc, seen = a.do(
            ledger_dir,
            registry,
            a.plant_part([f"workspace-01 {live_pid} {wrong_start}"]),
            a.sweep_part(),
        )

        assert proc.returncode == 0
        assert not [c for c in seen if c.startswith("stop")]

    def test_a_confirmed_instance_is_stopped(self, tmp_path, paths, live_pid):
        ledger_dir, registry = paths
        a = Session(tmp_path, "a").with_cli(FAKE_CLI)
        _registry(registry, {"workspace-01": live_pid})

        proc, seen = a.do(
            ledger_dir,
            registry,
            a.plant_part([f"workspace-01 {live_pid} {_starttime(live_pid)}"]),
            a.sweep_part(),
        )

        assert proc.returncode == 0
        assert "stop workspace-01" in seen
        assert "stopped 1" in proc.stdout

    def test_a_vanished_registry_entry_is_not_stopped(self, tmp_path, paths, live_pid):
        """Nothing to confirm against means nothing to stop."""
        ledger_dir, registry = paths
        a = Session(tmp_path, "a").with_cli(FAKE_CLI)
        _registry(registry, {})

        proc, seen = a.do(
            ledger_dir,
            registry,
            a.plant_part([f"workspace-01 {live_pid} {_starttime(live_pid)}"]),
            a.sweep_part(),
        )

        assert proc.returncode == 0
        assert not [c for c in seen if c.startswith("stop")]


class TestSessionScoping:
    def test_an_unrecorded_instance_is_never_stopped(self, tmp_path, paths, live_pid):
        """A bare `chrome-agent launch` is not this session's to stop."""
        ledger_dir, registry = paths
        _registry(registry, {"workspace-01": live_pid})
        a = Session(tmp_path, "a").with_cli(FAKE_CLI)

        proc, seen = a.do(ledger_dir, registry, a.sweep_part())

        assert proc.returncode == 0
        assert not [c for c in seen if c.startswith("stop")]

    def test_one_session_does_not_read_anothers_ledger(
        self, tmp_path, paths, live_pid
    ):
        ledger_dir, registry = paths
        _registry(registry, {"workspace-01": live_pid})
        owner = Session(tmp_path, "owner").with_cli(FAKE_CLI)
        owner.do(
            ledger_dir,
            registry,
            owner.plant_part([f"workspace-01 {live_pid} {_starttime(live_pid)}"]),
        )
        other = Session(tmp_path, "other").with_cli(FAKE_CLI)

        proc, seen = other.do(ledger_dir, registry, other.sweep_part())

        assert proc.returncode == 0
        assert not [c for c in seen if c.startswith("stop")]
        assert len(list(ledger_dir.glob("*.instances"))) == 1, (
            "another session's ledger was removed"
        )

    def test_launch_records_name_pid_and_starttime(self, tmp_path, paths, live_pid):
        ledger_dir, registry = paths
        a = Session(tmp_path, "a").with_cli(FAKE_CLI)
        a.launch_json.write_text(
            json.dumps({"name": "workspace-01", "port": 9222, "pid": live_pid}),
            encoding="utf-8",
        )
        _registry(registry, {"workspace-01": live_pid})

        proc, seen = a.do(ledger_dir, registry, a.launch_part())

        assert proc.returncode == 0, proc.stderr
        assert "launch --headless" in seen
        assert '"name": "workspace-01"' in proc.stdout, (
            "the wrapper must pass captivus's output through unchanged"
        )
        ledgers = list(ledger_dir.glob("*.instances"))
        assert len(ledgers) == 1
        assert ledgers[0].read_text(encoding="utf-8").split() == [
            "workspace-01",
            str(live_pid),
            _starttime(live_pid),
        ]

    def test_launch_then_sweep_is_a_round_trip(self, tmp_path, paths, live_pid):
        ledger_dir, registry = paths
        a = Session(tmp_path, "a").with_cli(FAKE_CLI)
        a.launch_json.write_text(
            json.dumps({"name": "workspace-01", "port": 9222, "pid": live_pid}),
            encoding="utf-8",
        )
        _registry(registry, {"workspace-01": live_pid})

        proc, seen = a.do(
            ledger_dir, registry, a.launch_part(), a.sweep_part()
        )

        assert "stop workspace-01" in seen
        assert "stopped 1" in proc.stdout

    def test_launch_records_nothing_when_the_instance_cannot_be_pinned(
        self, tmp_path, paths
    ):
        """No registry entry means no identity, so nothing is recorded."""
        ledger_dir, registry = paths
        a = Session(tmp_path, "a").with_cli(FAKE_CLI)
        _registry(registry, {})

        proc, _ = a.do(ledger_dir, registry, a.launch_part())

        assert proc.returncode == 0
        assert "could not pin" in proc.stderr
        assert not list(ledger_dir.glob("*.instances"))

    def test_launch_records_nothing_when_the_cli_fails(self, tmp_path, paths):
        ledger_dir, registry = paths
        failing = '#!/bin/bash\necho "$@" >> "$CHROME_AGENT_CALLS"\nexit 1\n'
        a = Session(tmp_path, "a").with_cli(failing)

        proc, _ = a.do(ledger_dir, registry, a.launch_part())

        assert "LAUNCH_EXIT=1" in proc.stdout, (
            "the wrapper must propagate captivus's exit code"
        )
        assert not list(ledger_dir.glob("*.instances"))


class TestSessionEndReason:
    """`clear` and `resume` fire while the session carries on."""

    @pytest.mark.parametrize("reason", ["clear", "resume"])
    def test_a_continuing_session_is_not_swept(
        self, tmp_path, paths, live_pid, reason
    ):
        ledger_dir, registry = paths
        a = Session(tmp_path, "a").with_cli(FAKE_CLI)
        _registry(registry, {"workspace-01": live_pid})
        payload = json.dumps({"hook_event_name": "SessionEnd", "reason": reason})

        proc, seen = a.do(
            ledger_dir,
            registry,
            a.plant_part([f"workspace-01 {live_pid} {_starttime(live_pid)}"]),
            a.sweep_part(payload),
        )

        assert proc.returncode == 0
        assert not [c for c in seen if c.startswith("stop")], (
            f"reason={reason} means the session continues; its browser is in use"
        )
        assert list(ledger_dir.glob("*.instances")), (
            "the ledger must survive a session that continues"
        )

    @pytest.mark.parametrize("reason", ["logout", "prompt_input_exit", "other"])
    def test_a_terminal_reason_is_swept(self, tmp_path, paths, live_pid, reason):
        ledger_dir, registry = paths
        a = Session(tmp_path, "a").with_cli(FAKE_CLI)
        _registry(registry, {"workspace-01": live_pid})
        payload = json.dumps({"hook_event_name": "SessionEnd", "reason": reason})

        proc, seen = a.do(
            ledger_dir,
            registry,
            a.plant_part([f"workspace-01 {live_pid} {_starttime(live_pid)}"]),
            a.sweep_part(payload),
        )

        assert proc.returncode == 0
        assert "stop workspace-01" in seen


class TestNeverFailsSessionEnd:
    def test_a_quiet_session_stays_quiet(self, tmp_path, paths):
        a = Session(tmp_path, "a").with_cli(FAKE_CLI)
        proc, seen = a.do(*paths, a.sweep_part())

        assert proc.returncode == 0
        assert proc.stdout.strip() == ""
        assert seen == []

    def test_absent_cli_is_a_no_op(self, tmp_path, paths, live_pid):
        ledger_dir, registry = paths
        a = Session(tmp_path, "a").with_cli(None)
        _registry(registry, {"workspace-01": live_pid})
        env = {
            **a.env(ledger_dir, registry),
            "PATH": f"{a.bindir}:/usr/bin:/bin",
        }
        assert (
            subprocess.run(
                ["bash", "-c", "command -v chrome-agent"], capture_output=True, env=env
            ).returncode
            != 0
        )

        proc = a.run(
            "\n".join(
                [
                    a.plant_part(
                        [f"workspace-01 {live_pid} {_starttime(live_pid)}"]
                    ),
                    a.sweep_part(),
                ]
            ),
            env,
        )

        assert proc.returncode == 0

    def test_a_reaped_dead_instance_is_not_counted_as_stopped(
        self, tmp_path, paths, live_pid
    ):
        ledger_dir, registry = paths
        a = Session(tmp_path, "a").with_cli(REAPING_CLI)
        _registry(registry, {"workspace-01": live_pid})

        proc, seen = a.do(
            ledger_dir,
            registry,
            a.plant_part([f"workspace-01 {live_pid} {_starttime(live_pid)}"]),
            a.sweep_part(),
        )

        assert proc.returncode == 0
        assert "stop workspace-01" in seen
        assert "stopped" not in proc.stdout

    def test_a_hanging_stop_is_bounded(self, tmp_path, paths, live_pid):
        """`|| true` in the hook wiring bounds the exit code, not the clock."""
        ledger_dir, registry = paths
        a = Session(tmp_path, "a").with_cli(HANGING_CLI)
        _registry(registry, {"workspace-01": live_pid})

        proc, _ = a.do(
            ledger_dir,
            registry,
            a.plant_part([f"workspace-01 {live_pid} {_starttime(live_pid)}"]),
            a.sweep_part(),
            timeout=180,
        )

        assert proc.returncode == 0

    def test_a_junk_ledger_line_never_reaches_a_command(
        self, tmp_path, paths, live_pid
    ):
        ledger_dir, registry = paths
        a = Session(tmp_path, "a").with_cli(FAKE_CLI)
        _registry(registry, {"workspace-01": live_pid})

        proc, seen = a.do(
            ledger_dir,
            registry,
            a.plant_part(
                [
                    f"workspace-01 {live_pid} {_starttime(live_pid)}",
                    "; rm -rf / 1 1",
                    "$(whoami) 1 1",
                    "",
                ]
            ),
            a.sweep_part(),
        )

        assert proc.returncode == 0
        assert [c for c in seen if c.startswith("stop")] == ["stop workspace-01"]


def test_scripts_are_executable():
    for script in (SWEEP, LAUNCH):
        assert script.stat().st_mode & stat.S_IXUSR, script
