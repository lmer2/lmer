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


def test_a_digest_keeps_its_run_and_the_turn_carries_it(tmp_path, monkeypatch):
    from lmer_platform import transcripts

    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path))
    data = {"host": "gitlab.example.com", "project": "group/app", "slug": "run-1",
            "label": "Fix the widget", "kind": "live_question", "state": None}
    assistant_conversation.record("gitlab.example.com/group/app/run-1 needs you — live_question: ok?",
                                  kind="live_question", data=data, session_id="s-1")
    assistant_conversation.record("no run here", kind="complete", data={"slug": "only"}, session_id="s-1")
    assistant_conversation.record("public reply", kind="reply", thread="thread", data=data, session_id="s-1")
    events = assistant_conversation.events()
    assert events[0].message["meta"]["run"] == {
        "host": "gitlab.example.com", "project": "group/app", "slug": "run-1", "label": "Fix the widget",
    }
    assert "run" not in events[1].message["meta"]
    assert "run" not in events[2].message["meta"]

    turns, _copies = transcripts._comms_turns(events, "s-1")
    first, second, third = (turn.to_dict() for turn in turns)
    assert first["role"] == transcripts.PLATFORM_ROLE
    assert first["digest"] == {"kind": "live_question", "run": events[0].message["meta"]["run"]}
    assert second["digest"] == {"kind": "complete", "run": None}
    assert "digest" not in third


def test_a_digest_run_is_bounded_and_needs_its_whole_key():
    long = "x" * 500
    run = assistant_conversation.digest_run({"host": "h", "project": "p", "slug": "s", "label": long})
    assert len(run["label"]) == assistant_conversation.MAX_RUN_FIELD_CHARS
    assert assistant_conversation.digest_run({"host": "h", "project": "p"}) is None
    nested = "/".join(["subgroup"] * 25) + "/app"
    assert len(nested) > assistant_conversation.MAX_RUN_FIELD_CHARS
    assert assistant_conversation.digest_run(
        {"host": "gitlab.example.com", "project": nested, "slug": "s", "label": "x"}
    ) is None, "a shortened project path would link a run that does not exist"
    limit = "p" * assistant_conversation.MAX_RUN_FIELD_CHARS
    assert assistant_conversation.digest_run(
        {"host": "h", "project": limit, "slug": "s"}
    ) == {"host": "h", "project": limit, "slug": "s"}
    assert assistant_conversation.digest_run({"host": "h", "project": 3, "slug": "s"}) is None
    assert assistant_conversation.digest_run("not a mapping") is None
