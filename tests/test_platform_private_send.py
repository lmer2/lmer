"""Send Privately (issue #510): private text as a 0400 file, kept out of chat.

The scope is the one the operator set on 2026-10-28: the existing upload store,
plus a restrictive mode, a reference-only chat line and cleanup when the session
ends. The test markers are synthetic and random per test.

The end-to-end test drives a real supervisor bus (the ``BusPlane`` of
``tests/test_platform_comms.py``). It then greps every byte the platform wrote,
every byte typed at the session, and every log record for the marker. Slack and
Matrix mirror what the bus and the journal hold, so a marker absent from those is
absent from the mirrors.
"""
from __future__ import annotations

import logging
import os
import secrets
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ask_channel import signal_cli
from lmer_platform import api, detect, registry, spawn, store, uploads
from lmer_platform import config as cfg
from tests.conftest import strip_lmer_env
from tests.test_platform_comms import BusPlane, auth, bearer, live_session
from tests.test_platform_detection import Spool, fleet
from tests.test_platform_uploads_routes import register

SECRET = "test-secret-value"
WORKER = "20261028-120000-1-4242"
ASSISTANT = "20261028-120000-2-4243"
SUCCESSOR = "20261028-130000-2-4244"


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
def client(platform_root):
    return TestClient(api.create_app(cfg.load(), SECRET))


@pytest.fixture
def marker():
    return f"PRIVMARK{secrets.token_hex(8)}"


def entry_for(session_id, kind=registry.WORKER_KIND):
    directory = uploads.prepare_upload_dir(uploads.upload_dir_for(session_id, kind))
    return {"id": session_id, "kind": kind, "uploads": uploads.registry_pointer(directory)}


def host_path(stored, kind=registry.WORKER_KIND):
    relative = stored.container_path[len(uploads.CONTAINER_UPLOADS_DIR) + 1:]
    return uploads.upload_dir_for(stored.session_id, kind) / relative


def files_containing(root: Path, needle: str) -> list:
    """Every file under *root* whose bytes contain *needle*, whatever its mode."""
    found = []
    raw = needle.encode("utf-8")
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        mode = path.stat().st_mode
        if not mode & stat.S_IRUSR:
            path.chmod(mode | stat.S_IRUSR)
        if raw in path.read_bytes():
            found.append(path)
    return found


# --- the store ----------------------------------------------------------------

def test_the_text_is_a_0400_file_in_the_session_s_own_private_directory(platform_root, marker):
    text = f"line one ✓ {marker}\nzweite Zeile — ünïcödé\n"
    stored = uploads.store_private(WORKER, entry_for(WORKER), text)
    path = host_path(stored)
    assert path.parent == uploads.session_upload_dir(WORKER) / uploads.PRIVATE_DIRNAME / WORKER
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700
    assert path.read_bytes() == text.encode("utf-8")
    assert marker not in path.name


def test_the_reference_names_the_path_and_never_the_text_or_its_size(platform_root, marker):
    text = marker * 7
    stored = uploads.store_private(WORKER, entry_for(WORKER), text)
    reply = stored.to_dict()
    assert reply["reference"].startswith(f"{uploads.PRIVATE_PREFIX} {stored.container_path} ")
    assert "chmod 000" in reply["reference"]
    flat = repr(reply)
    assert marker not in flat
    assert str(len(text)) not in flat and str(len(text.encode())) not in flat
    assert "bytes" not in reply


@pytest.mark.parametrize("text", ["", "   \n\t ", None, 42, ["x"]])
def test_empty_or_non_text_input_is_refused_and_nothing_is_written(platform_root, text):
    with pytest.raises(uploads.UploadRejected):
        uploads.store_private(WORKER, entry_for(WORKER), text)
    assert not (uploads.session_upload_dir(WORKER) / uploads.PRIVATE_DIRNAME).exists()


def test_an_over_cap_text_is_refused_without_quoting_or_measuring_it(platform_root, marker):
    text = marker * 100
    with pytest.raises(uploads.UploadTooLarge) as caught:
        uploads.store_private(WORKER, entry_for(WORKER), text, max_bytes=64)
    assert marker not in str(caught.value) and str(len(text)) not in str(caught.value)


def test_two_sends_in_one_second_are_two_files(platform_root):
    entry = entry_for(WORKER)
    first = uploads.store_private(WORKER, entry, "one")
    second = uploads.store_private(WORKER, entry, "two")
    assert first.name != second.name
    assert host_path(first).read_text() == "one" and host_path(second).read_text() == "two"


def test_a_session_without_a_store_is_refused(platform_root):
    with pytest.raises(uploads.StoreUnavailable):
        uploads.store_private(WORKER, {"id": WORKER, "kind": registry.WORKER_KIND}, "x")


@pytest.mark.parametrize("planted", ["root", "session"])
def test_a_symlink_planted_in_the_store_is_never_written_through(platform_root, tmp_path, planted):
    """The container has ``rw`` on the store, so it can plant a link at any level."""
    entry = entry_for(WORKER)
    outside = tmp_path / "outside"
    outside.mkdir()
    private = uploads.session_upload_dir(WORKER) / uploads.PRIVATE_DIRNAME
    if planted == "root":
        private.symlink_to(outside)
    else:
        private.mkdir(mode=0o700)
        (private / WORKER).symlink_to(outside)
    with pytest.raises(uploads.UploadError):
        uploads.store_private(WORKER, entry, "x")
    assert list(outside.iterdir()) == []


def test_the_read_back_route_does_not_serve_private_files(platform_root):
    entry = entry_for(WORKER)
    stored = uploads.store_private(WORKER, entry, "x")
    for name in (uploads.PRIVATE_DIRNAME, f"{uploads.PRIVATE_DIRNAME}/{WORKER}/{stored.name}", stored.name):
        with pytest.raises(uploads.UploadNotFound):
            uploads.open_stored(WORKER, entry, name)


def test_withdraw_removes_one_file_and_refuses_paths(platform_root):
    entry = entry_for(WORKER)
    keep = uploads.store_private(WORKER, entry, "keep")
    going = uploads.store_private(WORKER, entry, "going")
    assert uploads.withdraw_private(WORKER, entry, going.name) is True
    assert not host_path(going).exists() and host_path(keep).exists()
    assert uploads.withdraw_private(WORKER, entry, going.name) is False
    for bad in ("../x", f"{WORKER}/{keep.name}", ".hidden", "", None):
        with pytest.raises(uploads.UploadNotFound):
            uploads.withdraw_private(WORKER, entry, bad)
    assert host_path(keep).exists()


# --- lifetime -------------------------------------------------------------------

def test_discard_deletes_a_worker_s_files_even_after_the_run_locked_them(platform_root):
    entry = entry_for(WORKER)
    stored = uploads.store_private(WORKER, entry, "x")
    attachment = uploads.session_upload_dir(WORKER) / "kept.txt"
    attachment.write_text("an ordinary upload")
    host_path(stored).chmod(0)
    host_path(stored).parent.chmod(0)
    assert uploads.discard_private(WORKER) == 1
    assert not (uploads.session_upload_dir(WORKER) / uploads.PRIVATE_DIRNAME / WORKER).exists()
    assert attachment.read_text() == "an ordinary upload"


def test_discard_removes_a_planted_link_and_never_its_target(platform_root, tmp_path):
    entry = entry_for(WORKER)
    stored = uploads.store_private(WORKER, entry, "x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim").write_text("host file")
    (host_path(stored).parent / "link").symlink_to(outside)
    uploads.discard_private(WORKER)
    assert (outside / "victim").read_text() == "host file"
    assert not host_path(stored).parent.exists()


def test_the_assistant_s_shared_store_loses_one_generation_s_files_only(platform_root):
    old = uploads.store_private(ASSISTANT, entry_for(ASSISTANT, registry.ASSISTANT_KIND), "old")
    new = uploads.store_private(SUCCESSOR, entry_for(SUCCESSOR, registry.ASSISTANT_KIND), "new")
    upload = uploads.assistant_upload_dir() / "notes.txt"
    upload.write_text("kept")
    uploads.discard_private(ASSISTANT)
    assert not host_path(old, registry.ASSISTANT_KIND).exists()
    assert host_path(new, registry.ASSISTANT_KIND).read_text() == "new"
    assert upload.read_text() == "kept"


def test_record_exit_discards_on_a_clean_exit_and_on_a_crash(platform_root):
    for session_id, code in ((WORKER, 0), ("20261028-120000-3-4245", 1)):
        stored = uploads.store_private(session_id, entry_for(session_id), "x")
        spawn.record_exit(session_id, code)
        assert not host_path(stored).exists(), code


def test_the_sweep_deletes_every_session_that_is_not_live_and_keeps_live_ones(platform_root):
    live = register(WORKER)
    dead = register("20261028-120000-3-4245", live=False)
    gone = "20261028-120000-5-4247"
    kept = uploads.store_private(live, registry.read_session(live), "live")
    stale = uploads.store_private(dead, registry.read_session(dead), "dead")
    orphan = uploads.store_private(gone, entry_for(gone), "unregistered")
    removed = uploads.sweep_private()
    assert sorted(removed) == sorted([dead, gone])
    assert host_path(kept).read_text() == "live"
    assert not host_path(stale).exists() and not host_path(orphan).exists()


def test_a_directory_made_after_the_sweep_read_the_live_set_survives(platform_root, monkeypatch):
    """The listing comes first, so a session that starts mid-sweep is not deleted."""
    entry_for(WORKER)
    real = registry.list_sessions
    late = {}

    def list_then_store(**kwargs):
        sessions = real(**kwargs)
        late["stored"] = uploads.store_private("20261028-120000-6-4248", entry_for("20261028-120000-6-4248"), "late")
        return sessions

    monkeypatch.setattr(registry, "list_sessions", list_then_store)
    uploads.store_private(WORKER, entry_for(WORKER), "x")  # a root for the sweep to list
    uploads.sweep_private()
    assert host_path(late["stored"]).read_text() == "late"


def test_the_detector_tick_runs_the_sweep(platform_root, monkeypatch):
    calls = []
    monkeypatch.setattr(uploads, "sweep_private", lambda: calls.append(1) or [])
    detector = detect.Detector(cfg.load(), state_reader=lambda config, **kw: fleet(), notifier=Spool())
    detector.tick_once()
    assert calls == [1]


def test_without_o_path_cleanup_and_stores_take_the_no_follow_chmod_route(platform_root, monkeypatch):
    """The macOS route: no ``O_PATH`` and no ``/proc``, so ``fchmodat`` without following."""
    entry = entry_for(WORKER)
    stored = uploads.store_private(WORKER, entry, "x")
    host_path(stored).parent.chmod(0)
    monkeypatch.delattr(os, "O_PATH")
    real_chmod = os.chmod
    routes = []

    def chmod(path, mode, **kwargs):
        routes.append((str(path), kwargs.get("follow_symlinks", True)))
        return real_chmod(path, mode, **kwargs)

    monkeypatch.setattr(os, "chmod", chmod)
    again = uploads.store_private(WORKER, entry, "y")
    assert host_path(again).read_text() == "y"
    assert uploads.discard_private(WORKER) == 1
    assert (WORKER, False) in routes
    assert not [path for path, _ in routes if path.startswith("/proc/")]
    assert not (uploads.session_upload_dir(WORKER) / uploads.PRIVATE_DIRNAME / WORKER).exists()


def test_one_entry_that_cannot_be_purged_does_not_stop_the_sweep(platform_root, monkeypatch):
    first, second = "20261028-120000-3-4245", "20261028-120000-5-4247"
    for session_id in (first, second):
        uploads.store_private(session_id, entry_for(session_id), "x")
    real = uploads._purge

    def purge(parent_fd, name):
        if name == first:
            raise AttributeError("not an OSError")
        return real(parent_fd, name)

    monkeypatch.setattr(uploads, "_purge", purge)
    assert uploads.sweep_private() == [second]


def test_a_private_folder_of_the_assistant_s_own_is_never_swept(platform_root):
    entry = entry_for(ASSISTANT, registry.ASSISTANT_KIND)
    own = uploads.assistant_upload_dir() / "private" / "notes"
    own.mkdir(parents=True)
    (own / "previously-attached-notes.txt").write_text("kept")
    uploads.store_private(ASSISTANT, entry, "x")
    assert uploads.sweep_private() == [ASSISTANT]
    assert (own / "previously-attached-notes.txt").read_text() == "kept"
    assert (uploads.assistant_upload_dir() / uploads.PRIVATE_DIRNAME / uploads.PRIVATE_MARKER).is_file()


def test_a_reserved_directory_the_platform_did_not_make_is_neither_used_nor_swept(platform_root):
    entry = entry_for(WORKER)
    other = "20261028-120000-3-4245"
    foreign = uploads.session_upload_dir(WORKER) / uploads.PRIVATE_DIRNAME / other
    foreign.mkdir(parents=True)
    (foreign / "theirs.txt").write_text("kept")
    with pytest.raises(uploads.UploadError, match="not made by this platform"):
        uploads.store_private(WORKER, entry, "x")
    assert uploads.sweep_private() == []
    assert uploads.discard_private(other) == 0
    assert (foreign / "theirs.txt").read_text() == "kept"


def test_an_empty_reserved_directory_is_adopted(platform_root):
    entry = entry_for(WORKER)
    root = uploads.session_upload_dir(WORKER) / uploads.PRIVATE_DIRNAME
    root.mkdir()
    stored = uploads.store_private(WORKER, entry, "x")
    assert (root / uploads.PRIVATE_MARKER).is_file()
    assert host_path(stored).read_text() == "x"


def test_a_root_that_lost_its_marker_but_holds_only_private_files_is_reclaimed(platform_root):
    old = uploads.store_private(ASSISTANT, entry_for(ASSISTANT, registry.ASSISTANT_KIND), "old")
    uploads.store_private(ASSISTANT, entry_for(ASSISTANT, registry.ASSISTANT_KIND), "again")
    root = uploads.assistant_upload_dir() / uploads.PRIVATE_DIRNAME
    (root / uploads.PRIVATE_MARKER).unlink()
    host_path(old, registry.ASSISTANT_KIND).parent.chmod(0)
    assert uploads.sweep_private() == [ASSISTANT]
    assert (root / uploads.PRIVATE_MARKER).is_file()
    new = uploads.store_private(SUCCESSOR, entry_for(SUCCESSOR, registry.ASSISTANT_KIND), "new")
    assert host_path(new, registry.ASSISTANT_KIND).read_text() == "new"


def test_a_root_without_its_marker_holding_anything_else_stays_foreign(platform_root):
    entry = entry_for(WORKER)
    stored = uploads.store_private(WORKER, entry, "x")
    root = uploads.session_upload_dir(WORKER) / uploads.PRIVATE_DIRNAME
    (root / uploads.PRIVATE_MARKER).unlink()
    (host_path(stored).parent / "notes.txt").write_text("kept")
    assert uploads.discard_private(WORKER) == 0
    with pytest.raises(uploads.UploadError, match="not made by this platform"):
        uploads.store_private(WORKER, entry, "y")
    assert (host_path(stored).parent / "notes.txt").read_text() == "kept"


def test_a_locked_session_directory_still_takes_sends_and_a_locked_root_is_still_cleaned(platform_root):
    entry = entry_for(WORKER)
    first = uploads.store_private(WORKER, entry, "first")
    host_path(first).parent.chmod(0)
    second = uploads.store_private(WORKER, entry, "second")
    assert host_path(second).read_text() == "second"
    root = uploads.session_upload_dir(WORKER) / uploads.PRIVATE_DIRNAME
    root.chmod(0)
    assert uploads.discard_private(WORKER) == 1
    assert [path.name for path in root.iterdir()] == [uploads.PRIVATE_MARKER]


def test_the_sweep_cleans_a_locked_root(platform_root):
    gone = "20261028-120000-5-4247"
    stale = uploads.store_private(gone, entry_for(gone), "x")
    (uploads.session_upload_dir(gone) / uploads.PRIVATE_DIRNAME).chmod(0)
    assert uploads.sweep_private() == [gone]
    assert not host_path(stale).exists()


# --- the routes ----------------------------------------------------------------

def private(client, session_id, body, headers=None):
    return client.post(f"/api/sessions/{session_id}/private", json=body, headers=headers or auth())


def test_the_route_stores_and_answers_with_the_reference_only(client, platform_root, marker):
    session_id = register(WORKER)
    reply = private(client, session_id, {"text": f"token={marker}"})
    assert reply.status_code == 201, reply.text
    body = reply.json()
    assert set(body) == {"session", "name", "container_path", "reference"}
    assert marker not in reply.text
    assert host_path(uploads.StoredPrivate(session_id, body["name"], body["container_path"])).read_text() == f"token={marker}"


@pytest.mark.parametrize("shape", [
    lambda m: {"text": ""}, lambda m: {"text": "  "}, lambda m: {"nottext": m},
    lambda m: m, lambda m: [m], lambda m: {"text": [m]}, lambda m: None,
])
def test_a_refusal_never_repeats_what_was_sent(client, platform_root, marker, shape):
    session_id = register(WORKER)
    reply = private(client, session_id, shape(marker))
    assert reply.status_code == 400, reply.text
    assert marker not in reply.text


def test_a_body_that_is_not_json_is_refused_without_echo(client, platform_root, marker):
    session_id = register(WORKER)
    reply = client.post(
        f"/api/sessions/{session_id}/private",
        content=f'{{"text": "{marker}'.encode(),
        headers={**auth(), "content-type": "application/json"},
    )
    assert reply.status_code in (400, 422), reply.text
    assert marker not in reply.text


def test_an_ended_session_or_one_without_a_store_sends_nothing(client, platform_root):
    dead = register("20261028-120000-3-4245", live=False)
    assert private(client, dead, {"text": "x"}).status_code == 410
    bare = register("20261028-120000-4-4246", mounted=False)
    assert private(client, bare, {"text": "x"}).status_code == 409
    assert private(client, "20261028-120000-9-4249", {"text": "x"}).status_code == 404
    assert not list(platform_root.rglob(f"{uploads.PRIVATE_DIRNAME}/*/*"))


def test_only_the_operator_can_send_privately(client, platform_root):
    session_id = register(WORKER)
    connector = cfg.mint_connector_credential("slack-bridge", "slack")
    assert private(client, session_id, {"text": "x"}, bearer(connector)).status_code == 403
    assert client.delete(
        f"/api/sessions/{session_id}/private/whatever.txt", headers=bearer(connector),
    ).status_code == 403


def test_the_delete_route_withdraws_an_undelivered_file(client, platform_root):
    session_id = register(WORKER)
    body = private(client, session_id, {"text": "x"}).json()
    reply = client.delete(f"/api/sessions/{session_id}/private/{body['name']}", headers=auth())
    assert reply.status_code == 200 and reply.json()["withdrawn"] is True
    assert not host_path(uploads.StoredPrivate(session_id, body["name"], body["container_path"])).exists()


# --- end to end ------------------------------------------------------------------

def test_end_to_end_the_marker_lives_only_in_the_private_file_then_nowhere(
    platform_root, tmp_path, marker, caplog,
):
    """Send privately, deliver the reference over the real bus, then end the session."""
    caplog.set_level(logging.DEBUG)
    plane = BusPlane(tmp_path)
    try:
        session_id = live_session(plane)
        directory = uploads.prepare_upload_dir(uploads.session_upload_dir(session_id))
        registry.update(session_id, uploads=uploads.registry_pointer(directory))
        client = TestClient(api.create_app(cfg.load(), SECRET))
        text = f"user: someone\npassword: {marker}\n"

        stored = private(client, session_id, {"text": text})
        assert stored.status_code == 201, stored.text
        reference = stored.json()["reference"]
        sent = client.post(
            f"/api/sessions/{session_id}/input",
            json={"data": reference, "append_newline": True, "sanitize": True},
            headers=auth(),
        )
        assert sent.status_code == 200, sent.text
        page = client.get(f"/api/sessions/{session_id}/comms", headers=auth()).json()
        assert [m["text"] for m in page["messages"]] == [reference]

        responses = stored.text + sent.text + repr(page)
        typed = b"".join(plane.sink)
        assert reference.encode() in typed
        assert marker not in responses and marker.encode() not in typed
        assert marker not in caplog.text
        holders = files_containing(tmp_path, marker)
        assert holders == [uploads.session_upload_dir(session_id) / uploads.PRIVATE_DIRNAME / session_id / stored.json()["name"]]

        spawn.record_exit(session_id, 0)
        assert files_containing(tmp_path, marker) == []
    finally:
        plane.stop()


def test_lmer_signal_refuses_to_attach_a_private_file(tmp_path, monkeypatch):
    store_dir = tmp_path / "uploads"
    secret_file = store_dir / uploads.PRIVATE_DIRNAME / WORKER / "20261028-120000-private.txt"
    secret_file.parent.mkdir(parents=True)
    secret_file.write_text("x")
    (tmp_path / "link.txt").symlink_to(secret_file)
    monkeypatch.setenv(signal_cli.UPLOADS_DIR_ENV, str(store_dir))
    for path in (secret_file, tmp_path / "link.txt"):
        with pytest.raises(signal_cli.ArgsError, match="private"):
            signal_cli._stage_attachments([str(path)])
    assert [p.name for p in store_dir.iterdir()] == [uploads.PRIVATE_DIRNAME]


def test_both_composers_carry_the_control_beside_attach():
    """The assistant drawer and a run's detail view both render ``Chat.vue``."""
    components = Path(__file__).resolve().parent.parent / "web" / "src" / "components"
    chat = (components / "Chat.vue").read_text(encoding="utf-8")
    row = chat[chat.index('<div class="send-row">'):]
    assert row.index(">attach</v-btn>") < row.index(">send privately</v-btn>") < row.index(">send</v-btn>")
    assert "<Chat" in (components / "AssistantChat.vue").read_text(encoding="utf-8")
    assert "<Chat" in (components / "RunDetail.vue").read_text(encoding="utf-8")
