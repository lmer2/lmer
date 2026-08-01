"""Tests for the ask channel's wire format (issue #141, slice M2 / T23).

The format is the contract between a container-side CLI and a host daemon that
never talk to each other, so these tests are about what survives that gap: an
answer reaching the question it answers, an id nobody else took, a file a crash
tore in half not costing anything else in the directory.

Everything here runs against a real directory, because the whole design is
filesystem semantics — ``os.link`` refusing an existing target is the concurrency
guard, and a mock would assert that the code calls it rather than that it works.
"""

import json
import os
import threading

import pytest

from ask_channel import protocol
from ask_channel.protocol import AnswerMismatch, AskError, ChannelUnavailable
from tests.conftest import denied_access, strip_lmer_env


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def channel(tmp_path):
    directory = tmp_path / "ask"
    directory.mkdir()
    return directory


# --- resolving the channel ---------------------------------------------------

def test_the_env_var_names_the_channel(channel, monkeypatch):
    monkeypatch.setenv(protocol.ASK_DIR_ENV, str(channel))
    assert protocol.resolve_channel_dir() == channel


def test_an_explicit_directory_beats_the_env_var(channel, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv(protocol.ASK_DIR_ENV, str(channel))
    assert protocol.resolve_channel_dir(str(other)) == other


def test_no_env_var_means_not_orchestrated(monkeypatch):
    monkeypatch.delenv(protocol.ASK_DIR_ENV, raising=False)
    with pytest.raises(ChannelUnavailable, match="not set"):
        protocol.resolve_channel_dir()


def test_a_missing_directory_is_refused_and_never_created(tmp_path, monkeypatch):
    """The mount is missing, and creating it would hide that.

    A directory conjured here would take questions nobody reads and then block
    forever on answers nobody can write — the exact hang this whole feature
    exists to remove.
    """
    missing = tmp_path / "never-mounted"
    monkeypatch.setenv(protocol.ASK_DIR_ENV, str(missing))
    with pytest.raises(ChannelUnavailable, match="does not exist"):
        protocol.resolve_channel_dir()
    assert not missing.exists()


def test_a_whitespace_only_env_var_is_unset(monkeypatch):
    monkeypatch.setenv(protocol.ASK_DIR_ENV, "   ")
    with pytest.raises(ChannelUnavailable, match="not set"):
        protocol.resolve_channel_dir()


# --- posting and answering ---------------------------------------------------

def test_question_answer_round_trip(channel):
    question = protocol.post_question(channel, "rebase or merge?", ["rebase", "merge"])
    assert protocol.answer_for(channel, question) is None

    protocol.write_answer(channel, question, "rebase")
    answer = protocol.answer_for(channel, question)
    assert answer is not None
    assert answer.text == "rebase"
    assert answer.question_id == question.id


def test_the_question_carries_its_options_verbatim(channel):
    question = protocol.post_question(channel, "which?", ["a b", "c/d", "é"])
    stored = protocol.read_entry(channel, question.id)
    assert stored.options == ("a b", "c/d", "é")


def test_a_note_is_an_entry_nobody_can_answer(channel):
    note = protocol.post_note(channel, "cloning the repo, back shortly")
    entries = protocol.read_entries(channel)
    assert [entry.kind for entry in entries] == [protocol.KIND_NOTE]
    assert entries[0].text == "cloning the repo, back shortly"
    assert not (channel / f"{note.id}.answer.json").exists()


def test_an_answer_is_not_overwritten(channel):
    """The session may already have read the first one and acted on it."""
    question = protocol.post_question(channel, "ship it?")
    protocol.write_answer(channel, question, "yes")
    with pytest.raises(FileExistsError):
        protocol.write_answer(channel, question, "no, wait")
    assert protocol.answer_for(channel, question).text == "yes"


def test_ids_are_ordered_and_shared_by_questions_and_notes(channel):
    first = protocol.post_question(channel, "one")
    note = protocol.post_note(channel, "two")
    second = protocol.post_question(channel, "three")
    assert [first.seq, note.seq, second.seq] == [1, 2, 3]
    assert [entry.text for entry in protocol.read_entries(channel)] == [
        "one", "two", "three",
    ]


def test_entries_are_ordered_by_number_not_by_string(channel):
    """Zero-padding is cosmetic; the order must survive running out of digits.

    ``"1000000" < "999999"`` as strings and the other way round as numbers, so a
    channel that outgrew its padding is where a lexical sort silently reverses.
    """
    (channel / "999999.note.json").write_text(
        json.dumps({"kind": "note", "text": "the last padded one", "at": "x"}),
        encoding="utf-8",
    )
    (channel / "1000000.note.json").write_text(
        json.dumps({"kind": "note", "text": "one wider", "at": "x"}), encoding="utf-8"
    )
    assert [entry.text for entry in protocol.read_entries(channel)] == [
        "the last padded one", "one wider",
    ]


# --- the answer never lands on the wrong question ----------------------------

def test_an_answer_naming_another_question_is_refused(channel):
    question = protocol.post_question(channel, "which branch?")
    (channel / f"{question.id}{protocol.ANSWER_SUFFIX}").write_text(
        json.dumps({
            "question_id": "000042",
            "nonce": question.nonce,
            "text": "main",
            "answered_at": "2026-07-27T10:00:00Z",
        }),
        encoding="utf-8",
    )
    with pytest.raises(AnswerMismatch, match="000042"):
        protocol.answer_for(channel, question)


def test_a_recycled_id_does_not_inherit_the_old_answer(channel):
    """The nonce is what closes this: the ids match and the answer is stale.

    Reached by deleting a question file and re-creating the id by hand — which is
    what a hand-repaired channel or a restored backup looks like.
    """
    first = protocol.post_question(channel, "delete the branch?")
    protocol.write_answer(channel, first, "yes")
    (channel / f"{first.id}{protocol.QUESTION_SUFFIX}").unlink()

    reused = json.loads(
        (channel / f"{first.id}{protocol.ANSWER_SUFFIX}").read_text(encoding="utf-8")
    )
    assert reused["nonce"] == first.nonce, "fixture assumption"

    fresh = protocol.Entry(
        id=first.id, seq=first.seq, kind=protocol.KIND_QUESTION,
        text="drop the database?", at="2026-07-27T11:00:00Z",
        nonce="0000deadbeef0000",
    )
    with pytest.raises(AnswerMismatch, match="different question"):
        protocol.answer_for(channel, fresh)


def test_a_reused_id_is_never_handed_out_while_its_answer_survives(channel):
    """Belt to the nonce's braces: the allocator counts answers, not questions."""
    question = protocol.post_question(channel, "one")
    protocol.write_answer(channel, question, "done")
    (channel / f"{question.id}{protocol.QUESTION_SUFFIX}").unlink()

    nxt = protocol.post_question(channel, "two")
    assert nxt.id != question.id
    assert nxt.seq == question.seq + 1


def test_a_mismatched_answer_leaves_the_rest_of_the_view_readable(channel):
    """One broken pair must not empty the operator's whole channel."""
    good = protocol.post_question(channel, "fine")
    broken = protocol.post_question(channel, "confused")
    protocol.write_answer(channel, good, "ok")
    (channel / f"{broken.id}{protocol.ANSWER_SUFFIX}").write_text(
        json.dumps({"question_id": "000777", "text": "not yours"}), encoding="utf-8"
    )

    entries = {entry.id: entry for entry in protocol.read_entries(channel)}
    assert entries[good.id].answer.text == "ok"
    assert entries[broken.id].answer is None
    assert "000777" in entries[broken.id].problem


# --- the read receipt ---------------------------------------------------------
#
# What it is for: an answer on disk is not an answer delivered. A session that
# timed out its wait, worked on something else and never waited again leaves the
# operator's reply unread, which from the outside looks exactly like an answer the
# agent acted on. The receipt is what tells those apart.

def test_reading_an_answer_files_a_receipt_beside_it(channel):
    question = protocol.post_question(channel, "which branch?")
    protocol.write_answer(channel, question, "prep-release")

    receipt = protocol.mark_answer_read(channel, question, via="wait")

    assert (channel / f"{question.id}{protocol.READ_SUFFIX}").exists()
    assert receipt.question_id == question.id
    assert receipt.via == "wait"
    assert receipt.nonce == question.nonce, "the pairing check needs the nonce"
    entry = protocol.read_entry(channel, question.id)
    assert entry.answer_read is True
    assert entry.to_dict()["answer_read"] is True


def test_an_answer_nobody_read_is_unread(channel):
    """The whole discriminator: answered, and no receipt beside it."""
    question = protocol.post_question(channel, "ship it?")
    protocol.write_answer(channel, question, "yes, ship it")

    entry = protocol.read_entry(channel, question.id)
    assert entry.answered is True
    assert entry.answer_read is False
    assert protocol.is_answer_unread(entry) is True
    assert [e.id for e in protocol.unread_answers(channel)] == [question.id]


def test_a_read_answer_drops_off_the_unread_list(channel):
    question = protocol.post_question(channel, "ship it?")
    protocol.write_answer(channel, question, "yes")
    protocol.mark_answer_read(channel, question, via="wait")
    assert protocol.unread_answers(channel) == []


@pytest.mark.parametrize("state", ["unanswered", "closed", "note"])
def test_nothing_without_an_answer_is_an_unread_answer(channel, state):
    """Ending a session with an open question is legitimate; only a *reply* nobody
    read is the failure, so nothing else may look like one."""
    if state == "note":
        protocol.post_note(channel, "just saying")
    else:
        question = protocol.post_question(channel, "anyone there?")
        if state == "closed":
            protocol.close_question(channel, question, reason="decided it myself")

    assert protocol.unread_answers(channel) == []


def test_an_answer_that_raced_a_close_is_still_unread(channel):
    """The pair no verb printed: `close` only hands over an answer it found before
    filing, so an answer that landed after that check reached nobody."""
    question = protocol.post_question(channel, "ship it?")
    protocol.close_question(channel, question)
    protocol.write_answer(channel, question, "yes")

    assert [e.id for e in protocol.unread_answers(channel)] == [question.id]


def test_marking_read_twice_keeps_the_first_receipt(channel):
    """Reading twice is ordinary — wait, then list, then end the session — and
    nothing on this channel rewrites a record."""
    question = protocol.post_question(channel, "ok?")
    protocol.write_answer(channel, question, "yes")
    first = protocol.mark_answer_read(channel, question, via="wait")

    again = protocol.mark_answer_read(channel, question, via="end-session")

    assert again.read_at == first.read_at
    assert again.via == "wait", "the second read overwrote the first receipt"


def test_a_stale_receipt_does_not_claim_a_fresh_answer(channel):
    """Same id, different question — as for a stale close record, the nonce tells
    them apart, and the direction that costs a redundant delivery is the safe one.
    """
    question = protocol.post_question(channel, "the new question")
    protocol.write_answer(channel, question, "the new answer")
    (channel / f"{question.id}{protocol.READ_SUFFIX}").write_text(
        json.dumps({
            "question_id": question.id,
            "nonce": "some-earlier-questions-nonce",
            "read_at": "2026-07-01T00:00:00Z",
        }),
        encoding="utf-8",
    )

    assert protocol.read_entry(channel, question.id).answer_read is False
    assert [e.id for e in protocol.unread_answers(channel)] == [question.id]


def test_a_receipt_naming_another_question_is_ignored(channel):
    question = protocol.post_question(channel, "mine?")
    protocol.write_answer(channel, question, "yes")
    (channel / f"{question.id}{protocol.READ_SUFFIX}").write_text(
        json.dumps({"question_id": "000042", "read_at": "2026-07-01T00:00:00Z"}),
        encoding="utf-8",
    )
    assert protocol.read_entry(channel, question.id).answer_read is False


def test_an_unreadable_receipt_reads_as_unread(channel):
    """Fails toward delivering again: a corrupt file must not be able to say an
    operator's reply already reached somebody."""
    question = protocol.post_question(channel, "still unread?")
    protocol.write_answer(channel, question, "yes")
    (channel / f"{question.id}{protocol.READ_SUFFIX}").write_text(
        "{ torn", encoding="utf-8"
    )
    assert protocol.read_entry(channel, question.id).answer_read is False
    assert [e.id for e in protocol.unread_answers(channel)] == [question.id]


def test_a_receipt_is_not_an_entry_of_its_own(channel):
    """It is filed under a question, like the answer and the close record.

    And it holds the id out of circulation by itself: with the question and the
    answer gone — a hand-repaired channel, a restored backup — a reissued id would
    come with a receipt claiming somebody had read a brand-new question's answer.
    """
    question = protocol.post_question(channel, "one")
    protocol.write_answer(channel, question, "yes")
    protocol.mark_answer_read(channel, question, via="wait")

    assert [entry.id for entry in protocol.read_entries(channel)] == [question.id]

    (channel / f"{question.id}{protocol.QUESTION_SUFFIX}").unlink()
    (channel / f"{question.id}{protocol.ANSWER_SUFFIX}").unlink()
    assert protocol.post_question(channel, "two").id != question.id


def test_a_receipt_leaves_no_temp_file_behind(channel):
    question = protocol.post_question(channel, "tidy?")
    protocol.write_answer(channel, question, "yes")
    protocol.mark_answer_read(channel, question, via="wait")
    assert [name for name in os.listdir(channel) if name.endswith(".tmp")] == []


# --- signals: the shape addressed past the operator (T122) --------------------
#
# A signal is filed like a note and read by nobody the other five shapes are for:
# the daemon picks it up and turns it into a digest for the orchestrating
# assistant. So what these pin is that it is a first-class entry where that
# matters (id allocation, the entry cap, exclusive writes) and absent everywhere
# the operator is (the feed, the answerable set, ``read_entry``).

def test_a_signal_is_filed_like_any_other_entry(channel):
    signal = protocol.post_signal(channel, "pushed MR !167 for review")
    assert (channel / f"{signal.id}{protocol.SIGNAL_SUFFIX}").is_file()
    assert signal.kind == protocol.KIND_SIGNAL
    assert protocol.read_signals(channel)[0].text == "pushed MR !167 for review"


def test_a_signal_takes_an_id_from_the_one_sequence(channel):
    """One total order on the channel, or an answer can pair with the wrong thing.

    An id counted from a second counter would eventually collide with a question's,
    and the nonce check is the only thing that would notice.
    """
    question = protocol.post_question(channel, "one")
    signal = protocol.post_signal(channel, "two")
    note = protocol.post_note(channel, "three")
    assert [question.seq, signal.seq, note.seq] == [1, 2, 3]


def test_a_signal_is_not_in_the_operator_s_feed(channel):
    """``read_entries`` is what the fleet view and ``lmer-ask list`` render.

    A milestone the orchestrator already consumed would arrive there as a card
    that asked for nothing and cannot be answered.
    """
    protocol.post_question(channel, "which approach?")
    protocol.post_note(channel, "cloning the repo")
    protocol.post_signal(channel, "the review is finished")

    assert [entry.kind for entry in protocol.read_entries(channel)] == [
        protocol.KIND_QUESTION, protocol.KIND_NOTE,
    ]
    assert [entry.kind for entry in protocol.read_signals(channel)] == [
        protocol.KIND_SIGNAL,
    ]


def test_nothing_can_be_answered_or_closed_under_a_signal_s_id(channel):
    """One-way and terminal: no answer, no closure, no receipt, so no reply box."""
    signal = protocol.post_signal(channel, "done with the current task")
    assert protocol.read_entry(channel, signal.id) is None
    assert protocol.open_questions(channel) == []
    assert not protocol.is_answerable(protocol.read_signals(channel)[0])


def test_signals_are_counted_by_the_full_channel_refusal(channel, monkeypatch):
    """The one bound on a shape nothing waits for — see ``post_signal``.

    ``MAX_ENTRIES`` is patched rather than reached, because what is under test is
    that signals are *counted*, not the value.
    """
    monkeypatch.setattr(protocol, "MAX_ENTRIES", 2)
    protocol.post_signal(channel, "one")
    protocol.post_signal(channel, "two")
    with pytest.raises(AskError, match="which is the limit"):
        protocol.post_signal(channel, "three")


def test_an_oversized_signal_is_refused_and_nothing_is_filed(channel):
    with pytest.raises(AskError, match="over the"):
        protocol.post_signal(channel, "x" * (protocol.MAX_SIGNAL_CHARS + 1))
    assert protocol.read_signals(channel) == []


def test_a_torn_signal_costs_only_itself(channel):
    """Same tolerance every other read here has: one bad file, one lost entry."""
    protocol.post_signal(channel, "the review is finished")
    (channel / "000002.signal.json").write_text('{"kind": "sig', encoding="utf-8")
    protocol.post_signal(channel, "and the MR is pushed")

    assert [entry.text for entry in protocol.read_signals(channel)] == [
        "the review is finished", "and the MR is pushed",
    ]


# --- tolerance ---------------------------------------------------------------

def test_a_truncated_question_costs_only_itself(channel):
    good = protocol.post_question(channel, "readable")
    torn = channel / "000009.question.json"
    torn.write_text('{"kind": "question", "text": "half a fi', encoding="utf-8")

    texts = [entry.text for entry in protocol.read_entries(channel)]
    assert texts == ["readable"]
    assert torn.exists(), "the bad bytes are evidence; do not delete them"
    assert protocol.read_entry(channel, good.id).text == "readable"


@pytest.mark.parametrize("body", [
    "",
    "[]",
    '"a string"',
    '{"kind": "question"}',
    '{"kind": "question", "text": 17}',
    '{"kind": "note", "text": "wrong kind for this filename"}',
    '{"id": "000042", "text": "wrong id for this filename"}',
])
def test_records_that_disagree_with_their_filename_are_skipped(channel, body):
    (channel / "000005.question.json").write_text(body, encoding="utf-8")
    assert protocol.read_entries(channel) == []
    assert protocol.read_entry(channel, "000005") is None


def test_a_file_that_is_not_an_entry_is_ignored(channel):
    (channel / "notes.txt").write_text("hello", encoding="utf-8")
    (channel / ".000001.question.json.tmp").write_text("{}", encoding="utf-8")
    (channel / "README.question.json").write_text("{}", encoding="utf-8")
    assert protocol.read_entries(channel) == []
    assert protocol.post_question(channel, "first").id == "000001"


def test_an_unreadable_answer_is_loud_not_silent(channel):
    """The operator answered; reporting "still waiting" would strand the session."""
    question = protocol.post_question(channel, "go?")
    (channel / f"{question.id}{protocol.ANSWER_SUFFIX}").write_text(
        "{ truncated", encoding="utf-8"
    )
    with pytest.raises(AskError, match="unreadable"):
        protocol.answer_for(channel, question)


def test_reading_a_channel_that_is_not_there_is_empty(tmp_path):
    assert protocol.read_entries(tmp_path / "nope") == []


# --- limits ------------------------------------------------------------------

@pytest.mark.parametrize("text", ["", "   ", "\n\n"])
def test_an_empty_question_is_refused(channel, text):
    with pytest.raises(AskError, match="empty"):
        protocol.post_question(channel, text)


def test_an_oversized_question_is_refused(channel):
    with pytest.raises(AskError, match="over the"):
        protocol.post_question(channel, "x" * (protocol.MAX_QUESTION_CHARS + 1))


def test_an_oversized_answer_is_refused(channel):
    question = protocol.post_question(channel, "how much?")
    with pytest.raises(AskError, match="over the"):
        protocol.write_answer(channel, question, "y" * (protocol.MAX_ANSWER_CHARS + 1))


def test_too_many_options_are_refused(channel):
    options = [f"choice {index}" for index in range(protocol.MAX_OPTIONS + 1)]
    with pytest.raises(AskError, match="over the"):
        protocol.post_question(channel, "pick one", options)


def test_an_oversized_option_is_refused(channel):
    with pytest.raises(AskError, match="over the"):
        protocol.post_question(
            channel, "pick", ["ok", "z" * (protocol.MAX_OPTION_CHARS + 1)]
        )


def test_an_agent_looping_on_questions_is_stopped(channel):
    """An unbounded loop would bury the one question worth reading.

    The refusal names both ways out — wait, or close what it stopped waiting
    for (T34) — because the agent reading it is mid-loop and this message is
    the only steer it gets."""
    for index in range(protocol.MAX_OPEN_QUESTIONS):
        protocol.post_question(channel, f"question {index}")
    with pytest.raises(AskError, match="still open.*close the ones"):
        protocol.post_question(channel, "one too many")


def test_the_cap_counts_only_unanswered_questions(channel):
    for index in range(protocol.MAX_OPEN_QUESTIONS):
        question = protocol.post_question(channel, f"question {index}")
        protocol.write_answer(channel, question, "yes")
    assert protocol.post_question(channel, "still fine").seq


def test_a_channel_that_is_full_refuses_more(channel):
    """Notes have no answer to wait for, so nothing else bounds them."""
    for index in range(protocol.MAX_ENTRIES):
        (channel / f"{index + 1:06d}.note.json").write_text(
            json.dumps({"kind": "note", "text": f"note {index}", "at": "x"}),
            encoding="utf-8",
        )
    with pytest.raises(AskError, match="which is the limit"):
        protocol.post_note(channel, "one too many")
    with pytest.raises(AskError, match="which is the limit"):
        protocol.post_question(channel, "and no questions either")


def test_the_full_cap_counts_entries_not_the_files_they_accumulate(channel, monkeypatch):
    """A long session's ordinary shape is not a full channel (issue #191).

    A healthy question ends up as three files — question, answer, read receipt —
    so a cap that counted files would refuse at a third of the entries it claims
    to count, and tell the session it was looping while it was doing exactly what
    the channel is for. ``MAX_ENTRIES`` is patched rather than reached because
    what is under test is *what* is counted, not the value.
    """
    monkeypatch.setattr(protocol, "MAX_ENTRIES", 3)

    for index in range(2):
        question = protocol.post_question(channel, f"question {index}")
        protocol.write_answer(channel, question, "yes")
        protocol.mark_answer_read(channel, question)
    # Six files, two entries. Counting files, the channel is already over the cap.
    assert len(list(channel.iterdir())) == 6

    third = protocol.post_question(channel, "the entry a file count would refuse")
    protocol.close_question(channel, third, reason="stopped waiting")

    assert len(list(channel.iterdir())) == 8
    with pytest.raises(AskError, match=r"holds 3 entries, which is the limit"):
        protocol.post_question(channel, "one too many")
    with pytest.raises(AskError, match=r"holds 3 entries, which is the limit"):
        protocol.post_note(channel, "and no notes either")


def test_an_unwritable_channel_reads_as_no_channel(channel):
    """What a uid mismatch across the bind mount looks like from inside.

    ``os.access`` is patched rather than a mode bit set: for uid 0 it answers
    True for a directory that would be unwritable to anybody else, so on a suite
    running as root — CI's pytest job — the mode-bit version of this test never
    reaches its own assertion.
    """
    channel.chmod(0o500)
    try:
        with denied_access(channel):
            with pytest.raises(ChannelUnavailable, match="not writable"):
                protocol.resolve_channel_dir(str(channel))
    finally:
        channel.chmod(0o700)


def test_an_id_is_validated_before_it_reaches_a_path(channel):
    for bad in ("../../etc/passwd", "1/2", "abc", "", "0" * 13):
        assert not protocol.valid_entry_id(bad)
        with pytest.raises(AskError, match="invalid entry id"):
            protocol.load_answer(channel, bad)


# --- concurrency -------------------------------------------------------------

def test_questions_asked_at_the_same_moment_all_survive(channel):
    """Two agents in one session must not collide on an id.

    ``os.link`` is what makes this hold: the loser of a race gets
    ``FileExistsError`` and draws the next id, rather than clobbering a question
    that was already asked.
    """
    posted = []
    errors = []
    start = threading.Barrier(8)

    def ask(index):
        start.wait(timeout=5)
        try:
            posted.append(protocol.post_question(channel, f"from thread {index}"))
        except Exception as exc:  # pragma: no cover - a failure prints the reason
            errors.append(exc)

    threads = [threading.Thread(target=ask, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors, errors
    assert len({entry.id for entry in posted}) == 8, "an id was handed out twice"
    stored = protocol.read_entries(channel)
    assert len(stored) == 8
    assert {entry.text for entry in stored} == {
        f"from thread {index}" for index in range(8)
    }


def test_each_concurrent_question_gets_its_own_answer(channel):
    """The point of the ids: eight open questions, eight distinct answers."""
    questions = [protocol.post_question(channel, f"q{index}") for index in range(8)]
    for index, question in enumerate(questions):
        protocol.write_answer(channel, question, f"a{index}")
    for index, question in enumerate(questions):
        assert protocol.answer_for(channel, question).text == f"a{index}"


def test_no_temp_files_are_left_behind(channel):
    question = protocol.post_question(channel, "tidy?")
    protocol.post_note(channel, "note")
    protocol.write_answer(channel, question, "yes")
    assert [name for name in os.listdir(channel) if name.endswith(".tmp")] == []


# --- waiting -----------------------------------------------------------------

def test_wait_returns_the_answer_that_is_already_there(channel):
    question = protocol.post_question(channel, "already?")
    protocol.write_answer(channel, question, "yes")
    slept = []
    answer = protocol.wait_for_answer(
        channel, question, timeout=30, interval=1, sleep=slept.append
    )
    assert answer.text == "yes"
    assert slept == [], "an answer already on disk must not cost an interval"


def test_wait_picks_up_an_answer_written_mid_wait(channel):
    question = protocol.post_question(channel, "soon?")
    calls = []

    def fake_sleep(_seconds):
        calls.append(_seconds)
        if len(calls) == 2:
            protocol.write_answer(channel, question, "now")

    answer = protocol.wait_for_answer(
        channel, question, timeout=100, interval=1,
        sleep=fake_sleep, monotonic=lambda: len(calls),
    )
    assert answer.text == "now"


def test_wait_gives_up_at_the_deadline(channel):
    question = protocol.post_question(channel, "never?")
    ticks = iter(range(0, 100))
    answer = protocol.wait_for_answer(
        channel, question, timeout=3, interval=1,
        sleep=lambda _s: None, monotonic=lambda: next(ticks),
    )
    assert answer is None
    assert protocol.open_questions(channel), "a timeout leaves the question open"
