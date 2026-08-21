"""Work-repo commits defer while a gate is in flight (issue #201).

The collision this covers: a gate leaves the run dir dirty via its own receipt,
which arms the Stop hook, whose mandated `work commit` then sweeps tracked files
out from under the running suite and fails its /work isolation guard. Deferral
is the cut — at the single choke point every durability push goes through, not
just the one path that bit first.
"""
import ast
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from lmer_cli import gate_lock
from work_repo import git_ops
from work_repo.git_ops import commit_work_path

HOST = "git.example.com"
PROJECT = "group/project"
SLUG = "develop-issue-201"
RUN_REL = f"{HOST}/{PROJECT}/runs/{SLUG}"


@pytest.fixture
def work_repo(tmp_path, monkeypatch):
    """A work repo with a run dir holding one committed, then dirtied, file."""
    repo = tmp_path / "work"
    repo.mkdir()
    for cmd in (
        ["git", "init"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test User"],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)

    run_dir = repo / RUN_REL
    run_dir.mkdir(parents=True)
    (run_dir / "state.yaml").write_text("slug: develop-issue-201\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "seed"], cwd=repo, check=True, capture_output=True
    )
    # The dirt a real run carries when a gate is running: an appended receipt.
    (run_dir / "events.jsonl").write_text(
        json.dumps({"type": "gate", "note": "gate-check: pass"}) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(repo))
    monkeypatch.setenv("LMER_REPO_HOST", HOST)
    monkeypatch.setenv("LMER_REPO_PROJECT", PROJECT)
    monkeypatch.setenv("LMER_TASK", "develop")
    monkeypatch.setenv("LMER_TASK_TARGET", "https://git.example.com/group/project/-/issues/201")
    monkeypatch.setenv(gate_lock.LOCK_DIR_ENV, str(tmp_path / "locks"))
    monkeypatch.delenv(gate_lock.GUARD_ENV, raising=False)
    # No remote: the push half warns and fails, which is irrelevant here — every
    # assertion below is about whether a COMMIT was created.
    return repo


def _commits(repo):
    result = subprocess.run(
        ["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def _porcelain(repo):
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo, capture_output=True, text=True,
    )
    return sorted(line for line in result.stdout.splitlines() if line.strip())


def _hold_other_process_marker(monkeypatch, gate="gate-check"):
    """Pretend another process holds the marker (self-markers never defer)."""
    directory = gate_lock.lock_dir()
    directory.mkdir(parents=True, exist_ok=True)
    pid = os.getppid()
    (directory / f"{pid}.json").write_text(
        json.dumps({"pid": pid, "gate": gate, "started_at": time.time()}),
        encoding="utf-8",
    )
    return pid


def _events(repo):
    path = repo / RUN_REL / "events.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestDeferral:
    def test_commits_normally_when_no_gate_is_running(self, work_repo):
        before = len(_commits(work_repo))
        commit_work_path(RUN_REL, "run-state: test")
        assert len(_commits(work_repo)) == before + 1

    def test_defers_while_a_gate_is_in_flight(self, work_repo, monkeypatch, capsys):
        _hold_other_process_marker(monkeypatch)
        before = _commits(work_repo)
        dirty_before = _porcelain(work_repo)

        rc = commit_work_path(RUN_REL, "run-state: test")

        assert rc == 0, "a deferral is not a failure"
        assert _commits(work_repo) == before, "nothing may be committed mid-gate"
        assert _porcelain(work_repo) == dirty_before, "files stay exactly as they were"
        out = capsys.readouterr().out
        assert "deferred" in out.lower()
        assert "gate-check" in out
        assert "#201" in out

    def test_deferral_leaves_a_trace_for_the_next_commit_to_flush(
        self, work_repo, monkeypatch
    ):
        """Exit zero would otherwise tell the caller the work is safe when it
        is not — the trace is what makes the deferral visible to the reviewer."""
        _hold_other_process_marker(monkeypatch)
        commit_work_path(RUN_REL, "run-state: test")

        # Parked outside the work repo: writing it into the run dir now is the
        # very thing deferral exists to avoid.
        assert not any(e["type"] == "commit_deferred" for e in _events(work_repo))
        queue = gate_lock.lock_dir() / git_ops.DEFERRAL_QUEUE_NAME
        assert queue.exists()

        # Gate over: the next commit flushes the trace and carries it.
        (gate_lock.lock_dir() / f"{os.getppid()}.json").unlink()
        commit_work_path(RUN_REL, "run-state: test")

        deferred = [e for e in _events(work_repo) if e["type"] == "commit_deferred"]
        assert len(deferred) == 1
        assert deferred[0]["data"]["gate"] == "gate-check"
        assert RUN_REL in deferred[0]["data"]["paths"]
        assert not queue.exists()
        committed = subprocess.run(
            ["git", "show", "--name-only", "--format=", "HEAD"],
            cwd=work_repo, capture_output=True, text=True,
        ).stdout
        assert "events.jsonl" in committed, "the trace must land IN the commit"

    def test_flush_is_idempotent_across_repeated_commits(self, work_repo, monkeypatch):
        _hold_other_process_marker(monkeypatch)
        commit_work_path(RUN_REL, "run-state: test")
        (gate_lock.lock_dir() / f"{os.getppid()}.json").unlink()

        commit_work_path(RUN_REL, "run-state: test")
        (work_repo / RUN_REL / "log.yaml").write_text("- x\n", encoding="utf-8")
        commit_work_path(RUN_REL, "run-state: test")

        deferred = [e for e in _events(work_repo) if e["type"] == "commit_deferred"]
        assert len(deferred) == 1, "a flushed deferral must not replay"

    def test_clean_paths_defer_silently(self, work_repo, monkeypatch, capsys):
        """A no-op commit has nothing to defer, so it must not cry wolf."""
        commit_work_path(RUN_REL, "run-state: settle")  # make it clean
        capsys.readouterr()
        _hold_other_process_marker(monkeypatch)

        rc = commit_work_path(RUN_REL, "run-state: test")

        assert rc == 0
        assert "deferred" not in capsys.readouterr().out.lower()
        assert not (gate_lock.lock_dir() / git_ops.DEFERRAL_QUEUE_NAME).exists()

    def test_allow_during_gate_commits_anyway(self, work_repo, monkeypatch):
        """The session-end escape: the container is going away, so the gate
        whose window we would protect is being torn down with it."""
        _hold_other_process_marker(monkeypatch)
        before = len(_commits(work_repo))
        commit_work_path(RUN_REL, "run-state: session end", allow_during_gate=True)
        assert len(_commits(work_repo)) == before + 1

    def test_kill_switch_restores_the_old_behavior(self, work_repo, monkeypatch):
        _hold_other_process_marker(monkeypatch)
        monkeypatch.setenv(gate_lock.GUARD_ENV, "0")
        before = len(_commits(work_repo))
        commit_work_path(RUN_REL, "run-state: test")
        assert len(_commits(work_repo)) == before + 1

    def test_a_dead_gates_marker_does_not_defer(self, work_repo, monkeypatch):
        """Liveness is the OS's answer, not the marker's: a crashed gate must
        not wedge every later commit."""
        proc = subprocess.Popen(["true"])
        proc.wait()
        directory = gate_lock.lock_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{proc.pid}.json").write_text(
            json.dumps({"pid": proc.pid, "gate": "gate-check", "started_at": time.time()}),
            encoding="utf-8",
        )
        before = len(_commits(work_repo))
        commit_work_path(RUN_REL, "run-state: test")
        assert len(_commits(work_repo)) == before + 1

    def test_a_zombie_gates_marker_does_not_defer(self, work_repo, monkeypatch):
        """A gate is usually a child of the session that commits next, and an
        exited-but-unwaited-on child still answers kill(pid, 0). Reading that as
        live deferred every commit behind a finished gate — exit zero, so
        nobody noticed until the run dir was still dirty at session end (#261)."""
        proc = subprocess.Popen(["true"])
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not gate_lock._is_zombie(proc.pid):
                time.sleep(0.02)
            assert gate_lock._is_zombie(proc.pid) is True, "expected an unreaped zombie"

            directory = gate_lock.lock_dir()
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"{proc.pid}.json").write_text(
                json.dumps(
                    {"pid": proc.pid, "gate": "gate-check", "started_at": time.time()}
                ),
                encoding="utf-8",
            )
            before = len(_commits(work_repo))
            commit_work_path(RUN_REL, "run-state: test")
            assert len(_commits(work_repo)) == before + 1
        finally:
            proc.wait()

    def test_own_marker_does_not_defer_the_holder(self, work_repo, monkeypatch):
        """`work verify` holds a marker while its own bookkeeping runs."""
        directory = gate_lock.lock_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{os.getpid()}.json").write_text(
            json.dumps({"pid": os.getpid(), "gate": "work verify t1", "started_at": time.time()}),
            encoding="utf-8",
        )
        before = len(_commits(work_repo))
        commit_work_path(RUN_REL, "run-state: test")
        assert len(_commits(work_repo)) == before + 1


class TestExemptionsAreDeclaredAtTheirCallSites:
    """The two `allow_during_gate` callers, asserted at the source so a future
    edit cannot quietly drop them (or quietly add a third)."""

    def test_session_end_is_exempt(self):
        source = (Path(__file__).parent.parent / "src" / "work_repo" / "cli.py").read_text(
            encoding="utf-8"
        )
        assert 'f"run-state: session end {state[\'slug\']}",\n            allow_during_gate=True,' in source

    def test_only_session_end_is_exempt(self):
        source = (Path(__file__).parent.parent / "src" / "work_repo" / "cli.py").read_text(
            encoding="utf-8"
        )
        assert source.count("allow_during_gate=True") == 1

    def test_release_cas_never_routes_through_commit_work_path(self):
        """The claim path is arbitration, not durability — it commits with its
        own git calls, so it is exempt structurally rather than by flag."""
        source = (Path(__file__).parent.parent / "src" / "work_repo" / "cli.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        claim_write = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_commit_claim_write"
        )
        called = {
            node.func.id
            for node in ast.walk(claim_write)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "commit_work_path" not in called


class TestVerifyHoldsTheMarker:
    def test_cmd_verify_wraps_the_command(self):
        source = (Path(__file__).parent.parent / "src" / "work_repo" / "cli.py").read_text(
            encoding="utf-8"
        )
        assert "with gate_lock.hold_gate_lock(f\"work verify" in source


class TestResumeExposesTheFlag:
    """The Stop hook reads `gate_in_flight` off `work resume --json` rather
    than the marker dir — hooks import no project code (issue #100's pattern)."""

    def _decide(self, capsys):
        from work_repo import cli

        cli.cmd_resume(as_json=True)
        return json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    def test_absent_when_idle(self, work_repo, capsys):
        assert "gate_in_flight" not in self._decide(capsys)

    def test_present_and_described_when_a_gate_runs(self, work_repo, monkeypatch, capsys):
        _hold_other_process_marker(monkeypatch, gate="gate-push")
        decision = self._decide(capsys)
        assert decision["gate_in_flight"] is True
        assert "gate-push" in decision["gate_in_flight_detail"]
