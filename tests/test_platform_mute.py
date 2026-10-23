"""Muting a run (#458).

A run the operator steers directly stops waking the assistant with its routine
chatter. Pinned from both sides, because the failure that matters is the
opposite one — a muted run that has gone deaf:

- routine records (messages, replies, notes, answers, commands, closes, each
  from the writers that send it as chatter) from a muted run never reach the
  assistant's spool, in projects mode and in the legacy consumer alike; a
  question, a milestone and the platform's own notices from the same run still do
- unmuting resumes delivery from that point and replays nothing
- the operator's decision (scope A): a muted run is left out of ``stale_runs``
  check-ins and its ``stalled`` condition is not spooled, but the condition
  stays in the detector's baseline so unmuting does not replay it either, and
  every other detector digest for the run is untouched
- the flag is platform state on a tracked run: refused for an untracked one,
  carried across a key transition and onto a schema-2 continuation before it
  starts, found from a slug a transition or a rename left behind — but never on
  a bare slug that is tracked as a run of its own, which a renamed run's resume
  carries the mute onto explicitly — dropped on forget; a session with no
  tracked run is muted on its own registry entry, and unmuting the run by any
  door clears that flag
- the fleet row says ``muted`` from the same resolver the record filter uses, so
  nobody wonders why a run is silent, or why a muted one is not
- the session routes are scoped to the session's project in projects mode
"""

import inspect
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from lmer_platform import (
    api, assistant, checkin, config as platform_config, ctl, daemon, detect, mute,
    project_context as scope, project_events, projects, registry, run_transition,
    runs, store,
)
from lmer_platform import config as cfg
from tests.conftest import node_binary, require_node_toolchain, strip_lmer_env
from tests.test_platform_detection import Spool, detector, fleet as fleet_payload, row
from tests.test_platform_projects import fleet  # noqa: F401

SECRET = "test-secret-value"
HOST, PROJECT, SLUG = "gitlab.example.com", "agents/global", "chat-1"
RUN = (HOST, PROJECT, SLUG)
BODY = {"host": HOST, "project": PROJECT, "slug": SLUG}
RUN_KEY = runs.run_key(*RUN)
UID = "a" * 32


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    for name in ("LMER_WORK_REPO", cfg.ENV_BIND_ADDRESS, cfg.ENV_BIND_PORT):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


@pytest.fixture
def tracked(platform_root):
    runs.track(*RUN, source="spawned", taskdef="chat")
    return RUN


@pytest.fixture
def client(platform_root):
    return TestClient(api.create_app(
        cfg.load(), SECRET, state_builder=lambda config, force_pull=False: {},
    ))


def auth():
    return {"Authorization": f"Bearer {SECRET}"}


#: The writer each routine kind is sent by in these tests.
WRITERS = {
    "message": {"channel": "slack", "author": "operator"},
    "note": {"channel": "agent"},
    "answer": {"channel": "slack", "author": "operator"},
    "command": {"channel": "platform", "author": "operator"},
    "close": {"channel": "agent"},
    "question": {"channel": "agent"},
    "milestone": {"channel": "agent"},
}


def record(kind, text="hello", ident=None, source=None, **fields):
    return {"id": ident or f"{kind}-{text}", "kind": kind, "text": text,
            "source": source or WRITERS.get(kind, {"channel": "agent"}), **fields}


def register(session_id, run=None):
    registry.register(session_id, pid=os.getpid(), run=run)
    return session_id


# --- the store ---------------------------------------------------------------


def test_muting_an_untracked_run_is_refused_404(platform_root):
    with pytest.raises(mute.RunNotTracked) as caught:
        mute.set_run(*RUN, muted=True)
    assert caught.value.status == 404
    assert mute.load_all() == {}


def test_mute_and_unmute_report_whether_they_changed_anything(tracked):
    assert mute.set_run(*RUN, muted=True) == {"muted": True, "changed": True, "key": RUN_KEY}
    assert mute.set_run(*RUN, muted=True)["changed"] is False
    assert mute.is_muted(RUN)
    assert mute.set_run(*RUN, muted=False)["changed"] is True
    assert mute.set_run(*RUN, muted=False)["changed"] is False
    assert not mute.is_muted(RUN)


def mute_events():
    return [(e["type"], e.get("data")) for e in store.read_events()
            if e["type"] in (mute.RUN_MUTED, mute.RUN_UNMUTED)]


def test_mute_and_unmute_are_platform_events_naming_who(tracked):
    by = {"kind": "operator", "name": "operator"}
    mute.set_run(*RUN, muted=True, by=by)
    mute.set_run(*RUN, muted=True, by=by)
    mute.set_run(*RUN, muted=False, by={"kind": "assistant", "name": "uber-lmer",
                                        "session": "s-ul"})
    assert mute_events() == [
        (mute.RUN_MUTED, {"run": RUN_KEY, "by": by}),
        (mute.RUN_UNMUTED, {"run": RUN_KEY, "by": {"kind": "assistant", "name": "uber-lmer",
                                                    "session": "s-ul"}}),
    ]


def test_forgetting_a_run_drops_its_mute(tracked):
    mute.set_run(*RUN, muted=True)
    runs.forget(*RUN)
    assert mute.load_all() == {}
    # Re-tracking the run later starts it unmuted.
    runs.track(*RUN, source="adopted")
    assert not mute.is_muted(RUN)


def test_rekey_moves_the_mute_once(tracked):
    # The transition itself is exercised end to end in
    # tests/test_platform_run_transition.py; this pins the store's own contract.
    assert "mute" in runs.TRANSITION_STORES
    assert run_transition._STORE_MODULES["mute"] is mute
    mute.set_run(*RUN, muted=True)
    outcome = mute.rekey(RUN, (HOST, PROJECT, UID))
    assert outcome == {"moved": True, "conflict": False}
    assert mute.is_muted((HOST, PROJECT, UID)) and not mute.is_muted(RUN)
    assert mute.rekey(RUN, (HOST, PROJECT, UID)) == {"moved": False, "conflict": False}
    assert not mute.conflicted((HOST, PROJECT, UID))


def test_a_session_with_no_tracked_run_is_muted_on_its_registry_entry(platform_root):
    register("s-chat")
    assert mute.set_session("s-chat", muted=True) == {
        "muted": True, "changed": True, "session": "s-chat"}
    assert registry.read_session("s-chat")["muted"] is True
    assert mute.load_all() == {}
    assert mute.set_session("s-chat", muted=False)["changed"] is True
    assert registry.read_session("s-chat")["muted"] is False


def test_a_tracked_runs_session_mutes_the_run_not_the_session(tracked):
    register("s-1", run=BODY)
    assert mute.set_session("s-1", muted=True) == {"muted": True, "changed": True, "run": RUN_KEY}
    assert mute.is_muted(RUN)
    assert "muted" not in registry.read_session("s-1")


def test_unmuting_clears_a_session_flag_set_before_the_run_was_tracked(platform_root):
    register("s-1", run=BODY)
    mute.set_session("s-1", muted=True)
    assert registry.read_session("s-1")["muted"] is True
    runs.track(*RUN, source="adopted")

    reply = mute.set_session("s-1", muted=False)

    assert reply == {"muted": False, "changed": True, "run": RUN_KEY}
    assert registry.read_session("s-1")["muted"] is False
    assert mute.suppressed("s-1", record("message"), BODY) is False


def test_a_reaped_session_reports_no_change(platform_root, monkeypatch):
    register("s-gone")
    monkeypatch.setattr(registry, "update", lambda *a, **k: None)
    real_read = registry.read_session
    reads = []

    def read_then_vanish(session_id):
        reads.append(session_id)
        return real_read(session_id) if len(reads) == 1 else None

    monkeypatch.setattr(registry, "read_session", read_then_vanish)
    assert mute.set_session("s-gone", muted=True)["changed"] is False
    assert not [e for e in store.read_events() if e["type"] == mute.RUN_MUTED]


def test_a_slug_spelled_run_block_finds_a_mute_stored_under_the_uid(platform_root):
    runs.track(HOST, PROJECT, SLUG, uid=UID, source="spawned")
    # The alias a key transition leaves: the slug key now names the uid entry.
    index = runs.load_index()
    index[runs.run_key(HOST, PROJECT, UID)]["aliases"] = [RUN_KEY]
    runs._save_index(index)
    mute.set_run(*RUN, muted=True)
    assert set(mute.load_all()) == {runs.run_key(HOST, PROJECT, UID)}
    assert mute.suppressed("s-1", record("message"), BODY) is True


def test_a_continuation_inherits_the_mute(platform_root):
    source, started = (HOST, PROJECT, UID), (HOST, PROJECT, "b" * 32)
    runs.track(HOST, PROJECT, SLUG, uid=UID)
    assert mute.source_of(*source) is None  # not muted: nothing to carry
    mute.set_run(*source, muted=True)
    assert mute.source_of(*source) == source
    # Primed before the spawn tracks the run, so no tracked check here.
    assert mute.prime(source, started) is True
    assert mute.is_muted(started)
    assert mute.prime(source, started) is False  # already there
    assert mute_events()[-1] == (mute.RUN_MUTED, {
        "run": runs.run_key(*started), "carried_from": runs.run_key(*source)})


def test_unmuting_a_continuation_leaves_the_run_it_continued(platform_root):
    # A schema-2 continuation is a row of its own: its mute was carried, but
    # unmuting it says nothing about the source, unlike a renamed run's halves.
    source, started = (HOST, PROJECT, UID), (HOST, PROJECT, "b" * 32)
    runs.track(HOST, PROJECT, SLUG, uid=UID)
    mute.set_run(*source, muted=True)
    mute.prime(source, started)
    runs.track(HOST, PROJECT, SLUG, uid="b" * 32)
    mute.set_run(*started, muted=False)
    assert mute.is_muted(source)
    assert not mute.is_muted(started)


def test_a_mute_still_under_a_slug_key_is_found_for_a_continuation(platform_root):
    # A mute a transition has not moved yet: stored under the slug key, which is
    # now the uid entry's alias.
    runs.track(HOST, PROJECT, SLUG, uid=UID, source="spawned")
    index = runs.load_index()
    index[runs.run_key(HOST, PROJECT, UID)]["aliases"] = [RUN_KEY]
    runs._save_index(index)
    mute._save({RUN_KEY: {"muted": True}})
    assert not mute.is_muted((HOST, PROJECT, UID))
    assert mute.source_of(HOST, PROJECT, UID) == (HOST, PROJECT, UID)


def test_an_unknown_session_is_refused_404(platform_root):
    with pytest.raises(mute.SessionNotFound) as caught:
        mute.set_session("s-nobody", muted=True)
    assert caught.value.status == 404


OPERATOR = "operator"


@pytest.mark.parametrize("kind, channel, author, held", [
    # Routine chatter, from the writers that send it.
    ("message", "slack", OPERATOR, True),
    ("message", "matrix", OPERATOR, True),
    ("message", "terminal", None, True),
    ("message", "agent", None, True),
    ("message", "uber-lmer", None, True),
    ("message", "platform", OPERATOR, True),  # the operator, in the UI
    ("note", "agent", None, True),
    ("answer", "slack", OPERATOR, True),
    ("answer", "platform", OPERATOR, True),
    ("command", "platform", OPERATOR, True),
    ("command", "uber-lmer", None, True),
    ("close", "agent", None, True),
    # The platform's own notices: unauthored platform records, and the
    # supervisor's. A Codex delivery that needs review is a platform note.
    ("note", "platform", None, False),
    ("message", "platform", None, False),
    ("close", "platform", None, False),
    ("note", "supervisor", None, False),
    # A routine kind from a writer nobody listed spools.
    ("message", "some-connector", OPERATOR, False),
    ("note", "slack", OPERATOR, False),
    ("command", "slack", OPERATOR, False),
    # Never routine, whoever writes it.
    ("question", "agent", None, False),
    ("milestone", "agent", None, False),
    ("some-future-kind", "agent", None, False),
    (None, "agent", None, False),
])
def test_only_routine_records_are_suppressed(tracked, kind, channel, author, held):
    mute.set_run(*RUN, muted=True)
    source = {"channel": channel, **({"author": author} if author else {})}
    assert mute.suppressed("s-1", {"kind": kind, "source": source}, BODY) is held


def test_a_record_with_no_source_is_not_routine(tracked):
    mute.set_run(*RUN, muted=True)
    assert mute.suppressed("s-1", {"kind": "message"}, BODY) is False
    assert mute.suppressed("s-1", {"kind": "message", "source": "slack"}, BODY) is False


def test_a_classified_platform_notice_is_never_suppressed(tracked):
    mute.set_run(*RUN, muted=True)
    notice = record("message", "thread disconnected", source={"channel": "platform",
                                                               "author": OPERATOR},
                    meta={"notification_class": "connector_health"})
    assert mute.suppressed("s-1", notice, BODY) is False


def codex_needs_review(ident="note-codex"):
    """The supervisor's fallback when it cannot raise the escalation question
    (``report_codex_delivery``): a ``platform`` note nobody authored."""
    return record("note", "Codex delivery d-1 needs review: original retained, not replayed",
                  ident=ident, source={"channel": "platform"})


def test_nothing_is_suppressed_for_an_unmuted_run(tracked):
    assert mute.suppressed("s-1", record("message"), BODY) is False


# --- projects mode -----------------------------------------------------------


@pytest.fixture
def project_spool(fleet):  # noqa: F811
    projects.migrate()
    with scope.bound("general"):
        runs.track(*RUN)
        yield


def deliver(message, session="s-1"):
    project_events.enqueue(session, message, run=BODY)
    project_events.flush()


def spooled():
    return [note.note for note in assistant.read_state().pending]


def test_projects_mode_muted_chatter_never_spools_but_a_question_does(project_spool):
    mute.set_run(*RUN, muted=True)
    deliver(record("message", "tell me your container id"))
    deliver(record("note", "it is abc123"))
    assert spooled() == []

    deliver(record("question", "may I force-push?"))
    deliver(record("milestone", "pushed the MR"))
    notes = spooled()
    assert len(notes) == 2
    assert "may I force-push?" in notes[0] and "pushed the MR" in notes[1]


def test_projects_mode_a_platform_failure_note_on_a_muted_run_still_spools(project_spool):
    mute.set_run(*RUN, muted=True)
    deliver(codex_needs_review())
    notes = spooled()
    assert len(notes) == 1 and "needs review" in notes[0]


def test_projects_mode_one_batch_keeps_only_what_is_not_chatter(project_spool):
    # Enqueued together and flushed once: the filter is per record, so the
    # question and the milestone are not held back by the chatter around them.
    mute.set_run(*RUN, muted=True)
    for message in (record("message", "chatter one"), record("question", "may I merge?"),
                    record("note", "chatter two"), record("milestone", "merged"),
                    record("message", "chatter three")):
        project_events.enqueue("s-1", message, run=BODY)
    project_events.flush()
    joined = "\n".join(spooled())
    assert "may I merge?" in joined and "merged" in joined
    assert "chatter" not in joined


def test_projects_mode_unmute_resumes_and_replays_nothing(project_spool):
    mute.set_run(*RUN, muted=True)
    deliver(record("message", "while muted"))
    mute.set_run(*RUN, muted=False)
    deliver(record("message", "after unmute"))
    notes = spooled()
    assert len(notes) == 1
    assert "after unmute" in notes[0] and "while muted" not in notes[0]


# --- legacy mode -------------------------------------------------------------


class Hub:
    def __init__(self):
        self.queued = []

    def queue_digest(self, event):
        self.queued.append(event.message["kind"])


def test_legacy_consumer_suppresses_muted_chatter_but_not_questions(tracked):
    assert not projects.enabled()
    hub = Hub()
    consume = daemon._assistant_consumer(hub)
    mute.set_run(*RUN, muted=True)
    for kind in ("message", "note", "question", "answer"):
        consume(SimpleNamespace(session="s-1", message=record(kind), run=BODY))
    assert hub.queued == ["question"]

    mute.set_run(*RUN, muted=False)
    consume(SimpleNamespace(session="s-1", message=record("message"), run=BODY))
    assert hub.queued == ["question", "message"]


def test_legacy_consumer_spools_a_platform_failure_note_from_a_muted_run(tracked):
    hub = Hub()
    consume = daemon._assistant_consumer(hub)
    mute.set_run(*RUN, muted=True)
    consume(SimpleNamespace(session="s-1", message=codex_needs_review(), run=BODY))
    assert hub.queued == ["note"]


def test_legacy_consumer_honours_a_muted_session_with_no_run(platform_root):
    hub = Hub()
    consume = daemon._assistant_consumer(hub)
    register("s-chat")
    mute.set_session("s-chat", muted=True)
    consume(SimpleNamespace(session="s-chat", message=record("message"), run=None))
    consume(SimpleNamespace(session="s-chat", message=record("question"), run=None))
    assert hub.queued == ["question"]


# --- stale_runs and stalled (scope A) ----------------------------------------


def test_a_muted_run_is_never_stale(platform_root):
    old = "2026-01-01T00:00:00Z"
    rows = [{**BODY, "state": "running"},
            {**BODY, "slug": "other", "state": "running"}]
    marks = {RUN_KEY: {"first_seen": old},
             runs.run_key(HOST, PROJECT, "other"): {"first_seen": old}}
    rows[0]["muted"] = True
    stale = checkin.stale_runs({"runs": rows}, window=60, marks=marks)
    assert [run.slug for run in stale] == ["other"]
    # The badge agrees with the digest: no stale marker on the muted row.
    assert checkin.row_checkin(rows[0], marks, window=60)["stale"] is False
    assert checkin.row_checkin(rows[1], marks, window=60)["stale"] is True


def muted_row(slug, **kwargs):
    return {**row(slug, **kwargs), "muted": True}


def test_a_muted_runs_stalled_condition_is_not_spooled_and_not_replayed(platform_root):
    config = cfg.load()
    spool = Spool()
    stalled = dict(reason="stalled", note="no output for 60m", since="2026-10-21T10:00:00Z",
                   state="running")
    tick = detector(
        config,
        fleet_payload(),
        fleet_payload(muted_row("chat-1", **stalled)),
        fleet_payload(row("chat-1", **stalled)),
        spool=spool,
    )
    tick.tick_once()
    assert tick.tick_once() == []
    assert tick.muted_stalls == 1
    # Unmuted while the same condition stands: it is in the baseline, so
    # nothing is replayed.
    assert tick.tick_once() == []
    assert "stalled" not in spool.kinds


def test_an_unmuted_stall_still_spools(platform_root):
    config = cfg.load()
    spool = Spool()
    tick = detector(
        config, fleet_payload(),
        fleet_payload(row("dev-1", reason="stalled", note="quiet", since="t1", state="running")),
        spool=spool,
    )
    tick.tick_once()
    assert [signal.kind for signal in tick.tick_once()] == ["stalled"]
    assert tick.muted_stalls == 0


@pytest.mark.parametrize("reason", ["live_question", "crashed", "critical_error", "question",
                                    "yield"])
def test_every_other_condition_of_a_muted_run_still_spools(platform_root, reason):
    config = cfg.load()
    spool = Spool()
    tick = detector(
        config, fleet_payload(),
        fleet_payload(muted_row("chat-1", reason=reason, note="n", since="t1")),
        spool=spool,
    )
    tick.tick_once()
    assert [signal.kind for signal in tick.tick_once()] == [reason]
    assert reason in spool.kinds


def test_a_muted_run_still_spools_complete(platform_root):
    config = cfg.load()
    spool = Spool()
    tick = detector(
        config,
        fleet_payload({**row("chat-1", state="running"), "muted": True}),
        fleet_payload({**row("chat-1", state="complete", updated="2026-10-21T11:00:00Z"),
                       "muted": True}),
        spool=spool,
    )
    tick.tick_once()
    assert [signal.kind for signal in tick.tick_once()] == ["complete"]
    assert spool.kinds == ["complete"]


def test_a_muted_runs_answered_question_still_spools(platform_root):
    config = cfg.load()
    spool = Spool()
    waiting = fleet_payload(muted_row("chat-1", reason="question", note="which way?",
                                      state="waiting_on_you", since="t1"))
    answered = fleet_payload(muted_row("chat-1", state="running"))
    tick = detector(config, fleet_payload(), waiting, answered, spool=spool)
    tick.tick_once()
    tick.tick_once()
    tick.tick_once()
    assert spool.kinds == ["question", detect.QUESTION_ANSWERED_KIND]


# --- the fleet row -----------------------------------------------------------


def test_fleet_rows_carry_muted_before_check_ins_read_it(tracked, monkeypatch):
    register("s-chat")
    mute.set_session("s-chat", muted=True)
    mute.set_run(*RUN, muted=True)
    payload = {"runs": [
        {**BODY, "state": "running", "session": {"id": "s-1"}},
        {"host": HOST, "project": PROJECT, "slug": "other", "state": "running",
         "session": {"id": "s-other"}},
        {"host": None, "project": None, "slug": None, "state": "running",
         "session": {"id": "s-chat"}},
    ]}
    payload["attention"] = [dict(payload["runs"][0])]
    api._fold_mute(payload, registry.list_sessions(live_only=False))
    assert [r["muted"] for r in payload["runs"]] == [True, False, True]
    assert payload["attention"][0]["muted"] is True


def test_build_state_folds_mute_before_check_ins():
    source = inspect.getsource(api.build_state)
    assert source.index("_fold_mute(") < source.index("_fold_checkins(")


# --- routes ------------------------------------------------------------------


def test_mute_route_round_trip(client, tracked):
    reply = client.post("/api/runs/mute", json=BODY, headers=auth())
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["muted"] is True and body["changed"] is True and body["run"] == RUN_KEY
    assert body["local"] is True and body["note"] == mute.LOCALITY_NOTE

    reply = client.post("/api/runs/unmute", json=BODY, headers=auth())
    assert reply.status_code == 200 and reply.json()["muted"] is False
    assert not mute.is_muted(RUN)


def test_mute_route_refusals(client, platform_root):
    assert client.post("/api/runs/mute", json=BODY, headers=auth()).status_code == 404
    # The session form has its own path, which the project binding scopes.
    for body in ({"session": "s-chat"}, {"session": ""}, {**BODY, "session": "s-chat"}):
        reply = client.post("/api/runs/mute", json=body, headers=auth())
        assert reply.status_code == 400 and "/api/sessions/" in reply.text
    assert client.post("/api/runs/mute", json={"host": HOST},
                       headers=auth()).status_code == 400
    assert client.post("/api/runs/mute", json=BODY).status_code in (401, 403)
    assert client.post("/api/sessions/s-nobody/mute", headers=auth()).status_code == 404


def test_mute_route_by_session(client, platform_root):
    register("s-chat")
    reply = client.post("/api/sessions/s-chat/mute", headers=auth())
    assert reply.status_code == 200, reply.text
    assert reply.json()["session"] == "s-chat" and reply.json()["muted"] is True
    reply = client.post("/api/sessions/s-chat/unmute", headers=auth())
    assert reply.status_code == 200 and reply.json()["changed"] is True


def test_the_routes_record_the_caller_on_the_event(client, tracked):
    client.post("/api/runs/mute", json=BODY, headers=auth())
    register("s-chat")
    client.post("/api/sessions/s-chat/mute", headers=auth())
    by = {"kind": "operator", "name": "operator"}
    assert mute_events() == [(mute.RUN_MUTED, {"run": RUN_KEY, "by": by}),
                             (mute.RUN_MUTED, {"session": "s-chat", "by": by})]


def test_api_help_lists_the_mute_routes(client):
    text = client.get("/api", headers=auth()).text
    assert "POST /api/runs/mute" in text and "POST /api/runs/unmute" in text
    assert "POST /api/sessions/{id}/mute" in text and "POST /api/sessions/{id}/unmute" in text


# --- lmer-ctl ----------------------------------------------------------------


def build(*argv):
    args = ctl.create_parser().parse_args(list(argv))
    return args.call(args)


@pytest.mark.parametrize("verb", ["mute", "unmute"])
def test_ctl_verbs_go_by_run_or_by_the_sessions_own_path(verb):
    call = build("runs", verb, RUN_KEY)
    assert (call.method, call.path, call.body) == ("POST", f"/api/runs/{verb}", BODY)
    call = build("runs", verb, "--session", "s-chat")
    assert (call.method, call.path) == ("POST", f"/api/sessions/s-chat/{verb}")
    assert not call.body


@pytest.mark.parametrize("argv", [["runs", "mute"], ["runs", "mute", RUN_KEY, "--session", "s"],
                                  ["runs", "mute", "--session", ""]])
def test_ctl_needs_exactly_one_of_key_or_session(argv):
    with pytest.raises(ctl.CtlError):
        build(*argv)


# --- one resolver for every door (review round 1) ----------------------------


def test_unmuting_by_run_key_clears_a_session_flag_set_before_adoption(platform_root):
    # The reviewer's probe: muted by session while the run was untracked, then
    # adopted, then unmuted by its key — the door the UI and lmer-ctl use.
    register("s-pre", run=BODY)
    assert mute.set_session("s-pre", muted=True)["session"] == "s-pre"
    runs.track(*RUN, source="adopted")

    reply = mute.set_run(*RUN, muted=False)

    assert reply == {"muted": False, "changed": True, "key": RUN_KEY}
    assert registry.read_session("s-pre")["muted"] is False
    assert mute.suppressed("s-pre", record("message"), BODY) is False
    payload = {"runs": [{**BODY, "state": "running", "session": {"id": "s-pre"}}]}
    api._fold_mute(payload, registry.list_sessions(live_only=False))
    assert payload["runs"][0]["muted"] is False


def test_unmuting_by_run_key_leaves_another_runs_session_flag(platform_root):
    register("s-pre", run=BODY)
    register("s-other", run={**BODY, "slug": "other"})
    mute.set_session("s-pre", muted=True)
    mute.set_session("s-other", muted=True)
    runs.track(*RUN, source="adopted")
    mute.set_run(*RUN, muted=False)
    assert registry.read_session("s-other")["muted"] is True


RENAMED = "develop-x--nice-name"
RECORDED = {"host": HOST, "project": PROJECT, "slug": "develop-x"}


def test_a_renamed_legacy_runs_current_session_is_muted(platform_root):
    # Tracked under its directory name; its session registers under the slug.
    runs.track(HOST, PROJECT, RENAMED, source="adopted")
    register("s-ren", run=RECORDED)
    mute.set_run(HOST, PROJECT, RENAMED, muted=True)

    assert mute.suppressed("s-ren", record("message"), RECORDED) is True
    # The row and the records agree: a session row spelled by the slug is muted.
    payload = {"runs": [{**RECORDED, "state": "running", "session": {"id": "s-ren"}}]}
    api._fold_mute(payload, registry.list_sessions(live_only=False))
    assert payload["runs"][0]["muted"] is True


def test_a_renamed_legacy_runs_resumed_session_is_muted(platform_root):
    runs.track(HOST, PROJECT, RENAMED, source="adopted")
    mute.set_run(HOST, PROJECT, RENAMED, muted=True)
    # A resume's spawn tracks the run under the recorded slug it derived too
    # (resume.py, the renamed-run branch), and carries the mute there before the
    # new session registers (spawn._prime_mute).
    source = mute.source_of(HOST, PROJECT, RENAMED)
    assert mute.prime(source, (HOST, PROJECT, "develop-x")) is True
    runs.track(HOST, PROJECT, "develop-x", source="spawned")
    register("s-next", run=RECORDED)
    assert mute.suppressed("s-next", record("note"), RECORDED) is True
    payload = {"runs": [{**RECORDED, "state": "running", "session": {"id": "s-next"}}]}
    api._fold_mute(payload, registry.list_sessions(live_only=False))
    assert payload["runs"][0]["muted"] is True


def test_unmuting_the_resumed_session_clears_the_renamed_runs_mute_too(platform_root):
    # The other direction: the live row unmutes by session, which resolves to
    # the bare key; the renamed key's mute goes with it, or the next resume
    # would carry it back.
    runs.track(HOST, PROJECT, RENAMED, source="adopted")
    mute.set_run(HOST, PROJECT, RENAMED, muted=True)
    mute.prime((HOST, PROJECT, RENAMED), (HOST, PROJECT, "develop-x"))
    runs.track(HOST, PROJECT, "develop-x", source="spawned")
    register("s-next", run=RECORDED)

    reply = mute.set_session("s-next", muted=False, by={"kind": "operator"})

    assert reply == {"muted": False, "changed": True,
                     "run": runs.run_key(HOST, PROJECT, "develop-x")}
    assert mute.load_all() == {}
    assert mute.source_of(HOST, PROJECT, RENAMED) is None
    assert mute_events()[-1] == (mute.RUN_UNMUTED, {
        "run": runs.run_key(HOST, PROJECT, RENAMED),
        "carried_to": runs.run_key(HOST, PROJECT, "develop-x"),
        "by": {"kind": "operator"},
    })


def test_unmuting_a_bare_key_muted_on_its_own_leaves_the_renamed_run(platform_root):
    runs.track(HOST, PROJECT, RENAMED, source="adopted")
    runs.track(HOST, PROJECT, "develop-x", source="adopted")
    mute.set_run(HOST, PROJECT, RENAMED, muted=True)
    mute.set_run(HOST, PROJECT, "develop-x", muted=True)
    mute.set_run(HOST, PROJECT, "develop-x", muted=False)
    assert mute.run_muted((HOST, PROJECT, RENAMED)) is True


def test_a_bare_slug_run_beside_a_muted_renamed_run_stays_unmuted(platform_root):
    # Two runs: one tracked by its renamed directory, and a different one
    # tracked under the bare slug. Muting the first must not silence the second.
    old = "2026-01-01T00:00:00Z"
    bare = {"host": HOST, "project": PROJECT, "slug": "masterplan"}
    renamed = {**bare, "slug": "masterplan--foo"}
    runs.track(HOST, PROJECT, "masterplan--foo", source="adopted")
    runs.track(HOST, PROJECT, "masterplan", source="adopted")
    register("s-bare", run=bare)
    mute.set_run(HOST, PROJECT, "masterplan--foo", muted=True)

    assert mute.run_muted((HOST, PROJECT, "masterplan")) is False
    assert mute.suppressed("s-bare", record("message"), bare) is False
    payload = {"runs": [{**bare, "state": "running", "session": {"id": "s-bare"}},
                        {**renamed, "state": "running"}]}
    api._fold_mute(payload, registry.list_sessions(live_only=False))
    assert [row["muted"] for row in payload["runs"]] == [False, True]
    # Its digests: still stale when it is, and its stall still spools.
    marks = {runs.run_key(HOST, PROJECT, row["slug"]): {"first_seen": old}
             for row in payload["runs"]}
    stale = checkin.stale_runs(payload, window=60, marks=marks)
    assert [run.slug for run in stale] == ["masterplan"]
    spool = Spool()
    stalled = dict(reason="stalled", note="no output for 60m",
                   since="2026-10-21T10:00:00Z", state="running")
    tick = detector(
        cfg.load(), fleet_payload(),
        fleet_payload({**row("masterplan", **stalled), "muted": payload["runs"][0]["muted"]}),
        spool=spool,
    )
    tick.tick_once()
    tick.tick_once()
    assert "stalled" in spool.kinds
    # The bare run's own mute is its own, and the renamed run's unmute leaves it.
    mute.set_run(HOST, PROJECT, "masterplan", muted=True)
    mute.set_run(HOST, PROJECT, "masterplan--foo", muted=False)
    assert mute.run_muted((HOST, PROJECT, "masterplan")) is True


def test_unmuting_a_renamed_run_drops_the_mute_its_resume_carried(platform_root):
    runs.track(HOST, PROJECT, RENAMED, source="adopted")
    mute.set_run(HOST, PROJECT, RENAMED, muted=True)
    mute.prime((HOST, PROJECT, RENAMED), (HOST, PROJECT, "develop-x"))
    runs.track(HOST, PROJECT, "develop-x", source="spawned")
    register("s-next", run=RECORDED)

    reply = mute.set_run(HOST, PROJECT, RENAMED, muted=False, by={"kind": "operator"})

    assert reply["changed"] is True
    assert mute.load_all() == {}
    assert mute.suppressed("s-next", record("note"), RECORDED) is False
    assert mute_events()[-1] == (mute.RUN_UNMUTED, {
        "run": runs.run_key(HOST, PROJECT, "develop-x"),
        "carried_from": runs.run_key(HOST, PROJECT, RENAMED),
        "by": {"kind": "operator"},
    })


def test_two_runs_renamed_from_one_slug_are_not_guessed_between(platform_root):
    # Two runs whose recorded slug is the same bare taskdef: nothing here can
    # tell which one a slug-spelled record belongs to, so it is not muted.
    runs.track(HOST, PROJECT, "masterplan--one", source="adopted")
    runs.track(HOST, PROJECT, "masterplan--two", source="adopted")
    mute.set_run(HOST, PROJECT, "masterplan--one", muted=True)
    block = {"host": HOST, "project": PROJECT, "slug": "masterplan"}
    assert mute.suppressed("s-1", record("message"), block) is False


def test_a_slug_spelled_session_row_after_a_transition_folds_muted(platform_root):
    runs.track(HOST, PROJECT, SLUG, uid=UID, source="spawned")
    index = runs.load_index()
    index[runs.run_key(HOST, PROJECT, UID)]["aliases"] = [RUN_KEY]
    runs._save_index(index)
    mute.set_run(HOST, PROJECT, uid=UID, muted=True)
    payload = {"runs": [{**BODY, "state": "running", "session": {"id": "s-1"}}]}
    api._fold_mute(payload, [])
    assert payload["runs"][0]["muted"] is True
    assert mute.suppressed("s-1", record("message"), BODY) is True


# --- a muted session with no run identity --------------------------------------


@pytest.mark.parametrize("kind, source", [
    ("answer", {"channel": "slack", "author": OPERATOR}),
    ("close", {"channel": "agent"}),
])
def test_an_identity_less_sessions_question_bookkeeping_still_spools(platform_root, kind, source):
    # Its question reached the assistant, and the detector has no row to report
    # it settled from, so the answer or the withdrawal must go through.
    register("s-chat")
    mute.set_session("s-chat", muted=True)
    reply = record(kind, "yes", source=source, in_reply_to="s-chat#4")
    assert mute.suppressed("s-chat", reply, None) is False
    # Chatter from the same session is still held back.
    assert mute.suppressed("s-chat", record("message"), None) is True
    assert mute.suppressed("s-chat", record("close", "done", source={"channel": "agent"}),
                           None) is True


def test_a_tracked_runs_answers_stay_muted(tracked):
    # The detector reports a tracked run's question settled itself
    # (question_answered), so its answers are chatter.
    mute.set_run(*RUN, muted=True)
    assert mute.suppressed("s-1", record("answer", "yes", in_reply_to="s-1#4"), BODY) is True


# --- projects mode: the session routes are the session's project's ---------------


@pytest.fixture
def project_client(fleet):  # noqa: F811
    projects.migrate()
    app = api.create_app(fleet, platform_config.ensure_secret(fleet),
                         state_builder=lambda *_a, **_k: {"runs": [], "sessions": [],
                                                          "attention": []})
    with TestClient(app, headers={
            "Authorization": f"Bearer {platform_config.ensure_secret(fleet)}"}) as client:
        yield client


def project_session(client, name, session_id):
    project = client.post("/api/projects", json={"name": name}).json()
    register(session_id)
    projects.bind_session(session_id, project["id"])
    return project


def test_projects_mode_the_operator_mutes_a_session_by_its_own_path(project_client):
    alpha = project_session(project_client, "Alpha", "s-chat")
    # The UI sends the row's project; lmer-ctl may send none. Both bind to the
    # session's own project.
    reply = project_client.post("/api/sessions/s-chat/mute", json={"workspace_id": alpha["id"]})
    assert reply.status_code == 200, reply.text
    assert reply.json() == {"muted": True, "changed": True, "session": "s-chat",
                            "local": True, "note": mute.LOCALITY_NOTE}
    reply = project_client.post("/api/sessions/s-chat/unmute")
    assert reply.status_code == 200, reply.text
    assert registry.read_session("s-chat")["muted"] is False


def test_projects_mode_an_assistant_cannot_mute_another_projects_session(project_client):
    alpha = project_client.post("/api/projects", json={"name": "Alpha"}).json()
    project_session(project_client, "Beta", "s-beta")
    with scope.bound(alpha["id"]):
        token = platform_config.active_assistant_credential()
    reply = project_client.post("/api/sessions/s-beta/mute",
                                headers={"Authorization": f"Bearer {token}"})
    assert reply.status_code == 409, reply.text
    assert registry.read_session("s-beta").get("muted") is not True
    # The same assistant may mute a session in its own project.
    register("s-alpha")
    projects.bind_session("s-alpha", alpha["id"])
    reply = project_client.post("/api/sessions/s-alpha/mute",
                                headers={"Authorization": f"Bearer {token}"})
    assert reply.status_code == 200, reply.text
    events = [data for kind, data in mute_events() if kind == mute.RUN_MUTED]
    assert events[-1]["by"]["kind"] == "assistant"


def test_projects_mode_the_body_session_form_is_refused(project_client):
    project_session(project_client, "Alpha", "s-chat")
    reply = project_client.post("/api/runs/mute", json={"session": "s-chat"})
    assert reply.status_code in (400, 409)
    assert registry.read_session("s-chat").get("muted") is not True


# --- the UI toggle -------------------------------------------------------------


def test_the_ui_toggle_goes_through_the_right_door():
    """Executed, not read: which route a row posts to is what scopes it."""
    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")
    script = Path(__file__).resolve().parent / "test_platform_web_mute.js"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "all assertions passed" in result.stdout
