"""Tests for reading and writing a running session (issue #141, slice M2 / T16).

Three properties carry most of the weight here, because they are the ones that
would fail silently and look like something else:

- **Scrollback outlives the session.** A crashed or cleanly-exited session must
  still serve its log, so the read path is exercised with no registry entry at
  all — the state a clean exit leaves behind.
- **Bytes stay bytes.** The log is raw terminal output, so a chunk that splits a
  UTF-8 sequence has to survive the round trip. A test asserts on invalid UTF-8
  precisely because "it worked in my terminal" would not have caught it.
- **The control token never surfaces.** It is read from disk, put in a header,
  and must appear in no response and no log line.

The control plane is a real (tiny) HTTP server rather than a monkeypatched
function: the interesting part of the input path is that the bearer header
arrives, and stubbing out the transport is exactly what would stop testing that.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import queue
import socket as socket_mod
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from lmer_platform import api, registry, session_io, spawn
from lmer_platform import config as cfg
from lmer_platform import store
from tests.conftest import strip_lmer_env

SECRET = "test-secret-value"
CONTROL_TOKEN = "control-plane-bearer-for-tests"

#: A pid nothing can be running under, so an entry reads as crashed. Same value
#: the rest of the platform tests use.
DEAD_PID = 2**22


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    for name in ("LMER_WORK_REPO", cfg.ENV_BIND_ADDRESS, cfg.ENV_BIND_PORT):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _quick_poll(monkeypatch):
    """Shorten the follower's idle poll so live-byte tests finish immediately.

    Autouse rather than opt-in: a test that forgot it would still pass, just a
    fifth of a second slower each time, which is how a suite quietly gets slow.
    """
    monkeypatch.setattr(session_io, "POLL_INTERVAL_SECONDS", 0.01)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


@pytest.fixture
def config(platform_root):
    return cfg.load()


def make_client(config):
    """A client over the real routes, with a stub fleet view (no work repo)."""
    app = api.create_app(
        config, SECRET, state_builder=lambda config, force_pull=False: {}
    )
    return TestClient(app)


@pytest.fixture
def client(config):
    return make_client(config)


def bearer_header(token=SECRET):
    return {"Authorization": f"Bearer {token}"}


# --- a stand-in for a session's control plane -------------------------------

class _FakeControlHandler(BaseHTTPRequestHandler):
    """As much of the supervisor's control plane as this module needs."""

    protocol_version = "HTTP/1.1"

    def _reply(self, status, payload):
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
        """The read half — ``/healthz``, for the idle reading (T95).

        Same ``answers`` table the writes use, so a test says what the container
        replies by planting it rather than by subclassing. A path with no answer
        planted 404s, which is what an image that never had the route does.
        """
        plane = self.server.plane
        plane.calls.append({
            "path": self.path,
            "method": "GET",
            "authorization": self.headers.get("Authorization"),
        })
        status, payload = plane.answers.get(
            self.path, (404, {"detail": "no such route"})
        )
        self._reply(status, payload)

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = None

        plane = self.server.plane
        plane.calls.append({
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "body": body,
        })
        status, payload = plane.answers.get(self.path, (200, {"ok": True}))
        self._reply(status, payload)

    def log_message(self, *args):
        """Silence — the fake plane's access log is noise in test output."""


class FakeControlPlane:
    """A loopback HTTP server that records what the platform sent it."""

    def __init__(self):
        self.calls = []
        self.answers = {
            "/input": (200, {"bytes_written": 7}),
            "/resize": (200, {"rows": 24, "cols": 80}),
        }
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeControlHandler)
        self._server.plane = self
        self.port = self._server.server_address[1]
        # A short poll interval only so ``shutdown()`` returns promptly: the
        # default half-second wait per test adds up to most of the module.
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self._thread.start()

    def answer(self, path, status, payload):
        self.answers[path] = (status, payload)

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def control_plane():
    plane = FakeControlPlane()
    yield plane
    plane.stop()


# --- planting sessions ------------------------------------------------------

def write_log(session_id, data: bytes) -> Path:
    path = session_io.session_log_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def append_log(session_id, data: bytes) -> None:
    with session_io.session_log_path(session_id).open("ab") as handle:
        handle.write(data)


def write_container_log(session_id, data: bytes) -> Path:
    """Stand in for the container's supervisor writing the session's own log.

    Written through :func:`session_io.container_log_path` rather than to a path
    spelled out here, so a test cannot pass while the read path looks somewhere
    else — the same reason the spawn tests read the mount out of the argument.
    """
    path = session_io.container_log_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def mount_container_log_dir(session_id) -> Path:
    """Create the mounted directory and nothing in it — what an old image leaves.

    The platform makes this directory for every session it spawns; only a session
    whose image knows about the log puts a file in it.
    """
    directory = session_io.container_log_path(session_id).parent
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _unused_port() -> int:
    """A port with nothing listening on it.

    Bind, read the port the kernel picked, close. Racy in principle — something
    could claim it before the test connects — but that is the same trick
    tests/test_pipe.py uses for its dead-port case, and the failure mode is a
    connection refused rather than a wrong answer, which is what these tests want
    anyway: the point is "nobody is listening", not "this exact number".
    """
    with socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def plant_session(
    session_id,
    *,
    port=None,
    live=True,
    log=None,
    credential=CONTROL_TOKEN,
):
    """Register a session, optionally with a control plane and a log."""
    control = {}
    if port is not None:
        control = {
            "host": "127.0.0.1",
            "port": port,
            "token_ref": str(spawn.token_file_for(session_id)),
        }
    registry.register(
        session_id, pid=os.getpid() if live else DEAD_PID, control=control
    )
    if port is not None and credential is not None:
        spawn.token_file_for(session_id).write_text(credential, encoding="utf-8")
    if log is not None:
        write_log(session_id, log)


def next_frame(socket, timeout=5.0):
    """Read one websocket frame, failing the test rather than hanging.

    ``TestClient``'s receive blocks forever, so a regression in the follower
    would wedge the suite instead of reporting. The reader runs on a daemon
    thread so a timed-out one cannot keep the interpreter alive either.
    """
    box: queue.Queue = queue.Queue(maxsize=1)
    threading.Thread(
        target=lambda: box.put(socket.receive_json()), daemon=True
    ).start()
    try:
        return box.get(timeout=timeout)
    except queue.Empty:
        pytest.fail(f"no websocket frame arrived within {timeout}s")


def open_tty(client, session_id, *, offset=0, ticket=None):
    """Mint a ticket and open the socket, the way a browser client would."""
    if ticket is None:
        minted = client.post(
            f"/api/sessions/{session_id}/tty-ticket", headers=bearer_header()
        )
        assert minted.status_code == 200, minted.text
        ticket = minted.json()["ticket"]
    return client.websocket_connect(
        f"/api/sessions/{session_id}/tty?ticket={ticket}&offset={offset}"
    )


# --- reading the log -------------------------------------------------------

def test_log_reads_from_an_offset(platform_root):
    plant_session("s-1", log=b"0123456789")
    chunk = session_io.read_log("s-1", offset=4)

    assert chunk.data == b"456789"
    assert (chunk.offset, chunk.next_offset, chunk.size) == (4, 10, 10)


def test_negative_offset_reads_the_tail(platform_root):
    plant_session("s-1", log=b"0123456789")
    chunk = session_io.read_log("s-1", offset=-3)

    assert chunk.data == b"789"
    assert chunk.offset == 7, "a tail read must report where it landed"


def test_a_tail_longer_than_the_log_starts_at_the_beginning(platform_root):
    plant_session("s-1", log=b"short")
    assert session_io.read_log("s-1", offset=-9000).data == b"short"


def test_offset_past_the_end_is_empty_not_an_error(platform_root):
    plant_session("s-1", log=b"abc")
    chunk = session_io.read_log("s-1", offset=99)

    assert chunk.data == b""
    assert chunk.next_offset == 3, "a cursor past the end resolves to the end"


def test_a_truncated_log_resolves_to_its_new_end(platform_root):
    """Someone truncating the log must not leave a follower reading past it."""
    plant_session("s-1", log=b"0123456789")
    write_log("s-1", b"ab")

    chunk = session_io.read_log("s-1", offset=10)
    assert (chunk.data, chunk.next_offset, chunk.size) == (b"", 2, 2)


def test_a_registered_session_with_no_log_yet_reads_empty(platform_root):
    """The log file appears with the first drained byte, not at spawn."""
    plant_session("s-1")
    chunk = session_io.read_log("s-1")

    assert (chunk.data, chunk.size) == (b"", 0)


def test_the_limit_is_clamped(platform_root, monkeypatch):
    monkeypatch.setattr(session_io, "MAX_LOG_LIMIT", 4)
    plant_session("s-1", log=b"0123456789")

    assert len(session_io.read_log("s-1", limit=9000).data) == 4
    assert len(session_io.read_log("s-1", limit=0).data) == 1


def test_a_dead_session_log_is_still_readable(platform_root):
    """A clean exit removes the entry; the scrollback is the only record left."""
    write_log("s-gone", b"everything it did")
    chunk = session_io.read_log("s-gone")

    assert chunk.data == b"everything it did"
    assert session_io.require_session("s-gone") is None


def test_an_unknown_session_is_not_found(platform_root):
    with pytest.raises(session_io.SessionNotFound) as caught:
        session_io.read_log("s-never-existed")
    assert caught.value.status == 404


@pytest.mark.parametrize("session_id", ["..", ".", "", "-nope", "a b"])
def test_an_id_that_could_not_name_an_entry_is_refused(platform_root, session_id):
    """The id lands in a filename, so it gets the registry's rule, not a new one."""
    with pytest.raises(session_io.SessionNotFound):
        session_io.session_log_path(session_id)


def test_bytes_that_are_not_utf8_survive_the_round_trip(platform_root):
    raw = b"\x1b[31mred\x1b[0m \xff\xfe lone-surrogate \xed\xa0\x80 end"
    plant_session("s-1", log=raw)

    wire = session_io.read_log("s-1").to_dict()
    assert wire["encoding"] == "base64"
    assert base64.b64decode(wire["data"]) == raw


def test_a_split_utf8_sequence_is_not_corrupted(platform_root):
    """The reason nothing here decodes: a chunk boundary lands mid-character."""
    text = "héllo wörld".encode("utf-8")
    plant_session("s-1", log=text)

    halves = [
        session_io.read_log("s-1", offset=0, limit=2),
        session_io.read_log("s-1", offset=2),
    ]
    assert b"".join(chunk.data for chunk in halves) == text
    assert halves[0].data == b"h\xc3", "the split must be reported as raw bytes"


# --- choosing which of a session's two logs to serve (#150) ------------------
#
# A session can have its output on disk twice: the host-side PTY tee, written by a
# thread in the daemon, and the log the supervisor writes from inside the
# container. The second one is canonical *when it exists*, and the whole point of
# these tests is the "when": the writer ships in the session image, not in this
# daemon, so a fleet permanently contains workers that will never write it and
# every one of them has to keep behaving exactly as it did before the file was
# invented. Nothing here may come to depend on a version, only on the file.

def test_the_session_s_own_log_is_served_instead_of_the_host_tee(platform_root):
    plant_session("s-1", log=b"host tee bytes")
    write_container_log("s-1", b"in-container bytes")

    chunk = session_io.read_log("s-1")
    assert chunk.data == b"in-container bytes"
    assert chunk.source == session_io.LOG_SOURCE_CONTAINER
    assert chunk.to_dict()["source"] == session_io.LOG_SOURCE_CONTAINER


def test_a_session_with_no_log_of_its_own_reads_the_host_tee(platform_root):
    """The permanent case, not a transitional one: an older worker image."""
    plant_session("s-1", log=b"host tee bytes")

    chunk = session_io.read_log("s-1")
    assert chunk.data == b"host tee bytes"
    assert chunk.source == session_io.LOG_SOURCE_HOST


def test_a_mounted_directory_with_no_log_in_it_reads_the_host_tee(platform_root):
    """The mixed-fleet case exactly: the platform offered, the image did not answer.

    This is the one a version check would get wrong — the daemon is new enough to
    have made the directory, and the session's image is not new enough to write in
    it.
    """
    plant_session("s-1", log=b"host tee bytes")
    mount_container_log_dir("s-1")

    chunk = session_io.read_log("s-1")
    assert chunk.data == b"host tee bytes"
    assert chunk.source == session_io.LOG_SOURCE_HOST


def test_an_empty_log_of_its_own_does_not_blank_the_terminal(platform_root):
    """A file with nothing in it has recorded nothing, so the tee is still it.

    The window this covers is real and visible: the supervisor opens its log when
    the container starts, which is *after* host-side lmer has printed the pull and
    the clone into the tee. Serving the empty file there would show an operator a
    blank screen with the session's whole launch sitting in the other file.
    """
    plant_session("s-1", log=b"pulling the image\r\ncloning\r\n")
    write_container_log("s-1", b"")

    chunk = session_io.read_log("s-1")
    assert chunk.data == b"pulling the image\r\ncloning\r\n"
    assert chunk.source == session_io.LOG_SOURCE_HOST


def test_a_log_the_writer_abandoned_falls_back_to_the_complete_copy(platform_root):
    """``SessionLog`` unlinks on a failed write; the fallback is what that buys."""
    plant_session("s-1", log=b"host tee bytes")
    write_container_log("s-1", b"in-container bytes").unlink()

    chunk = session_io.read_log("s-1")
    assert chunk.data == b"host tee bytes"
    assert chunk.source == session_io.LOG_SOURCE_HOST


def test_a_session_known_only_by_its_own_log_is_still_a_session(platform_root):
    """Either log is evidence the session existed; a 404 would deny it happened."""
    write_container_log("s-1", b"in-container bytes")

    assert session_io.require_session("s-1") is None
    assert session_io.read_log("s-1").data == b"in-container bytes"


def test_the_offsets_belong_to_the_log_that_served_them(platform_root):
    """One offset space per read, and it is the canonical log's own byte space."""
    plant_session("s-1", log=b"0123456789" * 2)
    write_container_log("s-1", b"abcdef")

    chunk = session_io.read_log("s-1", offset=2)
    assert (chunk.data, chunk.offset, chunk.next_offset, chunk.size) == (
        b"cdef", 2, 6, 6,
    )
    assert session_io.read_log("s-1", offset=-2).data == b"ef", "tail of the same log"


def test_a_cursor_from_the_other_log_clamps_forward_rather_than_rewinding(
    platform_root,
):
    """The seam, and which way it is allowed to be wrong.

    A client that read the tee before the container had started hands back a cursor
    measured in that file. Clamping it to the end of the canonical log can skip a
    little; rewinding to serve from the start would re-render bytes the terminal
    already has, and a duplicated redraw corrupts an emulator's screen until the
    page is reloaded.
    """
    plant_session("s-1", log=b"host preamble that is much longer")
    write_container_log("s-1", b"harness")

    chunk = session_io.read_log("s-1", offset=len(b"host preamble that is much longer"))
    assert chunk.data == b"", "no bytes the client already rendered are repeated"
    assert chunk.next_offset == 7, "the cursor continues from the canonical end"


def test_a_follower_keeps_the_source_it_started_with(platform_root):
    """Resolved once per stream: the cursor it hands out has to keep its meaning.

    A log of its own appearing mid-stream must not silently redefine the offsets an
    attached terminal is tracking — it is picked up by the next client instead, and
    nothing is lost meanwhile because the tee is alive for as long as this daemon
    is.
    """
    plant_session("s-1", log=b"host ", live=False)

    async def drive():
        chunks = []
        stream = session_io.follow_log("s-1", poll=0.01)
        chunks.append(await stream.__anext__())
        # The upgrade lands between two reads of a live stream.
        write_container_log("s-1", b"in-container bytes")
        append_log("s-1", b"tee ")
        chunks.append(await stream.__anext__())
        await stream.aclose()
        return chunks

    first, second = asyncio.run(drive())
    assert (first.data, first.source) == (b"host ", session_io.LOG_SOURCE_HOST)
    assert (second.data, second.source) == (b"tee ", session_io.LOG_SOURCE_HOST)


def test_a_new_follower_picks_up_the_session_s_own_log(platform_root):
    """The other half of the above: the next client is served the better source."""
    plant_session("s-1", log=b"host tee bytes", live=False)
    write_container_log("s-1", b"in-container bytes")

    async def drive():
        stream = session_io.follow_log("s-1", poll=0.01)
        chunk = await stream.__anext__()
        await stream.aclose()
        return chunk

    chunk = asyncio.run(drive())
    assert chunk.data == b"in-container bytes"
    assert chunk.source == session_io.LOG_SOURCE_CONTAINER


def test_the_log_route_says_which_source_it_served(client, platform_root):
    """An operator reading the JSON can tell a self-recording session from a teed one."""
    plant_session("s-1", log=b"host tee bytes")
    write_container_log("s-1", b"in-container bytes")

    response = client.get("/api/sessions/s-1/log", headers=bearer_header())
    assert response.status_code == 200, response.text
    body = response.json()
    assert base64.b64decode(body["data"]) == b"in-container bytes"
    assert body["source"] == session_io.LOG_SOURCE_CONTAINER


def test_the_tty_socket_streams_the_session_s_own_log(client, platform_root):
    plant_session("s-1", log=b"host tee bytes")
    write_container_log("s-1", b"in-container bytes")

    with open_tty(client, "s-1") as socket:
        assert next_frame(socket)["type"] == "open"
        frame = next_frame(socket)
    assert frame["type"] == "data"
    assert base64.b64decode(frame["data"]) == b"in-container bytes"
    assert frame["source"] == session_io.LOG_SOURCE_CONTAINER


# --- reading the log that is *not* canonical (#141 T79) ----------------------
#
# "Not merged" left something unreachable. Once a session writes its own log,
# every read of the canonical source starts at the harness's first byte — and the
# pull, the clone and lmer's own announce lines happened before that file
# existed, in the tee. Paging back through the canonical log stops at its origin
# and calls that the beginning of the record, which for those sessions it is not.
#
# So a reader may name a log. What these pin is the narrowness of that: it
# selects a file, it never stitches two together, and the chunk still says which
# offset space the caller is now holding a number in.

def test_a_named_source_reads_that_log_and_not_the_canonical_one(platform_root):
    plant_session("s-1", log=b"pulling the image\r\ncloning\r\n")
    write_container_log("s-1", b"in-container bytes")

    chunk = session_io.read_log("s-1", source=session_io.LOG_SOURCE_HOST)
    assert chunk.data == b"pulling the image\r\ncloning\r\n"
    assert chunk.source == session_io.LOG_SOURCE_HOST, (
        "the chunk must name the file it served, or a caller cannot tell which "
        "offset space its cursor is in"
    )
    assert session_io.read_log("s-1").source == session_io.LOG_SOURCE_CONTAINER, (
        "naming a source once must not change what the log of record is"
    )


def test_a_named_source_can_also_ask_for_the_session_s_own_log(platform_root):
    """Both names resolve, so the parameter is a selector and not a "use the tee" flag."""
    plant_session("s-1", log=b"host tee bytes")
    write_container_log("s-1", b"in-container bytes")

    chunk = session_io.read_log("s-1", source=session_io.LOG_SOURCE_CONTAINER)
    assert chunk.data == b"in-container bytes"
    assert chunk.source == session_io.LOG_SOURCE_CONTAINER


def test_the_offsets_of_a_named_read_are_that_file_s_own(platform_root):
    """One file per read, in its own byte space — the whole reason nothing merges.

    The tee here is longer than the log of record, so an offset that means one
    thing in it means something else in the other: exactly the confusion a shared
    cursor would cause, and the reason a named read is a second *view* rather than
    a second cursor.
    """
    plant_session("s-1", log=b"0123456789")
    write_container_log("s-1", b"abc")

    chunk = session_io.read_log("s-1", offset=4, source=session_io.LOG_SOURCE_HOST)
    assert (chunk.data, chunk.offset, chunk.next_offset, chunk.size) == (
        b"456789", 4, 10, 10,
    )


def test_a_named_log_that_was_never_written_reads_empty(platform_root):
    """Nothing there is a truthful answer, not a 404 on the session.

    A session known only by its own log has no tee — the daemon that would have
    written one is not the one being asked. The view that offers "how this was
    launched" gets an empty read and can say so; an error would read as the
    session having gone missing.
    """
    write_container_log("s-1", b"in-container bytes")

    chunk = session_io.read_log("s-1", source=session_io.LOG_SOURCE_HOST)
    assert (chunk.data, chunk.size) == (b"", 0)
    assert chunk.source == session_io.LOG_SOURCE_HOST


def test_a_source_that_names_neither_log_is_not_found(platform_root):
    """404, because what was named is a log this session does not have.

    A path is never built from the name: :func:`named_log` matches the two
    constants and refuses everything else, so the parameter cannot be steered at
    a file of the caller's choosing.
    """
    plant_session("s-1", log=b"host tee bytes")

    with pytest.raises(session_io.SessionNotFound):
        session_io.read_log("s-1", source="../../etc/passwd")
    with pytest.raises(session_io.SessionNotFound):
        session_io.read_log("s-1", source="")


def test_the_log_route_serves_a_named_source(client, platform_root):
    """The terminal's launch view, end to end: the head of the other file."""
    plant_session("s-1", log=b"pulling the image\r\ncloning\r\n")
    write_container_log("s-1", b"in-container bytes")

    response = client.get(
        "/api/sessions/s-1/log",
        params={"offset": 0, "limit": 4096, "source": session_io.LOG_SOURCE_HOST},
        headers=bearer_header(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert base64.b64decode(body["data"]) == b"pulling the image\r\ncloning\r\n"
    assert body["source"] == session_io.LOG_SOURCE_HOST

    # Unasked, the route is exactly what it was: the log of record.
    plain = client.get("/api/sessions/s-1/log", headers=bearer_header()).json()
    assert plain["source"] == session_io.LOG_SOURCE_CONTAINER


def test_the_log_route_refuses_an_unknown_source(client, platform_root):
    plant_session("s-1", log=b"host tee bytes")

    response = client.get(
        "/api/sessions/s-1/log", params={"source": "somewhere-else"},
        headers=bearer_header(),
    )
    assert response.status_code == 404, response.text


# --- resolving the control plane -------------------------------------------

def test_a_session_with_no_control_block_says_so(platform_root):
    plant_session("s-1", log=b"")

    with pytest.raises(session_io.ControlUnavailable) as caught:
        session_io.send_input("s-1", "hello")

    assert caught.value.status == 409
    assert "--fastapi" in str(caught.value)


def test_an_exited_session_cannot_be_written_to(platform_root):
    write_log("s-gone", b"scrollback")

    with pytest.raises(session_io.ControlUnavailable) as caught:
        session_io.send_input("s-gone", "hello")
    assert "has exited" in str(caught.value)


def test_a_crashed_session_cannot_be_written_to(platform_root, control_plane):
    plant_session("s-dead", port=control_plane.port, live=False)

    with pytest.raises(session_io.ControlUnavailable) as caught:
        session_io.send_input("s-dead", "hello")

    assert "not running" in str(caught.value)
    assert control_plane.calls == [], "a dead session must not be dialled"


def test_a_missing_control_token_is_a_clear_error(platform_root, control_plane):
    plant_session("s-1", port=control_plane.port, credential=None)

    with pytest.raises(session_io.ControlUnavailable) as caught:
        session_io.send_input("s-1", "hello")
    assert "token" in str(caught.value)


def test_an_unreachable_control_plane_is_a_gateway_error(platform_root):
    plane = FakeControlPlane()
    port = plane.port
    plane.stop()
    plant_session("s-1", port=port)

    with pytest.raises(session_io.ControlPlaneError) as caught:
        session_io.send_input("s-1", "hello")
    assert caught.value.status == 502


def test_a_refused_input_is_reported_as_a_failure(platform_root, control_plane):
    control_plane.answer("/input", 401, {"detail": "invalid bearer token"})
    plant_session("s-1", port=control_plane.port)

    with pytest.raises(session_io.ControlPlaneError) as caught:
        session_io.send_input("s-1", "hello")
    assert "invalid bearer token" in str(caught.value)


def test_a_control_plane_that_echoes_the_token_is_scrubbed(
    platform_root, control_plane
):
    """The upstream detail is relayed to a browser, so it cannot carry the token."""
    control_plane.answer(
        "/input", 400, {"detail": f"bad header: Bearer {CONTROL_TOKEN}"}
    )
    plant_session("s-1", port=control_plane.port)

    with pytest.raises(session_io.ControlPlaneError) as caught:
        session_io.send_input("s-1", "hello")

    assert CONTROL_TOKEN not in str(caught.value)
    assert "<redacted>" in str(caught.value)


def test_the_endpoint_keeps_the_token_out_of_its_repr(platform_root, control_plane):
    plant_session("s-1", port=control_plane.port)
    endpoint = session_io.control_endpoint("s-1")

    assert CONTROL_TOKEN not in repr(endpoint)
    assert endpoint.auth_headers() == {"Authorization": f"Bearer {CONTROL_TOKEN}"}


# --- writing input ---------------------------------------------------------

def test_input_arrives_with_the_session_bearer_token(platform_root, control_plane):
    plant_session("s-1", port=control_plane.port)

    reply = session_io.send_input("s-1", "the answer", append_newline=True)

    assert reply == {"bytes_written": 7}
    assert control_plane.calls == [{
        "path": "/input",
        "authorization": f"Bearer {CONTROL_TOKEN}",
        "body": {"data": "the answer", "append_newline": True},
    }]


def test_append_newline_defaults_off(platform_root, control_plane):
    """Matching the control plane: a caller that means Enter says so."""
    plant_session("s-1", port=control_plane.port)
    session_io.send_input("s-1", "no enter")

    assert control_plane.calls[0]["body"]["append_newline"] is False


def test_the_input_payload_is_never_logged(platform_root, control_plane, caplog):
    plant_session("s-1", port=control_plane.port)
    caplog.set_level("DEBUG")

    session_io.send_input(
        "s-1", "hunter2-is-what-the-operator-pasted", append_newline=True
    )

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "platform_session_input" in logged
    assert "hunter2" not in logged
    assert CONTROL_TOKEN not in logged


def test_non_string_input_is_refused_before_any_call(platform_root, control_plane):
    plant_session("s-1", port=control_plane.port)

    with pytest.raises(session_io.SessionIOError):
        session_io.send_input("s-1", 42)
    assert control_plane.calls == []


def test_input_attempts_are_logged_and_land_in_the_events_log(
    platform_root, control_plane, caplog
):
    """Endpoint, status, length and an HMAC — never the payload (#197).

    The write path used to leave no queryable trace: a payload accepted by
    ``/input`` but never submitted existed nowhere afterwards. What makes the
    record safe to keep durable is that it is an HMAC under the session's
    control token, not a bare hash — a short answer's raw SHA-256 is a
    dictionary lookup, i.e. content.
    """
    caplog.set_level("INFO")
    plant_session("s-1", port=control_plane.port)

    session_io.send_input("s-1", "the answer", append_newline=True)

    raw_sha = hashlib.sha256(b"the answer").hexdigest()
    keyed = hmac.new(
        CONTROL_TOKEN.encode(), b"the answer", hashlib.sha256
    ).hexdigest()
    logged = "\n".join(
        record.getMessage() for record in caplog.records
        if "platform_session_input" in record.getMessage()
    )
    assert f"endpoint=127.0.0.1:{control_plane.port}" in logged
    assert "status=200" in logged
    assert keyed in logged
    assert raw_sha not in logged, (
        "the raw payload hash is invertible for short payloads and must "
        "never be recorded"
    )
    assert "the answer" not in logged

    events = [e for e in store.read_events() if e["type"] == "session_input"]
    assert events, "the attempt must land in the events log"
    data = events[-1]["data"]
    assert data["session"] == "s-1"
    assert data["payload_hmac"] == keyed
    assert data["length"] == len(b"the answer")
    assert data["receipt_match"] is None, (
        "the fake control plane sent no receipt, and that must be recorded "
        "as unverified rather than as a match"
    )
    serialized = json.dumps(events[-1])
    assert "the answer" not in serialized
    assert raw_sha not in serialized


def test_keystroke_writes_are_not_durably_recorded(
    platform_root, control_plane, caplog
):
    """``append_newline=False`` is the web terminal's per-keystroke path.

    One durable event per typed character would grow the never-pruned events
    log without bound and reconstruct typed input character by character —
    including at hidden prompts, which never echo into the PTY log. The
    delivery-forensics contract covers messages; keystrokes get a debug line.
    """
    caplog.set_level("DEBUG")
    plant_session("s-1", port=control_plane.port)

    session_io.send_input("s-1", "y")

    assert not [
        e for e in store.read_events() if e["type"] == "session_input"
    ], "a keystroke write must not land in the durable events log"
    info_and_up = [
        record for record in caplog.records
        if record.levelname != "DEBUG"
        and "platform_session" in record.getMessage()
    ]
    assert not info_and_up, "keystrokes log at DEBUG only"
    debug = "\n".join(
        record.getMessage() for record in caplog.records
        if "platform_session_keystroke" in record.getMessage()
    )
    assert "status=200" in debug
    assert hashlib.sha256(b"y").hexdigest() not in debug


def test_an_unanswered_input_attempt_is_still_recorded(platform_root):
    """Transport failures are attempts too — the class that needs forensics.

    The first write against a starting session routinely dies as a connection
    reset; a log in which that leaves no record reads as "no attempt was
    made", which is exactly the #197 silence the events log exists to remove.
    """
    plane = FakeControlPlane()
    port = plane.port
    plane.stop()
    plant_session("s-1", port=port)

    with pytest.raises(session_io.ControlPlaneError):
        session_io.send_input("s-1", "the answer", append_newline=True)

    events = [e for e in store.read_events() if e["type"] == "session_input"]
    assert events, "an attempt that got no HTTP answer must still be recorded"
    data = events[-1]["data"]
    assert data["status"] is None
    assert data["error"], "the record must say what happened instead"
    assert data["endpoint"] == f"127.0.0.1:{port}"


def test_an_unresolvable_input_attempt_is_still_recorded(platform_root):
    """Endpoint resolution failing is the earliest attempt outcome there is."""
    plant_session("s-dead", live=False)

    with pytest.raises(session_io.ControlUnavailable):
        session_io.send_input("s-dead", "the answer", append_newline=True)

    events = [e for e in store.read_events() if e["type"] == "session_input"]
    assert events
    data = events[-1]["data"]
    assert data["endpoint"] is None
    assert data["status"] is None
    assert "not running" in data["error"]


def test_a_matching_input_receipt_passes_verification(
    platform_root, control_plane
):
    """A real receipt, message path: the event's verdict is True — and a
    verdict is all it is. Recording the supervisor's raw receipt hash would
    reintroduce the short-payload inversion the HMAC exists to close, through
    the other field; this is the guard that keeps ``receipt_match`` a boolean.
    """
    sent_sha = hashlib.sha256(b"hi!").hexdigest()
    control_plane.answer(
        "/input",
        200,
        {"bytes_written": 3, "payload_sha256": sent_sha, "payload_length": 3},
    )
    plant_session("s-1", port=control_plane.port)

    reply = session_io.send_input("s-1", "hi!", append_newline=True)

    assert reply["payload_sha256"] == sent_sha
    events = [e for e in store.read_events() if e["type"] == "session_input"]
    assert events[-1]["data"]["receipt_match"] is True
    assert sent_sha not in json.dumps(events[-1]), (
        "the raw receipt hash must never be durably recorded — the event "
        "stores a verdict"
    )


def test_a_mismatched_input_receipt_is_loud(platform_root, control_plane):
    """A control plane acknowledging different bytes must not look like a 200.

    Delivery was proven byte-perfect while diagnosing #236, so this should
    never fire — but "we believe the wire is clean" only became a fact when the
    receipt existed to check, and a mismatch discovered by the receiver acting
    on corrupt instructions is the worst possible way to learn of it.

    Loud, but not a lie: the 200 means the bytes were already typed into the
    session, and an error that reads as "not sent" teaches the operator to
    retype — delivering it twice. The message has to carry both halves.
    """
    control_plane.answer(
        "/input",
        200,
        {"bytes_written": 3, "payload_sha256": "not-what-was-sent"},
    )
    plant_session("s-1", port=control_plane.port)

    with pytest.raises(session_io.ControlPlaneError) as caught:
        session_io.send_input("s-1", "hi!", append_newline=True)
    assert "different bytes" in str(caught.value)
    assert "WAS typed into the session" in str(caught.value), (
        "the error must not read as a refusal — the write already happened"
    )
    events = [e for e in store.read_events() if e["type"] == "session_input"]
    assert events[-1]["data"]["receipt_match"] is False, (
        "the durable record must carry the mismatch verdict"
    )
    serialized = json.dumps(events[-1])
    assert hashlib.sha256(b"hi!").hexdigest() not in serialized
    assert "not-what-was-sent" not in serialized


# --- resizing (best-effort) ------------------------------------------------

def test_resize_reaches_the_control_plane(platform_root, control_plane):
    plant_session("s-1", port=control_plane.port)

    report = session_io.apply_resize("s-1", 40, 120)

    assert report.applied is True
    assert control_plane.calls[0]["body"] == {"rows": 40, "cols": 120}


@pytest.mark.parametrize("rows, cols", [(1, 120), (40, 2), (1, 1), (4, 19)])
def test_a_sliver_resize_is_refused_before_the_control_plane(
    platform_root, control_plane, rows, cols
):
    """No real terminal is 1-2 columns wide — that reading is a layout artifact.

    A client fitting against a mid-animation sliver once wrote such a value to a
    live PTY and reflowed its TUI to one character per line for every watcher,
    which is why the refusal happens here, in the one writer all clients share,
    and happens BEFORE the control plane is touched (the write must not depend
    on which client proposed it).
    """
    plant_session("s-1", port=control_plane.port)

    report = session_io.apply_resize("s-1", rows, cols)

    assert report.applied is False
    assert report.event == "resize_refused"
    assert f"{cols}x{rows}" in report.message
    assert control_plane.calls == []


def test_the_narrowest_real_screen_still_resizes(platform_root, control_plane):
    """The floor sits below any deliberate size: a 20x5 terminal goes through."""
    plant_session("s-1", port=control_plane.port)

    report = session_io.apply_resize("s-1", 5, 20)

    assert report.applied is True
    assert control_plane.calls[0]["body"] == {"rows": 5, "cols": 20}


@pytest.mark.parametrize(
    "status, detail, says",
    [
        (404, "Not Found", "does not serve /resize"),
        (503, "resize unavailable: this supervisor was started without PTY "
              "resize support", "cannot be resized"),
    ],
)
def test_an_unsupported_resize_is_tolerated(
    platform_root, control_plane, status, detail, says
):
    """404 = whatever answers that port has no /resize route (#236),
    503 = no PTY hook. Both mean carry on."""
    control_plane.answer("/resize", status, {"detail": detail})
    plant_session("s-1", port=control_plane.port)

    report = session_io.apply_resize("s-1", 24, 80)

    assert report.applied is False
    assert report.event == "resize_unsupported"
    assert report.status == status
    assert says in report.message


def test_the_resize_404_message_states_the_observation_not_a_diagnosis(
    platform_root, control_plane
):
    """A 404 says a route is unserved — it does not say why.

    #236 twice over: the old message asserted "the image predates the route"
    and was wrong (the supervisor had imported a stale checkout), and the
    first replacement asserted the stale-supervisor cause instead — equally
    unknowable from a 404, since a re-bound port produces the identical
    answer. This message sticks in the web client as a permanent notice, so a
    wrong cause sends the operator hunting the wrong thing for days. The
    message may name the endpoint and the observation; any causal word is a
    regression.
    """
    control_plane.answer("/resize", 404, {"detail": "Not Found"})
    plant_session("s-1", port=control_plane.port)

    report = session_io.apply_resize("s-1", 24, 80)

    assert "does not serve /resize" in report.message
    assert f"127.0.0.1:{control_plane.port}" in report.message, (
        "the observation includes WHERE it was made"
    )
    for diagnosis in ("image", "predates", "stale", "supervisor is running"):
        assert diagnosis not in report.message, (
            f"message asserts a cause ({diagnosis!r}) that a 404 cannot "
            "establish"
        )


def test_an_unanswered_resize_attempt_is_still_recorded(platform_root):
    """Same contract as input: no HTTP answer is still an attempt (#197)."""
    plane = FakeControlPlane()
    port = plane.port
    plane.stop()
    plant_session("s-1", port=port)

    with pytest.raises(session_io.ControlPlaneError):
        session_io.apply_resize("s-1", 24, 80)

    events = [e for e in store.read_events() if e["type"] == "session_resize"]
    assert events, "an attempt that got no HTTP answer must still be recorded"
    data = events[-1]["data"]
    assert data["status"] is None
    assert data["error"]
    assert data["rows"] == 24 and data["cols"] == 80


def test_resize_attempts_are_logged_with_endpoint_and_status(
    platform_root, control_plane, caplog
):
    """Every attempt, at INFO, naming the endpoint the daemon actually dialed.

    #236's 404 went unexplained partly because the only record was a debug
    line naming neither. The same facts land in the platform's events log so
    delivery questions are answerable after the fact, not just while tailing.
    """
    caplog.set_level("INFO")
    plant_session("s-1", port=control_plane.port)

    session_io.apply_resize("s-1", 40, 120)

    logged = "\n".join(
        record.getMessage() for record in caplog.records
        if "platform_session_resize" in record.getMessage()
    )
    assert f"endpoint=127.0.0.1:{control_plane.port}" in logged
    assert "status=200" in logged

    events = [e for e in store.read_events() if e["type"] == "session_resize"]
    assert events, "the attempt must land in the events log"
    assert events[-1]["data"]["session"] == "s-1"
    assert events[-1]["data"]["status"] == 200
    assert events[-1]["data"]["endpoint"] == f"127.0.0.1:{control_plane.port}"


def test_a_failed_resize_is_surfaced_as_a_problem(platform_root, control_plane):
    """500 means the PTY is gone — the session is ending, and that is news."""
    control_plane.answer("/resize", 500, {"detail": "cannot set window size"})
    plant_session("s-1", port=control_plane.port)

    report = session_io.apply_resize("s-1", 24, 80)

    assert report.applied is False
    assert report.event == "resize_failed"
    assert "ending" in report.message
    # An HTTP refusal is an answered attempt whose reason must not exist only
    # in an exception string nothing writes down: the upstream detail rides
    # the record beside the status.
    events = [e for e in store.read_events() if e["type"] == "session_resize"]
    assert events[-1]["data"]["status"] == 500
    assert "cannot set window size" in events[-1]["data"]["error"]


def test_resize_on_an_unreachable_session_still_raises(platform_root):
    """Reachability is one condition for input and resize, handled in one place."""
    write_log("s-gone", b"")

    with pytest.raises(session_io.ControlUnavailable):
        session_io.apply_resize("s-gone", 24, 80)


# --- following the log -----------------------------------------------------

async def test_follow_yields_the_backlog_then_live_bytes(platform_root):
    plant_session("s-1", log=b"backlog")
    stream = session_io.follow_log("s-1", offset=0, poll=0.01)

    first = await asyncio.wait_for(anext(stream), timeout=5)
    assert first.data == b"backlog"

    append_log("s-1", b"...and more")
    second = await asyncio.wait_for(anext(stream), timeout=5)
    assert second.data == b"...and more"
    assert second.offset == 7, "the follower must resume where it stopped"

    await stream.aclose()


async def test_follow_ends_once_the_session_is_gone(platform_root):
    """Otherwise an attached client watches a silent socket forever."""
    write_log("s-gone", b"final output")

    async def collect():
        return [chunk async for chunk in session_io.follow_log("s-gone", poll=0.01)]

    chunks = await asyncio.wait_for(collect(), timeout=5)
    assert b"".join(chunk.data for chunk in chunks) == b"final output"


async def test_follow_starts_at_the_tail_by_default(platform_root):
    plant_session("s-1", log=b"0123456789")
    stream = session_io.follow_log("s-1", offset=-4, poll=0.01)

    first = await asyncio.wait_for(anext(stream), timeout=5)
    assert first.data == b"6789"
    await stream.aclose()


# --- tickets ---------------------------------------------------------------

def test_a_ticket_is_single_use():
    tickets = session_io.TicketStore()
    ticket = tickets.mint("s-1")

    assert tickets.redeem(ticket, "s-1") is True
    assert tickets.redeem(ticket, "s-1") is False


def test_a_ticket_is_bound_to_one_session():
    tickets = session_io.TicketStore()
    ticket = tickets.mint("s-1")

    assert tickets.redeem(ticket, "s-2") is False
    assert tickets.redeem(ticket, "s-1") is False, "a mismatch burns the ticket"


def test_a_ticket_expires():
    tickets = session_io.TicketStore(ttl=0.02)
    ticket = tickets.mint("s-1")
    time.sleep(0.05)

    assert tickets.redeem(ticket, "s-1") is False
    assert tickets.live_count() == 0


@pytest.mark.parametrize("presented", ["", "not-a-ticket", "x" * 43])
def test_junk_is_not_a_ticket(presented):
    tickets = session_io.TicketStore()
    tickets.mint("s-1")
    assert tickets.redeem(presented, "s-1") is False


def test_unredeemed_tickets_do_not_grow_without_bound(caplog):
    tickets = session_io.TicketStore(capacity=2)
    for _ in range(5):
        tickets.mint("s-1")

    assert tickets.live_count() == 2
    assert any("platform_tty_ticket_evicted" in r.message for r in caplog.records)


def test_the_store_holds_no_plaintext_ticket():
    """Keyed by digest: the daemon has no reason to keep the plaintext."""
    tickets = session_io.TicketStore()
    ticket = tickets.mint("s-1")

    assert ticket not in tickets._issued
    assert all(ticket not in str(value) for value in tickets._issued.values())


# --- the log route ---------------------------------------------------------

def test_log_route_serves_base64_bytes(client, platform_root):
    raw = b"\x1b[2Jclear \xff\xfe"
    plant_session("s-1", log=raw)

    payload = client.get(
        "/api/sessions/s-1/log", headers=bearer_header()
    ).json()

    assert base64.b64decode(payload["data"]) == raw
    assert payload["session"] == "s-1"
    assert payload["live"] is True
    assert (payload["offset"], payload["next_offset"], payload["size"]) == (
        0, len(raw), len(raw)
    )


def test_log_route_tails_from_a_negative_offset(client, platform_root):
    plant_session("s-1", log=b"0123456789")

    payload = client.get(
        "/api/sessions/s-1/log?offset=-4", headers=bearer_header()
    ).json()

    assert base64.b64decode(payload["data"]) == b"6789"
    assert payload["offset"] == 6


def test_log_route_serves_a_dead_session(client, platform_root):
    """The whole point of D16: history is readable after the container is gone."""
    write_log("s-gone", b"what happened before the crash")

    payload = client.get(
        "/api/sessions/s-gone/log", headers=bearer_header()
    ).json()

    assert base64.b64decode(payload["data"]) == b"what happened before the crash"
    assert payload["live"] is False


def test_log_route_404s_for_an_unknown_session(client, platform_root):
    response = client.get("/api/sessions/s-nope/log", headers=bearer_header())

    assert response.status_code == 404
    assert "no such session" in response.json()["detail"]


@pytest.mark.parametrize("raw_id", ["%2e%2e", "..%20", ".hidden", "%2e%2e%2fsecret"])
def test_log_route_refuses_a_traversal(client, platform_root, raw_id):
    """The daemon is reachable from a phone on a LAN; the id lands in a path."""
    plant_session("s-1", log=b"")
    outside = session_io.session_log_path("s-1").parent.parent / "secret"
    outside.write_text("classified", encoding="utf-8")

    response = client.get(f"/api/sessions/{raw_id}/log", headers=bearer_header())

    assert response.status_code == 404
    assert "classified" not in response.text


def test_log_route_refuses_an_oversized_limit(client, platform_root):
    plant_session("s-1", log=b"x")
    response = client.get(
        f"/api/sessions/s-1/log?limit={session_io.MAX_LOG_LIMIT + 1}",
        headers=bearer_header(),
    )
    assert response.status_code == 422


# --- the input route -------------------------------------------------------

def test_input_route_proxies_to_the_control_plane(
    client, platform_root, control_plane
):
    plant_session("s-1", port=control_plane.port)

    response = client.post(
        "/api/sessions/s-1/input",
        headers=bearer_header(),
        json={"data": "an answer", "append_newline": True},
    )

    assert response.status_code == 200
    assert response.json() == {"session": "s-1", "bytes_written": 7}
    assert control_plane.calls[0]["authorization"] == f"Bearer {CONTROL_TOKEN}"


def test_an_unconfirmed_submit_reaches_the_caller(
    client, platform_root, control_plane
):
    """The supervisor writes Enter to the PTY and cannot see whether the TUI
    took it as a submit. A 200 that said only ``bytes_written`` would read as
    "delivered and sent", which is the half of it nobody can check — so the
    control plane's own uncertainty is passed through rather than dropped."""
    plant_session("s-1", port=control_plane.port)
    control_plane.answer("/input", 200, {
        "bytes_written": 7,
        "submit_confirmed": False,
        "note": "…submit it from the session's terminal view.",
    })

    response = client.post(
        "/api/sessions/s-1/input",
        headers=bearer_header(),
        json={"data": "an answer", "append_newline": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["submit_confirmed"] is False
    assert "terminal view" in body["note"]


@pytest.mark.parametrize("verdict", ["read", "unread", "unknown"])
def test_the_text_read_verdict_reaches_the_caller(
    client, platform_root, control_plane, verdict
):
    """``submit_text`` is the one half of a submit the supervisor can observe.

    It says whether the harness was seen taking the text before Enter was
    pressed (#210), which is what distinguishes "the submit may not have
    registered" from "the message never got there" — so it travels with
    ``submit_confirmed`` instead of being dropped at this boundary. All three
    values are relayed as given: reducing ``unknown`` to a flag would turn "not
    observed" into either a warning or a clean bill of health, and it is neither.
    """
    plant_session("s-1", port=control_plane.port)
    control_plane.answer("/input", 200, {
        "bytes_written": 9,
        "submit_confirmed": False,
        "note": "…submit it from the session's terminal view.",
        "submit_text": verdict,
    })

    body = client.post(
        "/api/sessions/s-1/input",
        headers=bearer_header(),
        json={"data": "an answer", "append_newline": True},
    ).json()

    assert body["submit_text"] == verdict


def test_an_older_session_image_keeps_its_reply_shape(
    client, platform_root, control_plane
):
    """A session whose image predates the text-read verdict reports nothing about
    it, and the route must not invent one — an absent field reads as "this session
    cannot tell you", while a fabricated value would read as a measurement."""
    plant_session("s-1", port=control_plane.port)
    control_plane.answer("/input", 200, {"bytes_written": 9})

    body = client.post(
        "/api/sessions/s-1/input",
        headers=bearer_header(),
        json={"data": "an answer", "append_newline": True},
    ).json()

    assert "submit_text" not in body
    assert body == {"session": "s-1", "bytes_written": 9}


def test_input_route_never_returns_the_session_token(
    client, platform_root, control_plane
):
    plant_session("s-1", port=control_plane.port)
    control_plane.answer("/input", 500, {"detail": "boom"})

    for payload in ({"data": "x"}, {"data": 1}, {}):
        response = client.post(
            "/api/sessions/s-1/input", headers=bearer_header(), json=payload
        )
        assert CONTROL_TOKEN not in response.text
        assert SECRET not in response.text


def test_input_route_requires_a_string_data_field(client, platform_root):
    plant_session("s-1")
    response = client.post(
        "/api/sessions/s-1/input", headers=bearer_header(), json={"data": None}
    )

    assert response.status_code == 400
    assert "'data'" in response.json()["detail"]


def test_input_route_explains_a_session_with_no_control_plane(
    client, platform_root
):
    """A clear 409, not a traceback: the session is fine, it just cannot be typed to."""
    plant_session("s-1", log=b"")

    response = client.post(
        "/api/sessions/s-1/input", headers=bearer_header(), json={"data": "hi"}
    )

    assert response.status_code == 409
    assert "--fastapi" in response.json()["detail"]


def test_input_route_404s_for_an_unknown_session(client, platform_root):
    response = client.post(
        "/api/sessions/s-nope/input", headers=bearer_header(), json={"data": "hi"}
    )
    assert response.status_code == 404


# --- tty tickets over HTTP -------------------------------------------------

def test_ticket_route_mints_for_a_known_session(client, platform_root):
    plant_session("s-1", log=b"")

    payload = client.post(
        "/api/sessions/s-1/tty-ticket", headers=bearer_header()
    ).json()

    assert payload["session"] == "s-1"
    assert payload["expires_in"] == session_io.TICKET_TTL_SECONDS
    assert len(payload["ticket"]) >= 32
    assert SECRET not in payload["ticket"]


def test_ticket_route_404s_for_an_unknown_session(client, platform_root):
    response = client.post(
        "/api/sessions/s-nope/tty-ticket", headers=bearer_header()
    )
    assert response.status_code == 404


# --- the tty socket --------------------------------------------------------

def test_tty_needs_a_ticket(client, platform_root):
    plant_session("s-1", log=b"hello")

    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect("/api/sessions/s-1/tty"):
            pass

    assert caught.value.code == api.WS_POLICY_VIOLATION


def test_tty_refuses_the_shared_secret_as_a_ticket(client, platform_root):
    """The secret must never work in a URL — that is the whole point of tickets."""
    plant_session("s-1", log=b"hello")

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/sessions/s-1/tty?ticket={SECRET}"):
            pass


def test_tty_refuses_a_bearer_header_instead_of_a_ticket(client, platform_root):
    """One way in, so there is one thing to reason about."""
    plant_session("s-1", log=b"hello")

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/api/sessions/s-1/tty", headers=bearer_header()
        ):
            pass


def test_tty_refuses_a_reused_ticket(client, platform_root):
    plant_session("s-1", log=b"hello")
    ticket = client.post(
        "/api/sessions/s-1/tty-ticket", headers=bearer_header()
    ).json()["ticket"]

    with open_tty(client, "s-1", ticket=ticket) as socket:
        assert next_frame(socket)["type"] == "open"

    with pytest.raises(WebSocketDisconnect):
        with open_tty(client, "s-1", ticket=ticket):
            pass


def test_tty_refuses_an_expired_ticket(config, platform_root, monkeypatch):
    monkeypatch.setattr(session_io, "TICKET_TTL_SECONDS", 0.02)
    expiring_client = make_client(config)
    plant_session("s-1", log=b"hello")

    ticket = expiring_client.post(
        "/api/sessions/s-1/tty-ticket", headers=bearer_header()
    ).json()["ticket"]
    time.sleep(0.05)

    with pytest.raises(WebSocketDisconnect):
        with open_tty(expiring_client, "s-1", ticket=ticket):
            pass


def test_a_ticket_cannot_open_another_sessions_socket(client, platform_root):
    plant_session("s-1", log=b"one")
    plant_session("s-2", log=b"two")
    ticket = client.post(
        "/api/sessions/s-1/tty-ticket", headers=bearer_header()
    ).json()["ticket"]

    with pytest.raises(WebSocketDisconnect):
        with open_tty(client, "s-2", ticket=ticket):
            pass


def test_a_rejected_handshake_is_logged(client, platform_root, caplog):
    plant_session("s-1", log=b"hello")

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/sessions/s-1/tty?ticket=forged"):
            pass

    assert any(
        "platform_tty_ticket_rejected" in record.message
        for record in caplog.records
    )


def test_tty_streams_the_backlog_then_live_bytes(client, platform_root):
    plant_session("s-1", log=b"backlog bytes")

    with open_tty(client, "s-1") as socket:
        assert next_frame(socket) == {
            "type": "open", "session": "s-1", "live": True
        }

        backlog = next_frame(socket)
        assert base64.b64decode(backlog["data"]) == b"backlog bytes"
        assert backlog["type"] == "data"

        append_log("s-1", b" then live")
        live = next_frame(socket)
        assert base64.b64decode(live["data"]) == b" then live"
        assert live["offset"] == len(b"backlog bytes")


def test_tty_honours_the_offset_it_is_given(client, platform_root):
    plant_session("s-1", log=b"0123456789")

    with open_tty(client, "s-1", offset=8) as socket:
        assert next_frame(socket)["type"] == "open"
        assert base64.b64decode(next_frame(socket)["data"]) == b"89"


def test_tty_reports_a_session_that_has_ended(client, platform_root):
    """An exited session replays and then says so, rather than going quiet."""
    write_log("s-gone", b"last words")

    with open_tty(client, "s-gone") as socket:
        assert next_frame(socket)["live"] is False
        assert base64.b64decode(next_frame(socket)["data"]) == b"last words"

        ended = next_frame(socket)
        assert ended["type"] == "status"
        assert ended["event"] == "ended"


def test_tty_input_frame_reaches_the_control_plane(
    client, platform_root, control_plane
):
    plant_session("s-1", port=control_plane.port, log=b"")

    with open_tty(client, "s-1") as socket:
        next_frame(socket)
        socket.send_json({"type": "input", "data": "typed", "append_newline": True})

        deadline = time.monotonic() + 5
        while not control_plane.calls and time.monotonic() < deadline:
            time.sleep(0.01)

    assert control_plane.calls == [{
        "path": "/input",
        "authorization": f"Bearer {CONTROL_TOKEN}",
        "body": {"data": "typed", "append_newline": True},
    }]


def test_tty_reports_an_input_failure_without_dropping_the_socket(
    client, platform_root, control_plane
):
    control_plane.answer("/input", 500, {"detail": "the pty is gone"})
    plant_session("s-1", port=control_plane.port, log=b"")

    with open_tty(client, "s-1") as socket:
        next_frame(socket)
        socket.send_json({"type": "input", "data": "typed"})

        status = next_frame(socket)
        assert status["event"] == "input_failed"
        assert "the pty is gone" in status["message"]

        socket.send_json({"type": "input", "data": "again"})
        assert next_frame(socket)["event"] == "input_failed", "socket survived"


@pytest.mark.parametrize("status", [404, 503])
def test_tty_tolerates_an_unsupported_resize(
    client, platform_root, control_plane, status
):
    control_plane.answer("/resize", status, {"detail": "no resize here"})
    plant_session("s-1", port=control_plane.port, log=b"")

    with open_tty(client, "s-1") as socket:
        next_frame(socket)
        socket.send_json({"type": "resize", "rows": 24, "cols": 80})

        frame = next_frame(socket)
        assert frame["event"] == "resize_unsupported"

        socket.send_json({"type": "input", "data": "still here"})
        assert control_plane.calls[-1]["path"] in ("/resize", "/input")


def test_tty_surfaces_a_failed_resize(client, platform_root, control_plane):
    control_plane.answer("/resize", 500, {"detail": "cannot set window size"})
    plant_session("s-1", port=control_plane.port, log=b"")

    with open_tty(client, "s-1") as socket:
        next_frame(socket)
        socket.send_json({"type": "resize", "rows": 24, "cols": 80})

        frame = next_frame(socket)
        assert frame["event"] == "resize_failed"
        assert "ending" in frame["message"]


def test_tty_applies_a_good_resize_silently(client, platform_root, control_plane):
    plant_session("s-1", port=control_plane.port, log=b"")

    with open_tty(client, "s-1") as socket:
        next_frame(socket)
        socket.send_json({"type": "resize", "rows": 50, "cols": 200})
        socket.send_json({"type": "input", "data": "after"})

        deadline = time.monotonic() + 5
        while len(control_plane.calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)

    assert [call["path"] for call in control_plane.calls] == ["/resize", "/input"]
    assert control_plane.calls[0]["body"] == {"rows": 50, "cols": 200}


@pytest.mark.parametrize(
    "frame",
    [
        {"type": "resize", "rows": 0, "cols": 80},
        {"type": "resize", "rows": True, "cols": 80},
        {"type": "resize", "cols": 80},
        {"type": "input", "data": 7},
        {"type": "nonsense"},
        {},
    ],
)
def test_tty_reports_a_bad_frame_and_stays_open(
    client, platform_root, control_plane, frame
):
    plant_session("s-1", port=control_plane.port, log=b"")

    with open_tty(client, "s-1") as socket:
        next_frame(socket)
        socket.send_json(frame)

        assert next_frame(socket)["event"] == "bad_frame"
        assert control_plane.calls == [], "a bad frame must not reach the session"


def test_tty_reports_junk_instead_of_dropping_the_socket(client, platform_root):
    plant_session("s-1", log=b"")

    with open_tty(client, "s-1") as socket:
        next_frame(socket)
        socket.send_text("not json at all")
        assert next_frame(socket)["event"] == "bad_frame"

        socket.send_bytes(b"\x00\x01binary")
        assert next_frame(socket)["event"] == "bad_frame"

        socket.send_json([1, 2, 3])
        assert next_frame(socket)["event"] == "bad_frame"


def test_a_session_that_is_still_starting_is_not_reported_as_a_dead_pty(
    client, platform_root, control_plane
):
    """The dev6 bug: opening the terminal before the harness has finished starting.

    Its control port has no listener yet, so the first resize — which fit-to-screen
    sends the moment the terminal opens — gets a connection reset. That used to come
    back as ``resize_failed``, whose message says the PTY is gone and the session is
    ending, so the client switched fitting off and left a sticky error that only a
    page reload cleared. On a session that was up and working seconds later.

    ``resize_deferred`` is the distinct answer, because the two need opposite
    responses: one is "stop asking", the other is "ask again shortly".
    """
    # A port with nothing listening on it: exactly the pre-startup state.
    plant_session("s-1", port=_unused_port(), log=b"")

    with open_tty(client, "s-1") as socket:
        next_frame(socket)
        socket.send_json({"type": "resize", "rows": 24, "cols": 80})

        frame = next_frame(socket)
        assert frame["event"] == "resize_deferred", (
            "an unreachable control plane must not be reported as a failed ioctl"
        )
        assert "ending" not in frame["message"], (
            "the message must not claim the session is ending — it is starting"
        )


def test_a_genuinely_refused_resize_is_still_a_failure(
    client, platform_root, control_plane
):
    """The other half: the new case must not swallow the real one.

    A control plane that answers and refuses is a different fact from one that is
    not there, and only the first means stop asking.
    """
    control_plane.answer("/resize", 500, {"detail": "cannot set window size"})
    plant_session("s-1", port=control_plane.port, log=b"")

    with open_tty(client, "s-1") as socket:
        next_frame(socket)
        socket.send_json({"type": "resize", "rows": 24, "cols": 80})

        assert next_frame(socket)["event"] == "resize_failed"


def test_resizing_a_session_that_has_no_control_plane_is_a_failure_not_a_wait(
    client, platform_root, control_plane
):
    """The third case, and the one that had no test at all.

    ``apply_resize`` raises before it reaches the network when the session has no
    control plane to reach — a session spawned without ``--fastapi``. That is not a
    startup race and never becomes one, so it must keep the "stop asking" answer;
    routing it to ``resize_deferred`` would leave the client retrying forever
    against a session that will never accept a resize.

    Found by mutation: sending ``bad_frame`` from this branch broke no test.
    """
    plant_session("s-1", port=None, log=b"")

    with open_tty(client, "s-1") as socket:
        next_frame(socket)
        socket.send_json({"type": "resize", "rows": 24, "cols": 80})

        frame = next_frame(socket)
        assert frame["event"] == "resize_failed", (
            "a session with no control plane can never be resized, so this is a "
            f"refusal rather than something to wait out (got {frame['event']!r})"
        )


# --- how long a session's harness has been quiet (T95) ------------------------
#
# The fact the host cannot see for itself: run state moves when a session *ends*
# (spec D24), so a run that finished its work and is sitting at its prompt reads
# exactly like one that is working. The supervisor inside the container times the
# gap since its last byte of PTY output and reports it on /healthz; this is the
# read.
#
# It is on the *same route* probe_health raises on, and it must never raise. Its
# caller is assembling a fleet payload polled from a phone, once per live session,
# and every way of not knowing is ordinary — an older image, an unreachable
# container, a session with no control plane at all. So the whole surface here is
# "a record, or None", and each None below is a case that occurs in the field.

def test_a_live_sessions_idle_reading_comes_off_its_control_plane(
    platform_root, control_plane
):
    control_plane.answer("/healthz", 200, {
        "ok": True, "cursor": 42, "rows": 24, "cols": 80,
        "last_output_at": "2026-07-28T11:38:00Z", "idle_seconds": 1320.0,
    })
    plant_session("s-1", port=control_plane.port)

    assert session_io.session_activity("s-1") == {
        "last_output_at": "2026-07-28T11:38:00Z",
        "idle_seconds": 1320.0,
    }
    assert [call["path"] for call in control_plane.calls] == ["/healthz"]


def test_the_idle_reading_is_asked_for_with_the_session_bearer_token(
    platform_root, control_plane
):
    """Same credential discipline as every other control-plane call.

    The token travels in a header, never in the URL — ``requests`` echoes the URL
    it was given into its exception messages, which is the leak
    ``_scrub_credentials`` exists to fix — and this route is now called on every
    fleet poll, so it is the call most likely to end up in a log.
    """
    control_plane.answer("/healthz", 200, {"ok": True, "idle_seconds": 1})
    plant_session("s-1", port=control_plane.port)

    session_io.session_activity("s-1")

    call = control_plane.calls[0]
    assert call["authorization"] == f"Bearer {CONTROL_TOKEN}"
    assert CONTROL_TOKEN not in call["path"]


def test_an_image_that_reports_no_idleness_reads_as_unknown(
    platform_root, control_plane
):
    """The mixed fleet, pinned: an older supervisor answers without the fields.

    The supervisor ships in the *session image*, not in this daemon, so a fleet
    always contains workers that will never report this. They have to read exactly
    as they did before the fields existed — ``None`` — and above all not as an idle
    of zero, which would claim the harness had just produced something.
    """
    control_plane.answer("/healthz", 200, {"ok": True, "cursor": 42, "rows": 0, "cols": 0})
    plant_session("s-1", port=control_plane.port)

    assert session_io.session_activity("s-1") is None


def test_a_control_plane_that_cannot_be_reached_reads_as_unknown(platform_root):
    """Absorbed, not raised: one dead container must not cost the fleet view.

    The contrast with :func:`session_io.probe_health` on the same route is the
    point — that one raises because its caller is about to claim a session is
    alive, and this one answers "no idea" because its caller is drawing a row.
    """
    plant_session("s-1", port=_unused_port())

    assert session_io.session_activity("s-1") is None


def test_a_session_with_no_control_plane_is_not_asked_anything(platform_root):
    plant_session("s-1", port=None)

    assert session_io.session_activity("s-1") is None


def test_a_crashed_session_reports_no_idleness(platform_root, control_plane):
    """Its last output is not a fact about now, and its port may be someone else's.

    ``control_endpoint`` checks liveness before it reads the port, which is what
    keeps this from probing whatever process inherited it.
    """
    control_plane.answer("/healthz", 200, {"ok": True, "idle_seconds": 5})
    plant_session("s-1", port=control_plane.port, live=False)

    assert session_io.session_activity("s-1") is None
    assert control_plane.calls == [], "a dead session's control plane was polled"


def test_a_refused_health_probe_reads_as_unknown(platform_root, control_plane):
    control_plane.answer("/healthz", 503, {"detail": "no"})
    plant_session("s-1", port=control_plane.port)

    assert session_io.session_activity("s-1") is None


@pytest.mark.parametrize("idle", [True, False, -1, "1320", None, {"seconds": 5}])
def test_an_unusable_idle_reading_is_refused(platform_root, control_plane, idle):
    """``bool`` is an ``int``, so a JSON ``true`` would become "idle one second".

    The same check :func:`session_io.read_control_output` makes of its cursor, for
    the same reason: a number that arrived in the wrong shape is a control plane
    this platform does not understand, and rendering a guess off it is worse than
    rendering nothing.
    """
    control_plane.answer("/healthz", 200, {"ok": True, "idle_seconds": idle})
    plant_session("s-1", port=control_plane.port)

    assert session_io.session_activity("s-1") is None


def test_a_missing_timestamp_still_yields_the_measurement(
    platform_root, control_plane
):
    """The number is the fact; the timestamp is how it gets written down.

    A reply carrying only the number is still readable, so it is kept — the
    timestamp is the tooltip and the loggable form, and a record with the timestamp
    and no number would have nothing to render.
    """
    control_plane.answer("/healthz", 200, {"ok": True, "idle_seconds": 0})
    plant_session("s-1", port=control_plane.port)

    assert session_io.session_activity("s-1") == {
        "last_output_at": None, "idle_seconds": 0,
    }


def test_the_idle_read_gives_up_long_before_a_write_would(
    platform_root, control_plane, monkeypatch
):
    """The bound on the new load, pinned where it is spent.

    This read happens while a fleet payload is being assembled — the browser's
    poll, ``lmer platform status``, every detection tick — once per live session,
    and the whole view waits on it. At the five seconds a write gets, two wedged
    containers would hold the view past the interval that asked for it.
    """
    plant_session("s-1", port=control_plane.port)
    seen = {}
    real_get = session_io._get

    def recording_get(endpoint, path, **kwargs):
        seen[path] = kwargs.get("timeout")
        return real_get(endpoint, path, **kwargs)

    monkeypatch.setattr(session_io, "_get", recording_get)
    session_io.session_activity("s-1")

    assert seen["/healthz"] == session_io.ACTIVITY_TIMEOUT_SECONDS
    assert session_io.ACTIVITY_TIMEOUT_SECONDS < session_io.CONTROL_TIMEOUT_SECONDS, (
        "the fleet view's read is on the same budget as an operator's keystroke"
    )
