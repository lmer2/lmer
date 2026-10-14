"""Projects-mode spool merging (issue #584).

A project's spool has no numeric cap, so the two digest classes a quiet fleet
repeats are merged as they arrive. Pinned from both sides: what merges, and that
questions — a worker's or an operator's — never do.
"""

import pytest

from lmer_platform import assistant, project_context as scope, project_events, projects, runs
from lmer_platform import spool_merge
from tests.test_platform_projects import fleet  # noqa: F401


def check_in(*refs, at="2026-10-13T10:00:00Z", check_ins=None):
    data = {
        "window_seconds": 3600,
        "count": len(refs),
        "runs": [
            {"host": "forge.test", "project": "group/repo", "slug": slug, "label": label,
             "state": "in-progress", "since": at, "age_seconds": 4000}
            for slug, label in refs
        ],
        "workspace_id": "general",
    }
    if check_ins:
        data["check_ins"] = check_ins
    return assistant.PendingNote(at=at, kind="stale_runs", note="stale", data=data)


def question(note="Should I rebase?"):
    return assistant.PendingNote(at="2026-10-13T10:00:00Z", kind="question", note=note)


def test_a_check_in_absorbs_the_earlier_ones_and_keeps_every_run():
    pending = [check_in(("a", "old A")), question(), check_in(("a", "older A"), ("b", "B"))]

    merged = spool_merge.add_check_in(pending, check_in(("a", "new A"), ("c", "C")))

    assert [note.kind for note in merged] == ["question", "stale_runs"]
    latest = merged[-1]
    assert {run["slug"]: run["label"] for run in latest.data["runs"]} == {
        "a": "new A", "b": "B", "c": "C",
    }
    assert latest.data["count"] == 3 and latest.data["check_ins"] == 3
    assert latest.note.startswith("3 runs have gone unchecked")
    assert latest.data["workspace_id"] == "general"


def test_a_first_check_in_is_appended_as_it_is():
    entry = check_in(("a", "A"))

    assert spool_merge.add_check_in([question()], entry) == [question(), entry]


def test_merged_check_ins_keep_counting():
    merged = spool_merge.add_check_in([check_in(("a", "A"), check_ins=4)], check_in(("a", "A")))

    assert merged[-1].data["check_ins"] == 5


def comms(pending, session, *, routine=True, count=2, lines=("[slack] message: hi",)):
    return spool_merge.add_comms(
        pending, at="2026-10-13T10:00:00Z", session=session, lines=list(lines),
        routine=routine, data={"session": session, "count": count},
    )


def test_routine_comms_merge_per_session():
    pending = comms([], "s1", count=2)
    pending = comms(pending, "s2", count=1)
    pending = comms(pending, "s1", count=3, lines=("[slack] message: latest",))

    assert [(note.data["session"], note.data["count"]) for note in pending] == [("s2", 1), ("s1", 5)]
    assert pending[-1].note == "Session s1 (5 records, latest 1 shown): [slack] message: latest"


def test_a_batch_with_a_question_never_merges_either_way():
    pending = comms([], "s1", count=2)
    pending = comms(pending, "s1", routine=False, count=1, lines=("[agent] question: ok?",))
    pending = comms(pending, "s1", count=4)

    asked = [note for note in pending if note.data["routine"] is False]
    assert len(asked) == 1 and asked[0].note == "Session s1: [agent] question: ok?"
    assert [note.data["count"] for note in pending if note.data["routine"]] == [6]


@pytest.mark.parametrize("kinds, expected", [
    (["note", "note"], True),
    (["note", "milestone"], False),
    (["close"], False),
    (["command"], False),
    (["answer"], False),
    (["message"], False),
    ([None], False),
    (["a-kind-added-later"], False),
    ([["note"]], False),
    ([{"kind": "note"}], False),
])
def test_only_note_batches_are_routine(kinds, expected):
    messages = [{"text": "x"} if kind is None else {"kind": kind, "text": "x"} for kind in kinds]

    assert spool_merge.routine(messages) is expected


def test_a_milestone_is_never_absorbed_by_a_later_note():
    pending = comms([], "s1", routine=spool_merge.routine([{"kind": "milestone"}]), count=1,
                    lines=("[agent] milestone: pushed MR, done",))
    pending = comms(pending, "s1", routine=spool_merge.routine([{"kind": "note"}]), count=1,
                    lines=("[agent] note: idle",))

    assert [note.note for note in pending] == [
        "Session s1: [agent] milestone: pushed MR, done", "Session s1: [agent] note: idle",
    ]


def test_an_untagged_note_from_before_the_upgrade_is_left_alone():
    legacy = assistant.PendingNote(at="2026-10-13T09:00:00Z", kind="session_comms",
                                   note="Session s1: old", data={"session": "s1", "count": 9})

    pending = comms([legacy], "s1", count=1)

    assert pending[0] == legacy and pending[-1].data["count"] == 1


@pytest.fixture
def project_spool(fleet):  # noqa: F811
    projects.migrate()
    with scope.bound("general"):
        runs.track("forge.test", "group/repo", "run-1")
        yield


def test_repeated_check_ins_leave_one_note_beside_untouched_questions(project_spool):
    assistant.notify("Should I rebase?", kind="question", data={"workspace_id": "general"})
    for label in ("first", "second", "third"):
        assistant.notify("stale", kind="stale_runs", data=check_in(("run-1", label)).data)

    kinds = [note.kind for note in assistant.read_state().pending]

    assert kinds == ["question", "stale_runs"]
    assert assistant.read_state().pending[-1].data["check_ins"] == 3


def test_routine_comms_merge_in_the_project_spool_but_others_do_not(project_spool):
    run = {"host": "forge.test", "project": "group/repo", "slug": "run-1"}

    def deliver(identity, kind, text):
        project_events.enqueue("worker-1", {"id": identity, "kind": kind, "text": text,
                                            "source": {"channel": "agent"}}, run=run)
        project_events.flush()

    deliver("n1", "note", "working")
    deliver("n2", "note", "still working")
    deliver("q1", "question", "May I force-push?")
    deliver("m1", "milestone", "pushed MR, done")
    deliver("n3", "note", "idle")

    notes = [(note.data["routine"], note.data["count"]) for note in assistant.read_state().pending]
    assert notes == [(False, 1), (False, 1), (True, 3)]


def test_a_question_arriving_after_a_check_in_is_never_absorbed():
    entry = question("Worker asks: may I force-push main?")

    merged = spool_merge.add_check_in([check_in(("a", "A"))], entry)

    assert [(note.kind, note.note) for note in merged] == [
        ("stale_runs", "stale"), ("question", "Worker asks: may I force-push main?"),
    ]


def test_a_question_after_a_check_in_survives_in_the_project_spool(project_spool):
    assistant.notify("stale", kind="stale_runs", data=check_in(("run-1", "first")).data)
    assistant.notify("May I force-push main?", kind="question", data={"workspace_id": "general"})
    assistant.notify("stale", kind="stale_runs", data=check_in(("run-1", "second")).data)

    notes = [(note.kind, note.note) for note in assistant.read_state().pending]

    assert notes[0] == ("question", "May I force-push main?")
    assert [kind for kind, _ in notes] == ["question", "stale_runs"]


def test_a_move_that_empties_the_spool_ends_its_episode(project_spool):
    other = projects.create("Other")["id"]
    assistant.notify("run-1 crashed", kind="crashed",
                     data={"host": "forge.test", "project": "group/repo", "slug": "run-1"})
    assistant.mark_nudged("assistant-1")
    assistant.mark_stalled("assistant-1")
    before = assistant.read_state().episode

    projects.move("forge.test/group/repo/run-1", other, expected_project="general")

    after = assistant.read_state()
    assert after.pending == ()
    assert (after.nudges, after.stalled_at, after.episode) == (0, None, before + 1)


def test_a_projects_mode_take_ends_the_episode(project_spool):
    assistant.notify("run-1 crashed", kind="crashed",
                     data={"host": "forge.test", "project": "group/repo", "slug": "run-1"})
    assistant.mark_nudged("assistant-1")
    assistant.mark_stalled("assistant-1")
    before = assistant.read_state().episode

    taken = assistant.take_pending()

    after = assistant.read_state()
    assert [note["kind"] for note in taken] == ["crashed"] and after.pending == ()
    assert (after.nudges, after.stalled_at, after.episode) == (0, None, before + 1)


def test_a_move_that_keeps_notes_keeps_its_episode(project_spool):
    other = projects.create("Other")["id"]
    runs.track("forge.test", "group/repo", "run-2")
    for slug in ("run-1", "run-2"):
        assistant.notify(f"{slug} crashed", kind="crashed",
                         data={"host": "forge.test", "project": "group/repo", "slug": slug})
    assistant.mark_nudged("assistant-1")
    assistant.mark_stalled("assistant-1")
    before = assistant.read_state()
    assert before.nudges and before.stalled_at

    projects.move("forge.test/group/repo/run-1", other, expected_project="general")

    after = assistant.read_state()
    assert [note.note for note in after.pending] == ["run-2 crashed"]
    assert (after.nudges, after.stalled_at, after.episode) == (
        before.nudges, before.stalled_at, before.episode)
