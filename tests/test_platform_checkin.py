"""Check-in tracking: the platform notices nobody has looked (issue #244).

The mechanism that answers silence. Everything else reaching the assistant's spool
is an *event*, so a run that simply stops moving produced nothing at all — which is
how a review sat four hours with its fix already pushed.

What is pinned, in the order it matters:

- **The window is a boundary, not a mood** — both directions, because a check that
  only fires late is as useless as one that fires always.
- **A check restarts the clock**, and so does the digest, but the two are recorded
  apart so "when did anybody actually look" survives being announced.
- **One digest names every stale run**, inside the spool's bound, with the
  structured half keeping the whole list.
- **A run announced this window is not announced again next tick.**
- **Newly seen, finished and orchestrator rows never nag** — the first so a daemon
  restart cannot flood a bounded spool, the last because an assistant reminded to
  check in on itself is a loop.

Time is injected where it has to be and otherwise moved by writing stamps: this
suite runs in a one-CPU container where a sleep-based assertion is a flake.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from lmer_platform import checkin, store

WINDOW = 3600


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


def iso(**delta):
    """An ISO-Z stamp *delta* in the past (``minutes=…``, ``hours=…``)."""
    when = datetime.now(timezone.utc) - timedelta(**delta)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def row(slug="develop-issue-1", **overrides):
    payload = {
        "host": "git.example.com",
        "project": "acme/widgets",
        "slug": slug,
        "label": slug,
        "state": "dormant",
        "orchestrator": False,
    }
    payload.update(overrides)
    return payload


def fleet(*rows):
    return {"runs": list(rows)}


def stamp(slug, field, when, host="git.example.com", project="acme/widgets"):
    """Write one mark directly, which is how this suite moves time."""
    path = checkin.marks_path()
    marks = checkin.read_marks()
    record = dict(marks.get(f"{host}/{project}/{slug}") or {})
    record[field] = when
    marks[f"{host}/{project}/{slug}"] = record
    store.write_json(path, {"runs": marks})


# --- the boundary ------------------------------------------------------------

def test_a_run_inside_the_window_is_not_stale(platform_root):
    checkin.observe(fleet(row()))
    stamp("develop-issue-1", "checked_at", iso(minutes=59))
    assert checkin.stale_runs(fleet(row()), window=WINDOW) == []


def test_a_run_past_the_window_is_stale(platform_root):
    checkin.observe(fleet(row()))
    stamp("develop-issue-1", "first_seen", iso(hours=5))
    stamp("develop-issue-1", "checked_at", iso(hours=4, minutes=20))
    stale = checkin.stale_runs(fleet(row()), window=WINDOW)
    assert [run.slug for run in stale] == ["develop-issue-1"]
    assert 15000 < stale[0].age_seconds < 16000


def test_a_check_restarts_the_clock(platform_root):
    checkin.observe(fleet(row()))
    stamp("develop-issue-1", "first_seen", iso(hours=4))
    assert checkin.stale_runs(fleet(row()), window=WINDOW), "precondition"

    assert checkin.record_check("git.example.com", "acme/widgets", "develop-issue-1")
    assert checkin.stale_runs(fleet(row()), window=WINDOW) == []


def test_a_window_of_zero_disables_the_whole_thing(platform_root):
    checkin.observe(fleet(row()))
    stamp("develop-issue-1", "first_seen", iso(hours=48))
    assert checkin.stale_runs(fleet(row()), window=0) == []


# --- what can go stale -------------------------------------------------------

def test_a_newly_seen_run_is_not_stale(platform_root):
    """A daemon restart must not turn a long history into a digest per run."""
    payload = fleet(row(), row("review-mr-9"))
    checkin.observe(payload)
    assert checkin.stale_runs(payload, window=WINDOW) == []


def test_a_run_the_platform_has_never_seen_is_not_stale(platform_root):
    """Not even without an observe() first: no stamp is not 'infinitely old'."""
    assert checkin.stale_runs(fleet(row()), window=WINDOW) == []


def test_a_complete_run_never_nags(platform_root):
    payload = fleet(row(state="complete"))
    checkin.observe(payload)
    stamp("develop-issue-1", "first_seen", iso(hours=48))
    assert checkin.stale_runs(payload, window=WINDOW) == []


def test_the_orchestrators_own_row_never_nags(platform_root):
    payload = fleet(row(slug="chat-fleet", orchestrator=True, state="running"))
    checkin.observe(payload)
    stamp("chat-fleet", "first_seen", iso(hours=48))
    assert checkin.stale_runs(payload, window=WINDOW) == []


def test_a_dormant_run_can_go_stale(platform_root):
    """The motivating case: the session ended and the turn was never taken."""
    payload = fleet(row(slug="review-mr-202", state="dormant"))
    checkin.observe(payload)
    stamp("review-mr-202", "first_seen", iso(hours=4, minutes=20))
    assert [run.slug for run in checkin.stale_runs(payload, window=WINDOW)] == [
        "review-mr-202"
    ]


def test_an_unparseable_stamp_does_not_produce_an_endless_reminder(platform_root):
    payload = fleet(row())
    checkin.observe(payload)
    stamp("develop-issue-1", "checked_at", "yesterday afternoon")
    assert checkin.stale_runs(payload, window=WINDOW) == []


# --- the digest --------------------------------------------------------------

def test_one_digest_names_every_stale_run(platform_root):
    payload = fleet(
        row("review-mr-202", state="dormant"),
        row("develop-issue-236", state="running"),
        row("review-mr-199", state="running"),
    )
    checkin.observe(payload)
    stamp("review-mr-202", "first_seen", iso(hours=4, minutes=20))
    stamp("develop-issue-236", "first_seen", iso(hours=10))
    stamp("review-mr-199", "first_seen", iso(hours=2, minutes=35))

    stale = checkin.stale_runs(payload, window=WINDOW)
    note, data = checkin.digest(stale, window=WINDOW)

    assert [run.slug for run in stale] == [
        "develop-issue-236", "review-mr-202", "review-mr-199",
    ], "oldest first — the one that has waited longest is read first"
    assert "3 runs have gone unchecked for over 1h" in note
    assert "develop-issue-236 (10h)" in note
    assert "review-mr-202 (4h20m)" in note
    assert data["count"] == 3
    assert data["window_seconds"] == WINDOW
    assert [entry["slug"] for entry in data["runs"]] == [
        "develop-issue-236", "review-mr-202", "review-mr-199",
    ]


def test_the_note_stays_inside_the_spools_bound(platform_root):
    """Twenty stale runs is one note a context window can hold, not twenty."""
    from lmer_platform import assistant

    rows = [row(f"run-{index:02d}") for index in range(20)]
    payload = fleet(*rows)
    checkin.observe(payload)
    for entry in rows:
        stamp(entry["slug"], "first_seen", iso(hours=3))

    stale = checkin.stale_runs(payload, window=WINDOW)
    note, data = checkin.digest(stale, window=WINDOW)

    assert len(stale) == 20
    assert len(note) <= assistant.MAX_NOTE_CHARS
    assert f"(+{20 - checkin.MAX_NAMED} more)" in note
    assert len(data["runs"]) == 20, "the data keeps what the prose summarised"


def test_one_stale_run_reads_as_one(platform_root):
    payload = fleet(row())
    checkin.observe(payload)
    stamp("develop-issue-1", "first_seen", iso(hours=2))
    note, _ = checkin.digest(checkin.stale_runs(payload, window=WINDOW), window=WINDOW)
    assert note.startswith("1 run has gone unchecked")


# --- announcing is not checking ---------------------------------------------

def test_an_announced_run_is_not_announced_again_inside_the_window(platform_root):
    payload = fleet(row())
    checkin.observe(payload)
    stamp("develop-issue-1", "first_seen", iso(hours=4))

    first = checkin.stale_runs(payload, window=WINDOW)
    assert first
    checkin.record_announced(first)

    assert checkin.stale_runs(payload, window=WINDOW) == [], (
        "a stale run costs one digest per window, not one per tick"
    )


def test_a_run_announced_a_window_ago_is_announced_again(platform_root):
    payload = fleet(row())
    checkin.observe(payload)
    stamp("develop-issue-1", "first_seen", iso(hours=8))
    stamp("develop-issue-1", "announced_at", iso(hours=2))
    assert [run.slug for run in checkin.stale_runs(payload, window=WINDOW)] == [
        "develop-issue-1"
    ]


def test_an_announcement_is_not_recorded_as_a_check(platform_root):
    """'Nobody has looked' has to survive the platform saying so."""
    payload = fleet(row())
    checkin.observe(payload)
    stamp("develop-issue-1", "first_seen", iso(hours=4))
    checkin.record_announced(checkin.stale_runs(payload, window=WINDOW))

    marks = checkin.read_marks()["git.example.com/acme/widgets/develop-issue-1"]
    assert marks.get("announced_at")
    assert marks.get("checked_at") is None
    facts = checkin.checkin_of(marks, window=WINDOW)
    assert facts["checked_at"] is None, "the digest must not look like a check"
    assert facts["since"] == marks["announced_at"]


# --- bookkeeping -------------------------------------------------------------

def test_marks_for_runs_that_left_the_fleet_are_dropped(platform_root):
    both = fleet(row("kept"), row("forgotten"))
    checkin.observe(both)
    assert len(checkin.read_marks()) == 2

    checkin.observe(fleet(row("kept")))
    assert list(checkin.read_marks()) == ["git.example.com/acme/widgets/kept"]


def test_a_corrupt_marks_file_reads_as_a_fresh_baseline(platform_root):
    platform_root.mkdir(parents=True, exist_ok=True)
    checkin.marks_path().write_text("{not json", encoding="utf-8")
    assert checkin.read_marks() == {}


def test_a_marks_file_from_another_shape_is_ignored_not_trusted(platform_root):
    platform_root.mkdir(parents=True, exist_ok=True)
    checkin.marks_path().write_text(
        json.dumps({"schema": 1, "runs": {"a/b/c": "not a record"}}),
        encoding="utf-8",
    )
    assert checkin.read_marks() == {}


def test_a_check_on_an_unknown_run_is_a_no_op_not_a_crash(platform_root):
    assert checkin.record_check("", "acme/widgets", "slug") is False
    assert checkin.read_marks() == {}


def test_checkin_of_reports_the_facts_a_fleet_row_needs(platform_root):
    """And the clock runs from the *latest* stamp, not the first sight of the run."""
    checkin.observe(fleet(row()))
    checked = iso(minutes=90)
    stamp("develop-issue-1", "first_seen", iso(hours=3))
    stamp("develop-issue-1", "checked_at", checked)
    marks = checkin.read_marks()
    facts = checkin.checkin_of(
        marks["git.example.com/acme/widgets/develop-issue-1"], window=WINDOW
    )
    assert facts["checked_at"] == checked
    assert facts["stale"] is True
    assert 5300 < facts["age_seconds"] < 5500


# --- the note's bound is enforced, not assumed --------------------------------
#
# assistant.notify REFUSES a note over MAX_NOTE_CHARS rather than truncating it,
# and a refused digest is retried on the next tick — so a note that grew past the
# bound would not be a long message, it would be the end of check-ins on that
# host, silently, for as long as the run that caused it existed. A run's label is
# agent-authored (`work name`, else the slug) and nothing upstream bounds it.

def test_the_note_bound_is_the_seams_own_number():
    """Not a copy of it. A second constant equal to 2000 is a second constant to
    forget to change; the module that *refuses* the note is the one that owns the
    limit, and this pins that nothing re-declared it."""
    from lmer_platform import assistant

    assert checkin.MAX_NOTE_CHARS is assistant.MAX_NOTE_CHARS


def test_a_pathological_label_cannot_kill_the_digest(platform_root):
    from lmer_platform import assistant

    rows = [row("x" * 4000, label="L" * 4000) for _ in range(1)]
    rows += [row(f"run-{index}", label="M" * 500) for index in range(12)]
    payload = fleet(*rows)
    checkin.observe(payload)
    for entry in rows:
        stamp(entry["slug"], "first_seen", iso(hours=5))

    stale = checkin.stale_runs(payload, window=WINDOW)
    note, data = checkin.digest(stale, window=WINDOW)

    assert len(stale) == 13
    assert len(note) <= assistant.MAX_NOTE_CHARS, (
        "notify() would refuse this note, and a refused digest is retried "
        "forever — check-ins would be silently over on this host"
    )
    assert "…" in note, "a truncated label has to say it was truncated"
    assert len(data["runs"]) == 13, "the data names every stale run"
    assert max(len(entry["label"]) for entry in data["runs"]) == (
        checkin.MAX_DATA_LABEL_CHARS
    ), (
        "the structured half is generous, not unbounded: one digest carries N "
        "runs and up to MAX_PENDING of them are parsed and re-scrubbed on every "
        "read of assistant.json"
    )


# --- one bad stamp must not silence a run forever (review iteration 1) --------
#
# `_latest_stamp` used to take the lexical max over the raw strings and validate
# afterwards, so an unparseable or future-dated value won the comparison
# permanently: the run read as "age unknown, therefore not stale" and no later
# real check could clear it, because the bad value kept winning. Both shapes are
# reachable without hand-editing — the daemon writes these from its own wall
# clock, so a host that boots skewed and is corrected by NTP has poisoned every
# stamp it wrote in between.

def test_a_current_check_beats_an_unparseable_stamp(platform_root):
    payload = fleet(row())
    checkin.observe(payload)
    stamp("develop-issue-1", "first_seen", iso(hours=9))
    stamp("develop-issue-1", "announced_at", "yesterday afternoon")
    stamp("develop-issue-1", "checked_at", iso(minutes=5))

    assert checkin.stale_runs(payload, window=WINDOW) == [], (
        "a correct, current checked_at sat beside the junk and lost to it"
    )


def test_a_current_check_beats_a_future_dated_stamp(platform_root):
    payload = fleet(row())
    checkin.observe(payload)
    stamp("develop-issue-1", "first_seen", iso(hours=9))
    stamp("develop-issue-1", "announced_at", "2036-08-03T23:32:59Z")
    stamp("develop-issue-1", "checked_at", iso(hours=4))

    stale = checkin.stale_runs(payload, window=WINDOW)
    assert [run.slug for run in stale] == ["develop-issue-1"], (
        "a stamp ten years in the future hid this run until 2036"
    )


def test_a_run_whose_every_stamp_is_unusable_recovers_on_the_next_check(
    platform_root
):
    payload = fleet(row())
    checkin.observe(payload)
    stamp("develop-issue-1", "first_seen", "not a timestamp")
    stamp("develop-issue-1", "announced_at", "2036-01-01T00:00:00Z")
    assert checkin.stale_runs(payload, window=WINDOW) == [], "nothing usable to age"

    checkin.record_check("git.example.com", "acme/widgets", "develop-issue-1")
    facts = checkin.checkin_of(
        checkin.read_marks()["git.example.com/acme/widgets/develop-issue-1"],
        window=WINDOW,
    )
    assert facts["checked_at"] == facts["since"], (
        "the real check has to win the moment it is written"
    )


def test_a_stamp_a_few_seconds_ahead_is_still_believed(platform_root):
    """Second-level jitter between a write and a read is not clock skew."""
    ahead = (datetime.now(timezone.utc) + timedelta(seconds=5)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    facts = checkin.checkin_of({"checked_at": ahead}, window=WINDOW)
    assert facts["since"] == ahead
