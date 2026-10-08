"""The project-index kernel: pure renderers, lock-free readers, README writers.

`work_repo.project_index` turns a project's run records into one Markdown
block and splices it between the index markers of `<project>/README.md`.
Everything a row says is a function of its own record — no clock, no counts,
no mtime, no environment — so two regenerations differ only where records
differ. These tests pin that contract: the state vocabulary with and without
`now`, labels and their escaping, environment-independent redaction, the
parsed-UTC ordering that keeps an existing row in place when a sibling
lands, the fast state reader's parity with a full YAML parse, the splice's
byte preservation outside the markers, the hand-text marker screen, the
tracked-or-staged filter over a real git index, and the size and timing
budgets a shared repo's deepest chains must stay inside.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from tests import timing_budgets as timing
from tests.conftest import strip_lmer_env
from tests.schema2_fixtures import (
    DEFAULT_PROJECT,
    RecordSpec,
    Schema2Tree,
    build_schema2_plexus,
    chain,
    git,
    make_env,
    publication2,
    stamp,
    uid_for,
)
from work_repo import project_index as pi
from work_repo import run_state
from work_repo import session_record
from work_repo.project_index import (
    END_MARKER,
    FORMAT,
    START_MARKER,
    IndexSkipped,
    MalformedMarkers,
    RecordSummary,
)

REPO = Path(__file__).resolve().parents[1]
KERNEL_SOURCE = REPO / "src" / "work_repo" / "project_index.py"
UTC = timezone.utc

#: Day 14 at 10:00 is exactly fourteen days after a record updated day 0 at 10:00.
UPDATED = "2026-09-01T10:00:00Z"
AT_WINDOW = datetime(2026, 9, 15, 10, 0, 0, tzinfo=UTC)

#: The spec's worked example (Index format), synthetic: a continuation and its
#: source in a local-path `chat-.` group, both labelled by their first log line,
#: and a schema-1 record labelled by its name rather than its frozen directory suffix.
WORKED_EXAMPLE_RECORDS = (
    RecordSpec(
        uid="3c1e9a07b5d24f6e8a90c4b2d7e15f38", slug="chat-.",
        started_at="2026-10-02T00:54:38.470654+00:00", updated="2026-10-02T00:54:46Z",
        log_message="Continuation writes its own fresh execution record",
        source_uid="9b4d2f60e1a84c75b3f2a6d90e8c7b14",
    ),
    RecordSpec(
        uid="9b4d2f60e1a84c75b3f2a6d90e8c7b14", slug="chat-.",
        started_at="2026-10-02T00:53:47.609710+00:00", updated="2026-10-02T00:53:57Z",
        log_message="Schema 2 live verification: new execution writes its own record",
    ),
    RecordSpec(
        uid="20260815-093000-1-10001", slug="chat-.", schema=1, dir_suffix="first-draft",
        started_at="2026-08-15T09:30:00Z", updated="2026-09-09T05:40:00Z",
        status="complete", stop_reason="complete", name="release-notes-review",
        phase="seed-project",
    ),
)

WORKED_EXAMPLE_ROWS = (
    "| 2026-10-02 00:54 | _(unnamed)_ [Continuation writes its own fresh execution record]"
    "(_runs/chat-./3c1e9a07b5d24f6e8a90c4b2d7e15f38/) · continues [9b4d2f60]"
    "(_runs/chat-./9b4d2f60e1a84c75b3f2a6d90e8c7b14/) | ended, not closed | 2026-10-02 00:54 "
    "| `chat-.` · `3c1e9a07` |",
    "| 2026-10-02 00:53 | _(unnamed)_ [Schema 2 live verification: new execution writes its "
    "own record](_runs/chat-./9b4d2f60e1a84c75b3f2a6d90e8c7b14/) | ended, not closed | "
    "2026-10-02 00:53 | `chat-.` · `9b4d2f60` |",
    "| 2026-08-15 09:30 | [release-notes-review]"
    "(_runs/chat-./20260815-093000-1-10001--first-draft/) | closed · phase seed-project | "
    "2026-09-09 05:40 | `chat-.` · `20260815-093000-1-10001` |",
)

#: A live-shaped manifest, synthetic: four registered projects under three
#: namespaces, in an order that is not sorted (one name a prefix of the name before it).
EXAMPLE_MANIFEST = {
    "schema": 2,
    "name": "example-work",
    "projects": {
        "gadgets-api": {"repo": "git.example.test/acme/gadgets-api"},
        "gadgets": {"repo": "git.example.test/alice/gadgets"},
        "work-notes": {"repo": "git.example.test/alice/work-notes"},
        "ballots": {"repo": "git.example.test/example-org/ballots"},
    },
}

GLPAT = "glpat-" + "A1b2C3d4E5f6G7h8I9j0K1l2"

#: Every scalar shape `write_state`'s emitter (`yaml.safe_dump(state, sort_keys=False)`)
#: can put inside a nested context block, keyed by what it exercises.
EMITTER_SHAPES = {
    "plain scalar folded at width 80": ("review the widgets " * 8).strip(),
    "single-quoted multi-line scalar": ("first line: has a colon\nsecond [bracket] line {brace} "
                                        "and it's quoted \"twice\"\n\nthird after a blank line"),
    "double-quoted scalar, escaped and folded": "caf\u00e9 \u2014 na\u00efve r\u00e9sum\u00e9 # not a comment " * 5,
    "indicators and both quote kinds inside quotes": "[WIP] fix #12: don't break \"quoted\" {flow}",
}


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def tree(tmp_path) -> Schema2Tree:
    """An empty registered project, ready for records."""
    return build_schema2_plexus(tmp_path / "plexus")


def _summary(**overrides) -> RecordSummary:
    base = dict(
        dir_rel="_runs/chat/" + uid_for(1), dir_name=uid_for(1), uid=uid_for(1), readable=True,
        slug="chat", name="named-run", goal_line=None, first_log=None, status="in-progress",
        stop_reason=None, phase=None, started_at="2026-09-20T10:00:00Z",
        updated="2026-09-20T10:05:00Z", signed_out=True, seeded=False,
        source_uid=None, source_rel=None,
    )
    base.update(overrides)
    return RecordSummary(**base)


def _rows(block: str) -> list[str]:
    return [line for line in block.splitlines() if line.startswith("| ") and "started (UTC)" not in line]


def _render(tree: Schema2Tree, project: str = DEFAULT_PROJECT, **kwargs) -> str:
    return pi.render_project_block(pi.scan_project(tree.project_dir(project), **kwargs))


# ---------------------------------------------------------------------------
# The state vocabulary (session_record.display_state)
# ---------------------------------------------------------------------------


class TestContinuesResolvesByUid:
    """A1-rest.index-labels: a `continues` source is addressed by
    `session.source_session_uid`, so a folded, freeze-renamed or archived
    source never dangles."""

    def _move(self, tree, uid, dest_rel):
        src = tree.record_dir(DEFAULT_PROJECT, uid)
        dest = tree.project_dir(DEFAULT_PROJECT) / dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dest)
        tree.records[DEFAULT_PROJECT][uid] = dest
        return dest

    def test_a_folded_source_resolves_to_its_new_group(self, tree):
        src, cont = uid_for(1), uid_for(2)
        tree.add(DEFAULT_PROJECT, RecordSpec(src, "chat-.", stamp(1), stamp(1, 11)))
        self._move(tree, src, f"_runs/chat/{src}")
        tree.add(DEFAULT_PROJECT, RecordSpec(
            cont, "chat", stamp(2), stamp(2, 11), source_uid=src,
            source_path=f"{DEFAULT_PROJECT}/_runs/chat-./{src}"))
        assert f"continues [{src[:8]}](_runs/chat/{src}/)" in _rows(_render(tree))[0]

    def test_an_archived_source_resolves_to_the_archive(self, tree):
        src, cont = uid_for(1), uid_for(2)
        tree.add(DEFAULT_PROJECT, RecordSpec(src, "chat", stamp(1), stamp(1, 11)))
        self._move(tree, src, f"_archive/_runs/chat/{src}")
        tree.add(DEFAULT_PROJECT, RecordSpec(
            cont, "chat", stamp(2), stamp(2, 11), source_uid=src,
            source_path=f"{DEFAULT_PROJECT}/_runs/chat/{src}"))
        assert f"continues [{src[:8]}](_archive/_runs/chat/{src}/)" in _rows(_render(tree))[0]

    def test_an_unknown_uid_falls_back_to_the_recorded_path(self, tree):
        src, cont = uid_for(1), uid_for(2)
        tree.add(DEFAULT_PROJECT, RecordSpec(
            cont, "chat", stamp(2), stamp(2, 11), source_uid=src,
            source_path=f"{DEFAULT_PROJECT}/_runs/chat/{src}"))
        assert f"continues [{src[:8]}](_runs/chat/{src}/)" in _rows(_render(tree))[0]


class TestSeededRow:
    """A1-rest.index-labels: the `seeded` state word (D13)."""

    def _mark_seeded(self, tree, uid, seeded_by):
        import yaml as _yaml

        path = tree.record_dir(DEFAULT_PROJECT, uid) / "state.yaml"
        state = _yaml.safe_load(path.read_text())
        state["session"]["seeded_by"] = seeded_by
        path.write_text(_yaml.safe_dump(state, sort_keys=False))

    def test_a_seeded_record_reads_seeded_in_the_index(self, tree):
        uid = uid_for(1)
        tree.add(DEFAULT_PROJECT, RecordSpec(uid, "chat", stamp(1), stamp(1, 11), signed_out=None))
        self._mark_seeded(tree, uid, "b" * 32)
        assert "| seeded |" in _rows(_render(tree))[0]

    def test_a_seeded_record_reads_seeded_in_the_sessions_listing(self, tree):
        uid = uid_for(1)
        tree.add(DEFAULT_PROJECT, RecordSpec(uid, "chat", stamp(1), stamp(1, 11), signed_out=None))
        self._mark_seeded(tree, uid, "b" * 32)
        rows = run_state.list_sessions(base=tree.project_dir() / "_runs", now=stamp(1, 12))
        row = next(r for r in rows if r["id"] == uid)
        assert row["seeded"] is True and row["state"] == "seeded"

    def test_the_legend_lists_seeded(self):
        assert "**seeded**" in pi.PROJECT_LEGEND


class TestDisplayState:
    @pytest.mark.parametrize("status", ["complete", "archived"])
    def test_a_complete_or_archived_record_is_closed(self, status):
        state = session_record.display_state(
            readable=True, status=status, stop_reason="complete", signed_out=True, updated=UPDATED,
        )
        assert state == "closed"

    def test_a_recorded_question_outranks_the_sign_out(self):
        state = session_record.display_state(
            readable=True, status="in-progress", stop_reason="question", signed_out=True,
            updated=UPDATED,
        )
        assert state == "waiting on question"

    def test_a_seeded_record_reads_seeded(self):
        state = session_record.display_state(
            readable=True, status="in-progress", stop_reason=None, signed_out=False,
            updated=UPDATED, seeded=True,
        )
        assert state == "seeded"

    def test_seeded_outranks_ended_and_open(self):
        assert session_record.display_state(
            readable=True, status="in-progress", stop_reason=None, signed_out=True,
            updated=UPDATED, seeded=True,
        ) == "seeded"
        assert session_record.display_state(
            readable=True, status="in-progress", stop_reason=None, signed_out=False,
            updated=UPDATED, now=AT_WINDOW, seeded=True,
        ) == "seeded"

    def test_a_signed_out_in_progress_record_is_ended_not_closed(self):
        state = session_record.display_state(
            readable=True, status="in-progress", stop_reason=None, signed_out=True, updated=UPDATED,
        )
        assert state == "ended, not closed"

    def test_no_sign_out_and_no_clock_is_open(self):
        state = session_record.display_state(
            readable=True, status="in-progress", stop_reason=None, signed_out=False, updated=UPDATED,
        )
        assert state == "open"

    def test_unknown_sign_out_reads_as_open(self):
        state = session_record.display_state(
            readable=True, status="in-progress", stop_reason=None, signed_out=None, updated=UPDATED,
        )
        assert state == "open"

    def test_an_unreadable_record_is_unreadable_whatever_else_is_passed(self):
        state = session_record.display_state(
            readable=False, status="complete", stop_reason="question", signed_out=True,
            updated=UPDATED, now=AT_WINDOW,
        )
        assert state == "unreadable"

    def test_with_a_clock_an_open_record_inside_the_window_is_active(self):
        state = session_record.display_state(
            readable=True, status="in-progress", stop_reason=None, signed_out=False,
            updated=UPDATED, now=AT_WINDOW,
        )
        assert state == "active"

    def test_with_a_clock_an_open_record_past_the_window_is_stale(self):
        state = session_record.display_state(
            readable=True, status="in-progress", stop_reason=None, signed_out=False,
            updated=UPDATED, now=AT_WINDOW + timedelta(seconds=1),
        )
        assert state == "stale"

    def test_the_clock_accepts_the_iso_string_list_sessions_already_holds(self):
        later = (AT_WINDOW + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        state = session_record.display_state(
            readable=True, status="in-progress", stop_reason=None, signed_out=False,
            updated=UPDATED, now=later,
        )
        assert state == "stale"

    def test_the_clock_never_changes_a_signed_out_or_closed_verdict(self):
        far = AT_WINDOW + timedelta(days=400)
        ended = session_record.display_state(
            readable=True, status="in-progress", stop_reason=None, signed_out=True,
            updated=UPDATED, now=far,
        )
        closed = session_record.display_state(
            readable=True, status="complete", stop_reason=None, signed_out=False,
            updated=UPDATED, now=far,
        )
        assert (ended, closed) == ("ended, not closed", "closed")

    def test_an_unparseable_updated_stamp_with_a_clock_is_open(self):
        state = session_record.display_state(
            readable=True, status="in-progress", stop_reason=None, signed_out=False,
            updated="yesterday", now=AT_WINDOW,
        )
        assert state == "open"

    def test_an_out_of_range_updated_stamp_with_a_clock_is_open(self):
        """The UTC conversion of year 1 with a positive offset overflows; with
        a clock that is an unparseable `updated`, which reads `open`."""
        state = session_record.display_state(
            readable=True, status="in-progress", stop_reason=None, signed_out=False,
            updated="0001-01-01T00:00:00+01:00", now=AT_WINDOW,
        )
        assert state == "open"

    @pytest.mark.parametrize("status", [["complete"], {"status": "complete"}])
    def test_an_unhashable_status_is_never_closed_and_never_an_exception(self, status):
        """A hand-edited record can put a list or a mapping where the status
        string belongs, and `list_sessions` passes it through raw; the word
        is `open` (or `unreadable`), never a TypeError out of the listing."""
        readable = session_record.display_state(
            readable=True, status=status, stop_reason=None, signed_out=False, updated=UPDATED,
        )
        unreadable = session_record.display_state(
            readable=False, status=status, stop_reason=None, signed_out=False, updated=UPDATED,
        )
        assert (readable, unreadable) == ("open", "unreadable")

    def test_a_custom_window_moves_the_boundary(self):
        state = session_record.display_state(
            readable=True, status="in-progress", stop_reason=None, signed_out=False,
            updated=UPDATED, now=AT_WINDOW, stale_after=60,
        )
        assert state == "stale"

    def test_the_window_is_the_validators_window(self):
        from lmer_schema.checks.record import STALE_AFTER_SECONDS

        assert session_record.STALE_AFTER_SECONDS == STALE_AFTER_SECONDS == 14 * 24 * 60 * 60
        assert pi.STALE_AFTER_SECONDS is session_record.STALE_AFTER_SECONDS
        assert pi.display_state is session_record.display_state


# ---------------------------------------------------------------------------
# Labels, escaping, redaction
# ---------------------------------------------------------------------------


class TestLabel:
    def test_the_name_wins_and_is_not_derived(self):
        assert pi.label(_summary(name="named-run", goal_line="a goal", first_log="log")) == (
            "named-run", False)

    def test_the_goal_line_is_the_first_fallback(self):
        assert pi.label(_summary(name=None, goal_line="Fix the thing", first_log="log")) == (
            "Fix the thing", True)

    def test_the_first_log_message_is_the_second_fallback(self):
        assert pi.label(_summary(name=None, goal_line=None, first_log="Started work")) == (
            "Started work", True)

    def test_nothing_at_all_reads_unnamed(self):
        assert pi.label(_summary(name=None, goal_line=None, first_log=None)) == ("(unnamed)", True)

    def test_a_derived_label_is_marked_in_the_row_and_a_name_is_not(self):
        derived = pi.render_row(_summary(name=None, goal_line="Fix the thing"))
        named = pi.render_row(_summary(name="named-run"))
        assert "| _(unnamed)_ [Fix the thing](" in derived
        assert "| [named-run](" in named and "_(unnamed)_" not in named

    def test_the_last_fallback_renders_the_unnamed_mark_and_a_link(self):
        row = pi.render_row(_summary(name=None, goal_line=None, first_log=None))
        assert f"| _(unnamed)_ [(unnamed)](_runs/chat/{uid_for(1)}/) |" in row


class TestFlatten:
    @pytest.mark.parametrize("char,escaped", [
        ("|", "\\|"), ("[", "\\["), ("]", "\\]"), ("`", "\\`"), ("<", "\\<"), ("&", "\\&"),
        ("\\", "\\\\"),
    ])
    def test_table_and_markup_characters_are_escaped(self, char, escaped):
        assert pi.flatten(f"a{char}b") == f"a{escaped}b"

    def test_whitespace_collapses_and_control_characters_vanish(self):
        assert pi.flatten("  one\n\ttwo \x1b three\x00\x7f ") == "one two three"

    def test_a_short_label_is_untouched(self):
        assert pi.flatten("Fix issue 12") == "Fix issue 12"

    def test_a_long_label_is_cut_at_the_last_space_past_the_floor(self):
        words = " ".join(f"w{index:02d}" for index in range(40))  # 4-char words, 199 chars
        cut = pi.flatten(words)
        assert cut.endswith("…") and len(cut) <= pi.LABEL_MAX + 1
        assert not cut[:-1].endswith(" ") and cut[:-1] in words
        assert len(cut) - 1 >= pi.LABEL_CUT_FLOOR

    def test_a_long_label_without_a_usable_space_is_cut_hard(self):
        text = "short " + "x" * 120
        assert pi.flatten(text) == text[:pi.LABEL_MAX] + "…"

    def test_the_cut_respects_a_smaller_limit(self):
        assert pi.flatten("abcdefghij", limit=4) == "abcd…"

    def test_a_hard_cut_through_an_escapable_character_keeps_the_escape_whole(self):
        """The label is cut before it is escaped (the spec lists the two steps the
        other way round), so no cut can split an escape sequence and leave a
        dangling backslash before the ellipsis."""
        assert pi.flatten("abcd\\|xyz", limit=5) == "abcd\\\\…"
        assert pi.flatten("abc|defgh", limit=4) == "abc\\|…"

    def test_a_token_shaped_value_is_redacted(self):
        assert GLPAT not in pi.flatten(f"use {GLPAT} to push")
        assert "***REDACTED***" in pi.flatten(f"use {GLPAT} to push")

    def test_url_userinfo_is_redacted(self):
        flat = pi.flatten("clone https://oauth2:s3cretvalue@git.example.test/x.git")
        assert "s3cretvalue" not in flat and "git.example.test" in flat

    def test_redaction_never_reads_the_renderers_environment(self, monkeypatch):
        from work_repo import utils

        monkeypatch.setenv("MY_TOKEN", "plainwordvalue")
        assert "plainwordvalue" not in utils.redact_secrets("plainwordvalue here"), (
            "the probe must be a value redact_secrets WOULD take from the environment")
        flat = pi.flatten(f"plainwordvalue beside {GLPAT}")
        assert flat.startswith("plainwordvalue beside ")
        assert GLPAT not in flat


    def test_a_lone_surrogate_becomes_the_replacement_character(self):
        """A directory name that is not valid UTF-8 is listed with a surrogate
        escape (and a hand-edited `"\\uD800"` can put any lone surrogate in a
        record); generated text must still encode as UTF-8."""
        assert pi.flatten("a\udcffb") == "a\ufffdb"
        assert pi.flatten("a\ud800b") == "a\ufffdb"
        assert pi._code("a\udcffb") == "`a\ufffdb`"

# ---------------------------------------------------------------------------
# Stamps: coercion, formatting, ordering keys
# ---------------------------------------------------------------------------


class TestStamps:
    def test_a_string_passes_through_and_none_stays_none(self):
        assert pi._stamp("2026-09-20T10:00:00Z") == "2026-09-20T10:00:00Z"
        assert pi._stamp(None) is None
        assert pi._stamp("") is None

    def test_an_aware_datetime_becomes_its_utc_iso_string(self):
        plus_two = datetime(2026, 9, 20, 12, 0, tzinfo=timezone(timedelta(hours=2)))
        assert pi._stamp(plus_two) == "2026-09-20T10:00:00+00:00"

    def test_a_naive_datetime_is_read_as_utc_not_local_time(self):
        assert pi._stamp(datetime(2026, 9, 20, 10, 0)) == "2026-09-20T10:00:00+00:00"

    def test_another_scalar_is_stringified(self):
        assert pi._stamp(20260920) == "20260920"

    @pytest.mark.parametrize("value,expected", [
        ("2026-09-20T10:07:00Z", "2026-09-20 10:07"),
        ("2026-09-20T12:07:00+02:00", "2026-09-20 10:07"),
        ("2026-09-20T10:07:38.470654+00:00", "2026-09-20 10:07"),
        ("2026-09-20T10:07:00", "2026-09-20 10:07"),
        ("not a stamp at all, really", "not a stamp at a"),
        (None, "—"),
    ])
    def test_fmt_stamp_renders_utc_minutes_or_the_raw_head(self, value, expected):
        assert pi.fmt_stamp(value) == expected

    def test_a_raw_stamp_is_escaped_for_the_table(self):
        assert pi.fmt_stamp("a|b") == "a\\|b"

    @pytest.mark.parametrize("stamp", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
    def test_a_stamp_whose_utc_instant_is_out_of_range_is_unparseable_not_an_error(self, stamp):
        """Year 1 with a positive offset, or 9999 with a negative one, parses
        but overflows the datetime range on the UTC conversion; such a stamp
        reads as unparseable — raw text, sorted after the parseable ones —
        never as an exception out of a renderer."""
        assert pi._utc_key(stamp) == (0, stamp)
        assert pi.fmt_stamp(stamp) == stamp[:16]

    def test_an_unquoted_out_of_range_stamp_keeps_the_spelling_its_quoted_twin_has(self):
        """YAML resolves an unquoted stamp to a datetime; when its UTC
        conversion overflows, the coerced string is the datetime's own
        spelling — exactly what the quoted twin reads as — so both twins
        render the same raw cell and neither becomes an unreadable row."""
        plus_one = datetime(1, 1, 1, 0, 0, tzinfo=timezone(timedelta(hours=1)))
        assert pi._stamp(plus_one) == "0001-01-01T00:00:00+01:00"
        assert pi.fmt_stamp(pi._stamp(plus_one)) == pi.fmt_stamp("0001-01-01T00:00:00+01:00")

    def test_the_sort_key_orders_instants_not_spellings(self):
        z = pi._utc_key("2026-09-20T10:00:00Z")
        plus_zero = pi._utc_key("2026-09-20T10:00:00+00:00")
        plus_two = pi._utc_key("2026-09-20T11:00:00+02:00")  # 09:00Z
        later = pi._utc_key("2026-09-20T10:00:00.000001Z")
        assert z == plus_zero
        assert plus_two < z < later
        assert z[0] == 1

    def test_unparseable_sorts_after_parseable_and_missing_last(self):
        assert pi._utc_key("soon") == (0, "soon")
        assert pi._utc_key(None) == (-1, "")
        assert pi._utc_key(None) < pi._utc_key("soon") < pi._utc_key("1900-01-01T00:00:00Z")


# ---------------------------------------------------------------------------
# Rows and blocks over real records
# ---------------------------------------------------------------------------


class TestRows:
    def test_the_worked_example_renders_byte_for_byte(self, tmp_path):
        tree = build_schema2_plexus(tmp_path / "plexus", {"work-notes": WORKED_EXAMPLE_RECORDS})
        text = pi.render_project_readme(tree.project_dir("work-notes"), heading="work-notes")
        expected = (
            "# work-notes\n\n" + START_MARKER + "\n\n## Runs\n\n" + pi.PROJECT_LEGEND + "\n\n"
            + "| started (UTC) | run | state | last activity | record |\n|---|---|---|---|---|\n"
            + "\n".join(WORKED_EXAMPLE_ROWS) + "\n\n" + END_MARKER + "\n"
        )
        assert text == expected

    def test_the_legend_carries_the_format_token(self):
        block = pi.render_project_block([])
        assert f"(lmer index format {FORMAT})" in block
        assert pi.block_format(block) == FORMAT

    def test_a_blank_line_follows_the_start_marker_and_precedes_the_end_marker(self):
        block = pi.render_project_block([_summary()])
        assert block.startswith(START_MARKER + "\n\n## Runs\n\n")
        assert block.endswith("|\n\n" + END_MARKER)

    def test_zero_records_replace_the_table_with_the_empty_body(self, tree):
        block = _render(tree)
        assert "_No run records yet._" in block
        assert "| started (UTC) |" not in block

    def test_a_continuation_links_its_source_record(self, tree):
        src, cont = uid_for(1), uid_for(2)
        tree.add(DEFAULT_PROJECT,
                 RecordSpec(src, "chat", stamp(1), stamp(1, 11)),
                 RecordSpec(cont, "chat", stamp(2), stamp(2, 11), source_uid=src))
        row = _rows(_render(tree))[0]
        assert f"](_runs/chat/{cont}/) · continues [{src[:8]}](_runs/chat/{src}/) |" in row

    def test_a_source_path_without_a_runs_component_shows_a_plain_uid(self, tree):
        src, cont = uid_for(1), uid_for(2)
        tree.add(DEFAULT_PROJECT, RecordSpec(cont, "chat", stamp(2), stamp(2, 11), source_uid=src,
                                             source_path="elsewhere/archive/" + src))
        row = _rows(_render(tree))[0]
        assert f" · continues `{src[:8]}` |" in row

    def test_a_source_record_renamed_at_the_freeze_is_linked_by_its_recorded_path(self, tree):
        src, cont = uid_for(1), uid_for(2)
        tree.add(DEFAULT_PROJECT,
                 RecordSpec(src, "chat", stamp(1), stamp(1, 11), name="first", dir_suffix="first"),
                 RecordSpec(cont, "chat", stamp(2), stamp(2, 11), source_uid=src))
        assert f"continues [{src[:8]}](_runs/chat/{src}--first/)" in _rows(_render(tree))[0]

    def test_a_flat_legacy_record_links_the_group_and_shows_no_id(self, tree):
        tree.add(DEFAULT_PROJECT, RecordSpec("20260101-010101-1-1", "develop-issue-7", stamp(1),
                                             stamp(1, 11), schema=1, flat=True, name="flat-run"))
        row = _rows(_render(tree))[0]
        assert "| [flat-run](_runs/develop-issue-7/) |" in row
        assert row.endswith("| `develop-issue-7` · — |")

    def test_an_unreadable_record_renders_the_unreadable_row(self, tree):
        uid = uid_for(3)
        tree.add(DEFAULT_PROJECT, RecordSpec(uid, "chat", stamp(1), stamp(1, 11), readable=False))
        assert _rows(_render(tree)) == [
            f"| — | [{uid}](_runs/chat/{uid}/) | unreadable | — | `chat` · `{uid[:8]}` |"]

    def test_the_slug_falls_back_to_the_group_directory_name(self, tree):
        rdir = tree.project_dir() / "_runs" / "chat-v9" / uid_for(4)
        rdir.mkdir(parents=True)
        (rdir / "state.yaml").write_text("schema: 2\nname: bare\nstatus: in-progress\n")
        assert _rows(_render(tree))[0].endswith(f"| `chat-v9` · `{uid_for(4)[:8]}` |")

    @pytest.mark.parametrize("stop_reason,status,expected", [
        ("yield", "in-progress", "ended, not closed (yield)"),
        ("critical_error", "in-progress", "ended, not closed (critical_error)"),
        ("aborted", "in-progress", "ended, not closed (aborted)"),
        ("complete", "complete", "closed"),
        ("question", "in-progress", "waiting on question"),
    ])
    def test_the_state_cell_carries_the_stop_reason_suffix(self, stop_reason, status, expected):
        row = pi.render_row(_summary(status=status, stop_reason=stop_reason, signed_out=True))
        assert f"| {expected} |" in row

    def test_a_recorded_phase_is_appended_to_the_state(self):
        row = pi.render_row(_summary(status="in-progress", signed_out=False, phase="spec"))
        assert "| open · phase spec |" in row

    def test_the_record_cell_shows_a_legacy_id_whole_and_a_uid_short(self):
        legacy = pi.render_row(_summary(uid="20260815-093000-1-10001", slug="chat-."))
        opaque = pi.render_row(_summary(uid=uid_for(5), slug="chat"))
        assert legacy.endswith("| `chat-.` · `20260815-093000-1-10001` |")
        assert opaque.endswith(f"| `chat` · `{uid_for(5)[:8]}` |")

    def test_the_log_is_read_only_when_name_and_goal_are_both_null(self, tree):
        tree.add(DEFAULT_PROJECT,
                 RecordSpec(uid_for(1), "chat", stamp(1), stamp(1, 1), name="named", log_message="L1"),
                 RecordSpec(uid_for(2), "chat", stamp(2), stamp(2, 1), goal="Goal\n\nmore", log_message="L2"),
                 RecordSpec(uid_for(3), "chat", stamp(3), stamp(3, 1), log_message="L3"))
        summaries = {s.uid: s for s in pi.scan_project(tree.project_dir())}
        assert summaries[uid_for(1)].first_log is None
        assert summaries[uid_for(2)].first_log is None
        assert summaries[uid_for(2)].goal_line == "Goal"
        assert summaries[uid_for(3)].first_log == "L3"

    def test_the_first_log_message_survives_a_long_multi_entry_log(self, tree):
        uid = uid_for(6)
        tree.add(DEFAULT_PROJECT, RecordSpec(uid, "chat", stamp(1), stamp(1, 1)))
        entries = [{"timestamp": stamp(1), "message": "first: multi\nline message", "extra": {"k": [1, 2]}}]
        entries += [{"timestamp": stamp(1), "message": f"later {i}", "level": "info"} for i in range(200)]
        text = yaml.safe_dump(entries, sort_keys=False)
        (tree.record_dir(DEFAULT_PROJECT, uid) / "log.yaml").write_text(text)
        assert pi._first_message(text) == yaml.safe_load(text)[0]["message"]
        assert pi.scan_project(tree.project_dir())[0].first_log == "first: multi\nline message"

    def test_a_log_over_the_read_cap_is_not_read(self, tree):
        uid = uid_for(7)
        tree.add(DEFAULT_PROJECT, RecordSpec(uid, "chat", stamp(1), stamp(1, 1), log_message="x"))
        log = tree.record_dir(DEFAULT_PROJECT, uid) / "log.yaml"
        log.write_text("- message: huge\n" + "# pad\n" * (pi.LOG_READ_CAP // 6 + 1))
        assert pi.scan_project(tree.project_dir())[0].first_log is None

    def test_link_segments_are_url_quoted(self, tree):
        tree.add(DEFAULT_PROJECT, RecordSpec("20260815-093000-1-10001", "chat", stamp(1), stamp(1, 1),
                                             schema=1, dir_suffix="menu plan", name="menu"))
        assert "](_runs/chat/20260815-093000-1-10001--menu%20plan/)" in _rows(_render(tree))[0]

    def test_no_generated_line_can_be_a_conflict_marker(self, tree):
        """Every free-text fragment of a row is one line, so a marker-shaped value
        (a hand-edited structural field, a hostile directory name) can never put
        seven identical characters at column 0 inside the generated block."""
        tree.add(DEFAULT_PROJECT,
                 RecordSpec(uid_for(1), "chat", stamp(1), stamp(1, 1), goal="======="),
                 RecordSpec(uid_for(2), "chat", stamp(2), stamp(2, 1), name="|||||||"),
                 RecordSpec(uid_for(3), "chat", stamp(3), stamp(3, 1), name="hostile-fields",
                            source_uid=uid_for(1)))
        hostile = tree.record_dir(DEFAULT_PROJECT, uid_for(3)) / "state.yaml"
        state = yaml.safe_load(hostile.read_text())
        state["updated"] = "x\n=======\n"
        state["slug"] = "chat\n|||||||"
        state["phase"] = "p\n>>>>>>>"
        state["session"]["source_session_uid"] = "src\n<<<<<<< ours"
        hostile.write_text(yaml.safe_dump(state, sort_keys=False))
        evil = tree.project_dir() / "_runs" / "chat" / "evil\n======="
        evil.mkdir()
        (evil / "state.yaml").write_text("state: [unclosed\n")
        block = _render(tree)
        assert "=======" in block
        assert len(_rows(block)) == 4, "every record is exactly one row"
        assert pi.MARKER_LINE_RE.search(block) is None
        assert not pi.hand_text_has_marker("# h\n\n" + block + "\n")

    def test_an_event_whose_type_is_not_a_string_is_ignored_not_fatal(self, tree):
        """A hand-edited events line with a list or mapping `type` has no lifecycle
        meaning; it is skipped like a torn line rather than raising out of the scan."""
        uid = uid_for(9)
        tree.add(DEFAULT_PROJECT, RecordSpec(uid, "chat", stamp(1), stamp(1, 1), goal="g",
                                             signed_out=False))
        events = tree.record_dir(DEFAULT_PROJECT, uid) / "events.jsonl"
        events.write_text(events.read_text() + '{"type": ["session_end"]}\n'
                          '{"type": {"session_end": 1}}\n')
        summary, = pi.scan_project(tree.project_dir())
        assert summary.readable and summary.signed_out is False
        assert "| open |" in pi.render_row(summary)

    def test_a_surprise_after_the_reads_is_one_unreadable_row_never_an_exception(self, tree, monkeypatch):
        uid = uid_for(9)
        tree.add(DEFAULT_PROJECT, RecordSpec(uid, "chat", stamp(1), stamp(1, 1), goal="g"))

        def explode(_events):
            raise RuntimeError("surprise")

        monkeypatch.setattr(pi, "signed_out", explode)
        summary, = pi.scan_project(tree.project_dir())
        assert not summary.readable and summary.dir_name == uid
        assert "| unreadable |" in pi.render_row(summary)

    def test_a_whitespace_only_name_falls_through_to_the_goal(self, tree):
        tree.add(DEFAULT_PROJECT, RecordSpec(uid_for(1), "chat", stamp(1), stamp(1, 1), name="   ", goal="g"))
        summary, = pi.scan_project(tree.project_dir())
        assert summary.name is None
        assert "| _(unnamed)_ [g](" in pi.render_row(summary)

    def test_a_whitespace_only_name_and_goal_still_read_the_log(self, tree):
        tree.add(DEFAULT_PROJECT, RecordSpec(uid_for(1), "chat", stamp(1), stamp(1, 1), name=" ",
                                             goal="\n", log_message="L"))
        summary, = pi.scan_project(tree.project_dir())
        assert (summary.name, summary.goal_line, summary.first_log) == (None, None, "L")

    def test_a_log_whose_first_entry_cannot_be_constructed_yields_no_label(self):
        assert pi._first_message("- timestamp: 2026-13-45\n  message: m\n- message: later\n") is None

    def test_summaries_are_frozen_records_of_their_own_file(self, tree):
        uid = uid_for(8)
        tree.add(DEFAULT_PROJECT, RecordSpec(uid, "chat", stamp(1), stamp(1, 1), goal="g"))
        summary = pi.scan_project(tree.project_dir())[0]
        assert summary.dir_rel == f"_runs/chat/{uid}" and summary.uid == uid
        with pytest.raises(AttributeError):
            summary.name = "x"  # type: ignore[misc]


    def test_a_record_directory_that_is_not_utf8_renders_one_row_and_writes(self, tree):
        """The walk lists such a name with a surrogate escape; the row links the
        real byte (`%FF`, which the server resolves) and shows the replacement
        character, so the renderer never raises for a record it listed and the
        README still encodes."""
        tree.add(DEFAULT_PROJECT, RecordSpec(uid_for(1), "chat", stamp(1), stamp(1, 1), name="fine"))
        group = os.fsencode(str(tree.project_dir() / "_runs" / "chat"))
        evil = os.path.join(group, b"evil\xff")
        try:
            os.mkdir(evil)
        except (OSError, UnicodeEncodeError) as exc:
            pytest.skip(f"the filesystem refuses a non-UTF-8 name: {exc}")
        with open(os.path.join(evil, b"state.yaml"), "wb") as handle:
            handle.write(b"schema: 2\nname: evil\nstatus: in-progress\ncreated: '2026-09-02T10:00:00Z'\n")
        summaries = pi.scan_project(tree.project_dir())
        assert {s.dir_name for s in summaries} == {uid_for(1), "evil\udcff"}
        block = pi.render_project_block(summaries)
        block.encode("utf-8")
        assert "| [evil](_runs/chat/evil%FF/) |" in block
        assert "| `chat` · `evil\ufffd` |" in block
        assert pi.write_project_readme(tree.project_dir(), heading="widgets") == (True, None)
        assert "[evil](_runs/chat/evil%FF/)" in tree.readme().read_text(encoding="utf-8")

    def test_a_link_quotes_its_bytes_so_any_lone_surrogate_still_links(self):
        assert pi._link("_runs/chat/a\udcffb") == "_runs/chat/a%FFb/"
        assert pi._link("_runs/chat/a\ud800b") == "_runs/chat/a%3Fb/"
        assert pi._link("_runs/chat-./caf\u00e9 x") == "_runs/chat-./caf%C3%A9%20x/"

# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


class TestOrdering:
    def test_newest_first_by_session_start_with_created_as_the_fallback(self, tree):
        tree.add(DEFAULT_PROJECT,
                 RecordSpec(uid_for(1), "chat", stamp(2), stamp(2, 1), name="middle"),
                 RecordSpec("20260903-100000-1-1", "chat", stamp(3), stamp(3, 1), schema=1, name="newest"),
                 RecordSpec(uid_for(3), "chat", stamp(1), stamp(1, 1), name="oldest"))
        labels = [re.search(r"\[([^\]]+)\]", row).group(1) for row in _rows(_render(tree))]
        assert labels == ["newest", "middle", "oldest"]

    def test_equal_instants_tie_break_on_the_directory_name_descending(self, tree):
        tree.add(DEFAULT_PROJECT,
                 RecordSpec("0" * 32, "chat", stamp(1), stamp(1, 1), name="zeros"),
                 RecordSpec("f" * 32, "chat", stamp(1), stamp(1, 1), name="effs"))
        labels = [re.search(r"\[([^\]]+)\]", row).group(1) for row in _rows(_render(tree))]
        assert labels == ["effs", "zeros"]

    def test_the_order_ignores_file_modification_times(self, tree):
        tree.add(DEFAULT_PROJECT,
                 RecordSpec(uid_for(1), "chat", stamp(1), stamp(1, 1), name="older"),
                 RecordSpec(uid_for(2), "chat", stamp(2), stamp(2, 1), name="newer"))
        future = 2000000000
        for path in (tree.record_dir(DEFAULT_PROJECT, uid_for(1)),
                     tree.record_dir(DEFAULT_PROJECT, uid_for(1)) / "state.yaml"):
            os.utime(path, (future, future))
        labels = [re.search(r"\[([^\]]+)\]", row).group(1) for row in _rows(_render(tree))]
        assert labels == ["newer", "older"]

    def test_an_offset_stamp_sorts_and_prints_by_its_utc_instant(self, tree):
        tree.add(DEFAULT_PROJECT,
                 RecordSpec(uid_for(1), "chat", "2026-09-20T12:30:00+02:00", stamp(20, 12), name="plus-two"),
                 RecordSpec(uid_for(2), "chat", "2026-09-20T11:00:00Z", stamp(20, 12), name="zulu"))
        rows = _rows(_render(tree))
        assert [re.search(r"\[([^\]]+)\]", row).group(1) for row in rows] == ["zulu", "plus-two"]
        assert rows[1].startswith("| 2026-09-20 10:30 |")

    def test_z_and_plus_zero_spellings_interleave_by_instant(self, tree):
        tree.add(DEFAULT_PROJECT,
                 RecordSpec(uid_for(1), "chat", "2026-09-20T10:00:00+00:00", stamp(20), name="a"),
                 RecordSpec(uid_for(2), "chat", "2026-09-20T10:00:01Z", stamp(20), name="b"),
                 RecordSpec(uid_for(3), "chat", "2026-09-20T10:00:00.500000+00:00", stamp(20), name="c"),
                 RecordSpec(uid_for(4), "chat", "2026-09-20T09:59:59Z", stamp(20), name="d"))
        labels = [re.search(r"\[([^\]]+)\]", row).group(1) for row in _rows(_render(tree))]
        assert labels == ["b", "c", "a", "d"]

    def test_unparseable_stamps_follow_parseable_ones_and_missing_come_last(self, tree):
        tree.add(DEFAULT_PROJECT,
                 RecordSpec(uid_for(1), "chat", "soon", stamp(20), name="odd"),
                 RecordSpec(uid_for(2), "chat", "1900-01-01T00:00:00Z", stamp(20), name="ancient"))
        rdir = tree.project_dir() / "_runs" / "chat" / uid_for(3)
        rdir.mkdir()
        (rdir / "state.yaml").write_text("schema: 2\nname: dateless\nstatus: in-progress\n")
        labels = [re.search(r"\[([^\]]+)\]", row).group(1) for row in _rows(_render(tree))]
        assert labels == ["ancient", "odd", "dateless"]

    def test_unquoted_stamps_render_exactly_like_quoted_ones(self, tmp_path):
        spec = RecordSpec(uid_for(1), "chat", "2026-09-20T12:30:00+02:00", "2026-09-21T10:00:00Z",
                          goal="twin")
        quoted = build_schema2_plexus(tmp_path / "quoted", {"p": [spec]})
        unquoted = build_schema2_plexus(tmp_path / "unquoted",
                                        {"p": [RecordSpec(**{**spec.__dict__, "quoted_stamps": False})]})
        raw = yaml.safe_load((unquoted.record_dir("p", uid_for(1)) / "state.yaml").read_text())
        assert isinstance(raw["created"], datetime), "the twin must really hold YAML datetimes"
        assert _render(quoted, "p") == _render(unquoted, "p")
        assert "| 2026-09-20 10:30 |" in _render(unquoted, "p")

    def test_an_existing_row_never_moves_when_a_sibling_is_added(self, tree):
        tree.add(DEFAULT_PROJECT,
                 RecordSpec(uid_for(1), "chat", stamp(5), stamp(5, 1), name="one"),
                 RecordSpec(uid_for(2), "chat", stamp(6), stamp(6, 1), name="two"))
        before = _rows(_render(tree))
        tree.add(DEFAULT_PROJECT,
                 RecordSpec(uid_for(3), "chat", stamp(9), stamp(9, 1), name="newer"),
                 RecordSpec(uid_for(4), "chat", stamp(1), stamp(1, 1), name="older"),
                 RecordSpec(uid_for(5), "review", "2026-09-05T12:00:00+02:00", stamp(5, 1), name="same-instant"))
        after = _rows(_render(tree))
        assert [row for row in after if row in before] == before
        assert len(after) == 5


# ---------------------------------------------------------------------------
# Splice, markers, formats, screens
# ---------------------------------------------------------------------------


BLOCK = pi.render_project_block([])
NEW_BLOCK = pi.render_project_block([_summary()])


class TestSplice:
    def test_no_file_yields_a_heading_and_the_block(self):
        assert pi.splice_block(None, BLOCK, heading="widgets") == f"# widgets\n\n{BLOCK}\n"

    def test_a_markerless_file_gets_the_block_appended_with_one_normalised_gap(self):
        assert pi.splice_block("# Hand\n\nprose\n\n\n", BLOCK, heading="x") == f"# Hand\n\nprose\n\n{BLOCK}\n"
        assert pi.splice_block("# Hand", BLOCK, heading="x") == f"# Hand\n\n{BLOCK}\n"

    def test_a_marked_span_is_replaced_and_every_other_byte_kept(self):
        text = f"# Hand\r\n\r\npre  \n{BLOCK}\n\n\npost\n\t\n"
        assert pi.splice_block(text, NEW_BLOCK, heading="x") == f"# Hand\r\n\r\npre  \n{NEW_BLOCK}\n\n\npost\n\t\n"

    def test_the_splice_is_idempotent(self):
        once = pi.splice_block("# Hand\n", BLOCK, heading="x")
        assert pi.splice_block(once, BLOCK, heading="x") == once

    def test_an_empty_file_is_treated_as_absent(self):
        assert pi.splice_block("", BLOCK, heading="w") == pi.splice_block(None, BLOCK, heading="w")

    def test_the_heading_is_one_line_so_a_hostile_name_cannot_plant_a_marker(self):
        """The heading is the one generated fragment outside the block; a name
        holding a newline and seven `=` must not become a conflict-marker line
        in the hand text, which the staging gate would refuse forever."""
        text = pi.splice_block(None, BLOCK, heading="h\n=======")
        assert text.startswith("# h =======\n\n" + START_MARKER)
        assert not pi.hand_text_has_marker(text)

    @pytest.mark.parametrize("text", [
        f"a\n{START_MARKER}\nb\n", f"a\n{END_MARKER}\nb\n", f"{END_MARKER}\nx\n{START_MARKER}\n",
        f"{START_MARKER}\n{END_MARKER}\n{START_MARKER}\n{END_MARKER}\n",
    ])
    def test_malformed_markers_raise(self, text):
        with pytest.raises(MalformedMarkers):
            pi.splice_block(text, BLOCK, heading="x")
        with pytest.raises(MalformedMarkers):
            pi.extract_block(text)

    def test_extract_block_returns_exactly_the_rendered_span(self):
        assert pi.extract_block(f"pre\n{BLOCK}\npost\n") == BLOCK
        assert pi.extract_block("no markers here\n") is None

    def test_strip_block_compares_hand_text_whatever_the_trailing_newlines(self):
        assert pi.strip_block(f"# H\n\n{BLOCK}\n") == pi.strip_block(f"# H\n\n\n\n{BLOCK}") == "# H"
        assert pi.strip_block("plain\n\n") == "plain"
        assert pi.strip_block(f"# A\n{BLOCK}\n") != pi.strip_block(f"# B\n{BLOCK}\n")

    def test_block_format_reads_the_legend_token(self):
        assert pi.block_format(BLOCK) == 1
        assert pi.block_format(BLOCK.replace("format 1)", "format 2)")) == 2
        assert pi.block_format("<!-- lmer:index:start -->\nno token\n<!-- lmer:index:end -->") is None


class TestMarkerScreen:
    @pytest.mark.parametrize("line", ["=======", "<<<<<<< x", ">>>>>>>\t", "|||||||"])
    def test_a_seven_character_marker_line_is_caught(self, line):
        assert pi.hand_text_has_marker(f"# H\n\n{line}\nmore\n")
        assert pi.hand_text_has_marker(f"# H\n\n{line}")

    @pytest.mark.parametrize("line", ["========", " =======", "===", "=======x", "<<<<<<=", "====== ="])
    def test_anything_else_passes(self, line):
        assert not pi.hand_text_has_marker(f"# H\n\n{line}\nmore\n")

    def test_a_crlf_file_is_screened_too(self):
        assert pi.hand_text_has_marker("# H\r\n=======\r\n")

    def test_markers_inside_the_generated_block_are_ignored(self):
        planted = BLOCK.replace("\n\n" + END_MARKER, "\n=======\n\n" + END_MARKER)
        assert not pi.hand_text_has_marker(f"# H\n\n{planted}\n")
        assert pi.hand_text_has_marker(f"# H\n=======\n\n{planted}\n")

    def test_the_screen_is_gits_rule_for_the_commit_gate(self, tmp_path):
        """A README the screen passes commits; one it catches is what `diff --check` refuses."""
        repo = tmp_path / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "--initial-branch=main")
        for name, text in {"clean.md": "# H\n\n========\n", "dirty.md": "# H\n\n=======\n"}.items():
            (repo / name).write_text(text)
            git(repo, "add", "--", name)
        refused = git(repo, "diff", "--cached", "--check", "--", "clean.md", "dirty.md", check=False)
        assert refused.returncode != 0 and "dirty.md" in refused.stdout and "clean.md" not in refused.stdout
        assert not pi.hand_text_has_marker((repo / "clean.md").read_text())
        assert pi.hand_text_has_marker((repo / "dirty.md").read_text())


class TestMarkerTextInRecords:
    """A marker is a whole line of the block grammar: column 0, nothing after
    it but trailing whitespace. The same text inside a record's free text (a
    goal, a log message, a slug) or a manifest description is one more escaped
    label, and a README it lands in stays regenerable."""

    def test_marker_text_in_a_goal_a_log_message_and_a_slug_keeps_the_index_regenerable(self, tree):
        tree.add(DEFAULT_PROJECT,
                 RecordSpec(uid_for(1), "chat", stamp(1), stamp(1, 1),
                            goal=f"debug the {END_MARKER} splice in the project README"),
                 RecordSpec(uid_for(2), "chat", stamp(2), stamp(2, 1), log_message=f"see {START_MARKER} here"),
                 RecordSpec(uid_for(3), START_MARKER, stamp(3), stamp(3, 1), name="marker-slug"))
        assert pi.write_project_readme(tree.project_dir(), heading="widgets") == (True, None)
        text = tree.readme().read_text()
        assert text.count(START_MARKER) >= 2 and text.count(END_MARKER) >= 2, (
            "the probe must really plant the marker text inside the rows")
        block = pi.extract_block(text)
        assert len(_rows(block)) == 3
        assert block == pi.render_project_block(pi.scan_project(tree.project_dir()))
        assert pi.write_project_readme(tree.project_dir(), heading="widgets") == (False, text.encode("utf-8"))
        assert pi.strip_block(text) == "# widgets"
        assert not pi.hand_text_has_marker(text)

    def test_a_marker_mentioned_mid_line_in_hand_text_is_prose(self):
        hand = f"# W\n\nThe block sits between `{START_MARKER}` and `{END_MARKER}`.\n"
        assert pi.extract_block(hand) is None
        assert pi.strip_block(hand) == hand.rstrip("\n")
        assert not pi.hand_text_has_marker(hand)
        spliced = pi.splice_block(hand, BLOCK, heading="x")
        assert spliced == f"{hand}\n{BLOCK}\n"
        assert pi.extract_block(spliced) == BLOCK
        assert pi.splice_block(spliced, NEW_BLOCK, heading="x") == f"{hand}\n{NEW_BLOCK}\n"

    @pytest.mark.parametrize("tail", ["", "  ", "\t", "\r"], ids=["bare", "spaces", "tab", "cr"])
    def test_a_marker_line_may_carry_trailing_whitespace_which_the_splice_keeps(self, tail):
        text = f"# W\n\n{START_MARKER}{tail}\nbody\n{END_MARKER}{tail}\npost\n"
        assert pi.extract_block(text) == f"{START_MARKER}{tail}\nbody\n{END_MARKER}"
        assert pi.splice_block(text, BLOCK, heading="x") == f"# W\n\n{BLOCK}{tail}\npost\n"

    def test_an_indented_marker_is_not_a_marker(self):
        """Column 0 is the grammar (an indented HTML comment is a Markdown code
        block); a block is appended rather than spliced into a human's indented pair."""
        text = f"# W\n\n    {START_MARKER}\n    {END_MARKER}\n"
        assert pi.extract_block(text) is None
        assert pi.splice_block(text, BLOCK, heading="x") == f"{text}\n{BLOCK}\n"

    def test_a_manifest_description_holding_the_marker_text_is_one_escaped_cell(self, tmp_path):
        manifest = {"name": "example", "projects": {"p": {"description": f"x {END_MARKER} y"}}}
        block = pi.render_root_block(manifest)
        assert block.count(END_MARKER) == 2, "the probe must really plant the marker text in a row"
        root = tmp_path / "plexus"
        root.mkdir()
        text = pi.render_root_readme(root, manifest)
        (root / "README.md").write_text(text)
        assert pi.render_root_readme(root, manifest) == text
        assert pi.extract_block(text) == block


class TestProjectRelsForPaths:
    PROJECTS = ("a", "work-notes", "h/g/p")

    @pytest.mark.parametrize("path,expected", [
        ("a/_runs/x", ["a"]), ("a/_runs", ["a"]), ("a/_runs/chat/" + "0" * 32, ["a"]),
        ("h/g/p/_runs/x", ["h/g/p"]), ("a/_runs_x/y", []), ("a/_archive/_runs/x", []),
        ("work-notes/_archive/_runs/x", []), ("a/_agents/memory/x.md", []),
        ("_agents/memory/x.md", []), ("_napkin/example/idea.md", []), ("README.md", []),
    ])
    def test_one_path(self, path, expected):
        assert pi.project_rels_for_paths([path], self.PROJECTS) == expected

    def test_results_are_deduplicated_in_first_seen_order(self):
        paths = ["work-notes/_runs/chat/x", "a/_runs/y", "work-notes/_runs/chat", "h/g/p/_runs/z", "a/_runs"]
        assert pi.project_rels_for_paths(paths, self.PROJECTS) == ["work-notes", "a", "h/g/p"]

    def test_the_longest_matching_project_wins(self):
        assert pi.project_rels_for_paths(["h/g/p/_runs/x"], ("h", "h/g/p")) == ["h/g/p"]


# ---------------------------------------------------------------------------
# The fast state reader
# ---------------------------------------------------------------------------


def _full_parse(path: Path):
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _parity(monkeypatch, tree: Schema2Tree, project: str) -> list[RecordSummary]:
    fast = pi.scan_project(tree.project_dir(project))
    monkeypatch.setattr(pi, "read_state_fast", _full_parse)
    full = pi.scan_project(tree.project_dir(project))
    assert fast == full and fast
    return fast


class TestReadStateFast:
    def test_the_fixture_records_and_a_twenty_deep_chain_agree_with_a_full_parse(self, tmp_path, monkeypatch):
        tree = build_schema2_plexus(tmp_path / "plexus", {"p": WORKED_EXAMPLE_RECORDS})
        tree.add("p",
                 RecordSpec(uid_for(1), "chat", stamp(1), stamp(1, 1), readable=False),
                 RecordSpec(uid_for(2), "chat", stamp(2), stamp(2, 1), quoted_stamps=False, goal="g"),
                 RecordSpec(uid_for(3), "chat", stamp(3), stamp(3, 1), signed_out=None),
                 RecordSpec("20260101-010101-1-1", "flat", stamp(4), stamp(4, 1), schema=1, flat=True))
        specs = chain(tree, "p", 20)
        summaries = _parity(monkeypatch, tree, "p")
        by_uid = {s.uid: s for s in summaries}
        deepest = by_uid[specs[-1].uid]
        assert deepest.source_uid == specs[-2].uid
        assert deepest.source_rel == f"_runs/chat/{specs[-2].uid}"

    def test_the_cut_is_what_reads_a_chain_record(self, tmp_path):
        tree = build_schema2_plexus(tmp_path / "plexus")
        specs = chain(tree, DEFAULT_PROJECT, 3)
        path = tree.record_dir(DEFAULT_PROJECT, specs[-1].uid) / "state.yaml"
        fast = pi.read_state_fast(path)
        assert fast["context"] == {"source_path": f"{DEFAULT_PROJECT}/_runs/chat/{specs[-2].uid}"}
        assert set(_full_parse(path)["context"]) > {"source_path", "state", "events"}
        assert fast["session"] == _full_parse(path)["session"]

    def test_a_flow_style_context_falls_back_to_the_full_parse(self, tree, monkeypatch):
        uid, src = uid_for(1), uid_for(2)
        tree.add(DEFAULT_PROJECT, RecordSpec(uid, "chat", stamp(1), stamp(1, 1), source_uid=src))
        path = tree.record_dir(DEFAULT_PROJECT, uid) / "state.yaml"
        text = re.sub(r"^context:\n(?:[ \t].*\n|\n)*", "", path.read_text(), flags=re.M)
        text += f"context: {{source_session_uid: {src}, source_path: widgets/_runs/chat/{src},\n  state: {{}}}}\n"
        path.write_text(text)
        assert pi.read_state_fast(path)["context"] == _full_parse(path)["context"]
        assert _parity(monkeypatch, tree, DEFAULT_PROJECT)[0].source_rel == f"_runs/chat/{src}"

    def test_a_record_without_context_is_read_whole(self, tree, monkeypatch):
        tree.add(DEFAULT_PROJECT, RecordSpec(uid_for(1), "chat", stamp(1), stamp(1, 1), goal="plain"))
        path = tree.record_dir(DEFAULT_PROJECT, uid_for(1)) / "state.yaml"
        assert "context" not in path.read_text()
        assert pi.read_state_fast(path) == _full_parse(path)
        _parity(monkeypatch, tree, DEFAULT_PROJECT)

    @pytest.mark.parametrize("value,expected", [
        ("'widgets/_runs/chat/src'", "widgets/_runs/chat/src"),
        ('"widgets/_runs/chat/src"  # trailing note', "widgets/_runs/chat/src"),
        ("widgets/_runs/chat/src", "widgets/_runs/chat/src"),
    ])
    def test_a_quoted_or_commented_source_path_is_read_as_the_loader_would(self, tmp_path, value, expected):
        path = tmp_path / "state.yaml"
        path.write_text(f"schema: 2\ncontext:\n  source_session_uid: x\n  source_path: {value}\n"
                        "  state:\n    schema: 2\nsessions:\n- a\n")
        assert pi.read_state_fast(path)["context"] == {"source_path": expected}
        assert pi.read_state_fast(path)["sessions"] == ["a"]

    def test_a_block_scalar_or_oddly_indented_source_path_falls_back(self, tmp_path):
        block = tmp_path / "block.yaml"
        block.write_text("schema: 2\ncontext:\n  source_path: |\n    widgets/_runs/chat/src\n")
        indented = tmp_path / "indented.yaml"
        indented.write_text("schema: 2\ncontext:\n    source_path: widgets/_runs/chat/src\n")
        for path in (block, indented):
            assert pi.read_state_fast(path) == _full_parse(path)
            assert pi.read_state_fast(path)["context"]["source_path"].strip() == "widgets/_runs/chat/src"
            assert set(pi.read_state_fast(path)["context"]) == {"source_path"}, "a full parse, not a stub"

    def test_unreadable_or_non_mapping_state_is_none(self, tmp_path):
        broken = tmp_path / "broken.yaml"
        broken.write_text("state: [unclosed\n")
        listed = tmp_path / "list.yaml"
        listed.write_text("- a\n- b\n")
        assert pi.read_state_fast(broken) is None
        assert pi.read_state_fast(listed) is None
        assert pi.read_state_fast(tmp_path / "missing.yaml") is None

    def test_an_impossible_date_is_a_constructor_error_the_reader_turns_into_none(self, tmp_path):
        """The timestamp constructor is Python under every loader (libyaml only
        scans and composes) and raises a bare ValueError, not a YAMLError, for an
        unquoted impossible date; the reader's contract is None."""
        text = "schema: 2\nname: x\ncreated: 2026-13-45\n"
        path = tmp_path / "state.yaml"
        path.write_text(text)
        with pytest.raises(ValueError) as caught:
            yaml.load(text, Loader=pi._LOADER)
        assert not isinstance(caught.value, yaml.YAMLError), "the probe must not be a YAMLError"
        assert pi.read_state_fast(path) is None

    def test_pathological_nesting_reads_exactly_as_the_kernels_loader_reads_it(self, tmp_path):
        """Pure-Python PyYAML composes recursively and raises RecursionError on
        the probe, which the reader turns into None; libyaml (the container image,
        every manylinux wheel) composes it in C and the reader hands the mapping
        through. The reader's verdict is its own loader's, whichever the host has."""
        text = "schema: 2\ngoal: " + "[" * 5000 + "]" * 5000 + "\n"
        path = tmp_path / "state.yaml"
        path.write_text(text)
        try:
            expected = yaml.load(text, Loader=pi._LOADER)
        except RecursionError:
            expected = None
        result = pi.read_state_fast(path)
        if expected is None:
            assert result is None
        else:
            assert result is not None and result["schema"] == 2
            assert isinstance(result["goal"], list)

    def test_a_recursion_error_from_the_loader_reads_as_none_on_both_paths(self, tmp_path, monkeypatch):
        """Pinned without relying on which loader the host has: a loader that blows
        the stack makes the cut parse answer None, then the full parse, then the reader."""
        path = tmp_path / "state.yaml"
        path.write_text("schema: 2\ncontext:\n  state: {}\nstatus: in-progress\n")
        assert pi._cut_context(path.read_text()) is not None, "the probe must take the cut"
        calls = []

        class Exploding(yaml.SafeLoader):
            def get_single_data(self):
                calls.append(1)
                raise RecursionError("maximum recursion depth exceeded")

        monkeypatch.setattr(pi, "_LOADER", Exploding)
        assert pi.read_state_fast(path) is None
        assert len(calls) == 2, "the cut parse raised, then the full parse raised"

    def test_a_bracket_in_an_ancestors_prose_keeps_the_cut(self, tmp_path, monkeypatch):
        """Brackets and quotes inside prose are not YAML tokens: a chain whose every
        goal opens a bracket still reads through the cut (the deep-chain lever)."""
        tree = build_schema2_plexus(tmp_path / "plexus")
        specs = chain(tree, DEFAULT_PROJECT, 6, goal="[WIP chain goal: it's \"quoted\" {too}")
        path = tree.record_dir(DEFAULT_PROJECT, specs[-1].uid) / "state.yaml"
        assert path.read_text().count("[WIP") == 6, "every level of the chain carries the bracket"
        fast = pi.read_state_fast(path)
        assert fast["context"] == {"source_path": f"{DEFAULT_PROJECT}/_runs/chat/{specs[-2].uid}"}
        assert fast["goal"] == "[WIP chain goal: it's \"quoted\" {too}"
        _parity(monkeypatch, tree, DEFAULT_PROJECT)

    def test_a_production_shaped_chain_with_every_emitter_shape_takes_the_cut(self, tmp_path, monkeypatch):
        """The fixture writes records with `write_state`'s own emitter call, so a
        chain carrying every scalar shape it can produce — a plain scalar folded
        at width 80, a multi-line single-quoted scalar, a double-quoted scalar with
        `\\x`/`\\u` escapes and `\\` folds, indicators and both quote kinds inside
        quoted text, empty flow collections — reads through the cut and agrees
        with a full parse at every level."""
        shapes = list(EMITTER_SHAPES.values())
        tree = build_schema2_plexus(tmp_path / "plexus")
        specs = chain(tree, DEFAULT_PROJECT, 8, goal=lambda level: shapes[level % len(shapes)],
                      name=lambda level: ("plain name with many words " * 4).strip() if level % 2 else None)
        path = tree.record_dir(DEFAULT_PROJECT, specs[-1].uid) / "state.yaml"
        text = path.read_text()
        for shape in ("\\u2014", "\\xE9", "\\\n", "''", " #", "[WIP", "{}", "[]", "\n\n"):
            assert shape in text, f"the emitter must really have produced {shape!r}"
        assert max(len(line) for line in text.splitlines()) < 200, "long scalars are folded, not one line"
        fast = pi.read_state_fast(path)
        assert fast["context"] == {"source_path": f"{DEFAULT_PROJECT}/_runs/chat/{specs[-2].uid}"}
        assert fast["goal"] == shapes[7 % len(shapes)]
        _parity(monkeypatch, tree, DEFAULT_PROJECT)

    @pytest.mark.parametrize("text,status", [
        ("schema: 2\ngoal: fine\ncontext:\n  state: {a: 1,\nb: 2}\nstatus: in-progress\n", "in-progress"),
        ("schema: 2\ncontext:\n  note: \"leaks to\nstatus: complete\"\nstatus: in-progress\n", "in-progress"),
        ("schema: 2\ncontext:\n  note: 'leaks to\nstatus: complete'\nstatus: in-progress\n", "in-progress"),
        ("schema: 2\ncontext:\n  items: [a,\nstatus: complete]\nstatus: in-progress\n", "in-progress"),
        ("schema: 2\ncontext:\n  note: \"leaks to\nstatus: complete\nphase: x\"\nstatus: in-progress\n",
         "in-progress"),
        ("schema: 2\ncontext:\n  items: [\"]\",\nstatus: complete]\nstatus: in-progress\n", "in-progress"),
        ("schema: 2\ncontext:\n  - \"leaks to\nstatus: complete\"\nstatus: in-progress\n", "in-progress"),
        # A prose quote after a mid-scalar dash must not pair with the real opener below it.
        ("schema: 2\ncontext:\n  a: x - \"p\n  b: \"leak\nstatus: complete\"\nphase: x\n", None),
        ("schema: 2\ncontext:\n  a: x - \"p\n  b: [\"q\", r,\nstatus: complete]\nphase: x\n", None),
    ], ids=["flow-mapping", "double-quoted", "single-quoted", "flow-sequence",
            "double-quoted-two-lines", "quoted-bracket-in-flow", "sequence-item",
            "prose-quote-before-quoted-leak", "prose-quote-before-flow-leak"])
    def test_a_cut_that_would_lie_falls_back_to_the_full_parse(self, tmp_path, text, status):
        path = tmp_path / "state.yaml"
        path.write_text(text)
        full = _full_parse(path)
        assert full is not None and full.get("status") == status, (
            "the probe must be valid YAML whose leak swallows the status line")
        assert pi.read_state_fast(path) == full

    @pytest.mark.parametrize("block,expected", [
        ('  a: x - "p\n  b: "leak\n', False),
        ('  a: x - "p\n  b: "closed"\n', True),
        ('  a: x - [p\n  b: ["q", r,\n', False),
        ('  - "item"\n  key: "v"\n  my key: "w"\n  ? "k"\n  : "x"\n', True),
        ('  k: [1, 2]\n  l: {a: "b"}\n  m: !!str "t"\n  n: &a "u"\n', True),
        ('  k: x [1\n  l: it\'s fine (don\'t) {really}\n  m: key:"glued"\n', True),
        ('  k: [1,\n', False),
        ('  k: "open\n', False),
        ('  k: v # a "comment\n', True),
    ], ids=["prose-quote-then-leak", "prose-quote-then-closed", "prose-quote-then-flow-leak",
            "node-quotes-after-indicators", "flow-and-properties", "prose-tokens", "open-flow",
            "open-string", "comment"])
    def test_the_self_containment_check_reads_tokens_where_yaml_does(self, block, expected):
        """A quote or bracket begins a node only first on its line or after
        structural words (`-`, `?`, `key:`, a property); in prose it is text."""
        assert pi._block_is_self_contained(block) is expected


    def test_a_source_path_folded_by_the_emitter_is_read_whole(self, tmp_path, monkeypatch):
        """`yaml.safe_dump` folds a plain scalar at a space once the line passes
        column 80, so a source directory holding a space under a bucket-length
        project rel puts the tail of `source_path` on a continuation line the
        anchored expression cannot see; such a record takes the full parse."""
        project = "git.example.test/acme/widgets"
        tree = build_schema2_plexus(tmp_path / "plexus", {project: []}, repos={project: project})
        specs = chain(tree, project, 2,
                      dir_suffix=lambda level: "menu plan for the talk" if level == 0 else None)
        path = tree.record_dir(project, specs[1].uid) / "state.yaml"
        text = path.read_text()
        assert re.search(r"^  source_path: \S+\n {4}\S", text, re.M), "the emitter must really fold the path"
        full = yaml.safe_load(text)["context"]["source_path"]
        assert full.endswith(f"{specs[0].uid}--menu plan for the talk")
        assert pi.read_state_fast(path)["context"]["source_path"] == full
        assert pi._cut_context(text) is None, "a folded value is not a clean cut"
        summaries = _parity(monkeypatch, tree, project)
        continuation = {s.uid: s for s in summaries}[specs[1].uid]
        assert continuation.source_rel == f"_runs/chat/{specs[0].uid}--menu plan for the talk"
        assert (f"continues [{specs[0].uid[:8]}](_runs/chat/{specs[0].uid}--menu%20plan%20for%20the%20talk/)"
                in pi.render_row(continuation))

    @pytest.mark.parametrize("text", [
        "schema: 2\ncontext:\n  source_path: a/_runs/x/b\n    c d\n  state: {}\nstatus: in-progress\n",
        "schema: 2\ncontext:\n  source_path: a/_runs/x/b\n   c\nstatus: in-progress\n",
        "schema: 2\ncontext:\n  source_path: a/_runs/x/b\n\n    c\nstatus: in-progress\n",
    ], ids=["four-space-continuation", "three-space-continuation", "blank-line-then-continuation"])
    def test_a_continued_source_path_line_takes_the_full_parse(self, tmp_path, text):
        path = tmp_path / "state.yaml"
        path.write_text(text)
        full = _full_parse(path)
        assert full is not None and full["context"]["source_path"] != "a/_runs/x/b", (
            "the probe's value must really continue past its first line")
        assert pi._cut_context(text) is None
        assert pi.read_state_fast(path) == full

    def test_a_source_path_that_is_not_a_scalar_reads_as_the_loader_reads_the_file(self, tmp_path):
        """`  source_path: a: b` puts a mapping where a scalar belongs; the loader
        rejects the file, so the cut must not read it as a readable record whose
        `source_path` is a mapping."""
        path = tmp_path / "state.yaml"
        path.write_text("schema: 2\nname: n\ncontext:\n  source_path: a: b\nstatus: in-progress\n")
        assert _full_parse(path) is None, "the probe must be a file the loader rejects"
        assert pi.read_state_fast(path) is None

    @pytest.mark.parametrize("raw,expected", [
        ("a: b", pi._INVALID), ("[a, b]", pi._INVALID), ("{a: b}", pi._INVALID),
        ("|", pi._INVALID), ("&a x", pi._INVALID), ("!!str x", pi._INVALID), ("'", pi._INVALID),
        ("plain/_runs/x", "plain/_runs/x"), ("'q'", "q"), ('"q"  # note', "q"), ("~", None),
        ("12", 12), ("true", True), ("2026-01-01", date(2026, 1, 1)),
        ("2026-01-01 10:00:00", datetime(2026, 1, 1, 10, 0)),
    ])
    def test_the_one_line_scalar_reader_accepts_only_a_scalar(self, raw, expected):
        assert pi._scalar(raw) == expected

    def test_a_quoted_top_level_scalar_holding_a_context_line_is_not_cut(self, tmp_path):
        """A hand-edited multi-line quoted `goal` can hold a column-0 `context:`
        line; the string opened before that line is what the loader reads, so
        the record takes the full parse rather than a cut that drops its middle."""
        path = tmp_path / "state.yaml"
        path.write_text('schema: 2\ngoal: "a\ncontext:\nb"\nstatus: in-progress\n')
        full = _full_parse(path)
        assert full["goal"] == "a context: b", "the probe must really fold the context line into the goal"
        assert pi._cut_context(path.read_text()) is None
        assert pi.read_state_fast(path) == full

# ---------------------------------------------------------------------------
# The tracked-or-staged filter over a real index
# ---------------------------------------------------------------------------


class TestTrackedRecordFilter:
    @pytest.fixture
    def checkout(self, tmp_path, monkeypatch):
        """A committed project with one tracked record and two untracked ones."""
        case = publication2(tmp_path, monkeypatch, projects={
            DEFAULT_PROJECT: [RecordSpec(uid_for(1), "chat", stamp(1), stamp(1, 1), name="tracked")],
        })
        case.tree.add(DEFAULT_PROJECT,
                      RecordSpec(uid_for(2), "chat", stamp(2), stamp(2, 1), name="untracked"),
                      RecordSpec(uid_for(3), "review", stamp(3), stamp(3, 1), name="staged-group"))
        return case

    def test_tracked_untracked_and_staged_under_a_path(self, checkout):
        include = pi.tracked_record_filter(checkout.root, DEFAULT_PROJECT, staged=["widgets/_runs/review"])
        assert include(f"widgets/_runs/chat/{uid_for(1)}")
        assert not include(f"widgets/_runs/chat/{uid_for(2)}")
        assert include(f"widgets/_runs/review/{uid_for(3)}")
        assert include("widgets/_runs/review")

    def test_a_newly_staged_record_counts_as_tracked(self, checkout):
        git(checkout.root, "add", "--", f"widgets/_runs/chat/{uid_for(2)}")
        include = pi.tracked_record_filter(checkout.root, DEFAULT_PROJECT)
        assert include(f"widgets/_runs/chat/{uid_for(2)}")

    def test_scan_project_applies_the_filter_with_the_project_prefix(self, checkout):
        seen = []

        def include(rel):
            seen.append(rel)
            return rel.endswith(uid_for(1))

        summaries = pi.scan_project(checkout.tree.project_dir(), include=include, project_rel="widgets")
        assert [s.uid for s in summaries] == [uid_for(1)]
        assert set(seen) == {f"widgets/_runs/chat/{uid_for(1)}", f"widgets/_runs/chat/{uid_for(2)}",
                             f"widgets/_runs/review/{uid_for(3)}"}

    @pytest.mark.parametrize("project_rel", ["widgets/", "./widgets", "/widgets/"])
    def test_scan_project_normalises_the_project_rel_it_prefixes(self, checkout, project_rel):
        """A trailing slash or a `./` prefix must not build keys the tracked set never holds."""
        seen = []

        def spy(rel):
            seen.append(rel)
            return True

        pi.scan_project(checkout.tree.project_dir(), include=spy, project_rel=project_rel)
        assert {rel.rsplit("/", 1)[0] for rel in seen} == {"widgets/_runs/chat", "widgets/_runs/review"}
        include = pi.tracked_record_filter(checkout.root, project_rel)
        summaries = pi.scan_project(checkout.tree.project_dir(), include=include, project_rel=project_rel)
        assert [s.uid for s in summaries] == [uid_for(1)]

    def test_a_git_error_makes_the_filter_permissive(self, tmp_path):
        plain = tmp_path / "not-a-repo"
        plain.mkdir()
        assert pi.tracked_record_filter(plain, DEFAULT_PROJECT)("widgets/_runs/chat/x")
        assert pi.tracked_record_filter(tmp_path / "missing", DEFAULT_PROJECT)("anything")

    def test_the_filter_runs_git_with_a_c_locale_and_a_timeout(self, checkout, monkeypatch):
        calls = []
        real_run = subprocess.run

        def spy(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return real_run(cmd, **kwargs)

        monkeypatch.setattr(pi.subprocess, "run", spy)
        pi.tracked_record_filter(checkout.root, DEFAULT_PROJECT)
        assert len(calls) == 1
        cmd, kwargs = calls[0]
        assert cmd[:3] == ["git", "ls-files", "-z"]
        # The live run area and C's archive home, one invocation.
        assert cmd[-2:] == ["widgets/_runs", "widgets/_archive/_runs"]
        assert kwargs["env"]["LC_ALL"] == "C" and kwargs["timeout"] == pi.GIT_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# README readers and writers
# ---------------------------------------------------------------------------


class TestProjectDirRefusal:
    """The write boundary both index writers share, unit by unit; the writers'
    own tests cover what each does with a refusal."""

    @pytest.fixture
    def root(self, tmp_path):
        root = tmp_path / "plexus"
        (root / "widgets" / "_runs" / "chat" / uid_for(1)).mkdir(parents=True)
        (root / "group" / "gadgets").mkdir(parents=True)
        (tmp_path / "outside").mkdir()
        return root

    def test_a_real_project_directory_is_accepted(self, root):
        assert pi.project_dir_refusal(root, "widgets") is None
        assert pi.project_dir_refusal(root, "group/gadgets") is None

    def test_a_root_reached_through_a_symlink_is_accepted(self, root, tmp_path):
        (tmp_path / "via").symlink_to(root)
        assert pi.project_dir_refusal(tmp_path / "via", "widgets") is None

    def test_an_alias_escaping_the_root_is_refused_before_the_symlink_reason(self, root):
        (root / "alias").symlink_to(root.parent / "outside")
        assert pi.project_dir_refusal(root, "alias") == "project directory resolves outside the work repo"

    def test_an_alias_into_a_record_is_refused_as_inside_a_record(self, root):
        (root / "alias").symlink_to(root / "widgets" / "_runs" / "chat" / uid_for(1))
        assert pi.project_dir_refusal(root, "alias") == "project directory lies inside a _runs/ record"
        assert pi.project_dir_refusal(root, f"widgets/_runs/chat/{uid_for(1)}") == (
            "project directory lies inside a _runs/ record"
        )

    def test_an_alias_inside_the_root_is_refused_as_a_symlink(self, root):
        (root / "alias").symlink_to(root / "widgets")
        assert pi.project_dir_refusal(root, "alias") == "project directory is a symlink"

    def test_a_directory_below_an_aliased_level_is_refused(self, root):
        (root / "linked").symlink_to(root / "group")
        assert pi.project_dir_refusal(root, "linked/gadgets") == "project directory lies below a symlink"

    def test_a_symlink_loop_is_refused_without_naming_machine_paths(self, root):
        # Non-strict resolve() returns a loop unresolved on current Pythons,
        # so the symlink check refuses it; an interpreter that raises instead
        # gets "cannot be resolved". Either way: refused, no path in the reason.
        (root / "loop").symlink_to(root / "loop")
        reason = pi.project_dir_refusal(root, "loop")
        assert reason in ("project directory is a symlink", "project directory cannot be resolved")
        assert str(root) not in reason


class TestWriteProjectReadme:
    def test_a_missing_readme_is_created_with_the_heading(self, tree):
        tree.add(DEFAULT_PROJECT, RecordSpec(uid_for(1), "chat", stamp(1), stamp(1, 1), name="one"))
        changed, previous = pi.write_project_readme(tree.project_dir(), heading="widgets")
        assert (changed, previous) == (True, None)
        text = tree.readme().read_text()
        assert text.startswith("# widgets\n\n" + START_MARKER) and text.endswith(END_MARKER + "\n")
        assert text == pi.render_project_readme(tree.project_dir(), heading="widgets")

    def test_an_unchanged_render_writes_nothing_and_returns_the_bytes(self, tree):
        pi.write_project_readme(tree.project_dir(), heading="widgets")
        before = tree.readme().read_bytes()
        stat = tree.readme().stat()
        assert pi.write_project_readme(tree.project_dir(), heading="widgets") == (False, before)
        assert tree.readme().stat().st_ino == stat.st_ino

    def test_hand_text_around_the_block_is_preserved_byte_for_byte(self, tree):
        tree.readme().write_bytes(b"# Widgets\r\n\r\nHand prose.\r\n")
        changed, previous = pi.write_project_readme(tree.project_dir(), heading="x")
        assert changed and previous == b"# Widgets\r\n\r\nHand prose.\r\n"
        appended = tree.readme().read_bytes()
        assert appended.startswith(b"# Widgets\r\n\r\nHand prose.\r\n\n" + START_MARKER.encode())
        tree.add(DEFAULT_PROJECT, RecordSpec(uid_for(1), "chat", stamp(1), stamp(1, 1), name="one"))
        tree.readme().write_bytes(appended + b"\r\npost\r\n")
        changed, previous = pi.write_project_readme(tree.project_dir(), heading="x")
        assert changed and previous == appended + b"\r\npost\r\n"
        after = tree.readme().read_bytes()
        assert after.startswith(b"# Widgets\r\n\r\nHand prose.\r\n\n" + START_MARKER.encode())
        assert after.endswith(END_MARKER.encode() + b"\n\r\npost\r\n")
        assert b"[one](" in after

    def test_the_write_is_atomic_and_keeps_the_file_mode(self, tree):
        tree.readme().write_text("# Widgets\n")
        tree.readme().chmod(0o640)
        pi.write_project_readme(tree.project_dir(), heading="x")
        assert oct(tree.readme().stat().st_mode & 0o777) == oct(0o640)
        assert [p.name for p in tree.project_dir().iterdir() if p.name.startswith(".README")] == []

    def test_a_symlink_readme_is_refused(self, tree, tmp_path):
        target = tmp_path / "elsewhere.md"
        target.write_text("# elsewhere\n")
        tree.readme().symlink_to(target)
        with pytest.raises(IndexSkipped, match="symlink"):
            pi.write_project_readme(tree.project_dir(), heading="x")
        assert target.read_text() == "# elsewhere\n"

    def test_a_directory_readme_is_refused(self, tree):
        tree.readme().mkdir()
        with pytest.raises(IndexSkipped, match="directory"):
            pi.write_project_readme(tree.project_dir(), heading="x")

    def test_malformed_markers_are_refused(self, tree):
        tree.readme().write_text(f"# W\n{START_MARKER}\nno end\n")
        with pytest.raises(IndexSkipped, match="malformed"):
            pi.write_project_readme(tree.project_dir(), heading="x")
        with pytest.raises(MalformedMarkers):
            pi.render_project_readme(tree.project_dir(), heading="x")

    def test_a_newer_format_block_is_left_alone(self, tree):
        newer = BLOCK.replace("format 1)", "format 2)")
        tree.readme().write_text(f"# W\n\n{newer}\n")
        with pytest.raises(IndexSkipped, match="newer"):
            pi.write_project_readme(tree.project_dir(), heading="x")
        assert tree.readme().read_text() == f"# W\n\n{newer}\n"

    def test_a_hand_text_marker_line_is_refused_with_the_gates_hint(self, tree):
        tree.readme().write_text("# W\n\nSetext\n=======\n")
        with pytest.raises(IndexSkipped, match="indent or lengthen"):
            pi.write_project_readme(tree.project_dir(), heading="x")
        assert tree.readme().read_text() == "# W\n\nSetext\n=======\n"

    def test_a_readme_that_is_not_utf8_is_refused(self, tree):
        tree.readme().write_bytes(b"# W\n\xff\xfe\n")
        with pytest.raises(IndexSkipped, match="UTF-8"):
            pi.write_project_readme(tree.project_dir(), heading="x")

    def test_an_unreadable_readme_is_refused_not_raised(self, tree):
        if os.geteuid() == 0:
            pytest.skip("root reads every file; the permission probe cannot fail")
        tree.readme().write_text("# W\n")
        tree.readme().chmod(0)
        try:
            with pytest.raises(IndexSkipped, match="not readable"):
                pi.write_project_readme(tree.project_dir(), heading="x")
            with pytest.raises(IndexSkipped, match="not readable"):
                pi.render_project_readme(tree.project_dir(), heading="x")
        finally:
            tree.readme().chmod(0o644)
        assert tree.readme().read_text() == "# W\n"

    def test_the_writer_honours_the_include_predicate(self, tree):
        tree.add(DEFAULT_PROJECT,
                 RecordSpec(uid_for(1), "chat", stamp(1), stamp(1, 1), name="kept"),
                 RecordSpec(uid_for(2), "chat", stamp(2), stamp(2, 1), name="dropped"))
        pi.write_project_readme(tree.project_dir(), heading="x", project_rel="widgets",
                                include=lambda rel: rel.endswith(uid_for(1)))
        text = tree.readme().read_text()
        assert "[kept](" in text and "dropped" not in text

    def test_nothing_under_runs_is_ever_written(self, tree):
        tree.add(DEFAULT_PROJECT, RecordSpec(uid_for(1), "chat", stamp(1), stamp(1, 1), log_message="m"))
        runs = tree.project_dir() / "_runs"
        before = {p: p.read_bytes() for p in runs.rglob("*") if p.is_file()}
        pi.write_project_readme(tree.project_dir(), heading="x")
        assert {p: p.read_bytes() for p in runs.rglob("*") if p.is_file()} == before


class TestRootBlock:
    def test_the_example_manifest_renders_in_manifest_order_with_the_constant_legend(self):
        block = pi.render_root_block(EXAMPLE_MANIFEST)
        rows = [line for line in block.splitlines() if line.startswith("| [")]
        assert rows == [
            "| [gadgets-api](gadgets-api/) | `git.example.test/acme/gadgets-api` | — |",
            "| [gadgets](gadgets/) | `git.example.test/alice/gadgets` | — |",
            "| [work-notes](work-notes/) | `git.example.test/alice/work-notes` | — |",
            "| [ballots](ballots/) | `git.example.test/example-org/ballots` | — |",
        ]
        assert "## Projects" in block and pi.ROOT_LEGEND in block
        assert f"(lmer index format {FORMAT})" in block and pi.block_format(block) == FORMAT
        assert block.startswith(START_MARKER + "\n\n") and block.endswith("\n\n" + END_MARKER)

    def test_the_example_manifest_names_only_example_test_repositories(self):
        """This module ships on the public mirror: the manifest standing in for
        a live one is synthetic, so every repository it names is under
        `example.test`."""
        repos = [project["repo"] for project in EXAMPLE_MANIFEST["projects"].values()]
        assert len(repos) == 4
        assert [repo for repo in repos if not repo.startswith("git.example.test/")] == []

    def test_a_repoless_project_shows_its_description_and_no_repository(self):
        block = pi.render_root_block({"projects": {"q3": {"description": "Q3 planning | notes"}}})
        assert "| [q3](q3/) | — | Q3 planning \\| notes |" in block

    def test_the_legend_is_a_constant_string(self):
        assert isinstance(pi.ROOT_LEGEND, str) and "docs/SCHEMA.md" in pi.ROOT_LEGEND
        assert pi.render_root_block(EXAMPLE_MANIFEST) == pi.render_root_block(dict(EXAMPLE_MANIFEST))

    def test_no_root_line_can_be_a_conflict_marker(self):
        hostile = {"projects": {"p\n=======": {"repo": "r\n|||||||", "description": "d\n>>>>>>>"}}}
        block = pi.render_root_block(hostile)
        tame = pi.render_root_block({"projects": {"p": {"repo": "r", "description": "d"}}})
        assert len(block.splitlines()) == len(tame.splitlines()), "a hostile manifest is still one line per row"
        assert pi.MARKER_LINE_RE.search(block) is None

    def test_the_root_readme_is_created_from_the_instance_name_or_spliced(self, tmp_path):
        root = tmp_path / "plexus"
        root.mkdir()
        created = pi.render_root_readme(root, EXAMPLE_MANIFEST)
        assert created.startswith("# example-work\n\n" + START_MARKER)
        (root / "README.md").write_text("# Alice's work repo\n\nPreamble.\n")
        spliced = pi.render_root_readme(root, EXAMPLE_MANIFEST)
        assert spliced == "# Alice's work repo\n\nPreamble.\n\n" + pi.render_root_block(EXAMPLE_MANIFEST) + "\n"

    def test_a_hostile_instance_name_cannot_plant_a_marker_in_the_root_readme(self, tmp_path):
        root = tmp_path / "plexus"
        root.mkdir()
        text = pi.render_root_readme(root, {**EXAMPLE_MANIFEST, "name": "p\n======="})
        assert text.startswith("# p =======\n\n" + START_MARKER)
        assert not pi.hand_text_has_marker(text)


# ---------------------------------------------------------------------------
# Purity, size and timing budgets
# ---------------------------------------------------------------------------


class TestLegendText:
    """The legend sentences are pinned literally: a wording change makes every
    published block stale, so it must be a conscious FORMAT bump — and the
    literals carry the token, so neither can move without the other."""

    def test_the_project_legend_is_the_specified_sentence(self):
        assert pi.PROJECT_LEGEND == (
            "One row per execution record under `_runs/`, newest first — rendered by lmer from each "
            "record's own `state.yaml`, `events.jsonl` and `log.yaml` on every work-repo push and by "
            "`lmer-schema index --write` (lmer index format 1). States: **closed** = status complete "
            "or archived · **waiting on question** = stopped on a recorded question · **seeded** = "
            "a `work seed` record nobody has run in yet · **ended, not closed** = in progress with "
            "a recorded session end · **open** = in progress with no "
            "sign-out recorded; the last-activity column is the date the record last changed. "
            "Nothing here ever ends a run. Edit this file outside the markers only."
        )

    def test_the_root_intro_is_the_specified_sentence(self):
        assert pi.ROOT_INTRO == (
            "Generated from `_plexus.yaml` by `lmer-schema index --write` (lmer index format 1). "
            "Each project directory shows its own run index below its file list. Edit this file "
            "outside the markers only."
        )

    def test_the_root_legend_is_the_specified_name_list(self):
        assert pi.ROOT_LEGEND == (
            "Other names at this level: `_plexus.yaml`, `_agents/`, `_knowledge/`, `_napkin/`, "
            "`_taskdef/`, `_archive/`, `_memory_retirements.json`, `napkin/`, `taskdef/`, "
            "`agent-files/`, `mirrors/`, `knowledge/` — see lmer `docs/SCHEMA.md`; everything else "
            "is human content."
        )


#: Every spelling of "what time is it" (and the mtime conversion) a renderer must not use.
CLOCK_CALL = re.compile(
    r"\b(?:datetime\.now|datetime\.today|utcnow|date\.today|fromtimestamp|time\.time"
    r"|localtime|gmtime|monotonic|perf_counter)\("
)
ENVIRONMENT_NAMES = frozenset({"environ", "environb", "getenv", "putenv"})


def _environment_users(tree: ast.AST) -> set[str]:
    """The enclosing function — or ``<module>`` — of every environment access."""
    users: set[str] = set()

    def visit(node: ast.AST, scope: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = node.name
        if isinstance(node, ast.Attribute) and node.attr in ENVIRONMENT_NAMES:
            users.add(scope)
        elif isinstance(node, ast.Name) and node.id in ENVIRONMENT_NAMES:
            users.add(scope)
        elif isinstance(node, ast.ImportFrom) and node.module == "os" and any(
                alias.name in ENVIRONMENT_NAMES for alias in node.names):
            users.add(scope)
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(tree, "<module>")
    return users


class TestKernelPurity:
    def test_the_kernel_imports_nothing_from_lmer_schema(self):
        tree = ast.parse(KERNEL_SOURCE.read_text())
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        assert not [name for name in imported if name.startswith("lmer_schema")]

    def test_the_kernel_reads_no_clock_and_the_environment_only_for_git(self):
        source = KERNEL_SOURCE.read_text()
        assert CLOCK_CALL.search(source) is None
        assert _environment_users(ast.parse(source)) == {"tracked_record_filter"}

    def test_the_purity_scan_catches_what_it_claims_to_catch(self):
        planted = (
            "import os\n"
            "import time\n"
            "from datetime import datetime\n"
            "from os import environ\n"
            "HOME = os.environ['HOME']\n"
            "def f():\n"
            "    return os.getenv('X'), datetime.today(), time.localtime()\n"
        )
        assert _environment_users(ast.parse(planted)) == {"<module>", "f"}
        assert sorted(CLOCK_CALL.findall(planted)) == ["datetime.today(", "localtime("]

    def test_two_thousand_rows_stay_under_a_megabyte(self):
        summaries = [
            _summary(uid=uid_for(i), dir_name=uid_for(i), dir_rel=f"_runs/chat/{uid_for(i)}",
                     name=None, goal_line=f"Goal number {i} with a reasonably long sentence to size it",
                     source_uid=uid_for(i - 1), source_rel=f"_runs/chat/{uid_for(i - 1)}",
                     phase="implement", started_at=stamp(1 + i % 28, i % 24))
            for i in range(2000)
        ]
        block = pi.render_project_block(summaries)
        assert len(_rows(block)) == 2000
        assert len(block.encode("utf-8")) < 1_000_000


class TestBudgets:
    def test_a_thousand_flat_records_render_inside_the_budget(self, tmp_path):
        specs = [
            RecordSpec(uid_for(i, "flat"), ("chat", "review", "develop")[i % 3],
                       stamp(1 + i % 28, i % 24, i % 60), stamp(1 + i % 28, i % 24, i % 60),
                       name=f"run-{i}" if i % 3 == 0 else None,
                       goal=f"Goal {i}" if i % 3 == 1 else None,
                       log_message=f"Log {i}" if i % 3 == 2 else None,
                       signed_out=(True, False, None)[i % 3])
            for i in range(1000)
        ]
        tree = build_schema2_plexus(tmp_path / "plexus", {DEFAULT_PROJECT: specs})
        started = time.perf_counter()
        block = pi.render_project_block(pi.scan_project(tree.project_dir()))
        elapsed = time.perf_counter() - started
        assert len(_rows(block)) == 1000
        assert elapsed < timing.INDEX_FLAT, f"{elapsed:.2f}s"

    def test_ten_twenty_deep_chains_render_inside_the_budget(self, tmp_path):
        tree = build_schema2_plexus(tmp_path / "plexus")
        for index in range(10):
            # Half the chains open a bracket in every goal: prose must not cost the cut.
            goal = "[WIP chain goal" if index % 2 else "chain goal"
            chain(tree, DEFAULT_PROJECT, 20, slug=f"chat-{index}", salt=f"chain-{index}", goal=goal)
        deepest = max(p.stat().st_size for p in tree.project_dir().rglob("state.yaml"))
        assert deepest > 50_000, "the deepest record must really nest its whole chain"
        started = time.perf_counter()
        block = pi.render_project_block(pi.scan_project(tree.project_dir()))
        elapsed = time.perf_counter() - started
        assert len(_rows(block)) == 200
        assert elapsed < timing.INDEX_CHAINS, f"{elapsed:.2f}s"


# ---------------------------------------------------------------------------
# The shared fixture builder itself
# ---------------------------------------------------------------------------


class TestSchema2Fixtures:
    def test_make_env_points_the_work_cli_at_the_tree(self, tree, monkeypatch):
        from work_repo import run_state

        make_env(monkeypatch, tree.root, DEFAULT_PROJECT)
        assert os.environ["LMER_WORK_REPO_PATH"] == str(tree.root)
        assert os.environ["LMER_REPO_HOST"] == "git.example.test"
        assert os.environ["LMER_REPO_PROJECT"] == "acme/widgets"
        assert os.environ["LMER_TASK_TARGET"] == ""
        assert run_state.runs_base() == tree.project_dir() / "_runs"
        assert run_state.derive_slug() == "chat"

    def test_a_chain_record_nests_its_source_the_way_the_kernel_does(self, tree):
        specs = chain(tree, DEFAULT_PROJECT, 3)
        state = yaml.safe_load((tree.record_dir(DEFAULT_PROJECT, specs[2].uid) / "state.yaml").read_text())
        assert state["session"]["source_session_uid"] == specs[1].uid
        assert state["context"]["source_path"] == f"widgets/_runs/chat/{specs[1].uid}"
        assert state["context"]["state"]["session"]["uid"] == specs[1].uid
        assert state["context"]["state"]["context"]["state"]["session"]["uid"] == specs[0].uid
        assert len(state["context"]["events"]) == 25
        assert state["context"]["state"]["context"]["events"][-1]["type"] == "session_end"

    def test_records_are_written_by_the_production_emitter_call(self, tree):
        """`run_state.write_state` writes `yaml.safe_dump(state, sort_keys=False)`; the
        fixture makes the same call over the same document shape (`context` nests
        the source's parsed state and its last 25 events), so every chain test runs
        over the bytes a live chain has: indentless sequences, `\\x`/`\\u` escapes,
        width-80 folds — and a record round-trips through the emitter unchanged."""
        specs = chain(tree, DEFAULT_PROJECT, 3, goal="caf\u00e9 \u2014 " + ("long words " * 12).strip())
        for spec in specs:
            text = (tree.record_dir(DEFAULT_PROJECT, spec.uid) / "state.yaml").read_text()
            assert text == yaml.safe_dump(yaml.safe_load(text), sort_keys=False)
            assert "&id" not in text, "no object may be shared inside one document"
        deepest = (tree.record_dir(DEFAULT_PROJECT, specs[-1].uid) / "state.yaml").read_text()
        assert "\n  events:\n  - ts: " in deepest and "\\xE9 \\u2014" in deepest and "\\\n" in deepest
        assert list(yaml.safe_load(deepest)["context"]) == [
            "source_session_uid", "source_path", "state_sha256", "state", "events",
            "checkpoints", "unsigned_source_acknowledged"]

    def test_publication2_builds_a_pushed_tree_and_clean_peers(self, tmp_path, monkeypatch):
        case = publication2(tmp_path, monkeypatch, clones=3, projects={
            DEFAULT_PROJECT: [RecordSpec(uid_for(1), "chat", stamp(1), stamp(1, 1), name="seeded")],
        })
        assert len(case.clones) == 3
        for repo in (case.root, *case.clones):
            assert git(repo, "status", "--porcelain").stdout == ""
            assert (repo / "_plexus.yaml").is_file()
            assert (repo / "widgets" / "_runs" / "chat" / uid_for(1) / "state.yaml").is_file()
        assert case.origin_file("_plexus.yaml") == case.tree.manifest.read_text()
        assert case.origin_file("widgets/README.md") is None
        assert os.environ["GIT_CONFIG_GLOBAL"] == "/dev/null"

    def test_publication2_shields_its_repositories_from_the_global_git_config(self, tmp_path, monkeypatch):
        """A developer's global config (a hooks path, gpgsign) never reaches the fixture's git."""
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        (hooks / "pre-commit").write_text("#!/bin/sh\nexit 1\n")
        (hooks / "pre-commit").chmod(0o755)
        hostile = tmp_path / "gitconfig"
        hostile.write_text(f"[core]\n\thooksPath = {hooks}\n")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile))
        probe = tmp_path / "probe"
        probe.mkdir()
        git(probe, "init", "-q", "--initial-branch=main")
        git(probe, "config", "user.name", "Test User")
        git(probe, "config", "user.email", "test@example.com")
        (probe / "f").write_text("x\n")
        git(probe, "add", "--", "f")
        assert git(probe, "commit", "-q", "-m", "probe", check=False).returncode != 0, (
            "the hostile global config must really refuse a plain commit")
        case = publication2(tmp_path, monkeypatch)
        assert case.origin_file("_plexus.yaml") is not None
        assert os.environ["GIT_CONFIG_GLOBAL"] == "/dev/null"
        with pytest.raises(TypeError):
            publication2(tmp_path / "again")  # type: ignore[call-arg]  # monkeypatch is required

    def test_make_env_keeps_the_gate_guard_publication2_disabled(self, tmp_path, monkeypatch):
        """`make_env` strips every LMER_* variable; the one `publication2` set to
        keep durability commits from deferring behind a live gate must survive."""
        from lmer_cli import gate_lock

        case = publication2(tmp_path, monkeypatch)
        assert os.environ[gate_lock.GUARD_ENV] == "0"
        monkeypatch.setenv("LMER_SESSION_ID", "stale-context")
        make_env(monkeypatch, case.root)
        assert "LMER_SESSION_ID" not in os.environ, "the strip must still run"
        assert os.environ[gate_lock.GUARD_ENV] == "0"
        assert os.environ["GIT_CONFIG_GLOBAL"] == "/dev/null"
