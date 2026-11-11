"""The time-tracking settings routes (issue #668, docs/TIME-TRACKING.md#settings).

The registry, the ``time_tracking`` switch and the platform-wide defaults are
edited through the API instead of by hand in ``config.json``, and every write
takes effect in the running daemon. What must hold (TIME-TRACKING.md#settings): only the operator
credential edits people and defaults; an assistant lists or renames only the
person it is talking to, taken from the record or the request it handles,
never from the body, and sets only its own project. A write that would leave
the registry unusable is refused, naming the problem, rather than turning
tracking off.
"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from lmer_platform import (
    api, comms, projects, store, timesheet_store, timesheet_writer, timetrack, timetrack_activity, timetrack_live,
)
from lmer_platform import config as cfg
from tests.test_platform_projects import fleet  # noqa: F401
from tests.test_assistant_mentions import SLACK_USER
from tests.test_platform_project_transports import transport_host  # noqa: F401
from tests.test_platform_time_api import (  # noqa: F401
    OTHER, PEOPLE, PERSON, SLACK_ID, STRANGER_ID, _assistant, _bearer, _detail, _journal, _record, docs,
    mirror, webshop,
)
from tests.test_project_review_requests import actions, delivered

HANDLER = "name: board\nlabel: Board\nfields:\n  - key: task_id\n    required: true\n    pattern: '[a-z0-9]+'\n"


@pytest.fixture
def platform(fleet, mirror, monkeypatch):
    """A platform whose registry lives in ``config.json``, where the routes write it."""
    projects.migrate()
    monkeypatch.setattr(comms, "_HUB", comms.CommsHub())
    store.write_json(cfg.config_path(), {"people": [dict(person) for person in PEOPLE],
                                         "default_person": PERSON, "work_repo_mirror": str(mirror)})
    return cfg.load({"lmer_bin": fleet.lmer_bin, "max_concurrent_sessions": 1})


@pytest.fixture
def tracking(platform):
    unit = timetrack_live.TimeTracking().start(platform)
    timetrack_live.register_tracking(unit)
    yield unit
    unit.stop()
    timetrack_live.register_tracking(None)
    timetrack.register_tracker(None)
    timesheet_writer.register_writer(None)
    timetrack_activity.register_activity(None)


@pytest.fixture
def client(platform, tracking):
    secret = cfg.ensure_secret(platform)
    app = api.create_app(platform, secret, state_builder=lambda *_a, **_k: {"runs": []})
    with TestClient(app, headers=_bearer(secret)) as http:
        yield http


@pytest.fixture
def ratchet(platform, webshop, docs):
    projects.reconcile(platform)
    return _assistant(webshop)


@pytest.fixture
def asked(transport_host, fleet, mirror, monkeypatch):
    """``make(people)``: store *people*, start tracking, and deliver a Slack request
    from ``SLACK_USER`` to a project assistant; answers ``(http, headers, session,
    request_id)``. ``time/me`` writes only inside such a request."""
    monkeypatch.setattr(comms, "_HUB", comms.CommsHub())
    units = []

    def make(people, **stored):
        store.write_json(cfg.config_path(), {"people": [dict(person) for person in people],
                                             "work_repo_mirror": str(mirror), **stored})
        unit = timetrack_live.TimeTracking().start(cfg.load({"lmer_bin": fleet.lmer_bin}))
        timetrack_live.register_tracking(unit)
        units.append(unit)
        http = transport_host[0]
        project, _request, request_id, headers = delivered(transport_host, fleet)
        return http, headers, projects.status(project["id"])["session_id"], request_id

    yield make
    for unit in units:
        unit.stop()
    timetrack_live.register_tracking(None)
    timetrack.register_tracker(None)
    timesheet_writer.register_writer(None)
    timetrack_activity.register_activity(None)


def _stored():
    return store.read_json(cfg.config_path())


def _ok(response):
    assert response.status_code == 200, response.text
    return response.json()


# --- reading ------------------------------------------------------------------

def test_the_settings_show_the_registry_and_where_each_default_comes_from(client, webshop, monkeypatch):
    projects.update(webshop, {"settings": {**projects.get(webshop)["settings"], "time_idle_grace_minutes": 5}})
    monkeypatch.setenv(cfg.ENV_TIME_PAUSE_HOURS, "2")

    reply = _ok(client.get("/api/time/settings"))

    assert reply["tracking"] == {"switch": True, "runs": True, "problems": [], "live": True}
    assert [person["key"] for person in reply["people"]] == [PERSON, OTHER]
    assert reply["default_person"] == {"stored": PERSON, "effective": PERSON}
    grace = reply["defaults"]["idle_grace_minutes"]
    assert (grace["value"], grace["source"], grace["stored"], grace["minimum"]) == (15, "default", None, 1)
    pause = reply["defaults"]["pause_hours"]
    assert (pause["value"], pause["source"], pause["env"]) == (2, "env", cfg.ENV_TIME_PAUSE_HOURS)
    rows = {row["workspace_id"]: row for row in reply["projects"]}
    assert (rows[webshop]["idle_grace_minutes"], rows[webshop]["effective_idle_grace_minutes"]) == (5, 5)
    assert all(row["effective_idle_grace_minutes"] == 15 for key, row in rows.items() if key != webshop)


# --- people (operator) ---------------------------------------------------------

def test_adding_the_first_person_starts_tracking_without_a_restart(fleet, mirror, monkeypatch):
    projects.migrate()
    monkeypatch.setattr(comms, "_HUB", comms.CommsHub())
    store.write_json(cfg.config_path(), {"work_repo_mirror": str(mirror)})
    unit = timetrack_live.TimeTracking().start(cfg.load())
    timetrack_live.register_tracking(unit)
    try:
        secret = cfg.ensure_secret(fleet)
        http = TestClient(api.create_app(cfg.load(), secret, state_builder=lambda *_a, **_k: {}),
                          headers=_bearer(secret))
        assert _detail(http.get("/api/time/status"), 503)["code"] == "time_tracking_off"

        reply = _ok(http.post("/api/time/people", json={"key": "sam", "name": "Sam", "slack": ["U0SAM"]}))

        assert reply["applied"] == timetrack_live.STARTED
        assert reply["default_person"]["effective"] == "sam", "one person listed is the default"
        assert _ok(http.get("/api/time/status"))["person"] == "sam"
        assert _stored()["people"] == [{"key": "sam", "name": "Sam", "slack": ["U0SAM"]}]
    finally:
        unit.stop()
        timetrack_live.register_tracking(None)


def test_an_edit_applies_in_place_to_the_running_registry(client, tracking):
    activity = tracking.activity
    reply = _ok(client.patch(f"/api/time/people/{OTHER}", json={"name": "Blair B.", "matrix": ["@blair:example.org"]}))

    assert reply["applied"] == timetrack_live.UPDATED and tracking.activity is activity
    assert activity.registry.person_for_sender("matrix", "@blair:example.org") == OTHER
    entry = next(person for person in _stored()["people"] if person["key"] == OTHER)
    assert entry == {"key": OTHER, "name": "Blair B.", "slack": ["U0BLAIR"], "matrix": ["@blair:example.org"]}


def test_adding_an_unmapped_identity_moves_it_from_whoever_had_it(client):
    _ok(client.patch(f"/api/time/people/{OTHER}", json={"add": [f"slack:{SLACK_ID}"]}))

    stored = {person["key"]: person for person in _stored()["people"]}
    assert SLACK_ID in stored[OTHER]["slack"] and SLACK_ID not in stored[PERSON]["slack"]


@pytest.mark.parametrize("body, status, code", [
    ({"key": PERSON}, 409, "time_person_exists"),
    ({"key": "Not A Slug"}, 400, "time_settings_invalid"),
    ({"key": "runs"}, 400, "time_settings_invalid"),
    ({"key": "sam", "slack": [SLACK_ID]}, 400, "time_settings_invalid"),
    ({"key": "sam", "slack": "U0SAM"}, 400, "time_settings_invalid"),
    ({"key": "sam", "email": "sam@example.org"}, 400, "time_settings_invalid"),
])
def test_an_add_that_would_break_the_registry_is_refused_and_nothing_is_written(client, body, status, code):
    before = _stored()
    detail = _detail(client.post("/api/time/people", json=body), status)
    assert detail["code"] == code
    assert _stored() == before


def test_removing_the_default_person_clears_default_person(client):
    reply = _ok(client.delete(f"/api/time/people/{PERSON}"))

    assert "default_person" not in _stored()
    assert reply["default_person"] == {"stored": None, "effective": OTHER}
    assert _detail(client.delete("/api/time/people/nobody"), 404)["code"] == "time_person_unknown"


# --- defaults (operator) ---------------------------------------------------------

def test_defaults_are_patched_cleared_and_validated(client):
    reply = _ok(client.patch("/api/time/defaults", json={"idle_grace_minutes": "20", "writer_name": "Time Bot"}))
    assert (reply["defaults"]["idle_grace_minutes"]["value"], reply["defaults"]["idle_grace_minutes"]["source"]) == (
        20, "config.json")
    assert _stored()["time_writer_name"] == "Time Bot"
    assert timetrack.get_tracker().config.time_writer_name == "Time Bot"

    _ok(client.patch("/api/time/defaults", json={"idle_grace_minutes": None}))
    assert "time_idle_grace_minutes" not in _stored()

    for body in ({"idle_grace_minutes": 0}, {"pause_hours": -1}, {"idle_grace_minutes": True},
                 {"writer_email": "not an email"}, {"default_person": "nobody"}, {"color": "red"}, {}):
        assert _detail(client.patch("/api/time/defaults", json=body), 400)["code"] == "time_settings_invalid"


def test_the_switch_stops_and_restarts_tracking_and_keeps_the_registry(client):
    off = _ok(client.patch("/api/time/defaults", json={"tracking": False}))
    assert off["applied"] == timetrack_live.STOPPED and timetrack.get_tracker() is None
    assert off["tracking"]["switch"] is False and len(off["people"]) == 2
    detail = _detail(client.get("/api/time/status"), 503)
    assert "switched off" in detail["message"]

    on = _ok(client.patch("/api/time/defaults", json={"tracking": True}))
    assert on["applied"] == timetrack_live.STARTED and timetrack.get_tracker() is not None


def _kept_stops(person):
    with timesheet_store.database() as db:
        return [row["trigger_stop"] for row in db.execute("SELECT trigger_stop FROM kept WHERE person=?",
                                                          (person,))]


def test_switching_off_ends_a_running_timer_rather_than_leaving_it_to_accrue(client, docs):
    """A manual timer left open over an off period would come back with that period
    on it, and nobody could stop it while tracking was off."""
    _ok(client.post("/api/time/start", json={"project": docs}))
    _ok(client.patch("/api/time/defaults", json={"tracking": False}))
    with timesheet_store.database() as db:
        assert db.execute("SELECT COUNT(*) FROM timers").fetchone()[0] == 0
    assert _kept_stops(PERSON) == ["manual"], "a sliver: kept locally, stopped at the switch"

    _ok(client.patch("/api/time/defaults", json={"tracking": True}))
    assert _ok(client.get("/api/time/status"))["running"] is None


def test_removing_a_person_ends_their_running_timer(client, docs):
    _ok(client.post("/api/time/start", json={"project": docs}))
    _ok(client.delete(f"/api/time/people/{PERSON}"))
    assert _kept_stops(PERSON) == ["manual"]
    with timesheet_store.database() as db:
        assert db.execute("SELECT COUNT(*) FROM timers").fetchone()[0] == 0


def test_an_id_added_from_not_listed_yet_leaves_that_list(client, tracking):
    stranger = _record("s-1", author_id=STRANGER_ID, author="Sam")
    tracking(SimpleNamespace(message=stranger, session="s-1"))
    assert _ok(client.get("/api/time/settings"))["unmapped"] == [f"slack:{STRANGER_ID}"]

    reply = _ok(client.patch(f"/api/time/people/{OTHER}", json={"add": [f"slack:{STRANGER_ID}"]}))
    assert reply["unmapped"] == []
    assert _ok(client.get("/api/time/status"))["unmapped"] == []


def test_registry_problems_quote_ids_only_to_the_operator(client, ratchet, tracking, mirror):
    store.write_json(cfg.config_path(), {"people": [{"key": PERSON, "slack": ["U_PRIVATE"]},
                                                    {"key": OTHER, "slack": ["U_PRIVATE"]}],
                                         "work_repo_mirror": str(mirror)})
    tracking.reload(cfg.load)
    headers, _session = ratchet

    assert "U_PRIVATE" in str(_ok(client.get("/api/time/settings"))["tracking"]["problems"])
    for reply in (_ok(client.get("/api/time/settings", headers=headers))["tracking"],
                  _detail(client.get("/api/time/status", headers=headers), 503)):
        assert "U_PRIVATE" not in str(reply) and OTHER not in str(reply)
        assert reply["problems"] == ["config.json has 1 time-tracking problem(s); the operator sees them "
                                     "in Platform settings → Time tracking."]
    assert "U_PRIVATE" in str(_detail(client.get("/api/time/status"), 503)), "the operator still sees them"


def test_a_time_me_refusal_counts_registry_problems_rather_than_quoting_them(asked):
    """Listing the asker parses the whole stored registry, so its refusal would
    carry other people's ids; the assistant gets the count the other routes give."""
    http, headers, _session, request_id = asked([{"key": PERSON, "slack": ["U_PRIVATE"]},
                                                 {"key": OTHER, "slack": ["U_PRIVATE"]}])
    before = _stored()

    detail = _detail(http.post("/api/time/me", json={"key": "newbie"}, headers=headers), 400)

    count = ("config.json has 1 time-tracking problem(s); the operator sees them in Platform settings "
             "→ Time tracking.")
    assert (detail["code"], detail["message"], detail["problems"]) == ("time_settings_invalid", count, [count])
    assert "U_PRIVATE" not in str(detail) and OTHER not in str(detail)
    assert _stored() == before
    assert actions(request_id)[0]["state"] == "refused"


def test_a_start_in_flight_across_the_switch_opens_no_timer(client, docs):
    """A request that took the tracker before the switch and starts after its
    timers were closed would leave a timer open that nobody can stop."""
    held = timetrack.get_tracker()
    _ok(client.patch("/api/time/defaults", json={"tracking": False}))

    result = held.start(PERSON, docs, at=held.now(), trigger="manual")

    assert result.outcome == timetrack.REFUSED and "ended" in result.reasons[0]
    with timesheet_store.database() as db:
        assert db.execute("SELECT COUNT(*) FROM timers").fetchone()[0] == 0


def test_a_start_in_flight_for_a_removed_person_opens_no_timer_until_they_are_listed_again(client, docs):
    held = timetrack.get_tracker()
    _ok(client.delete(f"/api/time/people/{PERSON}"))

    assert held.start(PERSON, docs, at=held.now(), trigger="manual").outcome == timetrack.REFUSED
    with timesheet_store.database() as db:
        assert db.execute("SELECT COUNT(*) FROM timers").fetchone()[0] == 0

    _ok(client.post("/api/time/people", json={"key": PERSON, "slack": [SLACK_ID]}))
    assert held.start(PERSON, docs, at=held.now(), trigger="manual").outcome == timetrack.STARTED


# --- who may write ----------------------------------------------------------------

WRITES = [
    ("POST", "/api/time/people", {"key": "sam"}),
    ("PATCH", f"/api/time/people/{OTHER}", {"name": "B"}),
    ("DELETE", f"/api/time/people/{OTHER}", None),
    ("PATCH", "/api/time/defaults", {"idle_grace_minutes": 5}),
]


@pytest.mark.parametrize("method, path, body", WRITES)
def test_an_assistant_cannot_edit_people_or_defaults(client, ratchet, method, path, body):
    headers, _session = ratchet
    before = _stored()
    detail = _detail(client.request(method, path, json=body, headers=headers), 403)
    assert detail["code"] == "time_settings_operator_only"
    assert _stored() == before


@pytest.mark.parametrize("method, path, body", WRITES)
def test_a_connector_cannot_edit_people_or_defaults(client, method, path, body):
    connector = cfg.mint_connector_credential("slack-bridge", "slack")
    detail = _detail(client.request(method, path, json=body, headers=_bearer(connector)), 403)
    assert detail["code"] == "time_settings_operator_only"


# --- time/me (assistant) ------------------------------------------------------------

def test_an_assistant_lists_the_unmapped_person_it_talks_to(asked):
    http, headers, _session, _request_id = asked([PEOPLE[1]])

    reply = _ok(http.post("/api/time/me", json={"key": "sam", "name": "Sam", "person": PERSON,
                                                "slack": ["U0EVIL"]}, headers=headers))

    assert (reply["person"], reply["created"], reply["identity"]) == ("sam", True, f"slack:{SLACK_USER}")
    entry = next(person for person in _stored()["people"] if person["key"] == "sam")
    assert entry == {"key": "sam", "name": "Sam", "slack": [SLACK_USER]}, "the body names no identity"
    assert _ok(http.get("/api/time/status", headers=headers))["acting"]["person"] == "sam"


def test_an_assistant_cannot_attach_the_asker_to_someone_else(asked):
    http, headers, _session, _request_id = asked([PEOPLE[1]])
    before = _stored()

    assert _detail(http.post("/api/time/me", json={"key": OTHER}, headers=headers), 409)["code"] == (
        "time_person_exists")
    assert _stored() == before


def test_a_listed_asker_may_rename_only_themselves(asked):
    http, headers, _session, _request_id = asked(PEOPLE, default_person=PERSON)

    reply = _ok(http.post("/api/time/me", json={"name": "Alex P."}, headers=headers))
    assert (reply["person"], reply["created"]) == (PERSON, False)
    assert next(p for p in _stored()["people"] if p["key"] == PERSON)["name"] == "Alex P."
    assert _detail(http.post("/api/time/me", json={"key": "alex2"}, headers=headers), 409)["code"] == (
        "time_identity_listed")


def test_time_me_is_for_an_assistant(client):
    assert _detail(client.post("/api/time/me", json={"key": "sam"}), 403)["code"] == "time_settings_assistant_only"


def test_time_me_needs_a_human_who_asked(client, ratchet):
    headers, _session = ratchet
    assert _detail(client.post("/api/time/me", json={}, headers=headers), 422)["code"] == "time_no_record"


def test_outside_a_request_time_me_writes_nothing_whoever_posted(client, ratchet):
    """Two humans in a thread, no delivered request: the latest poster or a named
    record is not known to be the one who asked, so nobody is listed or renamed."""
    headers, session = ratchet
    asker = _journal(session, _record(session, author_id=STRANGER_ID, author="Sam"))
    _journal(session, _record(session, author_id="U0BYSTANDER", author="Bo"))
    before = _stored()

    for body in ({"key": "sam"}, {"key": "sam", "record": asker}, {"name": "Pwned"}):
        assert _detail(client.post("/api/time/me", json=body, headers=headers), 403)["code"] == "time_no_request"
    assert _stored() == before

    _journal(session, _record(session))
    reply = _ok(client.post("/api/time/me", json={}, headers=headers))
    assert (reply["person"], reply["created"]) == (PERSON, False), "a lookup writes nothing and still answers"


# --- a project's time settings ---------------------------------------------------------

def test_an_assistant_sets_its_own_projects_time_settings(client, ratchet, webshop, docs):
    headers, _session = ratchet

    reply = _ok(client.patch("/api/time/project", json={"handler": "board", "clients": ["acme"],
                                                       "idle_grace_minutes": 10}, headers=headers))

    assert (reply["workspace_id"], reply["handler"], reply["clients"], reply["idle_grace_minutes"]) == (
        webshop, "board", ["acme"], 10)
    settings = projects.get(webshop)["settings"]
    assert (settings["time_handler"], settings["time_clients"]) == ("board", ["acme"])
    assert _detail(client.patch("/api/time/project", json={"project": docs, "handler": "board"},
                                headers=headers), 403)["code"] == "time_other_project"


def test_the_operator_sets_any_projects_time_settings_and_bad_values_are_refused(client, docs):
    assert _ok(client.patch("/api/time/project", json={"project": docs, "clients": ["a", "b"]}))["clients"] == ["a", "b"]
    assert _ok(client.patch("/api/time/project", json={"project": docs, "clients": ""}))["clients"] is None
    assert _detail(client.patch("/api/time/project", json={"project": docs, "idle_grace_minutes": 0}),
                   400)["code"]
    assert _detail(client.patch("/api/time/project", json={"project": docs, "colour": "red"}), 400)["code"] == (
        "time_invalid")
    assert _detail(client.patch("/api/time/project", json={"clients": ["a"]}), 400)["code"] == (
        "time_project_required")


# --- handler definitions -----------------------------------------------------------------

def test_a_handler_definition_is_checked_with_the_daemons_parser(client):
    good = _ok(client.post("/api/time/handlers/check", json={"name": "board", "text": HANDLER}))
    assert good["ok"] and good["problems"] == []
    assert good["handler"]["fields"][0] == {"key": "task_id", "label": "task_id", "required": True,
                                           "pattern": "[a-z0-9]+", "hint": None, "link": None,
                                           "per_issue": False}

    renamed = _ok(client.post("/api/time/handlers/check", json={"name": "other", "text": HANDLER}))
    assert not renamed["ok"] and "does not match the file name" in renamed["problems"][0]
    broken = _ok(client.post("/api/time/handlers/check", json={"name": "board", "text": "fields: [{label: x}]"}))
    assert not broken["ok"]
    builtin = _ok(client.post("/api/time/handlers/check", json={"name": "timesheet", "text": "name: timesheet"}))
    assert not builtin["ok"]
    assert _detail(client.post("/api/time/handlers/check", json={"text": HANDLER}), 400)["code"] == "time_invalid"


def test_settings_work_while_tracking_is_off(client):
    _ok(client.patch("/api/time/defaults", json={"tracking": False}))
    assert _ok(client.get("/api/time/settings"))["tracking"]["runs"] is False
    assert _ok(client.post("/api/time/people", json={"key": "sam"}))["applied"] == timetrack_live.OFF


def test_a_live_apply_keeps_the_daemons_own_overrides(client, tracking, platform):
    mirror = tracking.tracker.config.work_repo_mirror
    _ok(client.patch("/api/time/defaults", json={"idle_grace_minutes": 7}))
    assert tracking.tracker.config.work_repo_mirror == mirror == platform.work_repo_mirror
    assert tracking.tracker.config.lmer_bin == platform.lmer_bin


# --- refusals, the default person, project keys and the shared drain lock ---------------

def test_a_connector_cannot_change_a_projects_time_settings(client, docs):
    connector = cfg.mint_connector_credential("slack-bridge", "slack")
    before = projects.get(docs)["settings"]
    detail = _detail(client.patch("/api/time/project", json={"project": docs, "clients": ["evil"]},
                                  headers=_bearer(connector)), 403)
    assert detail["code"] == "time_caller_refused"
    assert projects.get(docs)["settings"] == before


def _sole_person(mirror):
    store.write_json(cfg.config_path(), {"people": [dict(PEOPLE[0])], "work_repo_mirror": str(mirror)})


def test_listing_a_second_person_keeps_the_implicit_default(client, mirror):
    _sole_person(mirror)
    reply = _ok(client.post("/api/time/people", json={"key": "sam", "slack": ["U0SAM"]}))
    assert _stored()["default_person"] == PERSON
    assert reply["default_person"] == {"stored": PERSON, "effective": PERSON}
    assert _ok(client.get("/api/time/status"))["person"] == PERSON


def test_an_asker_listed_through_chat_never_moves_the_default_person(asked):
    http, headers, _session, _request_id = asked([PEOPLE[1]])
    _ok(http.post("/api/time/me", json={"key": "sam"}, headers=headers))
    assert _stored()["default_person"] == OTHER
    assert _ok(http.get("/api/time/status"))["person"] == OTHER


def test_chat_cannot_list_the_first_person(asked):
    http, headers, _session, request_id = asked([])
    assert _detail(http.post("/api/time/me", json={"key": "sam"}, headers=headers), 409)["code"] == (
        "time_registry_empty")
    assert _stored()["people"] == []
    assert actions(request_id)[0]["state"] == "refused"


def test_an_assistant_reads_only_its_own_project_and_no_ones_ids(client, ratchet, webshop):
    headers, _session = ratchet
    reply = _ok(client.get("/api/time/settings", headers=headers))
    assert [row["workspace_id"] for row in reply["projects"]] == [webshop]
    assert reply["people"] == [] and reply["unmapped"] == []
    assert len(_ok(client.get("/api/time/settings"))["people"]) == 2, "the operator still sees everyone"


def test_a_project_key_cannot_be_cleared(client, docs):
    assert _detail(client.patch("/api/time/project", json={"project": docs, "project_key": ""}), 400)["code"] == (
        "time_invalid")


def test_a_project_key_taken_inside_a_request_is_a_refusal_not_an_uncertain_action(asked):
    http, headers, _session, request_id = asked(PEOPLE, default_person=PERSON)
    beta = projects.create("Beta")
    projects.update(beta["id"], {"settings": {"time_project_key": "beta"}})

    detail = _detail(http.patch("/api/time/project", json={"project_key": "beta"}, headers=headers), 409)
    assert detail["code"] == "project_key_taken"
    assert actions(request_id)[0]["state"] == "refused", "nothing was written, so a retry may run"


def test_inside_a_request_only_the_requesters_record_speaks_for_them(asked):
    http, headers, session, _request_id = asked(PEOPLE, default_person=PERSON)
    bystander = _journal(session, _record(session, author_id="U0BLAIR", author="Blair"))

    detail = _detail(http.post("/api/time/me", json={"name": "Pwned", "record": bystander}, headers=headers), 403)
    assert detail["code"] == "time_record_not_asker"
    assert next(p for p in _stored()["people"] if p["key"] == OTHER)["name"] == "Blair"

    reply = _ok(http.post("/api/time/me", json={"name": "Alex A."}, headers=headers))
    assert (reply["person"], reply["identity"]) == (PERSON, f"slack:{SLACK_USER}")
    assert next(p for p in _stored()["people"] if p["key"] == PERSON)["name"] == "Alex A."


def test_writers_share_one_drain_lock():
    first = timesheet_writer.TimesheetWriter(cfg.load())
    second = timesheet_writer.TimesheetWriter(cfg.load())
    assert first._drain_lock is second._drain_lock, (
        "a writer whose stop timed out would drain the shared clone beside its successor")
