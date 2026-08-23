"""``matrix_bridge.threads`` — the map that makes a reply addressable.

A human's reply says which run it answers only by *being in a thread*. If the
bridge loses the thread↔run map it opens a second thread for a run the room is
already showing, and ignores every reply in the first — a failure that looks
from the room like the bridge went deaf.

So: persisted eagerly (the moment a thread exists), written atomically through
the platform's own snapshot writer, and never guessing when it does not know.
"""

import json
import os

import pytest

from lmer_platform import store
from matrix_bridge.threads import RunKey, ThreadMap

ROOT = "$root-event-id"
OTHER_ROOT = "$other-root-event-id"
RUN = RunKey("git.20c.com", "agents/global", "develop-327")
OTHER_RUN = RunKey("git.20c.com", "agents/global", "review-mr-242")


@pytest.fixture
def path(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
    return tmp_path / "platform" / "matrix" / "threads.json"


# --- the run key -------------------------------------------------------------

def test_a_run_key_round_trips_through_its_text_form():
    assert str(RUN) == "git.20c.com/agents/global/develop-327"
    assert RunKey.parse(str(RUN)) == RUN


def test_a_project_may_contain_slashes():
    """``gh/peeringdb/peeringdb`` is one project, not three path segments —
    which is why the key is split from both ends rather than by count."""
    key = RunKey.parse("git.20c.com/gh/peeringdb/peeringdb/develop-1")
    assert key == RunKey("git.20c.com", "gh/peeringdb/peeringdb", "develop-1")


def test_something_that_is_not_a_run_key_is_refused():
    with pytest.raises(ValueError):
        RunKey.parse("git.20c.com/agents")


def test_a_state_row_becomes_a_key():
    row = {"host": "git.20c.com", "project": "agents/global",
           "slug": "develop-327", "attention": {"reason": "question"}}
    assert RunKey.from_row(row) == RUN


# --- binding and resolving ---------------------------------------------------

def test_a_bound_thread_resolves_in_both_directions(path):
    threads = ThreadMap.load(path)
    threads.bind(ROOT, RUN)
    assert threads.run_for(ROOT) == RUN
    assert threads.root_for(RUN) == ROOT


def test_an_unknown_root_resolves_to_nothing(path):
    """Never a guess: a message in a thread the bridge does not know is logged
    and ignored, and that decision starts here."""
    threads = ThreadMap.load(path)
    threads.bind(ROOT, RUN)
    assert threads.run_for("$some-other-thread") is None
    assert threads.root_for(OTHER_RUN) is None


def test_two_runs_keep_two_threads(path):
    threads = ThreadMap.load(path)
    threads.bind(ROOT, RUN)
    threads.bind(OTHER_ROOT, OTHER_RUN)
    assert threads.run_for(ROOT) == RUN
    assert threads.run_for(OTHER_ROOT) == OTHER_RUN
    assert len(threads) == 2


def test_rebinding_a_run_replaces_its_old_thread(path):
    """A redacted thread, or a room the operator cleared, must not leave a run
    permanently unannounceable — and must not leave a stale root resolving to
    it either."""
    threads = ThreadMap.load(path)
    threads.bind(ROOT, RUN)
    threads.bind(OTHER_ROOT, RUN)
    assert threads.root_for(RUN) == OTHER_ROOT
    assert threads.run_for(ROOT) is None
    assert len(threads) == 1


def test_forgetting_a_run_drops_both_directions(path):
    threads = ThreadMap.load(path)
    threads.bind(ROOT, RUN)
    threads.forget(RUN)
    assert threads.run_for(ROOT) is None
    assert threads.root_for(RUN) is None
    threads.forget(RUN)  # silent when there is nothing to forget


# --- persistence -------------------------------------------------------------

def test_a_binding_survives_a_restart(path):
    ThreadMap.load(path).bind(ROOT, RUN)
    assert ThreadMap.load(path).run_for(ROOT) == RUN


def test_the_binding_is_written_when_it_happens_not_at_shutdown(path):
    """The fact worth keeping is "this run already has a thread", and the moment
    it becomes true is the moment a crash would lose it."""
    threads = ThreadMap.load(path)
    assert not path.exists()
    threads.bind(ROOT, RUN)
    assert path.exists()
    assert json.loads(path.read_text())["threads"] == {ROOT: str(RUN)}


def test_the_file_is_owner_only(path):
    """It is written through the platform's snapshot writer, so it inherits the
    mode every other snapshot has rather than a second implementation's."""
    ThreadMap.load(path).bind(ROOT, RUN)
    assert oct(os.stat(path).st_mode)[-3:] == "600"
    assert oct(os.stat(path.parent).st_mode)[-3:] == "700"


def test_a_missing_file_starts_empty(path):
    threads = ThreadMap.load(path)
    assert len(threads) == 0
    assert threads.run_for(ROOT) is None


def test_a_corrupt_file_starts_empty_rather_than_refusing(path):
    """One duplicate thread per waiting run, against every notification lost
    until someone notices. The store has already moved the bad file aside."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    threads = ThreadMap.load(path)
    assert len(threads) == 0
    threads.bind(ROOT, RUN)
    assert ThreadMap.load(path).run_for(ROOT) == RUN


def test_an_entry_that_is_not_a_run_key_is_dropped_not_fatal(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    store.write_json(path, {"threads": {ROOT: str(RUN), OTHER_ROOT: 42}})
    threads = ThreadMap.load(path)
    assert threads.run_for(ROOT) == RUN
    assert threads.run_for(OTHER_ROOT) is None


def test_a_string_that_is_not_a_run_key_is_dropped_too(path):
    """The case the type filter lets through and the parse used to raise on
    (!243 review): valid JSON, valid types, one hand-edit or one truncated write
    away — and it took startup with it, which is exactly what the load contract
    chose against. One dict value is the whole test."""
    path.parent.mkdir(parents=True, exist_ok=True)
    store.write_json(path, {"threads": {ROOT: str(RUN), OTHER_ROOT: "nope"}})

    threads = ThreadMap.load(path)

    assert threads.run_for(ROOT) == RUN, "the usable entry survived"
    assert threads.run_for(OTHER_ROOT) is None
    assert len(threads) == 1
