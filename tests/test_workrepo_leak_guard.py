"""The /work leak guard's verdict and its diagnosis (issues #93, #201).

The guard itself is a session fixture in conftest.py; the part a person reads at
2am is the message, so that is extracted as a pure function and tested here. Two
causes produce the same porcelain drift and they need opposite responses: a test
that leaked into the operational work repo (#93 — fix the test's env isolation),
and a concurrent writer that committed underneath a running suite (#201 — fix
the deferral). The message used to name only the first.
"""
import os
from pathlib import Path

import pytest

from lmer_cli import gate_lock
from tests.conftest import _HARNESS_ENV, strip_lmer_env, work_repo_drift_report

WORK = "/work"
APPEARED = [" M git.example.com/group/project/runs/run-1/log.yaml"]
VANISHED = [" M git.example.com/group/project/runs/run-2/log.yaml"]
HEAD_A = "1111111111111111111111111111111111111111"
HEAD_B = "2222222222222222222222222222222222222222"


class TestVerdict:
    def test_no_drift_is_no_report(self):
        assert work_repo_drift_report(WORK, [], [], HEAD_A, HEAD_A) is None

    @pytest.mark.parametrize(
        "appeared,vanished",
        [(APPEARED, []), ([], VANISHED), (APPEARED, VANISHED)],
    )
    def test_any_drift_still_fails(self, appeared, vanished):
        """Still a failure, never a warning: once the work repo moved under a
        run, the fixture state the assertions were written against changed
        mid-flight, so a green from that run is not evidence of anything."""
        report = work_repo_drift_report(WORK, appeared, vanished, HEAD_A, HEAD_B)
        assert report is not None
        assert "leaked into or altered the operational work repo" in report

    def test_names_the_entries_on_both_sides(self):
        report = work_repo_drift_report(WORK, APPEARED, VANISHED, HEAD_A, HEAD_A)
        assert "appeared:" in report
        assert "vanished (deleted, or swept into a commit):" in report
        assert APPEARED[0] in report
        assert VANISHED[0] in report

    def test_keeps_the_env_isolation_pointer(self):
        """#93 is still a real cause — it just stops being the only one."""
        report = work_repo_drift_report(WORK, APPEARED, [], HEAD_A, HEAD_A)
        assert "issue #93" in report
        assert "LMER_* env" in report


class TestConcurrentWriterDiagnosis:
    def _moved(self):
        return work_repo_drift_report(WORK, [], VANISHED, HEAD_A, HEAD_B)

    def test_head_move_is_reported_as_the_concurrent_writer(self):
        report = self._moved()
        assert "HEAD MOVED" in report
        assert HEAD_A[:8] in report and HEAD_B[:8] in report
        assert "concurrent writer" in report.lower()

    def test_names_issue_201_and_the_commands_that_cause_it(self):
        report = self._moved()
        assert "#201" in report
        assert "work commit" in report
        assert "work state set" in report

    def test_says_plainly_that_the_deferral_broke(self):
        """With deferral shipped this cannot happen, so seeing it is the
        regression signal for the deferral itself — not a test to go fix."""
        report = self._moved()
        assert "THE DEFERRAL BROKE" in report
        assert "commit_work_path" in report
        assert "Chase that, not the tests." in report

    def test_the_diagnosis_leads_the_env_pointer(self):
        """The old message sent readers to env isolation first, which is the
        wrong end for this cause and cost two runs twenty minutes."""
        report = self._moved()
        assert report.index("HEAD MOVED") < report.index("issue #93")

    def test_unmoved_head_gets_the_bare_write_note_instead(self):
        report = work_repo_drift_report(WORK, APPEARED, [], HEAD_A, HEAD_A)
        assert "HEAD did not move" in report
        assert "THE DEFERRAL BROKE" not in report
        assert "`work log`" in report

    def test_vanished_only_without_a_head_move_stays_quiet_about_both(self):
        report = work_repo_drift_report(WORK, [], VANISHED, HEAD_A, HEAD_A)
        assert "HEAD MOVED" not in report
        assert "HEAD did not move" not in report

    @pytest.mark.parametrize(
        "before,after",
        [(None, HEAD_B), (HEAD_A, None), (None, None)],
    )
    def test_unreadable_head_simply_drops_the_extra_diagnosis(self, before, after):
        report = work_repo_drift_report(WORK, [], VANISHED, before, after)
        assert report is not None
        assert "HEAD MOVED" not in report


class TestGateLockIsolation:
    """`gate-check` holds a marker while the suite runs inside it, so without
    isolation every test touching a commit path would defer instead of
    committing — i.e. the suite could only pass when nobody was gating it."""

    def test_the_suite_runs_against_an_isolated_lock_dir(self):
        value = os.environ.get(gate_lock.LOCK_DIR_ENV, "")
        assert value, "the session fixture must point the lock dir somewhere safe"
        assert value != "/tmp/lmer-gate-inflight"

    def test_the_module_default_is_redirected_too(self, monkeypatch):
        """The env var alone is not enough: a test isolating its environment
        with `clear=True` drops it and lands on the module default, where a
        live gate-check marker would defer the commit path under test."""
        assert gate_lock.DEFAULT_LOCK_DIR != "/tmp/lmer-gate-inflight"
        monkeypatch.delenv(gate_lock.LOCK_DIR_ENV, raising=False)
        assert Path(gate_lock.DEFAULT_LOCK_DIR) == gate_lock.lock_dir()
        assert gate_lock.active_gate() is None

    def test_no_gate_is_visible_from_inside_the_isolated_dir(self):
        assert gate_lock.active_gate() is None

    def test_strip_lmer_env_leaves_the_lock_dir_alone(self, monkeypatch):
        """Stripping it would send an env-isolating module back to the
        operational lock dir, where the live gate would defer its commits."""
        assert gate_lock.LOCK_DIR_ENV in _HARNESS_ENV
        before = os.environ[gate_lock.LOCK_DIR_ENV]
        strip_lmer_env(monkeypatch)
        assert os.environ.get(gate_lock.LOCK_DIR_ENV) == before
        assert "LMER_REPO_HOST" not in os.environ  # the rest really is stripped
