"""Tests for surviving the loss of a session's host PTY (issue #141, T36).

The bug these exist to keep fixed was diagnosed from a live daemon restart: the
session looked wedged in the UI (full scrollback, no progress) while ``/healthz``
answered and the supervisor's cursor kept climbing. Nothing was wedged. The
platform's *view* had died with the PTY master its daemon owned.

Three properties carry the weight, and all three fail silently if they regress:

- **The two streams are not one stream.** ``cursor`` indexes the container's ring
  buffer, not the host log. A re-attach that starts at 0 duplicates the whole
  pre-restart scrollback underneath itself, and every test that appends output
  therefore asserts on the *bytes in the log*, not on a call being made.
- **Detached is not liveness.** A session whose control plane does not answer
  must not read as ``running`` on the strength of a PID nobody could ask
  anything.
- **A gap is announced.** Evicted output and unrecoverable host-side chatter are
  both stated in the log, because a terminal that silently jumps reads as the
  agent losing its place.
- **A session that records itself is left alone.** Once a session's image writes
  its own log inside the container (#150), the host tee it would be drained into
  is not the file anyone is served, so the recovery here has to *not happen* — and
  the answer to "which log is the record" is a probe of a file that changes over a
  session's life, never a remembered one.

The control plane is a real loopback HTTP server wrapping the **real**
:class:`lmer_cli.supervisor.OutputBuffer`, so cursors, eviction and
``dropped_bytes`` are the semantics the platform will actually meet rather than
this file's idea of them. One test goes further and drives the real supervisor
FastAPI app, pinning the response shape the platform reads.
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from lmer_cli import supervisor
from lmer_platform import inventory, reattach, registry, session_io, spawn
from lmer_platform import store
from tests.conftest import strip_lmer_env

CONTROL_TOKEN = "reattach-control-plane-token"

#: A pid nothing can be running under, so an entry reads as dead. Same value the
#: rest of the platform tests use.
DEAD_PID = 2**22

#: Server-side long poll used throughout: the drain's cadence has to be short
#: enough that a test finishes, and every assertion here is about *what* was
#: appended rather than how long it took to arrive.
FAST_POLL = 0.02


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


@pytest.fixture(autouse=True)
def _no_leaked_drains():
    """Stop every drain a test started, and fail if one refuses to stop.

    Drains are daemon threads polling a tmp directory that is about to vanish, so
    one that outlives its test is both a slow-test source and a way for one
    test's appends to land in the next test's log. Starting them is normal;
    *not stopping* is the bug — a drain that ignores :meth:`stop` would otherwise
    only show up as unexplained flakiness somewhere else.
    """
    yield
    started = reattach.active_drains()
    for drain in list(reattach._ACTIVE.values()):
        drain.stop()
    deadline = time.monotonic() + 5
    while reattach.active_drains() and time.monotonic() < deadline:
        time.sleep(0.01)
    stubborn = reattach.active_drains()
    reattach._ACTIVE.clear()
    assert not stubborn, (
        f"control drains {stubborn} ignored stop() (started: {started})"
    )


# --- a stand-in for one session's control plane ------------------------------

class _Handler(BaseHTTPRequestHandler):
    """The routes a re-attach touches, answered like the supervisor answers them."""

    protocol_version = "HTTP/1.1"

    def _plane(self):
        return self.server.plane

    def _respond(self, status, payload):
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
        plane = self._plane()
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        plane.calls.append({
            "path": parsed.path,
            "query": query,
            "raw": self.path,
            "authorization": self.headers.get("Authorization"),
        })
        if plane.answer_delay:
            time.sleep(plane.answer_delay)
        if parsed.path == "/healthz":
            if plane.healthz_status != 200:
                self._respond(plane.healthz_status, {"detail": "no"})
                return
            payload = plane.healthz_payload
            if payload is None:
                payload = {
                    "ok": True,
                    "cursor": plane.output.end_offset,
                    "rows": plane.rows,
                    "cols": plane.cols,
                }
            self._respond(200, payload)
            return
        if parsed.path == "/output":
            if plane.output_status != 200:
                self._respond(plane.output_status, {"detail": "no"})
                return
            if plane.output_payload is not None:
                self._respond(200, plane.output_payload)
                return
            cursor = int(query.get("cursor", 0))
            timeout = min(float(query.get("timeout", 0.0)), 1.0)
            data, next_cursor, dropped = plane.output.read_since(cursor, timeout=timeout)
            self._respond(200, {
                "data": data.decode("utf-8", errors="replace"),
                "cursor": next_cursor,
                "dropped_bytes": dropped,
            })
            return
        self._respond(404, {"detail": "no such route"})

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
        plane = self._plane()
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        parsed = urlparse(self.path)
        plane.calls.append({
            "path": parsed.path,
            "body": body,
            "authorization": self.headers.get("Authorization"),
        })
        if parsed.path == "/resize":
            if plane.resize_status != 200:
                self._respond(plane.resize_status, {"detail": "no resize here"})
                return
            plane.resizes.append((body.get("rows"), body.get("cols")))
            plane.rows, plane.cols = body.get("rows"), body.get("cols")
            self._respond(200, {"rows": plane.rows, "cols": plane.cols})
            return
        self._respond(404, {"detail": "no such route"})

    def log_message(self, *args):
        """Silence — the fake plane's access log is noise in test output."""


class FakeSupervisor:
    """A loopback control plane backed by the real :class:`OutputBuffer`.

    The buffer is the real one on purpose: cursor arithmetic, eviction and
    ``dropped_bytes`` are exactly the behaviours the re-attach has to get right,
    and a hand-rolled stand-in would only ever confirm this file's assumptions.
    """

    def __init__(self, *, rows=0, cols=0, limit=1024):
        self.output = supervisor.OutputBuffer(limit=limit)
        self.rows = rows
        self.cols = cols
        self.calls = []
        self.resizes = []
        self.healthz_status = 200
        self.healthz_payload = None
        self.output_status = 200
        self.output_payload = None
        self.resize_status = 200
        self.answer_delay = 0.0
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.plane = self
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self._thread.start()

    def emit(self, data: bytes) -> None:
        """Output the harness produced inside the container."""
        self.output.append(data)

    def paths(self):
        return [call["path"] for call in self.calls]

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def plane():
    server = FakeSupervisor()
    yield server
    server.stop()


# --- planting a session that survived a daemon -------------------------------

def plant_session(session_id, *, port=None, live=True, log=b"", credential=CONTROL_TOKEN):
    """Register a session the way :mod:`lmer_platform.spawn` would have."""
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
    path = spawn.log_path_for(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(log)
    return path


def read_log(session_id) -> bytes:
    return spawn.log_path_for(session_id).read_bytes()


def write_container_log(session_id, data: bytes):
    """Stand in for the container's own supervisor writing the session's log (#150).

    Written through :func:`session_io.container_log_path` rather than a path spelled
    out here, so a test cannot pass while the re-attach probes somewhere else — the
    same reason the session_io tests plant it that way.
    """
    path = session_io.container_log_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def mount_container_log_dir(session_id):
    """Create the mounted directory and nothing in it — what an older image leaves.

    The platform makes this directory for every session it spawns, so its presence
    says only that *this daemon* is new enough to have offered it.
    """
    directory = session_io.container_log_path(session_id).parent
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def wait_for(predicate, *, timeout=5.0, what="condition"):
    """Poll until *predicate* holds, failing the test rather than hanging."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail(f"{what} did not happen within {timeout}s")


def wait_for_log(session_id, needle: bytes, *, timeout=5.0):
    wait_for(
        lambda: needle in read_log(session_id),
        timeout=timeout,
        what=f"{needle!r} reaching the log",
    )


def detached_of(session_id):
    return reattach.detached_record(registry.read_session(session_id))


def fleet_state(session_id):
    """The state the fleet view shows for a planted session, end to end.

    Built from the registry entries a re-attach actually wrote rather than from a
    hand-made one, because that is the only way to catch the two disagreeing: a
    report that says a session is being read while its entry says nothing reaches
    its log shows up as ``detached`` (:func:`inventory._is_blind`), and a test that
    reads only the report never sees it.
    """
    inv = inventory.build_inventory([], registry.list_sessions(live_only=False))
    return {run.session["id"]: run.state for run in inv.runs}[session_id]


# --- the contract with the real supervisor -----------------------------------

def test_the_platform_reads_the_shape_the_supervisor_actually_answers():
    """Pin the seam: /healthz and /output must carry what a re-attach needs.

    Everything else here talks to a stand-in. This one drives the real supervisor
    app, so a route that renamed ``cursor`` or dropped ``dropped_bytes`` fails
    here rather than in production at the worst possible moment.
    """
    from fastapi.testclient import TestClient

    buffer = supervisor.OutputBuffer(limit=1024)
    app = supervisor._build_fastapi_app(
        buffer, lambda data: len(data), "tok", get_winsize=lambda: (0, 0)
    )
    client = TestClient(app)
    auth = {"Authorization": "Bearer tok"}

    buffer.append(b"hello")
    health = client.get("/healthz", headers=auth).json()
    assert health["cursor"] == 5, "the probe must hand back a resumable offset"
    assert (health["rows"], health["cols"]) == (0, 0), (
        "an unsized PTY reports 0x0 — the case a re-attach exists to fix"
    )

    body = client.get("/output", params={"cursor": 0, "timeout": 0}, headers=auth).json()
    assert set(body) == {"data", "cursor", "dropped_bytes"}
    assert body["data"] == "hello"
    assert body["cursor"] == 5


# --- reading a session over its control plane --------------------------------

def test_probe_health_reports_the_container_cursor(platform_root, plane):
    plant_session("s-1", port=plane.port)
    plane.emit(b"already produced")

    health = session_io.probe_health("s-1")
    assert health["cursor"] == len(b"already produced")


def test_probe_health_is_loud_when_the_plane_refuses(platform_root, plane):
    """A "no" must raise: a caller about to claim liveness cannot ignore a value."""
    plant_session("s-1", port=plane.port)
    plane.healthz_status = 503

    with pytest.raises(session_io.ControlPlaneError):
        session_io.probe_health("s-1")


def test_probe_health_sends_the_token_in_a_header_not_the_url(platform_root, plane):
    plant_session("s-1", port=plane.port)
    session_io.probe_health("s-1")

    call = plane.calls[-1]
    assert call["authorization"] == f"Bearer {CONTROL_TOKEN}"
    assert CONTROL_TOKEN not in call["raw"], "the credential must never reach a URL"


def test_reading_output_returns_bytes_and_the_servers_cursor(platform_root, plane):
    plant_session("s-1", port=plane.port)
    plane.emit(b"first")

    chunk = session_io.read_control_output("s-1", cursor=0)
    assert chunk.data == b"first"
    assert chunk.cursor == 5
    assert chunk.dropped == 0


def test_the_cursor_comes_from_the_answer_not_from_the_decoded_length(
    platform_root, plane
):
    """A multi-byte character makes ``len(text)`` and the byte cursor disagree.

    Advancing by the decoded length would put the reader behind by one byte per
    multi-byte character, and it would re-read that byte forever.
    """
    plant_session("s-1", port=plane.port)
    plane.emit("héllo".encode("utf-8"))  # 6 bytes, 5 characters

    chunk = session_io.read_control_output("s-1", cursor=0)
    assert chunk.cursor == 6, "the ring buffer counts bytes, not characters"
    assert chunk.data == "héllo".encode("utf-8")


def test_evicted_output_is_reported_as_dropped(platform_root, plane):
    """The container's buffer is bounded; what fell out of it is gone for good."""
    plane.output = supervisor.OutputBuffer(limit=16)
    plant_session("s-1", port=plane.port)
    plane.emit(b"a" * 16)
    plane.emit(b"b" * 16)

    chunk = session_io.read_control_output("s-1", cursor=0)
    assert chunk.dropped == 16
    assert chunk.data == b"b" * 16


def test_a_refused_output_read_says_the_plane_refused_it(platform_root, plane):
    """The message ends up in ``detached.detail``, which is what the UI shows.

    A refusal and a malformed answer both fail, but they send an operator to
    different places: "refused (invalid bearer token)" is a stale token file,
    while "no usable data/cursor" is a supervisor that does not speak this
    protocol. Reporting the first as the second costs an afternoon.
    """
    plant_session("s-1", port=plane.port)
    plane.output_status = 401

    with pytest.raises(session_io.ControlPlaneError) as caught:
        session_io.read_control_output("s-1", cursor=0)
    assert "refused an output read" in str(caught.value)


def test_a_malformed_output_answer_is_a_gateway_error_not_silence(
    platform_root, plane
):
    """Fabricating an empty chunk would read as "the session went quiet"."""
    plant_session("s-1", port=plane.port)
    plane.output_payload = {"data": "text", "cursor": None}

    with pytest.raises(session_io.ControlPlaneError):
        session_io.read_control_output("s-1", cursor=0)


@pytest.mark.parametrize("payload", [
    {"cursor": 4},
    {"data": "text"},
    {"data": 17, "cursor": 4},
    {"data": "text", "cursor": True},
])
def test_output_answers_missing_a_usable_cursor_or_data_are_refused(
    platform_root, plane, payload
):
    plant_session("s-1", port=plane.port)
    plane.output_payload = payload

    with pytest.raises(session_io.ControlPlaneError):
        session_io.read_control_output("s-1", cursor=0)


def test_a_missing_dropped_count_reads_as_none_dropped(platform_root, plane):
    plant_session("s-1", port=plane.port)
    plane.output_payload = {"data": "text", "cursor": 4}

    assert session_io.read_control_output("s-1", cursor=0).dropped == 0


def test_the_long_poll_is_not_cut_short_by_the_ordinary_control_timeout(
    platform_root, plane, monkeypatch
):
    """``/output`` blocks on purpose, so its read budget is poll + grace.

    Timing out a deliberately-held request at the ordinary five seconds would
    make every idle session look like an unreachable one.
    """
    monkeypatch.setattr(session_io, "CONTROL_TIMEOUT_SECONDS", 0.2)
    plant_session("s-1", port=plane.port)
    plane.answer_delay = 0.5
    plane.output_payload = {"data": "late", "cursor": 4}

    chunk = session_io.read_control_output("s-1", cursor=0, timeout=1.0)
    assert chunk.data == b"late"


def test_reading_a_session_whose_process_is_gone_is_unavailable(platform_root, plane):
    """The resolver checks liveness first, which is how the drain learns to stop."""
    plant_session("s-1", port=plane.port, live=False)

    with pytest.raises(session_io.ControlUnavailable):
        session_io.read_control_output("s-1", cursor=0)


# --- re-attaching one session ------------------------------------------------

def test_reattach_appends_new_output_after_a_seam(platform_root, plane):
    plant_session("s-1", port=plane.port, log=b"scrollback from before the restart")
    plane.emit(b"output the container already produced")

    report = reattach.reattach_session("s-1", poll=FAST_POLL)
    assert report.draining is True

    plane.emit(b"AFTER-THE-RESTART")
    wait_for_log("s-1", b"AFTER-THE-RESTART")

    log = read_log("s-1")
    assert log.startswith(b"scrollback from before the restart"), (
        "spec D16: the pre-restart bytes stay exactly where they were"
    )
    assert b"the daemon restarted" in log, "the seam has to be in the log"
    assert log.index(b"the daemon restarted") < log.index(b"AFTER-THE-RESTART")


def test_reattach_starts_at_the_current_end_not_at_zero(platform_root, plane):
    """Starting at 0 replays the ring buffer under the copy the log already has."""
    plant_session("s-1", port=plane.port, log=b"tee'd before the restart: OLD-OUTPUT")
    plane.emit(b"OLD-OUTPUT")

    reattach.reattach_session("s-1", poll=FAST_POLL)
    plane.emit(b"NEW-OUTPUT")
    wait_for_log("s-1", b"NEW-OUTPUT")

    assert read_log("s-1").count(b"OLD-OUTPUT") == 1, (
        "the container's pre-restart output must not be appended a second time"
    )


def test_the_seam_says_the_host_side_gap_is_unrecoverable(platform_root, plane):
    """Honesty is the feature: those bytes were never written anywhere."""
    plant_session("s-1", port=plane.port)
    reattach.reattach_session("s-1", poll=FAST_POLL)

    seam = read_log("s-1")
    assert b"cannot be recovered" in seam
    assert b"host" in seam


def test_reattach_marks_the_session_detached_before_it_probes(platform_root):
    """Whatever the probe says, the host PTY is already gone.

    With no control plane at all the probe cannot even be made, so this is the
    case that shows the mark does not depend on it.
    """
    plant_session("s-1")  # registered, live, no control plane

    report = reattach.reattach_session("s-1", poll=FAST_POLL)

    assert report.detached is True
    assert report.draining is False
    assert detached_of("s-1")["output"] == reattach.OUTPUT_NONE


def test_an_unreachable_control_plane_leaves_it_detached_and_stops(
    platform_root, plane
):
    plant_session("s-1", port=plane.port)
    plane.stop()  # the container is gone; the host process lingers

    report = reattach.reattach_session("s-1", poll=FAST_POLL)

    assert (report.detached, report.draining) == (True, False)
    assert detached_of("s-1")["output"] == reattach.OUTPUT_NONE
    assert reattach.active_drains() == [], "no drain may run against nothing"
    assert read_log("s-1") == b"", "no seam for output that is not coming"


def test_an_unexpected_probe_failure_still_leaves_the_session_marked(
    platform_root, plane, monkeypatch
):
    """Why the mark goes on *before* the probe rather than after it.

    ``SessionIOError`` is the failure this path expects; anything else — a bug, a
    registry read blowing up — must not be able to hand back a session that
    silently reads as ``running`` again. The mark is a statement about the host
    PTY, which is gone regardless of what happens next.
    """
    plant_session("s-1", port=plane.port)

    def explode(session_id):
        raise RuntimeError("something nobody planned for")

    monkeypatch.setattr(reattach, "probe_health", explode)
    assert reattach.reattach_all(poll=FAST_POLL) == []

    assert detached_of("s-1")["output"] == reattach.OUTPUT_NONE


def test_a_corrupt_entry_is_skipped_quietly_not_logged_as_a_failure(
    platform_root, caplog
):
    """Registry files are hand-editable; one with a nonsense id is not an incident."""
    plant_session("s-good")
    bad = store.sessions_dir() / "s-bad.json"
    bad.write_text(json.dumps({"id": 42, "pid": os.getpid()}), encoding="utf-8")

    with caplog.at_level("WARNING", logger="lmer_platform.reattach"):
        reports = reattach.reattach_all(poll=FAST_POLL)

    assert [r.session_id for r in reports] == ["s-good"]
    assert not [
        record for record in caplog.records
        if "platform_reattach_failed" in record.message
    ]


def test_a_health_probe_without_a_cursor_refuses_to_drain(platform_root, plane):
    """With no offset there is no safe place to start, and 0 is the unsafe one."""
    plant_session("s-1", port=plane.port, log=b"before")
    plane.healthz_payload = {"ok": True, "rows": 24, "cols": 80}

    report = reattach.reattach_session("s-1", poll=FAST_POLL)

    assert report.draining is False
    assert detached_of("s-1")["output"] == reattach.OUTPUT_NONE
    assert read_log("s-1") == b"before", "nothing may be appended without a cursor"


def test_a_session_that_vanished_before_the_mark_is_reported_not_raised(
    platform_root
):
    report = reattach.reattach_session("s-never-registered", poll=FAST_POLL)

    assert (report.detached, report.draining) == (False, False)
    assert "vanished" in report.detail


def test_a_second_reattach_does_not_start_a_second_drain(platform_root, plane):
    """Two drains on one log is the duplicated-output failure, spelled twice."""
    plant_session("s-1", port=plane.port)
    reattach.reattach_session("s-1", poll=FAST_POLL)
    plane.emit(b"ONCE")
    wait_for_log("s-1", b"ONCE")

    second = reattach.reattach_session("s-1", poll=FAST_POLL)

    assert second.draining is True
    assert reattach.active_drains() == ["s-1"]
    plane.emit(b"TWICE")
    wait_for_log("s-1", b"TWICE")
    assert read_log("s-1").count(b"TWICE") == 1
    assert read_log("s-1").count(reattach.seam_marker(0)[:40]) == 1, (
        "a re-attach that changed nothing must not write a second seam"
    )


def test_an_unwritable_log_stops_the_reattach_rather_than_claiming_recovery(
    platform_root, plane, monkeypatch
):
    plant_session("s-1", port=plane.port)
    monkeypatch.setattr(reattach, "_append", lambda session_id, data: False)

    report = reattach.reattach_session("s-1", poll=FAST_POLL)

    assert report.draining is False
    assert detached_of("s-1")["output"] == reattach.OUTPUT_NONE
    assert reattach.active_drains() == []


# --- geometry ----------------------------------------------------------------

def test_a_session_that_was_never_sized_is_resized_on_reattach(platform_root, plane):
    """The live probe found rows:0, cols:0 — a TUI rendering into nothing."""
    plane.rows, plane.cols = 0, 0
    plant_session("s-1", port=plane.port)

    reattach.reattach_session("s-1", poll=FAST_POLL)

    assert plane.resizes == [(reattach.REATTACH_ROWS, reattach.REATTACH_COLS)]


def test_a_session_that_already_has_a_geometry_is_left_alone(platform_root, plane):
    """Re-applying a default would shrink a terminal that was correctly sized."""
    plane.rows, plane.cols = 50, 200
    plant_session("s-1", port=plane.port)

    reattach.reattach_session("s-1", poll=FAST_POLL)

    assert plane.resizes == []


def test_a_refused_resize_does_not_cost_the_output_recovery(platform_root, plane):
    """Resize is cosmetic; the output is what the operator came for."""
    plane.rows, plane.cols = 0, 0
    plane.resize_status = 503
    plant_session("s-1", port=plane.port)

    report = reattach.reattach_session("s-1", poll=FAST_POLL)

    assert report.draining is True
    plane.emit(b"STILL-DRAINING")
    wait_for_log("s-1", b"STILL-DRAINING")


def test_a_session_that_dies_between_the_probe_and_the_resize_still_drains(
    platform_root, plane, monkeypatch
):
    """The resize is a second round trip, and the session can end inside it."""
    plane.rows, plane.cols = 0, 0
    plant_session("s-1", port=plane.port)

    def gone(session_id, rows, cols):
        raise session_io.ControlUnavailable("the session is not running")

    monkeypatch.setattr(reattach, "apply_resize", gone)
    assert reattach.reattach_session("s-1", poll=FAST_POLL).draining is True


# --- a session whose own log is the record (#150) -----------------------------
#
# The drain and the seam both act on the host-side PTY tee. For a session whose
# image writes its own log inside the container, that file is not what the read
# path serves (``session_io.canonical_log``), so draining into it produces bytes
# nobody will be shown and the seam marker announces a break in a stream that has
# none. Both have to stop happening — while every session that does *not* write its
# own log keeps the recovery above, unchanged, forever.

def test_a_session_that_records_itself_is_not_drained_into_the_host_tee(
    platform_root, plane
):
    """The finding: the drain would feed a file the read path stopped serving."""
    plant_session("s-1", port=plane.port, log=b"host tee bytes from before")
    write_container_log("s-1", b"the harness drew this, from inside")
    plane.emit(b"output the container already produced")

    report = reattach.reattach_session("s-1", poll=FAST_POLL)

    assert report.draining is False
    assert reattach.active_drains() == [], "no drain may run against a file nobody reads"
    assert report.cursor is None, "there is no offset to resume from; nothing resumes"


def test_no_seam_is_written_into_a_log_that_is_not_the_record(platform_root, plane):
    """A seam would claim a gap the in-container log does not have.

    Asserted on both files: the host tee must not grow a marker nobody will read,
    and the record itself must not be touched at all — it is the container's file
    and the platform only ever reads it.
    """
    plant_session("s-1", port=plane.port, log=b"host tee bytes from before")
    record = write_container_log("s-1", b"the harness drew this, from inside")

    reattach.reattach_session("s-1", poll=FAST_POLL)

    assert read_log("s-1") == b"host tee bytes from before"
    assert record.read_bytes() == b"the harness drew this, from inside"


def test_a_session_that_records_itself_is_still_marked_detached(platform_root, plane):
    """The mark is about the PTY, which is gone whichever file holds the output."""
    plant_session("s-1", port=plane.port)
    write_container_log("s-1", b"the harness drew this, from inside")

    report = reattach.reattach_session("s-1", poll=FAST_POLL)

    assert report.detached is True
    record = detached_of("s-1")
    assert record["reason"] == reattach.DETACH_REASON_DAEMON_RESTART
    assert record["output"] == reattach.OUTPUT_SESSION_LOG, (
        "neither 'control_plane' (no drain is running) nor 'none' (its own log is "
        "still being written) is true here"
    )
    assert report.output == reattach.OUTPUT_SESSION_LOG
    assert "nothing to recover" in record["detail"]


def test_a_session_that_records_itself_is_still_resized_if_it_was_never_sized(
    platform_root, plane
):
    """0x0 is a fact about the container's PTY, not about which file it lands in."""
    plane.rows, plane.cols = 0, 0
    plant_session("s-1", port=plane.port)
    write_container_log("s-1", b"the harness drew this, from inside")

    reattach.reattach_session("s-1", poll=FAST_POLL)

    assert plane.resizes == [(reattach.REATTACH_ROWS, reattach.REATTACH_COLS)]


def test_a_session_that_records_itself_but_stopped_answering_is_still_blind(
    platform_root, plane
):
    """Why the source is asked *after* the probe, not before it.

    A container that no longer answers is not writing its log either, so what is on
    disk is a complete but finished record. Reporting that as a live self-record
    would credit a dead session with a writer.
    """
    plant_session("s-1", port=plane.port)
    write_container_log("s-1", b"what it drew before the container went away")
    plane.stop()

    report = reattach.reattach_session("s-1", poll=FAST_POLL)

    assert (report.detached, report.draining) == (True, False)
    assert detached_of("s-1")["output"] == reattach.OUTPUT_NONE
    assert "nothing is being recorded" in report.detail


@pytest.mark.parametrize("prepare", [
    pytest.param(lambda session_id: None, id="no-mount"),
    pytest.param(mount_container_log_dir, id="mounted-but-nothing-written"),
    pytest.param(lambda session_id: write_container_log(session_id, b""), id="empty-log"),
])
def test_a_session_with_nothing_in_a_log_of_its_own_is_recovered_the_old_way(
    platform_root, plane, prepare
):
    """The in-between, and the permanent case, are the same case.

    An older image never writes the file; a modern one has not written it yet when
    the daemon dies during the pull or the clone. Both are sessions whose only
    record is the tee, and both must get the drain — which is exactly what a probe
    of the file's *content* answers and what a version check would get wrong.
    """
    plant_session("s-1", port=plane.port, log=b"host tee bytes from before")
    prepare("s-1")

    report = reattach.reattach_session("s-1", poll=FAST_POLL)

    assert report.draining is True
    assert report.output == reattach.OUTPUT_CONTROL_PLANE
    assert b"the daemon restarted" in read_log("s-1"), "the seam belongs in the record"
    plane.emit(b"AFTER-THE-RESTART")
    wait_for_log("s-1", b"AFTER-THE-RESTART")


def test_which_log_is_the_record_is_resolved_at_reattach_not_remembered(
    platform_root, plane
):
    """Two re-attaches of one session, with the answer changing in between.

    A source cached anywhere — on the entry, in this process, at spawn — keeps the
    first answer and drains a session whose log of record has moved on. The order
    here is the real one: the daemon dies during the launch window, is restarted,
    drains; the supervisor then starts writing; a later re-attach must see that.
    """
    plant_session("s-1", port=plane.port, log=b"host tee bytes from before")
    write_container_log("s-1", b"")

    first = reattach.reattach_session("s-1", poll=FAST_POLL)
    assert first.draining is True, "nothing in its own log yet: the tee is the record"
    reattach._ACTIVE["s-1"].stop()
    wait_for(lambda: reattach.active_drains() == [], what="the first drain stopping")
    seams = read_log("s-1").count(b"the daemon restarted")

    write_container_log("s-1", b"the harness drew this, from inside")
    second = reattach.reattach_session("s-1", poll=FAST_POLL)

    assert second.draining is False
    assert second.output == reattach.OUTPUT_SESSION_LOG
    assert detached_of("s-1")["output"] == reattach.OUTPUT_SESSION_LOG
    assert read_log("s-1").count(b"the daemon restarted") == seams, (
        "the second re-attach recovered nothing, so it announced nothing"
    )


def test_a_log_that_appears_under_a_running_drain_leaves_the_drain_alone(
    platform_root, plane
):
    """Decided, not overlooked: the drain runs on.

    Its cost is what it was before the second log existed, and its output misleads
    nobody — while stopping it would give up the only host-side reader of a session
    whose in-container log can stop being canonical again (its writer unlinks it on
    a failed write, and an emptied log reads as the tee's).
    """
    plant_session("s-1", port=plane.port)
    reattach.reattach_session("s-1", poll=FAST_POLL)
    plane.emit(b"BEFORE-THE-LOG-APPEARED")
    wait_for_log("s-1", b"BEFORE-THE-LOG-APPEARED")

    write_container_log("s-1", b"the harness drew this, from inside")

    plane.emit(b"AFTER-THE-LOG-APPEARED")
    wait_for_log("s-1", b"AFTER-THE-LOG-APPEARED")
    assert reattach.active_drains() == ["s-1"]


def test_a_reattach_will_not_relabel_a_session_this_daemon_is_draining(
    platform_root, plane
):
    """The entry must never say ``session_log`` while a thread here appends to the tee.

    ``reattach_all`` is reachable from more than the startup path, so this pair of
    states — a drain running, an in-container log that has since appeared — is one a
    second call really meets.

    What the entry says afterwards is what the drain makes true: ``control_plane``,
    because a thread here is appending to the tee.
    """
    plant_session("s-1", port=plane.port)
    reattach.reattach_session("s-1", poll=FAST_POLL)
    write_container_log("s-1", b"the harness drew this, from inside")

    second = reattach.reattach_session("s-1", poll=FAST_POLL)

    assert second.draining is True
    assert "already being read" in second.detail
    assert reattach.active_drains() == ["s-1"]
    assert detached_of("s-1")["output"] == reattach.OUTPUT_CONTROL_PLANE, (
        "never ``session_log`` while a thread here appends to the tee — and what it "
        "says instead is the drain, not the opening provisional mark"
    )


def test_a_probe_that_fails_on_a_rescan_cannot_unmark_a_working_drain(
    platform_root, plane, monkeypatch
):
    """The drain outranks the health probe too, so the probe is not even made.

    Which is the difference between a placeholder and an answer. ``none`` on the
    entry means "nothing is appending to this log", and a blip in one ``/healthz``
    round trip is not evidence of that while a thread in this process is reading the
    session and writing what it reads — as the appends after the rescan show.
    """
    plant_session("s-1", port=plane.port)
    reattach.reattach_session("s-1", poll=FAST_POLL)
    plane.emit(b"BEFORE-THE-RESCAN")
    wait_for_log("s-1", b"BEFORE-THE-RESCAN")

    def blip(session_id):
        raise session_io.ControlUnavailable("the plane chose this moment not to answer")

    monkeypatch.setattr(reattach, "probe_health", blip)
    second = reattach.reattach_session("s-1", poll=FAST_POLL)

    assert (second.detached, second.draining) == (True, True)
    assert detached_of("s-1")["output"] == reattach.OUTPUT_CONTROL_PLANE
    assert second.cursor is not None, (
        "the offset can only be the drain's own position: nothing answered a probe"
    )
    assert detached_of("s-1")["cursor"] == second.cursor
    plane.emit(b"AFTER-THE-RESCAN")
    wait_for_log("s-1", b"AFTER-THE-RESCAN")


# --- the drain itself --------------------------------------------------------

def test_the_drain_stops_when_the_session_process_is_gone(platform_root, plane):
    plant_session("s-1", port=plane.port)
    reattach.reattach_session("s-1", poll=FAST_POLL)
    plane.emit(b"WORKING")
    wait_for_log("s-1", b"WORKING")

    registry.update("s-1", pid=DEAD_PID)
    wait_for(
        lambda: reattach.active_drains() == [],
        what="the drain noticing the session ended",
    )


def test_the_drain_announces_bytes_the_container_evicted(platform_root, plane):
    """A silent jump in a terminal reads as the agent losing its place."""
    plane.output = supervisor.OutputBuffer(limit=32)
    plant_session("s-1", port=plane.port)
    reattach.reattach_session("s-1", poll=FAST_POLL)

    drain = reattach._ACTIVE["s-1"]
    drain.stop()
    wait_for(lambda: reattach.active_drains() == [], what="the drain stopping")

    # Overflow the buffer well past the cursor the drain was holding, then run one
    # more pass from that stale cursor — exactly what a slow reader would meet.
    plane.emit(b"x" * 64)
    assert reattach.ControlDrain("s-1", cursor=0, poll=FAST_POLL).poll_once() is True

    log = read_log("s-1")
    assert b"32 bytes of this session's output were" in log
    assert b"evicted" in log


def test_the_drain_resumes_from_the_servers_cursor_not_from_what_it_appended(
    platform_root, plane
):
    """After a drop, ``cursor + len(appended)`` is *behind* the server's cursor.

    The gap is exactly the dropped bytes, so a drain that advanced by what it
    wrote would sit permanently behind the ring buffer's start and report a fresh
    data loss on every single pass — inventing gaps that never happened while
    re-appending output already in the log.
    """
    plane.output = supervisor.OutputBuffer(limit=64)
    plant_session("s-1", port=plane.port)
    drain = reattach.ControlDrain("s-1", cursor=0, poll=FAST_POLL)

    plane.emit(b"A" * 64)               # evicted before the drain ever sees it
    plane.emit(b"B" * 64)               # buffer now spans 64..128
    assert drain.poll_once() is True
    assert drain.cursor == 128, "the resume point is the server's end offset"

    plane.emit(b"TAIL")                 # buffer now spans 68..132
    assert drain.poll_once() is True

    log = read_log("s-1")
    assert log.endswith(b"TAIL")
    assert log.count(b"evicted from the container's buffer") == 1, (
        "only the first pass lost anything; a lagging cursor invents more"
    )


def test_the_drain_gives_up_after_repeated_failures_and_says_so(
    platform_root, plane, monkeypatch
):
    """A control plane that stopped answering while the PID lingers is a dead
    container under a host process that has not noticed. Retrying forever is a
    thread leak, and leaving the entry claiming a live view is the original lie."""
    monkeypatch.setattr(reattach, "MAX_CONSECUTIVE_FAILURES", 2)
    plant_session("s-1", port=plane.port)
    reattach.reattach_session("s-1", poll=FAST_POLL)
    assert detached_of("s-1")["output"] == reattach.OUTPUT_CONTROL_PLANE

    plane.output_status = 500
    wait_for(
        lambda: detached_of("s-1")["output"] == reattach.OUTPUT_NONE,
        what="the drain giving up",
    )
    wait_for(lambda: reattach.active_drains() == [], what="the drain thread exiting")


def test_a_transient_failure_does_not_end_the_drain(platform_root, plane, monkeypatch):
    """One blip must not cost the operator their only view of a working session.

    The cap counts *consecutive* failures, so this also pins the reset: two blips
    early on plus one more an hour later must not add up to a give-up. Without
    the reset the third failure here is the third of three and the drain stops,
    so the second recovery never lands.
    """
    monkeypatch.setattr(reattach, "MAX_CONSECUTIVE_FAILURES", 3)
    monkeypatch.setattr(reattach, "FAILURE_BACKOFF_SECONDS", 0.01)
    plant_session("s-1", port=plane.port)
    reattach.reattach_session("s-1", poll=FAST_POLL)

    def after_more_polls(count):
        seen = sum(1 for c in plane.calls if c["path"] == "/output")
        wait_for(
            lambda: sum(1 for c in plane.calls if c["path"] == "/output") >= seen + count,
            what=f"{count} more poll(s)",
        )

    plane.output_status = 500
    after_more_polls(2)
    plane.output_status = 200
    plane.emit(b"RECOVERED")
    wait_for_log("s-1", b"RECOVERED")

    # One more failure, long after the first two. With the counter reset it is
    # failure 1 of 3; without it, failure 3 of 3.
    plane.output_status = 500
    after_more_polls(1)
    plane.output_status = 200
    plane.emit(b"RECOVERED-AGAIN")

    wait_for_log("s-1", b"RECOVERED-AGAIN")
    assert detached_of("s-1")["output"] == reattach.OUTPUT_CONTROL_PLANE


def test_a_drain_that_cannot_write_the_log_gives_up(platform_root, plane, monkeypatch):
    plant_session("s-1", port=plane.port)
    plane.emit(b"pre-existing")
    drain = reattach.ControlDrain("s-1", cursor=0, poll=FAST_POLL)
    monkeypatch.setattr(reattach, "_append", lambda session_id, data: False)

    assert drain.poll_once() is False
    assert detached_of("s-1")["output"] == reattach.OUTPUT_NONE


def test_starting_a_duplicate_drain_is_refused(platform_root, plane):
    plant_session("s-1", port=plane.port)
    first = reattach.ControlDrain("s-1", cursor=0, poll=FAST_POLL)
    first.start()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            reattach.ControlDrain("s-1", cursor=0, poll=FAST_POLL).start()
    finally:
        first.stop()
        wait_for(lambda: reattach.active_drains() == [], what="the drain stopping")


# --- the whole fleet ---------------------------------------------------------

def test_reattach_all_skips_a_session_whose_process_is_gone(platform_root, plane):
    """A stale entry is the crash signal; marking a corpse detached erases it."""
    plant_session("s-dead", port=plane.port, live=False)
    plant_session("s-live", port=plane.port)

    reports = reattach.reattach_all(poll=FAST_POLL)

    assert [r.session_id for r in reports] == ["s-live"]
    assert detached_of("s-dead") is None


def test_reattach_all_reports_each_survivor(platform_root, plane):
    plant_session("s-reachable", port=plane.port)
    plant_session("s-blind")  # live, but advertises no control plane

    reports = {r.session_id: r for r in reattach.reattach_all(poll=FAST_POLL)}

    assert reports["s-reachable"].draining is True
    assert reports["s-blind"].draining is False
    assert reports["s-blind"].detached is True


def test_the_startup_notice_names_what_the_operator_cannot_see(platform_root, plane):
    plant_session("s-reachable", port=plane.port)
    plant_session("s-blind")

    notice = reattach.startup_notice(reattach.reattach_all(poll=FAST_POLL))

    assert "s-reachable" in notice
    assert "s-blind" in notice
    assert "nothing is recorded" in notice


def test_the_startup_notice_does_not_promise_a_seam_that_is_not_there(
    platform_root, plane
):
    """A self-recording survivor came through whole; the seam is the other bucket's.

    Both halves matter to whoever reads that line at boot: an operator told to
    expect a break goes looking for one, and an operator *not* told about the
    session that carries one misses the only warning they get.
    """
    plant_session("s-recording", port=plane.port)
    write_container_log("s-recording", b"the harness drew this, from inside")
    plant_session("s-drained", port=plane.port)

    notice = reattach.startup_notice(reattach.reattach_all(poll=FAST_POLL))

    recording, drained = [
        line for line in notice.splitlines() if "s-recording" in line or "s-drained" in line
    ]
    assert "s-recording" in recording and "nothing was lost" in recording
    assert "seam" not in recording
    assert "s-drained" in drained and "seam" in drained


def test_no_survivors_means_no_notice(platform_root):
    assert reattach.startup_notice([]) is None


def test_a_session_that_vanished_is_not_announced_as_a_survivor(platform_root):
    """It produced a report and no survivor; announcing one points at nothing."""
    vanished = reattach.ReattachReport(
        session_id="s-gone", detached=False, draining=False, detail="vanished"
    )
    assert reattach.startup_notice([vanished]) is None


def test_the_daemon_reattaches_before_it_serves(platform_root, monkeypatch):
    """Order matters: the first request for a survivor's log must see the seam.

    And the assistant's auto-start (T63) sits between the two: a surviving
    assistant has to be marked and re-attached before anything adopts it, and it
    has to be up before uvicorn accepts anything. Stubbed rather than run,
    because an unpatched ``run`` here would spawn the ``lmer`` on PATH.
    """
    from lmer_platform import daemon

    order = []
    monkeypatch.setattr(
        daemon, "reattach_all", lambda: order.append("reattach") or []
    )

    class NoSupervisor:
        def stop(self):
            pass

    monkeypatch.setattr(
        daemon, "_supervise_assistant",
        lambda config: order.append("assistant") or NoSupervisor(),
    )

    class FakeUvicorn:
        @staticmethod
        def run(app, **kwargs):
            order.append("serve")

    monkeypatch.setitem(__import__("sys").modules, "uvicorn", FakeUvicorn)
    daemon.main(["run"])

    assert order == ["reattach", "assistant", "serve"]


# --- what the fleet view says ------------------------------------------------

def _entry(**detached):
    entry = {
        "id": "s-1",
        "pid": 4242,
        "live": True,
        "started_at": "2026-07-27T10:00:00Z",
        "run": {
            "host": "gitlab.example.com", "project": "agents/global", "slug": "r1",
        },
        "task": {"taskdef": "develop", "target": "issue-141"},
    }
    if detached:
        entry["detached"] = detached
    return entry


def _run_dir(tmp_path, slug="r1"):
    """A run dir in the mirror, so the row is built from run state, not the entry.

    The two paths through the inventory (:func:`_view_from_run_dir` versus
    :func:`_view_from_session`) derive their state in different functions, and a
    run that has committed state once — which is every run more than a minute old
    — only ever takes the first.
    """
    from lmer_platform.workrepo import RunDirRef

    host, project = "gitlab.example.com", "agents/global"
    path = tmp_path / host / project / "runs" / slug
    path.mkdir(parents=True, exist_ok=True)
    (path / "state.yaml").write_text(
        'schema: 1\nstatus: "in-progress"\n', encoding="utf-8"
    )
    return RunDirRef(host=host, project=project, slug=slug, path=path)


def test_a_run_whose_session_nobody_can_see_is_detached(tmp_path):
    ref = _run_dir(tmp_path)
    run = inventory.build_inventory(
        [ref], [_entry(output=reattach.OUTPUT_NONE, detail="no answer")]
    ).runs[0]

    assert run.state == "detached"
    assert run.live is True, "the process is there; that is the whole problem"


def test_a_run_being_read_over_the_control_plane_is_still_running(tmp_path):
    ref = _run_dir(tmp_path)
    run = inventory.build_inventory(
        [ref], [_entry(output=reattach.OUTPUT_CONTROL_PLANE, detail="recovered")]
    ).runs[0]

    assert run.state == "running"


def test_a_detached_run_can_still_be_asked_a_question(tmp_path):
    """Two axes (spec D23): the ask channel is a mount, not the control plane."""
    ref = _run_dir(tmp_path)
    inv = inventory.build_inventory(
        [ref],
        [_entry(output=reattach.OUTPUT_NONE, detail="no answer")],
        questions={"s-1": [{"id": "q1", "text": "which repo?"}]},
    )
    run = inv.runs[0]

    assert run.state == "detached"
    assert run.attention.reason == "live_question"


def test_a_session_nobody_can_see_is_detached_not_running():
    run = inventory.build_inventory(
        [], [_entry(output=reattach.OUTPUT_NONE, detail="no answer")]
    ).runs[0]

    assert run.state == "detached"
    assert run.live is True, "the process is there; that is the whole problem"
    assert run.to_dict()["detached"]["detail"] == "no answer"


def test_a_session_being_read_over_the_control_plane_is_still_running():
    """The seam is a fact about the log, not a statement about now."""
    run = inventory.build_inventory(
        [], [_entry(output=reattach.OUTPUT_CONTROL_PLANE, detail="recovered")]
    ).runs[0]

    assert run.state == "running"
    assert run.to_dict()["detached"]["output"] == reattach.OUTPUT_CONTROL_PLANE


def test_a_session_recording_itself_is_still_running():
    """The T78 skip must not read as blindness in the fleet view: a session whose
    own supervisor writes the log of record (T71) is being recorded *without*
    this daemon's help, which is the opposite of a session nobody can see."""
    run = inventory.build_inventory(
        [], [_entry(output=reattach.OUTPUT_SESSION_LOG, detail="records itself")]
    ).runs[0]

    assert run.state == "running"
    assert run.to_dict()["detached"]["output"] == reattach.OUTPUT_SESSION_LOG


# --- the ending nothing here reaps -------------------------------------------

def test_a_reattached_session_that_finished_stops_reading_as_a_crash(
    platform_root, plane
):
    """The known limit this module documents, and where it now stops.

    Nothing here reaps a re-attached session: the ``_watch`` thread that removes a
    cleanly-exited entry died with the old daemon, and the drain never had the exit
    code — so the stale entry is left as evidence of *something*, which reads as
    ``crashed``. What tells the two endings apart afterwards is the detection tick's
    sweep, on the strength of the run's own committed state and not a guess, and it
    is pinned from this side because this module's docstring makes the claim.
    """
    from lmer_platform import config as cfg
    from lmer_platform import detect

    config = cfg.load()
    session_id = "s-reattached"
    plant_session(session_id, port=plane.port)
    assert reattach.reattach_session(session_id, poll=FAST_POLL).detached is True

    # The session then finishes, having marked its run complete and pushed. Nothing
    # was waiting on the process, so its entry stays exactly as it was.
    registry.update(
        session_id,
        pid=DEAD_PID,
        run={"host": "gitlab.example.com", "project": "agents/global", "slug": "r1"},
    )
    assert fleet_state(session_id) == "crashed", "the documented symptom"

    run_dir = (
        config.mirror_path / "gitlab.example.com" / "agents/global" / "runs" / "r1"
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.yaml").write_text(
        'schema: 1\nstatus: "complete"\n', encoding="utf-8"
    )

    assert detect.sweep_finished_sessions(config) == [session_id]

    assert registry.read_session(session_id) is None
    assert inventory.build_inventory(
        [], registry.list_sessions(live_only=False)
    ).runs == [], "the reconciled session is still a row of its own"
    assert [
        e["data"]["exit_code"] for e in store.read_events()
        if e.get("type") == detect.SESSION_ENDED_UNWATCHED
    ] == [None], "the exit code was never knowable and must not be invented"


def test_an_ordinary_session_carries_no_detached_record():
    run = inventory.build_inventory([], [_entry()]).runs[0]

    assert run.state == "running"
    assert run.to_dict()["detached"] is None


def test_a_detached_session_can_still_be_asked_a_question():
    """Two axes (spec D23): the ask channel is a mount, not the control plane."""
    entry = _entry(output=reattach.OUTPUT_NONE, detail="no answer")
    inv = inventory.build_inventory(
        [], [entry], questions={"s-1": [{"id": "q1", "text": "which repo?"}]}
    )
    run = inv.runs[0]

    assert run.state == "detached"
    assert run.attention.reason == "live_question"


def test_a_hand_edited_detached_field_does_not_break_the_fleet_view():
    """Registry files are debugging artifacts people edit; one must not be fatal."""
    entry = _entry()
    entry["detached"] = "yes please"

    run = inventory.build_inventory([], [entry]).runs[0]
    assert run.state == "running"
    assert run.to_dict()["detached"] is None


def test_a_rescan_leaves_a_session_it_is_already_draining_reading_as_running(
    platform_root, plane
):
    """A second pass over a session this daemon is draining must not blind it.

    ``reattach_all`` is not only the startup path. The second pass used to leave
    the opening provisional mark — ``output: none``, written before anything is
    known — on the entry of a session whose drain was running and reading fine,
    and nothing re-marked it until that drain gave up. The fleet view then called a
    session the platform was successfully recording ``detached``, which is the
    original T36 symptom put back by the code that fixes it.
    """
    plant_session("s-1", port=plane.port)
    reattach.reattach_all(poll=FAST_POLL)
    plane.emit(b"WORKING")
    wait_for_log("s-1", b"WORKING")
    assert fleet_state("s-1") == "running"

    reattach.reattach_all(poll=FAST_POLL)

    assert reattach.active_drains() == ["s-1"], "the same drain is still the reader"
    assert fleet_state("s-1") == "running"
    assert detached_of("s-1")["output"] == reattach.OUTPUT_CONTROL_PLANE
    plane.emit(b"STILL-WORKING")
    wait_for_log("s-1", b"STILL-WORKING")


def test_a_rescan_still_reads_a_session_whose_plane_never_answered_as_detached(
    platform_root, plane
):
    """The other half: ``none`` here is the answer, not a stage on the way to one.

    What keeps a drained session ``running`` across a rescan is this daemon holding
    a drain for it, never the fact that a re-attach ran — a session whose control
    plane never answered has no reader at all, and a re-mark that could not tell
    the two apart would hide the one state T36 exists to show.
    """
    plant_session("s-1", port=plane.port)
    plane.stop()  # the container is gone; the host process lingers

    reattach.reattach_all(poll=FAST_POLL)
    reattach.reattach_all(poll=FAST_POLL)

    assert reattach.active_drains() == [], "nothing is reading this session"
    assert detached_of("s-1")["output"] == reattach.OUTPUT_NONE
    assert fleet_state("s-1") == "detached"


def test_a_rescan_cannot_call_a_drain_that_has_already_given_up_a_reader(
    platform_root, plane, monkeypatch
):
    """A drain missing from ``_ACTIVE`` has already had its final word.

    ``_give_up`` used to write ``output: none`` and leave the removal from
    ``_ACTIVE`` to the thread unwinding behind it. A rescan landing in that gap
    found a drain that had already stopped, marked ``control_plane`` on the
    strength of its presence, and left a session nothing was reading labelled as
    watched — the T36 symptom inverted, the entry claiming the *good* state, with
    nothing to correct it until the next pass.

    Milliseconds wide, so it is pinned by a held interleaving rather than by
    racing two threads on a box that may have one CPU: the rescan does not start
    until the give-up mark is on the entry, and the drain is not let go until the
    rescan has reached the ``_ACTIVE`` lookup. Both orders around that lock end
    blind; what this catches is the ordering that had no lock across it at all.
    """
    monkeypatch.setattr(reattach, "MAX_CONSECUTIVE_FAILURES", 1)
    plant_session("s-1", port=plane.port)
    reattach.reattach_session("s-1", poll=FAST_POLL)
    assert reattach.active_drains() == ["s-1"]

    real_mark, real_lock = reattach.mark_detached, reattach._ACTIVE_LOCK
    armed = threading.Event()            # the drain's next mark is its last
    gave_up = threading.Event()          # ...and it has landed on the entry
    rescan_arrived = threading.Event()   # the rescan is at the ``_ACTIVE`` lookup
    rescanned = {}

    def gated_mark(session_id, **fields):
        entry = real_mark(session_id, **fields)
        if armed.is_set() and threading.current_thread() is not rescanner:
            # The give-up mark is written. Hold the drain right here — still inside
            # whatever it holds to write it — until the rescan is at the door.
            gave_up.set()
            rescan_arrived.wait(timeout=30)
        return entry

    class GatedLock:
        """The real lock, announcing the rescan's arrival before it blocks on it."""

        def __enter__(self):
            if threading.current_thread() is rescanner:
                rescan_arrived.set()
            return real_lock.__enter__()

        def __exit__(self, *exc_info):
            return real_lock.__exit__(*exc_info)

    def rescan():
        rescanned["report"] = reattach.reattach_session("s-1", poll=FAST_POLL)

    rescanner = threading.Thread(target=rescan, name="rescan")
    monkeypatch.setattr(reattach, "mark_detached", gated_mark)
    monkeypatch.setattr(reattach, "_ACTIVE_LOCK", GatedLock())

    armed.set()                 # before the plane breaks: the gate must not be missed
    plane.output_status = 500   # the drain's next poll fails, and one is its cap
    plane.healthz_status = 500  # the whole plane is down, so the probe path is blind
    assert gave_up.wait(timeout=30), "the drain never gave up"

    rescanner.start()
    rescanner.join(timeout=30)

    assert not rescanner.is_alive(), "the rescan never finished"
    assert rescanned["report"].draining is False, (
        "the drain had already spoken, so the rescan had to ask the container "
        "itself — which is what tells it there is no reader left"
    )
    assert reattach.active_drains() == []
    assert detached_of("s-1")["output"] == reattach.OUTPUT_NONE
    assert fleet_state("s-1") == "detached"


def test_a_rescans_mark_does_not_outlive_the_drain_it_spoke_for(
    platform_root, plane, monkeypatch
):
    """The other order round the lock, and why nothing is lost in it.

    A rescan that reaches the lookup while the drain is still registered marks
    ``control_plane``, which is true at the moment it is written. The drain's own
    ``none`` comes afterwards and replaces it, so the state the operator is left
    with is the drain's, not the rescan's — the guarantee that makes it safe for a
    rescan to trust a drain it finds in ``_ACTIVE``.
    """
    monkeypatch.setattr(reattach, "MAX_CONSECUTIVE_FAILURES", 1)
    plant_session("s-1", port=plane.port)
    reattach.reattach_session("s-1", poll=FAST_POLL)
    plane.emit(b"WORKING")
    wait_for_log("s-1", b"WORKING")

    second = reattach.reattach_session("s-1", poll=FAST_POLL)
    assert second.draining is True
    assert detached_of("s-1")["output"] == reattach.OUTPUT_CONTROL_PLANE

    plane.output_status = 500  # the plane stops answering under the running drain
    wait_for(
        lambda: detached_of("s-1")["output"] == reattach.OUTPUT_NONE,
        what="the drain's give-up mark replacing the rescan's",
    )
    wait_for(lambda: reattach.active_drains() == [], what="the drain thread exiting")
    assert fleet_state("s-1") == "detached"


def test_the_log_route_tells_a_terminal_its_scrollback_has_a_seam(
    platform_root, plane
):
    from fastapi.testclient import TestClient

    from lmer_platform import api
    from lmer_platform import config as cfg

    secret = "test-secret-value"
    client = TestClient(
        api.create_app(cfg.load(), secret, state_builder=lambda config, **kw: {})
    )
    headers = {"Authorization": f"Bearer {secret}"}

    plant_session("s-1", port=plane.port, log=b"before")
    assert client.get("/api/sessions/s-1/log", headers=headers).json()["detached"] is None

    reattach.reattach_session("s-1", poll=FAST_POLL)
    payload = client.get("/api/sessions/s-1/log", headers=headers).json()

    assert payload["detached"]["output"] == reattach.OUTPUT_CONTROL_PLANE
    assert payload["live"] is True
