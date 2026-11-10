"""IncrementalReader gives summarise()'s numbers from appended bytes only (#315, #647)."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lmer_usage import buckets
from lmer_usage.incremental import VERSION, IncrementalReader
from lmer_usage.summarise import active_seconds, summarise

FIXTURES = Path(__file__).parent / "fixtures" / "usage"
IDLE = 300

SESSIONS = {
    "claude": (FIXTURES / "claude" / "session-a", [
        "main.jsonl", "resumed.jsonl",
        "subagents/agent-abc123.jsonl", "subagents/agent-def456.jsonl",
    ]),
    "codex": (FIXTURES / "codex", ["rollout-main.jsonl", "rollout-copied.jsonl", "rollout-fork.jsonl"]),
    "pi": (FIXTURES / "pi", ["session-tree.jsonl", "session-fork.jsonl"]),
}


def _chunks(data: bytes) -> list:
    """Two to four pieces of ``data``; the first always ends mid-line."""
    first_line_end = data.index(b"\n")
    cuts = sorted({c for c in (first_line_end // 2, len(data) // 2, len(data) - 2) if 0 < c < len(data)})
    pieces = [data[a:b] for a, b in zip([0] + cuts, cuts + [len(data)])]
    assert 2 <= len(pieces) <= 4 and not pieces[0].endswith(b"\n")
    return pieces


def _session(tmp_path, harness):
    """Empty copies of one fixture session's transcripts (sidecar files copied
    whole), and each transcript's full content."""
    source, names = SESSIONS[harness]
    for meta in source.rglob("*.meta.json"):
        target = tmp_path / meta.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(meta, target)
    files, contents = [], {}
    for name in names:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"")
        files.append(target)
        contents[target] = (source / name).read_bytes()
    return files, contents


def _append(path, data):
    with open(path, "ab") as handle:
        handle.write(data)


def _feed(reader, files, contents, *, rounds=None):
    """Append every file's chunks round-robin, calling update() after each chunk.

    Returns the events update() handed back. ``rounds`` stops early, leaving
    the rest of every file unwritten."""
    pending = {path: _chunks(contents[path]) for path in files}
    returned = []
    step = 0
    while any(pending.values()) and (rounds is None or step < rounds):
        for path in files:
            if pending[path]:
                _append(path, pending[path].pop(0))
                returned.extend(reader.update(files))
        step += 1
    return returned, pending


def _assert_same(got, expected):
    assert expected.totals["requests"] > 0
    assert got.rows == expected.rows
    assert got.days == expected.days
    assert got.totals == expected.totals
    assert got.started_at == expected.started_at
    assert got.ended_at == expected.ended_at
    assert got.active_seconds == expected.active_seconds
    # Float addition order follows the order keys were first seen, which
    # interleaved reads change; the amounts themselves are the same.
    assert got.reported_cost == pytest.approx(expected.reported_cost)
    assert got.harnesses == expected.harnesses
    assert got.not_collected == expected.not_collected


def _full_reader(tmp_path, harness):
    files, contents = _session(tmp_path, harness)
    for path in files:
        path.write_bytes(contents[path])
    reader = IncrementalReader(idle_seconds=IDLE)
    reader.update(files)
    return reader, files


def _assistant(message_id, timestamp, *, output=5):
    return json.dumps({
        "type": "assistant", "timestamp": timestamp, "uuid": f"{message_id}-{timestamp}",
        "message": {"id": message_id, "model": "claude-opus-5-5",
                    "usage": {"input_tokens": 1, "output_tokens": output}},
    }).encode() + b"\n"


class TestEqualsSummarise:
    @pytest.mark.parametrize("harness", sorted(SESSIONS))
    def test_chunked_appends_equal_a_full_read(self, tmp_path, harness):
        files, contents = _session(tmp_path, harness)
        reader = IncrementalReader(idle_seconds=IDLE)
        returned, _ = _feed(reader, files, contents)
        expected = summarise(files, idle_seconds=IDLE)
        _assert_same(reader.summary(), expected)
        # Every response was handed back exactly once.
        keys = [event.key for event in returned]
        assert len(keys) == len(set(keys)) == expected.totals["requests"]

    def test_idle_threshold_follows_the_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LMER_USAGE_IDLE_SECONDS", "4000")
        files, contents = _session(tmp_path, "claude")
        reader = IncrementalReader()
        _feed(reader, files, contents)
        assert reader.summary().active_seconds == summarise(files).active_seconds > 3540


class TestPartialLines:
    def test_a_partial_last_line_is_read_once_it_completes(self, tmp_path):
        path = tmp_path / "s.jsonl"
        line = _assistant("m2", "2026-11-05T10:00:01Z")
        path.write_bytes(_assistant("m1", "2026-11-05T10:00:00Z") + line[:20])
        reader = IncrementalReader(idle_seconds=IDLE)
        assert [e.key for e in reader.update([path])] == [("claude", "m1")]
        assert reader.to_json()["files"][str(path)]["offset"] == len(_assistant("m1", "2026-11-05T10:00:00Z"))
        _append(path, line[20:])
        assert [e.key for e in reader.update([path])] == [("claude", "m2")]


class TestRestart:
    @pytest.mark.parametrize("harness", sorted(SESSIONS))
    def test_a_replaced_file_is_reread_without_new_events(self, tmp_path, harness):
        reader, files = _full_reader(tmp_path, harness)
        before = reader.summary()
        target = files[0]
        data = target.read_bytes()
        old_inode = target.stat().st_ino
        temp = target.with_name(target.name + ".tmp")
        temp.write_bytes(data)
        os.replace(temp, target)
        assert target.stat().st_ino != old_inode
        assert reader.update(files) == []
        state = reader.to_json()["files"][str(target)]
        assert (state["inode"], state["offset"]) == (target.stat().st_ino, len(data))
        _assert_same(reader.summary(), before)
        # The file's own timestamps are not counted twice.
        assert "stamps" not in state and reader.summary().active_seconds == before.active_seconds

    @pytest.mark.parametrize("harness", sorted(SESSIONS))
    def test_a_truncated_and_rewritten_file_gives_no_new_events(self, tmp_path, harness):
        reader, files = _full_reader(tmp_path, harness)
        before = reader.summary()
        target = files[0]
        data = target.read_bytes()
        with open(target, "r+b") as handle:
            handle.truncate(len(data) // 2)
        assert reader.update(files) == []
        assert reader.to_json()["files"][str(target)]["offset"] <= len(data) // 2
        target.write_bytes(data)
        assert reader.update(files) == []
        assert reader.to_json()["files"][str(target)]["offset"] == len(data)
        _assert_same(reader.summary(), before)

    def test_a_restart_returns_only_keys_it_never_saw(self, tmp_path):
        path = tmp_path / "s.jsonl"
        path.write_bytes(_assistant("m1", "2026-11-05T10:00:00Z"))
        reader = IncrementalReader(idle_seconds=IDLE)
        reader.update([path])
        temp = tmp_path / "s.tmp"
        temp.write_bytes(_assistant("m1", "2026-11-05T10:00:00Z") + _assistant("m9", "2026-11-05T10:00:09Z"))
        os.replace(temp, path)
        assert [e.key for e in reader.update([path])] == [("claude", "m9")]


class TestNewEventsOnly:
    def test_one_appended_response_is_one_event_at_its_own_time(self, tmp_path):
        reader, files = _full_reader(tmp_path, "claude")
        assert reader.update(files) == []
        _append(files[0], _assistant("msg_new", "2026-11-07T12:00:00Z", output=21))
        events = reader.update(files)
        assert len(events) == 1
        event = events[0]
        assert event.key == ("claude", "msg_new")
        assert event.at == datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
        assert event.counters["output"] == 21
        assert reader.update(files) == []
        _assert_same(reader.summary(), summarise(files, idle_seconds=IDLE))

    def test_a_response_split_over_records_in_one_read_is_one_event(self, tmp_path):
        path = tmp_path / "s.jsonl"
        path.write_bytes(_assistant("m1", "2026-11-05T10:00:00Z") + _assistant("m1", "2026-11-05T10:00:01Z"))
        events = IncrementalReader(idle_seconds=IDLE).update([path])
        assert [e.key for e in events] == [("claude", "m1")]

    def test_a_key_seen_in_another_file_is_not_returned(self, tmp_path):
        first, second = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
        first.write_bytes(_assistant("m1", "2026-11-05T10:00:00Z"))
        second.write_bytes(b"")
        reader = IncrementalReader(idle_seconds=IDLE)
        assert len(reader.update([first, second])) == 1
        second.write_bytes(_assistant("m1", "2026-11-05T10:00:00Z"))
        assert reader.update([first, second]) == []


class TestJsonRoundTrip:
    @pytest.mark.parametrize("harness", sorted(SESSIONS))
    def test_state_survives_json_and_reading_continues(self, tmp_path, harness):
        files, contents = _session(tmp_path, harness)
        reader = IncrementalReader(idle_seconds=IDLE)
        first, pending = _feed(reader, files, contents, rounds=1)
        assert any(pending.values())
        data = json.loads(json.dumps(reader.to_json()))
        assert data["version"] == VERSION == 2
        restored = IncrementalReader.from_json(data)
        assert restored.to_json() == reader.to_json()
        returned = list(first)
        while any(pending.values()):
            for path in files:
                if pending[path]:
                    _append(path, pending[path].pop(0))
                    returned.extend(restored.update(files))
        expected = summarise(files, idle_seconds=IDLE)
        _assert_same(restored.summary(), expected)
        keys = [event.key for event in returned]
        assert len(keys) == len(set(keys)) == expected.totals["requests"]

    def test_codex_previous_comes_back_as_a_tuple(self, tmp_path):
        reader, files = _full_reader(tmp_path, "codex")
        restored = IncrementalReader.from_json(json.loads(json.dumps(reader.to_json())))
        _append(files[0], json.dumps({
            "timestamp": "2026-11-05T09:04:00Z", "type": "event_msg",
            "payload": {"type": "token_count", "info": {"total_token_usage": {
                "input_tokens": 900, "cached_input_tokens": 300, "cache_write_input_tokens": 0,
                "output_tokens": 70, "reasoning_output_tokens": 10}}},
        }).encode() + b"\n")
        events = restored.update(files)
        assert len(events) == 1 and events[0].counters["input"] == 400


class TestSpans:
    def test_out_of_order_and_bridging_stamps_equal_the_sorted_sum(self):
        base = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc).timestamp()
        offsets = [0, 50, 400, 30, 1000, 260, 100, 900, 2000, 1500, 2000]
        reader = IncrementalReader(idle_seconds=IDLE)
        for offset in offsets:
            reader._add_stamp(base + offset)
        stamps = [datetime.fromtimestamp(base + o, timezone.utc) for o in offsets]
        summary = reader.summary()
        assert summary.active_seconds == active_seconds(stamps, IDLE)
        assert (summary.started_at, summary.ended_at) == (min(stamps), max(stamps))
        assert all(a[1] + IDLE < b[0] for a, b in zip(reader._spans, reader._spans[1:]))

    def test_a_record_without_usage_that_moves_the_spans_is_a_revision(self, tmp_path):
        reader, files = _full_reader(tmp_path, "claude")
        held, ended = reader.revision, reader.summary().ended_at
        later = ended.timestamp() + 30
        reader._add_stamp(later)
        assert reader.revision == held + 1
        assert reader.summary().ended_at.timestamp() == later
        reader._add_stamp(later)
        reader._add_stamp(ended.timestamp())
        assert reader.revision == held + 1

    def test_re_reading_the_same_records_is_not_a_revision(self, tmp_path):
        reader, files = _full_reader(tmp_path, "claude")
        held = reader.revision
        for path in files:
            copy = path.with_name(f".{path.name}.tmp")
            copy.write_bytes(path.read_bytes())
            os.replace(copy, path)
        reader.update(files)
        assert reader.revision == held


class TestBadState:
    def _assert_fresh(self, reader):
        assert reader.to_json() == IncrementalReader().to_json()
        assert reader.summary().totals == buckets.zero()

    @pytest.mark.parametrize("data", [
        None, {"garbage": 1}, [], "x", {"version": 2}, {"version": True},
        {"version": 1, "files": "x"},
        {"version": 1, "events": [{"key": "not-a-list"}]},
        {"version": 1, "files": {"/x": {"dev": -1, "inode": 1, "size": 0, "offset": 0,
                                        "states": {}, "seen": [], "stamps": []}}},
    ])
    def test_malformed_state_gives_a_fresh_reader(self, data):
        self._assert_fresh(IncrementalReader.from_json(data))

    def test_a_wrong_version_of_good_state_is_refused(self, tmp_path):
        reader, files = _full_reader(tmp_path, "claude")
        data = reader.to_json()
        data["version"] = VERSION + 1
        restored = IncrementalReader.from_json(data)
        self._assert_fresh(restored)
        # A fresh reader starts over and so returns every response again.
        assert len(restored.update(files)) == reader.summary().totals["requests"]

    def test_bad_counters_are_refused(self, tmp_path):
        reader, _ = _full_reader(tmp_path, "pi")
        data = json.loads(json.dumps(reader.to_json()))
        data["events"][0]["counters"]["output"] = "lots"
        self._assert_fresh(IncrementalReader.from_json(data))

    def test_an_adapter_state_of_the_wrong_shape_is_refused(self, tmp_path):
        reader, files = _full_reader(tmp_path, "codex")
        data = json.loads(json.dumps(reader.to_json()))
        del data["files"][str(files[0])]["states"]["codex"]["previous"]
        self._assert_fresh(IncrementalReader.from_json(data))


class TestUnsafeFiles:
    def test_a_symlinked_transcript_is_refused(self, tmp_path):
        target = tmp_path / "real.jsonl"
        target.write_bytes(_assistant("m1", "2026-11-05T10:00:00Z"))
        link = tmp_path / "link.jsonl"
        link.symlink_to(target)
        reader = IncrementalReader(idle_seconds=IDLE)
        assert reader.update([link]) == []
        summary = reader.summary()
        assert summary.totals == buckets.zero()
        assert summary.not_collected[0]["file"] == str(link)
        assert "unreadable" in summary.not_collected[0]["reason"]

    def test_a_fifo_does_not_block_and_a_missing_file_does_not_raise(self, tmp_path):
        fifo = tmp_path / "pipe.jsonl"
        os.mkfifo(fifo)
        reader = IncrementalReader(idle_seconds=IDLE)
        assert reader.update([fifo, tmp_path / "missing.jsonl"]) == []
        assert [e["file"] for e in reader.summary().not_collected] == [str(fifo), str(tmp_path / "missing.jsonl")]

    def test_a_drop_in_transcript_is_noted_like_summarise(self, tmp_path):
        path = tmp_path / "s.jsonl"
        path.write_text(json.dumps({"type": "lmer.message", "role": "assistant", "text": "x"}) + "\n")
        reader = IncrementalReader(idle_seconds=IDLE)
        reader.update([path])
        assert reader.summary().not_collected == summarise([path]).not_collected
