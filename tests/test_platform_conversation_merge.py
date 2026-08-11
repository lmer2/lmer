"""Tests for merging a live session's ask channel into its conversation (T67).

The gap being closed, stated once: an answer given through the ask channel never
touches the PTY and is never handed to the harness as input — it is a file the
platform writes into a bind-mounted directory the session polls. So the harness
records nothing about it, and until this merge the operator could answer a
question and find no trace of their own words in the conversation view. The
operator, from live testing: *"answering lmer's question in the operator chat,
doesn't show my answer in the conversation chat … for coherence it'd ideally still
show up as input i did"*.

Five properties carry this, and each one fails quietly in a different direction:

- **The answer is yours, and shown as yours.** Not paraphrased, not rendered as
  markdown, not attributed to the agent.
- **Nothing is written into the transcript.** The merge is a read-path join; the
  transcript is the harness's artifact and the one durable record of a session,
  and forging operator turns into it would make that record partly fiction.
- **A session with no channel reads exactly as it did before.** The assistant's
  own session (T31) has no run and no channel at all, and every session spawned
  before the channel existed is in the same position.
- **The credential scrub reaches the merged turns.** An operator can paste a
  secret into an answer, and this is the last place before a browser.
- **The order is deterministic.** Two clocks with different precision meet here,
  so equal timestamps must not decide anything by luck.

Ordering is asserted against captured transcript timestamps rather than against
"now": the fixture's turns are stamped 09:00:01 … 09:00:14, and the channel
entries are restamped to land at chosen points inside that.
"""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ask_channel import protocol
from lmer_platform import api, ask, registry, store, transcripts
from lmer_platform import config as cfg
from tests.conftest import strip_lmer_env
from tests.test_platform_transcripts import (
    DEAD_PID,
    FOLLOWUP_FIXTURE,
    RUN,
    plant_log,
    plant_session,
)

SECRET = "test-secret-value"

WEB = Path(__file__).resolve().parent.parent / "web"
CHAT = WEB / "src" / "components" / "Chat.vue"

#: Where the fixture transcript's own turns sit, so a test can say "between the
#: failed Edit and /usage" without restating the fixture.
BEFORE_EDIT = "2026-07-20T09:00:06Z"
AT_THE_EDIT = "2026-07-20T09:00:08Z"
AFTER_THE_EDIT = "2026-07-20T09:00:09Z"
AFTER_THE_ANSWER = "2026-07-20T09:00:10Z"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    for name in ("LMER_WORK_REPO", cfg.ENV_BIND_ADDRESS, cfg.ENV_BIND_PORT):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


@pytest.fixture(autouse=True)
def _isolated_transcript_root(tmp_path, monkeypatch):
    """Never fall through to the developer's own ``~/.claude/projects``.

    Autouse for the reason ``test_platform_transcripts`` gives: the default root is
    ``Path.home()``-based, so a test that forgot would assert against whatever real
    conversations happen to be on this machine.
    """
    root = tmp_path / "harness-transcripts"
    root.mkdir()
    monkeypatch.setattr(transcripts, "TRANSCRIPT_ROOT", str(root))
    return root


def _restamp(path, **fields):
    """Rewrite the time fields of one channel record already on disk.

    The records are *posted* through :mod:`ask_channel.protocol` so they are the
    real format — nonce, schema, padded id and all — and only then restamped,
    because the protocol stamps the moment it wrote and these tests need an entry
    that lands at a known point inside a captured transcript. Rewriting the field
    is the smaller lie than reimplementing the file format here.
    """
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(fields)
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")


def plant_question(
    session_id,
    text="which branch?",
    *,
    at=AFTER_THE_EDIT,
    answer=None,
    answered_at=None,
    options=(),
):
    """Put one question — and optionally its answer — on a session's channel."""
    directory = ask.prepare_ask_dir(session_id)
    entry = protocol.post_question(directory, text, list(options))
    _restamp(directory / f"{entry.id}{protocol.QUESTION_SUFFIX}", at=at)
    if answer is not None:
        # At the protocol layer, not ask.answer_question: these fixtures register
        # dead sessions (the merge only needs a transcript to read), and the
        # platform's write path now refuses a dead session's question (T94). The
        # record being planted is "an answer already exists on this channel" —
        # the ordering T94 deliberately keeps legal, since answers survive the
        # session that received them.
        protocol.write_answer(directory, entry, answer)
        _restamp(
            directory / f"{entry.id}{protocol.ANSWER_SUFFIX}",
            answered_at=answered_at or at,
        )
    return entry.id


def plant_note(session_id, text="ran the suite, all green"):
    return protocol.post_note(ask.prepare_ask_dir(session_id), text)


def said(page):
    """``(role, text)`` for every turn on a page, in the order it renders."""
    return [(message.role, message.text) for message in page.messages]


# --- the answer is yours, and shown as yours ---------------------------------

def test_an_answer_you_gave_appears_as_your_own_turn(platform_root):
    """The whole point: your words are in the conversation, attributed to you.

    ``role='user'`` is not cosmetic — it is what puts the turn in the view's
    verbatim branch, so an answer containing asterisks is shown as typed rather
    than reinterpreted as markup.
    """
    plant_session("s-merge")
    plant_question("s-merge", answer="prep-release")

    page = transcripts.read_messages("s-merge", limit=100)
    yours = [message for message in page.messages if message.text == "prep-release"]

    assert len(yours) == 1, "the answer the operator gave is not in the conversation"
    assert yours[0].role == "user"
    assert yours[0].kind == "said", "an answer is not machinery talking to the model"
    assert yours[0].via == transcripts.ASK_CHANNEL_VIA
    # No transcript file holds it, and pretending one does would be the lie.
    assert yours[0].origin is None


def test_the_question_is_shown_above_the_answer_it_explains(platform_root):
    """A bubble of your own saying "prep-release" means nothing on its own.

    The question is the agent's own words — it ran ``lmer-ask`` — so it is an
    assistant turn, and it is not a second copy of anything: ``lmer-ask`` is
    documented to take its text from a file or stdin, so the transcript usually
    holds only a bounded tool-hint line for the command that posted it.
    """
    plant_session("s-pair")
    plant_question("s-pair", "which branch?", answer="prep-release")

    page = transcripts.read_messages("s-pair", limit=100)
    turns = said(page)

    assert ("assistant", "which branch?") in turns
    assert turns.index(("assistant", "which branch?")) < turns.index(
        ("user", "prep-release")
    ), "the answer renders above the question it answers"
    assert [
        message.via for message in page.messages if message.text == "which branch?"
    ] == [transcripts.ASK_CHANNEL_VIA]


def test_an_unanswered_question_is_still_part_of_the_conversation(platform_root):
    """A question that timed out is exactly what a later reader needs to see.

    Dropping it would leave the transcript's own ``lmer-ask`` tool line
    unexplained, which is the state an operator is trying to understand when they
    scroll back at all.
    """
    plant_session("s-open")
    plant_question("s-open", "delete the branch?")

    turns = said(transcripts.read_messages("s-open", limit=100))
    assert ("assistant", "delete the branch?") in turns


def test_a_progress_note_is_not_a_conversation_turn(platform_root):
    """A note wants no reply, so it is not half of an exchange to read back.

    It is progress, and the operator channel is where it surfaces. Merging notes
    too would put the agent's status pings in the middle of a conversation that
    already carries the agent's own account of the same work.
    """
    plant_session("s-note")
    plant_note("s-note", "ran the suite, all green")

    turns = said(transcripts.read_messages("s-note", limit=100))
    assert not [text for _, text in turns if "all green" in text]


def test_the_options_a_question_offered_are_not_merged(platform_root):
    """What the operator chose is shown verbatim; the roads not taken are not.

    The ask panel is where a pending question's choices belong. Pasting them into
    the conversation would put text in an agent turn that the agent never wrote as
    prose.
    """
    plant_session("s-options")
    plant_question(
        "s-options", "which branch?", options=("main", "prep-release"), answer="main"
    )

    page = transcripts.read_messages("s-options", limit=100)
    assert [
        message.text for message in page.messages
        if message.via == transcripts.ASK_CHANNEL_VIA
    ] == ["which branch?", "main"]


# --- nothing is written back --------------------------------------------------

def test_a_merged_read_does_not_touch_the_transcript_file(platform_root):
    """The transcript is the harness's artifact and the one durable record.

    Writing synthetic operator turns into it would leave no later reader able to
    tell which turns the session actually received from which ones the platform
    added — and it is the file ``scrub_transcript`` rewrites, so a forged turn
    would also become the thing served as "what the harness wrote".
    """
    target = plant_session("s-readonly")
    plant_question("s-readonly", answer="prep-release")

    before = target.read_bytes()
    before_stat = target.stat()
    names_before = sorted(path.name for path in target.parent.iterdir())

    page = transcripts.read_messages("s-readonly", limit=100)
    assert ("user", "prep-release") in said(page), "nothing was merged, so prove nothing"

    assert target.read_bytes() == before, "the merge wrote into the transcript"
    assert target.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert sorted(path.name for path in target.parent.iterdir()) == names_before


def test_a_merged_read_does_not_touch_the_channel_either(platform_root):
    """Read-only in both directions: the channel is the container's to write.

    Nobody rewrites anybody else's file on that channel (``ask_channel.protocol``
    says so, and it is what makes a directory shared across a bind mount workable
    without coordination). A read path that touched it would be the first
    exception.
    """
    plant_session("s-channel-readonly")
    plant_question("s-channel-readonly", answer="prep-release")
    directory = ask.session_ask_dir("s-channel-readonly")
    before = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(directory.iterdir())
    }

    transcripts.read_messages("s-channel-readonly", limit=100)

    assert {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(directory.iterdir())
    } == before


# --- a session with no channel is untouched ----------------------------------

def test_a_session_with_no_channel_reads_exactly_as_before(platform_root):
    """T31 pointed the assistant drawer at a session with no run and no channel.

    Every session spawned before the channel existed is in the same position, and
    so is every run that never asked anything — which is almost all of them. The
    merge has to contribute nothing there, not merely nothing visible: the list is
    handed straight back, so no clock is computed and no ordering decision is made
    on a conversation that has only one source.
    """
    plant_session("s-plain", run=None)
    assert not ask.session_ask_dir("s-plain").exists()

    page = transcripts.read_messages("s-plain", limit=100)
    assert page.total == 8, "the transcript's own turn count changed"
    assert all(message.via is None for message in page.messages)
    assert [message.seq for message in page.messages] == list(range(8))

    # The shortcut itself, since "nothing visible changed" is the weaker claim.
    messages = list(page.messages)
    assert transcripts._interleave(messages, []) is messages


def test_an_empty_channel_contributes_nothing(platform_root):
    """A directory exists as soon as a session is spawned; most stay empty."""
    plant_session("s-empty-channel")
    before = transcripts.read_messages("s-empty-channel", limit=100).to_dict()

    ask.prepare_ask_dir("s-empty-channel")

    assert transcripts.read_messages("s-empty-channel", limit=100).to_dict() == before


def test_a_session_whose_transcript_never_mounted_still_shows_the_exchange(
    platform_root,
):
    """The channel is the whole conversation a session like this has.

    Common today: codex and pi keep their session files somewhere this adapter does
    not read, so the transcript resolves to nothing while the ask channel is fully
    there. An empty view would report the exchange as never having happened.
    """
    registry.register("s-no-transcript", pid=DEAD_PID, run=dict(RUN))
    plant_question("s-no-transcript", "which branch?", answer="prep-release")

    page = transcripts.read_messages("s-no-transcript", limit=100)
    assert said(page) == [("assistant", "which branch?"), ("user", "prep-release")]
    assert page.sources == ()


def test_an_unreadable_channel_costs_the_questions_and_nothing_else(
    platform_root, monkeypatch, caplog
):
    """Tolerance is the contract here as everywhere else in this module.

    The conversation is the transcript's; a channel directory that cannot be read
    must not be able to empty it.
    """
    plant_session("s-broken-channel")

    def boom(session_id):
        raise OSError("channel is gone")

    monkeypatch.setattr(ask, "read_entries", boom)

    page = transcripts.read_messages("s-broken-channel", limit=100)
    assert page.total == 8
    assert "platform_transcript_ask_unreadable" in caplog.text


# --- ordering -----------------------------------------------------------------

def test_the_exchange_lands_where_it_happened_in_the_transcript(platform_root):
    """Appending it would put an answer given mid-run at the end of the run.

    The fixture's turns are stamped 09:00:01 … 09:00:14; this exchange happened at
    :09 and :10, which is after the failed Edit (:08) and before ``/usage`` (:11).
    """
    plant_session("s-order")
    plant_question(
        "s-order",
        "which branch?",
        at=AFTER_THE_EDIT,
        answer="prep-release",
        answered_at=AFTER_THE_ANSWER,
    )

    turns = said(transcripts.read_messages("s-order", limit=100))
    positions = [
        turns.index(("assistant", "which branch?")),
        turns.index(("user", "prep-release")),
    ]
    assert positions == [5, 6]
    assert turns[4][0] == "assistant"          # the failed Edit, at :08
    assert turns[7] == ("system", "/usage\nusage")


def test_an_equal_timestamp_breaks_toward_the_transcript(platform_root):
    """Two clocks, two precisions: milliseconds against whole seconds.

    A channel entry stamped :08 collides with a transcript record stamped
    :08.000Z, and the tie cannot be left to luck — a merge that reordered itself
    between two polls would renumber history under a client's cursor. The
    transcript wins because the channel record is *caused by* the tool call the
    transcript is recording: the agent ran ``lmer-ask``, and that call is the
    transcript turn.
    """
    plant_session("s-tie")
    plant_question("s-tie", "which branch?", at=AT_THE_EDIT, answer="prep-release")

    first = transcripts.read_messages("s-tie", limit=100)
    turns = said(first)
    # The Edit turn is the fixture's :08; the question shares the instant.
    assert turns.index(("assistant", "which branch?")) == 5
    assert first.messages[4].tools[0].name == "Edit"

    # And it is stable: the same read twice cannot renumber anything.
    again = transcripts.read_messages("s-tie", limit=100)
    assert again.to_dict() == first.to_dict()


def test_the_transcript_keeps_its_file_order_whatever_its_timestamps_say(
    platform_root, tmp_path
):
    """A merge may interleave into a sequence; it may not reorder it.

    Harness timestamps are not guaranteed monotonic — a resumed session or a
    record written from a queue can date one turn before the turn above it — and a
    plain timestamp sort would move it, renumbering history for every cursor in
    flight. The clamp is what makes the file the authority on its own order.
    """
    registry.register("s-backwards", pid=DEAD_PID, run=dict(RUN))
    directory = transcripts.session_transcript_dir("s-backwards") / "-workspace"
    directory.mkdir(parents=True)
    rows = [
        {"type": "user", "timestamp": "2026-07-20T09:00:20.000Z",
         "message": {"role": "user", "content": "first, late-dated"}},
        {"type": "assistant", "timestamp": "2026-07-20T09:00:02.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "second, early-dated"},
         ]}},
    ]
    (directory / "backwards.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    plant_question("s-backwards", "third?", at="2026-07-20T09:00:30Z", answer="yes")

    turns = said(transcripts.read_messages("s-backwards", limit=100))
    assert turns == [
        ("user", "first, late-dated"),
        ("assistant", "second, early-dated"),
        ("assistant", "third?"),
        ("user", "yes"),
    ]


def test_an_answer_never_renders_above_its_own_question(platform_root):
    """The clamp again, on the channel side, where a stamp can simply be missing.

    An answer with no ``answered_at`` inherits its question's instant rather than
    floating to the top of the conversation, and an answer somehow dated *before*
    its question is held to the question's time.
    """
    plant_session("s-clamped")
    plant_question(
        "s-clamped",
        "which branch?",
        at=AFTER_THE_EDIT,
        answer="prep-release",
        answered_at=BEFORE_EDIT,
    )

    turns = said(transcripts.read_messages("s-clamped", limit=100))
    assert turns.index(("assistant", "which branch?")) < turns.index(
        ("user", "prep-release")
    )


def test_an_undated_channel_record_is_ordered_by_its_own_sequence(platform_root):
    """Corruption path, but it must not be a random one.

    ``at`` is always written by the protocol, so a record without one has been
    damaged. The channel's own sequence order is then what it is placed by — never
    a guess at a time it might have had.
    """
    plant_session("s-undated")
    plant_question("s-undated", "first?", at=AFTER_THE_EDIT, answer="yes")
    plant_question("s-undated", "second?", at="", answer="also yes")

    channel = [
        message.text for message in transcripts.read_messages("s-undated", limit=100).messages
        if message.via == transcripts.ASK_CHANNEL_VIA
    ]
    assert channel == ["first?", "yes", "second?", "also yes"]


def test_two_sessions_of_one_run_keep_their_channels_apart(platform_root):
    """A channel belongs to one container, so it merges into one transcript.

    Interleaving the earlier session's questions into the later session's turns
    would show an answer in a conversation that never received it — and the
    session boundary is what keeps the run's overall order from becoming a global
    timestamp sort.
    """
    plant_session("s-20260720T090000-aaaa")
    plant_session("s-20260720T113000-bbbb", fixture=FOLLOWUP_FIXTURE)
    # Dated inside the *first* session's window on purpose: the session grouping,
    # not the timestamp, has to decide which transcript it lands in.
    plant_question("s-20260720T113000-bbbb", "which branch?", at=AFTER_THE_EDIT,
                   answer="prep-release")

    turns = said(transcripts.read_messages("s-20260720T113000-bbbb", limit=100))
    assert turns.index(("assistant", "which branch?")) >= 8, (
        "the second session's question was merged into the first session's turns"
    )


def test_the_cursor_still_neither_skips_nor_repeats_across_a_merge(platform_root):
    """Paging is the whole basis of the poll, and the merge renumbers a session."""
    plant_session("s-paged")
    plant_question("s-paged", "which branch?", answer="prep-release")

    seen = []
    cursor = 0
    while True:
        page = transcripts.read_messages("s-paged", since=cursor, limit=3)
        if not page.messages:
            break
        seen.extend(message.seq for message in page.messages)
        cursor = page.cursor

    assert seen == list(range(10)), "a page skipped or repeated a turn"
    assert transcripts.read_messages("s-paged", limit=100).total == 10


# --- the credential scrub reaches the merged turns ---------------------------

def test_a_credential_pasted_into_an_answer_is_masked(platform_root):
    """An operator answering from a phone can paste a token into the box.

    Merged turns are served by the same path as transcript turns, so they go
    through the same chokepoint — that is what makes the scrub a property of the
    module rather than of whichever caller remembered.
    """
    leak = "glpat-" + "a" * 24
    plant_session("s-leak")
    plant_question(
        "s-leak",
        f"push with --fastapi-token {leak}?",
        answer=f"yes, use {leak}",
    )

    payload = json.dumps(transcripts.read_messages("s-leak", limit=100).to_dict())
    assert leak not in payload, "a credential in an answer reached the browser"
    assert "<redacted>" in payload


def test_the_platforms_own_secret_is_masked_in_an_answer(platform_root):
    """The credential that spawns containers, matched by value rather than shape.

    Nothing shaped can find it: it has no prefix, no delimiter and no giveaway, so
    an operator quoting it back in an answer is only caught because the value is
    known.
    """
    secret = cfg.ensure_secret(cfg.load())
    plant_session("s-secret")
    plant_question("s-secret", "what is the token?", answer=f"it is {secret}")

    page = transcripts.read_messages("s-secret", limit=100)
    payload = json.dumps(page.to_dict())
    assert secret not in payload
    assert "<redacted>" in payload


def test_the_channel_file_itself_is_left_as_written(platform_root):
    """Stated so the story stays consistent, not because it is comfortable.

    A transcript is rewritten when its session ends (``scrub_transcript``) because
    it is a file the platform mounted out and now owns. An answer file is not: the
    container it is mounted into was *handed* that answer, so there is nothing left
    to withhold from it, and the record sits 0600 in a 0700 directory. The scrub's
    job here is the one it has everywhere in this module — what reaches a browser.
    """
    leak = "glpat-" + "b" * 24
    plant_session("s-ondisk")
    question_id = plant_question("s-ondisk", "which token?", answer=f"use {leak}")

    answer_file = (
        ask.session_ask_dir("s-ondisk") / f"{question_id}{protocol.ANSWER_SUFFIX}"
    )
    assert leak in answer_file.read_text(encoding="utf-8")

    # The write-path scrub runs over the session's transcripts and stops there.
    assert transcripts.scrub_session_transcripts("s-ondisk") == 1
    assert leak in answer_file.read_text(encoding="utf-8")

    # And the read path masks it anyway, which is the promise that matters.
    page = transcripts.read_messages("s-ondisk", limit=100)
    assert leak not in json.dumps(page.to_dict())


def test_a_very_long_answer_is_trimmed_like_any_other_turn(platform_root):
    """The channel allows more characters than one turn is served with.

    ``MAX_ANSWER_CHARS`` is twice ``TEXT_LIMIT``, so a merged answer has to pass
    through the same truncation as everything else — and keep the same end, since
    what a person types last is what they concluded.
    """
    assert protocol.MAX_ANSWER_CHARS > transcripts.TEXT_LIMIT
    body = "x" * (transcripts.TEXT_LIMIT + 200) + " the actual decision"
    plant_session("s-long")
    plant_question("s-long", "which branch?", answer=body)

    merged = [
        message for message in transcripts.read_messages("s-long", limit=100).messages
        if message.role == "user" and message.via == transcripts.ASK_CHANNEL_VIA
    ]
    assert len(merged) == 1
    assert merged[0].truncated
    assert len(merged[0].text) <= transcripts.TEXT_LIMIT
    assert merged[0].text.endswith("the actual decision")


# --- the route that serves it -------------------------------------------------

def test_the_messages_route_serves_the_merged_turns(platform_root):
    """Wiring check: ``api.py`` gets this for free, and must actually get it.

    The merge lives in :mod:`lmer_platform.transcripts` precisely so the route
    keeps one call and one exception mapping.
    """
    plant_session("s-20260720T090000-cccc")
    plant_log("s-20260720T090000-cccc")
    plant_question("s-20260720T090000-cccc", "which branch?", answer="prep-release")

    client = TestClient(
        api.create_app(
            cfg.load(), SECRET, state_builder=lambda config, force_pull=False: {}
        )
    )
    reply = client.get(
        "/api/sessions/s-20260720T090000-cccc/messages?limit=100",
        headers={"Authorization": f"Bearer {SECRET}"},
    )
    assert reply.status_code == 200
    payload = reply.json()
    merged = [
        message for message in payload["messages"]
        if message["via"] == transcripts.ASK_CHANNEL_VIA
    ]
    assert [(message["role"], message["text"]) for message in merged] == [
        ("assistant", "which branch?"), ("user", "prep-release"),
    ]


# --- the view's half ----------------------------------------------------------

def _chat():
    return CHAT.read_text(encoding="utf-8")


def test_a_merged_answer_is_not_run_through_the_markdown_renderer():
    """The user asked for "something i sent", and that is a verbatim bubble.

    The property is bought by attribution rather than by a special case: a merged
    answer carries ``role: 'user'``, so it falls into the branch that was already
    there for everything the operator sent. A second branch keyed on ``via`` would
    be a second place for the renderer to creep into.
    """
    text = _chat()
    assert 'v-if="message.text && message.role === \'user\'"' in text, (
        "the operator's own turns no longer key the verbatim branch on the role"
    )
    assert text.count("said plain") == 2, (
        "a rendering branch was added or removed; the merged answer must reuse the "
        "one that already shows a sent message verbatim"
    )
    assert re.search(r'<Markdown\s+v-else-if="message\.text"', text), (
        "the rendered half is no longer the fallthrough, so a turn could reach it "
        "by some route other than not being the operator's"
    )
    assert text.count("message.via") == 1, (
        "`via` is consulted somewhere besides the header — where a turn came from "
        "must not decide how its words are rendered"
    )


def test_a_merged_turn_says_where_it_came_from():
    """Neither half of the exchange is passed off as transcript content.

    The answer is genuinely the operator's and the question is genuinely the
    agent's, but the harness wrote down neither — so the header says so, and an
    operator comparing the chat against the terminal is not left wondering why two
    turns are missing from the one and present in the other.
    """
    text = _chat()
    assert "message.via === 'ask'" in text, "a merged turn is indistinguishable"
    assert "ask channel" in text


def test_the_server_and_the_view_agree_on_the_marker():
    """One spelling of the marker, checked across the seam rather than assumed."""
    assert transcripts.ASK_CHANNEL_VIA == "ask"
    assert f"message.via === '{transcripts.ASK_CHANNEL_VIA}'" in _chat()
