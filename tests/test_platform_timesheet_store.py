"""Tests for ``lmer_platform.timesheet_store``'s schema upgrades (issue #665).

A file a pre-#665 daemon created opens with the new ``kept`` columns and its
one-per-project prompts moved to ``key_prompts``; opening is idempotent and two
connections opening an old file at once do not both alter it.
"""

import sqlite3
import threading

import pytest

from lmer_platform import store, timesheet_store
from tests.conftest import strip_lmer_env

OLD_SCHEMA = (
    """CREATE TABLE kept (
        id TEXT PRIMARY KEY, person TEXT NOT NULL, workspace_id TEXT NOT NULL,
        project_key TEXT NOT NULL, handler TEXT NOT NULL, fields TEXT NOT NULL DEFAULT '{}',
        client TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', start TEXT NOT NULL,
        end TEXT NOT NULL, duration INTEGER NOT NULL, trigger_start TEXT NOT NULL,
        trigger_stop TEXT NOT NULL, reason TEXT NOT NULL, at TEXT NOT NULL,
        sessions TEXT NOT NULL DEFAULT '[]')""",
    """CREATE TABLE prompts (
        person TEXT NOT NULL, workspace_id TEXT NOT NULL, project_key TEXT NOT NULL,
        missing TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (person, workspace_id))""",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def old_file(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    root.mkdir()
    path = root / timesheet_store.DB_FILE
    with sqlite3.connect(path) as db:
        for statement in OLD_SCHEMA:
            db.execute(statement)
        db.execute("INSERT INTO prompts VALUES ('alex', 'ws-1', 'widgets', '[\"task_id\"]',"
                   " '2026-11-04T09:00:00Z')")
    return path


def _columns(db, table):
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}


def test_an_old_file_gains_the_kept_columns_and_moves_its_prompts(old_file):
    with timesheet_store.database() as db:
        assert {"identity", "issues"} <= _columns(db, "kept")
        assert db.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 0
        rows = [dict(row) for row in db.execute("SELECT * FROM key_prompts")]
    assert rows == [{
        "id": 1, "person": "alex", "workspace_id": "ws-1", "project_key": "widgets",
        "work_key": "", "run": "", "issues": "[]", "missing": '["task_id"]',
        "at": "2026-11-04T09:00:00Z",
    }]


def test_opening_twice_changes_nothing_more(old_file):
    with timesheet_store.database():
        pass
    with timesheet_store.database() as db:
        assert db.execute("SELECT COUNT(*) FROM key_prompts").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 0


def test_new_tables_take_one_row_per_key(old_file):
    with timesheet_store.database() as db:
        db.execute("INSERT INTO key_values VALUES ('alex', 'tracker-x', 'k', 'task_id', 'v', 't')")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO key_values VALUES ('alex', 'tracker-x', 'k', 'task_id', 'w', 't')")
        db.execute("INSERT INTO key_prompts (person, workspace_id, project_key, work_key, missing, at)"
                   " VALUES ('alex', 'ws-1', 'widgets', 'k', '[]', 't')")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO key_prompts (person, workspace_id, project_key, work_key,"
                       " missing, at) VALUES ('alex', 'ws-1', 'widgets', 'k', '[]', 't')")


def test_two_connections_opening_an_old_file_at_once_both_succeed(old_file):
    errors = []
    barrier = threading.Barrier(2)

    def open_it():
        try:
            barrier.wait(timeout=10)
            with timesheet_store.database() as db:
                _columns(db, "kept")
        except Exception as exc:  # pragma: no cover - the failure being guarded
            errors.append(exc)

    threads = [threading.Thread(target=open_it) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    with timesheet_store.database() as db:
        assert {"identity", "issues"} <= _columns(db, "kept")
        assert db.execute("SELECT COUNT(*) FROM key_prompts").fetchone()[0] == 1
