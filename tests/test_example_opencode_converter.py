"""Tests for the opencode worked example's converter (issue #296, spec §8).

The example drop-in in ``examples/harnesses/opencode/`` is the proof that a
harness with no in-tree support can reach the chat view: its converter runs in
the container, emits the canonical format documented in
``docs/TRANSCRIPT-FORMAT.md``, and the platform's canonical adapter reads it
back. So this exercises the whole chain in one place — converter → golden file →
:func:`lmer_platform.transcripts.normalise_records` → the conversation the API
would serve — because each half is only interesting if the other half agrees.

The fixtures under ``tests/fixtures/opencode/`` are **captured**, not invented:
``opencode session list --format json`` and ``opencode export <sessionID>`` from
opencode 1.18.18 driven against a fake OpenAI-compatible endpoint on 127.0.0.1
(no credentials, the capture method from the #280 run). Two exports of the same
session are checked in — one taken while its tool call was still running, one
after it finished — because that pair is what the interesting properties need:

- **Injected material must not read as an operator turn.** The captured user
  message carries a ``synthetic`` text part *and* a typed one, which is
  opencode's own provenance and the only thing the converter may decide
  ``kind`` from.
- **The output is append-only.** The second pass may only add lines: a client
  polling the chat view holds a cursor into this file, so a rewritten turn is a
  skipped or repeated one on someone's phone.
- **A tool that finishes after its turn was written still resolves.** That is
  the whole reason ``lmer.tool_update`` exists.

The converter is driven with a stub ``opencode`` on the path that replays the
captured JSON, so nothing here needs opencode installed or a model reachable.
"""

import copy
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lmer_cli.user_harnesses import (
    HARNESSES_DIR_ENV,
    clear_user_harness_cache,
    refresh_user_harnesses,
)
from lmer_platform import spawn, transcripts

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples" / "harnesses"
CONVERTER = EXAMPLES / "opencode" / "converter.py"
FIXTURES = REPO / "tests" / "fixtures" / "opencode"
SESSION_LIST = FIXTURES / "session-list.json"
EXPORT_RUNNING = FIXTURES / "export-running.json"
EXPORT_FULL = FIXTURES / "export-full.json"
GOLDEN = FIXTURES / "golden-canonical.jsonl"

#: The working directory the captured session names. The converter picks the
#: newest session *for its directory*, so the stub's listing has to be asked
#: about the same one.
CAPTURED_DIRECTORY = "/home/developer/workspace"

#: The captured session's id, which is also the output file's stem — the id the
#: API reports for the transcript.
CAPTURED_SESSION = "ses_ffd6166e7ffe9F2LjXeJrj8wxF"

STUB = """#!/bin/bash
# Replay captured opencode output: `session list --format json`, `export <id>`.
case "$1" in
    session) cat "$OPENCODE_SESSION_LIST" ;;
    export)  cat "$OPENCODE_EXPORT" ;;
    *) echo "stub: unexpected $*" >&2; exit 2 ;;
esac
"""


@pytest.fixture
def opencode_stub(tmp_path):
    """A stand-in ``opencode`` binary that replays the captured fixtures."""
    stub = tmp_path / "opencode-stub"
    stub.write_text(STUB)
    stub.chmod(0o755)
    return stub


def convert(stub, out_dir, export):
    """Run one deterministic single pass of the converter over *export*."""
    env = dict(os.environ)
    env["OPENCODE_SESSION_LIST"] = str(SESSION_LIST)
    env["OPENCODE_EXPORT"] = str(export)
    done = subprocess.run(
        [sys.executable, str(CONVERTER), "--once",
         "--opencode", str(stub),
         "--output-dir", str(out_dir),
         "--directory", CAPTURED_DIRECTORY],
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert done.returncode == 0, done.stderr
    return done


def lines(path):
    return [line for line in path.read_text().splitlines() if line.strip()]


def records(path):
    return [json.loads(line) for line in lines(path)]


def converter_module():
    """The example converter, imported.

    For the one property a subprocess cannot express: what a *failed write*
    leaves behind. The rest of this file drives the converter the way
    ``runner.sh`` does, since that is what ships.
    """
    spec = importlib.util.spec_from_file_location("opencode_converter", CONVERTER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def grown_export(tmp_path, name="export-grown.json"):
    """The captured finished export, plus a second tool call and a closing line.

    Built from the captured parts rather than invented: the assistant message
    that already carried one ``bash`` call gets a second one and a further text
    part appended to it — the shape a multi-tool turn has, and the shape that
    used to be dropped when a message was consumed whole the first time it was
    seen.
    """
    export = json.loads(EXPORT_FULL.read_text())
    turn = export["messages"][1]
    tool = copy.deepcopy(
        [part for part in turn["parts"] if part["type"] == "tool"][0]
    )
    tool["id"] = "prt_grown_tool"
    tool["callID"] = "call_fake_2"
    tool["state"]["status"] = "running"
    tool["state"].pop("output", None)
    tool["state"]["input"] = {"command": "git status --short",
                              "description": "Check the tree"}
    text = copy.deepcopy(
        [part for part in turn["parts"] if part["type"] == "text"][0]
    )
    text["id"] = "prt_grown_text"
    text["text"] = "Now checking the tree."
    turn["parts"] = turn["parts"] + [text, tool]
    path = tmp_path / name
    path.write_text(json.dumps(export, indent=2))
    return path


def test_the_example_drop_in_loads_as_a_user_harness():
    """The example directory is a drop-in root, so load it as one.

    ``examples/harnesses/`` has exactly the shape ``~/.lmer/harnesses/`` has, and
    the walkthrough tells the reader to copy it there — so a manifest that the
    loader would reject, or a runner the entrypoint would not find, is a broken
    promise this suite can catch. ``session_dir`` is the line that makes the
    converter's output reach the host at all.
    """
    harnesses = refresh_user_harnesses(EXAMPLES)

    assert "opencode" in harnesses
    opencode = harnesses["opencode"]
    assert opencode.binary == "opencode"
    assert opencode.session_dir == "/home/developer/.opencode-transcripts"
    assert (EXAMPLES / "opencode" / "runner.sh").is_file()
    assert CONVERTER.is_file()


def test_the_examples_declared_session_dir_survives_the_spawn_checks(monkeypatch):
    """Parsing is not accepting: the platform decides at spawn whether to mount.

    A ``session_dir`` the manifest parser likes can still be refused when the
    session starts — it is checked against what the platform already mounts,
    the container home, the staging area, and whatever it would cover
    (``_harness_session_dirs``). A refusal is deliberately quiet: the harness
    runs, the converter writes, and nothing reaches the host. So a drop-in the
    walkthrough says to copy has to be run through the real decision, not
    through the parser alone — otherwise a tightened check, or an edit to this
    manifest, leaves a copyable example that is silently transcript-less.

    ``_harness_session_dirs`` is private, and is also the whole decision: it
    reads the merged registry and returns the directories that will be mounted.
    Pointing the drop-in directory at ``examples/harnesses`` is what the
    walkthrough's copy step does, with one fewer copy.
    """
    monkeypatch.setenv(HARNESSES_DIR_ENV, str(EXAMPLES))
    clear_user_harness_cache()
    try:
        mounted = spawn._harness_session_dirs()
    finally:
        clear_user_harness_cache()

    assert mounted.get("opencode") == "/home/developer/.opencode-transcripts"
    # The built-ins are untouched by a drop-in being present.
    assert {"claude", "codex", "pi"} <= set(mounted)


def test_converted_export_matches_the_golden_canonical_file(
    tmp_path, opencode_stub
):
    """Two passes over a session that grew produce exactly the golden file.

    Byte equality rather than a shape assertion: this file is the contract a
    third party copies, so a change in what the converter writes should show up
    as a diff to read, not as a test that still passes.
    """
    out_dir = tmp_path / "transcripts"
    convert(opencode_stub, out_dir, EXPORT_RUNNING)
    convert(opencode_stub, out_dir, EXPORT_FULL)

    written = out_dir / ("%s.jsonl" % CAPTURED_SESSION)
    assert written.read_text() == GOLDEN.read_text()


def test_the_header_declares_the_drop_ins_own_harness_name(
    tmp_path, opencode_stub
):
    """``lmer.meta`` is what makes the API label the source ``opencode``."""
    out_dir = tmp_path / "transcripts"
    convert(opencode_stub, out_dir, EXPORT_FULL)

    written = out_dir / ("%s.jsonl" % CAPTURED_SESSION)
    header = records(written)[0]
    assert header == {
        "type": "lmer.meta", "format": 1, "harness": "opencode",
        "generator": "opencode-converter/0.1",
        "native_format": "opencode-export/1",
    }

    _, source = transcripts.read_source(
        transcripts.Source(path=written, session="session-under-test")
    )
    assert source.harness == "opencode"


def test_a_second_pass_over_a_grown_export_only_appends(tmp_path, opencode_stub):
    """Delta-append: each export is a full snapshot, the file is not.

    The first pass sees a session whose tool call is still running; the second
    sees the same session finished, with one more assistant turn. What the
    first pass wrote must survive byte for byte — an in-flight client's cursor
    points into it — and the new lines must be the outcome and the new turn,
    nothing re-emitted.
    """
    out_dir = tmp_path / "transcripts"
    convert(opencode_stub, out_dir, EXPORT_RUNNING)
    written = out_dir / ("%s.jsonl" % CAPTURED_SESSION)
    first = written.read_text()
    first_lines = lines(written)

    convert(opencode_stub, out_dir, EXPORT_FULL)
    second_lines = lines(written)

    assert written.read_text().startswith(first)
    assert second_lines[:len(first_lines)] == first_lines

    appended = [json.loads(line) for line in second_lines[len(first_lines):]]
    assert [record["type"] for record in appended] == [
        "lmer.tool_update", "lmer.message",
    ]
    assert appended[0] == {
        "type": "lmer.tool_update", "id": "call_fake_1", "status": "ok",
    }
    assert appended[1]["role"] == "assistant"
    assert appended[1]["text"] == "The tool reported: hello from the fake tool."


def test_a_turn_that_grows_more_parts_is_continued_not_abandoned(
    tmp_path, opencode_stub
):
    """A message keeps growing after the poll that first converted it.

    A multi-tool turn is the ordinary case: the export shows ``[text, tool]``,
    the converter emits it, and by the next poll the same message carries
    another paragraph and a second call. Consumption is tracked per *part* for
    exactly this — a message consumed whole would lose everything appended to
    it, silently, and the turn would read as if the agent stopped after its
    first tool.
    """
    out_dir = tmp_path / "transcripts"
    convert(opencode_stub, out_dir, EXPORT_RUNNING)
    written = out_dir / ("%s.jsonl" % CAPTURED_SESSION)
    before = lines(written)

    convert(opencode_stub, out_dir, grown_export(tmp_path))
    after = lines(written)

    assert after[:len(before)] == before
    appended = [json.loads(line) for line in after[len(before):]]
    assert [record["type"] for record in appended] == [
        "lmer.tool_update", "lmer.message", "lmer.message",
    ]

    grown = appended[1]
    assert grown["native_id"] == json.loads(before[-1])["native_id"]
    assert grown["text"] == "Now checking the tree."
    assert [(tool["name"], tool["id"], tool["status"]) for tool in grown["tools"]] == [
        ("bash", "call_fake_2", "pending"),
    ]
    # The part span picks up where the first record stopped, so nothing between
    # them is converted twice or skipped.
    assert json.loads(before[-1])["native_parts"][1] == grown["native_parts"][0]

    messages = transcripts.normalise_records(records(written))
    assert [message.text for message in messages] == [
        messages[0].text,
        "say hi and run a quick shell command",
        "I will check the working tree.",
        "Now checking the tree.",
        "The tool reported: hello from the fake tool.",
    ]
    assert [(tool.name, tool.status)
            for message in messages for tool in message.tools] == [
        ("bash", "ok"), ("bash", "pending"),
    ]


def test_a_streaming_assistant_text_part_is_held_until_it_carries_an_end(
    tmp_path, opencode_stub
):
    """A streaming part waits for its own ``time.end`` — and for nothing else.

    Nothing can extend a record once it is written, so an assistant text part
    that is still growing must not be converted. What says it is still growing
    is the part's own ``time.end``, probed live against opencode 1.18.18 on a
    slow-streaming endpoint: the in-flight part carries ``{"start": …}`` and no
    ``end`` throughout, and gains ``end`` when it finishes.

    Asserted here in the shape that tells the two candidate rules apart — the
    streaming message is **not** the last one in the export. Under the position
    rule this file used to carry ("a later message proves it final") the half
    paragraph is released and frozen; under the part's own marker it waits,
    while the successor turn beside it is converted normally.

    A trailing *tool* part is different and is emitted at once: a call that has
    started is a fact, and ``lmer.tool_update`` carries its outcome — which is
    why the captured running export, whose trailing part is the tool, converts
    its whole turn.
    """
    export = json.loads(EXPORT_RUNNING.read_text())
    turn = export["messages"][-1]
    turn["parts"] = [part for part in turn["parts"] if part["type"] != "tool"]
    streaming = turn["parts"][-1]
    assert streaming["type"] == "text"
    streaming["time"].pop("end")          # the shape the probe observed
    streaming["text"] = "I will check the"
    # A successor message: the operator typing again while the reply streams.
    successor = copy.deepcopy(export["messages"][0])
    successor["info"]["id"] = "msg_successor"
    successor["info"]["time"] = {"created": turn["info"]["time"]["created"] + 5}
    successor["parts"] = [dict(successor["parts"][-1], id="prt_successor",
                               text="actually, never mind")]
    export["messages"].append(successor)
    mid_stream = tmp_path / "export-mid-stream.json"
    mid_stream.write_text(json.dumps(export, indent=2))

    out_dir = tmp_path / "transcripts"
    convert(opencode_stub, out_dir, mid_stream)

    written = out_dir / ("%s.jsonl" % CAPTURED_SESSION)
    messages = transcripts.normalise_records(records(written))
    # Every turn but the one still streaming, including the one *after* it.
    assert [(message.role, message.text) for message in messages] == [
        ("user", messages[0].text),
        ("user", "say hi and run a quick shell command"),
        ("user", "actually, never mind"),
    ]

    # The same text, once the part has ended, is converted whole — not the
    # truncated prefix that was in flight.
    convert(opencode_stub, out_dir, EXPORT_FULL)
    messages = transcripts.normalise_records(records(written))
    assert [message.text for message in messages][3:] == [
        "I will check the working tree.",
        "The tool reported: hello from the fake tool.",
    ]


def test_a_held_part_is_released_once_its_message_stops_changing(
    tmp_path, opencode_stub, monkeypatch
):
    """The wait for ``time.end`` is bounded, because that marker can never come.

    Probed live: interrupting ``opencode run`` mid-stream (SIGINT) leaves the
    assistant message with ``time.created`` alone and its trailing text part
    with ``time.start`` alone — no ``time.end``, no ``time.completed``, ever.
    A message that has stopped changing has no later poll that could change it,
    so an unbounded hold is a turn dropped for the rest of the session with no
    marker. It is released after :data:`STALLED_POLLS` polls in which nothing
    about the message moved.
    """
    module = converter_module()
    export = json.loads(EXPORT_RUNNING.read_text())
    turn = export["messages"][-1]
    turn["parts"] = [part for part in turn["parts"] if part["type"] != "tool"]
    interrupted = turn["parts"][-1]
    interrupted["time"].pop("end")
    interrupted["text"] = "I will check the"
    cut = tmp_path / "export-interrupted.json"
    cut.write_text(json.dumps(export, indent=2))

    monkeypatch.setenv("OPENCODE_SESSION_LIST", str(SESSION_LIST))
    monkeypatch.setenv("OPENCODE_EXPORT", str(cut))
    out_dir = tmp_path / "transcripts"
    converter = module.Converter(
        str(opencode_stub), str(out_dir), CAPTURED_DIRECTORY
    )
    written = out_dir / ("%s.jsonl" % CAPTURED_SESSION)

    converter.poll()
    held = [record.get("role") for record in records(written)]
    assert held == [None, "user", "user"]

    # A stream that is merely slow keeps moving, and keeps its part held.
    for poll in range(module.STALLED_POLLS):
        interrupted["text"] += " word"
        cut.write_text(json.dumps(export, indent=2))
        converter.poll()
        assert [record.get("role") for record in records(written)] == held, (
            "a growing part was released on poll %d" % poll
        )

    # Nothing moves from here on: the same export, polled again.
    for _ in range(module.STALLED_POLLS):
        converter.poll()

    messages = transcripts.normalise_records(records(written))
    assert messages[-1].role == "assistant"
    assert messages[-1].text == interrupted["text"]

    # And it is released exactly once, however long the session goes on.
    after = lines(written)
    converter.poll()
    converter.poll()
    assert lines(written) == after


def test_a_user_turn_is_emitted_before_any_reply_is_persisted(
    tmp_path, opencode_stub
):
    """The newest message being the user's must not withhold what they typed.

    Between the prompt landing and the first assistant message being persisted,
    the user's message is the only one in the export — and if the reply never
    lands (a provider error, an interrupt, the container going away) that is
    permanent. Their typed text is also what the chat view's pending bubble
    settles against, so holding it back is visible as a send that never
    arrives. User parts are not streamed: they arrive whole with the prompt.
    """
    export = json.loads(EXPORT_RUNNING.read_text())
    export["messages"] = export["messages"][:1]
    assert export["messages"][0]["info"]["role"] == "user"
    assert "completed" not in export["messages"][0]["info"]["time"]
    prompt_only = tmp_path / "export-prompt-only.json"
    prompt_only.write_text(json.dumps(export, indent=2))

    out_dir = tmp_path / "transcripts"
    convert(opencode_stub, out_dir, prompt_only)

    written = out_dir / ("%s.jsonl" % CAPTURED_SESSION)
    messages = transcripts.normalise_records(records(written))
    assert [(message.role, message.kind) for message in messages] == [
        ("user", "injected"), ("user", "said"),
    ]
    assert messages[1].text == "say hi and run a quick shell command"

    # And the reply, when it does land, is appended rather than re-run.
    before = lines(written)
    convert(opencode_stub, out_dir, EXPORT_FULL)
    assert lines(written)[:len(before)] == before


def test_a_write_that_fails_mid_message_resumes_at_the_missing_part(
    tmp_path, opencode_stub, monkeypatch
):
    """A half-flushed message is finished by the next pass, exactly once.

    The container can go away between two appends, and the guaranteed-final-pass
    pattern runs a fresh ``--once`` process into the file a dead one left. Both
    directions are failures worth catching: re-emitting a turn corrupts an
    append-only file that clients hold cursors into, and skipping the rest of a
    message loses it for good — which is what a message-granular resume did to
    the very window that pattern exists to close.
    """
    module = converter_module()
    monkeypatch.setenv("OPENCODE_SESSION_LIST", str(SESSION_LIST))
    monkeypatch.setenv("OPENCODE_EXPORT", str(EXPORT_RUNNING))

    out_dir = tmp_path / "transcripts"
    converter = module.Converter(
        str(opencode_stub), str(out_dir), CAPTURED_DIRECTORY
    )
    appended = {"count": 0}
    real_append = converter._append

    def failing_append(record):
        """Write the header and the first turn, then lose the disk."""
        appended["count"] += 1
        return real_append(record) if appended["count"] <= 2 else False

    converter._append = failing_append
    converter.poll()

    written = out_dir / ("%s.jsonl" % CAPTURED_SESSION)
    partial = lines(written)
    assert [json.loads(line)["type"] for line in partial] == [
        "lmer.meta", "lmer.message",
    ]

    # A fresh process, resuming from the file and nothing else.
    convert(opencode_stub, out_dir, EXPORT_RUNNING)
    resumed = lines(written)

    assert resumed[:len(partial)] == partial
    convert(opencode_stub, out_dir, EXPORT_FULL)
    assert lines(written) == lines(GOLDEN)


def test_a_file_left_empty_by_a_failed_header_still_gets_one(
    tmp_path, opencode_stub
):
    """An existing file is not the same as a headered one.

    Appending in mode ``a`` creates the file before it writes, so a header write
    that failed leaves nothing but a zero-byte file — and reading *that* as
    "already headered" would spend the rest of the session writing turns into a
    file with no ``lmer.meta``, which the API reads back labelled ``lmer``
    instead of ``opencode``. The state is what was read from the file, not that
    it opened.
    """
    out_dir = tmp_path / "transcripts"
    out_dir.mkdir()
    written = out_dir / ("%s.jsonl" % CAPTURED_SESSION)
    written.touch()

    convert(opencode_stub, out_dir, EXPORT_FULL)

    assert records(written)[0]["type"] == "lmer.meta"
    _, source = transcripts.read_source(
        transcripts.Source(path=written, session="session-under-test")
    )
    assert source.harness == "opencode"


def test_an_output_directory_that_is_not_there_yet_keeps_the_pinned_session(
    tmp_path, opencode_stub, monkeypatch
):
    """A retry re-opens the file; it does not re-choose the session.

    ``session_dir`` is a symlink the entrypoint creates and the linker is
    fail-soft, so the first polls of a session can find nothing to write into.
    Re-running discovery on that path would let a subagent's session — newer by
    construction the moment it starts — take over the conversation's file
    mid-session, which is what pinning exists to prevent.

    The unwritable directory is simulated rather than staged with permissions:
    a mode-based version of this test passes for an unprivileged user and fails
    for root, who is who CI runs as — and a test whose subject is which session
    got pinned should not depend on the uid it runs under.
    """
    module = converter_module()
    listing = tmp_path / "sessions.json"
    listing.write_text(SESSION_LIST.read_text())
    monkeypatch.setenv("OPENCODE_SESSION_LIST", str(listing))
    monkeypatch.setenv("OPENCODE_EXPORT", str(EXPORT_FULL))

    out_dir = tmp_path / "not-linked-yet" / "transcripts"

    def no_mount(*args, **kwargs):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(module.os, "makedirs", no_mount)
    converter = module.Converter(
        str(opencode_stub), str(out_dir), CAPTURED_DIRECTORY
    )
    assert converter.poll() is False
    assert converter.session_id == CAPTURED_SESSION
    assert converter.path is None

    # A subagent's session appears, newer than the pinned one, and the mount
    # arrives.
    sessions = json.loads(listing.read_text())
    subagent = dict(sessions[0])
    subagent["id"] = "ses_subagent_newer"
    subagent["updated"] = sessions[0]["updated"] + 1000
    listing.write_text(json.dumps(sessions + [subagent]))
    monkeypatch.undo()
    monkeypatch.setenv("OPENCODE_SESSION_LIST", str(listing))
    monkeypatch.setenv("OPENCODE_EXPORT", str(EXPORT_FULL))

    assert converter.poll() is True
    assert converter.session_id == CAPTURED_SESSION
    assert [path.name for path in out_dir.iterdir()] == [
        "%s.jsonl" % CAPTURED_SESSION
    ]


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("YES", True), ("on", True),
    ("0", False), ("false", False), ("", False), ("no", False),
])
def test_the_single_pass_env_var_is_read_as_a_flag_not_as_presence(
    monkeypatch, value, expected
):
    """``LMER_OPENCODE_ONCE=0`` means no, as it does everywhere else in lmer."""
    module = converter_module()
    monkeypatch.setenv("LMER_OPENCODE_ONCE", value)
    assert module.parse_args([]).once is expected


def test_a_running_tool_is_pending_until_its_update_arrives(
    tmp_path, opencode_stub
):
    """The first pass may not guess an outcome it has not been told."""
    out_dir = tmp_path / "transcripts"
    convert(opencode_stub, out_dir, EXPORT_RUNNING)

    written = out_dir / ("%s.jsonl" % CAPTURED_SESSION)
    messages = transcripts.normalise_records(records(written))
    tools = [tool for message in messages for tool in message.tools]
    assert [(tool.name, tool.status) for tool in tools] == [("bash", "pending")]


def test_the_converted_session_reads_back_as_the_captured_conversation(
    tmp_path, opencode_stub
):
    """The point of the whole example: what the chat view would show.

    A user turn the operator typed, the context opencode injected alongside it
    marked ``injected`` rather than drawn as words a person said, and an
    assistant turn whose tool call carries the outcome that arrived after the
    turn was written.
    """
    out_dir = tmp_path / "transcripts"
    convert(opencode_stub, out_dir, EXPORT_RUNNING)
    convert(opencode_stub, out_dir, EXPORT_FULL)

    written = out_dir / ("%s.jsonl" % CAPTURED_SESSION)
    messages = transcripts.normalise_records(records(written))

    assert [(message.role, message.kind) for message in messages] == [
        ("user", "injected"),
        ("user", "said"),
        ("assistant", "said"),
        ("assistant", "said"),
    ]

    injected, said, acting, reporting = messages
    assert injected.text.startswith("Called the Read tool")
    assert said.text == "say hi and run a quick shell command"
    assert said.tools == []

    assert acting.text == "I will check the working tree."
    assert len(acting.tools) == 1
    call = acting.tools[0]
    assert call.name == "bash"
    assert call.detail == "sleep 6; echo hello from the fake tool"
    assert call.status == "ok"
    assert call.error is None

    assert reporting.text == "The tool reported: hello from the fake tool."
    # Timestamps ride along for the ask-channel interleave; file order is what
    # decides conversation order, so all that matters is that they parse.
    assert all(message.at and message.at.endswith("Z") for message in messages)


def test_an_unreadable_export_costs_nothing(tmp_path, opencode_stub):
    """A failed ``opencode`` invocation is retried, never fatal.

    The converter is backgrounded beside a live session: it exits non-zero for
    nobody, and it must not leave a half-written turn behind when the CLI
    answers with something that is not an export.
    """
    out_dir = tmp_path / "transcripts"
    convert(opencode_stub, out_dir, EXPORT_FULL)
    written = out_dir / ("%s.jsonl" % CAPTURED_SESSION)
    before = written.read_text()

    torn = tmp_path / "torn.json"
    torn.write_text('{"info": {"id": "ses_x"}, "messa')
    convert(opencode_stub, out_dir, torn)

    assert written.read_text() == before
