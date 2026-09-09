"""Threads a run is pointed at, without being connected to them (issue #414).

A reference connects nothing: it is a permalink and a title attached to a run,
which the run is told about once and then reads or posts with `lmer-signal`.
What is pinned here is that shape — platform state per run, the routes over it,
the one-off announcement, and the fleet payload the menu draws from.
"""
from __future__ import annotations

import base64
import os

import pytest
from fastapi.testclient import TestClient

from lmer_platform import (
    api, references, registry, runs, spawn, store, thread_service,
)
from lmer_platform import config as cfg
from lmer_cli.threads import ThreadError
from tests.conftest import strip_lmer_env

SECRET = "test-secret-value"
SLACK_LINK = "https://20c.slack.com/archives/C0123ABCDEF/p1712345678000900"
MATRIX_LINK = "https://matrix.to/#/!room:example.org/$root"
RUN = ("gitlab.example.com", "agents/global", "develop-414")


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
    return tmp_path / "platform"


@pytest.fixture
def tracked(platform_root):
    runs.track(*RUN)
    return RUN


def register(session_id="s-1"):
    spawn.token_file_for(session_id).parent.mkdir(parents=True, exist_ok=True)
    spawn.token_file_for(session_id).write_text("control-token", encoding="utf-8")
    registry.register(
        session_id, kind="worker", pid=os.getpid(),
        run={"host": RUN[0], "project": RUN[1], "slug": RUN[2]},
        control={"host": "127.0.0.1", "port": 8123,
                 "token_ref": str(spawn.token_file_for(session_id))},
        started_at="2026-09-05T09:00:00Z",
    )
    return session_id


@pytest.fixture
def client(platform_root):
    return TestClient(api.create_app(cfg.load(), SECRET))


def auth(secret=SECRET):
    raw = f":{secret}".encode("utf-8")
    return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}


class TestTheStore:
    def test_a_reference_keeps_the_thread_the_transport_names(self, tracked):
        record = references.add(*RUN, SLACK_LINK, title="the design thread", by="vegu")
        assert record.channel == "slack"
        assert record.title == "the design thread"
        assert record.added_by == "vegu"
        assert references.listing(*RUN)[0]["permalink"] == record.permalink

    def test_without_a_title_the_link_supplies_one(self, tracked):
        record = references.add(*RUN, MATRIX_LINK)
        assert record.title == "!room:example.org"

    def test_the_same_thread_twice_is_one_reference(self, tracked):
        first = references.add(*RUN, SLACK_LINK, title="one")
        second = references.add(*RUN, SLACK_LINK, title="two")
        assert first.id == second.id
        listed = references.listing(*RUN)
        assert len(listed) == 1 and listed[0]["title"] == "two"

    def test_a_run_this_orchestrator_does_not_track_is_refused(self, platform_root):
        with pytest.raises(references.ReferenceRunNotTracked):
            references.add(*RUN, SLACK_LINK)

    def test_a_link_that_names_no_thread_is_refused(self, tracked):
        with pytest.raises(ThreadError):
            references.add(*RUN, "https://example.org/not-a-thread")

    def test_forgetting_the_run_takes_its_references(self, tracked):
        references.add(*RUN, SLACK_LINK)
        runs.forget(*RUN)
        assert references.listing(*RUN) == []


class TestTheRoutes:
    def test_add_list_and_remove_through_the_run_behind_a_session(self, client, tracked):
        session = register()
        added = client.post(
            f"/api/sessions/{session}/references",
            json={"permalink": SLACK_LINK, "title": "the design thread"}, headers=auth(),
        )
        assert added.status_code == 201, added.text
        reference = added.json()["reference"]
        assert reference["announced"] is False
        assert "connects nothing" in added.json()["note"]

        listed = client.get(f"/api/sessions/{session}/references", headers=auth()).json()
        assert [item["id"] for item in listed["references"]] == [reference["id"]]
        assert listed["run"]["slug"] == "develop-414"

        gone = client.delete(
            f"/api/sessions/{session}/references/{reference['id']}", headers=auth(),
        )
        assert gone.status_code == 200
        assert client.get(
            f"/api/sessions/{session}/references", headers=auth()
        ).json()["references"] == []

    def test_a_run_this_orchestrator_does_not_track_is_a_404_that_says_so(
        self, client, platform_root
    ):
        session = register("s-untracked")
        refused = client.post(
            f"/api/sessions/{session}/references", json={"permalink": SLACK_LINK}, headers=auth(),
        )
        assert refused.status_code == 404
        assert "not tracked by this orchestrator" in refused.json()["detail"]

    def test_a_session_with_no_run_at_all_is_a_404_naming_why(self, client, platform_root):
        spawn.token_file_for("s-assistant").parent.mkdir(parents=True, exist_ok=True)
        registry.register(
            "s-assistant", kind="assistant", pid=os.getpid(),
            started_at="2026-09-05T09:00:00Z",
        )
        refused = client.post(
            "/api/sessions/s-assistant/references",
            json={"permalink": SLACK_LINK}, headers=auth(),
        )
        assert refused.status_code == 404
        assert "reference thread is attached to a run" in refused.json()["detail"]

    def test_a_bad_permalink_is_refused_before_anything_is_stored(self, client, tracked):
        session = register()
        refused = client.post(
            f"/api/sessions/{session}/references",
            json={"permalink": "https://example.org/nope"}, headers=auth(),
        )
        assert refused.status_code == 400
        assert references.listing(*RUN) == []

    def test_the_fleet_payload_carries_them_on_the_run(self, client, tracked, monkeypatch):
        register()
        references.add(*RUN, SLACK_LINK, title="the design thread")
        payload = {"runs": [{"run": {"host": RUN[0], "project": RUN[1], "slug": RUN[2]}}]}
        api._fold_references(payload)
        assert [item["title"] for item in payload["runs"][0]["references"]] == [
            "the design thread"
        ]


class TestTheAnnouncement:
    def test_a_run_is_told_once_and_the_record_says_how_to_use_it(self, tracked, monkeypatch):
        session = register()
        reference = references.add(*RUN, SLACK_LINK, title="the design thread", by="vegu")
        posted = []

        class _Hub:
            def post(self, session_id, **kw):
                posted.append((session_id, kw))
                return {}

        monkeypatch.setattr("lmer_platform.comms.ensure_hub", lambda: _Hub())
        services = thread_service.ThreadServices(environ={})
        assert services.announce({session}) == 1
        session_id, kw = posted[0]
        assert session_id == session and kw["channel"] == "platform"
        assert kw["text"] == (
            'reference thread "the design thread": '
            f'{reference.permalink} — lmer-signal read / post --thread'
        )
        assert kw["ref"] == f"reference:{reference.id}"
        # Once: a second tick finds nothing pending.
        assert services.announce({session}) == 0
        assert references.listing(*RUN)[0]["announced"] is True

    def test_a_session_whose_bus_is_not_up_is_told_on_a_later_tick(self, tracked, monkeypatch):
        session = register()
        references.add(*RUN, SLACK_LINK)
        failures = {"n": 0}

        class _Hub:
            def post(self, session_id, **kw):
                failures["n"] += 1
                if failures["n"] == 1:
                    raise RuntimeError("no message bus")
                return {}

        monkeypatch.setattr("lmer_platform.comms.ensure_hub", lambda: _Hub())
        services = thread_service.ThreadServices(environ={})
        assert services.announce({session}) == 0
        assert references.listing(*RUN)[0]["announced"] is False
        assert services.announce({session}) == 1
        assert references.listing(*RUN)[0]["announced"] is True


class TestReferencesAtSpawn:
    def test_a_title_over_the_limit_is_refused_before_the_container_starts(self):
        """Caught at the point of storage it is caught after the session is up,
        where the only thing left to do with it is log it and lose the thread."""
        from lmer_platform.spawn import SpawnError, SpawnRequest

        with pytest.raises(SpawnError) as caught:
            SpawnRequest(
                taskdef="develop", target="https://git.example.com/g/p/-/issues/1",
                references=[{"permalink": SLACK_LINK, "title": "T" * 200}],
            ).validate()
        assert "title" in str(caught.value)

    def test_a_reference_lost_after_the_spawn_is_reported_as_a_warning(self, platform_root):
        """A 201 with nothing said is how the operator's context thread goes
        missing; SpawnResult.warning exists for exactly this loss."""
        from lmer_platform.spawn import SpawnRequest, _write_run_references

        request = SpawnRequest(
            taskdef="develop", target="https://git.example.com/g/p/-/issues/1",
            references=[{"permalink": SLACK_LINK}],
        )
        # The run is not tracked here, which is what `references.add` refuses.
        warning = _write_run_references(request, *RUN)
        assert warning is not None and SLACK_LINK in warning

    """A run can start knowing its context threads (issue #414)."""

    def test_the_request_refuses_an_unusable_list_before_anything_starts(self):
        from lmer_platform.spawn import SpawnError, SpawnRequest

        base = dict(taskdef="develop", target="https://git.example.com/g/p/-/issues/1")
        for bad in ("not-a-list", [{"title": "no link"}], [{"permalink": "https://x/y"}]):
            with pytest.raises(SpawnError):
                SpawnRequest(**base, references=bad).validate()

    def test_a_well_formed_list_passes_validation(self):
        from lmer_platform.spawn import SpawnRequest

        request = SpawnRequest(
            taskdef="develop", target="https://git.example.com/g/p/-/issues/1",
            references=[{"permalink": SLACK_LINK, "title": "the design thread"}],
        ).validate()
        assert request.references[0]["title"] == "the design thread"

    def test_they_are_attached_to_the_run_once_it_is_tracked(self, tracked):
        from lmer_platform.spawn import SpawnRequest, _write_run_references

        request = SpawnRequest(
            taskdef="develop", target="https://git.example.com/g/p/-/issues/1",
            references=[{"permalink": SLACK_LINK, "title": "the design thread"}],
        )
        _write_run_references(request, *RUN)
        listed = references.listing(*RUN)
        assert [item["title"] for item in listed] == ["the design thread"]
        # Not yet told: there is no session to tell until the spawn has one.
        assert listed[0]["announced"] is False

    def test_one_refused_reference_does_not_fail_a_running_session(self, tracked, caplog):
        from lmer_platform.spawn import SpawnRequest, _write_run_references

        request = SpawnRequest(
            taskdef="develop", target="https://git.example.com/g/p/-/issues/1",
            references=[{"permalink": "https://example.org/nope"}, {"permalink": SLACK_LINK}],
        )
        _write_run_references(request, *RUN)
        assert [item["channel"] for item in references.listing(*RUN)] == ["slack"]
