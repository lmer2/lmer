"""Usage adapters count each API response once, per model, per agent (#315, #647)."""

from __future__ import annotations

import ast
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lmer_platform import transcripts
from lmer_usage import buckets, claude, shape
from lmer_usage import pi as pi_adapter
from lmer_usage.summarise import summarise

FIXTURES = Path(__file__).parent / "fixtures" / "usage"
CLAUDE_SESSION = FIXTURES / "claude" / "session-a"


def _claude_files():
    return [
        CLAUDE_SESSION / "main.jsonl",
        CLAUDE_SESSION / "resumed.jsonl",
        CLAUDE_SESSION / "subagents" / "agent-abc123.jsonl",
        CLAUDE_SESSION / "subagents" / "agent-def456.jsonl",
    ]


def _row(summary, model, agent=None, harness="claude"):
    return summary.rows[(harness, model, agent)]


class TestBuckets:
    @pytest.mark.parametrize("value", [True, False, 1.0, -1, 2**53, "3", [1]])
    def test_count_rejects_non_counts(self, value):
        assert buckets.count(value) is None

    @pytest.mark.parametrize("value,expected", [(None, 0), (0, 0), (7, 7), (2**53 - 1, 2**53 - 1)])
    def test_count_accepts_counts(self, value, expected):
        assert buckets.count(value) == expected

    def test_make_refuses_one_bad_field(self):
        assert buckets.make(input=1, output=-2) is None

    def test_thinking_is_clamped_to_output(self):
        assert buckets.make(output=5, thinking=9)["thinking"] == 5

    def test_add_saturates(self):
        into = buckets.make(output=buckets.MAX_COUNT)
        buckets.add(into, buckets.make(output=10))
        assert into["output"] == buckets.MAX_COUNT


class TestClaudeAdapter:
    def test_a_response_split_over_records_counts_once(self):
        summary = summarise([CLAUDE_SESSION / "main.jsonl"])
        opus = _row(summary, "claude-opus-5-5")
        # msg_1 is written as three records; naive summation would count it thrice.
        naive_output = 0
        for line in (CLAUDE_SESSION / "main.jsonl").read_text().splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            usage = (record.get("message") or {}).get("usage") or {}
            naive_output += usage.get("output_tokens", 0)
        assert opus["output"] == 50 + 7 + 11
        assert naive_output > opus["output"] + 30
        assert opus["requests"] == 3

    def test_synthetic_no_usage_and_all_zero_are_excluded(self):
        summary = summarise([CLAUDE_SESSION / "main.jsonl"])
        assert not any(model == claude.SYNTHETIC_MODEL for _, model, _ in summary.rows)
        assert summary.totals["requests"] == 4  # msg_1, msg_2, msg_3, msg_4

    def test_model_switch_is_attributed_per_model(self):
        summary = summarise([CLAUDE_SESSION / "main.jsonl"])
        sonnet = _row(summary, "claude-sonnet-5-5")
        assert (sonnet["input"], sonnet["cache_read"], sonnet["output"], sonnet["requests"]) == (5, 500, 30, 1)

    def test_subagents_are_rows_of_their_own(self):
        summary = summarise(_claude_files())
        explore = _row(summary, "claude-haiku-5-5", agent="Explore")
        unnamed = _row(summary, "claude-haiku-5-5", agent="def456")
        assert (explore["input"], explore["cache_create"], explore["output"]) == (4, 10, 9)
        assert unnamed["output"] == 2

    def test_dedup_is_per_session_across_files(self):
        summary = summarise(_claude_files())
        opus = _row(summary, "claude-opus-5-5")
        # resumed.jsonl repeats msg_1 and adds one response keyed by requestId.
        assert opus["requests"] == 4
        assert (opus["input"], opus["cache_create"], opus["cache_read"], opus["output"], opus["thinking"]) == (
            16, 140, 4900, 81, 23,
        )

    def test_totals_days_and_wall_clock(self):
        summary = summarise(_claude_files(), idle_seconds=300)
        assert summary.totals == {
            "requests": 7, "input": 26, "cache_create": 150, "cache_read": 5700,
            "output": 122, "thinking": 23,
        }
        assert sorted(summary.days) == ["2026-11-05", "2026-11-06"]
        assert summary.days["2026-11-05"]["input"] == 21
        assert summary.days["2026-11-06"]["input"] == 5
        for name in buckets.COUNTERS:
            assert sum(day[name] for day in summary.days.values()) == summary.totals[name]
        assert summary.started_at == datetime(2026, 11, 5, 10, 0, 0, tzinfo=timezone.utc)
        assert summary.ended_at == datetime(2026, 11, 6, 0, 5, 0, tzinfo=timezone.utc)
        assert summary.active_seconds == 60 + 10 + 270

    def test_idle_threshold_comes_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("LMER_USAGE_IDLE_SECONDS", "4000")
        summary = summarise(_claude_files())
        assert summary.active_seconds > 3540

    def test_summarise_is_idempotent(self):
        first = summarise(_claude_files())
        second = summarise(_claude_files())
        assert first.rows == second.rows and first.days == second.days

    def test_a_partially_written_last_line_is_skipped(self, tmp_path):
        path = tmp_path / "s.jsonl"
        complete = {"type": "assistant", "timestamp": "2026-11-05T10:00:00Z", "uuid": "u1",
                    "message": {"id": "m1", "model": "claude-opus-5-5", "usage": {"output_tokens": 5}}}
        partial = dict(complete, uuid="u2", message=dict(complete["message"], id="m2"))
        path.write_text(json.dumps(complete) + "\n" + json.dumps(partial))
        assert summarise([path]).totals["requests"] == 1

    def test_invalid_counts_drop_the_response(self, tmp_path):
        path = tmp_path / "s.jsonl"
        record = {"type": "assistant", "timestamp": "2026-11-05T10:00:00Z", "uuid": "u",
                  "message": {"id": "m", "model": "claude-opus-5-5",
                              "usage": {"input_tokens": True, "output_tokens": 5}}}
        path.write_text(json.dumps(record) + "\n")
        assert summarise([path]).totals["requests"] == 0


class TestFileSafety:
    def test_meta_symlink_is_not_followed(self, tmp_path):
        sub = tmp_path / "subagents"
        sub.mkdir()
        target = tmp_path / "elsewhere.json"
        target.write_text(json.dumps({"agentType": "Planted"}))
        (sub / "agent-x1.meta.json").symlink_to(target)
        assert claude.agent_for_file(sub / "agent-x1.jsonl") == "x1"

    def test_meta_fifo_does_not_block(self, tmp_path):
        sub = tmp_path / "subagents"
        sub.mkdir()
        os.mkfifo(sub / "agent-x2.meta.json")
        assert claude.agent_for_file(sub / "agent-x2.jsonl") == "x2"

    def test_transcript_symlink_is_refused(self, tmp_path):
        link = tmp_path / "link.jsonl"
        link.symlink_to(CLAUDE_SESSION / "main.jsonl")
        summary = summarise([link])
        assert summary.totals["requests"] == 0
        assert summary.not_collected and "unreadable" in summary.not_collected[0]["reason"]


class TestDispatch:
    def test_the_transcript_view_uses_the_shared_rule(self):
        assert transcripts._harness_of_record is shape.harness_of_record
        assert transcripts._PI_RECORD_TYPES is shape.PI_RECORD_TYPES

    @pytest.mark.parametrize("kind", ["usage", "compaction", "branch_summary"])
    def test_pi_spend_records_belong_to_pi(self, kind):
        assert shape.harness_of_record({"type": kind}) == "pi"

    def test_record_type_sets_are_disjoint(self):
        sets = [shape.CLAUDE_RECORD_TYPES, shape.PI_RECORD_TYPES, shape.CODEX_RECORD_TYPES]
        for i, a in enumerate(sets):
            for b in sets[i + 1:]:
                assert not a & b

    def test_drop_in_transcript_is_not_collected(self, tmp_path):
        path = tmp_path / "s.jsonl"
        path.write_text(json.dumps({"type": "lmer.message", "role": "assistant", "text": "x"}) + "\n")
        summary = summarise([path])
        assert summary.totals == buckets.zero()
        assert summary.not_collected[0]["reason"].startswith("drop-in harness")


class TestNeutralPackage:
    def test_lmer_usage_imports_neither_consumer(self):
        root = Path(__file__).resolve().parents[1] / "src" / "lmer_usage"
        for module in root.glob("*.py"):
            tree = ast.parse(module.read_text())
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    names = [node.module]
                for name in names:
                    assert not name.startswith(("lmer_platform", "work_repo")), (module.name, name)


CODEX = FIXTURES / "codex"
PI = FIXTURES / "pi"


class TestCodexAdapter:
    def test_codex_deltas_are_attributed_to_the_turn_model(self):
        summary = summarise([CODEX / "rollout-main.jsonl"])
        main = _row(summary, "gpt-5.5-codex", harness="codex")
        mini = _row(summary, "gpt-5.5-mini", harness="codex")
        assert (main["input"], main["cache_create"], main["cache_read"], main["output"], main["thinking"],
                main["requests"]) == (400, 100, 2500, 250, 90, 2)
        assert (mini["input"], mini["cache_read"], mini["output"], mini["thinking"], mini["requests"]) == (
            300, 600, 80, 15, 2,
        )

    def test_codex_repeated_snapshot_and_empty_info_are_ignored(self):
        summary = summarise([CODEX / "rollout-main.jsonl"])
        assert summary.totals["requests"] == 4

    def test_codex_reset_lower_is_a_baseline_not_a_delta(self):
        summary = summarise([CODEX / "rollout-main.jsonl"])
        # After the reset to (200, 100, ...) only the next increase (300 input) counts.
        mini = _row(summary, "gpt-5.5-mini", harness="codex")
        assert mini["input"] == 200 + 100

    def test_codex_fork_first_snapshot_is_a_baseline(self):
        summary = summarise([CODEX / "rollout-fork.jsonl"])
        row = _row(summary, "gpt-5.5-codex", harness="codex")
        assert (row["input"], row["cache_read"], row["output"], row["requests"]) == (100, 300, 40, 1)

    def test_codex_copied_history_dedupes_across_the_session(self):
        alone = summarise([CODEX / "rollout-main.jsonl"])
        with_copy = summarise([CODEX / "rollout-main.jsonl", CODEX / "rollout-copied.jsonl"])
        assert with_copy.rows == alone.rows
        everything = summarise(sorted(CODEX.glob("*.jsonl")))
        assert everything.totals["requests"] == 5

    def test_codex_buckets_partition_input(self):
        summary = summarise(sorted(CODEX.glob("*.jsonl")))
        for row in summary.rows.values():
            assert row["thinking"] <= row["output"]
        main = _row(summary, "gpt-5.5-codex", harness="codex")
        # cached + cache-write + uncached input == the input_tokens deltas (1000 + 2000 + 400).
        assert main["input"] + main["cache_read"] + main["cache_create"] == 3400


class TestPiAdapter:
    def test_pi_every_branch_of_the_tree_counts(self):
        summary = summarise([PI / "session-tree.jsonl"])
        sonnet = _row(summary, "claude-sonnet-5-5", harness="pi")
        dated = _row(summary, "claude-sonnet-5-5-20261001", harness="pi")
        assert (sonnet["input"], sonnet["cache_create"], sonnet["cache_read"], sonnet["output"],
                sonnet["thinking"], sonnet["requests"]) == (16, 50, 4100, 141, 30, 3)
        assert (dated["input"], dated["output"], dated["requests"]) == (6, 45, 1)

    def test_pi_cache_warm_tool_and_summary_spend_counts(self):
        summary = summarise([PI / "session-tree.jsonl"])
        summaries = _row(summary, pi_adapter.SUMMARY_MODEL, harness="pi")
        assert (summaries["input"], summaries["output"], summaries["requests"]) == (4300, 320, 2)
        assert summary.totals["requests"] == 6

    def test_pi_cost_is_kept_as_harness_reported(self):
        summary = summarise([PI / "session-tree.jsonl"])
        assert summary.reported_cost == {"USD": pytest.approx(0.0426)}

    def test_pi_fork_dedupes_copied_entries(self):
        summary = summarise(sorted(PI.glob("*.jsonl")))
        sonnet = _row(summary, "claude-sonnet-5-5", harness="pi")
        assert sonnet["requests"] == 4
        assert summary.reported_cost == {"USD": pytest.approx(0.0496)}


class TestUnknownHarness:
    def test_a_record_shape_nobody_claims_is_not_counted_as_zero_usage(self, tmp_path):
        path = tmp_path / "other.jsonl"
        path.write_text(json.dumps({"type": "lmer.meta", "format": 1, "harness": "opencode"}) + "\n")
        summary = summarise([path])
        assert summary.not_collected and summary.totals == buckets.zero()


class TestMixedSessions:
    def test_codex_and_pi_files_in_one_call_keep_their_harness(self):
        summary = summarise([CODEX / "rollout-main.jsonl", PI / "session-tree.jsonl"])
        assert summary.harnesses == {"codex", "pi"}
