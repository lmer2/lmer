"""A review session records the MR notes it writes on its run (#401 F3).

The check-in digest must tell the reviewer's own replies ("verified in <sha>")
from its author's, and the two are often the same bot account. A time cannot
do it: the review session's last event is often its teardown, days after the
reviewer stopped writing. So the tool that writes the notes says which notes it
wrote — a ``review_note`` event per note, carrying the note id — and the digest
excludes exactly those ids.

What is pinned:

- every MR note-creating call on :class:`GitLabClient` (thread reply, inline
  comment, general comment) records the created note's id, in a review session
  only (``LMER_TASK=review``); a fix author's replies are the author's movement;
- recording never fails the write it follows: a ``work`` CLI that is missing,
  fails or hangs is a warning, and the unrecorded note reads as the author's —
  the run stays named, which is the fail-open side.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from gitlab_reviewer import review_notes
from gitlab_reviewer.client import GitLabClient, InlineComment

THREAD_ID = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def recorded(monkeypatch):
    """Capture the ``work`` invocations instead of running them."""
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(review_notes.subprocess, "run", run)
    return calls


@pytest.fixture
def review_session(monkeypatch):
    monkeypatch.setenv("LMER_TASK", "review")


def client():
    return GitLabClient(host="gitlab.example.com", token="fake-token")


def events(calls):
    found = []
    for argv in calls:
        assert argv[:3] == ["work", "event", review_notes.REVIEW_NOTE_EVENT]
        assert argv[3] == "--data"
        found.append(json.loads(argv[4]))
    return found


def test_a_reply_in_a_review_session_is_recorded(recorded, review_session):
    note = {"id": 9101, "created_at": "2026-09-25T04:50:34.765Z", "system": False}
    with patch.object(GitLabClient, "_request", return_value=note):
        assert client().reply_to_merge_request_discussion(
            "acme/widgets", 7, THREAD_ID, "Verified in abc1234."
        ) == note
    assert events(recorded) == [{
        "mr": "gitlab.example.com/acme/widgets!7",
        "note_id": 9101,
        "created_at": "2026-09-25T04:50:34.765Z",
    }]


def test_a_general_comment_in_a_review_session_is_recorded(recorded, review_session):
    note = {"id": 9102, "created_at": "2026-09-25T04:50:40.199Z"}
    with patch.object(GitLabClient, "_request", return_value=note):
        client().add_general_comment("acme/widgets", 7, "Superseded by iteration 2.")
    assert [event["note_id"] for event in events(recorded)] == [9102]


def test_an_inline_comment_records_the_note_inside_the_new_discussion(recorded, review_session):
    """Creating a discussion answers with the discussion; its first note is ours."""
    discussion = {"id": THREAD_ID, "notes": [{"id": 9103, "created_at": "2026-09-25T04:50:01Z"}]}
    comment = InlineComment(file_path="src/app.py", line_number=3, comment="F1")
    with patch.object(GitLabClient, "_request", return_value=discussion):
        client().add_inline_comment("acme/widgets", 7, comment, "base", "head")
    assert [event["note_id"] for event in events(recorded)] == [9103]


@pytest.mark.parametrize("task", [None, "followup", "develop"])
def test_outside_a_review_session_nothing_is_recorded(recorded, monkeypatch, task):
    """A fix author's reply is the author moving: it must stay countable."""
    if task is None:
        monkeypatch.delenv("LMER_TASK", raising=False)
    else:
        monkeypatch.setenv("LMER_TASK", task)
    with patch.object(GitLabClient, "_request", return_value={"id": 1}):
        client().reply_to_merge_request_discussion("acme/widgets", 7, THREAD_ID, "Fixed.")
    assert recorded == []


def test_an_answer_without_a_note_id_records_nothing(recorded, review_session):
    with patch.object(GitLabClient, "_request", return_value={"message": "odd"}):
        client().add_general_comment("acme/widgets", 7, "hello")
    assert recorded == []


@pytest.mark.parametrize("failure", [
    pytest.param(FileNotFoundError("work"), id="no-work-cli"),
    pytest.param(subprocess.TimeoutExpired(["work"], 30), id="hung"),
    pytest.param(subprocess.CompletedProcess(["work"], 1, "", "no active run"), id="refused"),
])
def test_a_failed_record_never_fails_the_write(monkeypatch, review_session, capsys, failure):
    def run(argv, **kwargs):
        if isinstance(failure, BaseException):
            raise failure
        return failure

    monkeypatch.setattr(review_notes.subprocess, "run", run)
    note = {"id": 9104, "created_at": "2026-09-25T04:50:00Z"}
    with patch.object(GitLabClient, "_request", return_value=note):
        assert client().add_general_comment("acme/widgets", 7, "x") == note
    err = capsys.readouterr().err
    assert "9104" in err and "not recorded" in err
