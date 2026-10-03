"""The thread-request facility on the supervisor's control plane (issue #414).

What is pinned here is the contract a servicer ships against and the boundary
the session may not cross: a request is claimed once, a result reaches the
asker, the session may queue and collect but never poll or answer, and "no host
side can do this" is an answer rather than a timeout.
"""
from __future__ import annotations

import hashlib
import threading
import time

import pytest

from lmer_cli import supervisor, thread_requests
from lmer_cli.comms import MessageLog
from lmer_cli.thread_requests import (
    MAX_IN_FLIGHT, MAX_RECIPIENT_CHARS, OP_DM, OP_POST, OP_READ, STATE_DONE,
    ThreadRequestError, ThreadRequests,
)
from tests.conftest import strip_lmer_env


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def queue():
    facility = ThreadRequests()
    facility.servicer_seen()
    return facility


class TestTheQueue:
    def test_a_request_with_no_servicer_is_refused_not_queued(self):
        facility = ThreadRequests()
        with pytest.raises(ThreadRequestError) as caught:
            facility.submit(OP_READ, "https://example.slack.com/archives/C1/p1700000000123456")
        assert caught.value.status == 503
        assert "no host-side servicer" in str(caught.value)

    def test_a_servicer_counts_as_attached_only_while_it_keeps_polling(self, monkeypatch):
        facility = ThreadRequests()
        facility.claim(0)
        assert facility.servicer_attached() is True
        moved = time.monotonic() + thread_requests.SERVICER_TTL + 1
        monkeypatch.setattr(thread_requests.time, "monotonic", lambda: moved)
        assert facility.servicer_attached() is False

    def test_a_claimed_request_is_never_offered_again(self, queue):
        request = queue.submit(OP_POST, "https://example.slack.com/archives/C1/p1700000000123456", text="hi")
        first, cursor = queue.claim(0)
        assert [item.id for item in first] == [request.id]
        again, _cursor = queue.claim(0)
        assert again == []
        assert cursor == request.seq

    def test_a_result_reaches_the_waiter_and_is_then_dropped(self, queue):
        request = queue.submit(OP_READ, "https://example.slack.com/archives/C1/p1700000000123456")
        queue.claim(0)
        queue.complete(request.id, {"ok": True, "messages": []})
        done = queue.await_result(request.id, timeout=0)
        assert done.state == STATE_DONE and done.result["ok"] is True
        with pytest.raises(ThreadRequestError) as caught:
            queue.await_result(request.id, timeout=0)
        # 410 and not 404: a 404 on this control plane reads as "this supervisor
        # predates the facility", which would send the asker after the image.
        assert caught.value.status == 410

    def test_a_waiter_wakes_when_the_result_lands(self, queue):
        request = queue.submit(OP_READ, "https://example.slack.com/archives/C1/p1700000000123456")
        queue.claim(0)

        def answer():
            time.sleep(0.05)
            queue.complete(request.id, {"ok": True, "messages": []})

        threading.Thread(target=answer, daemon=True).start()
        done = queue.await_result(request.id, timeout=5)
        assert done.state == STATE_DONE

    def test_requests_in_flight_are_bounded(self, queue):
        for _ in range(MAX_IN_FLIGHT):
            queue.submit(OP_READ, "https://example.slack.com/archives/C1/p1700000000123456")
        with pytest.raises(ThreadRequestError) as caught:
            queue.submit(OP_READ, "https://example.slack.com/archives/C1/p1700000000123456")
        assert caught.value.status == 429

    def test_a_claim_the_servicer_never_answered_frees_its_slot(self, queue, monkeypatch):
        """The failure this cap must not have: a servicer that died holding a
        request would otherwise burn one of the eight slots for the life of the
        supervisor, and the refusal tells the agent to wait for one to finish."""
        for _ in range(MAX_IN_FLIGHT):
            queue.submit(OP_READ, "https://example.slack.com/archives/C1/p1700000000123456")
        assert len(queue.claim(0)[0]) == MAX_IN_FLIGHT
        with pytest.raises(ThreadRequestError):
            queue.submit(OP_READ, "https://example.slack.com/archives/C1/p1700000000123456")
        moved = time.monotonic() + thread_requests.RESULT_TTL + 1
        monkeypatch.setattr(thread_requests.time, "monotonic", lambda: moved)
        # A fresh servicer polling is what a restarted daemon looks like: it
        # sweeps what the dead one was holding and the session can ask again.
        assert queue.claim(0)[0] == []
        assert queue.submit(OP_READ, "https://example.slack.com/archives/C1/p1700000000123456")
        assert queue.health()["in_flight"] == 1

    def test_a_request_in_hand_vouches_for_the_servicer_holding_it(self, queue, monkeypatch):
        """A servicer answers its claimed batch serially, so between two polls it
        can spend minutes on one thread's files. Nothing polls in that window and
        the session would otherwise be told nothing can reach Slack."""
        queue.submit(OP_READ, "https://example.slack.com/archives/C1/p1700000000123456")
        queue.claim(0)
        moved = time.monotonic() + thread_requests.SERVICER_TTL + 1
        monkeypatch.setattr(thread_requests.time, "monotonic", lambda: moved)
        assert queue.servicer_attached() is True
        # But not forever: past RESULT_TTL the claim is what the sweep drops.
        later = time.monotonic() + thread_requests.RESULT_TTL + 1
        monkeypatch.setattr(thread_requests.time, "monotonic", lambda: later)
        assert queue.servicer_attached() is False

    def test_an_answer_counts_as_the_servicer_showing_itself(self, queue, monkeypatch):
        """The other half of attendance: after the batch, before the next poll."""
        request = queue.submit(OP_READ, "https://example.slack.com/archives/C1/p1700000000123456")
        queue.claim(0)
        moved = time.monotonic() + thread_requests.SERVICER_TTL + 1
        monkeypatch.setattr(thread_requests.time, "monotonic", lambda: moved)
        queue.complete(request.id, {"ok": True})
        assert queue.servicer_attached() is True
        # And it does age: nothing has been heard from since that answer.
        later = moved + thread_requests.SERVICER_TTL + 1
        monkeypatch.setattr(thread_requests.time, "monotonic", lambda: later)
        assert queue.servicer_attached() is False

    def test_uncollected_results_are_bounded_in_number_not_only_in_age(self, queue):
        """A `lmer-signal` killed mid-wait leaves its result behind, and a read's
        result is the thread it read. Age alone let a session's queue grow to
        hundreds of them inside one RESULT_TTL."""
        for _ in range(50):
            ids = [queue.submit(OP_READ, "https://example.slack.com/archives/C1/p1700000000123456").id
                   for _ in range(MAX_IN_FLIGHT)]
            queue.claim(0)
            for request_id in ids:
                queue.complete(request_id, {"ok": True, "messages": [{"text": "x"}]})
        held = len(queue._requests)
        assert held <= thread_requests.MAX_RETAINED_RESULTS + MAX_IN_FLIGHT
        assert queue.health()["in_flight"] == 0

    def test_an_attachment_carries_only_what_the_host_side_reads(self, queue):
        """Everything kept leaves the process — into a thread, or onto a host
        path — so a key nobody reads is a key nobody bounds."""
        request = queue.submit(
            OP_POST, "https://example.slack.com/archives/C1/p1700000000123456", text="here",
            attachments=[{"name": "a.txt", "path": "/u/a.txt", "media_type": "text/plain",
                          "bytes": 3, "junk": "x" * 1000}],
        )
        assert request.attachments == [
            {"name": "a.txt", "path": "/u/a.txt", "media_type": "text/plain", "bytes": 3}
        ]

    @pytest.mark.parametrize("attachment", [
        {"path": "/u/a\x00b"},
        {"media_type": "text/plain\nX-Evil: y"},
        {"media_type": "not-a-media-type"},
        {"bytes": -1},
        {"name": 7},
    ])
    def test_an_unusable_attachment_field_is_refused_here(self, queue, attachment):
        """Refused where a refusal is what this returns: a NUL in the path makes
        `os.open` raise ValueError, which is not what the host side's refusal
        path catches — it would unwind past it after the post had landed."""
        with pytest.raises(ThreadRequestError):
            queue.submit(
                OP_POST, "https://example.slack.com/archives/C1/p1700000000123456",
                text="here", attachments=[attachment],
            )

    def test_an_uncollected_result_ages_out(self, queue, monkeypatch):
        request = queue.submit(OP_READ, "https://example.slack.com/archives/C1/p1700000000123456")
        queue.claim(0)
        queue.complete(request.id, {"ok": True})
        moved = time.monotonic() + thread_requests.RESULT_TTL + 1
        monkeypatch.setattr(thread_requests.time, "monotonic", lambda: moved)
        queue.claim(0)
        with pytest.raises(ThreadRequestError):
            queue.await_result(request.id, timeout=0)


# ---------------------------------------------------------------------------
# The routes, and the boundary the session may not cross
# ---------------------------------------------------------------------------


class TestDirectMessages:
    """`dm` on the queue (issue #441): addressed by person, never by thread."""

    def test_a_dm_carries_its_recipient_to_the_servicer(self, queue):
        request = queue.submit(OP_DM, recipient="U0123ABC", text="the password is in your DMs")
        assert request.op == OP_DM
        assert request.recipient == "U0123ABC"
        assert request.permalink == ""
        # The servicer reads the request as a dict, so the field has to survive
        # that crossing — it is the only thing saying who the message is for.
        assert request.to_dict()["recipient"] == "U0123ABC"

    def test_a_dm_without_a_recipient_is_refused(self, queue):
        for missing in ("", "   "):
            with pytest.raises(ThreadRequestError) as caught:
                queue.submit(OP_DM, recipient=missing, text="hi")
            assert "needs a recipient" in str(caught.value)

    @pytest.mark.parametrize("field", ["permalink", "channel"])
    def test_a_dm_that_also_names_a_thread_or_channel_is_refused(self, queue, field):
        """Two addresses is not a detail to resolve: one of the answers is a
        room other people can read, and this message is the one that must not
        go there."""
        kwargs = {"recipient": "U0123ABC", "text": "hi"}
        if field == "permalink":
            kwargs["permalink"] = "https://example.slack.com/archives/C1/p1700000000123456"
        else:
            kwargs["channel"] = "#general"
        with pytest.raises(ThreadRequestError) as caught:
            queue.submit(OP_DM, **kwargs)
        assert "not a thread or a channel" in str(caught.value)

    @pytest.mark.parametrize("recipient", [
        "U0123 ABC", "U0123\tABC", "U0123\x00ABC", "U0123\nABC",
    ])
    def test_a_recipient_carrying_whitespace_or_control_characters_is_refused(self, queue, recipient):
        with pytest.raises(ThreadRequestError) as caught:
            queue.submit(OP_DM, recipient=recipient, text="hi")
        assert "whitespace or control characters" in str(caught.value)

    def test_a_recipient_is_bounded(self, queue):
        with pytest.raises(ThreadRequestError) as caught:
            queue.submit(OP_DM, recipient="U" * (MAX_RECIPIENT_CHARS + 1), text="hi")
        assert str(MAX_RECIPIENT_CHARS) in str(caught.value)

    def test_a_recipient_on_another_op_is_dropped(self, queue):
        """Only `dm` is addressed by person. A recipient riding on a post would
        be a field the servicer could read and act on for an op whose address is
        its permalink."""
        request = queue.submit(
            OP_POST, "https://example.slack.com/archives/C1/p1700000000123456",
            recipient="U0123ABC", text="hi",
        )
        assert request.recipient == ""


class TestRoutes:
    def _build(self, tmp_path):
        """A plane built the way `run_supervisor` builds one: the control
        credential verified by its hash, the session holding a token of its own."""
        from fastapi.testclient import TestClient

        buf = supervisor.OutputBuffer(limit=1024)
        control = "control-token-value"
        app = supervisor._build_fastapi_app(
            buf, lambda data: len(data), "", comms=MessageLog("sess-1", None),
            agent_token="agent-token-value",
            token_hash=hashlib.sha256(control.encode("utf-8")).hexdigest(),
        )
        return (
            TestClient(app),
            {"Authorization": f"Bearer {control}"},
            {"Authorization": "Bearer agent-token-value"},
        )

    def test_a_request_with_no_servicer_is_a_503_the_cli_reads_as_no_channel(self, tmp_path):
        client, _control, agent = self._build(tmp_path)
        refused = client.post(
            "/thread/requests",
            json={"op": OP_READ, "permalink": "https://example.slack.com/archives/C1/p1700000000123456"},
            headers=agent,
        )
        assert refused.status_code == 503
        assert "no host-side servicer" in refused.json()["detail"]

    def test_a_dm_recipient_survives_the_route_to_the_servicer(self, tmp_path):
        """The field is new on this body (#441), and the servicer reads the
        request the route stored — so a body field the model drops would leave
        the host side with a message and nobody to send it to."""
        client, control, agent = self._build(tmp_path)
        assert client.get("/thread/requests", params={"since": 0}, headers=control).status_code == 200
        queued = client.post(
            "/thread/requests",
            json={"op": OP_DM, "recipient": "@someone:example.org", "text": "the credential"},
            headers=agent,
        )
        assert queued.status_code == 200
        assert queued.json()["request"]["recipient"] == "@someone:example.org"
        claimed = client.get("/thread/requests", params={"since": 0}, headers=control).json()
        assert [item["recipient"] for item in claimed["requests"]] == ["@someone:example.org"]

    def test_a_dm_naming_a_thread_is_refused_by_the_route(self, tmp_path):
        client, control, agent = self._build(tmp_path)
        client.get("/thread/requests", params={"since": 0}, headers=control)
        refused = client.post(
            "/thread/requests",
            json={"op": OP_DM, "recipient": "U0123ABC",
                  "permalink": "https://example.slack.com/archives/C1/p1700000000123456",
                  "text": "hi"},
            headers=agent,
        )
        assert refused.status_code == 400
        assert "not a thread or a channel" in refused.json()["detail"]

    def test_the_session_may_ask_and_collect_but_never_poll_or_answer(self, tmp_path):
        client, control, agent = self._build(tmp_path)
        assert client.get("/thread/requests", params={"since": 0}, headers=control).status_code == 200
        queued = client.post(
            "/thread/requests",
            json={"op": OP_POST, "permalink": "https://example.slack.com/archives/C1/p1700000000123456",
                  "text": "the review is posted"},
            headers=agent,
        )
        assert queued.status_code == 200
        request_id = queued.json()["request"]["id"]
        # The session's own token opens neither the servicer's poll nor its answer:
        # both would let the container decide what a thread was told.
        assert client.get("/thread/requests", headers=agent).status_code == 403
        assert client.post(
            f"/thread/requests/{request_id}/result", json={"result": {"ok": True}}, headers=agent,
        ).status_code == 403
        claimed = client.get("/thread/requests", params={"since": 0}, headers=control).json()
        assert [item["id"] for item in claimed["requests"]] == [request_id]
        assert client.post(
            f"/thread/requests/{request_id}/result",
            json={"result": {"ok": True, "posted": {"ref": "C1:1.2:3.4"}}},
            headers=control,
        ).status_code == 200
        collected = client.get(f"/thread/requests/{request_id}", headers=agent).json()
        assert collected["request"]["result"]["posted"]["ref"] == "C1:1.2:3.4"

    def test_a_collected_result_is_gone_and_says_so(self, tmp_path):
        client, control, agent = self._build(tmp_path)
        client.get("/thread/requests", headers=control)
        queued = client.post(
            "/thread/requests",
            json={"op": OP_READ, "permalink": "https://example.slack.com/archives/C1/p1700000000123456"},
            headers=agent,
        ).json()["request"]
        client.get("/thread/requests", headers=control)
        client.post(
            f"/thread/requests/{queued['id']}/result", json={"result": {"ok": True, "messages": []}},
            headers=control,
        )
        assert client.get(f"/thread/requests/{queued['id']}", headers=agent).status_code == 200
        gone = client.get(f"/thread/requests/{queued['id']}", headers=agent)
        assert gone.status_code == 410

    def test_healthz_reports_whether_anything_can_serve_a_thread(self, tmp_path):
        client, control, _agent = self._build(tmp_path)
        assert client.get("/healthz", headers=control).json()["threads"]["servicer"] is False
        client.get("/thread/requests", headers=control)
        health = client.get("/healthz", headers=control).json()["threads"]
        assert health["servicer"] is True and health["in_flight"] == 0

    def test_a_permalink_is_bounded_before_anything_is_queued(self, tmp_path):
        client, control, agent = self._build(tmp_path)
        client.get("/thread/requests", headers=control)
        refused = client.post(
            "/thread/requests", json={"op": OP_READ, "permalink": "https://x/y z"}, headers=agent,
        )
        assert refused.status_code == 400
        assert "whitespace" in refused.json()["detail"]
        assert client.post(
            "/thread/requests", json={"op": "delete", "permalink": "https://x/y"}, headers=agent,
        ).status_code == 400
