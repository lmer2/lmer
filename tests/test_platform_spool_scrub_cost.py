"""What one assistant spool load costs, and that the savings mask nothing (issue #583).

A spool of hundreds of notes used to resolve every by-value credential for every
string: one ``projects.sqlite3`` open and a glob of every project's credential
file per string, on the thread the web server shares. These tests count those
reads directly, then pin that the memo standing in for repeated scrubs never
serves text a scrub would have masked.
"""

import pathlib
import sqlite3

import pytest

from lmer_platform import assistant, config, project_context, projects, store, transcripts
from tests.conftest import strip_lmer_env
from tests.test_platform_transcripts import plant_session

STRINGS_PER_NOTE = 10


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """A migrated host whose general assistant and two projects hold credentials.

    Two project credential files, because one is what the per-file memo in
    :func:`lmer_platform.config.active_assistant_credential` hides behind.
    """
    strip_lmer_env(monkeypatch)
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
    monkeypatch.setattr(assistant, "_SCRUBBED_NOTES", {})
    settings = config.load({"lmer_bin": str(tmp_path / "lmer")})
    config.ensure_secret(settings)
    projects.migrate()
    credentials = {}
    for workspace in (None, "alpha", "beta"):
        with project_context.bound(workspace):
            credentials[workspace] = config.mint_assistant_credential()
    return credentials


def _note(index, text="routine digest"):
    return {
        "at": f"2026-10-13T00:00:{index % 60:02d}Z",
        "kind": "stale_runs",
        "note": f"{text} {index}",
        "data": {f"field{n}": f"value {index}.{n}" for n in range(STRINGS_PER_NOTE)},
    }


def _save_spool(notes):
    projects.save_assistant_state({"pending": notes})


class _Reads:
    """Counts sqlite opens and file reads while it is installed."""

    def __init__(self, monkeypatch):
        self.connects = 0
        self.files = 0
        connect, read_text = sqlite3.connect, pathlib.Path.read_text

        def counting_connect(*args, **kwargs):
            self.connects += 1
            return connect(*args, **kwargs)

        def counting_read_text(path, *args, **kwargs):
            self.files += 1
            return read_text(path, *args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", counting_connect)
        monkeypatch.setattr(pathlib.Path, "read_text", counting_read_text)


def _count_scrubs(monkeypatch):
    """Record every string the spool's scrub sees, still scrubbing it."""
    calls = []
    scrub = transcripts._scrub

    def counting(text):
        calls.append(text)
        return scrub(text)

    monkeypatch.setattr(assistant, "_scrub", counting)
    monkeypatch.setattr(transcripts, "_scrub", counting)
    return calls


def _cold_load_cost(monkeypatch, count):
    _save_spool([_note(index) for index in range(count)])
    monkeypatch.setattr(assistant, "_SCRUBBED_NOTES", {})
    # Distinct content per load, so the stat-keyed file memos in config miss
    # exactly as they do after a rotation, and the count reflects real reads.
    for workspace in (None, "alpha", "beta"):
        with project_context.bound(workspace):
            config.mint_assistant_credential()
    with monkeypatch.context() as patch:
        reads = _Reads(patch)
        state = assistant.read_state()
    assert len(state.pending) == count
    return reads.connects, reads.files


def test_a_large_spool_costs_the_same_reads_as_a_small_one(fleet, monkeypatch):
    # The first load fills one-time memos (the secret's resolved path) that
    # would otherwise count against whichever size went first.
    _cold_load_cost(monkeypatch, 1)
    small = _cold_load_cost(monkeypatch, 3)
    large = _cold_load_cost(monkeypatch, 300)
    assert large == small
    connects, files = large
    assert connects < 10 and files < 10


def test_a_warm_spool_load_scrubs_nothing(fleet, monkeypatch):
    _save_spool([_note(index) for index in range(50)])
    first = assistant.read_state()
    calls = _count_scrubs(monkeypatch)
    second = assistant.read_state()
    assert calls == []
    assert second.pending == first.pending


def test_a_hand_edited_note_is_scrubbed_with_a_warm_memo(fleet):
    notes = [_note(index) for index in range(5)]
    _save_spool(notes)
    assistant.read_state()
    secret = config.active_secret()
    notes[2] = dict(notes[2], note=f"use {secret} to reach the forge",
                    data={"label": f"token for alpha {fleet['alpha']}"})
    _save_spool(notes)
    edited = assistant.read_state().pending[2]
    assert secret not in edited.note
    assert fleet["alpha"] not in edited.data["label"]
    assert transcripts._REDACTED in edited.note


def test_a_credential_minted_after_a_load_is_masked_on_the_next(fleet):
    value = "later-minted-credential-value-0123456789abcdef"
    _save_spool([_note(0, text=f"quoted {value}")])
    assert value in assistant.read_state().pending[0].note
    with project_context.bound("alpha"):
        path = config.assistant_credential_path()
    path.write_text(value + "\n", encoding="utf-8")
    assert value not in assistant.read_state().pending[0].note


def test_a_note_handed_out_cannot_change_a_later_load(fleet):
    _save_spool([_note(0)])
    assistant.read_state().pending[0].data["field0"] = "changed by a caller"
    assert assistant.read_state().pending[0].data["field0"] == "value 0.0"


def test_taken_notes_leave_the_memo(fleet):
    _save_spool([_note(index) for index in range(5)])
    assistant.read_state()
    _save_spool([_note(4)])
    assistant.read_state()
    _known, entries = assistant._SCRUBBED_NOTES[projects.GENERAL]
    assert len(entries) == 1


def test_each_project_keeps_its_own_memo(fleet):
    _save_spool([_note(index) for index in range(3)])
    assistant.read_state()
    assistant.AssistantState.from_dict({"pending": [_note(9)]}, memo_spool="alpha")
    _known, general = assistant._SCRUBBED_NOTES[projects.GENERAL]
    assert len(general) == 3


def test_a_transcript_page_resolves_credentials_once(fleet, monkeypatch, tmp_path):
    monkeypatch.setattr(transcripts, "TRANSCRIPT_ROOT", str(tmp_path / "harness-transcripts"))
    plant_session("spoolcost01")
    resolved = []
    known = transcripts._known_credentials

    def counting():
        resolved.append(1)
        return known()

    monkeypatch.setattr(transcripts, "_known_credentials", counting)
    page = transcripts.read_messages("spoolcost01")
    assert page.total >= 5
    assert len(resolved) == 1
    resolved.clear()
    transcripts.last_turn("spoolcost01")
    assert len(resolved) == 1


def test_a_credential_containing_another_is_masked_whole(fleet):
    shorter, longer = "abcdefgh12", "abcdefgh12XYZsecretTAIL"
    for workspace, value in (("alpha", shorter), ("beta", longer)):
        with project_context.bound(workspace):
            config.assistant_credential_path().write_text(value + "\n", encoding="utf-8")
    _save_spool([_note(0, text=f"here: {longer} end")])
    note = assistant.read_state().pending[0].note
    assert "XYZsecretTAIL" not in note
    assert note.startswith(f"here: {transcripts._REDACTED} end")


def test_the_memo_holds_across_project_contexts(fleet, monkeypatch):
    _save_spool([_note(index) for index in range(5)])
    raw = projects.assistant_state()
    with project_context.bound("alpha"):
        assistant.AssistantState.from_dict(raw, memo_spool=projects.GENERAL)
    calls = _count_scrubs(monkeypatch)
    assistant.AssistantState.from_dict(raw, memo_spool=projects.GENERAL)
    assert calls == []


def test_a_revoked_credential_empties_the_memo(fleet, monkeypatch):
    _save_spool([_note(index) for index in range(5)])
    assistant.read_state()
    with project_context.bound("alpha"):
        assert config.revoke_assistant_credential()
    calls = _count_scrubs(monkeypatch)
    assistant.read_state()
    assert calls
    known, _entries = assistant._SCRUBBED_NOTES[projects.GENERAL]
    assert fleet["alpha"] not in known


def test_switching_projects_off_empties_the_memo(fleet, monkeypatch):
    _save_spool([_note(index) for index in range(5)])
    raw = projects.assistant_state()
    assistant.AssistantState.from_dict(raw, memo_spool=projects.GENERAL)
    with sqlite3.connect(projects.path()) as db:
        db.execute("DELETE FROM metadata WHERE key='migrated'")
    assert not projects.enabled()
    calls = _count_scrubs(monkeypatch)
    assistant.AssistantState.from_dict(raw, memo_spool=projects.GENERAL)
    assert calls
    known, _entries = assistant._SCRUBBED_NOTES[projects.GENERAL]
    assert fleet["beta"] not in known
