"""The write-attribution journal (issue #233).

The /work leak guard cannot tell a leaking test from the session that
launched the suite doing its normal job — by *path* they are identical, both
landing in the current run dir. The journal records the discriminating fact
at the source: the `work` CLI, at write time, notes whether the gate-marker
holder (pytest, for a suite) is among its own process ancestors. These tests
cover the record's shape, the ancestry verdicts, the fail-soft journaling
mechanics, and the CLI wiring end to end (a real `work log` subprocess
journaling itself as suite-descendant).
"""
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from lmer_cli import gate_lock
from work_repo import write_journal

SRC_DIR = str(Path(write_journal.__file__).parents[1])


def _valid_record(**overrides):
    record = {
        "schema": 1,
        "ts": 1000.0,
        "pid": 42,
        "command": "log",
        "work_repo": "/work",
        "rel_paths": ["host/proj/runs/r1"],
        "markers": [{"pid": 7, "ancestor": False}],
    }
    record.update(overrides)
    return record


class TestParseRecord:
    def test_round_trips_a_valid_record(self):
        parsed = write_journal.parse_record(json.dumps(_valid_record()))
        assert parsed["work_repo"] == "/work"
        assert parsed["command"] == "log"
        assert parsed["rel_paths"] == ["host/proj/runs/r1"]
        assert parsed["markers"] == [{"pid": 7, "ancestor": False}]
        assert parsed["ts"] == 1000.0

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "not json",
            '{"truncated": ',
            "[1, 2]",
            '"a string"',
        ],
    )
    def test_unusable_text_is_discarded(self, text):
        assert write_journal.parse_record(text) is None

    def test_requires_a_work_repo(self):
        record = _valid_record()
        del record["work_repo"]
        assert write_journal.parse_record(json.dumps(record)) is None

    def test_requires_markers(self):
        assert (
            write_journal.parse_record(json.dumps(_valid_record(markers=[])))
            is None
        )

    def test_marker_entries_without_a_pid_are_dropped(self):
        record = _valid_record(
            markers=[{"ancestor": True}, {"pid": "x"}, {"pid": 9, "ancestor": True}]
        )
        parsed = write_journal.parse_record(json.dumps(record))
        assert parsed["markers"] == [{"pid": 9, "ancestor": True}]

    def test_non_bool_ancestor_reads_as_unknown(self):
        record = _valid_record(markers=[{"pid": 9, "ancestor": "yes"}])
        parsed = write_journal.parse_record(json.dumps(record))
        assert parsed["markers"] == [{"pid": 9, "ancestor": None}]

    def test_bad_rel_paths_and_ts_degrade_instead_of_discarding(self):
        record = _valid_record(rel_paths="oops", ts="not a float")
        parsed = write_journal.parse_record(json.dumps(record))
        assert parsed["rel_paths"] == []
        assert parsed["ts"] is None


class TestMarkerVerdicts:
    def test_marker_in_chain_is_ancestor(self):
        verdicts = write_journal.marker_verdicts([7], [99, 7, 1], True)
        assert verdicts == [{"pid": 7, "ancestor": True}]

    def test_partial_chain_containing_the_marker_is_still_conclusive(self):
        verdicts = write_journal.marker_verdicts([7], [99, 7], False)
        assert verdicts == [{"pid": 7, "ancestor": True}]

    def test_complete_chain_without_the_marker_is_not_ancestor(self):
        verdicts = write_journal.marker_verdicts([7], [99, 3, 1], True)
        assert verdicts == [{"pid": 7, "ancestor": False}]

    def test_incomplete_chain_without_the_marker_is_unknown(self):
        """Absence from a walk that never finished proves nothing — unknown
        must stay unknown, because the guard treats it as unattributed
        (fails), never as the session's own write (passes)."""
        verdicts = write_journal.marker_verdicts([7], [99, 3], False)
        assert verdicts == [{"pid": 7, "ancestor": None}]

    def test_no_chain_at_all_is_unknown(self):
        verdicts = write_journal.marker_verdicts([7], None, False)
        assert verdicts == [{"pid": 7, "ancestor": None}]


class TestGuardAddressing:
    def test_addressed_when_repo_and_marker_pid_match(self):
        record = write_journal.parse_record(json.dumps(_valid_record()))
        assert write_journal.record_addresses_guard(record, "/work", 7)

    def test_not_addressed_for_another_repo(self):
        record = write_journal.parse_record(json.dumps(_valid_record()))
        assert not write_journal.record_addresses_guard(record, "/elsewhere", 7)

    def test_not_addressed_for_another_guard_pid(self):
        record = write_journal.parse_record(json.dumps(_valid_record()))
        assert not write_journal.record_addresses_guard(record, "/work", 8)

    def test_guard_verdict_reads_the_matching_marker(self):
        record = write_journal.parse_record(
            json.dumps(
                _valid_record(
                    markers=[
                        {"pid": 7, "ancestor": False},
                        {"pid": 9, "ancestor": True},
                    ]
                )
            )
        )
        assert write_journal.guard_verdict(record, 7) is False
        assert write_journal.guard_verdict(record, 9) is True
        assert write_journal.guard_verdict(record, 11) is None

    @pytest.mark.parametrize(
        "verdicts,expected",
        [
            ([True, False], True),
            ([False, True], True),
            ([None, False], None),
            ([False, None, True], True),
            ([False, False], False),
        ],
    )
    def test_duplicate_guard_entries_resolve_worst_verdict_wins(
        self, verdicts, expected
    ):
        """A crafted record carrying a False beside a True must not flip the
        guard from fail to pass — the journal lives in a world-writable tmp
        dir, so the excuse is only as strong as the weakest claim in it."""
        record = write_journal.parse_record(
            json.dumps(
                _valid_record(
                    markers=[{"pid": 7, "ancestor": verdict} for verdict in verdicts]
                )
            )
        )
        assert write_journal.guard_verdict(record, 7) is expected


class TestPpidChain:
    def test_chain_starts_at_self_and_includes_the_parent(self):
        chain, _complete = write_journal.ppid_chain()
        assert chain is not None
        assert chain[0] == os.getpid()
        assert os.getppid() in chain

    def test_complete_walk_reaches_init(self):
        if not Path("/proc/1/status").exists():
            pytest.skip("no /proc process tree on this platform")
        chain, complete = write_journal.ppid_chain()
        assert complete
        assert chain[-1] == 1

    def test_an_orphaned_writer_reads_as_incomplete(self):
        """A chain that is nothing but [pid, init] means the writer's true
        ancestry was erased by re-parenting (a detached test child, e.g.
        via double-fork) — 'not a descendant' must not be concluded from
        it, so the walk reports incomplete and the verdict stays unknown."""
        if not Path("/proc/1/status").exists():
            pytest.skip("no /proc process tree on this platform")
        chain, complete = write_journal.ppid_chain(pid=1)
        assert chain == [1]
        assert not complete
        assert write_journal.marker_verdicts([424242], chain, complete) == [
            {"pid": 424242, "ancestor": None}
        ]


class TestRecordWriteIntent:
    """The impure writer: journaling keyed on live markers, always fail-soft."""

    @pytest.fixture
    def lock_dir(self, monkeypatch, tmp_path):
        directory = tmp_path / "locks"
        directory.mkdir()
        monkeypatch.setenv(gate_lock.LOCK_DIR_ENV, str(directory))
        return directory

    def _marker(self, lock_dir, pid):
        (lock_dir / f"{pid}.json").write_text(
            json.dumps({"pid": pid, "gate": "test", "started_at": time.time()})
        )

    def test_no_live_marker_means_no_journal(self, lock_dir, tmp_path):
        write_journal.record_write_intent("log", ["a/b"], work_repo=str(tmp_path))
        assert not (lock_dir / write_journal.JOURNAL_NAME).exists()

    def test_ancestor_marker_records_true(self, lock_dir, tmp_path):
        self._marker(lock_dir, os.getppid())
        write_journal.record_write_intent("log", ["a/b"], work_repo=str(tmp_path))
        lines = (lock_dir / write_journal.JOURNAL_NAME).read_text().splitlines()
        record = write_journal.parse_record(lines[-1])
        assert record["command"] == "log"
        assert record["work_repo"] == str(tmp_path.resolve())
        assert {"pid": os.getppid(), "ancestor": True} in record["markers"]

    def test_non_ancestor_marker_records_false(self, lock_dir, tmp_path):
        child = subprocess.Popen(["sleep", "30"])
        try:
            self._marker(lock_dir, child.pid)
            write_journal.record_write_intent(
                "state", ["a/b"], work_repo=str(tmp_path)
            )
            lines = (
                (lock_dir / write_journal.JOURNAL_NAME).read_text().splitlines()
            )
            record = write_journal.parse_record(lines[-1])
            assert {"pid": child.pid, "ancestor": False} in record["markers"]
        finally:
            child.kill()
            child.wait()

    def test_own_marker_journals_as_ancestor(self, lock_dir, tmp_path):
        """A marker held by the calling process itself yields ancestor=True —
        a process is trivially its own ancestor. This is what blames test
        code invoking the CLI in-process (it runs AS the marker-holding
        pytest); for `work verify` under its own marker the record is
        addressed to a pid no guard holds, so it is inert."""
        self._marker(lock_dir, os.getpid())
        write_journal.record_write_intent("log", ["a/b"], work_repo=str(tmp_path))
        lines = (lock_dir / write_journal.JOURNAL_NAME).read_text().splitlines()
        record = write_journal.parse_record(lines[-1])
        assert write_journal.guard_verdict(record, os.getpid()) is True

    def test_journal_only_marker_triggers_journaling_but_not_deferral(
        self, lock_dir, tmp_path
    ):
        """The suite's redirected-dir marker (issue #233): children that
        inherited the suite env must journal against it, while the commit
        paths tests exercise keep committing."""
        (lock_dir / f"{os.getppid()}.json").write_text(
            json.dumps(
                {
                    "pid": os.getppid(),
                    "gate": "pytest-suite",
                    "started_at": time.time(),
                    "journal_only": True,
                }
            )
        )
        assert gate_lock.active_gate(exclude_self=False) is None
        write_journal.record_write_intent("log", ["a/b"], work_repo=str(tmp_path))
        lines = (lock_dir / write_journal.JOURNAL_NAME).read_text().splitlines()
        record = write_journal.parse_record(lines[-1])
        assert write_journal.guard_verdict(record, os.getppid()) is True

    def test_kill_switch_disables_journaling(self, lock_dir, tmp_path, monkeypatch):
        monkeypatch.setenv(gate_lock.GUARD_ENV, "0")
        self._marker(lock_dir, os.getppid())
        write_journal.record_write_intent("log", ["a/b"], work_repo=str(tmp_path))
        assert not (lock_dir / write_journal.JOURNAL_NAME).exists()

    def test_append_never_rewrites_below_the_compaction_threshold(
        self, lock_dir, tmp_path
    ):
        """Appends must not read-modify-write a shared file: a truncation
        racing another writer or the guard's read would lose records, and a
        lost record is a spuriously failed suite. Stale lines are compacted
        at consume time instead."""
        stale = _valid_record(ts=time.time() - write_journal.STALE_AFTER_SECONDS - 60)
        path = lock_dir / write_journal.JOURNAL_NAME
        path.write_text(json.dumps(stale) + "\n")
        self._marker(lock_dir, os.getppid())
        write_journal.record_write_intent("log", ["a/b"], work_repo=str(tmp_path))
        lines = path.read_text().splitlines()
        assert len(lines) == 2
        assert write_journal.parse_record(lines[-1])["command"] == "log"

    def test_oversized_journal_is_compacted_on_append(self, lock_dir, tmp_path):
        stale = _valid_record(ts=time.time() - write_journal.STALE_AFTER_SECONDS - 60)
        path = lock_dir / write_journal.JOURNAL_NAME
        line = json.dumps(stale) + "\n"
        copies = write_journal.COMPACT_AT_BYTES // len(line) + 2
        path.write_text(line * copies)
        self._marker(lock_dir, os.getppid())
        write_journal.record_write_intent("log", ["a/b"], work_repo=str(tmp_path))
        lines = path.read_text().splitlines()
        assert len(lines) == 1
        assert write_journal.parse_record(lines[0])["command"] == "log"


class TestConsumeRecords:
    def _write(self, directory, record):
        path = directory / write_journal.JOURNAL_NAME
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        return path

    def test_missing_journal_is_empty(self, tmp_path):
        assert write_journal.consume_records("/work", 7, directory=tmp_path) == []

    def test_consumes_addressed_records_and_removes_the_file(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        path = self._write(
            tmp_path,
            _valid_record(work_repo=write_journal.resolve_work_repo(str(repo))),
        )
        records = write_journal.consume_records(str(repo), 7, directory=tmp_path)
        assert len(records) == 1
        assert records[0]["command"] == "log"
        assert not path.exists()

    def test_leaves_records_addressed_elsewhere(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        mine = _valid_record(work_repo=write_journal.resolve_work_repo(str(repo)))
        other_repo = _valid_record(work_repo="/somewhere/else", ts=time.time())
        other_guard = _valid_record(
            work_repo=write_journal.resolve_work_repo(str(repo)),
            markers=[{"pid": 999999, "ancestor": False}],
            ts=time.time(),
        )
        path = self._write(tmp_path, mine)
        self._write(tmp_path, other_repo)
        self._write(tmp_path, other_guard)
        records = write_journal.consume_records(str(repo), 7, directory=tmp_path)
        assert len(records) == 1
        remaining = [
            write_journal.parse_record(line)
            for line in path.read_text().splitlines()
        ]
        assert len(remaining) == 2

    def test_resolves_symlinked_repo_paths(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(repo)
        self._write(
            tmp_path,
            _valid_record(work_repo=write_journal.resolve_work_repo(str(repo))),
        )
        records = write_journal.consume_records(str(alias), 7, directory=tmp_path)
        assert len(records) == 1

    def test_multi_guard_records_survive_the_first_consume(self, tmp_path):
        """Consumption is per-guard: a record addressed to two overlapping
        suites must still carry its verdict for whichever tears down second
        (MR !200 review, minor finding)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        self._write(
            tmp_path,
            _valid_record(
                work_repo=write_journal.resolve_work_repo(str(repo)),
                markers=[
                    {"pid": 7, "ancestor": False},
                    {"pid": 9, "ancestor": True},
                ],
                ts=time.time(),
            ),
        )
        first = write_journal.consume_records(str(repo), 7, directory=tmp_path)
        assert len(first) == 1
        assert write_journal.guard_verdict(first[0], 7) is False
        second = write_journal.consume_records(str(repo), 9, directory=tmp_path)
        assert len(second) == 1
        assert write_journal.guard_verdict(second[0], 9) is True
        assert not (tmp_path / write_journal.JOURNAL_NAME).exists()

    def test_consume_prunes_stale_unaddressed_records(self, tmp_path):
        """The consume rewrite is the journal's compaction point: records a
        dead suite left behind must not accumulate forever."""
        repo = tmp_path / "repo"
        repo.mkdir()
        stale = _valid_record(
            work_repo="/somewhere/else",
            ts=time.time() - write_journal.STALE_AFTER_SECONDS - 60,
        )
        fresh_other = _valid_record(work_repo="/somewhere/else", ts=time.time())
        mine = _valid_record(
            work_repo=write_journal.resolve_work_repo(str(repo)), ts=time.time()
        )
        path = self._write(tmp_path, stale)
        self._write(tmp_path, fresh_other)
        self._write(tmp_path, mine)
        records = write_journal.consume_records(str(repo), 7, directory=tmp_path)
        assert len(records) == 1
        remaining = path.read_text().splitlines()
        assert len(remaining) == 1
        assert write_journal.parse_record(remaining[0])["ts"] != stale["ts"]


class TestNoContextInvocationsAreNotJournaled:
    """The MR !200 critical: a write-shaped command with no run context has
    an empty scope — it cannot address the operational repo (it errors before
    writing), so journaling it hands the guard a blameable record for a write
    that never was. The suite's own no-context CLI tests run exactly this
    shape in-process under the suite's journal-only marker."""

    def test_in_process_no_context_state_set_leaves_no_record(
        self, monkeypatch, tmp_path
    ):
        from unittest.mock import patch as mock_patch
        from work_repo import cli as work_cli

        lock_dir = tmp_path / "locks"
        lock_dir.mkdir()
        (lock_dir / f"{os.getpid()}.json").write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "gate": "pytest-suite",
                    "started_at": time.time(),
                    "journal_only": True,
                }
            )
        )
        for key in list(os.environ):
            if key.startswith("LMER_"):
                monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv(gate_lock.LOCK_DIR_ENV, str(lock_dir))
        with mock_patch.object(
            work_cli.sys, "argv", ["work", "state", "set", "--phase=interview"]
        ):
            assert work_cli.main() == 1
        assert not (lock_dir / write_journal.JOURNAL_NAME).exists()

    def test_record_write_intent_is_not_called_with_an_empty_scope(
        self, monkeypatch, tmp_path
    ):
        """The hook itself must skip: `not rel_paths` covers both None
        (read-only) and [] (write-shaped, no context)."""
        from unittest.mock import patch as mock_patch
        import argparse
        from work_repo import cli as work_cli

        for key in list(os.environ):
            if key.startswith("LMER_"):
                monkeypatch.delenv(key, raising=False)
        with mock_patch.object(write_journal, "record_write_intent") as record:
            work_cli._journal_write_intent(
                argparse.Namespace(command="state", action="set")
            )
        record.assert_not_called()


class TestWriteIntentMap:
    """_write_intent_rel_paths — which invocations count as writes, and where.

    Over-journaling matters as much as under-journaling: a read-only command
    journaled as a write widens the excuse mask over the whole run dir for
    the suite's window."""

    def _paths(self, **attrs):
        from work_repo.cli import _write_intent_rel_paths
        import argparse

        return _write_intent_rel_paths(argparse.Namespace(**attrs))

    def _run_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
        monkeypatch.setenv("LMER_REPO_PROJECT", "group/project")
        monkeypatch.setenv("LMER_TASK", "develop")
        monkeypatch.setenv("LMER_TASK_TARGET", "probe")

    def test_log_with_message_covers_the_run_dir(self, monkeypatch, tmp_path):
        self._run_env(monkeypatch, tmp_path)
        paths = self._paths(command="log", message="hello there")
        assert paths and all("runs/" in p for p in paths)

    @pytest.mark.parametrize(
        "attrs",
        [
            {"command": "log", "message": None},
            {"command": "goal", "description": None},
            {"command": "name", "value": None},
            {"command": "state", "action": None},
            {"command": "ledger", "action": None},
            {"command": "goals", "goals_action": "check"},
            {"command": "goals", "goals_action": None},
            {"command": "specs-index", "rebuild": False},
            {"command": "memory", "memory_action": "restore"},
            {"command": "release", "release_action": "status"},
            {"command": "release", "release_action": "claim-status"},
            {"command": "release", "release_action": None},
            {"command": "resume", "as_json": False},
            {"command": "read-project-info"},
            {"command": "commit", "message": None},
        ],
    )
    def test_read_only_shapes_are_not_journaled(self, attrs, monkeypatch, tmp_path):
        self._run_env(monkeypatch, tmp_path)
        assert self._paths(**attrs) is None

    @pytest.mark.parametrize(
        "attrs",
        [
            {"command": "state", "action": "set"},
            {"command": "ledger", "action": "set"},
            {"command": "goals", "goals_action": "freeze"},
            {"command": "goals", "goals_action": "assess"},
            {"command": "event", "type": "x"},
            {"command": "answer", "text": "x"},
            {"command": "release", "release_action": "claim"},
            {"command": "release", "release_action": "record"},
            {"command": "session-start"},
            {"command": "session-end"},
        ],
    )
    def test_write_shapes_cover_the_run_dir(self, attrs, monkeypatch, tmp_path):
        self._run_env(monkeypatch, tmp_path)
        paths = self._paths(**attrs)
        assert paths and all("runs/" in p for p in paths)

    def test_memory_persist_covers_the_memory_dir(self, monkeypatch, tmp_path):
        self._run_env(monkeypatch, tmp_path)
        assert self._paths(command="memory", memory_action="persist") == [
            "git.example.com/group/project/memory"
        ]

    def test_artifact_covers_run_dir_and_specs(self, monkeypatch, tmp_path):
        self._run_env(monkeypatch, tmp_path)
        paths = self._paths(command="artifact", name="spec.md", sync=False)
        assert any("runs/" in p for p in paths)
        assert any(p.endswith("/specs") for p in paths)


class TestWriteIntentMapCoversTheParser:
    """Every real subcommand must be classified, through the real parser.

    The intent map is a second copy of "what each command writes"; this test
    ties it to `create_parser()` so drift fails loudly in both directions
    (MR !200 review, minor finding): a NEW subcommand missing from the table
    below fails collection of expectations; a dest rename breaks the argv
    parse or flips the classification, unlike Namespace-built fixtures which
    stay green through renames.
    """

    #: (subcommand, argv, expects_write) rows — SEVERAL per subcommand where
    #: one command has both modes. One row each pinned dests but not the map's
    #: branches: deleting the `log` write branch, or dropping `record` from
    #: the release tuple, kept a single-row table green while the session's
    #: own use of that command stopped journaling (MR !200 review round 2).
    ROWS = [
        ("read-project-info", ["read-project-info"], False),
        ("log", ["log"], False),
        ("log", ["log", "a message long enough to pass validation"], True),
        ("commit", ["commit"], False),
        ("commit", ["commit", "--message", "m"], False),
        ("report", ["report", "--file", "somefile.md"], True),
        ("goal", ["goal"], False),
        ("goal", ["goal", "a stated goal"], True),
        ("memory", ["memory", "restore"], False),
        ("memory", ["memory", "persist"], True),
        ("setup-workspace", ["setup-workspace", "https://x/y.git"], False),
        ("state", ["state"], False),
        ("state", ["state", "set", "--phase=x"], True),
        ("answer", ["answer", "text"], True),
        ("name", ["name"], False),
        ("name", ["name", "some-name"], True),
        ("verify", ["verify", "tests", "--", "true"], True),
        ("event", ["event", "note"], True),
        ("ledger", ["ledger"], False),
        ("ledger", ["ledger", "set", "T1", "--status", "done"], True),
        ("plan", ["plan", "check"], False),
        ("goals", ["goals"], False),
        ("goals", ["goals", "check"], False),
        ("goals", ["goals", "freeze"], True),
        ("goals", ["goals", "amend"], True),
        ("goals", ["goals", "assess"], True),
        ("resume", ["resume"], False),
        ("artifact", ["artifact", "spec.md", "--file", "x.md"], True),
        ("specs-index", ["specs-index"], False),
        ("specs-index", ["specs-index", "--rebuild"], True),
        ("seed", ["seed", "develop", "some-target"], True),
        ("release", ["release", "status"], False),
        ("release", ["release", "claim-status"], False),
        ("release", ["release", "claim"], True),
        ("release", ["release", "unclaim"], True),
        ("release", ["release", "abort", "--reason", "r"], True),
        ("release", ["release", "record", "tag", "v1.2.3", "--sha", "abc123"], True),
        ("session-start", ["session-start"], True),
        ("session-end", ["session-end"], True),
    ]

    def _subcommands(self):
        import argparse
        from work_repo.cli import create_parser

        parser = create_parser()
        action = next(
            a
            for a in parser._actions
            if isinstance(a, argparse._SubParsersAction)
        )
        return parser, sorted(action.choices)

    def test_every_subcommand_is_classified(self):
        _, commands = self._subcommands()
        covered = {row[0] for row in self.ROWS}
        unclassified = [c for c in commands if c not in covered]
        assert not unclassified, (
            f"new subcommand(s) {unclassified} must be classified in "
            "TestWriteIntentMapCoversTheParser.ROWS: decide whether each "
            "writes the work repo (a row per mode if it has both), and "
            "extend _write_intent_rel_paths if it does — an unclassified "
            "write-shaped command journals nothing, so the session's own use "
            "of it during a suite fails the run with the pre-#233 text "
            "(issue #233)."
        )

    @staticmethod
    def _conditional_commands():
        """Commands the map classifies conditionally, read from its source.

        Derived rather than hand-listed: a third copy of "which commands are
        dual-mode" would let a command that NEWLY becomes dual-mode satisfy
        the classified check via its existing read row while its write branch
        ships unpinned (MR !200 review round 3 observation). A branch counts
        as conditional when its body can yield both a scope and None.
        """
        import ast
        import inspect
        from work_repo.cli import _write_intent_rel_paths

        def returned_expressions(node):
            """Every expression the branch can return, IfExp arms included."""
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Return):
                    continue
                pending = [inner.value]
                while pending:
                    expr = pending.pop()
                    if isinstance(expr, ast.IfExp):
                        pending.extend((expr.body, expr.orelse))
                    else:
                        yield expr

        def is_none(expr):
            return expr is None or (
                isinstance(expr, ast.Constant) and expr.value is None
            )

        tree = ast.parse(textwrap.dedent(inspect.getsource(_write_intent_rel_paths)))
        conditional = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.If) or "command" not in ast.unparse(node.test):
                continue
            returns = list(returned_expressions(node))
            if not any(map(is_none, returns)):
                continue
            if all(map(is_none, returns)):
                continue
            conditional.update(
                literal.value
                for literal in ast.walk(node.test)
                if isinstance(literal, ast.Constant)
                and isinstance(literal.value, str)
            )
        return conditional

    def test_every_dual_mode_command_pins_both_modes(self):
        """A command the map treats conditionally needs a row on each side,
        or deleting its write branch stays green."""
        modes = {}
        for command, _argv, writes in self.ROWS:
            modes.setdefault(command, set()).add(writes)
        conditional = self._conditional_commands()
        assert conditional, "failed to read the map's conditional branches"
        _parser, commands = self._subcommands()
        missing = [
            c for c in sorted(conditional)
            if c in commands and modes.get(c) != {True, False}
        ]
        assert not missing, (
            f"{missing} are classified conditionally in "
            "_write_intent_rel_paths but are pinned in only one mode here"
        )

    def test_classification_matches_the_map_through_the_real_parser(
        self, monkeypatch, tmp_path
    ):
        from work_repo.cli import _write_intent_rel_paths

        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
        monkeypatch.setenv("LMER_REPO_PROJECT", "group/project")
        monkeypatch.setenv("LMER_TASK", "develop")
        monkeypatch.setenv("LMER_TASK_TARGET", "probe")
        parser, _commands = self._subcommands()
        for _command, argv, expects_write in self.ROWS:
            args = parser.parse_args(argv)
            paths = _write_intent_rel_paths(args)
            if expects_write:
                assert paths, (
                    f"`work {' '.join(argv)}` should journal a write scope"
                )
            else:
                assert paths is None, (
                    f"`work {' '.join(argv)}` should not be journaled"
                )


class TestCliWiring:
    """A real `work` subprocess journals itself — the end-to-end proof.

    The subprocess descends from this pytest process, so a marker naming
    THIS pid must produce an ancestor=True record: exactly the shape the
    leak guard blames, demonstrated with the real dispatch hook rather
    than a unit call.
    """

    def _work_env(self, tmp_path, lock_dir):
        fake_repo = tmp_path / "fake-work"
        fake_repo.mkdir(exist_ok=True)
        env = {
            k: v for k, v in os.environ.items() if not k.startswith("LMER_")
        }
        env["PYTHONPATH"] = SRC_DIR + os.pathsep + env.get("PYTHONPATH", "")
        env.update(
            LMER_WORK_REPO_PATH=str(fake_repo),
            LMER_REPO_HOST="git.example.com",
            LMER_REPO_PROJECT="group/project",
            LMER_TASK="develop",
            LMER_TASK_TARGET="issue-233-probe",
            LMER_GATE_LOCK_DIR=str(lock_dir),
        )
        return env

    def _run_work(self, env, *argv):
        return subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from work_repo.cli import main; sys.exit(main())",
                *argv,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_write_invocation_journals_as_suite_descendant(self, tmp_path):
        lock_dir = tmp_path / "locks"
        lock_dir.mkdir()
        (lock_dir / f"{os.getpid()}.json").write_text(
            json.dumps(
                {"pid": os.getpid(), "gate": "pytest-suite", "started_at": time.time()}
            )
        )
        env = self._work_env(tmp_path, lock_dir)
        result = self._run_work(
            env, "log", "a probe log message long enough to be accepted"
        )
        assert result.returncode == 0, result.stderr
        lines = (lock_dir / write_journal.JOURNAL_NAME).read_text().splitlines()
        record = write_journal.parse_record(lines[-1])
        assert record["command"] == "log"
        assert write_journal.guard_verdict(record, os.getpid()) is True
        assert any("runs/" in path for path in record["rel_paths"])

    def test_read_only_invocation_does_not_journal(self, tmp_path):
        lock_dir = tmp_path / "locks"
        lock_dir.mkdir()
        (lock_dir / f"{os.getpid()}.json").write_text(
            json.dumps(
                {"pid": os.getpid(), "gate": "pytest-suite", "started_at": time.time()}
            )
        )
        env = self._work_env(tmp_path, lock_dir)
        result = self._run_work(env, "state")
        assert result.returncode == 0, result.stderr
        assert not (lock_dir / write_journal.JOURNAL_NAME).exists()
