"""The /work leak guard's verdict and its diagnosis (issues #93, #201).

The guard itself is a session fixture in conftest.py; the part a person reads at
2am is the message, so that is extracted as a pure function and tested here. Two
causes produce the same porcelain drift and they need opposite responses: a test
that leaked into the operational work repo (#93 — fix the test's env isolation),
and a concurrent writer that committed underneath a running suite (#201 — fix
the deferral). The message used to name only the first.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lmer_cli import gate_lock
from work_repo import write_journal
from tests.conftest import (
    _HARNESS_ENV,
    partition_attributed_drift,
    porcelain_entry_path,
    strip_lmer_env,
    work_repo_blame_report,
    work_repo_drift_report,
)

pytest_plugins = "pytester"

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


class TestSuiteHeldMarker:
    """The suite holds its own gate marker in the OPERATIONAL lock dir
    (issue #233), so work-repo durability commits defer during bare `pytest`
    runs exactly as they do inside gate commands — outside processes read
    that dir, not the redirected one this suite runs against."""

    def test_the_marker_names_this_pytest_process(self, _isolate_gate_lock_dir):
        marker_path = Path(_isolate_gate_lock_dir) / f"{os.getpid()}.json"
        assert marker_path.exists(), (
            "the gate-lock isolation fixture must hold a pytest-suite marker "
            "in the operational dir it captured before redirecting"
        )
        marker = json.loads(marker_path.read_text())
        assert marker["pid"] == os.getpid()
        assert marker["gate"] == "pytest-suite"

    def test_outside_processes_see_a_live_gate(self, _isolate_gate_lock_dir):
        """The consumer's-eye view: a fresh process pointed at the
        operational dir (as any session-spawned `work` command is) must see
        an active gate while this suite runs."""
        code = (
            "import sys\n"
            "from lmer_cli import gate_lock\n"
            "gates = [m.get('gate') for m in gate_lock.read_markers(prune=False)]\n"
            "sys.exit(0 if 'pytest-suite' in gates else 1)\n"
        )
        env = dict(os.environ)
        env[gate_lock.LOCK_DIR_ENV] = str(_isolate_gate_lock_dir)
        result = subprocess.run(
            [sys.executable, "-c", code], env=env, timeout=60
        )
        assert result.returncode == 0

    def test_the_suite_itself_does_not_see_its_own_marker(self):
        """Inside the redirect nothing changed: tests exercising commit paths
        must not defer on the marker the suite holds for outsiders."""
        assert gate_lock.active_gate() is None


class TestPorcelainEntryPath:
    @pytest.mark.parametrize(
        "line,expected",
        [
            (" M host/proj/runs/r1/log.yaml", "host/proj/runs/r1/log.yaml"),
            ("?? host/proj/runs/r1/spec.md", "host/proj/runs/r1/spec.md"),
            ("A  staged.txt", "staged.txt"),
        ],
    )
    def test_reads_ordinary_entries(self, line, expected):
        assert porcelain_entry_path(line) == expected

    @pytest.mark.parametrize(
        "line",
        [
            "",
            " M",
            "R  old.txt -> new.txt",
            '?? "unterminated',
        ],
    )
    def test_unreadable_entries_are_none(self, line):
        """None routes the entry to 'unattributed', which FAILS the run —
        a parser that cannot read a path must not excuse it. (Ordinary
        quoted paths are decoded rather than refused — see
        TestPorcelainQuoting.)"""
        assert porcelain_entry_path(line) is None


class TestPartitionAttributedDrift:
    PREFIX = "host/proj/runs/r1"

    def test_entries_under_a_prefix_are_attributed(self):
        appeared = [f" M {self.PREFIX}/log.yaml", f"?? {self.PREFIX}/spec.md"]
        attributed, residual_appeared, residual_vanished = (
            partition_attributed_drift(appeared, [], [[self.PREFIX]])
        )
        assert attributed == appeared
        assert residual_appeared == [] and residual_vanished == []

    def test_exact_prefix_match_counts(self):
        attributed, residual, _ = partition_attributed_drift(
            [f"?? {self.PREFIX}"], [], [[self.PREFIX]]
        )
        assert attributed and not residual

    def test_sibling_paths_stay_residual(self):
        """Prefix means path-segment prefix: runs/r10 is NOT under runs/r1."""
        appeared = [" M host/proj/runs/r10/log.yaml"]
        attributed, residual, _ = partition_attributed_drift(
            appeared, [], [[self.PREFIX]]
        )
        assert not attributed
        assert residual == appeared

    def test_a_vanished_entry_with_no_rename_counterpart_keeps_failing(self):
        """A test deleting an untracked run-dir file: its status line stops
        being reported and nothing lands under another declared prefix, so
        it is never excused (MR !200 review round 2)."""
        vanished = [f"?? {self.PREFIX}/state.yml"]
        attributed, _, residual_vanished = partition_attributed_drift(
            [], vanished, [[self.PREFIX]]
        )
        assert attributed == []
        assert residual_vanished == vanished

    def test_a_deletion_arrives_as_an_appeared_entry_and_keeps_failing(self):
        """Deleting a TRACKED file adds a ` D path` line — an *appeared*
        entry. Keying the rule on the diff side let it ride the prefix
        excuse; the status code is what says "removed"."""
        appeared = [
            f" D {self.PREFIX}/events.jsonl",
            f"?? {self.PREFIX}/log.yaml",
        ]
        attributed, residual_appeared, _ = partition_attributed_drift(
            appeared, [], [[self.PREFIX]]
        )
        assert attributed == [f"?? {self.PREFIX}/log.yaml"]
        assert residual_appeared == [f" D {self.PREFIX}/events.jsonl"]

    @pytest.mark.parametrize("code", [" D", "D ", "AD", "MD"])
    def test_every_delete_status_is_caught(self, code):
        appeared = [f"{code} {self.PREFIX}/events.jsonl"]
        attributed, residual_appeared, _ = partition_attributed_drift(
            appeared, [], [[self.PREFIX]]
        )
        assert not attributed
        assert residual_appeared == appeared

    def test_no_scopes_attributes_nothing(self):
        appeared = [f" M {self.PREFIX}/log.yaml"]
        attributed, residual, _ = partition_attributed_drift(appeared, [], [])
        assert not attributed
        assert residual == appeared

    def test_undecodable_entries_stay_residual(self):
        appeared = [f'?? "{self.PREFIX}/bad\\zescape.md"']
        attributed, residual, _ = partition_attributed_drift(
            appeared, [], [[self.PREFIX]]
        )
        assert not attributed
        assert residual == appeared


class TestSessionRunDirRename:
    """`work goals freeze` and the lazy rename behind `work name` move the
    whole run dir mid-suite. The journal declares BOTH addresses, so the
    vacated half has a counterpart landing under the other one — that is what
    separates it from a test's rmtree (MR !200 review round 2, major).

    The lines below are the real porcelain of a run-dir rename, measured on a
    scratch repo rather than imagined: a tracked-file rename shows up as ` D`
    at the old address plus `??` at the new one, and the old address's former
    entries vanish.
    """

    OLD = "host/proj/runs/develop-233"
    NEW = "host/proj/runs/develop-233--named"
    PREFIXES = [NEW, OLD]

    APPEARED = [
        f" D {OLD}/events.jsonl",
        f" D {OLD}/state.yaml",
        f"?? {NEW}/events.jsonl",
        f"?? {NEW}/spec.md",
        f"?? {NEW}/state.yaml",
    ]
    VANISHED = [f" M {OLD}/state.yaml", f"?? {OLD}/spec.md"]

    def test_the_whole_rename_is_excused(self):
        attributed, residual_appeared, residual_vanished = (
            partition_attributed_drift(self.APPEARED, self.VANISHED, [self.PREFIXES])
        )
        assert residual_appeared == []
        assert residual_vanished == []
        assert len(attributed) == len(self.APPEARED) + len(self.VANISHED)

    def test_a_deletion_without_a_landing_counterpart_still_fails(self):
        """Same rename, plus a test deleting a file that does NOT reappear:
        the rename's excuse must not cover it."""
        appeared = self.APPEARED + [f" D {self.OLD}/secret.yaml"]
        attributed, residual_appeared, _ = partition_attributed_drift(
            appeared, self.VANISHED, [self.PREFIXES]
        )
        assert residual_appeared == [f" D {self.OLD}/secret.yaml"]
        assert f" D {self.OLD}/events.jsonl" in attributed

    def test_one_declared_prefix_can_never_excuse_a_removal(self):
        """No rename in play — a counterpart under a *different* declared
        prefix is impossible, so removals cannot be excused at all."""
        attributed, residual_appeared, residual_vanished = (
            partition_attributed_drift(
                [f" D {self.OLD}/state.yaml", f"?? {self.OLD}/state.yaml"],
                [f"?? {self.OLD}/spec.md"],
                [[self.OLD]],
            )
        )
        assert residual_appeared == [f" D {self.OLD}/state.yaml"]
        assert residual_vanished == [f"?? {self.OLD}/spec.md"]
        assert attributed == [f"?? {self.OLD}/state.yaml"]

    def test_a_counterpart_cannot_be_fabricated_from_two_records(self):
        """Which addresses were declared TOGETHER is what licenses a rename
        counterpart. Flattened into one union, an unrelated `work seed` of
        another run supplied a fresh `?? other/state.yaml` that excused a
        test's ` D current/state.yaml` — run-dir tails are universal, so the
        coincidence is cheap (MR !200 review round 3)."""
        current, other = "h/p/runs/current", "h/p/runs/other"
        appeared = [f" D {current}/state.yaml", f"?? {other}/state.yaml"]
        attributed, residual_appeared, _ = partition_attributed_drift(
            appeared, [], [[current], [other]]
        )
        assert residual_appeared == [f" D {current}/state.yaml"]
        assert attributed == [f"?? {other}/state.yaml"]

    def test_one_record_declaring_both_addresses_still_excuses(self):
        """The same two prefixes inside ONE record are a rename, and must
        keep working — this is what the post-dispatch journal write produces."""
        appeared = [f" D {self.OLD}/state.yaml", f"?? {self.NEW}/state.yaml"]
        attributed, residual_appeared, _ = partition_attributed_drift(
            appeared, [], [self.PREFIXES]
        )
        assert residual_appeared == []
        assert len(attributed) == 2

    def test_a_landing_that_is_itself_a_deletion_is_not_a_counterpart(self):
        """Two deletions across the two declared prefixes are a test
        clearing both addresses, not a rename — nothing landed."""
        appeared = [f" D {self.OLD}/state.yaml", f" D {self.NEW}/state.yaml"]
        attributed, residual_appeared, _ = partition_attributed_drift(
            appeared, [], [self.PREFIXES]
        )
        assert not attributed
        assert residual_appeared == appeared


class TestPorcelainQuoting:
    """Git C-quotes any path with a space/quote/backslash/control char, even
    under core.quotePath=false — refusing those failed whole runs over
    ordinary session artifacts (MR !200 review round 2, minor)."""

    @pytest.mark.parametrize(
        "line,expected",
        [
            ('?? "sp ace.md"', "sp ace.md"),
            ('?? "meeting notes.md"', "meeting notes.md"),
            ('?? "quo\\"te.md"', 'quo"te.md'),
            ('?? "back\\\\slash.md"', "back\\slash.md"),
            ('?? "tab\\there.md"', "tab\there.md"),
            ("?? résumé.md", "résumé.md"),
        ],
    )
    def test_quoted_paths_decode(self, line, expected):
        assert porcelain_entry_path(line) == expected

    def test_octal_escapes_decode_to_utf8(self):
        assert porcelain_entry_path('?? "caf\\303\\251.md"') == "café.md"

    @pytest.mark.parametrize(
        "line",
        [
            '?? "unterminated.md',
            '?? "bad\\zescape.md"',
            '?? "short\\7.md"',
            # Octal above \377: git's quote_c_style cannot emit it (it
            # formats an unsigned char), but the contract says undecodable
            # returns None — and a ValueError here escapes a teardown
            # fixture as a raw ERROR instead of any verdict.
            '?? "\\400"',
            '?? "\\777x"',
        ],
    )
    def test_undecodable_quoting_still_fails_safe(self, line):
        assert porcelain_entry_path(line) is None

    def test_unquote_guards_its_own_delimiters(self):
        """Unreachable through porcelain_entry_path, which checks first —
        but a helper that silently strips the first and last character of an
        unquoted string is a trap for the next caller."""
        from tests.conftest import unquote_c_style

        assert unquote_c_style("abc") is None
        assert unquote_c_style('"') is None
        assert unquote_c_style('"ok"') == "ok"

    def test_an_arrow_in_a_filename_is_not_a_rename_line(self):
        """Only R/C statuses carry two-path syntax; an untracked file merely
        NAMED `a -> b.md` must stay attributable (MR !200 review round 3)."""
        assert porcelain_entry_path('?? "a -> b.md"') == "a -> b.md"
        assert porcelain_entry_path("R  old.md -> new.md") is None
        assert porcelain_entry_path("C  old.md -> copy.md") is None

    def test_a_quoted_session_artifact_is_excusable(self):
        """The point of decoding: `meeting notes.md` in an excused run dir
        must not fail the run."""
        prefix = "host/proj/runs/r1"
        attributed, residual, _ = partition_attributed_drift(
            [f'?? "{prefix}/meeting notes.md"'], [], [[prefix]]
        )
        assert attributed and not residual

class TestAttributedNoteInDriftReport:
    ATTRIBUTED = ["?? host/proj/runs/r1/log.yaml"]

    def test_report_names_the_excluded_entries(self):
        report = work_repo_drift_report(
            WORK, APPEARED, [], HEAD_A, HEAD_A, attributed=self.ATTRIBUTED
        )
        assert "Excluded from this verdict" in report
        assert "#233" in report
        assert self.ATTRIBUTED[0] in report

    def test_no_note_without_attributed_entries(self):
        report = work_repo_drift_report(WORK, APPEARED, [], HEAD_A, HEAD_A)
        assert "Excluded from this verdict" not in report

    def test_attributed_alone_is_still_no_report(self):
        """Fully-excused drift passes: the report only exists when residual
        drift remains."""
        assert (
            work_repo_drift_report(
                WORK, [], [], HEAD_A, HEAD_A, attributed=self.ATTRIBUTED
            )
            is None
        )


class TestBlameReport:
    BLAMED = [{"command": "log", "pid": 4242}]

    def test_names_the_leaking_command(self):
        report = work_repo_blame_report(WORK, self.BLAMED, [], [])
        assert "`work log` (pid 4242)" in report
        assert "test-descendant" in report

    def test_points_at_ancestry_and_env_isolation(self):
        report = work_repo_blame_report(WORK, self.BLAMED, [], [])
        assert "#233" in report
        assert "#93" in report
        assert "_clean_lmer_env" in report

    def test_includes_the_drift_it_saw(self):
        report = work_repo_blame_report(WORK, self.BLAMED, APPEARED, VANISHED)
        assert APPEARED[0] in report
        assert VANISHED[0] in report


class TestGuardAttributionEndToEnd:
    """The guard's attribution wiring, driven through a real pytest
    subprocess — the TestLeakGuardEndToEnd harness (see
    test_workrepo_isolation_guard.py) extended with journal records."""

    def _fake_work_repo(self, tmp_path):
        fake = tmp_path / "fake-work"
        fake.mkdir()
        subprocess.run(
            ["git", "-C", str(fake), "init", "-q"],
            check=True,
            capture_output=True,
        )
        return fake

    def _run_probe(self, pytester, monkeypatch, tmp_path, probe_source):
        fake = self._fake_work_repo(tmp_path)
        conftest_src = (Path(__file__).parent / "conftest.py").read_text()
        pytester.makeconftest(conftest_src)
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(fake))
        pytester.makepyfile(test_probe=probe_source)
        return fake, pytester.runpytest_subprocess("-q", "-s")

    def test_session_attributed_write_passes(self, pytester, monkeypatch, tmp_path):
        """A write covered by a non-ancestor journal record is the launching
        session doing its job: the suite stays green and says what it excused."""
        _, result = self._run_probe(
            pytester, monkeypatch, tmp_path,
            "import json, os, time\n"
            "from pathlib import Path\n"
            "def test_probe(_isolate_gate_lock_dir):\n"
            "    repo = Path(os.environ['LMER_WORK_REPO_PATH'])\n"
            "    target = repo / 'host/proj/runs/r1'\n"
            "    target.mkdir(parents=True)\n"
            "    (target / 'log.yaml').write_text('entry\\n')\n"
            "    record = {\n"
            "        'schema': 1, 'ts': time.time(), 'pid': 12345,\n"
            "        'command': 'log',\n"
            "        'work_repo': str(repo.resolve()),\n"
            "        'rel_paths': ['host/proj/runs/r1'],\n"
            "        'markers': [{'pid': os.getpid(), 'ancestor': False}],\n"
            "    }\n"
            "    # Append — never truncate: this dir doubles as the OUTER\n"
            "    # suite's redirected lock dir, and clobbering its journal\n"
            "    # erases the outer session's attribution records.\n"
            "    with open(Path(_isolate_gate_lock_dir, 'work-writes.jsonl'),\n"
            "              'a', encoding='utf-8') as fh:\n"
            "        fh.write(json.dumps(record) + '\\n')\n",
        )
        assert result.ret == 0
        result.assert_outcomes(passed=1)
        result.stdout.fnmatch_lines(
            ["*attributed to this session's own journaled work-CLI writes*"]
        )

    def test_suite_descendant_write_fails_naming_the_command(
        self, pytester, monkeypatch, tmp_path
    ):
        """An ancestor=True record is a test reaching the real `work` CLI —
        the guard fails even though the porcelain status never changed (the
        append blind spot)."""
        _, result = self._run_probe(
            pytester, monkeypatch, tmp_path,
            "import json, os, time\n"
            "from pathlib import Path\n"
            "def test_probe(_isolate_gate_lock_dir):\n"
            "    repo = Path(os.environ['LMER_WORK_REPO_PATH'])\n"
            "    record = {\n"
            "        'schema': 1, 'ts': time.time(), 'pid': 4242,\n"
            "        'command': 'state',\n"
            "        'work_repo': str(repo.resolve()),\n"
            "        'rel_paths': ['host/proj/runs/r1'],\n"
            "        'markers': [{'pid': os.getpid(), 'ancestor': True}],\n"
            "    }\n"
            "    # Append — never truncate (see the attributed-write probe).\n"
            "    with open(Path(_isolate_gate_lock_dir, 'work-writes.jsonl'),\n"
            "              'a', encoding='utf-8') as fh:\n"
            "        fh.write(json.dumps(record) + '\\n')\n",
        )
        assert result.ret != 0
        result.assert_outcomes(passed=1, errors=1)
        result.stdout.fnmatch_lines(
            ["*test-descendant `work` invocation*`work state` (pid 4242)*"]
        )

    def test_inherited_env_work_child_is_blamed(
        self, pytester, monkeypatch, tmp_path
    ):
        """THE #93 shape, live: a probe spawns the real `work` CLI with the
        suite's own environment fully inherited — redirected lock dir and
        all. The journal-only marker in the redirected dir is what makes the
        child journal at all; its ancestry then names it, and the run fails
        blaming `work log` even though the write landed under a run-dir
        prefix a session write could have excused."""
        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
        monkeypatch.setenv("LMER_REPO_PROJECT", "group/project")
        monkeypatch.setenv("LMER_TASK", "develop")
        monkeypatch.setenv("LMER_TASK_TARGET", "leak-probe")
        _, result = self._run_probe(
            pytester, monkeypatch, tmp_path,
            "import os, subprocess, sys\n"
            "def test_probe():\n"
            "    result = subprocess.run(\n"
            "        [sys.executable, '-c',\n"
            "         'import sys; from work_repo.cli import main; "
            "sys.exit(main())',\n"
            "         'log', 'an ambient-env probe message long enough to "
            "be accepted'],\n"
            "        env=dict(os.environ), capture_output=True, text=True,\n"
            "        timeout=60,\n"
            "    )\n"
            "    assert result.returncode == 0, result.stderr\n",
        )
        assert result.ret != 0
        result.assert_outcomes(passed=1, errors=1)
        result.stdout.fnmatch_lines(
            ["*test-descendant `work` invocation*`work log`*"]
        )

    def test_unattributed_drift_still_fails(self, pytester, monkeypatch, tmp_path):
        """No journal record, no excuse — the pre-#233 verdict is untouched
        (a hand edit to the work repo mid-suite keeps failing)."""
        _, result = self._run_probe(
            pytester, monkeypatch, tmp_path,
            "import os\n"
            "from pathlib import Path\n"
            "def test_probe():\n"
            "    Path(os.environ['LMER_WORK_REPO_PATH'], 'leaked.txt')"
            ".write_text('x')\n",
        )
        assert result.ret != 0
        result.assert_outcomes(passed=1, errors=1)
        result.stdout.fnmatch_lines(["*?? leaked.txt*"])
