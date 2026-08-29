"""The store behind chat file upload (issue #246).

What is pinned here is the half that decides whether a file is *taken at all*:
which store a session gets, what the bytes are allowed to be, and that a read
cannot reach past the session's own directory. The routes that call it are
``tests/test_platform_uploads_routes.py``.

The type check is the interesting one. An allowlist that trusted the browser's
declared MIME type or the filename's extension could be got past by renaming a
file, so what is asserted below is that a PNG called ``notes.txt`` is a PNG and
a refused type stays refused however it is spelled.
"""

import base64
import logging
import os
import stat

import pytest

from lmer_platform import config as cfg
from lmer_platform import registry, store, uploads
from tests.conftest import strip_lmer_env

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 32


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


def worker_entry(session_id="20260826-120000-1-4242", *, mounted=True):
    """A registry entry as the spawn writes one, with or without a store."""
    directory = uploads.session_upload_dir(session_id) if mounted else None
    if mounted:
        uploads.prepare_upload_dir(directory)
    return {
        "id": session_id,
        "kind": registry.WORKER_KIND,
        "uploads": uploads.registry_pointer(directory),
    }


def assistant_entry(session_id="20260826-120000-2-4243"):
    directory = uploads.prepare_upload_dir(uploads.assistant_upload_dir())
    return {
        "id": session_id,
        "kind": registry.ASSISTANT_KIND,
        "uploads": uploads.registry_pointer(directory),
    }


def encoded(raw):
    return base64.b64encode(raw).decode("ascii")


# --- which store, and how private -------------------------------------------

def test_a_worker_gets_its_own_store_and_uber_lmer_the_host_s(platform_root):
    """Clarifications 3 and 4: one is the session's, one outlives every
    incarnation. Same mechanism, and that is the point — the container path is
    one constant, so nothing downstream has to know which surface it is on."""
    worker = uploads.upload_dir_for("20260826-120000-1-4242", registry.WORKER_KIND)
    assistant = uploads.upload_dir_for("20260826-120000-2-4243", registry.ASSISTANT_KIND)
    assert worker == store.logs_dir() / "20260826-120000-1-4242.uploads"
    assert assistant == store.platform_dir() / uploads.ASSISTANT_DIRNAME
    assert worker != assistant


def test_an_unknown_kind_does_not_fall_back_to_the_shared_store(platform_root):
    """A store every incarnation reads is a thing to opt into. A session whose
    kind cannot be read gets a private one rather than the assistant's."""
    directory = uploads.upload_dir_for("20260826-120000-1-4242", "")
    assert directory == store.logs_dir() / "20260826-120000-1-4242.uploads"


def test_the_store_and_its_files_are_owner_only(platform_root):
    """An upload holds whatever was on the operator's screen. The directory is
    rw-mounted into a container, and the host may have other accounts on it."""
    entry = worker_entry()
    stored = uploads.store_upload(entry['id'], entry, "shot.png", encoded(PNG))
    directory = uploads.session_upload_dir(entry["id"])
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(stored.path.stat().st_mode) == 0o600


def test_an_unusable_store_is_reported_and_not_fatal(platform_root, monkeypatch, caplog):
    """Fail-soft like the ask channel: the session starts, and the refusal
    happens where an operator can see it rather than at spawn."""
    def refuse(_directory):
        raise OSError("read-only file system")

    monkeypatch.setattr(store, "ensure_state_dir", refuse)
    with caplog.at_level(logging.WARNING, logger="lmer_platform.uploads"):
        assert uploads.prepare_upload_dir(uploads.assistant_upload_dir()) is None
    assert any(
        "platform_upload_dir_unusable" in record.getMessage()
        for record in caplog.records
    )


def test_a_store_this_user_cannot_write_is_not_mounted(platform_root, monkeypatch, caplog):
    """Mounting one produces "every upload fails" at the far end of the feature
    instead of a warning here."""
    directory = uploads.assistant_upload_dir()
    store.ensure_state_dir(directory)
    monkeypatch.setattr(os, "access", lambda *_args, **_kw: False)
    with caplog.at_level(logging.WARNING, logger="lmer_platform.uploads"):
        assert uploads.prepare_upload_dir(directory) is None
    assert any(
        "platform_upload_store_unwritable" in record.getMessage()
        for record in caplog.records
    )


# --- a session that cannot receive one ---------------------------------------

def test_a_session_started_before_this_feature_is_refused_with_the_way_out(platform_root):
    """The store's presence on the host says nothing about the container that is
    already running, so the *spawn* records the mount and this reads it. The
    refusal names the restart, because no retry adds a mount to a live
    container."""
    entry = {"id": "20260826-120000-1-4242", "kind": registry.WORKER_KIND}
    with pytest.raises(uploads.StoreUnavailable) as refusal:
        uploads.store_upload(entry['id'], entry, "shot.png", encoded(PNG))
    assert refusal.value.status == 409
    assert "restart" in str(refusal.value).lower()


def test_the_assistant_store_existing_is_not_a_mount(platform_root):
    """The sharp version of the case above: uber lmer's store is per host, so it
    exists as soon as any spawn has made one — while the incarnation now running
    may predate the mount entirely."""
    uploads.prepare_upload_dir(uploads.assistant_upload_dir())
    entry = {"id": "20260826-120000-2-4243", "kind": registry.ASSISTANT_KIND}
    with pytest.raises(uploads.StoreUnavailable):
        uploads.store_upload(entry['id'], entry, "shot.png", encoded(PNG))


def test_a_store_deleted_under_a_live_session_is_refused(platform_root):
    """The entry says there is one; the host disagrees. Refused rather than
    recreated: the mount is gone with it, so a fresh directory would be one
    nothing in the container is looking at."""
    entry = worker_entry()
    uploads.session_upload_dir(entry["id"]).rmdir()
    with pytest.raises(uploads.StoreUnavailable):
        uploads.store_upload(entry['id'], entry, "shot.png", encoded(PNG))


# --- what the bytes are allowed to be ----------------------------------------

def test_the_bytes_decide_the_type_not_the_name(platform_root):
    """A PNG called notes.txt is a PNG, and it is stored under a name that says
    so — an allowlist a rename could defeat would not be a policy."""
    entry = worker_entry()
    stored = uploads.store_upload(entry['id'], entry, "notes.txt", encoded(PNG))
    assert stored.kind == "png"
    assert stored.content_type == "image/png"
    assert stored.name.endswith(".png")


def test_a_type_outside_the_allowlist_is_refused_by_content(platform_root):
    """png-only: a JPEG is refused however it is named, and the refusal says
    what is accepted."""
    entry = worker_entry()
    with pytest.raises(uploads.UploadTypeRefused) as refusal:
        uploads.store_upload(entry['id'], entry, "shot.png", encoded(JPEG), allowed=("png",))
    assert refusal.value.status == 415
    assert "png" in str(refusal.value)


def test_text_is_identified_by_decoding_rather_than_by_a_signature(platform_root):
    """The one type with no magic number. Decoding as UTF-8 is a measurement of
    the bytes, not a guess from the name."""
    entry = worker_entry()
    stored = uploads.store_upload(entry['id'], entry, "log.bin", encoded("héllo\n".encode("utf-8")))
    assert stored.kind == "txt"
    with pytest.raises(uploads.UploadTypeRefused):
        uploads.store_upload(entry['id'], entry, "log.txt", encoded(b"\xff\xfe\x00binary"))


def test_binary_that_happens_to_decode_is_not_text(platform_root):
    """``txt`` is on by default, so "it decoded as UTF-8" would make it an
    allowlist entry that takes arbitrary binary: a run of NUL bytes decodes
    perfectly. Control characters no text file carries are what rules it out."""
    entry = worker_entry()
    with pytest.raises(uploads.UploadTypeRefused):
        uploads.store_upload(entry["id"], entry, "blob.txt",
                             encoded(b"GIF89a" + b"\x00" * 32))
    stored = uploads.store_upload(entry["id"], entry, "notes.txt",
                                  encoded(b"line one\r\n\tline two\n"))
    assert stored.kind == "txt"


def test_a_signature_wins_over_the_text_fallback(platform_root):
    """Order matters in the other direction too: bytes that would also decode
    are still the type their signature names."""
    entry = worker_entry()
    stored = uploads.store_upload(entry['id'], entry, "x", encoded(b"GIF89a plain ascii"),
                                  allowed=("txt", "gif"))
    assert stored.kind == "gif"


def test_every_known_type_is_recognisable_from_its_own_bytes():
    """The list an operator may enable is exactly the list this module can
    check. A name on the allowlist that had to be taken on trust would put the
    sender back in charge of the policy."""
    for name, raw in (("png", PNG), ("jpeg", JPEG), ("gif", GIF), ("webp", WEBP),
                      ("pdf", b"%PDF-1.7\n\xff\xfe"), ("txt", b"plain")):
        assert uploads.sniff(raw, uploads.KNOWN_TYPES).name == name
    assert set(uploads.DEFAULT_TYPES) <= set(uploads.KNOWN_TYPES)


def test_an_empty_allowlist_refuses_everything_and_says_so(platform_root):
    """A host that takes no uploads is a valid configuration, and the refusal
    must not read like a corrupt file."""
    entry = worker_entry()
    with pytest.raises(uploads.UploadTypeRefused) as refusal:
        uploads.store_upload(entry['id'], entry, "shot.png", encoded(PNG), allowed=())
    assert "LMER_PLATFORM_UPLOAD_TYPES" in str(refusal.value)


# --- size --------------------------------------------------------------------

def test_an_over_cap_file_is_refused(platform_root):
    entry = worker_entry()
    with pytest.raises(uploads.UploadTooLarge) as refusal:
        uploads.store_upload(entry['id'], entry, "shot.png", encoded(PNG), max_bytes=8)
    assert refusal.value.status == 413


def test_an_enormous_payload_is_refused_before_it_is_decoded(platform_root, monkeypatch):
    """The check is on the encoded text, so a huge paste costs a length rather
    than the buffer it would decode into."""
    def fail(*_args, **_kwargs):
        raise AssertionError("decoded a payload that was already over the cap")

    monkeypatch.setattr(uploads.base64, "b64decode", fail)
    with pytest.raises(uploads.UploadTooLarge):
        uploads.decode_payload("A" * 20_000, 1024)


def test_a_wrapped_payload_is_a_file_not_garbage(platform_root):
    """``base64`` wraps its output at 76 columns by default, so an API caller
    piping a file through it means a file. Whitespace is the only thing forgiven
    — strict decoding otherwise, because a silently-dropped character is a
    shorter file rather than an error."""
    # Long enough to actually wrap at 76 columns, which the 40-byte PNG above is
    # not — the guard below is what caught that.
    big = PNG + b"\x00" * 400
    text = encoded(big)
    wrapped = "\n".join([text[i:i + 76] for i in range(0, len(text), 76)])
    assert "\n" in wrapped, "the fixture no longer exercises wrapping"
    assert uploads.decode_payload(wrapped, 4096) == big


def test_a_payload_that_is_not_base64_is_a_400_naming_the_field(platform_root):
    with pytest.raises(uploads.UploadRejected) as refusal:
        uploads.decode_payload("not base64!!", 1024)
    assert refusal.value.status == 400
    assert "data" in str(refusal.value)


def test_an_empty_file_is_refused(platform_root):
    with pytest.raises(uploads.UploadRejected):
        uploads.decode_payload(encoded(b""), 1024)


# --- names -------------------------------------------------------------------

def test_two_pastes_of_the_same_name_are_two_files(platform_root):
    """Overwriting would lose a file the session may not have read yet, and it
    is invisible from the sending end."""
    entry = worker_entry()
    first = uploads.store_upload(entry['id'], entry, "image.png", encoded(PNG))
    second = uploads.store_upload(entry['id'], entry, "image.png", encoded(PNG + b"x"))
    assert first.name != second.name
    assert first.path.exists() and second.path.exists()


def test_a_collision_keeps_the_shape_every_other_stored_name_has(platform_root):
    """Two uploads of one over-long name in the same second. The marker used to be
    appended to the whole name, which put it over the bound and gave it the
    extension twice — a name an operator reads and an agent types (!272 review)."""
    entry = worker_entry()
    first = uploads.store_upload(entry["id"], entry, "x" * 300 + ".png", encoded(PNG))
    second = uploads.store_upload(
        entry["id"], entry, "x" * 300 + ".png", encoded(PNG + b"different"),
    )
    assert first.name != second.name
    for stored in (first, second):
        assert len(stored.name) <= uploads.MAX_NAME_CHARS
        assert stored.name.count(".png") == 1
    assert first.path.read_bytes() != second.path.read_bytes()


def test_a_name_cannot_carry_a_path_out_of_the_store(platform_root):
    """The operator's filename reaches a shell in the agent's hands."""
    entry = worker_entry()
    stored = uploads.store_upload(entry['id'], entry, "../../etc/passwd.png", encoded(PNG))
    assert stored.path.parent == uploads.session_upload_dir(entry["id"])
    assert "/" not in stored.name and ".." not in stored.name


def test_a_stored_name_stays_short_enough_to_read(platform_root):
    entry = worker_entry()
    stored = uploads.store_upload(entry['id'], entry, "x" * 400 + ".png", encoded(PNG))
    assert len(stored.name) <= uploads.MAX_NAME_CHARS
    assert stored.name.endswith(".png")


# --- reading one back --------------------------------------------------------

def test_a_stored_file_reads_back_with_the_type_it_has_now(platform_root):
    """Re-sniffed rather than remembered: the session can replace the file
    through the mount, and the served Content-Type has to describe what is
    there."""
    entry = worker_entry()
    stored = uploads.store_upload(entry['id'], entry, "shot.png", encoded(PNG))
    handle, kind, name = uploads.open_stored(entry['id'], entry, stored.name)
    with handle:
        assert name == stored.name
        assert handle.read() == PNG, "the handle was not rewound after the sniff"
    assert kind.content_type == "image/png"
    assert kind.inline is True
    stored.path.write_bytes(b"now it is text")
    handle, kind, _name = uploads.open_stored(entry['id'], entry, stored.name)
    handle.close()
    assert kind.inline is False


def test_a_worker_s_uploads_outlive_its_registry_entry(platform_root):
    """A clean exit reaps the entry, and the conversation stays readable after
    it — a screenshot in that conversation has to stay readable with it."""
    entry = worker_entry()
    stored = uploads.store_upload(entry["id"], entry, "shot.png", encoded(PNG))
    handle, kind, name = uploads.open_stored(entry["id"], None, stored.name)
    with handle:
        assert handle.read() == PNG
    assert (name, kind.name) == (stored.name, "png")


def test_a_session_with_no_store_at_all_reads_as_not_found(platform_root):
    """Not a 409 on the read path: nothing is being handed to anybody, so the
    only question is whether the file is there."""
    with pytest.raises(uploads.UploadNotFound):
        uploads.open_stored("20260826-120000-3-4244", None, "shot.png")


# --- the container is on the other side of this mount ------------------------
#
# Both tests below are for the same shape of defect and were both live: a check
# made on one resolution of a name, and an action taken on another. The store is
# bind-mounted ``rw`` into a container an agent drives, so between any two
# resolutions that side can rename the file and leave a symlink at the name. The
# swaps are performed deterministically at the exact point the window opens
# rather than raced with threads, so a failure names the window rather than
# appearing one run in fifty.

def test_the_mode_is_applied_to_the_file_and_never_to_a_planted_link(
    platform_root, tmp_path, monkeypatch,
):
    """The write takes long enough for the container to swap the name, and the
    mode used to be applied by *path* afterwards — which set a daemon-owned file
    outside the store to 0600. Aimed at a directory, that takes the platform's
    state tree out (!272 review)."""
    entry = worker_entry()
    victim = tmp_path / "keyfile"
    victim.write_text("secret", encoding="utf-8")
    victim.chmod(0o644)
    store_dir = uploads.session_upload_dir(entry["id"])

    real_fdopen = os.fdopen

    def swapping_fdopen(descriptor, *args, **kwargs):
        handle = real_fdopen(descriptor, *args, **kwargs)
        # What the container can do while the bytes are being written: the name
        # this call is about now points outside the store.
        for existing in store_dir.iterdir():
            existing.rename(store_dir / "moved.png")
            os.symlink(victim, existing)
        return handle

    monkeypatch.setattr(uploads.os, "fdopen", swapping_fdopen)
    uploads.store_upload(entry["id"], entry, "shot.png", encoded(PNG))
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644, (
        "the mode was applied through the name, so the container chose the target"
    )


def test_a_symlink_swapped_in_after_the_listing_is_not_served(
    platform_root, tmp_path, monkeypatch,
):
    """The window the fixed-symlink test below cannot see: the entry was a
    regular file when the directory was listed, and is a link by the time the
    bytes are read. ``O_NOFOLLOW`` is what closes it, so the swap lands and the
    read still refuses."""
    entry = worker_entry()
    stored = uploads.store_upload(entry["id"], entry, "shot.png", encoded(PNG))
    secret = tmp_path / "platform-secret"
    secret.write_text("PLATFORM-SECRET", encoding="utf-8")

    real_scandir = os.scandir

    def swapping_scandir(directory):
        entries = list(real_scandir(directory))
        # Between the listing and the open — the whole of the old window.
        stored.path.rename(stored.path.parent / "moved.png")
        os.symlink(secret, stored.path)
        return entries

    monkeypatch.setattr(uploads.os, "scandir", swapping_scandir)
    with pytest.raises(uploads.UploadNotFound):
        uploads.open_stored(entry["id"], entry, stored.name)
    assert secret.read_text(encoding="utf-8") == "PLATFORM-SECRET", "unchanged"


def test_a_fifo_in_the_store_does_not_hold_the_request_thread(platform_root):
    """A named pipe planted in the store would otherwise block inside ``open``
    until something wrote to it — one wedged threadpool worker per read. Opened
    non-blocking and refused on what ``fstat`` says it is."""
    entry = worker_entry()
    fifo = uploads.session_upload_dir(entry["id"]) / "20260826-000000-pipe.png"
    os.mkfifo(fifo)
    with pytest.raises(uploads.UploadNotFound):
        uploads.open_stored(entry["id"], entry, fifo.name)


def test_one_signature_table_serves_the_check_and_the_read(platform_root):
    """The matcher used to be written twice over different byte windows, so a
    signature added past the head would be seen by one of them and an image would
    be served as text with nothing logged (!272 review)."""
    entry = worker_entry()
    for kind_name, raw in (("png", PNG), ("gif", GIF), ("webp", WEBP)):
        stored = uploads.store_upload(
            entry["id"], entry, f"x.{kind_name}", encoded(raw),
            allowed=("png", "gif", "webp"),
        )
        handle, kind, _name = uploads.open_stored(entry["id"], entry, stored.name)
        handle.close()
        assert (stored.kind, kind.name) == (kind_name, kind_name)


def test_a_name_the_store_does_not_hold_is_a_404(platform_root):
    entry = worker_entry()
    with pytest.raises(uploads.UploadNotFound) as refusal:
        uploads.open_stored(entry['id'], entry, "../../../etc/passwd")
    assert refusal.value.status == 404


def test_a_symlink_planted_in_the_store_is_not_served(platform_root, tmp_path):
    """The store is rw-mounted into a container an agent drives, so a link out
    of it is something that side can plant."""
    entry = worker_entry()
    secret = tmp_path / "secret"
    secret.write_text("token", encoding="utf-8")
    (uploads.session_upload_dir(entry["id"]) / "innocent.png").symlink_to(secret)
    with pytest.raises(uploads.UploadNotFound):
        uploads.open_stored(entry['id'], entry, "innocent.png")


def test_one_session_cannot_read_another_s_store(platform_root):
    """The name is matched against this session's own listing, so another
    session's file is simply not there to ask for."""
    mine = worker_entry("20260826-120000-1-4242")
    theirs = worker_entry("20260826-120000-9-9999")
    stored = uploads.store_upload(theirs['id'], theirs, "shot.png", encoded(PNG))
    with pytest.raises(uploads.UploadNotFound):
        uploads.open_stored(mine['id'], mine, stored.name)


# --- what the session is told ------------------------------------------------

def test_the_reference_line_names_the_container_path(platform_root):
    """The message the agent receives has to say where the file is *inside* the
    container: the host path it was written to means nothing there."""
    entry = worker_entry()
    stored = uploads.store_upload(entry['id'], entry, "shot.png", encoded(PNG))
    line = uploads.reference_line(stored)
    assert line.startswith(uploads.REFERENCE_PREFIX)
    assert f"{uploads.CONTAINER_UPLOADS_DIR}/{stored.name}" in line
    assert stored.to_dict()["reference"] == line


def test_the_mount_flag_and_the_variable_name_one_path(platform_root):
    """A mount at one path and a variable naming another is a session told to
    read an empty directory."""
    directory = uploads.prepare_upload_dir(uploads.assistant_upload_dir())
    flags = uploads.mount_flags(directory)
    assert flags == ["--mount-dir", f"{directory}:{uploads.CONTAINER_UPLOADS_DIR}:rw"]
    assert uploads.registry_pointer(directory) == {
        "path": str(directory), "container_path": uploads.CONTAINER_UPLOADS_DIR,
    }
    assert uploads.registry_pointer(None) == {}


# --- the two knobs -----------------------------------------------------------

def test_the_defaults_are_the_ones_the_operator_asked_for(platform_root):
    config = cfg.load()
    assert config.upload_types == ("png", "jpeg", "txt")
    assert config.upload_max_bytes == cfg.DEFAULT_UPLOAD_MAX_BYTES


def test_both_knobs_resolve_from_the_environment(platform_root, monkeypatch):
    monkeypatch.setenv(cfg.ENV_UPLOAD_TYPES, "PNG, txt")
    monkeypatch.setenv(cfg.ENV_UPLOAD_MAX_BYTES, "1024")
    config = cfg.load()
    assert config.upload_types == ("png", "txt")
    assert config.upload_max_bytes == 1024


def test_a_type_nobody_can_check_is_dropped_rather_than_taken_on_trust(
    platform_root, monkeypatch, caplog,
):
    """A daemon that will not boot cannot be reconfigured through its own UI, so
    a typo costs the name rather than the host — but it is never quietly widened
    into "everything"."""
    monkeypatch.setenv(cfg.ENV_UPLOAD_TYPES, "png, jpg, exe")
    with caplog.at_level(logging.WARNING, logger="lmer_platform.config"):
        config = cfg.load()
    assert config.upload_types == ("png",)
    assert any(
        "platform_upload_types_unknown" in record.getMessage()
        for record in caplog.records
    )


def test_the_type_list_can_be_emptied_to_take_no_uploads(platform_root):
    """A valid configuration, not a broken one: the refusal names the empty
    list rather than pretending the default is in force."""
    config = cfg.load(overrides={"upload_types": []})
    assert config.upload_types == ()


def test_both_knobs_resolve_fresh_on_every_read(platform_root, monkeypatch):
    """Read per request rather than per daemon start (!272 review): the config
    route accepts ``upload_max_bytes`` — it is in ``INT_SETTINGS`` — so a value it
    persists has to be one the next upload actually measures against."""
    store.write_json(cfg.config_path(), {
        "upload_max_bytes": 4096, "upload_types": ["gif"],
    })
    assert cfg.upload_settings()["max_bytes"].value == 4096
    assert cfg.upload_settings()["max_bytes"].source == "config.json"
    assert cfg.upload_types() == ("gif",)
    # And an export still shadows the file, which is the chain's one trap and why
    # the resolved setting carries the layer that decided it.
    monkeypatch.setenv(cfg.ENV_UPLOAD_MAX_BYTES, "2048")
    monkeypatch.setenv(cfg.ENV_UPLOAD_TYPES, "png")
    assert cfg.upload_settings()["max_bytes"].value == 2048
    assert cfg.upload_settings()["max_bytes"].source == "env"
    assert cfg.upload_types() == ("png",)


def test_an_empty_type_list_in_the_file_is_what_the_floor_advice_names(platform_root):
    """The refusal for a cap below one advises turning uploads off by emptying
    the type list. A blank env var reads as unset and falls through to the
    default, so the advice has to name the file — and be true (!272 review)."""
    reason = cfg.INT_SETTINGS["upload_max_bytes"].floor_reason
    assert "config.json" in reason
    store.write_json(cfg.config_path(), {"upload_types": []})
    assert cfg.upload_types() == ()


def test_the_upload_group_is_its_own_table(platform_root):
    """Beside the check-in and nudge groups rather than inside one of them: the
    two of those are read on the daemon's tick and this one on a request, and a
    settings surface that merged them would report one under the other's name."""
    assert set(cfg.UPLOAD_SETTING_KEYS) == {"max_bytes"}
    assert cfg.UPLOAD_SETTING_KEYS["max_bytes"][0] == "upload_max_bytes"
    assert not set(cfg.UPLOAD_SETTING_KEYS) & set(cfg.NUDGE_SETTING_KEYS)
    assert not set(cfg.UPLOAD_SETTING_KEYS) & set(cfg.CHECKIN_SETTING_KEYS)


def test_the_cap_is_bounded_because_the_payload_is_held_in_memory(
    platform_root, monkeypatch,
):
    monkeypatch.setenv(cfg.ENV_UPLOAD_MAX_BYTES, str(cfg.MAX_UPLOAD_MAX_BYTES * 2))
    assert cfg.load().upload_max_bytes == cfg.DEFAULT_UPLOAD_MAX_BYTES
