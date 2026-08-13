"""Tests for the platform's end of a live session's ask channel (T23).

Two things are being kept apart here, and most of these tests are about the
seam: answering a run that **stopped** on a question respawns it
(:mod:`lmer_platform.answer`, tested in ``test_platform_answer.py``), while
answering a session that is **still running** writes a file into a directory the
container is polling. Nothing is spawned, nothing is typed into a terminal, and
the two must never be reachable through each other's route.

Every answer therefore needs a **live reader** to be written at all, and that is
three facts rather than one (:func:`lmer_platform.ask._reader_state`): a registry
entry, a live pid, and a container that answers. So most tests here run against a
real loopback control plane (:class:`HealthPlane`) — the third leg is an HTTP
request, and a stubbed seam would pass whether or not the refusal consults a
container at all.
"""

import base64
import json
import re
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ask_channel import protocol
from lmer_platform import api, ask, inventory, registry, spawn, store
from lmer_platform import config as cfg
from tests.conftest import strip_lmer_env
from tests.test_platform_inventory import plant_run, session, state_yaml

SECRET = "test-secret-value"

CONTROL_TOKEN = "ask-channel-control-plane-token"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


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


class _Handler(BaseHTTPRequestHandler):
    """``/healthz`` answered the way a session's supervisor answers it."""

    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
        plane = self.server.plane
        plane.calls.append(self.path)
        status = plane.healthz_status
        payload = (
            {"ok": True, "cursor": 0, "rows": 24, "cols": 80} if status == 200
            else {"detail": "no"}
        )
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):
        """Silence — the fake plane's access log is noise in test output."""


class HealthPlane:
    """One session's control plane, on loopback, answering health probes.

    Real HTTP because the third leg of "is there a live reader" is a real request
    into a container: a monkeypatched seam would let the refusals here pass whether
    or not anything is ever asked. :meth:`stop` is how a test says "the container is
    gone" — a closed port is what the platform actually meets during teardown.
    """

    def __init__(self):
        self.calls = []
        self.healthz_status = 200
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.plane = self
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self._thread.start()
        self._stopped = False

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        self._server.shutdown()
        self._server.server_close()


#: The control plane of the session this test planted. Module-level because the
#: helpers below are plain functions called from dozens of tests, and threading a
#: fixture through every one of them would say nothing that this comment does not.
_PLANE = None


@pytest.fixture(autouse=True)
def plane():
    global _PLANE
    server = HealthPlane()
    _PLANE = server
    yield server
    _PLANE = None
    server.stop()


def live_session(platform_root=None, session_id="s-20260727-aaaa", **overrides):
    """Register a session with a live pid *and* a control plane that answers.

    Both, because both are what makes a session's channel answerable: the pid is
    the host-side ``lmer``, which outlives its container by however long teardown
    takes, so a reply is only written while something inside still answers. A test
    that wants the teardown window stops the plane; one that wants a crash passes a
    dead ``pid``.

    ``platform_root`` is unused and taken positionally so the fixture that patches
    the state dir is ordered before this — the same shape it had before there was a
    control plane to wire up.
    """
    import os

    payload = {
        "kind": "worker",
        "pid": os.getpid(),
        "run": {
            "host": "gitlab.example.com",
            "project": "agents/global",
            "slug": "develop-141",
        },
        "task": {"taskdef": "develop", "target": "issue-141"},
        "control": {
            "host": "127.0.0.1",
            "port": _PLANE.port,
            "token_ref": str(spawn.token_file_for(session_id)),
        },
        "log_path": str(store.logs_dir() / f"{session_id}.log"),
        "started_at": "2026-07-27T09:00:00Z",
    }
    payload.update(overrides)
    registry.register(session_id, **payload)
    spawn.token_file_for(session_id).write_text(CONTROL_TOKEN, encoding="utf-8")
    log = store.logs_dir() / f"{session_id}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(b"")
    return session_id


def channel_for(session_id):
    """The channel directory, and a live reader for it unless one is planted.

    An answer needs the channel *and* a reader (see the module docstring), and the
    ordinary case is a session that has both — so this fills in the reader rather
    than leaving every round-trip test to say so. A test about a *missing* reader
    registers its own entry first (or calls ``ask.prepare_ask_dir`` directly, for
    the case where there is no entry at all), and this leaves that alone.
    """
    if registry.read_session(session_id) is None:
        live_session(session_id=session_id)
    return ask.prepare_ask_dir(session_id)


# --- the directory ------------------------------------------------------------

def test_the_channel_lives_beside_the_pty_log(platform_root):
    directory = ask.session_ask_dir("s-20260727-aaaa")
    assert directory.parent == store.logs_dir()
    assert directory.name.endswith(ask.SESSION_DIR_SUFFIX)


def test_the_channel_is_private_to_this_user(platform_root):
    """It is rw-mounted into a container and holds what the operator answered."""
    directory = ask.prepare_ask_dir("s-20260727-aaaa")
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_an_unusable_channel_directory_is_reported_not_raised(platform_root, caplog):
    blocker = platform_root / "logs"
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("not a directory", encoding="utf-8")
    assert ask.prepare_ask_dir("s-20260727-aaaa") is None
    assert any("platform_ask_dir_unusable" in record.message for record in caplog.records)


def test_a_session_id_that_could_escape_the_directory_is_refused(platform_root):
    with pytest.raises(ask.ChannelNotFound):
        ask.session_ask_dir("../../etc")


def test_a_session_with_no_channel_reads_as_empty(platform_root):
    assert ask.read_entries("s-20260727-aaaa") == []
    assert ask.pending_questions("s-20260727-aaaa") == []


# --- reading and answering ----------------------------------------------------

def test_the_round_trip_a_live_session_makes(platform_root):
    session_id = "s-20260727-aaaa"
    directory = channel_for(session_id)
    question = protocol.post_question(directory, "which branch?", ["main", "prep"])

    assert [entry.id for entry in ask.pending_questions(session_id)] == [question.id]

    ask.answer_question(session_id, question.id, "prep")

    assert ask.pending_questions(session_id) == []
    # And the container's side of the same round trip.
    assert protocol.answer_for(directory, question).text == "prep"


def test_an_answer_is_bound_to_one_question(platform_root):
    session_id = "s-20260727-aaaa"
    directory = channel_for(session_id)
    first = protocol.post_question(directory, "delete the branch?")
    second = protocol.post_question(directory, "force push?")

    ask.answer_question(session_id, second.id, "no")

    assert protocol.answer_for(directory, first) is None
    assert protocol.answer_for(directory, second).text == "no"


def test_answering_twice_is_refused(platform_root):
    """The first answer stands, and the refusal says when it was given.

    The timestamp is the difference between "you already answered this" and the
    race message below — an operator who sees the second wants to know the first
    happened five minutes ago, not that they lost a photo finish.
    """
    session_id = "s-20260727-aaaa"
    directory = channel_for(session_id)
    question = protocol.post_question(directory, "ship it?")
    first = ask.answer_question(session_id, question.id, "yes")
    with pytest.raises(ask.AlreadyAnswered) as exc:
        ask.answer_question(session_id, question.id, "no")
    assert first["answered_at"] in str(exc.value)
    assert protocol.answer_for(directory, question).text == "yes"


def test_two_operators_answering_at_once_do_not_both_win(platform_root):
    """The window the pre-check cannot close: answered between read and write.

    Simulated by answering from "elsewhere" after the entry was read — which is
    what a second daemon thread does. ``os.link`` is what makes the loser lose.
    """
    session_id = "s-20260727-aaaa"
    directory = channel_for(session_id)
    question = protocol.post_question(directory, "ship it?")

    real_read_entry = protocol.read_entry
    raced = []

    def read_then_race(directory_, question_id):
        entry = real_read_entry(directory_, question_id)
        if not raced:
            raced.append(True)
            protocol.write_answer(directory_, entry, "the other operator's answer")
        return entry

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(protocol, "read_entry", read_then_race)
        with pytest.raises(ask.AlreadyAnswered, match="just now"):
            ask.answer_question(session_id, question.id, "mine")

    assert protocol.answer_for(directory, question).text == (
        "the other operator's answer"
    )


def test_the_history_records_the_answer_without_recording_its_text(platform_root):
    """An audit trail of ids, never of content.

    ``events.jsonl`` is append-only and nobody prunes it, so the answer's words
    landing there would outlive the run that asked — the same reasoning that
    keeps ``lmer_platform.answer`` from logging what an operator typed.
    """
    session_id = "s-20260727-aaaa"
    question = protocol.post_question(channel_for(session_id), "which branch?")
    ask.answer_question(session_id, question.id, "the-secret-branch-name")

    events = store.read_events()
    answered = [e for e in events if e.get("type") == "session_question_answered"]
    assert answered, "answering a live session is worth a line of history"
    assert answered[0]["data"] == {"session": session_id, "question": question.id}
    assert "the-secret-branch-name" not in store.events_path().read_text(
        encoding="utf-8"
    )


def test_answering_an_unknown_question_is_a_404(platform_root):
    session_id = "s-20260727-aaaa"
    channel_for(session_id)
    with pytest.raises(ask.QuestionNotFound) as exc:
        ask.answer_question(session_id, "000042", "hello")
    assert exc.value.status == 404


def test_answering_a_session_with_no_channel_is_a_404(platform_root):
    with pytest.raises(ask.ChannelNotFound) as exc:
        ask.answer_question("s-20260727-aaaa", "000001", "hello")
    assert exc.value.status == 404


@pytest.mark.parametrize("question_id", [
    "../../../etc/passwd", "..", "0001/../0002", "abc", "",
])
def test_a_question_id_from_a_url_never_reaches_a_path(platform_root, question_id):
    """Refused at this boundary, in this layer's words, before any path join."""
    session_id = "s-20260727-aaaa"
    channel_for(session_id)
    with pytest.raises(ask.AskChannelError) as exc:
        ask.answer_question(session_id, question_id, "hello")
    assert exc.value.status == 400
    assert "invalid question id" in str(exc.value)


def test_a_malformed_entry_does_not_break_the_read(platform_root):
    session_id = "s-20260727-aaaa"
    directory = channel_for(session_id)
    good = protocol.post_question(directory, "readable")
    (directory / "000009.question.json").write_text("{ torn", encoding="utf-8")
    assert [entry.id for entry in ask.pending_questions(session_id)] == [good.id]


def test_one_unreadable_channel_does_not_break_the_fleet(platform_root, monkeypatch):
    sessions = [
        {"id": "s-good", "live": True},
        {"id": "s-bad", "live": True},
    ]
    channel_for("s-good")
    protocol.post_question(ask.session_ask_dir("s-good"), "still here?")

    real = ask.pending_questions

    def explode(session_id):
        if session_id == "s-bad":
            raise OSError("permission denied")
        return real(session_id)

    monkeypatch.setattr(ask, "pending_questions", explode)
    pending = ask.pending_by_session(sessions)
    assert list(pending) == ["s-good"]


def test_only_live_sessions_are_polled_for_questions(platform_root):
    """A dead session's question is a record: nothing is reading for the reply."""
    channel_for("s-dead")
    protocol.post_question(ask.session_ask_dir("s-dead"), "anyone?")
    assert ask.pending_by_session([{"id": "s-dead", "live": False}]) == {}


def test_a_detached_session_s_questions_stay_answerable(platform_root):
    """Two axes (spec D23): the *host PTY* is what detaching lost, not the reader.

    A session the daemon has gone blind to is still alive, still polling a directory
    this host mounted into it, and — since a re-attach is what proves that — still
    answering on its control plane. So its question stays on the attention list and
    its answer still lands. The close record is the session's own to write, which is
    why nothing on this side can take that away from it.
    """
    session_id = "s-detached"
    directory = channel_for(session_id)
    question = protocol.post_question(directory, "which repo did you mean?")
    entry = {
        "id": session_id, "live": True,
        "detached": {"output": "none", "reason": "daemon_restart"},
    }

    assert list(ask.pending_by_session([entry])) == [session_id]
    ask.answer_question(session_id, question.id, "agents/global")
    assert protocol.answer_for(directory, question).text == "agents/global"


# --- a question the session stopped waiting for -------------------------------
#
# `lmer-ask close` (T34): the answer-into-the-void case. The question outlived the
# wait that posted it, and the reply box on the operator's screen was the only
# thing that did not know.

def test_a_closed_question_leaves_the_attention_list_but_not_the_record(platform_root):
    session_id = "s-20260727-aaaa"
    directory = channel_for(session_id)
    question = protocol.post_question(directory, "which branch?")
    protocol.close_question(directory, question, reason="took the safe one")

    assert ask.pending_questions(session_id) == []
    (entry,) = ask.read_entries(session_id)
    assert entry.text == "which branch?", "the question is still what was asked"
    assert entry.closed is True
    assert entry.to_dict()["closure"]["reason"] == "took the safe one"


def test_answering_a_closed_question_is_refused(platform_root):
    """409 and nothing written: the operator is told, not quietly filed away."""
    session_id = "s-20260727-aaaa"
    directory = channel_for(session_id)
    question = protocol.post_question(directory, "which branch?")
    protocol.close_question(directory, question, reason="took the safe one")

    with pytest.raises(ask.QuestionClosed) as exc:
        ask.answer_question(session_id, question.id, "prep-release")

    assert exc.value.status == 409
    assert "took the safe one" in str(exc.value)
    assert not (directory / f"{question.id}{protocol.ANSWER_SUFFIX}").exists()


def test_an_answer_that_raced_the_close_is_still_the_answer(platform_root):
    """The race decision, on the read side: answered outranks closed.

    The close record is planted by hand because that pair only exists when a close
    and an answer cross — ``close_question`` refuses once an answer is on disk, and
    :func:`ask.answer_question` refuses once a close is. What must never happen is
    the operator's reply disappearing because the session closed a moment later.
    """
    session_id = "s-20260727-aaaa"
    directory = channel_for(session_id)
    question = protocol.post_question(directory, "ship it?")
    ask.answer_question(session_id, question.id, "yes, ship it")
    (directory / f"{question.id}{protocol.CLOSED_SUFFIX}").write_text(
        json.dumps({
            "question_id": question.id,
            "nonce": question.nonce,
            "closed_at": "2026-07-27T09:05:00Z",
            "reason": "gave up waiting",
        }),
        encoding="utf-8",
    )

    (entry,) = ask.read_entries(session_id)
    assert entry.answered and entry.closed
    assert entry.answer.text == "yes, ship it"
    assert entry.to_dict()["answered"] is True
    assert ask.pending_questions(session_id) == []
    # And the refusal a second operator gets names the answer, not the close:
    # what happened here is that this question was answered.
    with pytest.raises(ask.AlreadyAnswered):
        ask.answer_question(session_id, question.id, "no, hold on")


def test_a_question_from_an_older_image_is_answerable_forever(platform_root):
    """Mixed fleet: the old ``lmer-ask`` writes no state field and never will.

    The record is written by hand rather than posted, because the point is the
    bytes an older image leaves on disk — no closure, nothing that could be read
    as one — and this side has to treat that as "open" for as long as any such
    image exists.
    """
    session_id = "s-20260727-aaaa"
    directory = channel_for(session_id)
    (directory / f"000001{protocol.QUESTION_SUFFIX}").write_text(
        json.dumps({
            "schema": 1, "id": "000001", "kind": "question",
            "text": "which branch?", "options": [],
            "at": "2026-07-01T09:00:00Z", "nonce": "abc123",
        }),
        encoding="utf-8",
    )

    (entry,) = ask.read_entries(session_id)
    assert entry.closed is False
    assert entry.to_dict()["closure"] is None
    assert [pending.id for pending in ask.pending_questions(session_id)] == ["000001"]
    ask.answer_question(session_id, "000001", "prep-release")
    assert protocol.load_answer(directory, "000001").text == "prep-release"


def test_an_unreadable_close_record_keeps_the_question_answerable(platform_root):
    """The one asymmetry with a torn answer, and the direction is deliberate: a
    corrupt file must not be what stops an operator from replying."""
    session_id = "s-20260727-aaaa"
    directory = channel_for(session_id)
    question = protocol.post_question(directory, "which branch?")
    (directory / f"{question.id}{protocol.CLOSED_SUFFIX}").write_text(
        "{ torn", encoding="utf-8"
    )

    assert [entry.id for entry in ask.pending_questions(session_id)] == [question.id]
    ask.answer_question(session_id, question.id, "prep-release")
    assert protocol.answer_for(directory, question).text == "prep-release"


# --- a channel with nobody reading it -----------------------------------------
#
# The other answer-into-the-void case (T94), and the one nothing on screen used to
# admit: the question is still open and nothing is left to read the reply. Each
# session polls the channel directory named after it, so a reply filed here sits
# unread forever — a resume starts a session with a channel of its own and never
# sees this one.
#
# Three ways to have no reader, all refused, and the third is the one a live
# incident found (2026-07-29): a *live* pid whose container has already gone, which
# is the shape of every ordinary teardown for the minutes host-side `lmer` spends
# committing run state.

def dead_session(platform_root, **overrides):
    """A crashed session's registry entry: present, with a pid that cannot exist.

    Present is the point. A crash keeps the entry (``lmer_platform.spawn`` says
    why), which is what makes the death a fact this side can read.
    """
    return live_session(platform_root, pid=999999999, **overrides)


def test_answering_a_dead_session_s_question_is_refused(platform_root):
    """410 and nothing written, and the refusal says what to do instead."""
    session_id = dead_session(platform_root)
    directory = channel_for(session_id)
    question = protocol.post_question(directory, "which branch?")

    with pytest.raises(ask.SessionGone) as exc:
        ask.answer_question(session_id, question.id, "prep-release")

    assert exc.value.status == 410
    assert "resume the run" in str(exc.value).lower(), (
        "a refusal with no way forward leaves the operator holding a reply"
    )
    assert not (directory / f"{question.id}{protocol.ANSWER_SUFFIX}").exists()


def test_a_dead_pid_is_refused_without_asking_the_container(
    platform_root, plane, monkeypatch
):
    """Cheapest evidence first: the registry already said it.

    Not a micro-optimisation. The probe has a one-second budget and this path is an
    operator waiting on a button, so a refusal that could be read off a local file
    must not cost a round trip into a container that cannot be there — its pid is
    gone.

    Asserted at the seam ``ask`` reaches ``session_io`` through, because that is
    whose ordering decision this is. The plane's own log is checked as well and is
    belt-and-braces: :func:`session_io.control_endpoint` refuses a dead session
    before it reads a port, so no request would go out even if this order were
    reversed — and a test that pinned only *that* would pass with the reasoning here
    removed.
    """
    asked = []

    class _Stub:
        @staticmethod
        def control_plane_answers(session_id):
            asked.append(session_id)
            return True

    monkeypatch.setattr(ask, "_session_io", lambda: _Stub())
    session_id = dead_session(platform_root)
    question = protocol.post_question(channel_for(session_id), "which branch?")

    with pytest.raises(ask.SessionGone):
        ask.answer_question(session_id, question.id, "prep-release")

    assert asked == [], "a dead session's control plane was probed anyway"
    assert plane.calls == []


def test_answering_a_session_whose_container_has_gone_is_refused(platform_root, plane):
    """The teardown window, and the reason this check exists at all (2026-07-29).

    Everything the registry can see says yes: the entry is there and the pid — the
    host-side ``lmer``, which stays up for minutes finishing run-state commits — is
    alive. What is gone is the container, and with it the in-process ``lmer-ask``
    poll that was the only reader this channel ever had. Before the third leg, this
    wrote the answer, appended ``session_question_answered`` and returned 200 to an
    operator whose reply nobody would ever read.
    """
    session_id = live_session(platform_root)
    directory = channel_for(session_id)
    question = protocol.post_question(directory, "which branch?")
    plane.stop()

    with pytest.raises(ask.SessionGone) as exc:
        ask.answer_question(session_id, question.id, "prep-release")

    assert exc.value.status == 410
    assert not (directory / f"{question.id}{protocol.ANSWER_SUFFIX}").exists()
    assert [e for e in store.read_events()
            if e.get("type") == "session_question_answered"] == [], (
        "the history recorded a delivery that did not happen"
    )


def test_the_teardown_refusal_says_waiting_will_not_help(platform_root, plane):
    """Distinct words from the dead-session one, and honest about being final.

    "Shutting down" is the reading that most invites a retry in a minute, and a
    retry cannot work: what has gone is the poll, not the process, and the host
    process finishing its commits will not start reading for it. The run key is what
    the refusal names — that is what ``/api/runs/resume`` takes — and never a path.
    """
    session_id = live_session(platform_root)
    question = protocol.post_question(channel_for(session_id), "which branch?")
    plane.stop()

    with pytest.raises(ask.SessionGone) as exc:
        ask.answer_question(session_id, question.id, "prep-release")
    teardown = str(exc.value)

    assert "shutting down" in teardown
    assert "has exited" not in teardown, (
        "the two refusals read as one, so the operator cannot tell them apart"
    )
    assert "waiting will not help" in teardown.lower()
    assert "gitlab.example.com/agents/global/develop-141" in teardown
    assert str(store.logs_dir()) not in teardown, (
        "a refusal must name the run, not a host path"
    )


def test_a_reattached_session_still_accepts_answers(platform_root):
    """The path the third leg must not break (spec D23, T36).

    Registered, marked ``detached``, and answering on its control plane — which is
    how a re-attach found it in the first place, and how its output is being read
    (:mod:`lmer_platform.reattach`). Detaching cost this session its *host terminal*;
    the reader on the other side of the mount never went anywhere, and refusing here
    would turn a daemon restart into a channel nobody can reply through.
    """
    session_id = live_session(platform_root)
    registry.update(session_id, detached={
        "output": "control_plane", "reason": "daemon_restart",
    })
    directory = channel_for(session_id)
    question = protocol.post_question(directory, "which repo did you mean?")

    ask.answer_question(session_id, question.id, "agents/global")

    assert protocol.answer_for(directory, question).text == "agents/global"


def test_an_answered_question_on_a_dead_session_still_says_answered(platform_root):
    """The refusal order: what happened to the question beats what happened to the
    session.

    The answer is planted through the protocol rather than through this module
    because the pair only exists that way round — the session died *after* being
    answered. A second operator asking again must hear that the reply landed; "that
    session is gone" would read as if their colleague's answer went nowhere, and the
    session may well have acted on it before it died.
    """
    session_id = dead_session(platform_root)
    directory = channel_for(session_id)
    question = protocol.post_question(directory, "ship it?")
    answer = protocol.write_answer(directory, question, "yes, ship it")

    with pytest.raises(ask.AlreadyAnswered) as exc:
        ask.answer_question(session_id, question.id, "no, hold on")

    assert answer.answered_at in str(exc.value)
    assert protocol.answer_for(directory, question).text == "yes, ship it"


def test_a_closed_question_on_a_dead_session_still_names_the_close(platform_root):
    """The same order one step down: the session's own record outranks the
    inference about its process, and the reason it gave is the more useful of two
    refusals that both mean "nothing is listening"."""
    session_id = dead_session(platform_root)
    directory = channel_for(session_id)
    question = protocol.post_question(directory, "which branch?")
    protocol.close_question(directory, question, reason="took the safe one")

    with pytest.raises(ask.QuestionClosed) as exc:
        ask.answer_question(session_id, question.id, "prep-release")

    assert "took the safe one" in str(exc.value)


def test_an_unknown_question_on_a_dead_session_is_still_a_404(platform_root):
    """Gone is about the reader, not about which ids exist on the channel."""
    session_id = dead_session(platform_root)
    channel_for(session_id)
    with pytest.raises(ask.QuestionNotFound):
        ask.answer_question(session_id, "000042", "hello")


def test_a_channel_with_no_registry_entry_is_refused(platform_root):
    """No entry is no reader, and this is the reversal T94 got backwards.

    It used to be answerable, on the reasoning that a missing file is not evidence
    of anything and housekeeping must not cost a reply. The missing file is the
    registry entry, though, and the *only* thing that removes one for a session that
    ended is the reaper recording that it ended (``spawn._watch``) — so "no entry"
    is precisely the ordinary end of a session, which is the one case where filing
    the reply is guaranteed to be a lie. A channel prepared before its session was
    registered is the other reading, and the window is milliseconds inside a spawn
    the operator cannot yet see a question from.

    The cost is one refusal for a reply that could never have been read anyway, and
    the operator keeps their text.
    """
    session_id = "s-never-registered"
    directory = ask.prepare_ask_dir(session_id)
    question = protocol.post_question(directory, "which branch?")

    with pytest.raises(ask.SessionGone) as exc:
        ask.answer_question(session_id, question.id, "prep-release")

    assert exc.value.status == 410
    assert "no longer registered" in str(exc.value)
    assert not (directory / f"{question.id}{protocol.ANSWER_SUFFIX}").exists()


def test_the_protocol_layer_is_still_free_to_plant_an_answer(platform_root):
    """The bypass fixtures rely on, and it must stay a bypass.

    ``ask_channel.protocol`` is the *format*, shared with the container-side CLI,
    and it knows nothing about registries or readers — which is what lets
    ``tests/test_platform_conversation_merge.py`` build "this channel already
    carries an answer" against a session that is long gone. Every refusal in this
    module is on the platform's own write path, and none of them is enforced one
    layer down.
    """
    directory = ask.prepare_ask_dir("s-never-registered")
    question = protocol.post_question(directory, "which branch?")

    protocol.write_answer(directory, question, "planted at the protocol layer")

    assert protocol.answer_for(directory, question).text == (
        "planted at the protocol layer"
    )


# --- the fleet view -----------------------------------------------------------

def test_a_live_session_asking_is_running_and_needs_you():
    """Both axes at once (spec D23) — the case the two-axis model exists for."""
    entry = {
        "id": "s-1", "live": True,
        "run": {"host": "gitlab.example.com", "project": "agents/global", "slug": "s"},
    }
    questions = {"s-1": [{"id": "000001", "text": "which branch?", "at": "T"}]}
    inv = inventory.build_inventory([], [entry], questions=questions)
    (run,) = inv.runs
    assert run.state == "running"
    assert run.attention.reason == "live_question"
    assert run.attention.note == "which branch?"
    assert run.questions[0]["id"] == "000001"


def test_a_run_dir_row_surfaces_its_live_session_s_question(tmp_path):
    """The same fact through the other row builder — a run with committed state.

    ``_view_from_session`` covers the first minutes before the first
    ``work commit``; this is every minute after it, and the two are separate code
    paths that have to agree.
    """
    ref = plant_run(tmp_path, "develop-141", state_yaml=state_yaml(status="in-progress"))
    entry = session("develop-141")
    inv = inventory.build_inventory(
        [ref], [entry],
        questions={entry["id"]: [{"id": "000001", "text": "which branch?", "at": "T"}]},
    )
    (run,) = inv.runs
    assert run.state == "running"
    assert run.attention.reason == "live_question"
    assert run.questions[0]["text"] == "which branch?"


def test_a_live_ask_outranks_a_stale_question_stop(tmp_path):
    """Liveness beats committed state (spec D24), and the two must not merge.

    A run whose last commit recorded ``stop_reason=question`` and which is now
    running again — the ordinary shape after an answer — must present the *live*
    question, not offer to respawn a run that already has a container.
    """
    ref = plant_run(
        tmp_path, "develop-141",
        state_yaml=state_yaml(status="in-progress", stop_reason="question",
                              open_question="the stale one"),
    )
    entry = session("develop-141")
    inv = inventory.build_inventory(
        [ref], [entry],
        questions={entry["id"]: [{"id": "000002", "text": "the live one", "at": "T"}]},
    )
    (run,) = inv.runs
    assert run.attention.reason == "live_question"
    assert run.attention.note == "the live one"


def test_a_live_question_outranks_a_stopped_one():
    """The session is up and idle, holding a slot; the stopped run is not."""
    assert (
        inventory.ATTENTION_PRIORITY["live_question"]
        < inventory.ATTENTION_PRIORITY["question"]
    )


def test_several_open_questions_say_so_in_the_row():
    entry = {
        "id": "s-1", "live": True,
        "run": {"host": "gitlab.example.com", "project": "agents/global", "slug": "s"},
    }
    questions = {"s-1": [
        {"id": "000001", "text": "first", "at": "T"},
        {"id": "000002", "text": "second", "at": "T"},
    ]}
    inv = inventory.build_inventory([], [entry], questions=questions)
    assert inv.runs[0].attention.note == "first (+1 more)"


def test_a_live_session_with_nothing_open_is_just_running():
    entry = {
        "id": "s-1", "live": True,
        "run": {"host": "gitlab.example.com", "project": "agents/global", "slug": "s"},
    }
    inv = inventory.build_inventory([], [entry], questions={"s-1": []})
    assert inv.runs[0].attention is None
    assert inv.runs[0].to_dict()["questions"] == []


def test_the_fleet_payload_carries_a_live_question(platform_root, config):
    session_id = live_session(platform_root)
    protocol.post_question(channel_for(session_id), "which branch?", ["main"])

    payload = api.build_state(config)
    (run,) = payload["runs"]
    assert run["attention"]["reason"] == "live_question"
    assert run["questions"][0]["options"] == ["main"]


# --- the routes ---------------------------------------------------------------

def test_the_ask_routes_require_auth(client, platform_root):
    session_id = live_session(platform_root)
    assert client.get(f"/api/sessions/{session_id}/ask").status_code == 401
    assert client.post(
        f"/api/sessions/{session_id}/ask/000001/answer", json={"answer": "x"}
    ).status_code == 401


def test_reading_a_channel_over_the_api(client, platform_root):
    session_id = live_session(platform_root)
    directory = channel_for(session_id)
    protocol.post_note(directory, "cloning")
    protocol.post_question(directory, "which branch?", ["main", "prep"])

    reply = client.get(f"/api/sessions/{session_id}/ask", headers=auth())
    assert reply.status_code == 200
    body = reply.json()
    assert body["live"] is True
    assert [entry["kind"] for entry in body["entries"]] == ["note", "question"]
    assert body["entries"][1]["options"] == ["main", "prep"]
    assert body["entries"][1]["answered"] is False


def test_a_session_that_never_asked_anything_is_an_empty_channel(client, platform_root):
    session_id = live_session(platform_root)
    reply = client.get(f"/api/sessions/{session_id}/ask", headers=auth())
    assert reply.status_code == 200
    assert reply.json()["entries"] == []


def test_an_unknown_session_is_a_404(client, platform_root):
    reply = client.get("/api/sessions/s-nope/ask", headers=auth())
    assert reply.status_code == 404


def test_answering_over_the_api_lands_in_the_channel(client, platform_root):
    session_id = live_session(platform_root)
    directory = channel_for(session_id)
    question = protocol.post_question(directory, "which branch?")

    reply = client.post(
        f"/api/sessions/{session_id}/ask/{question.id}/answer",
        json={"answer": "prep-release"},
        headers=auth(),
    )
    assert reply.status_code == 200
    assert reply.json()["question_id"] == question.id
    assert protocol.answer_for(directory, question).text == "prep-release"


def test_the_answer_text_is_not_echoed_back(client, platform_root):
    session_id = live_session(platform_root)
    question = protocol.post_question(channel_for(session_id), "which branch?")
    reply = client.post(
        f"/api/sessions/{session_id}/ask/{question.id}/answer",
        json={"answer": "a secret-ish sentence"},
        headers=auth(),
    )
    assert "a secret-ish sentence" not in reply.text


def test_a_second_answer_over_the_api_is_a_409(client, platform_root):
    session_id = live_session(platform_root)
    question = protocol.post_question(channel_for(session_id), "ship?")
    url = f"/api/sessions/{session_id}/ask/{question.id}/answer"
    assert client.post(url, json={"answer": "yes"}, headers=auth()).status_code == 200
    second = client.post(url, json={"answer": "no"}, headers=auth())
    assert second.status_code == 409


def test_answering_a_closed_question_over_the_api_is_a_409(client, platform_root):
    """No route change needed for a new refusal — the status rides on it."""
    session_id = live_session(platform_root)
    directory = channel_for(session_id)
    question = protocol.post_question(directory, "ship?")
    protocol.close_question(directory, question, reason="stopped waiting")

    reply = client.post(
        f"/api/sessions/{session_id}/ask/{question.id}/answer",
        json={"answer": "yes"}, headers=auth(),
    )
    assert reply.status_code == 409
    assert "stopped waiting" in reply.json()["detail"]


def test_answering_a_dead_session_s_question_over_the_api_is_a_410(
    client, platform_root
):
    """A third refusal and still no route change — the status rides on it."""
    session_id = live_session(platform_root, pid=999999999)
    question = protocol.post_question(channel_for(session_id), "which branch?")

    reply = client.post(
        f"/api/sessions/{session_id}/ask/{question.id}/answer",
        json={"answer": "prep-release"}, headers=auth(),
    )
    assert reply.status_code == 410
    assert "resume the run" in reply.json()["detail"].lower()


def test_answering_a_session_in_teardown_over_the_api_is_a_410(
    client, platform_root, plane
):
    """The same status as a dead session over the route, and different words.

    One status because the operator's next move is identical, and the route needs no
    change for either — it carries whatever the refusal says, which is what keeps a
    new refusal from arriving as a 500.
    """
    session_id = live_session(platform_root)
    question = protocol.post_question(channel_for(session_id), "which branch?")
    plane.stop()

    reply = client.post(
        f"/api/sessions/{session_id}/ask/{question.id}/answer",
        json={"answer": "prep-release"}, headers=auth(),
    )
    assert reply.status_code == 410
    detail = reply.json()["detail"]
    assert "shutting down" in detail
    assert "resume the run" in detail.lower()


def test_the_channel_read_reports_the_closed_state(client, platform_root):
    session_id = live_session(platform_root)
    directory = channel_for(session_id)
    question = protocol.post_question(directory, "which branch?")
    protocol.close_question(directory, question, reason="took the safe one")

    body = client.get(f"/api/sessions/{session_id}/ask", headers=auth()).json()
    assert body["entries"][0]["closed"] is True
    assert body["entries"][0]["closure"]["reason"] == "took the safe one"
    assert body["entries"][0]["answered"] is False


def test_a_closed_question_drops_out_of_the_fleet_payload(platform_root, config):
    """The row said "is waiting on your reply"; it is not any more."""
    session_id = live_session(platform_root)
    directory = channel_for(session_id)
    question = protocol.post_question(directory, "which branch?")
    protocol.close_question(directory, question)

    payload = api.build_state(config)
    (run,) = payload["runs"]
    assert run["questions"] == []
    assert run["attention"] is None


@pytest.mark.parametrize("body", [{}, {"answer": None}, {"answer": 17}])
def test_an_answer_that_is_not_text_is_a_400(client, platform_root, body):
    session_id = live_session(platform_root)
    question = protocol.post_question(channel_for(session_id), "ship?")
    reply = client.post(
        f"/api/sessions/{session_id}/ask/{question.id}/answer",
        json=body, headers=auth(),
    )
    assert reply.status_code == 400


def test_an_empty_answer_is_refused(client, platform_root):
    session_id = live_session(platform_root)
    question = protocol.post_question(channel_for(session_id), "ship?")
    reply = client.post(
        f"/api/sessions/{session_id}/ask/{question.id}/answer",
        json={"answer": "   "}, headers=auth(),
    )
    assert reply.status_code == 400
    assert "empty" in reply.json()["detail"]


def test_a_traversing_question_id_over_the_api_is_refused(client, platform_root):
    session_id = live_session(platform_root)
    channel_for(session_id)
    reply = client.post(
        f"/api/sessions/{session_id}/ask/%2e%2e%2f%2e%2e%2fpasswd/answer",
        json={"answer": "x"}, headers=auth(),
    )
    assert reply.status_code in (400, 404)
    assert not list(store.logs_dir().glob("**/*passwd*"))


def test_the_route_list_keeps_the_two_answer_paths_apart(client):
    text = client.get("/api", headers=auth()).text
    assert "/api/sessions/{id}/ask" in text
    assert "No container is started." in text


def test_the_channel_survives_the_session(client, platform_root):
    """A record, not a request: exited sessions keep their channel readable."""
    session_id = live_session(platform_root, pid=999999999)
    directory = channel_for(session_id)
    protocol.post_question(directory, "asked before it died")

    body = client.get(f"/api/sessions/{session_id}/ask", headers=auth()).json()
    assert body["live"] is False
    assert body["entries"][0]["text"] == "asked before it died"


def test_an_operator_answer_is_readable_by_the_container_side(platform_root):
    """The two halves are one format: written by the host, read by the CLI.

    Not a restatement of the round-trip test above — this one asserts the file
    on disk is what ``ask_channel.protocol`` (running inside the container) reads,
    rather than what this module happens to return.
    """
    session_id = "s-20260727-aaaa"
    directory = channel_for(session_id)
    question = protocol.post_question(directory, "which branch?")
    ask.answer_question(session_id, question.id, "prep-release")

    raw = json.loads(
        (directory / f"{question.id}{protocol.ANSWER_SUFFIX}").read_text(encoding="utf-8")
    )
    assert raw["question_id"] == question.id
    assert raw["nonce"] == question.nonce
    assert protocol.wait_for_answer(
        directory, question, timeout=0.1, interval=0.01
    ).text == "prep-release"


# --- the UI ------------------------------------------------------------------
#
# Source-level, as everywhere else in this repo's web tests: there is no JS
# runner here (see tests/test_platform_web_app.py), and what has to hold is that
# the two answer paths never read as one thing on screen.

WEB = Path(__file__).resolve().parent.parent / "web"


def _component(name):
    return (WEB / "src" / "components" / name).read_text(encoding="utf-8")


def _chip_handler(source):
    """The body of the function a chip's tap is bound to, in AskBox.

    A tap does one of two things now — send, or join what is being written — so
    the pins below are on that one function rather than on the binding, and a
    chip rebound to something else stops them all here.
    """
    assert '@click="chooseOption(option)"' in source, (
        "the chips are no longer bound to the option handler"
    )
    body = source.partition("function chooseOption(option) {")[2]
    assert body, "AskBox must own what a tapped option does"
    return body.partition("\n}")[0]


def test_the_ask_components_exist():
    # Three since T40: the box that takes a reply, the dock that shows what is
    # waiting, and the record of everything the channel ever carried.
    for name in ("AskBox.vue", "AskChannel.vue", "AskHistory.vue"):
        assert (WEB / "src" / "components" / name).is_file(), f"missing {name}"


def test_the_reply_box_promises_no_container():
    """The one sentence that keeps it apart from AnswerBox a screen away."""
    source = _component("AskBox.vue")
    assert "nothing is started" in source
    assert "send reply" in source
    assert "starts a new session" not in source, (
        "that promise belongs to AnswerBox, where a container really does start"
    )


def test_the_two_boxes_are_reached_by_different_reasons():
    """One `attention.reason` each, so both can never be offered at once."""
    source = (WEB / "src" / "components" / "RunDetail.vue").read_text(encoding="utf-8")
    assert "reason === 'question'" in source
    assert "reason === 'live_question'" in source
    assert "AskChannel" in source and "AnswerBox" in source


def test_the_options_are_a_hint_not_a_menu():
    """D27: the chips are a shortcut past the box, never the only way in.

    A question whose options were the *whole* reply surface is a menu, and the
    session gets whichever of the agent's guesses was closest instead of the one
    sentence that was actually true.
    """
    source = _component("AskBox.vue")
    assert "<v-textarea" in source and 'label="your reply"' in source, (
        "the options are the only way to reply, which makes them a menu"
    )
    assert "or write anything else" in source


def test_tapping_an_option_sends_it():
    """From the operator's live pass: a chip that only filled the box left the
    reply one more tap away, on the one card where a session is blocked *now*.

    Through the same call the composer reaches, not a second one beside it. In
    flight, delivered and refused are all read off one send here, and a copy of it
    is how a chip ends up delivering a reply the card never admits it sent.
    """
    source = _component("AskBox.vue")
    handler = _chip_handler(source)
    assert "if (!draft.value.trim()) return send(option)" in handler, (
        "tapping an option does not send it"
    )
    assert "draft.value = option" not in handler, (
        "the chip still only fills the box, so the reply is a tap short of sent"
    )
    assert source.count("answerSessionQuestion(") == 1, (
        "two send paths; one of them will stop saying what happened to the reply"
    )
    assert "tap one to send it" in source, (
        "the copy still promises a tap fills the box"
    )


def test_tapping_an_option_while_composing_adds_it_instead():
    """The accommodation for an entry that asked three things at once.

    The prompt contract is one question per ask entry, and nothing on this side
    can enforce it, so the reply to a packed one is assembled out of chips and
    typing together. A chip that sent while the box had text in it would deliver
    a third of that answer and lose the rest, which exists nowhere but this card.
    """
    source = _component("AskBox.vue")
    handler = _chip_handler(source)
    assert "draft.value +=" in handler, "a tapped option cannot join the draft"
    assert handler.index("if (!draft.value.trim()) return send(option)") < handler.index(
        "draft.value +="
    ), "the send comes after the append, so an empty box gets text instead of a reply"
    assert "answerSessionQuestion" not in handler, (
        "the append path sends an answer of its own"
    )
    assert "or to add it to a reply in progress" in source, (
        "the copy still promises a send either way, so the append is undiscoverable"
    )


def test_an_added_option_is_separated_from_what_is_already_typed():
    """A space where the draft ends in none, and never a second one: the reply is
    read by an agent as the operator's own sentence."""
    handler = _chip_handler(_component("AskBox.vue"))
    assert "(/\\s$/.test(draft.value) ? '' : ' ')" in handler, (
        "an option is glued to the word before it, or padded when it is not"
    )


def test_the_chips_stay_armed_while_a_reply_is_assembled():
    """Every part of a packed answer can come from a chip, so the append must not
    take the send control's states — the second tap has nothing to be in flight
    behind, and a dead chip row leaves the rest to be typed out by hand."""
    handler = _chip_handler(_component("AskBox.vue"))
    appending = handler.partition("if (!draft.value.trim())")[2]
    assert "busy.value" not in appending and "sent.value" not in appending, (
        "the append path claims the in-flight states and disables the chips"
    )


def test_an_option_is_not_tappable_while_a_reply_is_in_flight():
    """The chips took on the send control's states along with its job.

    A second tap during the round trip is two replies posted for one question, and
    the second earns the refusal (409) — a red alert under a reply that was in fact
    delivered, which is the worst reading of this card there is.
    """
    source = _component("AskBox.vue")
    chips = source[source.index("<v-chip"):source.index("</v-chip>")]
    assert ':disabled="sent || busy"' in chips, (
        "the option chips stay armed while a reply is in flight"
    )


def test_an_option_is_legible_rather_than_washed_over():
    """The operator, from a live pass: "the chips (questions answers, port links) are
    quite hard to read, lets try variant outlined".

    A tonal chip is a wash of colour behind mid-emphasis text, and on a dark card the
    label is guessed at rather than read — which for an option is the worst possible
    place to be, since the label is the literal text a tap sends. Outlined draws the
    word in full-strength accent on the card's own surface and puts the tint's job on
    a border.

    A deliberate exception to the variant swept out of the rest of the app, and it
    stops at the chips: the send control in the field stays tonal (the composer
    guards keep that for all three composers, and keep `outlined` off every button).
    """
    source = _component("AskBox.vue")
    chips = source[source.index("<v-chip"):source.index("</v-chip>")]
    assert 'variant="outlined"' in chips, (
        "the option chips are back to a tint the operator could not read"
    )
    assert 'variant="flat"' not in source, (
        "flat is swept out of this app and stays swept out"
    )


def test_a_refused_option_lands_in_the_box():
    """The half of the kept-draft contract that a tapped option needs.

    A typed reply that is refused is still in the box it was typed in; a tapped one
    was never in the box, so unless the failure puts it there the operator is left
    with a red alert and nothing to retry from.
    """
    source = _component("AskBox.vue")
    _, sep, tail = source.partition('} catch (exc) {')
    assert sep, "AskBox must handle the server's refusal itself"
    handler = tail.partition('} finally {')[0]
    assert "draft.value = reply" in handler, (
        "a refused reply is not put back in the box, so a tapped option is lost"
    )


def test_the_chat_composer_warns_while_a_live_question_is_open():
    """The failure this feature exists for: keystrokes landing in a menu."""
    source = _component("Chat.vue")
    assert "askPending" in source
    assert "may be a menu rather than its prompt" in source


def test_the_channel_view_says_a_dead_session_cannot_be_replied_to():
    source = _component("AskChannel.vue")
    assert "nothing is reading the channel any more" in source


def test_the_channel_view_replaces_the_box_once_the_session_is_gone():
    """In place of the reply box, not underneath it.

    A warning under a working box is what let an operator send a reply into a
    directory nothing reads and be told it was delivered. The gate is the same
    liveness fact the server refuses on, and it is the prop the component already
    polls off.
    """
    source = _component("AskChannel.vue")
    # The gate is one computed since T40 — the dock draws a box for what is
    # `waiting`, and nothing is waiting on a session that has exited — so this is
    # where the liveness has to be, rather than in a template branch that another
    # one can grow beside it.
    assert "const waiting = computed(() => (props.live ? open.value : []))" in source, (
        "the reply box must be gated on the session being alive"
    )
    assert 'v-if="stranded"' in source and "const stranded = computed(" in source, (
        "and the sentence takes its place rather than joining it"
    )
    assert "Resume the run to continue it." in source


def test_a_dead_session_s_open_question_stays_in_the_record():
    """It loses its box, not its text: ten minutes on screen and then gone without
    a word is how an operator is left wondering what they missed.

    Two views of the channel since T40, and the property holds in both: the dock
    keeps showing the question as a record (it is not `waiting`, so it falls
    through to `recent`), and the history has every entry there ever was.
    """
    source = _component("AskChannel.vue")
    assert "!waiting.value.includes(entry)" in source, (
        "the dock must take in an open question once the session has exited"
    )
    # The label is written by the component both views draw an entry with (#274),
    # off the liveness the view hands it — so the dock passing `live` is the half
    # of it that lives here.
    assert ':live="live"' in source, (
        "the dock draws its entries without saying whether the session is alive"
    )
    assert "askEntryLabel(entry, live)" in _component("AskEntry.vue"), (
        "and it must not still be labelled as something you can answer"
    )
    # The word itself is in format.js, because two views render the same entries
    # and a channel that called one question "closed" here and "unanswered" there
    # would be two accounts of what happened to it.
    vocabulary = (WEB / "src" / "format.js").read_text(encoding="utf-8")
    assert "return live ? 'open' : 'unanswered'" in vocabulary, (
        "a question nothing is reading is still labelled as answerable"
    )
    assert ':live="live"' in _component("AskHistory.vue"), (
        "the record labels its entries with a vocabulary of its own"
    )


def test_a_refused_reply_keeps_what_the_operator_typed():
    """Any refusal — the 409s or the 410: the draft exists nowhere else yet."""
    source = _component("AskBox.vue")
    head, sep, tail = source.partition('} catch (exc) {')
    assert sep, "AskBox must handle the server's refusal itself"
    assert "draft.value = ''" in head
    assert "draft.value = ''" not in tail, (
        "clearing the draft on a refusal is the one way to lose the reply"
    )


def test_the_channel_view_shows_a_closed_question_as_unanswerable():
    """Not offered a reply box, and not silently gone either."""
    source = _component("AskChannel.vue")
    assert "!entry.answered && !entry.closed" in source, (
        "a closed question must not reach the reply box"
    )
    # Everything that is not waiting on an answer falls through to the dock's
    # record, so a closed question keeps its text and its timestamp rather than
    # vanishing off the page — and the history below has it either way.
    assert "const recent = computed(() => entries.value.filter(" in source, (
        "the dock no longer shows anything except what is waiting"
    )
    # The sentence belongs to the dock and is written in the shared entry
    # component, which draws it only for the view that asks (#274). The record does
    # not: it offers no box for any entry, so there is no absence to explain.
    assert "A reply can no longer reach it." in _component("AskEntry.vue")
    assert "unreachable" in source, (
        "the dock stopped asking for the line that says a reply cannot be "
        "delivered, so a closed question reads as one that could still be answered"
    )


def test_the_channel_view_leads_with_the_answer_when_both_landed():
    """The race decision on screen: answered before closed, as everywhere else."""
    vocabulary = (WEB / "src" / "format.js").read_text(encoding="utf-8")
    assert "if (entry.answered) return 'answered'" in vocabulary
    assert vocabulary.index("if (entry.answered)") < vocabulary.index(
        "if (entry.closed)"
    ), "a question that was answered as it was closed reads as closed"
    assert "entry.closed && !entry.answered" in _component("AskEntry.vue"), (
        "an entry says the session stopped waiting under an answer it got"
    )
    for name in ("AskChannel.vue", "AskHistory.vue"):
        assert "<AskEntry" in _component(name), (
            f"{name} draws a channel entry itself again, so the race decision "
            "above holds in one view and not in the other"
        )


def test_the_ui_labels_the_live_reason_differently():
    from lmer_platform.inventory import ATTENTION_REASONS

    source = (WEB / "src" / "format.js").read_text(encoding="utf-8")
    assert "live_question:" in source
    assert "live_question" in ATTENTION_REASONS
    labels = re.findall(r"^\s+(\w+): '([^']+)',", source, re.M)
    mapping = dict(labels)
    assert mapping["live_question"] != mapping["question"], (
        "the row must not read the same for a run that has exited and a session "
        "that is waiting"
    )
