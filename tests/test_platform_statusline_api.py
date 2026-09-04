"""What the two routes that serve a session's status document carry (issue #248).

Three surfaces, two reads: the run view and the run list both take the strip off
the *fleet* payload's row (a request per row is the wrong trade for a list
polled from a phone — the reasoning that puts a run's questions there), and the
chat pane takes it off ``GET /api/assistant``, which it already polls.

Pinned here: the field reaches both payloads, it is keyed to the *right*
session, and a run with nothing to report carries ``None`` rather than an empty
object a client would have to tell from a real reading.
"""

import base64
import json
import os
import threading

import pytest
from fastapi.testclient import TestClient

from lmer_platform import api, assistant, inventory, registry, statusline, store
from lmer_platform import config as cfg
from tests.conftest import strip_lmer_env
from tests.test_platform_inventory import plant_run, session, state_yaml

SECRET = "test-secret-value"

#: A pid nothing on the host answers for, so the entry reads as crashed.
_DEAD_PID = 2 ** 22

STATUS = {
    "harness": "claude",
    "updated": "2026-09-03T14:31:07Z",
    "age_seconds": 3.0,
    "context": {"percent": 9},
    "limits": [{"key": "five_hour", "label": "5h", "percent": 24}],
    "effort": {"level": "high"},
}


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


@pytest.fixture
def config(platform_root):
    return cfg.load()


@pytest.fixture
def client(config):
    return TestClient(api.create_app(config, SECRET))


def auth():
    raw = f":{SECRET}".encode("utf-8")
    return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}


def publish(session_id, document=None):
    """Put a document where *session_id* would have published it."""
    directory = statusline.prepare_status_dir(session_id)
    assert directory is not None
    (directory / statusline.STATUS_FILENAME).write_text(json.dumps(document or {
        "schema": 1,
        "harness": "claude",
        "updated": "2026-09-03T14:31:07Z",
        "context": {"percent": 9},
        "limits": [{"key": "five_hour", "label": "5h", "percent": 24}],
        "effort": {"level": "high"},
    }))
    return directory


class TestTheRunRow:
    def test_the_row_carries_the_live_sessions_status(self, tmp_path):
        ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
        entry = session("r1")
        inv = inventory.build_inventory(
            [ref], [entry], statusline={entry["id"]: STATUS},
        )
        assert inv.runs[0].to_dict()["statusline"] == STATUS

    def test_a_run_with_nothing_published_carries_none(self, tmp_path):
        """``None`` rather than ``{}``: a client tests the object to decide
        whether to draw a strip at all, and an empty mapping is truthy in
        JavaScript — it would put a blank strip on every row."""
        ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
        inv = inventory.build_inventory([ref], [session("r1")])
        assert inv.runs[0].to_dict()["statusline"] is None

    def test_a_dormant_run_carries_none(self, tmp_path):
        ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="complete"))
        inv = inventory.build_inventory([ref], [], statusline={"s-r1": STATUS})
        assert inv.runs[0].to_dict()["statusline"] is None

    def test_it_is_keyed_to_the_runs_own_session(self, tmp_path):
        """Two live runs, one publishing: the other must not borrow it."""
        refs = [
            plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress")),
            plant_run(tmp_path, "r2", state_yaml=state_yaml(status="in-progress")),
        ]
        entries = [session("r1"), session("r2")]
        inv = inventory.build_inventory(
            refs, entries, statusline={entries[0]["id"]: STATUS},
        )
        rows = {run.slug: run.to_dict()["statusline"] for run in inv.runs}
        assert rows == {"r1": STATUS, "r2": None}

    def test_a_session_whose_run_dir_has_not_landed_yet_carries_it_too(self, tmp_path):
        """The first minutes of a run are exactly when the numbers are moving,
        and that row is built from the session entry alone."""
        entry = session("brand-new")
        inv = inventory.build_inventory([], [entry], statusline={entry["id"]: STATUS})
        assert inv.runs[0].to_dict()["statusline"] == STATUS

    def test_the_field_is_absent_from_no_row(self, tmp_path):
        """Always present, so a client renders one shape rather than branching
        on whether this daemon sends the key."""
        ref = plant_run(tmp_path, "r1", state_yaml=state_yaml(status="in-progress"))
        inv = inventory.build_inventory([ref], [session("r1", live=False)])
        for row in inv.to_dict()["runs"]:
            assert "statusline" in row


class TestTheFleetPayload:
    """Through the route, because the fleet path is where the read is decided:
    the daemon looks the documents up itself there (nothing hands them in) and
    filters them by liveness on the way."""

    def register(self, session_id, *, pid):
        registry.register(
            session_id,
            pid=pid,
            run={"host": "gitlab.example.com", "project": "agents/global",
                 "slug": "r1"},
            task={"taskdef": "develop", "target": "issue-248"},
            started_at="2026-09-03T14:00:00Z",
        )
        return session_id

    def test_state_serves_what_a_live_session_published(self, client):
        session_id = self.register("s-live", pid=os.getpid())
        publish(session_id)
        reply = client.get("/api/state", headers=auth()).json()
        rows = [row for row in reply["runs"] if row["slug"] == "r1"]
        assert rows and rows[0]["statusline"]["context"] == {"percent": 9}
        assert rows[0]["statusline"]["limits"][0]["label"] == "5h"

    def test_a_dead_sessions_document_is_not_served(self, client):
        """Live only: a document outliving its session describes nobody. The
        entry itself stays — it is the crash signal."""
        session_id = self.register("s-dead", pid=_DEAD_PID)
        publish(session_id)
        reply = client.get("/api/state", headers=auth()).json()
        rows = [row for row in reply["runs"] if row["slug"] == "r1"]
        assert rows and rows[0]["statusline"] is None

    def test_the_api_index_says_the_field_is_there(self, client):
        text = client.get("/api", headers=auth()).text
        assert "statusline" in text


class TestTheAssistantRoute:
    def test_it_carries_the_running_incarnations_status(self, monkeypatch, client):
        monkeypatch.setattr(
            assistant, "_live_assistant",
            lambda: {"id": "s-uber", "pid": 4242, "kind": assistant.KIND,
                     "started_at": "2026-09-03T14:00:00Z"},
        )
        publish("s-uber")
        reply = client.get("/api/assistant", headers=auth()).json()
        assert reply["statusline"]["effort"] == {"level": "high"}
        assert reply["statusline"]["context"] == {"percent": 9}

    def test_nothing_running_carries_none(self, monkeypatch, client):
        monkeypatch.setattr(assistant, "_live_assistant", lambda: None)
        reply = client.get("/api/assistant", headers=auth()).json()
        assert reply["statusline"] is None

    def test_a_running_incarnation_that_publishes_nothing_carries_none(
        self, monkeypatch, client,
    ):
        """codex and pi publish no document, and that reads as no strip rather
        than as a failure."""
        monkeypatch.setattr(
            assistant, "_live_assistant",
            lambda: {"id": "s-uber", "pid": 4242, "kind": assistant.KIND,
                     "started_at": "2026-09-03T14:00:00Z"},
        )
        reply = client.get("/api/assistant", headers=auth()).json()
        assert reply["statusline"] is None

    def test_the_field_is_always_in_the_reply(self, monkeypatch, client):
        monkeypatch.setattr(assistant, "_live_assistant", lambda: None)
        assert "statusline" in client.get("/api/assistant", headers=auth()).json()

    def test_the_status_is_read_outside_the_publication_lock(self, monkeypatch):
        """``status()``'s docstring promises the lock covers only two adjacent
        writes, and ``_memory_snapshot()`` sits outside it for that reason. This
        read is of a container-written file: inside the lock, a poll that stalls
        on one would block starting or stopping uber lmer.
        """
        monkeypatch.setattr(
            assistant, "_live_assistant",
            lambda: {"id": "s-uber", "pid": 4242, "kind": assistant.KIND,
                     "started_at": "2026-09-03T14:00:00Z"},
        )
        held = []

        def observe(session_id, **kwargs):
            # Another thread, and that is load-bearing: ``_PUBLICATION_LOCK``
            # is an RLock, so a same-thread acquire would pass either way.
            # Non-blocking, so the test can never hang.
            def probe():
                free = assistant._PUBLICATION_LOCK.acquire(blocking=False)
                if free:
                    assistant._PUBLICATION_LOCK.release()
                held.append(not free)

            prober = threading.Thread(target=probe)
            prober.start()
            prober.join(timeout=5)
            return None

        monkeypatch.setattr(statusline, "for_session", observe)
        assistant.status()
        assert held == [False], "the status read happened while holding the lock"

    def test_reading_the_status_writes_nothing(self, monkeypatch, client):
        """:func:`lmer_platform.assistant.status` is side-effect free, and this
        addition is a file read on the same path."""
        monkeypatch.setattr(
            assistant, "_live_assistant",
            lambda: {"id": "s-uber", "pid": 4242, "kind": assistant.KIND,
                     "started_at": "2026-09-03T14:00:00Z"},
        )
        directory = publish("s-uber")
        before = sorted(path.name for path in directory.iterdir())
        client.get("/api/assistant", headers=auth())
        assert sorted(path.name for path in directory.iterdir()) == before
