"""Tests for the Codex Stop hook that resumes answered lmer-ask turns."""
from __future__ import annotations

import io
import json

import pytest

from ask_channel import protocol
from hooks import codex_ask_guard as guard
from lmer_cli.util import get_bool_env


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_inlined_channel_suffixes_match_protocol_source_of_truth():
    assert guard.QUESTION_SUFFIX == protocol.QUESTION_SUFFIX
    assert guard.ANSWER_SUFFIX == protocol.ANSWER_SUFFIX
    assert guard.CLOSED_SUFFIX == protocol.CLOSED_SUFFIX
    assert guard.READ_SUFFIX == protocol.READ_SUFFIX


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "1",
        "true",
        "TRUE",
        "yes",
        "0",
        "false",
        "no",
        "on",
        "off",
        "wat",
    ],
)
def test_env_flag_matches_shared_bool_parser(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("CODEX_GUARD_FLAG", raising=False)
    else:
        monkeypatch.setenv("CODEX_GUARD_FLAG", raw)
    assert guard.env_flag(raw) == get_bool_env("CODEX_GUARD_FLAG")


class TestChannelState:
    def test_unread_answers_are_oldest_first(self):
        names = [
            "000001.question.json",
            "000001.answer.json",
            "000002.question.json",
            "000002.answer.json",
            "000003.question.json",
            "000003.answer.json",
            "000003.read.json",
            "000004.question.json",
        ]
        assert guard.unread_answer_ids(names) == [1, 2]

    def test_closed_answer_is_still_unread(self):
        names = [
            "000001.question.json",
            "000001.answer.json",
            "000001.closed.json",
        ]
        assert guard.unread_answer_ids(names) == [1]

    @pytest.mark.parametrize("sidecar", ["answer", "closed"])
    def test_settled_question_is_not_answerable(self, sidecar):
        names = ["000001.question.json", f"000001.{sidecar}.json"]
        assert guard.has_answerable_question(names) is False

    def test_unsettled_question_is_answerable(self):
        assert guard.has_answerable_question(["000001.question.json"]) is True

    def test_malformed_names_are_ignored(self):
        names = ["draft.question.json", "000001.notes.json"]
        assert guard.unread_answer_ids(names) == []
        assert guard.has_answerable_question(names) is False


class TestWaitForUnreadAnswer:
    def test_answer_to_any_open_question_stops_the_wait(self):
        clock = _Clock()
        calls = 0

        def listdir(_path):
            nonlocal calls
            calls += 1
            if calls == 1:
                return ["000001.question.json", "000002.question.json"]
            return [
                "000001.question.json",
                "000001.answer.json",
                "000002.question.json",
            ]

        assert guard.wait_for_unread_answer(
            "/ask",
            timeout=5,
            interval=0.25,
            listdir=listdir,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        ) == 1

    def test_answer_already_present_returns_without_sleeping(self):
        sleeps = []
        assert guard.wait_for_unread_answer(
            "/ask",
            listdir=lambda _path: [
                "000007.question.json",
                "000007.answer.json",
            ],
            sleep=sleeps.append,
        ) == 7
        assert sleeps == []

    def test_close_stops_when_no_other_question_is_open(self):
        assert guard.wait_for_unread_answer(
            "/ask",
            listdir=lambda _path: [
                "000007.question.json",
                "000007.closed.json",
            ],
        ) is None

    def test_timeout_fails_open(self):
        clock = _Clock()
        assert guard.wait_for_unread_answer(
            "/ask",
            timeout=1,
            interval=0.25,
            listdir=lambda _path: ["000001.question.json"],
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        ) is None
        assert clock.now == 1.0

    def test_unreadable_channel_fails_open(self):
        def unreadable(_path):
            raise OSError("gone")

        assert guard.wait_for_unread_answer("/ask", listdir=unreadable) is None


class TestMain:
    def _run(self, monkeypatch, tmp_path, payload=None, *, noninteractive=None):
        monkeypatch.setenv(guard.ASK_DIR_ENV, str(tmp_path))
        if noninteractive is None:
            monkeypatch.delenv("LMER_NONINTERACTIVE", raising=False)
        else:
            monkeypatch.setenv("LMER_NONINTERACTIVE", noninteractive)
        monkeypatch.setattr(
            guard.sys, "stdin", io.StringIO(json.dumps(payload or {}))
        )
        stdout = io.StringIO()
        monkeypatch.setattr(guard.sys, "stdout", stdout)
        assert guard.main() == 0
        return stdout.getvalue()

    def test_answered_unread_question_blocks_into_a_continuation(
        self, monkeypatch, tmp_path
    ):
        (tmp_path / "000003.question.json").write_text("{}\n")
        (tmp_path / "000003.answer.json").write_text("{}\n")

        output = self._run(monkeypatch, tmp_path)

        decision = json.loads(output)
        assert decision["decision"] == "block"
        assert "lmer-ask wait 000003" in decision["reason"]
        assert "answer" not in decision, "the answer must stay in the file channel"

    @pytest.mark.parametrize(
        ("payload", "noninteractive"),
        [({"stop_hook_active": True}, None), ({}, "1"), ({}, "true")],
    )
    def test_repeated_or_noninteractive_stop_never_waits(
        self, monkeypatch, tmp_path, payload, noninteractive
    ):
        (tmp_path / "000001.question.json").write_text("{}\n")
        waited = []
        monkeypatch.setattr(
            guard,
            "wait_for_unread_answer",
            lambda *_a, **_k: waited.append(True) or 1,
        )
        assert self._run(
            monkeypatch, tmp_path, payload, noninteractive=noninteractive
        ) == ""
        assert waited == []

    def test_timeout_or_close_fails_open(self, monkeypatch, tmp_path):
        (tmp_path / "000001.question.json").write_text("{}\n")
        monkeypatch.setattr(
            guard, "wait_for_unread_answer", lambda *_a, **_k: None
        )
        assert self._run(monkeypatch, tmp_path) == ""

    def test_answer_with_read_receipt_is_a_noop(self, monkeypatch, tmp_path):
        (tmp_path / "000001.question.json").write_text("{}\n")
        (tmp_path / "000001.answer.json").write_text("{}\n")
        (tmp_path / "000001.read.json").write_text("{}\n")
        assert self._run(monkeypatch, tmp_path) == ""
