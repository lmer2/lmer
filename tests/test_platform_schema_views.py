"""The read contract behind #374's "sessions per target" view (spec §13.F, task 79).

``GET /api/runs/sessions`` answers the records of ONE schema-form
``_runs/<slug>/`` group in START order, each addressable by its uid, with the
platform's own facts about that row beside the mirror's. Pinned here:

- start order is the logged ``started_at``, not the directory name: the fixture
  group's uid order is the REVERSE of its start order, which is the shape every
  schema-2 group has half the time (a uid is random);
- every row carries its own ``live`` / ``tracked`` and — astra PR6 — its OWN
  project context (``workspace_id``, ``home_revision``, ``confirm_project``),
  read exactly as a fleet row's is, so a resume of one child never posts the
  parent row's home revision;
- locating the group by uid and by slug is the same answer;
- a legacy tree and a legacy row answer ``form: "legacy"`` with no sessions;
- a missing part is ``run_key``'s 400, and the route is behind the secret;
- the route never pulls, and is listed in the plain-text ``GET /api`` index;
- the reply, minus the fields named in :data:`VOLATILE`, is the checked-in
  golden ``web/tests/fixtures/schema-form/run-sessions.json`` the web tests
  render. Regenerate it with ``REGEN_SCHEMA_GOLDENS=1``.

:func:`build_sessions_mirror` is the fixture builder ``tests/test_platform_schema_report.py``
imports; it uses ``tests/schema2_fixtures.py`` read-only.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from lmer_platform import api, projects, registry, runs, schema_report, store, workrepo
from lmer_platform import config as cfg
from tests.conftest import strip_lmer_env
from tests.schema2_fixtures import RecordSpec, build_schema2_plexus, stamp

SECRET = "test-secret-value"
REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = REPO_ROOT / "web" / "tests" / "fixtures"
SESSIONS_GOLDEN = GOLDEN_DIR / "schema-form" / "run-sessions.json"
#: Set (to anything) to rewrite the goldens instead of comparing against them.
REGEN_ENV = "REGEN_SCHEMA_GOLDENS"

#: The identity the schema-2 fixture registers ``widgets/`` under.
HOST = "git.example.test"
PROJECT = "acme/widgets"
SLUG = "chat"
#: Started FIRST but sorts LAST by name, so name order is the wrong answer.
UID_FIRST = "f1" * 16
#: Started second, sorts first by name, and is frozen as ``<uid>--followup``.
UID_SECOND = "0a" * 16
SECOND_NAME = "followup"
LIVE_SESSION = "s-20260902-090000-0a0a"
#: The pinned clock: a day after the second record's last update, so an open
#: record reads ``active`` whatever day the suite runs on.
PINNED_NOW = datetime(2026, 9, 3, 0, 0, tzinfo=timezone.utc)

#: Reply fields whose VALUE is minted at run time (a project's uuid). The
#: golden keeps the field and replaces a non-null value with this marker.
VOLATILE = ("workspace_id",)
VOLATILE_MARKER = "<volatile>"

FIRST = RecordSpec(
    uid=UID_FIRST, slug=SLUG, started_at=stamp(1, 10), updated=stamp(1, 11),
    status="complete", signed_out=True, goal="first launch",
)
SECOND = RecordSpec(
    uid=UID_SECOND, slug=SLUG, started_at=stamp(2, 9), updated=stamp(2, 12),
    status="in-progress", signed_out=False, name=SECOND_NAME,
    dir_suffix=SECOND_NAME, goal="second launch",
)


def build_sessions_mirror(root) -> Path:
    """The mirror both #374 suites read: one schema-2 ``widgets/_runs/chat/``
    group of two records whose uid order is the reverse of their start order,
    the second frozen as ``<uid>--followup``. Returns the mirror root."""
    tree = build_schema2_plexus(root, {"widgets": [FIRST, SECOND]})
    return tree.root


def build_legacy_mirror(root) -> Path:
    """A tree with no manifest and one legacy ``{host}/{project}/runs/chat/`` run."""
    root = Path(root)
    run_dir = root / HOST / PROJECT / "runs" / SLUG
    run_dir.mkdir(parents=True)
    (run_dir / "state.yaml").write_text(
        yaml.safe_dump({"slug": SLUG, "status": "in-progress",
                        "created": stamp(1), "updated": stamp(1)}, sort_keys=False),
        encoding="utf-8",
    )
    return root


def pinned(reply: dict) -> dict:
    """*reply* with every :data:`VOLATILE` value replaced, for a golden."""
    def scrub(value):
        if isinstance(value, dict):
            return {
                key: (VOLATILE_MARKER if key in VOLATILE and item is not None else scrub(item))
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value
    return scrub(reply)


def assert_golden(path: Path, reply: dict) -> None:
    """Compare *reply* (pinned) to the golden at *path*, or rewrite it."""
    document = pinned(reply)
    if os.environ.get(REGEN_ENV):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert document == json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    regen = os.environ.get(REGEN_ENV)
    strip_lmer_env(monkeypatch)
    for name in ("LMER_WORK_REPO", cfg.ENV_WORK_REPO_MIRROR, cfg.ENV_WORK_REPO_FORGE):
        monkeypatch.delenv(name, raising=False)
    if regen:
        monkeypatch.setenv(REGEN_ENV, regen)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


@pytest.fixture
def pinned_clock(monkeypatch):
    monkeypatch.setattr(workrepo, "_now", lambda: PINNED_NOW.timestamp())


@pytest.fixture
def mirror(tmp_path):
    return build_sessions_mirror(tmp_path / "mirror")


def _client(mirror_root) -> TestClient:
    config = cfg.load({"work_repo_mirror": str(mirror_root)})

    def fake_state(config, *, force_pull=False):
        return {"runs": [], "attention": []}

    return TestClient(api.create_app(config, SECRET, state_builder=fake_state))


def _bearer():
    return {"Authorization": f"Bearer {SECRET}"}


def _sessions(client, **params):
    query = {"host": HOST, "project": PROJECT, **params}
    return client.get("/api/runs/sessions", params=query, headers=_bearer())


def _plant_platform_facts():
    """The second record has a live session; the first is tracked by uid."""
    runs.track(HOST, PROJECT, SLUG, uid=UID_FIRST, source="adopted")
    registry.register(
        LIVE_SESSION, pid=os.getpid(),
        run={"host": HOST, "project": PROJECT, "slug": SLUG, "uid": UID_SECOND},
    )


def _key(uid):
    return runs.run_key(HOST, PROJECT, uid)


@pytest.fixture
def homed(platform_root):
    """Projects on: the first record homed in General at revision 1, the second
    moved to Alpha (revision 2, reconfirmation required). Planted before the
    live session, because a move refuses a run with a live worker."""
    runs.track(HOST, PROJECT, SLUG, uid=UID_FIRST, source="adopted")
    projects.migrate()
    alpha = projects.create("Alpha")
    projects.bind_run(_key(UID_SECOND), projects.GENERAL)
    projects.move(_key(UID_SECOND), alpha["id"], expected_project=projects.GENERAL)
    registry.register(
        LIVE_SESSION, pid=os.getpid(),
        run={"host": HOST, "project": PROJECT, "slug": SLUG, "uid": UID_SECOND},
    )
    return alpha


# --- the rows -----------------------------------------------------------------

def test_two_records_answer_in_start_order_with_their_own_facts(
    platform_root, mirror, pinned_clock,
):
    _plant_platform_facts()
    reply = _sessions(_client(mirror), uid=UID_FIRST)

    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["form"] == "schema"
    assert body["group"] == "widgets/_runs/chat"
    rows = body["sessions"]
    assert [row["uid"] for row in rows] == [UID_FIRST, UID_SECOND]
    assert sorted(row["uid"] for row in rows) == [UID_SECOND, UID_FIRST]  # name order differs
    assert rows[0]["started_at"] < rows[1]["started_at"]
    assert [row["name_suffix"] for row in rows] == [None, SECOND_NAME]
    assert [row["name"] for row in rows] == [None, SECOND_NAME]
    assert [row["dir_name"] for row in rows] == [UID_FIRST, f"{UID_SECOND}--{SECOND_NAME}"]
    assert [row["status"] for row in rows] == ["complete", "in-progress"]
    assert [row["record_state"] for row in rows] == ["closed", "active"]
    assert [row["live"] for row in rows] == [False, True]
    assert [row["live_session"] for row in rows] == [None, LIVE_SESSION]
    assert [row["tracked"] for row in rows] == [True, False]
    assert [row["current"] for row in rows] == [True, False]
    assert [row["rel_path"] for row in rows] == [
        f"widgets/_runs/chat/{UID_FIRST}",
        f"widgets/_runs/chat/{UID_SECOND}--{SECOND_NAME}",
    ]


def test_each_row_carries_its_own_project_context(mirror, pinned_clock, homed):
    """astra PR6: the child's own workspace and home revision, never the
    parent row's — the second record moved, so posting the first's revision
    for it would be refused by the request binding."""
    reply = _sessions(_client(mirror), uid=UID_FIRST)

    assert reply.status_code == 200, reply.text
    rows = reply.json()["sessions"]
    assert [row["workspace_id"] for row in rows] == [projects.GENERAL, homed["id"]]
    assert [row["home_revision"] for row in rows] == [1, 2]
    assert [row["confirm_project"] for row in rows] == [False, True]
    assert [row["workspace_name"] for row in rows] == ["General", "Alpha"]
    # The same source the fleet row's resume reads (``project_api.annotate``).
    home = projects.run_home(_key(UID_SECOND))
    assert (home["project_id"], home["revision"], bool(home["reconfirm"])) == (
        homed["id"], 2, True,
    )


def test_without_projects_the_context_fields_are_empty(platform_root, mirror, pinned_clock):
    rows = _sessions(_client(mirror), uid=UID_FIRST).json()["sessions"]
    for row in rows:
        assert (row["workspace_id"], row["home_revision"], row["confirm_project"]) == (
            None, None, False,
        )


def test_locating_by_uid_and_by_slug_is_the_same_answer(platform_root, mirror, pinned_clock):
    _plant_platform_facts()
    client = _client(mirror)

    def without_current(reply):
        body = reply.json()
        return body["form"], body["group"], [
            {key: value for key, value in row.items() if key != "current"}
            for row in body["sessions"]
        ]

    by_first = without_current(_sessions(client, uid=UID_FIRST))
    by_second = without_current(_sessions(client, uid=UID_SECOND))
    by_slug = without_current(_sessions(client, slug=SLUG))
    assert by_first == by_second == by_slug
    assert len(by_slug[2]) == 2


def test_current_marks_the_addressed_launch_and_a_slug_marks_none(
    platform_root, mirror, pinned_clock,
):
    client = _client(mirror)
    by_second = _sessions(client, uid=UID_SECOND).json()["sessions"]
    assert [row["current"] for row in by_second] == [False, True]
    by_slug = _sessions(client, slug=SLUG).json()["sessions"]
    assert [row["current"] for row in by_slug] == [False, False]


def test_record_state_reads_the_pinned_clock(platform_root, mirror, monkeypatch):
    """The open record goes stale past the 14-day window — so the stamps and
    the clock, not the day the suite runs, decide what the row says."""
    later = datetime(2026, 9, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(workrepo, "_now", lambda: later.timestamp())
    rows = _sessions(_client(mirror), slug=SLUG).json()["sessions"]
    assert [row["record_state"] for row in rows] == ["closed", "stale"]


# --- workrepo.group_sessions directly -------------------------------------------

def test_group_sessions_sorts_by_logged_start_not_by_name(platform_root, mirror):
    config = cfg.load({"work_repo_mirror": str(mirror)})
    found = workrepo.group_sessions(config, HOST, PROJECT, SLUG, now=PINNED_NOW.isoformat())
    assert [row["uid"] for row in found["sessions"]] == [UID_FIRST, UID_SECOND]
    # The uid lookup finds the same group from either record.
    by_uid = workrepo.group_sessions(config, HOST, PROJECT, uid=UID_SECOND,
                                     now=PINNED_NOW.isoformat())
    assert by_uid == found


def test_a_seeded_record_reads_seeded(platform_root, tmp_path):
    """``work seed`` wrote it and nothing started in it (D13): not ``open``."""
    seed_uid = "5e" * 16
    seed = RecordSpec(uid=seed_uid, slug=SLUG, started_at=stamp(3), updated=stamp(3),
                      signed_out=None)
    tree = build_schema2_plexus(tmp_path / "seeded", {"widgets": [FIRST, seed]})
    state_file = tree.record_dir("widgets", seed_uid) / "state.yaml"
    state = yaml.safe_load(state_file.read_text(encoding="utf-8"))
    state["session"]["seeded_by"] = UID_FIRST
    state_file.write_text(yaml.safe_dump(state, sort_keys=False), encoding="utf-8")

    config = cfg.load({"work_repo_mirror": str(tree.root)})
    found = workrepo.group_sessions(config, HOST, PROJECT, SLUG, now=PINNED_NOW.isoformat())
    assert [row["record_state"] for row in found["sessions"]] == ["closed", "seeded"]


def test_an_unknown_uid_is_an_empty_schema_group(platform_root, mirror, pinned_clock):
    body = _sessions(_client(mirror), uid="9" * 32).json()
    assert (body["form"], body["group"], body["sessions"]) == ("schema", None, [])


# --- legacy ---------------------------------------------------------------------

def test_a_legacy_tree_answers_legacy_with_no_sessions(platform_root, tmp_path):
    root = build_legacy_mirror(tmp_path / "legacy")
    reply = _sessions(_client(root), slug=SLUG)
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert (body["form"], body["group"], body["sessions"]) == ("legacy", None, [])


def test_a_legacy_row_in_a_schema_tree_answers_legacy(platform_root, mirror):
    """A ``{host}/{project}/runs/<slug>/`` record beside the schema-form tree is
    one shared record per target: there are no launches to list."""
    run_dir = mirror / HOST / PROJECT / "runs" / "old-review"
    run_dir.mkdir(parents=True)
    (run_dir / "state.yaml").write_text("slug: old-review\nstatus: complete\n", encoding="utf-8")
    body = _sessions(_client(mirror), slug="old-review").json()
    assert (body["form"], body["sessions"]) == ("legacy", [])


# --- refusals, guard, index, no pull ----------------------------------------------

@pytest.mark.parametrize("missing", ["host", "project"])
def test_a_missing_part_is_run_keys_400(platform_root, mirror, missing):
    params = {"host": HOST, "project": PROJECT, "slug": SLUG, missing: ""}
    reply = _client(mirror).get("/api/runs/sessions", params=params, headers=_bearer())
    assert reply.status_code == 400
    assert missing in reply.json()["detail"]


def test_neither_slug_nor_uid_is_a_400(platform_root, mirror):
    reply = _sessions(_client(mirror))
    assert reply.status_code == 400


def test_a_malformed_uid_is_a_400(platform_root, mirror):
    reply = _sessions(_client(mirror), uid="not-a-uid")
    assert reply.status_code == 400
    assert "uid" in reply.json()["detail"]


def test_the_route_needs_the_shared_secret(platform_root, mirror):
    reply = _client(mirror).get(
        "/api/runs/sessions", params={"host": HOST, "project": PROJECT, "slug": SLUG},
    )
    assert reply.status_code == 401


def test_the_route_is_listed_in_the_api_index(platform_root, mirror):
    index = _client(mirror).get("/api", headers=_bearer()).text
    assert "GET  /api/runs/sessions" in index


def test_reading_a_group_does_not_pull_the_mirror(platform_root, mirror, monkeypatch):
    def refuse(*args, **kwargs):  # pragma: no cover - failing is the assertion
        raise AssertionError("GET /api/runs/sessions pulled the mirror")

    monkeypatch.setattr(api, "pull", refuse)
    monkeypatch.setattr(workrepo, "pull", refuse)
    assert _sessions(_client(mirror), slug=SLUG).status_code == 200


# --- the golden the web tests render ---------------------------------------------

def test_the_reply_is_the_checked_in_golden(mirror, pinned_clock, homed):
    reply = _sessions(_client(mirror), uid=UID_FIRST)
    assert reply.status_code == 200, reply.text
    assert_golden(SESSIONS_GOLDEN, reply.json())


# --- with projects on (#542) -------------------------------------------------------
#
# The request binding refuses an /api/ path it can attribute to no project. The
# schema panel is fleet-wide — the work repo is shared by every workspace — so it
# is on the binding's unbound list; the sessions view is run-addressed, so it binds
# exactly as the run's other GET reads do: through the run key in its query.

def test_the_fleet_schema_panel_answers_with_projects_on(mirror, homed):
    schema_report.clear_cache()
    try:
        assert projects.enabled()
        reply = _client(mirror).get("/api/workrepo/schema", headers=_bearer())
        assert reply.status_code == 200, reply.text
        assert reply.json()["form"] == "schema"
    finally:
        schema_report.clear_cache()


RUN_READS = ("/api/runs/meta", "/api/runs/files", "/api/runs/sessions")


def _run_read(client, path, **params):
    query = {"host": HOST, "project": PROJECT, **params}
    return client.get(path, params=query, headers=_bearer())


@pytest.mark.parametrize("path", RUN_READS)
def test_the_sessions_read_binds_like_the_runs_other_reads(mirror, pinned_clock, homed, path):
    """Same query, same verdict, for each of the run-addressed GETs: the
    addressed run's home binds it with no workspace named; its own workspace
    and revision (what api.js's runFields puts on the query) bind it too; and
    another project's workspace is refused as a project change."""
    client = _client(mirror)
    assert _run_read(client, path, uid=UID_FIRST).status_code == 200
    own = _run_read(client, path, uid=UID_FIRST,
                    workspace_id=projects.GENERAL, home_revision=1)
    assert own.status_code == 200, own.text
    other = _run_read(client, path, uid=UID_FIRST, workspace_id=homed["id"])
    assert other.status_code == 409, other.text


# --- review !392: timestamp-id rows and project scope -------------------------

TIMESTAMP_ID = "20261007-120000-1234-abcd"


def _plant_timestamp_record(mirror_root: Path) -> Path:
    """A timestamp-id launch beside the two uid records of the group."""
    group = next(Path(mirror_root).rglob(f"_runs/{SLUG}/{UID_FIRST}")).parent
    record = group / TIMESTAMP_ID
    record.mkdir()
    (record / "state.yaml").write_text(
        yaml.safe_dump({"slug": SLUG, "status": "in-progress",
                        "created": stamp(0), "updated": stamp(0)}, sort_keys=False),
        encoding="utf-8",
    )
    return record


def test_a_timestamp_id_row_takes_the_slug_runs_project_context(mirror, pinned_clock, homed):
    """Its key is the slug run's, so its home is the slug run's — never an
    'Unassigned' row because a timestamp id was read as a malformed uid."""
    _plant_timestamp_record(mirror)
    projects.bind_run(_key(SLUG), projects.GENERAL)

    rows = _sessions(_client(mirror), uid=UID_FIRST).json()["sessions"]

    (row,) = [row for row in rows if row["uid"] == TIMESTAMP_ID]
    assert row["workspace_id"] == projects.GENERAL
    assert row["live"] is None and row["tracked"] is None


def test_a_caller_scoped_to_one_project_sees_no_sibling_homed_elsewhere(
    mirror, pinned_clock, homed
):
    """The second record lives in Alpha; a caller scoped to General keeps its
    mirror facts but none of the platform's facts about it."""
    config = cfg.load({"work_repo_mirror": str(mirror)})
    run = {"host": HOST, "project": PROJECT, "slug": SLUG, "uid": UID_FIRST}
    parts = (HOST, PROJECT, UID_FIRST)

    scoped = api._run_sessions_reply(config, run, parts, scope=projects.GENERAL)
    full = api._run_sessions_reply(config, run, parts)

    by_uid = {row["uid"]: row for row in scoped["sessions"]}
    sibling, own = by_uid[UID_SECOND], by_uid[UID_FIRST]
    for field in api._RUN_SESSIONS_PLATFORM_FIELDS:
        assert sibling[field] is None, field
    assert sibling["status"] and sibling["started_at"], "mirror facts stay"
    assert own["workspace_id"] == projects.GENERAL and own["tracked"] is True
    full_sibling = {row["uid"]: row for row in full["sessions"]}[UID_SECOND]
    assert full_sibling["workspace_id"] == homed["id"], "the operator sees it all"
    assert full_sibling["live_session"] is not None


def test_the_work_repo_schema_report_is_operator_only(mirror, homed):
    """The document spans every project; an assistant is refused, not scoped."""
    credential = cfg.mint_assistant_credential()

    reply = _client(mirror).get(
        "/api/workrepo/schema", headers={"Authorization": f"Bearer {credential}"}
    )

    assert reply.status_code == 403, reply.text
