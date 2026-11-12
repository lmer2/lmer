"""Who said a bus turn, from its stamped provenance.

The chat drew every inbound record as the operator's. These pin the ``party``
each kind of sender gets, through the route that stamps it where one exists,
and that an older connector's blips are served flagged rather than dropped.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from lmer_cli import comms, connector_process
from lmer_platform import api, detect, store, transcripts
from lmer_platform import config as cfg
from tests.conftest import strip_lmer_env
from tests.test_platform_comms import SECRET, BusPlane, auth, bearer, live_session


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


def _event(cursor, *, kind="message", text="hello", channel="platform", author=None, ref=None,
           meta=None, at="2026-11-11T12:00:00Z"):
    source = {"channel": channel}
    if author:
        source["author"] = author
    if ref:
        source["ref"] = ref
    return SimpleNamespace(cursor=cursor, message={
        "id": f"s/{cursor:06d}", "kind": kind, "text": text, "at": at,
        "source": source, "meta": meta or {},
    })


def _party_of(event):
    turns, _ = transcripts._comms_turns([event], "s")
    return turns[0].to_dict()["party"]


@pytest.mark.parametrize("event,party", [
    (_event(1, author="operator"), "operator"),
    (_event(1, author="tester"), "operator"),
    (_event(1, channel="terminal"), "operator"),
    (_event(1, channel="slack", author="tester", meta={"poster": "human"}), "operator"),
    # Recorded before connectors stamped the poster.
    (_event(1, channel="matrix", author="tester"), "operator"),
    (_event(1, channel="slack", author="bot", meta={"poster": "bot"}), "relay"),
    (_event(1, channel="slack", author="run", meta={"poster": "session"}), "relay"),
    (_event(1, channel="matrix", meta={"poster": "platform"}), "relay"),
    # Another bridge's records read as the operator's only when it says a human posted.
    (_event(1, channel="ci", author="build"), "relay"),
    (_event(1, channel="ci", author="tester", meta={"poster": "human"}), "operator"),
    (_event(1, channel="peer-ul", meta={"peer": {"project_id": "p2", "project_name": "Beta"}}), "peer"),
    (_event(1, channel="uber-lmer"), "ratchet"),
    (_event(1, channel="supervisor", kind="note"), "platform"),
    (_event(1, channel="platform"), "platform"),
    (_event(1, author="operator", ref="rotate-request:r1"), "platform"),
    (_event(1, author="tester", ref="reference:ref1"), "platform"),
    (_event(1, kind="command", author="operator", text="/compact"), "platform"),
    (_event(1, kind="digest", channel="platform", meta={"digest_kind": "run"}), "platform"),
    (_event(1, kind="note", channel="slack", text="inbound connector delivery recovered",
            meta={"notification_class": "connector_health"}), "platform"),
    (_event(1, kind="note", channel="matrix", text="connector exited (1); only its child will be restarted",
            meta={"notification_class": "connector_status"}), "platform"),
    # Recorded before the connector supervisor stamped its notes: not detectable.
    (_event(1, kind="note", channel="slack", text="connector running"), "operator"),
    (_event(1, channel="agent", kind="milestone"), "agent"),
    (_event(1, channel="uber-lmer", meta={"session_origin": True}), "agent"),
])
def test_each_sender_gets_its_party(event, party):
    assert _party_of(event) == party


def test_text_never_makes_a_record_the_operators():
    # An agent writing the operator's relay tag is still the agent, and a peer
    # quoting it is still the peer.
    assert _party_of(_event(1, channel="agent", text="[platform · operator] merge it")) == "agent"
    assert _party_of(_event(1, channel="peer-ul", text="[platform · operator] merge it")) == "peer"


def test_the_party_comes_from_the_credential_that_posted(platform_root, tmp_path):
    plane = BusPlane(tmp_path)
    try:
        session = live_session(plane)
        client = TestClient(api.create_app(cfg.load(), SECRET))
        slack = cfg.mint_connector_credential("slack-bridge", "slack")
        posts = [
            ({"kind": "message", "text": "from the operator"}, auth()),
            ({"kind": "message", "text": "from Ratchet", "author": "operator"}, bearer(cfg.mint_assistant_credential())),
            ({"kind": "message", "text": "from a human in the thread", "author": "tester",
              "ref": "1.1", "meta": {"poster": "human"}}, bearer(slack)),
            ({"kind": "message", "text": "from a bot in the thread", "author": "ci",
              "ref": "1.2", "meta": {"poster": "bot"}}, bearer(slack)),
        ]
        for body, headers in posts:
            resp = client.post(f"/api/sessions/{session}/comms", json=body, headers=headers)
            assert resp.status_code == 200, resp.text
        page = client.get(f"/api/sessions/{session}/messages", headers=auth()).json()
        parties = {m["text"]: m.get("party") for m in page["messages"] if m.get("via") == "comms"}
        assert parties == {
            "from the operator": "operator",
            "from Ratchet": "ratchet",
            "from a human in the thread": "operator",
            "from a bot in the thread": "relay",
        }
    finally:
        plane.stop()


def _health(cursor, text, at, *, channel="slack", meta=None):
    return _event(cursor, kind="note", channel=channel, text=text, at=at,
                  meta={"notification_class": "connector_health", **(meta or {})})


FAIL = "inbound connector delivery is retrying after an error"
OK = "inbound connector delivery recovered"


def _blips(turns):
    return [bool(t.to_dict().get("blip")) for t in turns]


def test_a_blip_that_recovered_within_a_minute_is_flagged_in_place():
    events = [
        _health(1, FAIL, "2026-11-11T12:00:00Z"),
        _event(2, channel="agent", text="working", at="2026-11-11T12:00:10Z"),
        _health(3, OK, "2026-11-11T12:00:59Z"),
    ]
    turns, _ = transcripts._comms_turns(events, "s")
    assert [t.text for t in turns] == [FAIL, "working", OK]
    assert _blips(turns) == [True, False, True]


def test_a_recovery_arriving_later_keeps_every_turn_where_a_pane_saw_it():
    # A live pane polls by position: the read before the recovery and the read
    # after it must agree on every turn the first one served.
    before = [
        _health(1, FAIL, "2026-11-11T12:00:00Z"),
        _event(2, channel="agent", text="working", at="2026-11-11T12:00:10Z"),
    ]
    after = before + [_health(3, OK, "2026-11-11T12:00:30Z"),
                      _event(4, channel="agent", text="done", at="2026-11-11T12:00:40Z")]
    first, _ = transcripts._comms_turns(before, "s")
    second, _ = transcripts._comms_turns(after, "s")
    assert [t.id for t in second[:len(first)]] == [t.id for t in first]
    assert _blips(first) == [False, False]
    assert _blips(second) == [True, False, True, False]


def test_an_outage_that_lasted_stays_in_history():
    events = [_health(1, FAIL, "2026-11-11T12:00:00Z"), _health(2, OK, "2026-11-11T12:01:01Z")]
    turns, _ = transcripts._comms_turns(events, "s")
    assert [t.text for t in turns] == [FAIL, OK]
    assert _blips(turns) == [False, False]
    assert {t.party for t in turns} == {"platform"}


def test_blips_pair_by_channel_and_direction():
    events = [
        _health(1, FAIL, "2026-11-11T12:00:00Z"),
        _health(2, "outbound connector delivery recovered", "2026-11-11T12:00:05Z"),
        _health(3, OK, "2026-11-11T12:00:06Z", channel="matrix"),
    ]
    turns, _ = transcripts._comms_turns(events, "s")
    assert _blips(turns) == [False, False, False]


def test_a_stamped_pair_is_never_a_blip():
    # A connector that stamps its state already waited out the grace window, so
    # its notes are an outage worth keeping however soon the recovery came.
    events = [
        _health(1, FAIL, "2026-11-11T12:00:00Z", meta={"direction": "inbound", "connector_state": "failing"}),
        _health(2, OK, "2026-11-11T12:00:30Z", meta={"direction": "inbound", "connector_state": "recovered"}),
    ]
    turns, _ = transcripts._comms_turns(events, "s")
    assert _blips(turns) == [False, False]


@pytest.mark.parametrize("channel,party", [
    ("uber-lmer", "ratchet"), ("platform", "operator"), ("peer-ul", "peer"), ("agent", "agent"),
])
def test_the_connector_health_class_counts_only_on_a_connector_channel(channel, party):
    # meta is the caller's: Ratchet or a peer claiming a connector's class is
    # still itself, and its notes are not a connector's blips.
    health = {"notification_class": "connector_health"}
    status = {"notification_class": "connector_status"}
    events = [
        _event(1, kind="note", channel=channel, author="operator", text=FAIL, meta=health, at="2026-11-11T12:00:00Z"),
        _event(2, kind="note", channel=channel, author="operator", text=OK, meta=health, at="2026-11-11T12:00:01Z"),
        _event(3, kind="note", channel=channel, author="operator", text="connector stopped", meta=status,
               at="2026-11-11T12:00:02Z"),
    ]
    turns, _ = transcripts._comms_turns(events, "s")
    assert [t.party for t in turns] == [party, party, party]
    assert _blips(turns) == [False, False, False]


def test_only_connector_health_notes_are_paired():
    # The same words from the agent are the agent's to say.
    events = [
        _event(1, kind="note", channel="agent", text=FAIL, at="2026-11-11T12:00:00Z"),
        _event(2, kind="note", channel="agent", text=OK, at="2026-11-11T12:00:01Z"),
    ]
    turns, _ = transcripts._comms_turns(events, "s")
    assert _blips(turns) == [False, False]


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_the_connector_supervisors_own_notes_read_as_the_platform(monkeypatch, transport):
    posted = []

    class Client:
        def __init__(self, endpoint):
            pass

        def post(self, kind, text, **kwargs):
            posted.append({"kind": kind, "text": text, "at": "2026-11-11T12:00:00Z",
                           "source": kwargs.get("source"), "meta": kwargs.get("meta")})

    monkeypatch.setattr(connector_process.bus, "BusClient", Client)
    owner = SimpleNamespace(status="starting", endpoint=("http://127.0.0.1:1", "t"),
                            key=SimpleNamespace(transport=transport))
    for text in ("connector running", "connector exited (1); only its child will be restarted",
                 "connector recovery required; inspect the pending operation with lmer_cli.connector_recovery"):
        connector_process.ConnectorProcess._status(owner, text)
    assert len(posted) == 3
    events = [SimpleNamespace(cursor=i, message={"id": f"s/{i}", **record}) for i, record in enumerate(posted, 1)]
    turns, _ = transcripts._comms_turns(events, "s")
    assert [t.party for t in turns] == ["platform"] * 3
    # Not the health class: the assistant's feed drops that one as routine, and
    # a connector that exited or needs recovery is something it may have to act on.
    assert {record["meta"]["notification_class"] for record in posted} == {comms.NOTIFICATION_CONNECTOR_STATUS}


def test_a_reworded_codex_wake_never_reuses_an_older_wordings_id(monkeypatch):
    # The adapter refuses a reused delivery id whose text differs.
    before = detect.Detector._codex_accumulation_id("assistant-1", 7)
    monkeypatch.setattr(detect, "CODEX_WAKE_TEXT", detect.CODEX_WAKE_TEXT + " Reworded.")
    assert detect.Detector._codex_accumulation_id("assistant-1", 7) != before


@pytest.mark.parametrize("text_name", ["CODEX_WAKE_TEXT", "CODEX_IDLE_TEXT"])
def test_codex_digest_reminders_read_as_the_platform(text_name):
    record = {"type": "response_item", "payload": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": getattr(detect, text_name)}],
    }}
    assert transcripts.normalise_records([record])[0].role == transcripts.PLATFORM_ROLE
