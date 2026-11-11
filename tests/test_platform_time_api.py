"""The time API, its acting person and the entries join (issue #604, task T8).

Every test runs the real FastAPI app over a real projects database, a real
tracker on an injected clock and a fixture work-repo mirror defining one
handler, ``tracker-x``, with ``sync: true`` and a required, patterned field.
Assistant tests start real project assistants (the ``fleet`` fixture's sleeping
``lmer``) and authenticate with their minted credentials.

What must hold (spec D9, plan P8/P14): the operator acts for
``default_person``; an assistant acts for the human behind the record that
carried the request, found by the daemon on the assistant's own session or
from the conversation request it is handling, never from a name in the body;
unmapped, foreign and non-human records are refused, as is an assistant on
another project and any connector. The entries join (D18) shows every stage
and every ledger status, flags broken hand-written files, and lists an entry
the writer pushed before the mirror has it. Issue #648: the status offers
the mirror registry's short names as project-key suggestions and stores
nothing, and an assistant's manual start names its own run.
"""
import json
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from lmer_cli import comms as bus
from lmer_platform import (
    api, comms, project_requests, projects, timesheet_store, timesheet_writer, timetrack,
    timetrack_activity,
)
from lmer_platform import config as cfg
from lmer_platform import project_context as scope
from lmer_platform.comms import FleetEvent
from lmer_platform.people import CREDENTIAL_OPERATOR, META_CREDENTIAL, Registry
from lmer_platform.timetrack import Tracker
from lmer_platform.timetrack_activity import ActivityConsumer
from lmer_platform.timetrack_runs import RunFacts
from lmer_timesheet import (
    Entry, LedgerLine, SessionRef, Trigger, entry_hash, entry_path, ledger_path, parse_entry,
)
from tests.test_assistant_mentions import SLACK_USER, body
from tests.test_platform_projects import fleet  # noqa: F401
from tests.test_platform_project_transports import _link, _post, transport_host  # noqa: F401
from tests.test_platform_timetrack import Clock
from tests.test_project_review_requests import delivered

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
PERSON = "alex"
OTHER = "blair"
SLACK_ID = "U0ALEX"
STRANGER_ID = "U0STRANGER"
PEOPLE = [
    {"key": PERSON, "name": "Alex", "slack": [SLACK_ID, SLACK_USER]},
    {"key": OTHER, "name": "Blair", "slack": ["U0BLAIR"]},
]
HELD_REASON = "no Tracker X identity for alex"
LINK = "https://tracker-x.example.com/entries/"

TIME_ROUTES = [
    ("GET", "/api/time/status"),
    ("POST", "/api/time/start"),
    ("POST", "/api/time/stop"),
    ("PATCH", "/api/time/current"),
    ("PUT", "/api/time/auto"),
    ("GET", "/api/time/entries"),
]
#: The settings routes (issue #668): they work while tracking is off, so they
#: are not in the 503 sweep below; tests/test_platform_time_settings_api.py.
SETTINGS_ROUTES = [
    ("GET", "/api/time/settings"),
    ("POST", "/api/time/people"),
    ("PATCH", "/api/time/people/{key}"),
    ("DELETE", "/api/time/people/{key}"),
    ("PATCH", "/api/time/defaults"),
    ("POST", "/api/time/me"),
    ("PATCH", "/api/time/project"),
    ("POST", "/api/time/handlers/check"),
]


@pytest.fixture
def clock():
    return Clock(T0)


@pytest.fixture
def mirror(tmp_path):
    root = tmp_path / "mirror"
    handlers = root / "timesheet.d" / ".handlers"
    handlers.mkdir(parents=True)
    (handlers / "tracker-x.yaml").write_text(
        "name: tracker-x\nlabel: Tracker X\nsync: true\n"
        "fields:\n  - key: task_id\n    label: Tracker X task\n    required: true\n"
        "    pattern: '[a-z0-9]+'\n    hint: the task's short id\n"
        "    link: https://tracker-x.example.com/t/{value}\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def platform(fleet, mirror, monkeypatch):
    projects.migrate()
    monkeypatch.setattr(comms, "_HUB", comms.CommsHub())
    return replace(fleet, people=[dict(person) for person in PEOPLE], default_person=PERSON,
                   work_repo_mirror=str(mirror))


@pytest.fixture
def tracker(platform, clock):
    tracker = Tracker(platform, now=clock)
    timetrack.register_tracker(tracker)
    yield tracker
    timetrack.register_tracker(None)
    timesheet_writer.register_writer(None)
    timetrack_activity.register_activity(None)


@pytest.fixture
def client(platform):
    app = api.create_app(platform, cfg.ensure_secret(platform), state_builder=lambda *_a, **_k: {"runs": []})
    with TestClient(app, headers=_bearer(cfg.ensure_secret(platform))) as http:
        yield http


@pytest.fixture
def webshop(platform):
    row = projects.create("Webshop")
    return projects.update(row["id"], {"settings": {
        "time_handler": "tracker-x", "time_clients": ["acme", "globex"]}})["id"]


@pytest.fixture
def docs(platform):
    return projects.create("Docs Site")["id"]


def _bearer(value):
    return {"Authorization": f"Bearer {value}"}


def _assistant(workspace_id):
    """The project assistant's minted credential and its session."""
    with scope.bound(workspace_id):
        minted = cfg.active_assistant_credential()
    return _bearer(minted), projects.status(workspace_id)["session_id"]


@pytest.fixture
def ratchet(platform, webshop, docs):
    """Webshop's assistant, running: ``(headers, session)``."""
    projects.reconcile(platform)
    return _assistant(webshop)


_SEQ = {}


def _record(session, *, author_id=SLACK_ID, channel=bus.CHANNEL_SLACK, kind="message",
            author="Alex", at=T0):
    seq = _SEQ[session] = _SEQ.get(session, 0) + 1
    return bus.Message(
        id=bus.message_id(session, seq), seq=seq, session=session,
        at=at.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        source=bus.Source(channel, author=author, ref=f"C0123:1700000000.0001:17000001{seq:02d}.0001",
                          author_id=author_id),
        kind=kind, text="start my time on the webshop", meta={},
    ).to_dict()


def _journal(session, message):
    assert comms.ensure_hub()._ingest(session, message) is True
    return message["id"]


def _detail(response, status):
    assert response.status_code == status, response.text
    return response.json()["detail"]


def _timer_person():
    with timesheet_store.database() as db:
        return [row["person"] for row in db.execute("SELECT person FROM timers")]


# --- the routes and the index -------------------------------------------------

def test_the_index_lists_every_time_route(client):
    index = client.get("/api").text
    for method, path in TIME_ROUTES + SETTINGS_ROUTES:
        assert f"{method} {path}" in " ".join(index.split()), f"{method} {path} is not in GET /api"
    served = {(method, route.path) for route in client.app.routes
              for method in getattr(route, "methods", ()) if route.path.startswith("/api/time/")}
    assert served == set(TIME_ROUTES) | set(SETTINGS_ROUTES)


@pytest.mark.parametrize("method, path", TIME_ROUTES)
def test_every_time_route_needs_the_credential(platform, tracker, method, path):
    app = api.create_app(platform, cfg.ensure_secret(platform), state_builder=lambda *_a, **_k: {})
    assert TestClient(app).request(method, path).status_code == 401


@pytest.mark.parametrize("method, path", TIME_ROUTES)
def test_tracking_off_is_a_503_that_says_why(client, method, path):
    detail = _detail(client.request(method, path, json={}), 503)
    assert detail["code"] == "time_tracking_off"
    assert detail["message"].startswith("Time tracking is not running")


# --- the operator: default_person ----------------------------------------------

def test_operator_start_stop_round_trip_acts_for_the_default_person(client, tracker, clock, docs):
    started = client.post("/api/time/start", json={"project": docs, "title": "release notes"})
    assert started.status_code == 200, started.text
    payload = started.json()
    assert payload["acting"] == {"person": PERSON, "name": "Alex", "via": "default_person",
                                 "record": None, "request_id": None}
    assert (payload["outcome"], payload["timer"]["workspace_id"], payload["timer"]["mode"]) == (
        "started", docs, "manual")
    assert payload["timer"]["start"] == "2026-10-01T09:00:00Z"

    clock.advance(minutes=30)
    status = client.get("/api/time/status").json()
    assert status["person"] == PERSON
    assert status["running"]["elapsed_seconds"] == 1800
    assert status["running"]["title"] == "release notes"

    stopped = client.post("/api/time/stop", json={}).json()
    assert stopped["outcome"] == "stopped"
    assert (stopped["entry"]["person"], stopped["entry"]["duration"]) == (PERSON, 1800)
    assert [row.entry_id for row in timesheet_store.pending()] == [stopped["entry"]["id"]]
    assert client.get("/api/time/status").json()["running"] is None


def test_a_person_or_name_in_the_body_never_picks_the_person(client, tracker, docs):
    payload = client.post("/api/time/start", json={
        "project": docs, "person": OTHER, "name": "Blair", "author": "Blair"}).json()
    assert payload["acting"]["person"] == PERSON
    assert _timer_person() == [PERSON]


def test_operator_without_a_default_person_is_refused(platform, tracker, docs, monkeypatch):
    monkeypatch.setattr(tracker, "config", replace(platform, default_person=None))
    app = api.create_app(platform, cfg.ensure_secret(platform), state_builder=lambda *_a, **_k: {})
    http = TestClient(app, headers=_bearer(cfg.ensure_secret(platform)))
    for method, path in TIME_ROUTES:
        detail = _detail(http.request(method, path, json={"project": docs}), 409)
        assert detail["code"] == "time_no_default_person"
        assert "default_person" in detail["message"]
    assert _timer_person() == []


def test_operator_cannot_name_a_record(client, tracker, docs):
    detail = _detail(client.post("/api/time/start", json={"project": docs, "record": "s/000001"}), 400)
    assert detail["code"] == "time_record_not_operator"


def test_a_connector_credential_is_refused(platform, client, tracker, docs):
    connector = cfg.mint_connector_credential("slack-bridge", "slack")
    for method, path in TIME_ROUTES:
        detail = _detail(client.request(method, path, json={"project": docs}, headers=_bearer(connector)), 403)
        assert detail["code"] == "time_caller_refused"
    assert _timer_person() == []


def test_a_start_names_its_project(client, tracker):
    assert _detail(client.post("/api/time/start", json={}), 400)["code"] == "time_project_required"


# --- required handler fields and client labels (D19, D12) ----------------------

def test_a_manual_start_is_refused_until_the_required_field_is_set(client, tracker, webshop):
    refused = _detail(client.post("/api/time/start", json={"project": webshop}), 422)
    assert (refused["code"], refused["outcome"], refused["missing"]) == ("time_refused", "refused", ["task_id"])
    assert "task_id: required (Tracker X task)" in refused["message"]
    assert _timer_person() == []

    bad = _detail(client.post("/api/time/start", json={"project": webshop, "fields": {"task_id": "NOT OK"}}), 422)
    assert bad["missing"] == ["task_id"]

    accepted = client.post("/api/time/start", json={"project": webshop, "fields": {"task_id": "abc123"}})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["timer"]["fields"] == {"task_id": "abc123"}


def test_a_new_client_label_on_start_is_added_to_the_project(client, tracker, webshop):
    payload = client.post("/api/time/start", json={
        "project": webshop, "client": "initech", "fields": {"task_id": "abc123"}}).json()
    assert payload["timer"]["client"] == "initech"
    assert projects.get(webshop)["settings"]["time_clients"] == ["acme", "globex", "initech"]
    options = client.get("/api/time/status").json()["projects"][webshop]
    assert options["clients"] == ["acme", "globex", "initech"]


def test_patch_current_edits_the_running_timer(client, tracker, clock, webshop):
    client.post("/api/time/start", json={"project": webshop, "fields": {"task_id": "abc123"}})
    edited = client.patch("/api/time/current", json={"title": "checkout bug", "fields": {"task_id": "def456"}})
    assert edited.status_code == 200, edited.text
    assert edited.json()["outcome"] == "updated"
    clock.advance(minutes=5)
    entry = client.post("/api/time/stop", json={}).json()["entry"]
    assert (entry["title"], entry["fields"]) == ("checkout bug", {"task_id": "def456"})
    assert _detail(client.patch("/api/time/current", json={"title": "x"}), 422)["reasons"] == [
        "no timer is running"]


def test_status_offers_every_live_project_with_its_handler_and_recent_values(
        client, tracker, clock, webshop, docs):
    client.post("/api/time/start", json={"project": webshop, "title": "checkout", "client": "globex",
                                         "fields": {"task_id": "abc123"}})
    clock.advance(minutes=10)
    client.post("/api/time/stop", json={})
    status = client.get("/api/time/status").json()

    assert set(status["projects"]) >= {webshop, docs}
    shop = status["projects"][webshop]
    assert shop["project_key"] == "webshop"
    assert shop["handler"]["name"] == "tracker-x"
    assert shop["handler"]["fields"] == [{
        "key": "task_id", "label": "Tracker X task", "required": True, "pattern": "[a-z0-9]+",
        "hint": "the task's short id", "link": "https://tracker-x.example.com/t/{value}",
        "per_issue": False}]
    assert shop["recent"] == {"titles": ["checkout"], "clients": ["globex"], "fields": {"task_id": ["abc123"]}}
    assert status["projects"][docs]["handler"]["name"] == "timesheet"
    assert {handler["name"] for handler in status["handlers"]} == {"timesheet", "tracker-x"}
    assert status["pause"] is None, "the default pause after a manual stop is none"
    assert status["automatic"] == {"on": True, "off_since": None}
    assert status["outbox"]["depth"] == 1


def test_status_lists_unmapped_identities_the_activity_consumer_saw(client, tracker, platform):
    consumer = ActivityConsumer(platform, tracker, Registry.from_config(platform))
    timetrack_activity.register_activity(consumer)
    consumer(FleetEvent(cursor=1, session="s-worker", message=_record("s-worker", author_id=STRANGER_ID)))
    assert client.get("/api/time/status").json()["unmapped"] == [f"slack:{STRANGER_ID}"]


# --- one project identity: registry short names as key suggestions (#648) ---

MANIFEST = """\
schema: 1
name: test
projects:
  widgets:
    repo: https://git.example.com/acme/widgets.git
  docs-site:
    repo: https://git.example.com/acme/docs.git
  a--b:
    repo: https://git.example.com/acme/ab.git
  napkin:
    description: a project with no repository
"""


def _suggestions(client, workspace_id, **kwargs):
    return client.get("/api/time/status", **kwargs).json()["projects"][workspace_id]["key_suggestions"]


def test_status_suggests_registry_short_names_matched_first(client, tracker, mirror, webshop, docs):
    (mirror / "_lmer.yaml").write_text(MANIFEST, encoding="utf-8")
    projects.update(webshop, {"repositories": ["git@git.example.com:acme/widgets.git"]})

    assert _suggestions(client, webshop) == [
        {"key": "widgets", "matched": True, "taken": False},
        {"key": "docs-site", "matched": False, "taken": True},
        {"key": "napkin", "matched": False, "taken": False},
    ], "matched first; Docs Site holds docs-site; a--b can never be a project key"
    assert _suggestions(client, docs)[0] == {"key": "widgets", "matched": False, "taken": False}
    status = client.get("/api/time/status").json()
    assert status["projects"][webshop]["project_key"] == "webshop", "a suggestion stores nothing"


def test_a_picked_suggestion_is_the_key_and_is_then_taken(client, tracker, mirror, webshop, docs):
    (mirror / "_lmer.yaml").write_text(MANIFEST, encoding="utf-8")
    settings = projects.get(webshop)["settings"]
    assert client.patch(f"/api/projects/{webshop}", json={
        "settings": {**settings, "time_project_key": "widgets"}}).status_code == 200
    status = client.get("/api/time/status").json()
    assert status["projects"][webshop]["project_key"] == "widgets"
    assert {"key": "widgets", "matched": False, "taken": False} in status["projects"][webshop]["key_suggestions"]
    assert {"key": "widgets", "matched": False, "taken": True} in status["projects"][docs]["key_suggestions"]
    refused = client.patch(f"/api/projects/{docs}", json={"settings": {"time_project_key": "widgets"}})
    assert refused.status_code == 409, refused.text


def test_the_first_status_read_marks_taken_the_keys_it_allocates(client, tracker, mirror):
    (mirror / "_lmer.yaml").write_text(
        MANIFEST + "  widgets-2:\n    repo: https://git.example.com/acme/widgets-2.git\n",
        encoding="utf-8")
    alpha = projects.create("Widgets")["id"]
    beta = projects.create("Widgets!")["id"]

    shown = client.get("/api/time/status").json()["projects"]
    assert {shown[alpha]["project_key"], shown[beta]["project_key"]} == {"widgets", "widgets-2"}
    for own, other in ((alpha, beta), (beta, alpha)):
        taken = {item["key"]: item["taken"] for item in shown[own]["key_suggestions"]}
        assert taken[shown[own]["project_key"]] is False, "its own key is free to keep"
        assert taken[shown[other]["project_key"]] is True, "the other's key cannot be picked"


def test_no_manifest_suggests_nothing(client, tracker, webshop):
    assert _suggestions(client, webshop) == []


@pytest.mark.parametrize("raw", [
    b"schema: [not, an, int]\nprojects: 3\n",
    MANIFEST.encode("utf-8") + b"    description: caf\xe9\n",
], ids=["unusable", "not-utf-8"])
def test_an_unreadable_manifest_suggests_nothing_and_the_status_still_answers(
        client, tracker, mirror, webshop, raw):
    (mirror / "_lmer.yaml").write_bytes(raw)
    response = client.get("/api/time/status")
    assert response.status_code == 200, response.text
    assert response.json()["projects"][webshop]["key_suggestions"] == []


def test_an_assistant_is_suggested_no_keys(client, tracker, mirror, ratchet, webshop):
    (mirror / "_lmer.yaml").write_text(MANIFEST, encoding="utf-8")
    headers, session = ratchet
    _journal(session, _record(session))
    assert _suggestions(client, webshop, headers=headers) == []


def test_time_key_suggestions_skip_a_repository_url_the_registry_cannot_read():
    class Registry:
        def __iter__(self):
            return iter(["widgets"])

        def lookup(self, url):
            if url == "bad":
                raise ValueError("not a URL")
            return SimpleNamespace(name="widgets")

    assert api.time_key_suggestions(Registry(), ["bad", "good"], "w1", {"w1": "own"}) == [
        {"key": "widgets", "matched": True, "taken": False}]
    assert api.time_key_suggestions(None, ["good"], "w1", {}) == []


# --- the run a manual start came from (#648) ---------------------------------

UID_A = "981ec036134f4a8580927ddca71658a9"


def test_an_assistants_manual_start_names_its_run_and_the_operators_none(
        client, tracker, clock, ratchet, webshop, docs, monkeypatch):
    headers, session = ratchet
    asked = []

    def run_of_session(session_id):
        asked.append(session_id)
        return {"host": "forge.test", "project": "team/app", "slug": "fix-login", "uid": UID_A}

    monkeypatch.setattr(timetrack_activity.runs, "run_of_session", run_of_session)
    _journal(session, _record(session))
    started = client.post("/api/time/start", headers=headers, json={
        "project": webshop, "fields": {"task_id": "abc123"}})
    assert started.status_code == 200, started.text
    assert asked == [session]
    ref = {"run": f"forge.test/team/app/{UID_A}", "uid": UID_A}
    assert started.json()["timer"]["sessions"] == [ref]
    clock.advance(minutes=5)
    client.post("/api/time/stop", headers=headers, json={})
    [entry] = [parse_entry(row.entry_yaml)[0] for row in timesheet_store.pending()]
    assert [item.to_dict() for item in entry.sessions] == [ref]

    asked.clear()
    operator = client.post("/api/time/start", json={"project": docs})
    assert operator.status_code == 200, operator.text
    assert operator.json()["timer"]["sessions"] == []
    assert asked == [], "the operator credential is no session's"


# --- the assistant: the record that carried the request (D9, P8) -------------

def test_end_to_end_a_slack_record_on_the_session_picks_the_person(client, tracker, ratchet, webshop):
    headers, session = ratchet
    record = _journal(session, _record(session))
    started = client.post("/api/time/start", headers=headers, json={
        "project": webshop, "fields": {"task_id": "abc123"}, "person": OTHER, "name": "Blair"})
    assert started.status_code == 200, started.text
    assert started.json()["acting"] == {"person": PERSON, "name": "Alex", "via": "session",
                                        "record": record, "request_id": None}
    assert _timer_person() == [PERSON]

    status = client.get("/api/time/status", headers=headers).json()
    assert (status["person"], status["running"]["workspace_id"]) == (PERSON, webshop)
    assert list(status["projects"]) == [webshop], "an assistant sees only its own project"
    assert client.post("/api/time/stop", headers=headers, json={}).json()["acting"]["person"] == PERSON


def test_the_latest_human_record_wins_and_an_unmapped_one_is_refused(client, tracker, ratchet, webshop):
    headers, session = ratchet
    _journal(session, _record(session, author_id="U0BLAIR", author="Blair"))
    _journal(session, _record(session, channel=bus.CHANNEL_UBER, author=None, author_id=None))
    assert client.get("/api/time/status", headers=headers).json()["person"] == OTHER

    _journal(session, _record(session, author_id=STRANGER_ID, author="Someone"))
    detail = _detail(client.post("/api/time/start", headers=headers, json={"project": webshop}), 403)
    assert detail["code"] == "time_identity_unmapped"
    assert f"slack:{STRANGER_ID}" in detail["message"]
    assert _timer_person() == []


def test_an_explicit_record_on_the_own_session_names_the_person(client, tracker, ratchet, webshop):
    headers, session = ratchet
    blair = _journal(session, _record(session, author_id="U0BLAIR", author="Blair"))
    _journal(session, _record(session))
    payload = client.post("/api/time/start", headers=headers, json={
        "project": webshop, "record": blair, "fields": {"task_id": "abc123"}}).json()
    assert (payload["acting"]["person"], payload["acting"]["via"]) == (OTHER, "record")


def test_a_record_from_another_session_is_refused(client, tracker, ratchet, webshop):
    headers, session = ratchet
    foreign = _journal("s-worker-1", _record("s-worker-1"))
    detail = _detail(client.post("/api/time/start", headers=headers, json={
        "project": webshop, "record": foreign, "fields": {"task_id": "abc123"}}), 403)
    assert detail["code"] == "time_record_foreign"
    assert _timer_person() == []


def test_an_unmapped_record_is_refused(client, tracker, ratchet, webshop):
    headers, session = ratchet
    stranger = _journal(session, _record(session, author_id=STRANGER_ID))
    detail = _detail(client.post("/api/time/start", headers=headers, json={
        "project": webshop, "record": stranger, "fields": {"task_id": "abc123"}}), 403)
    assert detail["code"] == "time_identity_unmapped"


def test_a_non_human_record_is_refused(client, tracker, ratchet, webshop):
    headers, session = ratchet
    own = _journal(session, _record(session, channel=bus.CHANNEL_UBER, author=None, author_id=None))
    assert _detail(client.post("/api/time/stop", headers=headers, json={"record": own}), 422)["code"] == (
        "time_record_not_human")


def test_no_human_record_at_all_is_refused(client, tracker, ratchet):
    headers, _session = ratchet
    assert _detail(client.get("/api/time/status", headers=headers), 422)["code"] == "time_no_record"


def test_automatic_tracking_is_turned_off_and_on_for_the_acting_person(client, tracker, clock, ratchet, docs):
    assert _detail(client.put("/api/time/auto", json={"on": "off"}), 400)["code"] == "time_invalid"
    assert _detail(client.put("/api/time/auto", json={}), 400)["code"] == "time_invalid"

    off = client.put("/api/time/auto", json={"on": False})
    assert off.status_code == 200, off.text
    assert off.json()["automatic"] == {"on": False, "off_since": "2026-10-01T09:00:00Z"}
    assert off.json()["acting"]["person"] == PERSON
    assert client.get("/api/time/status").json()["automatic"]["on"] is False

    # An assistant flips it for whoever asked, without seeing a timer on another project.
    headers, session = ratchet
    _journal(session, _record(session))
    client.post("/api/time/start", json={"project": docs})
    on = client.put("/api/time/auto", headers=headers, json={"on": True})
    assert on.status_code == 200, on.text
    assert (on.json()["automatic"]["on"], on.json()["timer"]) == (True, None)
    assert client.get("/api/time/status").json()["automatic"] == {"on": True, "off_since": None}


def test_an_assistant_cannot_start_on_another_project(client, tracker, ratchet, docs):
    headers, session = ratchet
    _journal(session, _record(session))
    detail = _detail(client.post("/api/time/start", headers=headers, json={"project": docs}), 403)
    assert detail["code"] == "time_other_project"
    assert _timer_person() == []
    assert _detail(client.get("/api/time/entries", headers=headers, params={"project": docs}), 403)[
        "code"] == "time_other_project"


def test_an_assistant_cannot_stop_a_timer_on_another_project(client, tracker, ratchet, docs):
    headers, session = ratchet
    _journal(session, _record(session))
    assert client.post("/api/time/start", json={"project": docs}).status_code == 200
    refused = client.post("/api/time/stop", headers=headers, json={})
    detail = _detail(refused, 422)
    assert detail.pop("acting")["person"] == PERSON
    assert detail == {
        "code": "time_refused", "message": "a timer is running on another project",
        "outcome": "refused", "ok": False, "reasons": ["a timer is running on another project"],
        "warnings": [], "missing": [], "timer": None, "entry": None, "seq": None, "kept": None,
    }, "the refusal describes the other project's timer"
    assert projects.project_key(docs) not in refused.text
    assert _detail(client.post("/api/time/stop", headers=headers, json={"project": docs}), 403)[
        "code"] == "time_other_project"
    assert _detail(client.patch("/api/time/current", headers=headers, json={"title": "x"}), 403)[
        "code"] == "time_other_project"
    assert _timer_person() == [PERSON]


def _prompt(workspace_id):
    with timesheet_store.database() as db:
        db.execute("INSERT INTO prompts (person, workspace_id, project_key, missing, at) VALUES (?,?,?,?,?)",
                   (PERSON, workspace_id, projects.project_key(workspace_id),
                    json.dumps([{"key": "task_id", "label": "Tracker X task"}]),
                    T0.isoformat(timespec="microseconds")))


def test_an_assistant_sees_only_that_a_timer_runs_elsewhere(client, tracker, clock, ratchet, webshop, docs):
    """P14: Webshop's assistant learns that Docs Site has a timer, and nothing about it."""
    headers, session = ratchet
    _journal(session, _record(session))
    client.post("/api/time/start", json={"project": docs})
    clock.advance(minutes=10)
    stopped = client.post("/api/time/stop", json={}).json()
    timesheet_store.mark_failed(stopped["seq"], "push refused for alex/2026/10/docs-site entry")
    client.post("/api/time/start", json={"project": docs, "title": "release notes", "client": "initech"})
    _prompt(docs)
    _prompt(webshop)

    status = client.get("/api/time/status", headers=headers).json()
    assert (status["running"], status["running_elsewhere"]) == (None, True)
    assert status["warnings"] == []
    assert [prompt["workspace_id"] for prompt in status["prompts"]] == [webshop]
    assert (status["writer_refusal"], status["outbox"]["last_error"]) == (None, None)
    assert list(status["projects"]) == [webshop]
    text = json.dumps(status)
    for detail in ("release notes", "initech", "Docs Site", "docs-site", docs):
        assert detail not in text, f"{detail!r} is another project's"

    operator = client.get("/api/time/status").json()
    assert operator["running_elsewhere"] is False
    assert (operator["running"]["title"], operator["running"]["client"]) == ("release notes", "initech")
    assert {prompt["workspace_id"] for prompt in operator["prompts"]} == {docs, webshop}
    assert "push refused" in operator["writer_refusal"]


@pytest.mark.parametrize("ran", [timedelta(minutes=20), timedelta(seconds=5)], ids=["queued", "kept"])
def test_an_assistant_start_on_its_own_project_switches_a_timer_elsewhere(
        client, tracker, clock, ratchet, webshop, docs, ran):
    """One running timer per person, and the person asked for this start; stopping stays home-only."""
    headers, session = ratchet
    _journal(session, _record(session))
    client.post("/api/time/start", json={"project": docs, "title": "release notes"})
    clock.advance(seconds=ran.total_seconds())

    started = client.post("/api/time/start", headers=headers, json={
        "project": webshop, "fields": {"task_id": "abc123"}})
    assert started.status_code == 200, started.text
    payload = started.json()
    assert (payload["outcome"], payload["timer"]["workspace_id"]) == ("switched", webshop)
    assert (payload["entry"], payload["seq"], payload["kept"]) == (None, None, None), \
        "the finished entry is another project's"
    assert "minimum" not in started.text and "docs-site" not in started.text
    if ran < timedelta(minutes=1):
        assert [entry["project"] for entry in tracker.kept_entries(PERSON)] == ["docs-site"]
    else:
        finished = [parse_entry(row.entry_yaml)[0] for row in timesheet_store.pending()]
        assert [(entry.project, entry.title, entry.trigger.stop) for entry in finished] == [
            ("docs-site", "release notes", "switch")]

    status = client.get("/api/time/status", headers=headers).json()
    assert (status["running"]["workspace_id"], status["running_elsewhere"]) == (webshop, False)
    assert status["running"]["fields"] == {"task_id": "abc123"}


def test_the_conversation_request_names_the_person(transport_host, fleet, mirror, clock, monkeypatch):
    """The request this credential is handling is the carrier: its sender, not the session."""
    monkeypatch.setattr(comms, "_HUB", comms.CommsHub())
    platform = replace(fleet, people=[dict(person) for person in PEOPLE], default_person=PERSON,
                       work_repo_mirror=str(mirror))
    timetrack.register_tracker(Tracker(platform, now=clock))
    try:
        client, _transports, _component = transport_host
        project, _request, request_id, headers = delivered(transport_host, fleet)
        session = projects.status(project["id"])["session_id"]
        _journal(session, _record(session, author_id="U0BLAIR", author="Blair"))
        started = client.post("/api/time/start", headers=headers, json={"project": project["id"]})
        assert started.status_code == 200, started.text
        assert started.json()["acting"] == {"person": PERSON, "name": "Alex", "via": "request",
                                            "record": None, "request_id": request_id}
        again = client.post("/api/time/start", headers=headers, json={"project": project["id"]})
        assert again.json() == started.json(), "a retried action returns its receipt"
    finally:
        timetrack.register_tracker(None)


@pytest.fixture
def blair_asks(transport_host, fleet, mirror, clock, monkeypatch):
    """A platform where the Slack requester (``SLACK_USER``) is Blair, not the default person."""
    monkeypatch.setattr(comms, "_HUB", comms.CommsHub())
    platform = replace(fleet, default_person=PERSON, work_repo_mirror=str(mirror), people=[
        {"key": PERSON, "name": "Alex", "slack": [SLACK_ID]},
        {"key": OTHER, "name": "Blair", "slack": [SLACK_USER]},
    ])
    tracker = Tracker(platform, now=clock)
    timetrack.register_tracker(tracker)
    yield tracker
    timetrack.register_tracker(None)


def _web_chat(session):
    """What the web chat's post journals: the operator credential, naming nobody."""
    seq = _SEQ[session] = _SEQ.get(session, 0) + 1
    return bus.Message(
        id=bus.message_id(session, seq), seq=seq, session=session,
        at=T0.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        source=bus.Source(bus.CHANNEL_PLATFORM, author="operator"),
        kind="message", text="stop my time", meta={META_CREDENTIAL: CREDENTIAL_OPERATOR},
    ).to_dict()


def _bound_request(workspace_id):
    with projects.database() as db:
        row = db.execute("SELECT request_id FROM request_context WHERE project_id=?",
                         (workspace_id,)).fetchone()
    return row[0] if row else None


def test_a_replied_request_no_longer_says_who_asks(transport_host, fleet, blair_asks):
    """Blair's request is answered; Alex then asks in the web chat, so Alex is who asks."""
    client, _transports, _component = transport_host
    project, _request, request_id, headers = delivered(transport_host, fleet)
    session = projects.status(project["id"])["session_id"]
    assert client.get("/api/time/status", headers=headers).json()["acting"]["person"] == OTHER, (
        "while the request is delivered, its sender is who asks")
    replied = client.post(f"/api/assistant/requests/{request_id}/reply", headers=headers,
                          json={"text": "Done"})
    assert replied.status_code == 200, replied.text
    assert _bound_request(project["id"]) == request_id, "the context row outlives the reply"
    record = _journal(session, _web_chat(session))

    acting = client.get("/api/time/status", headers=headers).json()["acting"]
    assert acting == {"person": PERSON, "name": "Alex", "via": "session", "record": record,
                      "request_id": None}
    caller = api.Caller(api.CALLER_ASSISTANT, session_id=session)
    assert api.time_actor(caller, Registry.from_config(blair_asks.config), hub=comms.ensure_hub(),
                          request_id=request_id)["person"] == PERSON, (
        "the request id project_api.recheck() fills in is guarded where it becomes a person")


def test_a_queued_request_ratchet_has_not_taken_says_nothing(transport_host, fleet, blair_asks):
    """Blair's request waits untaken; a status read neither acts for Blair nor binds it."""
    client, _transports, component = transport_host
    project = projects.create("Alpha")
    projects.reconcile(fleet)
    session = projects.status(project["id"])["session_id"]
    request = body("slack", threaded=True)
    component.adopt_launch_targets(session, [_link(request)], by="operator")
    request_id = _post(client, request)["request_id"]
    assert project_requests.get(request_id)["state"] == "queued"
    headers, _session = _assistant(project["id"])
    record = _journal(session, _web_chat(session))

    status = client.get("/api/time/status", headers=headers)
    assert status.status_code == 200, status.text
    assert status.json()["acting"] == {"person": PERSON, "name": "Alex", "via": "session",
                                       "record": record, "request_id": None}
    assert _bound_request(project["id"]) is None, "a GET binds no request"
    caller = api.Caller(api.CALLER_ASSISTANT, session_id=session)
    assert api.time_actor(caller, Registry.from_config(blair_asks.config), hub=comms.ensure_hub(),
                          request_id=request_id)["person"] == PERSON


def test_a_request_delivered_to_an_earlier_generation_says_nothing(transport_host, fleet, blair_asks):
    _client, _transports, _component = transport_host
    project, _request, request_id, _headers = delivered(transport_host, fleet)
    session = projects.status(project["id"])["session_id"]
    with projects.database() as db:
        db.execute("UPDATE requests SET generation=? WHERE id=?", ("s-earlier", request_id))
    record = _journal(session, _web_chat(session))
    caller = api.Caller(api.CALLER_ASSISTANT, session_id=session)
    acting = api.time_actor(caller, Registry.from_config(blair_asks.config), hub=comms.ensure_hub(),
                            request_id=request_id)
    assert (acting["person"], acting["via"], acting["record"]) == (PERSON, "session", record)


# --- the entries join (D18, D20) -----------------------------------------------

def _git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=str(cwd) if cwd else None, check=True, capture_output=True)


def _write(root, entry, text=None):
    path = root / entry_path(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (text if text is not None else entry.to_yaml()).encode("utf-8")
    path.write_bytes(raw)
    return str(entry_path(entry)), raw


def _entry(entry_id, day, *, handler="tracker-x", fields=None, client="acme", source="lmer"):
    start = datetime(2026, 9, day, 10, 0, tzinfo=timezone.utc)
    return Entry(id=entry_id, person=PERSON, project="webshop", handler=handler,
                 fields={"task_id": "abc123"} if fields is None else fields, client=client,
                 title=f"work {entry_id}", start=start, end=start + timedelta(hours=1),
                 trigger=Trigger(start="manual", stop="manual"), source=source)


def _ledger(root, lines):
    path = root / ledger_path(PERSON, "tracker-x")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(line.to_json() + "\n" for line in lines), encoding="utf-8")


def _line(entry_id, raw, status, **extra):
    return LedgerLine(id=entry_id, hash=entry_hash(raw), status=status, at=T0, **extra)


@pytest.fixture
def clone_with(platform, tmp_path):
    """Publish entries to a bare work repo and clone it as the writer would have."""
    def publish(*entries):
        seed = tmp_path / "seed"
        seed.mkdir()
        _git("init", "-q", "-b", "main", cwd=seed)
        _git("config", "user.name", "Fixture Author", cwd=seed)
        _git("config", "user.email", "fixture@example.com", cwd=seed)
        for entry in entries:
            _write(seed, entry)
        _git("add", "-A", cwd=seed)
        _git("commit", "-q", "-m", "entries", cwd=seed)
        bare = tmp_path / "remote.git"
        _git("init", "-q", "--bare", "-b", "main", str(bare))
        _git("push", "-q", str(bare), "main", cwd=seed)
        _git("clone", "-q", str(bare), str(timesheet_writer.clone_path()))
    return publish


@pytest.fixture
def joined(client, tracker, clock, mirror, webshop, docs, clone_with):
    """One of everything: running, outbox, kept, and written in every sync state."""
    clock.now = T0 - timedelta(hours=3)
    client.post("/api/time/start", json={"project": docs})
    clock.advance(seconds=10)
    client.post("/api/time/stop", json={})
    clock.now = T0 - timedelta(hours=2)
    client.post("/api/time/start", json={"project": webshop, "fields": {"task_id": "abc123"}})
    clock.now = T0 - timedelta(hours=1)
    client.post("/api/time/stop", json={})
    clock.now = T0
    client.post("/api/time/start", json={"project": docs, "title": "now"})

    synced = _entry("e-synced", 1)
    held = _entry("e-held", 2)
    fresh = _entry("e-fresh", 3)
    edited = _entry("e-edited", 4)
    hand = _entry("e-hand", 5, fields={}, source="hand")
    _, synced_raw = _write(mirror, synced)
    _, held_raw = _write(mirror, held)
    _write(mirror, fresh)
    _write(mirror, edited, edited.to_yaml().replace("work e-edited", "work e-edited, renamed"))
    _write(mirror, hand)
    broken = mirror / "timesheet.d" / PERSON / "2026" / "09" / "20260906T100000Z--webshop--e-broken.yaml"
    broken.write_text("id: [unclosed\n", encoding="utf-8")
    _ledger(mirror, [
        _line("e-synced", synced_raw, "synced", ref="TX-1", link=LINK + "TX-1"),
        _line("e-held", held_raw, "held", reason=HELD_REASON),
        _line("e-edited", edited.to_yaml().encode("utf-8"), "synced", link=LINK + "TX-4"),
    ])
    clone_with(synced, _entry("e-pushed", 7))
    return client


def _rows(http, **params):
    response = http.get("/api/time/entries", params=params)
    assert response.status_code == 200, response.text
    return response.json()["rows"]


def _by_id(rows):
    return {row["id"]: row for row in rows if row["id"]}


def test_entries_join_every_stage(joined):
    rows = _rows(joined)
    stages = [row["stage"] for row in rows]
    assert stages[:3] == ["running", "in outbox", "kept locally"], "newest first"
    running = rows[0]
    assert (running["project_name"], running["title"], running["mode"], running["end"]) == (
        "Docs Site", "now", "manual", None)
    outbox = rows[1]
    assert (outbox["project"], outbox["fields"], outbox["outbox"]["state"]) == (
        "webshop", {"task_id": "abc123"}, "queued")
    assert outbox["field_links"] == {"task_id": "https://tracker-x.example.com/t/abc123"}
    assert outbox["path"].startswith("timesheet.d/alex/2026/10/")
    assert rows[2]["kept"] == "shorter than the 60-second minimum"
    assert {row["stage"] for row in rows[3:]} == {"written"}
    assert len(rows) == 3 + 7, "six mirror files and one only the clone has"


def test_every_stage_carries_the_runs_its_entry_covered(client, tracker, clock, mirror, docs):
    run = {"run": f"forge.test/team/app/{UID_A}", "uid": UID_A}
    ref = SessionRef(**run)
    for at, stop in ((T0 - timedelta(hours=3), timedelta(seconds=10)),
                     (T0 - timedelta(hours=2), timedelta(hours=1))):
        tracker.start(PERSON, docs, at=at, trigger="manual", sessions=(ref,))
        tracker.stop(PERSON, None, at=at + stop, trigger="manual")
    tracker.start(PERSON, docs, at=T0, trigger="manual", sessions=(ref,))
    written = replace(_entry("e-linked", 1), sessions=[SessionRef(run="forge.test/team/app/develop-issue-7")])
    _write(mirror, written)
    _write(mirror, _entry("e-plain", 2))

    rows = {row["stage"] if row["stage"] != "written" else row["id"]: row for row in _rows(client)}
    for stage in ("running", "in outbox", "kept locally"):
        assert rows[stage]["sessions"] == [run], stage
    assert rows["e-linked"]["sessions"] == [{"run": "forge.test/team/app/develop-issue-7"}]
    assert rows["e-plain"]["sessions"] == []


def test_entries_show_each_ledger_status(joined):
    rows = _by_id(_rows(joined))
    synced = rows["e-synced"]["sync"]
    assert (synced["status"], synced["link"], synced["ref"]) == ("synced", LINK + "TX-1", "TX-1")
    held = rows["e-held"]["sync"]
    assert (held["status"], held["reason"]) == ("held", HELD_REASON)
    assert (rows["e-fresh"]["sync"]["status"], rows["e-fresh"]["sync"]["detail"]) == (
        "pending", "no ledger line yet")
    edited = rows["e-edited"]["sync"]
    assert (edited["status"], edited["detail"], edited["link"]) == (
        "pending", "changed since it was synced", LINK + "TX-4")
    assert rows["e-synced"]["origin"] == "mirror", "the mirror's copy wins over the clone's"
    assert sum(1 for row in _rows(joined) if row["id"] == "e-synced") == 1


def test_an_entry_pushed_before_the_mirror_has_it_is_listed_from_the_clone(joined):
    pushed = _by_id(_rows(joined))["e-pushed"]
    assert (pushed["origin"], pushed["stage"], pushed["sync"]["status"]) == ("clone", "written", "pending")
    assert pushed["path"] == "timesheet.d/alex/2026/09/20260907T100000Z--webshop--e-pushed.yaml"


def test_broken_hand_written_files_carry_their_problems(joined):
    rows = _by_id(_rows(joined))
    hand = rows["e-hand"]
    assert "task_id: required (Tracker X task)" in hand["problems"]
    assert hand["source"] == "hand"
    assert "fields: {}" in hand["raw"]
    broken = rows["e-broken"]
    assert broken["problems"] and broken["problems"][0].startswith("not valid YAML")
    assert (broken["start"], broken["project"], broken["raw"]) == (
        "2026-09-06T10:00:00Z", "webshop", "id: [unclosed\n")
    assert rows["e-synced"]["problems"] == []


@pytest.mark.parametrize("params, expected", [
    ({"status": "held"}, {"e-held"}),
    ({"status": "synced"}, {"e-synced"}),
    ({"status": "pending"}, {"e-fresh", "e-edited", "e-hand", "e-pushed"}),
    ({"status": "in_outbox"}, {"outbox"}),
    ({"status": "kept locally"}, {"kept"}),
    ({"status": "running"}, {"running"}),
    ({"from": "2026-09-02", "to": "2026-09-04"}, {"e-held", "e-fresh", "e-edited"}),
    ({"to": "2026-09-01"}, {"e-synced"}),
    ({"handler": "timesheet"}, {"running", "kept"}),
    ({"client": "ACME", "from": "2026-09-05"}, {"e-hand", "e-pushed", "outbox"}),
])
def test_entries_filters(joined, params, expected):
    live = {"running": "running", "in outbox": "outbox", "kept locally": "kept"}
    named = {live.get(row["stage"], row["id"]) for row in _rows(joined, **params)}
    assert named == expected


def test_entries_filter_by_project_key_or_workspace_id(joined, webshop, docs):
    by_key = _rows(joined, project="webshop")
    by_id = _rows(joined, project=webshop)
    assert [row["id"] for row in by_key] == [row["id"] for row in by_id]
    assert "running" not in {row["stage"] for row in by_key}
    assert {row["stage"] for row in _rows(joined, project=docs)} == {"running", "kept locally"}


def test_an_unknown_status_filter_is_a_400(joined):
    detail = _detail(joined.get("/api/time/entries", params={"status": "lost"}), 400)
    assert detail["code"] == "time_invalid_filter"
    assert "running, in outbox" in detail["message"]


def test_held_entries_warn_in_the_header(joined):
    warnings = joined.get("/api/time/status").json()["warnings"]
    held = [warning for warning in warnings if warning["kind"] == "held"]
    assert held == [{"kind": "held", "handler": "tracker-x", "handler_label": "Tracker X",
                     "reason": HELD_REASON, "count": 1,
                     "message": f"1 Tracker X entry held: {HELD_REASON}"}]


def test_entries_are_the_acting_persons_only(joined, mirror):
    other = Entry(id="e-blair", person=OTHER, project="webshop", start=T0 - timedelta(days=3),
                  duration=600, trigger=Trigger(start="hand", stop="hand"))
    _write(mirror, other)
    assert "e-blair" not in _by_id(_rows(joined))
    assert all(row["person"] == PERSON for row in _rows(joined))


def test_an_assistant_reads_only_its_own_projects_entries(joined, ratchet, webshop):
    headers, session = ratchet
    _journal(session, _record(session))
    rows = joined.get("/api/time/entries", headers=headers).json()["rows"]
    assert rows and {row["workspace_id"] for row in rows} == {webshop}
    assert json.dumps(rows).count("Docs Site") == 0


# --- prompts, base and the run's facts (issue #665) -------------------------

ISSUE_13 = "https://git.example.com/acme/widgets/-/issues/13"


def _prompted(tracker, workspace_id, at=T0):
    run = RunFacts(ref=SessionRef(run="git.example.com/acme/widgets/fix-13"),
                   key="git.example.com/acme/widgets/fix-13", title="Fix 13", issues=(ISSUE_13,))
    assert tracker.note_activity(PERSON, workspace_id, at=at, runs=(run,)).outcome == "prompted"
    [prompt] = tracker.status(PERSON)["prompts"]
    return prompt


def test_answering_a_prompt_starts_its_run(client, tracker, webshop):
    prompt = _prompted(tracker, webshop)
    assert prompt["label"] == "#13" and prompt["work_key"] == ISSUE_13
    started = client.post("/api/time/start", json={"prompt": prompt["id"], "fields": {"task_id": "t13"}})
    assert started.status_code == 200, started.text
    timer = started.json()["timer"]
    assert (timer["mode"], timer["start_trigger"], timer["issues"]) == ("auto", "manual", [ISSUE_13])
    assert timer["identity"] == {"run": "git.example.com/acme/widgets/fix-13"}
    assert client.get("/api/time/status").json()["prompts"] == []


def test_a_bad_answer_is_a_422_and_keeps_the_prompt(client, tracker, webshop):
    prompt = _prompted(tracker, webshop)
    detail = _detail(client.post("/api/time/start", json={
        "prompt": prompt["id"], "fields": {"task_id": "NOT OK"}}), 422)
    assert detail["missing"] == ["task_id"]
    assert [item["id"] for item in client.get("/api/time/status").json()["prompts"]] == [prompt["id"]]


def test_a_prompt_for_another_project_or_nobody_is_refused(client, tracker, webshop, docs):
    prompt = _prompted(tracker, webshop)
    detail = _detail(client.post("/api/time/start", json={
        "prompt": prompt["id"], "project": docs, "fields": {"task_id": "t13"}}), 400)
    assert detail["code"] == "time_prompt_project"
    assert _detail(client.post("/api/time/start", json={"prompt": 999}), 404)["code"] == "time_prompt_unknown"
    assert _detail(client.post("/api/time/start", json={"prompt": "7"}), 400)["code"] == "time_invalid"


def test_an_assistant_answers_only_its_own_projects_prompts(client, tracker, ratchet, docs):
    headers, session = ratchet
    _journal(session, _record(session))
    projects.update(docs, {"settings": {"time_handler": "tracker-x"}})
    prompt = _prompted(tracker, docs)
    response = client.post("/api/time/start", headers=headers,
                           json={"prompt": prompt["id"], "fields": {"task_id": "t13"}})
    assert _detail(response, 403)["code"] == "time_other_project"


def test_an_assistant_remembering_its_answer_under_a_timer_elsewhere_learns_no_other_key(
        client, tracker, clock, ratchet, webshop, docs):
    """P14: a remembered answer is a success, and its reason must not name Docs Site's key."""
    headers, session = ratchet
    _journal(session, _record(session))
    prompt = _prompted(tracker, webshop)
    clock.advance(minutes=1)
    assert client.post("/api/time/start", json={"project": docs}).json()["timer"]["mode"] == "manual"
    answered = client.post("/api/time/start", headers=headers,
                           json={"prompt": prompt["id"], "fields": {"task_id": "t13"}})
    assert answered.status_code == 200, answered.text
    reply = answered.json()
    assert (reply["outcome"], reply["timer"]) == ("remembered", None)
    assert reply["reasons"] == [
        "a timer is running on another project; the value is kept for the next time this starts"]
    assert projects.project_key(docs) not in answered.text


def test_patch_with_base_ignores_an_unedited_title_and_splits_on_a_change(client, tracker, clock, docs):
    client.post("/api/time/start", json={"project": docs, "title": "First"})
    clock.advance(minutes=5)
    unedited = client.patch("/api/time/current", json={"title": "First", "base": {"title": "First"}})
    assert unedited.json()["outcome"] == "updated"
    split = client.patch("/api/time/current", json={"title": "Second", "base": {"title": "First"}})
    assert split.status_code == 200, split.text
    assert split.json()["outcome"] == "split"
    assert (split.json()["entry"]["title"], split.json()["entry"]["trigger"]["stop"]) == ("First", "edit")
    assert _detail(client.patch("/api/time/current", json={"title": "x", "base": "y"}), 400)["code"] == "time_invalid"


def test_status_lists_prompts_with_ids_and_handlers_name_per_issue(client, tracker, mirror, webshop):
    (mirror / "timesheet.d" / ".handlers" / "tracker-x" / "issues.yaml").parent.mkdir()
    (mirror / "timesheet.d" / ".handlers" / "tracker-x" / "issues.yaml").write_text("- broken\n")
    prompt = _prompted(tracker, webshop)
    status = client.get("/api/time/status", params={"project": webshop}).json()
    [listed] = status["prompts"]
    assert (listed["id"], listed["label"], listed["message"]) == (
        prompt["id"], "#13", "Webshop #13 needs a Tracker X task to track time")
    [field] = status["projects"][webshop]["handler"]["fields"]
    assert field["per_issue"] is False
    assert "timesheet.d/.handlers/tracker-x/issues.yaml: not a mapping, but a list" in status["handler_problems"]


def test_the_index_names_prompt_and_base(client):
    index = client.get("/api").text
    assert "prompt?" in index and "base?" in index
