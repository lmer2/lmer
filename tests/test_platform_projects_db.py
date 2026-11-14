"""The projects database's connections: setup once, reads off the write lock (#561).

Every ``projects.database()`` call used to rerun the whole schema script and then
``BEGIN IMMEDIATE`` — pure reads included — so each lookup queued on sqlite's one
write lock behind every other thread's transaction, the event loop's among them.
These tests pin the replacement: setup decided by the file's own
``user_version``, reads through a connection that takes no write lock, and an
``enabled()`` that does not open a connection per call.
"""

import hashlib
import inspect
import shutil
import sqlite3
import subprocess
import sys
import threading

import pytest

from lmer_platform import assistant_history, projects, store


@pytest.fixture
def platform(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    projects.migrate()
    return root


def _spy_builds(monkeypatch):
    builds = []
    original = projects._build_schema

    def spy(db):
        builds.append(1)
        return original(db)

    monkeypatch.setattr(projects, "_build_schema", spy)
    return builds


def test_the_schema_is_built_once_however_many_connections_follow(platform, monkeypatch):
    builds = _spy_builds(monkeypatch)
    for _ in range(50):
        projects.get(projects.GENERAL)
        projects.listing()
        projects.run_home("git.example.com/team/project/nothing")
        projects.session_home("20261111-000000-1-nothing")
        with projects.database() as db:
            db.execute("SELECT 1")
    assert builds == []
    with projects.reader() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == projects.PROJECTS_SCHEMA


def _hold_write_lock(path, held, release):
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("BEGIN IMMEDIATE")
    db.execute("UPDATE projects SET retries = retries WHERE id = ?", (projects.GENERAL,))
    held.set()
    release.wait(30)
    db.execute("ROLLBACK")
    db.close()


def test_reads_do_not_wait_for_a_writer(platform):
    held, release = threading.Event(), threading.Event()
    writer = threading.Thread(target=_hold_write_lock, args=(projects.path(), held, release))
    writer.start()
    assert held.wait(10)
    results = {}

    def read():
        results["get"] = projects.get(projects.GENERAL)["id"]
        results["listing"] = [row["id"] for row in projects.listing()]
        results["run_home"] = projects.run_home("git.example.com/team/project/nothing")
        results["session_home"] = projects.session_home("20261111-000000-1-nothing")
        results["session_run"] = projects.session_run("20261111-000000-1-nothing")
        results["enabled"] = projects.enabled()

    reader = threading.Thread(target=read)
    reader.start()
    # Before #561 each of these took BEGIN IMMEDIATE and sat in the busy
    # handler until the writer finished or 30 s passed.
    reader.join(5)
    finished = not reader.is_alive()
    release.set()
    writer.join(10)
    reader.join(40)
    assert finished, "a pure read waited for another connection's write lock"
    assert results == {"get": "general", "listing": ["general"], "run_home": None,
                       "session_home": None, "session_run": None, "enabled": True}


def test_a_reader_cannot_write(platform):
    with projects.reader() as db:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("DELETE FROM starts")


def test_a_database_overwritten_in_place_heals(platform, tmp_path, monkeypatch):
    builds = _spy_builds(monkeypatch)
    assert projects.enabled() is True
    empty = tmp_path / "empty.sqlite3"
    sqlite3.connect(empty).close()
    before = projects.path().stat().st_ino
    shutil.copyfile(empty, projects.path())  # truncates and rewrites the same inode
    assert projects.path().stat().st_ino == before

    # Not migrated any more, whatever was remembered about the old content...
    assert projects.enabled() is False
    # ...and its tables are rebuilt rather than reported missing.
    with pytest.raises(projects.ProjectError) as caught:
        projects.get(projects.GENERAL)
    assert caught.value.code == "project_not_found"
    assert builds == [1]


def test_reads_recover_a_hot_journal_left_by_a_writer_that_died(platform):
    script = (
        "import os, sqlite3, sys\n"
        "db = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
        "db.execute('PRAGMA cache_size = 1')\n"
        "db.execute('BEGIN IMMEDIATE')\n"
        "db.execute('CREATE TABLE padding (blob TEXT)')\n"
        "for _ in range(400):\n"
        "    db.execute('INSERT INTO padding VALUES (?)', ('x' * 4000,))\n"
        "db.execute(\"UPDATE projects SET name = 'half-written' WHERE id = 'general'\")\n"
        "os._exit(9)\n"
    )
    subprocess.run([sys.executable, "-c", script, str(projects.path())], check=False)
    journal = projects.path().with_name(projects.path().name + "-journal")
    assert journal.exists() and journal.stat().st_size > 0

    # The guard this design keeps: a mode=ro open cannot roll the journal back.
    uri = projects.path().absolute().as_uri() + "?mode=ro"
    with pytest.raises(sqlite3.OperationalError):
        with sqlite3.connect(uri, uri=True) as db:
            db.execute("SELECT name FROM projects").fetchall()

    assert projects.enabled() is True
    assert projects.get(projects.GENERAL)["name"] == "General"
    assert not journal.exists() or journal.stat().st_size == 0


def test_enabled_opens_no_connection_until_the_file_changes(platform, monkeypatch):
    assert projects.enabled() is True
    opened = []
    real = sqlite3.connect
    monkeypatch.setattr(projects.sqlite3, "connect",
                        lambda *a, **k: opened.append(1) or real(*a, **k))
    for _ in range(100):
        assert projects.enabled() is True
    assert opened == []
    with projects.database() as db:
        db.execute("UPDATE projects SET retries = retries + 1 WHERE id = ?", (projects.GENERAL,))
    opened.clear()
    assert projects.enabled() is True
    assert projects.enabled() is True
    assert opened == [1]


def test_enabled_never_remembers_a_negative_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
    assert projects.enabled() is False
    projects.migrate()
    assert projects.enabled() is True


# The schema numbers have to move with the schema. These fingerprints fail the
# moment the DDL changes; the fix is to bump the number AND update the hash, in
# that order, so no existing file keeps an old schema behind a current number.
PROJECTS_FINGERPRINT = "91c9d7da9d254021815a6fc10f837bcd053853ad741a5e06dc4443dff3c6320a"
HISTORY_FINGERPRINT = "9614f084f2e3c5ac2641488f23ef98be1d82e18d304c32746c558daae4b6a7ab"


def test_the_projects_schema_number_moves_with_its_ddl():
    source = projects._SCHEMA + inspect.getsource(projects._build_schema)
    assert hashlib.sha256(source.encode()).hexdigest() == PROJECTS_FINGERPRINT, (
        "projects._SCHEMA or _build_schema changed: bump PROJECTS_SCHEMA, then update this fingerprint"
    )
    assert projects.PROJECTS_SCHEMA == 1


def test_the_history_schema_number_moves_with_its_ddl():
    assert hashlib.sha256(assistant_history._SCHEMA.encode()).hexdigest() == HISTORY_FINGERPRINT, (
        "assistant_history._SCHEMA changed: bump HISTORY_SCHEMA, then update this fingerprint"
    )
    assert assistant_history.HISTORY_SCHEMA == 1


def test_history_reads_build_their_schema_once(platform, monkeypatch):
    with assistant_history._database() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == assistant_history.HISTORY_SCHEMA
    # Any rerun of the DDL would now raise: the reads below prove there is none.
    monkeypatch.setattr(assistant_history, "_SCHEMA", "THIS IS NOT SQL")
    for _ in range(20):
        assert assistant_history.latest() is None
        assert assistant_history.credential_owner("not-a-credential") is None
    with assistant_history._database(read_only=True) as db:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("DELETE FROM metadata")
