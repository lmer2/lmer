"""``lmer-signal`` as the agent's one command (issue #407, T2), and the
``lmer-ask`` shim over it.

The CLI is exercised against a real supervisor app (the FastAPI routes of
``lmer_cli.supervisor`` over a real ``MessageLog``) through FastAPI's test
client, so what is pinned here is the whole path an agent's shell takes: verb →
HTTP → bus → stdout and exit code.
"""
from __future__ import annotations

import io
import json
import threading
import time

import pytest

from ask_channel import args as ask_args
from ask_channel import bus, cli as ask_cli, signal_cli
from lmer_cli import comms, supervisor
from tests.conftest import strip_lmer_env


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


class _Session:
    """A ``requests.Session`` stand-in over the FastAPI test client, which
    accepts no ``timeout`` (the bus client always passes one)."""

    def __init__(self, http):
        self._http = http

    def request(self, method, url, **kwargs):
        kwargs.pop("timeout", None)
        return self._http.request(method, url, **kwargs)


class Rig:
    """A supervisor app with a bus, and the CLI pointed at it."""

    def __init__(self, tmp_path, monkeypatch, *, with_bus=True):
        from fastapi.testclient import TestClient

        self.log = comms.MessageLog("sess-1", str(tmp_path / "messages.jsonl")) if with_bus else None
        self.sink: list[bytes] = []
        buf = supervisor.OutputBuffer(limit=1024)

        def write_input(data: bytes) -> int:
            self.sink.append(bytes(data))
            return len(data)

        self.app = supervisor._build_fastapi_app(buf, write_input, "test-token", comms=self.log)
        self.http = TestClient(self.app)
        endpoint = bus.Endpoint(url="http://testserver", token="test-token")
        self.client = bus.BusClient(endpoint, session=_Session(self.http))
        monkeypatch.setattr(signal_cli, "_client", lambda: self.client)

    def answer(self, question_seq: int, text: str, *, channel="slack", author="tester", ref="t1",
               delay=0.0, when_waiting=False):
        """Post an answer from another thread.

        *when_waiting* answers as soon as the CLI's poll is actually registered
        rather than after a fixed sleep: a wall-clock delay races the wait's own
        budget, and on a loaded machine the sleeping thread can be scheduled
        after the wait has already given up (the CI run of pipeline 1993).
        """
        def go():
            if when_waiting:
                deadline = time.monotonic() + 30.0
                while time.monotonic() < deadline:
                    with self.log._lock:
                        waiting = bool(self.log._waiters)
                    if waiting:
                        break
                    time.sleep(0.01)
            elif delay:
                time.sleep(delay)
            self.log.append(
                kind="answer", text=text, in_reply_to=str(question_seq),
                source=comms.Source(channel, author=author, ref=ref),
            )
        thread = threading.Thread(target=go)
        thread.start()
        return thread

    def records(self):
        return self.log.read_since(0)[0]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


def run(argv):
    return signal_cli.main(argv)


# ---------------------------------------------------------------------------
# argv shape
# ---------------------------------------------------------------------------


class TestArgv:
    @pytest.mark.parametrize("argv, expected", [
        (["pushed MR !1"], ["milestone", "pushed MR !1"]),
        (["--message-file", "/tmp/m"], ["milestone", "--message-file", "/tmp/m"]),
        (["-"], ["milestone", "-"]),
        (["note", "x"], ["note", "x"]),
        (["ask", "q?"], ["ask", "q?"]),
        (["--help"], ["--help"]),
        ([], []),
    ])
    def test_bare_text_is_a_milestone(self, argv, expected):
        assert signal_cli.normalize_argv(argv) == expected

    def test_help_names_every_verb(self, capsys):
        with pytest.raises(SystemExit):
            run(["--help"])
        out = capsys.readouterr().out
        for verb in signal_cli.VERBS:
            assert verb in out

    def test_no_argument_prints_usage(self, capsys):
        assert run([]) == 1
        assert "usage" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# posting verbs
# ---------------------------------------------------------------------------


class TestPosting:
    def test_bare_text_files_a_milestone_and_prints_its_id(self, rig, capsys):
        assert run(["pushed MR !167 for review"]) == 0
        assert capsys.readouterr().out.strip() == "000001"
        (record,) = rig.records()
        assert (record.kind, record.text, record.source.channel) == ("milestone", "pushed MR !167 for review", "agent")

    def test_a_milestone_whose_text_is_a_verb_name_needs_the_explicit_verb(self, rig, capsys):
        assert run(["milestone", "note"]) == 0
        (record,) = rig.records()
        assert (record.kind, record.text) == ("milestone", "note")

    def test_note_and_post_are_their_own_kinds(self, rig, capsys):
        assert run(["note", "working on it"]) == 0
        assert run(["post", "here is the result"]) == 0
        out = capsys.readouterr().out.split()
        assert out == ["000001", "000002"]
        kinds = [r.kind for r in rig.records()]
        assert kinds == ["note", "message"]

    def test_text_from_stdin_and_file_is_verbatim(self, rig, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO("pushed `fix/T122` — $HOME safe\n"))
        assert run(["--stdin"]) == 0
        path = tmp_path / "m.txt"
        path.write_text("from a file\nwith two lines\n", encoding="utf-8")
        assert run(["note", "--message-file", str(path)]) == 0
        first, second = rig.records()
        assert first.text == "pushed `fix/T122` — $HOME safe"
        assert second.text == "from a file\nwith two lines"

    @pytest.mark.parametrize("argv", [
        ["note"], ["note", "a", "--stdin"], ["note", "a", "--message-file", "/x"],
    ])
    def test_ambiguous_or_missing_text_is_an_error(self, rig, argv, capsys):
        assert run(argv) == 1
        assert "exactly once" in capsys.readouterr().err
        assert rig.records() == []

    def test_an_oversized_milestone_is_refused_by_the_bus(self, rig, capsys):
        assert run(["m" * 1001]) == 1
        assert "1000 limit" in capsys.readouterr().err

    def test_attach_needs_an_upload_store(self, rig, tmp_path, capsys, monkeypatch):
        payload = tmp_path / "shot.png"
        payload.write_bytes(b"\x89PNG\r\n\x1a\n")
        monkeypatch.delenv(signal_cli.UPLOADS_DIR_ENV, raising=False)
        assert run(["post", "look", "--attach", str(payload)]) == 1
        assert "upload store" in capsys.readouterr().err
        store = tmp_path / "uploads"
        store.mkdir()
        monkeypatch.setenv(signal_cli.UPLOADS_DIR_ENV, str(store))
        assert run(["post", "look", "--attach", str(payload)]) == 0
        (record,) = rig.records()
        (attachment,) = record.attachments
        assert attachment["name"] == "shot.png" and attachment["media_type"] == "image/png"
        assert attachment["bytes"] == 8 and attachment["path"].startswith(str(store))
        assert len(list(store.iterdir())) == 1


# ---------------------------------------------------------------------------
# ask / wait / close
# ---------------------------------------------------------------------------


class TestAsk:
    def test_no_wait_prints_the_id_and_records_the_options(self, rig, capsys):
        assert run(["ask", "which branch?", "--option", "main", "--option", "prep", "--no-wait"]) == 0
        assert capsys.readouterr().out.strip() == "000001"
        (record,) = rig.records()
        assert (record.kind, record.options) == ("question", ("main", "prep"))

    def test_the_first_answer_from_any_channel_lands_on_stdout(self, rig, capsys):
        thread = rig.answer(1, "prep-release", when_waiting=True)
        rc = run(["ask", "which branch?", "--timeout", "30", "--interval", "0.1"])
        thread.join()
        assert rc == 0
        captured = capsys.readouterr()
        assert captured.out == "prep-release\n"
        assert "answered from slack · tester" in captured.err
        question, answer = rig.records()
        # The CLI was waiting, so the supervisor handed it over instead of
        # typing it into the harness.
        assert answer.meta["delivered"] == comms.DELIVERY_CLI
        assert rig.sink == []

    def test_a_timeout_is_its_own_exit_code_and_teaches_the_loop(self, rig, capsys):
        assert run(["ask", "anyone?", "--timeout", "0.3", "--interval", "0.1"]) == ask_args.TIMEOUT_EXIT_CODE
        err = capsys.readouterr().err
        assert "lmer-signal wait 000001" in err
        assert "lmer-signal close 000001" in err
        assert "HARD RULE" in err and "monitor or watch tool" in err
        assert rig.log.question("1").open

    def test_wait_resumes_and_finds_an_answer_that_arrived_meanwhile(self, rig, capsys):
        assert run(["ask", "q?", "--no-wait"]) == 0
        rig.answer(1, "yes").join()
        assert run(["wait", "000001"]) == 0
        assert capsys.readouterr().out.strip().splitlines()[-1] == "yes"

    def test_wait_accepts_the_id_in_any_spelling(self, rig, capsys):
        assert run(["ask", "q?", "--no-wait"]) == 0
        rig.answer(1, "yes").join()
        for ref in ("1", "000001", "sess-1/000001"):
            assert run(["wait", ref]) == 0
        assert capsys.readouterr().out.strip().splitlines()[-3:] == ["yes"] * 3

    def test_a_late_answer_is_not_the_answer(self, rig, capsys):
        assert run(["ask", "q?", "--no-wait"]) == 0
        rig.answer(1, "first", ref="t1").join()
        rig.answer(1, "second", channel="matrix", author="@a:x", ref="e1").join()
        assert run(["wait", "1"]) == 0
        assert capsys.readouterr().out.strip().splitlines()[-1] == "first"

    def test_waiting_on_an_unknown_or_closed_question_is_an_error(self, rig, capsys):
        assert run(["wait", "7"]) == 1
        assert run(["ask", "q?", "--no-wait"]) == 0
        assert run(["close", "1"]) == 0
        assert run(["wait", "1"]) == 1
        assert "was closed" in capsys.readouterr().err

    def test_a_wait_that_would_spin_is_refused_before_posting(self, rig, capsys):
        assert run(["ask", "q?", "--interval", "0"]) == 1
        assert run(["ask", "q?", "--timeout", "-1"]) == 1
        assert rig.records() == []

    def test_close_withdraws_the_question(self, rig, capsys):
        assert run(["ask", "q?", "--no-wait"]) == 0
        assert run(["close", "1", "--reason", "took the safe branch"]) == 0
        state = rig.log.question("1")
        assert state.state == comms.STATE_CLOSED
        close = rig.records()[-1]
        assert (close.kind, close.text, close.in_reply_to) == ("close", "took the safe branch", "sess-1/000001")
        assert run(["close", "1"]) == 0
        assert "already closed" in capsys.readouterr().err

    def test_an_answer_that_beat_the_close_is_handed_over(self, rig, capsys):
        assert run(["ask", "q?", "--no-wait"]) == 0
        rig.answer(1, "prep").join()
        assert run(["close", "1"]) == 0
        captured = capsys.readouterr()
        assert captured.out.strip().splitlines()[-1] == "prep"
        assert "answered before it could be closed" in captured.err
        assert rig.log.question("1").state == comms.STATE_ANSWERED

    def test_closing_an_unknown_question_is_an_error(self, rig, capsys):
        assert run(["close", "9"]) == 1


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


class TestList:
    def test_list_shows_every_record_with_its_source_and_state(self, rig, capsys):
        run(["note", "working"])
        run(["ask", "branch?", "--option", "main", "--no-wait"])
        rig.log.append(kind="message", text="hi there", source=comms.Source("slack", author="tester", ref="t9"))
        rig.answer(2, "main").join()
        assert run(["list"]) == 0
        out = capsys.readouterr().out
        assert "[000001] note from agent: working" in out
        assert "[000002] question from agent (answered): branch?" in out
        assert "    options: main" in out
        assert "[000003] message from slack · tester: hi there" in out
        assert "[000004] answer from slack · tester → #2: main" in out

    def test_list_open_only_and_since(self, rig, capsys):
        run(["ask", "a?", "--no-wait"])
        run(["ask", "b?", "--no-wait"])
        rig.answer(1, "x").join()
        assert run(["list", "--open"]) == 0
        out = capsys.readouterr().out
        assert "b?" in out and "a?" not in out
        assert run(["list", "--since", "2"]) == 0
        out = capsys.readouterr().out
        assert "answer" in out and "a?" not in out

    def test_list_json_is_one_record_per_line(self, rig, capsys):
        run(["note", "one"])
        run(["post", "two"])
        capsys.readouterr()
        assert run(["list", "--json"]) == 0
        lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
        assert [line["kind"] for line in lines] == ["note", "message"]
        assert lines[0]["schema"] == comms.SCHEMA_VERSION


# ---------------------------------------------------------------------------
# no channel
# ---------------------------------------------------------------------------


class TestNoChannel:
    def test_without_a_control_plane_in_the_environment(self, monkeypatch, capsys):
        monkeypatch.delenv(bus.PORT_ENV, raising=False)
        monkeypatch.delenv(bus.TOKEN_ENV, raising=False)
        assert run(["something happened"]) == ask_args.NO_CHANNEL_EXIT_CODE
        assert bus.PORT_ENV in capsys.readouterr().err

    def test_a_port_without_a_token(self, monkeypatch, capsys):
        monkeypatch.setenv(bus.PORT_ENV, "8701")
        monkeypatch.delenv(bus.TOKEN_ENV, raising=False)
        assert run(["something happened"]) == ask_args.NO_CHANNEL_EXIT_CODE
        assert bus.TOKEN_ENV in capsys.readouterr().err

    def test_a_supervisor_without_a_bus(self, tmp_path, monkeypatch, capsys):
        Rig(tmp_path, monkeypatch, with_bus=False)
        assert run(["something happened"]) == ask_args.NO_CHANNEL_EXIT_CODE
        assert "predates" in capsys.readouterr().err

    def test_a_supervisor_that_does_not_answer(self, monkeypatch, capsys):
        monkeypatch.setenv(bus.PORT_ENV, "1")  # nothing listens on port 1
        monkeypatch.setenv(bus.TOKEN_ENV, "t")
        assert run(["something happened"]) == ask_args.NO_CHANNEL_EXIT_CODE
        assert "does not answer" in capsys.readouterr().err

    def test_the_codes_are_distinct(self):
        assert ask_args.NO_CHANNEL_EXIT_CODE not in (0, 1, ask_args.TIMEOUT_EXIT_CODE)

    def test_resolve_endpoint_prefers_an_explicit_url(self):
        endpoint = bus.resolve_endpoint({bus.URL_ENV: "http://sidecar:8700/", bus.TOKEN_ENV: "sekrit-token-value"})
        assert endpoint.url == "http://sidecar:8700"
        assert "sekrit-token-value" not in repr(endpoint)


def test_answer_available_before_first_poll_is_consumed_once(rig, monkeypatch, capsys):
    original = rig.client.post
    def post(kind, text, **kwargs):
        response = original(kind, text, **kwargs)
        if kind == "question":
            rig.client.post("answer", "prep-release", source={"channel": "platform"},
                            in_reply_to=response["message"]["id"])
        return response
    monkeypatch.setattr(rig.client, "post", post)
    assert run(["ask", "branch?", "--timeout", "5"]) == 0
    assert capsys.readouterr().out.strip() == "prep-release"
    assert rig.log._claims == {}
    now = time.monotonic()
    monkeypatch.setattr(comms.time, "monotonic", lambda: now + 1000)
    assert rig.log.lapsed_answers() == [], "answer printed on stdout must never be relayed later"


def test_failed_initial_poll_releases_wait_claim(rig, monkeypatch):
    question = rig.client.post("question", "branch?")["message"]
    def fail(*args, **kwargs):
        raise bus.BusError("test transport failure")
    monkeypatch.setattr(rig.client, "messages", fail)
    assert run(["wait", question["id"], "--timeout", "5"]) == 1
    assert rig.log._claims == {}


@pytest.mark.parametrize("argv, routed", [
    (["post", "Delivered reply"], True),
    (["ask", "Question?", "--no-wait"], True),
    (["ask", "Question?", "--timeout", "0.01", "--interval", "0.01"], True),
    (["note", "Quiet progress"], False),
    (["milestone", "Finished"], False),
])
def test_cli_delivery_receipt_reaches_the_guard(rig, capsys, argv, routed, routing_call):
    from hooks.slack_reply_guard import evaluate
    from tests.test_slack_reply_guard import LONG, _assistant_bash, _assistant_text, _tool_result, _user_text
    result = run(argv)
    output = capsys.readouterr()
    records = rig.records()
    assert len(records) == 1
    if routed:
        assert records[0].id in output.err
    events = [_user_text('request'), _assistant_bash('uninterpreted command', tool_id='call'),
              _tool_result(output.out + output.err, tool_use_id='call', is_error=result != 0),
              _assistant_text(LONG)]
    assert (evaluate(events) is None) == routed


def test_real_help_does_not_produce_a_delivery_receipt(rig, capsys, routing_call):
    from lmer_cli.routing_receipts import delivered
    with pytest.raises(SystemExit) as exited:
        run(['ask', '--help'])
    assert exited.value.code == 0
    output = capsys.readouterr()
    assert not delivered('call', 'claude-test')
    assert rig.records() == []
