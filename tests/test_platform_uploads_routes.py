"""The two routes behind chat file upload (issue #246).

Driven through the app, because what is being pinned here is what a *client* gets
back: the status a refusal arrives with, and the reference line the composer then
puts in its message. The store's own rules — which types, how big, whose store —
are ``tests/test_platform_uploads.py``.

The read route is the one to be suspicious of: it serves operator content back to
a browser, so it is checked for what it will *not* do — leave the session's own
store, follow a symlink out of it, or let the browser decide for itself what a
file is.
"""

import base64
import os

import pytest
from fastapi.testclient import TestClient

from lmer_platform import api, registry, spawn, store, uploads
from lmer_platform import config as cfg
from tests.conftest import strip_lmer_env

SECRET = "test-secret-value"

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
#: A type the default allowlist does not carry, so "refused" is the default
#: platform's own answer rather than a reconfigured one.
GIF = b"GIF89a" + b"\x00" * 64


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    for name in (cfg.ENV_UPLOAD_TYPES, cfg.ENV_UPLOAD_MAX_BYTES):
        monkeypatch.delenv(name, raising=False)


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


def register(session_id="20260826-120000-1-4242", *, kind=registry.WORKER_KIND,
             mounted=True, live=True):
    """A registered session, with the upload store its spawn would have made."""
    directory = None
    if mounted:
        directory = uploads.prepare_upload_dir(uploads.upload_dir_for(session_id, kind))
    log = store.logs_dir() / f"{session_id}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(b"")
    registry.register(
        session_id,
        kind=kind,
        # A live pid, because "can this session be handed a file" is a question
        # about a container that is up — and a genuinely dead one for the other
        # case, found rather than guessed: pid 1 is init, which is very much
        # alive in a container.
        pid=os.getpid() if live else _dead_pid(),
        uploads=uploads.registry_pointer(directory),
        log_path=str(log),
    )
    return session_id


def _dead_pid():
    """A pid nothing is using, so ``registry.is_live`` answers no."""
    for candidate in range(4_194_300, 4_194_000, -1):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except PermissionError:
            continue
    raise AssertionError("no free pid to stand in for an exited session")


def encoded(raw):
    return base64.b64encode(raw).decode("ascii")


def post(client, session_id, name="shot.png", raw=PNG):
    return client.post(
        f"/api/sessions/{session_id}/uploads",
        json={"name": name, "data": encoded(raw)},
        headers=auth(),
    )


# --- storing one -------------------------------------------------------------

def test_an_upload_comes_back_with_the_line_to_put_in_the_message(client, platform_root):
    """The reply is what the composer needs and nothing more: where the file is
    inside the container, and the sentence naming it."""
    session_id = register()
    reply = post(client, session_id)
    assert reply.status_code == 201, reply.text
    body = reply.json()
    assert body["kind"] == "png"
    assert body["container_path"].startswith(uploads.CONTAINER_UPLOADS_DIR + "/")
    assert body["reference"].startswith(uploads.REFERENCE_PREFIX)
    assert body["container_path"] in body["reference"]
    stored = uploads.upload_dir_for(session_id, registry.WORKER_KIND) / body["name"]
    assert stored.read_bytes() == PNG


def test_storing_a_file_types_nothing_at_the_session(client, platform_root, monkeypatch):
    """Two acts, not one: an operator who attaches something and then thinks
    better of it has sent nothing."""
    def fail(*_args, **_kwargs):
        raise AssertionError("the upload route typed at the session")

    monkeypatch.setattr(api.session_io, "send_input", fail)
    assert post(client, register()).status_code == 201


def test_a_session_with_no_store_is_told_how_to_get_one(client, platform_root):
    """The pre-existing-session case, which every operator hits once: the
    container is up and has no mount, and no retry changes that."""
    reply = post(client, register(mounted=False))
    assert reply.status_code == 409
    assert "restart" in reply.json()["detail"].lower()


def test_a_session_that_has_exited_is_a_410(client, platform_root):
    """Different news from the one above, and a different next move: nothing is
    reading the store, and the message would reach nobody either."""
    reply = post(client, register(live=False))
    assert reply.status_code == 410
    assert "exited" in reply.json()["detail"]


def test_an_unknown_session_is_a_404(client, platform_root):
    reply = post(client, "20260826-120000-9-9999")
    assert reply.status_code == 404


def test_a_refused_type_is_a_415_naming_what_is_accepted(client, platform_root):
    """A GIF on a host running the default allowlist — and the refusal says what
    would have been taken, since the operator cannot see the config."""
    reply = post(client, register(), name="shot.gif", raw=GIF)
    assert reply.status_code == 415
    assert "png" in reply.json()["detail"]


def test_an_over_cap_file_is_a_413(client, platform_root, monkeypatch):
    monkeypatch.setenv(cfg.ENV_UPLOAD_MAX_BYTES, "32")
    client = TestClient(api.create_app(cfg.load(), SECRET))
    reply = post(client, register())
    assert reply.status_code == 413


def test_the_route_honours_the_configured_type_list(client, platform_root, monkeypatch):
    """The knob is the daemon's, read at request time from the config the app was
    built with — not a constant in the route."""
    monkeypatch.setenv(cfg.ENV_UPLOAD_TYPES, "jpeg")
    client = TestClient(api.create_app(cfg.load(), SECRET))
    session_id = register()
    assert post(client, session_id, name="shot.jpg", raw=JPEG).status_code == 201
    assert post(client, session_id).status_code == 415


@pytest.mark.parametrize("body", [
    {}, {"name": "shot.png"}, {"name": "shot.png", "data": "not base64!!"},
    {"name": "shot.png", "data": ""},
])
def test_an_unusable_body_is_a_400(client, platform_root, body):
    session_id = register()
    reply = client.post(
        f"/api/sessions/{session_id}/uploads", json=body, headers=auth(),
    )
    assert reply.status_code == 400


def test_an_upload_needs_the_secret(client, platform_root):
    session_id = register()
    reply = client.post(
        f"/api/sessions/{session_id}/uploads",
        json={"name": "shot.png", "data": encoded(PNG)},
    )
    assert reply.status_code == 401


# --- reading one back --------------------------------------------------------

def test_an_image_comes_back_renderable_but_never_sniffable(client, platform_root):
    """What a thumbnail needs (inline, with the right type) and what it must not
    allow (a browser deciding the type for itself)."""
    session_id = register()
    name = post(client, session_id).json()["name"]
    reply = client.get(
        f"/api/sessions/{session_id}/uploads/{name}", headers=auth(),
    )
    assert reply.status_code == 200
    assert reply.content == PNG
    assert reply.headers["content-type"] == "image/png"
    assert reply.headers["x-content-type-options"] == "nosniff"
    assert reply.headers["content-disposition"].startswith("inline")


def test_a_non_image_is_served_as_a_download(client, platform_root):
    """"Render this in the operator's browser" is a decision per type, and text
    that arrived from outside does not get it."""
    session_id = register()
    name = post(client, session_id, name="notes.txt", raw=b"plain words").json()["name"]
    reply = client.get(
        f"/api/sessions/{session_id}/uploads/{name}", headers=auth(),
    )
    assert reply.headers["content-disposition"].startswith("attachment")
    assert reply.headers["content-type"].startswith("text/plain")


def test_the_served_type_is_the_file_s_current_one(client, platform_root):
    """The session can replace the file through the mount, so the type is read
    off the bytes on the way out rather than remembered from the write."""
    session_id = register()
    name = post(client, session_id).json()["name"]
    (uploads.upload_dir_for(session_id, registry.WORKER_KIND) / name).write_bytes(
        b"not a png any more"
    )
    reply = client.get(
        f"/api/sessions/{session_id}/uploads/{name}", headers=auth(),
    )
    assert reply.headers["content-disposition"].startswith("attachment")


@pytest.mark.parametrize("name", [
    "..%2f..%2fetc%2fpasswd", "%2fetc%2fpasswd", "nothing-here.png",
])
def test_a_read_cannot_leave_the_session_s_own_store(client, platform_root, name):
    """The name is matched against the store's listing, so a path is not
    "rejected" — it is simply not one of the files there."""
    session_id = register()
    post(client, session_id)
    reply = client.get(
        f"/api/sessions/{session_id}/uploads/{name}", headers=auth(),
    )
    assert reply.status_code == 404


def test_a_symlink_in_the_store_is_not_served(client, platform_root, tmp_path):
    """The store is rw-mounted into a container an agent drives."""
    session_id = register()
    secret = tmp_path / "outside"
    secret.write_text("a token", encoding="utf-8")
    directory = uploads.upload_dir_for(session_id, registry.WORKER_KIND)
    (directory / "looks-fine.png").symlink_to(secret)
    reply = client.get(
        f"/api/sessions/{session_id}/uploads/looks-fine.png", headers=auth(),
    )
    assert reply.status_code == 404


def test_a_read_needs_the_secret(client, platform_root):
    session_id = register()
    name = post(client, session_id).json()["name"]
    assert client.get(f"/api/sessions/{session_id}/uploads/{name}").status_code == 401


def test_an_upload_still_reads_back_after_the_session_exited(client, platform_root):
    """The history case: a phone opening the run hours later. The write needs a
    live session; the read is part of a conversation that outlives it."""
    session_id = register()
    name = post(client, session_id).json()["name"]
    registry.remove(session_id, force=True)
    reply = client.get(
        f"/api/sessions/{session_id}/uploads/{name}", headers=auth(),
    )
    assert reply.status_code == 200
    assert reply.content == PNG


# --- the limits are the daemon's, read when the upload happens ---------------

def test_a_cap_persisted_through_the_config_route_applies_without_a_restart(
    client, platform_root,
):
    """``upload_max_bytes`` is in ``INT_SETTINGS``, which is where
    ``POST /api/assistant/config`` gets its accepted integer keys — so that route
    already validated it, persisted it and answered 200 with it under
    ``changed``. Reading the cap off the config the app was *built* with made
    that 200 a lie until the daemon restarted (!272 review)."""
    session_id = register()
    assert post(client, session_id).status_code == 201
    saved = client.post(
        "/api/assistant/config", json={"upload_max_bytes": 32}, headers=auth(),
    )
    assert saved.status_code == 200, saved.text
    assert "upload_max_bytes" in saved.json()["changed"]
    # Same app object, same process, no restart.
    assert post(client, session_id).status_code == 413


def test_the_config_route_shows_the_upload_group_it_accepts_writes_for(
    client, platform_root,
):
    """A key a route persists and reports as changed while showing nothing of it
    back is a 200 an operator cannot check. ``types`` rides along read-only —
    a cap without the allowlist beside it is half the answer."""
    body = client.get("/api/assistant/config", headers=auth()).json()
    assert body["uploads"]["max_bytes"]["value"] == cfg.DEFAULT_UPLOAD_MAX_BYTES
    assert body["uploads"]["max_bytes"]["source"] == "default"
    assert body["uploads"]["types"] == list(cfg.DEFAULT_UPLOAD_TYPES)


def test_a_type_list_edited_in_the_file_applies_without_a_restart(
    client, platform_root,
):
    """The other half of the group, and the case an operator hits when they want
    this host to stop taking uploads at all."""
    session_id = register()
    assert post(client, session_id).status_code == 201
    store.write_json(cfg.config_path(), {"upload_types": []})
    refused = post(client, session_id)
    assert refused.status_code == 415
    assert "LMER_PLATFORM_UPLOAD_TYPES" in refused.json()["detail"]


def test_an_upload_is_streamed_from_the_descriptor_the_store_opened(
    client, platform_root,
):
    """Not re-opened from a path at send time, which is the window the container
    owned (!272 review). What a client can see of that is the bytes and the
    headers, so this pins those and ``tests/test_platform_uploads.py`` pins the
    mechanism."""
    session_id = register()
    name = post(client, session_id).json()["name"]
    reply = client.get(f"/api/sessions/{session_id}/uploads/{name}", headers=auth())
    assert reply.status_code == 200
    assert reply.content == PNG
    assert reply.headers["content-type"] == "image/png"
    assert reply.headers["content-disposition"] == f'inline; filename="{name}"'


# --- uber lmer ---------------------------------------------------------------

def test_the_assistant_s_upload_lands_in_the_host_store(client, platform_root):
    """One route, two stores: the surface differs in which session it names, not
    in what a pasted file is."""
    session_id = register("20260826-120000-2-4243", kind=registry.ASSISTANT_KIND)
    body = post(client, session_id).json()
    assert (uploads.assistant_upload_dir() / body["name"]).is_file()
    assert not uploads.session_upload_dir(session_id).exists()
    reply = client.get(
        f"/api/sessions/{session_id}/uploads/{body['name']}", headers=auth(),
    )
    assert reply.status_code == 200


# --- the route list ----------------------------------------------------------

def test_the_api_index_names_both_routes(client, platform_root):
    """The plain-text index is how someone with a terminal finds these."""
    text = client.get("/api", headers=auth()).text
    assert "/api/sessions/{id}/uploads" in text
    assert "uploads/{name}" in text
