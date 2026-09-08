"""Tests for editing service slots through the platform API (issue #256).

The properties that matter, in the order the issue names them:

- a slot written through the API is visible to the *running* daemon — the fleet
  payload and the spawn gate both re-read ``config.json``, so nothing has to be
  restarted;
- the write is a read-modify-write that leaves the rest of ``config.json``
  alone, including keys this build does not know, and including slots it is not
  editing;
- an edit made in a text editor between the form's read and its submit is
  *detected* rather than overwritten (``If-Match``);
- the verdict the form shows before a save is the same sentence the row shows
  after one, because it is the same code — ``parse_slots`` and
  ``_resolve_definition``, over a candidate list;
- and nothing is applied silently under a running session.
"""

import json
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from lmer_platform import api
from lmer_platform import config as cfg
from lmer_platform import registry, slots, store
from tests.conftest import strip_lmer_env

SECRET = "test-secret-value"

#: Far above /proc/sys/kernel/pid_max on normal systems, so it reads dead.
DEAD_PID = 2**22


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture(autouse=True)
def _clean_probe_cache():
    slots.clear_probe_cache()
    yield
    slots.clear_probe_cache()


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


@pytest.fixture
def client(platform_root, slot_host):
    """A real app over a host that knows ``tests.conftest.SLOT_PRESETS``."""
    return TestClient(api.create_app(cfg.load(), SECRET))


def auth():
    return {"Authorization": f"Bearer {SECRET}"}


def write_config(payload):
    store.write_json(cfg.config_path(), payload)


def read_config():
    """The file as it is on disk — ``{}`` when nothing has written one yet."""
    path = cfg.config_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def stored_slots():
    return read_config().get("slots", [])


def create(client, **body):
    return client.post("/api/slots", json=body, headers=auth())


def row_of(client, name):
    """One slot row off the fleet payload, as the view sees it."""
    payload = client.get("/api/state", headers=auth()).json()
    for row in payload["slots"]:
        if row["name"] == name:
            return row
    return None


def etag_of(client, name):
    return row_of(client, name)["etag"]


def register(session_id, *, slot, pid=None):
    return registry.register(
        session_id,
        pid=pid if pid is not None else 1,
        slot=slot,
        run={"host": "h", "project": "p", "slug": session_id},
    )


# --- the routes are gated like everything else -------------------------------

@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.post("/api/slots", json={"name": "x", "preset": "webapp_dev"}),
        lambda c: c.put("/api/slots/x", json={"preset": "webapp_dev"}),
        lambda c: c.delete("/api/slots/x"),
    ],
)
def test_the_slot_routes_require_credentials(client, call):
    assert call(client).status_code == 401


# --- G1: a slot written through the API is live without a restart ------------

def test_a_created_slot_appears_on_the_running_daemons_payload(client):
    """The issue's first obstacle: config.json was resolved once at boot."""
    assert row_of(client, "webapp") is None

    response = create(client, name="webapp", preset="webapp_dev")

    assert response.status_code == 201, response.text
    assert response.json()["name"] == "webapp"
    # Same app object, no restart: the payload is built from a fresh read.
    assert row_of(client, "webapp")["state"] == slots.SLOT_FREE


def test_a_slot_written_by_hand_appears_without_a_restart(client):
    """The same property from the other direction — an editor, not the UI."""
    write_config({"slots": [{"name": "webapp", "preset": "webapp_dev"}]})

    assert row_of(client, "webapp")["preset"] == "webapp_dev"


def test_a_removed_slot_leaves_the_payload_without_a_restart(client):
    create(client, name="webapp", preset="webapp_dev")

    response = client.delete(
        "/api/slots/webapp",
        headers={**auth(), "If-Match": etag_of(client, "webapp")},
    )

    assert response.status_code == 200, response.text
    assert [entry["name"] for entry in response.json()["removed"]] == ["webapp"]
    assert row_of(client, "webapp") is None


def test_the_spawn_gate_sees_a_slot_created_after_boot(client, platform_root):
    """The gate must agree with the row, or the UI offers a spawn it refuses."""
    from lmer_platform import spawn

    create(client, name="webapp", preset="webapp_dev")

    status = slots.slot_status(
        cfg.with_stored_slots(cfg.load()), "webapp", cached=False,
    )
    assert status is not None and status.usable
    # And the gate itself resolves it off a config that predates the write.
    request, *_ = spawn._claim_slot(
        cfg.load(), spawn.SpawnRequest(
            taskdef="develop", target="t", repo_url="r", slot="webapp",
        ),
    )
    assert request.preset == "webapp_dev"


# --- G2: the write preserves everything it is not editing --------------------

def test_the_write_preserves_unknown_keys_and_other_settings(client):
    write_config({
        "bind_port": 8899,
        "matrix": {"room": "!r:example.org"},
        "a_key_from_a_newer_build": {"kept": True},
    })

    create(client, name="webapp", preset="webapp_dev")

    stored = read_config()
    assert stored["bind_port"] == 8899
    assert stored["matrix"] == {"room": "!r:example.org"}
    assert stored["a_key_from_a_newer_build"] == {"kept": True}
    assert [entry["name"] for entry in stored["slots"]] == ["webapp"]


def test_an_edit_keeps_the_entrys_own_unknown_keys_and_its_position(client):
    write_config({"slots": [
        {"name": "webapp", "preset": "webapp_dev", "note_from_a_newer_build": 7},
        {"name": "other", "preset": "other_dev"},
    ]})

    response = client.put(
        "/api/slots/webapp",
        json={"description": "the web stack"},
        headers={**auth(), "If-Match": etag_of(client, "webapp")},
    )

    assert response.status_code == 200, response.text
    entries = stored_slots()
    assert [entry["name"] for entry in entries] == ["webapp", "other"], (
        "position decides the one-service-one-slot race and must not move"
    )
    assert entries[0]["note_from_a_newer_build"] == 7
    assert entries[0]["description"] == "the web stack"


def test_editing_one_slot_leaves_a_concurrently_added_one_alone(client):
    """The window the lock covers: two writes, neither losing the other."""
    create(client, name="webapp", preset="webapp_dev")
    etag = etag_of(client, "webapp")
    # An operator adds a slot by hand after the form was read.
    write_config({"slots": stored_slots() + [
        {"name": "other", "preset": "other_dev"},
    ]})

    response = client.put(
        "/api/slots/webapp",
        json={"description": "edited"},
        headers={**auth(), "If-Match": etag},
    )

    assert response.status_code == 200, response.text
    assert [entry["name"] for entry in stored_slots()] == ["webapp", "other"]


# --- G3: a concurrent hand edit is detected ----------------------------------

def test_an_edit_made_behind_the_form_is_refused_not_overwritten(client):
    create(client, name="webapp", preset="webapp_dev")
    stale = etag_of(client, "webapp")
    write_config({"slots": [
        {"name": "webapp", "preset": "other_dev", "description": "by hand"},
    ]})

    response = client.put(
        "/api/slots/webapp",
        json={"description": "from the form"},
        headers={**auth(), "If-Match": stale},
    )

    assert response.status_code == 412, response.text
    assert response.json()["detail"]["code"] == "slot_changed"
    assert stored_slots() == [
        {"name": "webapp", "preset": "other_dev", "description": "by hand"},
    ], "the hand edit was overwritten"


def test_a_removal_against_a_stale_read_is_refused(client):
    create(client, name="webapp", preset="webapp_dev")
    stale = etag_of(client, "webapp")
    write_config({"slots": [{"name": "webapp", "preset": "other_dev"}]})

    response = client.delete(
        "/api/slots/webapp", headers={**auth(), "If-Match": stale},
    )

    assert response.status_code == 412
    assert stored_slots() == [{"name": "webapp", "preset": "other_dev"}]


def test_an_edit_without_an_etag_is_refused(client):
    create(client, name="webapp", preset="webapp_dev")

    response = client.put(
        "/api/slots/webapp", json={"description": "x"}, headers=auth(),
    )

    assert response.status_code == 428
    assert response.json()["detail"]["code"] == "etag_required"


def test_a_quoted_etag_is_accepted(client):
    """Browsers and proxies quote an If-Match; the row publishes it bare."""
    create(client, name="webapp", preset="webapp_dev")

    response = client.put(
        "/api/slots/webapp",
        json={"description": "x"},
        headers={**auth(), "If-Match": f'"{etag_of(client, "webapp")}"'},
    )

    assert response.status_code == 200, response.text


def test_the_etag_ignores_key_order_but_not_content(client):
    write_config({"slots": [{"preset": "webapp_dev", "name": "webapp"}]})
    reordered = etag_of(client, "webapp")
    write_config({"slots": [{"name": "webapp", "preset": "webapp_dev"}]})

    assert etag_of(client, "webapp") == reordered

    write_config({"slots": [{"name": "webapp", "preset": "other_dev"}]})
    assert etag_of(client, "webapp") != reordered


def test_an_edit_lands_on_the_entry_the_daemon_actually_reads(client, caplog):
    """A malformed entry carrying the same name is skipped when slots load.

    So "the first entry with this name" and "the entry this row is" are not the
    same one, and an edit that landed on the first would change something
    nothing reads while leaving the live definition alone.
    """
    write_config({"slots": [
        {"name": "webapp"},
        {"name": "webapp", "preset": "webapp_dev"},
    ]})
    with caplog.at_level("WARNING", logger="lmer_platform.slots"):
        etag = etag_of(client, "webapp")

    response = client.put(
        "/api/slots/webapp", json={"preset": "other_dev"},
        headers={**auth(), "If-Match": etag},
    )

    assert response.status_code == 200, response.text
    entries = stored_slots()
    assert entries[0] == {"name": "webapp"}, "the edit landed on the dead entry"
    assert entries[1] == {"name": "webapp", "preset": "other_dev"}


def test_editing_a_slot_this_host_does_not_declare_is_a_404(client):
    response = client.put(
        "/api/slots/nope", json={"description": "x"},
        headers={**auth(), "If-Match": "whatever"},
    )

    assert response.status_code == 404


def test_a_write_against_an_unreadable_config_refuses_rather_than_replaces(
    client, platform_root,
):
    """A read the store *refuses* must not become an empty stored config.

    The newer-schema branch is the case that makes the old fallback destructive:
    ``read_json`` deliberately leaves those bytes alone (nothing is moved
    aside), so starting from ``{}`` and writing would publish a slots-only file
    over settings only that newer build understands — and answer 201.
    """
    future = _make_unreadable({
        "schema": store.SCHEMA_VERSION + 1,
        "repo_url": "https://git.example.com/team/app.git",
        "a_key_from_a_newer_build": {"kept": True},
    })

    response = create(client, name="webapp", preset="webapp_dev")

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "config_unreadable"
    assert json.loads(cfg.config_path().read_text(encoding="utf-8")) == future, (
        "the write replaced a file it could not read"
    )


def _make_unreadable(payload=None):
    """A ``config.json`` the store refuses to read, leaving its bytes alone."""
    future = payload or {
        "schema": store.SCHEMA_VERSION + 1,
        "repo_url": "https://git.example.com/team/app.git",
    }
    path = cfg.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(future), encoding="utf-8")
    return future


def test_a_preview_against_an_unreadable_config_refuses_rather_than_guesses(
    client, platform_root,
):
    """The second call site of the same fault (review iteration 2).

    ``stored_slots()`` answers ``()`` on a read it could not do, so the editor's
    live preview resolved a candidate against a host it never saw — reporting
    "this host declares no service slot" about a file nobody could read, while
    the save behind the same button refused. Both now refuse.
    """
    _make_unreadable()

    response = client.post(
        "/api/slots", params={"dry_run": True},
        json={"name": "webapp", "preset": "webapp_dev"}, headers=auth(),
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "config_unreadable"


def test_the_preview_and_the_save_refuse_in_the_same_words(client, platform_root):
    """One sentence, because the editor shows both in the same box.

    Read at runtime rather than asserted against a literal, so improving one has
    to improve the other — the rule `_unknown_preset_reason` exists for.
    """
    _make_unreadable()
    body = {"name": "webapp", "preset": "webapp_dev"}

    preview = client.post(
        "/api/slots", params={"dry_run": True}, json=body, headers=auth(),
    )
    save = client.post("/api/slots", json=body, headers=auth())

    assert preview.status_code == save.status_code == 409
    assert preview.json()["detail"] == save.json()["detail"]


def test_previewing_an_edit_against_an_unreadable_config_refuses_too(
    client, platform_root,
):
    """The edit path reads the file twice — once for the rebinding check, once
    for the candidate list — and both are reads made on the operator's behalf."""
    create(client, name="webapp", preset="webapp_dev")
    etag = etag_of(client, "webapp")
    _make_unreadable()

    response = client.put(
        "/api/slots/webapp", params={"dry_run": True},
        json={"preset": "other_dev"},
        headers={**auth(), "If-Match": etag},
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "config_unreadable"


def test_the_payload_says_why_the_slots_are_missing(client, platform_root):
    """Empty slots is a claim about the host; a failed read must not make it.

    Before #256 the boot-time value carried the rows across a transient read
    failure. Re-reading per request moves that failure onto every poll — the
    trade hot slots buy — so the payload has to be able to say so, or a
    `config.json` saved mid-edit renders as "No service slots configured."
    """
    create(client, name="webapp", preset="webapp_dev")
    assert client.get("/api/state", headers=auth()).json()["slots_problem"] is None

    cfg.config_path().write_text("{ not json", encoding="utf-8")
    payload = client.get("/api/state", headers=auth()).json()

    assert payload["slots"] == []
    assert payload["slots_problem"], "the rows vanished and nothing says why"
    assert "config.json" in payload["slots_problem"]


# --- G4: the form's verdict is the row's verdict -----------------------------

def test_a_dry_run_reports_the_row_without_writing(client):
    response = client.post(
        "/api/slots", params={"dry_run": True},
        json={"name": "webapp", "preset": "webapp_dev"}, headers=auth(),
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == slots.SLOT_FREE
    assert stored_slots() == [], "a dry run wrote to config.json"


def test_a_dry_run_reports_a_service_already_bound_by_another_slot(client):
    create(client, name="webapp", preset="webapp_dev")

    response = client.post(
        "/api/slots", params={"dry_run": True},
        # Resolves to the same service as webapp_dev.
        json={"name": "second", "preset": "webapp_alt"}, headers=auth(),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == slots.SLOT_MISCONFIGURED
    assert "already bound by slot 'webapp'" in body["reason"]


def test_the_preview_reason_is_the_sentence_the_row_shows(client):
    """One implementation, so the form and the row cannot drift apart."""
    create(client, name="webapp", preset="webapp_dev")
    preview = client.post(
        "/api/slots", params={"dry_run": True},
        json={"name": "second", "preset": "webapp_alt"}, headers=auth(),
    ).json()

    create(client, name="second", preset="webapp_alt")

    assert row_of(client, "second")["reason"] == preview["reason"]


def test_a_preset_that_sets_no_service_is_reported_before_the_save(client):
    response = client.post(
        "/api/slots", params={"dry_run": True},
        json={"name": "plain", "preset": "no_service"}, headers=auth(),
    )

    assert response.status_code == 200, response.text
    assert "sets no service" in response.json()["reason"]


def test_an_unknown_preset_is_refused_in_the_rows_own_sentence(client):
    response = create(client, name="webapp", preset="not_a_preset")

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "unknown_preset"
    # The row's own words, because both can be on screen at once: the presets
    # file is hot, so a slot can go unknown between the dialog opening and the
    # save while an existing row reports the same fault beside it.
    assert detail["message"] == slots._unknown_preset_reason("not_a_preset")
    assert stored_slots() == []


def test_a_host_with_no_presets_refuses_with_the_rows_own_sentence(
    client, monkeypatch,
):
    monkeypatch.delenv("LMER_PRESETS_FILE", raising=False)

    response = create(client, name="webapp", preset="webapp_dev")

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "no_presets"
    assert detail["message"] == slots._no_presets_reason("webapp_dev")


def test_the_refusal_and_the_row_say_the_same_thing_about_one_preset(
    client, tmp_path, monkeypatch,
):
    """The two-in-one-viewport case, end to end.

    An existing slot's preset disappears from the presets file. The row reports
    it; a save naming the same preset is refused. Both sentences are read here
    rather than asserted against a literal, so improving one has to improve the
    other.
    """
    create(client, name="webapp", preset="webapp_dev")
    thin = tmp_path / "thin.json"
    thin.write_text('{"other_dev": {"checkout": "/srv/o", "service": "o"}}',
                    encoding="utf-8")
    monkeypatch.setenv("LMER_PRESETS_FILE", str(thin))

    refusal = create(client, name="second", preset="webapp_dev")

    assert refusal.status_code == 400
    assert refusal.json()["detail"]["message"] == row_of(client, "webapp")["reason"]


def test_a_duplicate_name_is_refused(client):
    create(client, name="webapp", preset="webapp_dev")

    response = create(client, name="webapp", preset="other_dev")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "name_taken"
    assert len(stored_slots()) == 1


@pytest.mark.parametrize("name", ["team/web", ".", ".."])
def test_a_name_the_routes_could_not_address_again_is_refused(client, name):
    """Measured against these routes, not reasoned about.

    They carry the name as one path segment: a percent-encoded ``/`` is resolved
    before routing and never matches, and ``.``/``..`` are normalised away. A
    slot created with one would render a row whose pencil 404s on its own
    preview — creatable from the editor, changeable only by hand.
    """
    response = create(client, name=name, preset="webapp_dev")

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "unaddressable_name"
    assert stored_slots() == []


@pytest.mark.parametrize("name", ["a b", "with.dot", "a:b", "a?b", "über"])
def test_names_the_routes_can_address_are_not_refused(client, name):
    """The refusal is exactly the unaddressable set — measured, not a guess.

    Every one of these round-trips through the path segment, so refusing them
    would be this route inventing a naming policy the rest of the tool has not
    got.
    """
    assert create(client, name=name, preset="webapp_dev").status_code == 201

    etag = etag_of(client, name)
    quoted = urllib.parse.quote(name, safe="")
    assert client.put(
        f"/api/slots/{quoted}", json={"description": "reachable"},
        headers={**auth(), "If-Match": etag},
    ).status_code == 200
    assert client.delete(
        f"/api/slots/{quoted}",
        headers={**auth(), "If-Match": etag_of(client, name)},
    ).status_code == 200


@pytest.mark.parametrize("body", [
    {"preset": "webapp_dev"},
    {"name": "webapp"},
    {"name": "   ", "preset": "webapp_dev"},
])
def test_an_incomplete_slot_is_refused(client, body):
    assert create(client, **body).status_code == 400


def test_an_unknown_field_is_refused_rather_than_stored(client):
    response = create(
        client, name="webapp", preset="webapp_dev", servcie="typo",
    )

    assert response.status_code == 400
    assert "servcie" in response.json()["detail"]
    assert stored_slots() == []


def test_a_description_is_cleared_by_an_empty_string(client):
    create(client, name="webapp", preset="webapp_dev", description="a stack")

    client.put(
        "/api/slots/webapp", json={"description": ""},
        headers={**auth(), "If-Match": etag_of(client, "webapp")},
    )

    assert "description" not in stored_slots()[0]


# --- G5: nothing is applied silently under a running session -----------------

def test_removing_an_occupied_slot_is_refused(client):
    create(client, name="webapp", preset="webapp_dev")
    register("s-live", slot="webapp")

    response = client.delete(
        "/api/slots/webapp", headers={**auth(), "If-Match": etag_of(client, "webapp")},
    )

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "slot_occupied"
    assert "s-live" in detail["message"]
    assert len(stored_slots()) == 1


def test_removing_a_duplicated_name_takes_the_shadow_with_it(client, caplog):
    """One row, two entries: removing the row must not leave the name behind.

    ``parse_slots`` keeps the first usable entry and skips the rest, so the
    second one is a shadow the fleet view never shows. Removing only the
    resolved index left the row on screen backed by the shadow — the button
    reading as broken, and one press needed per duplicate.
    """
    write_config({"slots": [
        {"name": "web", "preset": "webapp_dev"},
        {"name": "web", "preset": "other_dev"},
    ]})
    with caplog.at_level("WARNING", logger="lmer_platform.slots"):
        etag = etag_of(client, "web")

    response = client.delete(
        "/api/slots/web", headers={**auth(), "If-Match": etag},
    )

    assert response.status_code == 200, response.text
    assert len(response.json()["removed"]) == 2
    assert stored_slots() == []
    assert row_of(client, "web") is None


def test_removing_a_name_leaves_a_malformed_entry_of_the_same_name_nowhere(
    client, caplog,
):
    """A shadow need not be well formed to be a shadow."""
    write_config({"slots": [
        {"name": "web", "preset": "webapp_dev"},
        {"name": "web"},
    ]})
    with caplog.at_level("WARNING", logger="lmer_platform.slots"):
        etag = etag_of(client, "web")

    client.delete("/api/slots/web", headers={**auth(), "If-Match": etag})

    assert stored_slots() == []


def test_removing_an_occupied_slot_goes_through_on_an_explicit_confirm(client):
    create(client, name="webapp", preset="webapp_dev")
    register("s-live", slot="webapp")

    response = client.delete(
        "/api/slots/webapp", params={"confirm": True},
        headers={**auth(), "If-Match": etag_of(client, "webapp")},
    )

    assert response.status_code == 200, response.text
    assert stored_slots() == []


def test_a_slot_held_by_a_dead_session_is_removed_without_a_confirm(client):
    """Occupancy is liveness, not a record — a crashed session strands nothing."""
    create(client, name="webapp", preset="webapp_dev")
    register("s-dead", slot="webapp", pid=DEAD_PID)

    response = client.delete(
        "/api/slots/webapp", headers={**auth(), "If-Match": etag_of(client, "webapp")},
    )

    assert response.status_code == 200, response.text


def test_rebinding_an_occupied_slot_is_refused(client):
    create(client, name="webapp", preset="webapp_dev")
    register("s-live", slot="webapp")

    response = client.put(
        "/api/slots/webapp", json={"preset": "other_dev"},
        headers={**auth(), "If-Match": etag_of(client, "webapp")},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "slot_occupied"
    assert stored_slots()[0]["preset"] == "webapp_dev"


def test_describing_an_occupied_slot_is_not_gated(client):
    """A description changes nothing about what the session is bound to."""
    create(client, name="webapp", preset="webapp_dev")
    register("s-live", slot="webapp")

    response = client.put(
        "/api/slots/webapp", json={"description": "still fine"},
        headers={**auth(), "If-Match": etag_of(client, "webapp")},
    )

    assert response.status_code == 200, response.text


def test_re_sending_an_occupied_slots_own_preset_is_not_a_rebinding(client):
    """What the editor's live preview does on every keystroke.

    The gate is on the binding moving, not on the field appearing in the body —
    otherwise an occupied slot could not be previewed or described at all.
    """
    create(client, name="webapp", preset="webapp_dev")
    register("s-live", slot="webapp")

    response = client.put(
        "/api/slots/webapp",
        json={"preset": "webapp_dev", "description": "renamed"},
        headers={**auth(), "If-Match": etag_of(client, "webapp")},
    )

    assert response.status_code == 200, response.text
    assert stored_slots()[0]["description"] == "renamed"


def test_previewing_a_rebinding_of_an_occupied_slot_is_never_gated(client):
    """A dry run writes nothing, so there is nothing to refuse.

    Gating it hid the verdict the operator needs in order to decide whether to
    confirm — the form showed "pass confirm=true" and no row at all.
    """
    create(client, name="webapp", preset="webapp_dev")
    register("s-live", slot="webapp")

    response = client.put(
        "/api/slots/webapp", params={"dry_run": True},
        json={"preset": "other_dev"},
        headers={**auth(), "If-Match": etag_of(client, "webapp")},
    )

    assert response.status_code == 200, response.text
    assert response.json()["service"] == "other-web"
    assert stored_slots()[0]["preset"] == "webapp_dev", "a dry run wrote"


def test_rebinding_an_occupied_slot_goes_through_on_an_explicit_confirm(client):
    create(client, name="webapp", preset="webapp_dev")
    register("s-live", slot="webapp")

    response = client.put(
        "/api/slots/webapp", params={"confirm": True},
        json={"preset": "other_dev"},
        headers={**auth(), "If-Match": etag_of(client, "webapp")},
    )

    assert response.status_code == 200, response.text
    assert stored_slots()[0]["preset"] == "other_dev"


def test_a_service_held_under_another_slot_does_not_block_this_one(client):
    """The case that would make a broken slot unfixable.

    Two slots resolve to one service; the second is unusable, and a session runs
    under the first. Editing the *second* must not be refused for occupancy it
    does not have — that is exactly the state an operator edits their way out of.
    """
    write_config({"slots": [
        {"name": "webapp", "preset": "webapp_dev"},
        {"name": "second", "preset": "webapp_alt"},
    ]})
    register("s-live", slot="webapp")

    response = client.put(
        "/api/slots/second", json={"preset": "other_dev"},
        headers={**auth(), "If-Match": etag_of(client, "second")},
    )

    assert response.status_code == 200, response.text
    assert stored_slots()[1]["preset"] == "other_dev"
