"""The daemon's servicers: one per live session, and what only it can supply (#414).

The component itself is tested in ``test_lmer_thread_access.py``; what is pinned
here is the daemon's side of it — a servicer follows its session, it is given the
run's title as a callable so a run named later still posts under its name, and a
session with no control plane gets none rather than a servicer that spins.
"""
from __future__ import annotations

import os

import pytest

from lmer_platform import meta, registry, runs, spawn, store, thread_service
from tests.conftest import strip_lmer_env


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
    return tmp_path / "platform"


def register(session_id, *, control=True):
    entry = {
        "kind": "worker", "pid": os.getpid(),
        "run": {"host": "gitlab.example.com", "project": "agents/global", "slug": "develop-414"},
        "started_at": "2026-09-05T09:00:00Z",
    }
    if control:
        spawn.token_file_for(session_id).parent.mkdir(parents=True, exist_ok=True)
        spawn.token_file_for(session_id).write_text("control-token", encoding="utf-8")
        entry["control"] = {
            "host": "127.0.0.1", "port": 8123,
            "token_ref": str(spawn.token_file_for(session_id)),
        }
    registry.register(session_id, **entry)


class _Servicer:
    """A stand-in for the real component: started and stopped, never polling."""

    instances: list = []

    def __init__(self, **kw):
        self.kw = kw
        self.started = False
        self.stop_event = _Event()
        _Servicer.instances.append(self)

    def start(self):
        self.started = True

    def is_alive(self):
        return self.started and not self.stop_event.set_


class _Event:
    def __init__(self):
        self.set_ = False

    def set(self):
        self.set_ = True

    def is_set(self):
        return self.set_


@pytest.fixture
def servicers(monkeypatch):
    _Servicer.instances = []
    monkeypatch.setattr(thread_service, "ThreadServicer", _Servicer)
    return _Servicer.instances


class TestTheSet:
    def test_a_live_session_with_a_control_plane_gets_one(self, platform_root, servicers):
        register("s-1")
        services = thread_service.ThreadServices(environ={"SLACK_BOT_TOKEN": "xoxb-test"})
        services.reconcile()
        assert services.sessions() == ["s-1"]
        assert servicers[0].started is True
        assert servicers[0].kw["supervisor_url"] == "http://127.0.0.1:8123"
        assert servicers[0].kw["token"] == "control-token"

    def test_a_second_tick_does_not_start_a_second_servicer(self, platform_root, servicers):
        register("s-1")
        services = thread_service.ThreadServices(environ={})
        services.reconcile()
        services.reconcile()
        assert len(servicers) == 1

    def test_a_servicer_that_died_is_replaced_on_the_next_tick(self, platform_root, servicers):
        """A servicer ends on its own when the supervisor does not answer inside
        its connect budget. Left in the dict it makes every later tick a no-op,
        and the session loses both verbs for the rest of its life while the
        daemon still lists it as serviced."""
        register("s-1")
        services = thread_service.ThreadServices(environ={})
        services.reconcile()
        servicers[0].stop_event.set()
        services.reconcile()
        assert len(servicers) == 2 and servicers[1].started is True
        assert services.sessions() == ["s-1"]

    def test_a_session_that_ends_loses_its_servicer(self, platform_root, servicers):
        register("s-1")
        services = thread_service.ThreadServices(environ={})
        services.reconcile()
        registry.remove("s-1", force=True)
        services.reconcile()
        assert services.sessions() == []
        assert servicers[0].stop_event.is_set() is True

    def test_a_session_with_no_control_plane_gets_none_and_is_asked_again(
        self, platform_root, servicers
    ):
        register("s-2", control=False)
        services = thread_service.ThreadServices(environ={})
        services.reconcile()
        assert services.sessions() == [] and servicers == []
        register("s-2")
        services.reconcile()
        assert services.sessions() == ["s-2"]


class TestTheLabel:
    def test_a_run_named_after_it_starts_still_posts_under_its_name(
        self, platform_root, servicers
    ):
        register("s-1")
        runs.track("gitlab.example.com", "agents/global", "develop-414")
        services = thread_service.ThreadServices(environ={})
        services.reconcile()
        label = servicers[0].kw["label"]
        assert label() == "develop-414"
        meta.write("gitlab.example.com", "agents/global", "develop-414", title="the #414 run")
        assert label() == "the #414 run"

    def test_the_label_carries_no_repository_coordinates(self, platform_root, servicers):
        """This string is read by whoever is in the thread, which may be another
        company's workspace. The refusal wording an operator reads in their own
        fleet view names host/project/slug; this one must not."""
        register("s-1")
        runs.track("gitlab.example.com", "agents/global", "develop-414")
        services = thread_service.ThreadServices(environ={})
        services.reconcile()
        label = servicers[0].kw["label"]()
        assert "gitlab.example.com" not in label and "agents/global" not in label
