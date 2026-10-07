"""Retained conversation history is loaded once per active platform path."""
import json

from lmer_platform import assistant_conversation, comms, store


def test_consecutive_reads_and_appends_do_not_reload_history(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path))
    path = comms.comms_dir() / "assistant-conversation.jsonl"
    path.parent.mkdir(parents=True)
    for cursor, generation in [(1, path.with_suffix('.jsonl.1')), (2, path)]:
        event = comms.FleetEvent(cursor, "assistant", {"id": str(cursor), "text": str(cursor)})
        generation.write_text(json.dumps(event.to_dict()) + "\n")
    loads = []
    original = comms.Journal._load

    def load(journal):
        loads.append(journal.path)
        original(journal)

    monkeypatch.setattr(comms.Journal, "_load", load)
    assert [e.message['text'] for e in assistant_conversation.events()] == ['1', '2']
    assistant_conversation.record('new reply', kind='reply', thread='thread')
    assistant_conversation.record('needed digest', kind='complete')
    for _ in range(2):
        assert [e.cursor for e in assistant_conversation.events()] == [1, 2, 3, 4]
    assert loads == [path.resolve()]


def test_resolved_paths_reuse_the_cache_and_other_paths_stay_isolated(tmp_path, monkeypatch):
    first = tmp_path / 'first'
    second = tmp_path / 'second'
    alias = tmp_path / 'alias'
    first.mkdir()
    alias.symlink_to(first, target_is_directory=True)
    monkeypatch.setattr(store, 'PLATFORM_DIR', str(first))
    assistant_conversation.record('first reply', kind='reply', thread='thread')
    original = assistant_conversation._journal()
    monkeypatch.setattr(store, 'PLATFORM_DIR', str(alias))
    assert assistant_conversation._journal() is original
    monkeypatch.setattr(store, 'PLATFORM_DIR', str(second))
    assert assistant_conversation.events() == []
    assistant_conversation.record('second reply', kind='reply', thread='thread')
    assert [e.message['text'] for e in assistant_conversation.events()] == ['second reply']
    monkeypatch.setattr(store, 'PLATFORM_DIR', str(first))
    assert [e.message['text'] for e in assistant_conversation.events()] == ['first reply']
