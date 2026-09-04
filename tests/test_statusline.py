"""Tests for the lmer status line renderer (hooks/statusline.py, issue #106)."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from hooks.statusline import (
    DEFAULT_SEGMENTS,
    DEFAULT_WORKSPACE,
    STATUS_FILENAME,
    STATUS_LIMIT_CAP,
    STATUS_SCHEMA,
    build_line,
    context_percent,
    cost_text,
    ctx_text,
    duration_text,
    effort_level,
    effort_text,
    limit_keys,
    limit_percent,
    limit_resets_at,
    lines_text,
    model_name,
    parse_segments,
    payload_cwd,
    publish_if_mounted,
    publish_status,
    rate_limit_text,
    status_document,
)

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "hooks" / "statusline.py"


def _payload(*, cwd=None, window=None, model="Fable"):
    payload = {"model": {"display_name": model}}
    if cwd is not None:
        payload["workspace"] = {"current_dir": str(cwd)}
    if window is not None:
        payload["context_window"] = window
    return payload


# ---- context_percent ------------------------------------------------------------

class TestContextPercent:
    def test_direct_percentage_field(self):
        assert context_percent({"context_window": {"used_percentage": 42.4}}) == 42

    @pytest.mark.parametrize("key", ["used_percentage", "percent_used", "usage_percent"])
    def test_all_direct_percentage_keys(self, key):
        assert context_percent({"context_window": {key: 21}}) == 21

    def test_used_over_size(self):
        window = {"context_window_size": 200000, "total_tokens_used": 84211}
        assert context_percent({"context_window": window}) == 42

    def test_current_usage_sum_over_size(self):
        window = {
            "context_window_size": 200000,
            "current_usage": {
                "input_tokens": 30000,
                "cache_creation_input_tokens": 10000,
                "cache_read_input_tokens": 40000,
                "output_tokens": 4000,
            },
        }
        assert context_percent({"context_window": window}) == 42

    def test_clamped_to_100(self):
        window = {"context_window_size": 100, "total_tokens_used": 250}
        assert context_percent({"context_window": window}) == 100

    def test_clamped_to_0(self):
        assert context_percent({"context_window": {"used_percentage": -3}}) == 0

    @pytest.mark.parametrize("payload", [
        {},
        {"context_window": {}},
        {"context_window": {"context_window_size": 200000}},   # no usage
        {"context_window": {"total_tokens_used": 42}},         # no size
        {"context_window": {"context_window_size": 0, "total_tokens_used": 1}},
        {"context_window": "not a dict"},
        {"context_window": {"used_percentage": "42"}},         # wrong type
        {"context_window": {"used_percentage": True}},         # bool is not a number
        "not a dict",
        None,
    ])
    def test_unusable_payloads_return_none(self, payload):
        assert context_percent(payload) is None

    def test_current_usage_ignored_when_not_a_dict(self):
        window = {"context_window_size": 200000, "current_usage": [1, 2, 3]}
        assert context_percent({"context_window": window}) is None


# ---- payload_cwd / model_name ---------------------------------------------------

class TestPayloadCwd:
    def test_workspace_current_dir_wins(self):
        payload = {"workspace": {"current_dir": "/a"}, "cwd": "/b"}
        assert payload_cwd(payload) == "/a"

    def test_falls_back_to_cwd(self):
        assert payload_cwd({"cwd": "/b"}) == "/b"

    @pytest.mark.parametrize("payload", [{}, {"workspace": {}}, {"cwd": "  "}, None, "x"])
    def test_falls_back_to_default_workspace(self, payload):
        assert payload_cwd(payload) == DEFAULT_WORKSPACE


class TestModelName:
    def test_display_name(self):
        assert model_name({"model": {"display_name": "Fable"}}) == "Fable"

    @pytest.mark.parametrize("payload", [{}, {"model": {}}, {"model": "x"}, None])
    def test_absent(self, payload):
        assert model_name(payload) is None


# ---- segment texts ---------------------------------------------------------------

class TestSegmentTexts:
    def test_ctx_text(self):
        assert ctx_text({"context_window": {"used_percentage": 42}}) == "ctx 42%"

    def test_ctx_text_absent(self):
        assert ctx_text({}) is None

    def test_cost_text(self):
        assert cost_text({"cost": {"total_cost_usd": 1.234}}) == "$1.23"

    def test_cost_text_zero(self):
        assert cost_text({"cost": {"total_cost_usd": 0}}) == "$0.00"

    @pytest.mark.parametrize("payload", [
        {}, {"cost": {}}, {"cost": "x"}, {"cost": {"total_cost_usd": "1.2"}},
        {"cost": {"total_cost_usd": True}},
        {"cost": {"total_cost_usd": float("nan")}},
        {"cost": {"total_cost_usd": float("inf")}},
        None,
    ])
    def test_cost_text_absent(self, payload):
        assert cost_text(payload) is None

    def test_rate_limit_text_rounds(self):
        payload = {"rate_limits": {"five_hour": {"used_percentage": 23.5}}}
        assert rate_limit_text(payload, "five_hour", "5h") == "5h 24%"

    def test_rate_limit_text_seven_day(self):
        payload = {"rate_limits": {"seven_day": {"used_percentage": 41.2}}}
        assert rate_limit_text(payload, "seven_day", "7d") == "7d 41%"

    def test_rate_limit_text_clamped(self):
        payload = {"rate_limits": {"five_hour": {"used_percentage": 250}}}
        assert rate_limit_text(payload, "five_hour", "5h") == "5h 100%"

    @pytest.mark.parametrize("payload", [
        {},                                                  # no rate_limits
        {"rate_limits": "x"},                                # wrong type
        {"rate_limits": {}},                                 # no window
        {"rate_limits": {"five_hour": "x"}},                 # window wrong type
        {"rate_limits": {"five_hour": {}}},                  # no percentage
        {"rate_limits": {"five_hour": {"used_percentage": "23"}}},
        {"rate_limits": {"five_hour": {"used_percentage": float("inf")}}},
        {"rate_limits": {"five_hour": {"used_percentage": float("nan")}}},
        None,
    ])
    def test_rate_limit_text_absent(self, payload):
        assert rate_limit_text(payload, "five_hour", "5h") is None

    def test_effort_text(self):
        assert effort_text({"effort": {"level": "high"}}) == "eff high"

    @pytest.mark.parametrize("payload", [
        {}, {"effort": {}}, {"effort": "high"}, {"effort": {"level": "  "}},
        {"effort": {"level": 3}}, None,
    ])
    def test_effort_text_absent(self, payload):
        assert effort_text(payload) is None

    @pytest.mark.parametrize("ms,expected", [
        (0, "0s"),
        (42_000, "42s"),
        (59_999, "59s"),
        (60_000, "1m"),
        (3_599_000, "59m"),
        (3_780_000, "1h03m"),
        (7_380_000, "2h03m"),
    ])
    def test_duration_text(self, ms, expected):
        assert duration_text({"cost": {"total_duration_ms": ms}}) == expected

    @pytest.mark.parametrize("payload", [
        {}, {"cost": {}}, {"cost": {"total_duration_ms": -1}},
        {"cost": {"total_duration_ms": "42"}},
        {"cost": {"total_duration_ms": float("nan")}},
        {"cost": {"total_duration_ms": float("inf")}},
        None,
    ])
    def test_duration_text_absent(self, payload):
        assert duration_text(payload) is None

    def test_lines_text(self):
        cost = {"total_lines_added": 156, "total_lines_removed": 23}
        assert lines_text({"cost": cost}) == "+156/-23"

    def test_lines_text_missing_half_counts_as_zero(self):
        assert lines_text({"cost": {"total_lines_added": 5}}) == "+5/-0"

    def test_lines_text_negative_clamped(self):
        cost = {"total_lines_added": -5, "total_lines_removed": -3}
        assert lines_text({"cost": cost}) == "+0/-0"

    def test_lines_text_nan_half_counts_as_missing(self):
        cost = {"total_lines_added": float("nan"), "total_lines_removed": 3}
        assert lines_text({"cost": cost}) == "+0/-3"

    @pytest.mark.parametrize("payload", [
        {}, {"cost": {}},
        {"cost": {"total_lines_added": float("nan"),
                  "total_lines_removed": float("inf")}},
        None,
    ])
    def test_lines_text_absent(self, payload):
        assert lines_text(payload) is None


# ---- parse_segments ---------------------------------------------------------------

class TestParseSegments:
    @pytest.mark.parametrize("raw", [None, "", "   ", 42])
    def test_unset_or_blank_yields_default(self, raw):
        assert parse_segments(raw) == DEFAULT_SEGMENTS

    def test_ordered_list(self):
        assert parse_segments("model,ctx,cost") == ("model", "ctx", "cost")

    def test_case_and_whitespace_tolerant(self):
        assert parse_segments(" Model , CTX ") == ("model", "ctx")

    def test_unknown_names_ignored(self):
        assert parse_segments("model,bogus,cost") == ("model", "cost")

    def test_all_unknown_falls_back_to_default(self):
        assert parse_segments("bogus,nope") == DEFAULT_SEGMENTS

    def test_full_vocabulary(self):
        raw = "repo,branch,task,ctx,model,cost,5h,7d,effort,duration,lines"
        assert parse_segments(raw) == (
            "repo", "branch", "task", "ctx", "model", "cost",
            "5h", "7d", "effort", "duration", "lines",
        )


# ---- build_line -----------------------------------------------------------------

def _values(**overrides):
    values = {"repo": "group/project", "branch": "feature/x",
              "task": "develop", "ctx": "ctx 42%"}
    values.update(overrides)
    return values


class TestBuildLine:
    def test_all_segments(self):
        assert build_line(_values()) == "group/project @ feature/x | develop | ctx 42%"

    def test_missing_context_segment_omitted(self):
        line = build_line(_values(ctx=None))
        assert line == "group/project @ feature/x | develop"

    def test_repo_without_branch(self):
        assert build_line({"repo": "group/project"}) == "group/project"

    def test_branch_without_repo(self):
        assert build_line({"branch": "main"}) == "main"

    def test_zero_percent_still_rendered(self):
        assert build_line({"ctx": "ctx 0%"}) == "ctx 0%"

    def test_all_missing_falls_back_to_model(self):
        assert build_line({}, model="Fable") == "Fable"

    def test_all_missing_without_model_is_empty(self):
        assert build_line({}) == ""

    def test_indicators_appended(self):
        line = build_line({"repo": "r"}, container=True, danger_zone=True)
        assert line == "r 📦⚡"

    def test_indicators_alone_without_segments(self):
        assert build_line({}, container=True) == "📦"

    def test_custom_segment_order(self):
        values = {"model": "Fable", "ctx": "ctx 42%", "cost": "$1.23"}
        line = build_line(values, segments=("model", "ctx", "cost"))
        assert line == "Fable | ctx 42% | $1.23"

    def test_extra_segments_after_defaults(self):
        values = _values(model="Fable", cost="$1.23", **{"5h": "5h 24%"})
        line = build_line(
            values,
            segments=("repo", "branch", "task", "ctx", "model", "cost", "5h"),
        )
        assert line == ("group/project @ feature/x | develop | ctx 42% "
                        "| Fable | $1.23 | 5h 24%")

    def test_repo_branch_join_only_when_adjacent(self):
        line = build_line(_values(), segments=("repo", "task", "branch"))
        assert line == "group/project | develop | feature/x"

    def test_branch_before_repo_not_joined(self):
        line = build_line(_values(), segments=("branch", "repo"))
        assert line == "feature/x | group/project"

    def test_adjacent_repo_branch_with_missing_branch(self):
        line = build_line(_values(branch=None), segments=("repo", "branch", "task"))
        assert line == "group/project | develop"

    def test_configured_segments_all_empty_falls_back_to_model(self):
        line = build_line({}, segments=("cost", "5h"), model="Fable")
        assert line == "Fable"


# ---- main() via subprocess ------------------------------------------------------

def _run_script(payload, env_extra=None, raw_stdin=None):
    """Run the statusline script with a stripped-LMER env plus *env_extra*."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("LMER_")}
    env.pop("CLAUDE_CONTAINER", None)
    env.update(env_extra or {})
    stdin = raw_stdin if raw_stdin is not None else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=stdin, capture_output=True, text=True, env=env,
    )


def _git_repo(path, branch="feature/x"):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    return path


class TestMainSubprocess:
    def test_full_payload_in_git_repo(self, tmp_path):
        repo = _git_repo(tmp_path / "checkout")
        payload = _payload(cwd=repo, window={"context_window_size": 200000,
                                             "total_tokens_used": 84211})
        r = _run_script(payload, {"LMER_REPO_PROJECT": "group/project",
                                  "LMER_TASK": "develop"})
        assert r.returncode == 0
        assert r.stdout.rstrip("\n") == "group/project @ feature/x | develop | ctx 42%"

    def test_missing_usage_fields_omit_ctx_segment(self, tmp_path):
        repo = _git_repo(tmp_path / "checkout")
        payload = _payload(cwd=repo, window={"context_window_size": 200000})
        r = _run_script(payload, {"LMER_REPO_PROJECT": "group/project",
                                  "LMER_TASK": "develop"})
        assert r.returncode == 0
        assert r.stdout.rstrip("\n") == "group/project @ feature/x | develop"

    def test_repo_falls_back_to_git_toplevel_basename(self, tmp_path):
        repo = _git_repo(tmp_path / "myproject", branch="main")
        r = _run_script(_payload(cwd=repo))
        assert r.returncode == 0
        assert r.stdout.rstrip("\n") == "myproject @ main"

    def test_no_git_repo_and_no_env(self, tmp_path):
        bare = tmp_path / "not-a-repo"
        bare.mkdir()
        r = _run_script(_payload(cwd=bare))
        assert r.returncode == 0
        # No repo, branch, task, or usage — falls back to the model name.
        assert r.stdout.rstrip("\n") == "Fable"

    def test_env_repo_wins_over_git_toplevel(self, tmp_path):
        repo = _git_repo(tmp_path / "clone-dir", branch="main")
        r = _run_script(_payload(cwd=repo), {"LMER_REPO_PROJECT": "group/project"})
        assert r.stdout.rstrip("\n") == "group/project @ main"

    def test_task_without_repo_context(self, tmp_path):
        bare = tmp_path / "not-a-repo"
        bare.mkdir()
        r = _run_script(_payload(cwd=bare), {"LMER_TASK": "develop"})
        assert r.stdout.rstrip("\n") == "develop"

    def test_container_and_danger_zone_indicators(self, tmp_path):
        bare = tmp_path / "not-a-repo"
        bare.mkdir()
        r = _run_script(_payload(cwd=bare),
                        {"LMER_TASK": "develop", "CLAUDE_CONTAINER": "true",
                         "LMER_DANGER_ZONE": "1"})
        assert r.stdout.rstrip("\n") == "develop 📦⚡"

    def test_lmer_statusline_selects_and_orders_segments(self, tmp_path):
        repo = _git_repo(tmp_path / "checkout")
        payload = _payload(cwd=repo, window={"used_percentage": 42})
        payload["cost"] = {"total_cost_usd": 1.234, "total_duration_ms": 3_780_000,
                           "total_lines_added": 156, "total_lines_removed": 23}
        payload["rate_limits"] = {"five_hour": {"used_percentage": 23.5},
                                  "seven_day": {"used_percentage": 41.2}}
        payload["effort"] = {"level": "high"}
        env = {"LMER_REPO_PROJECT": "group/project", "LMER_TASK": "develop",
               "LMER_STATUSLINE": "repo,branch,task,ctx,model,cost,5h,7d,effort,duration,lines"}
        r = _run_script(payload, env)
        assert r.returncode == 0
        assert r.stdout.rstrip("\n") == (
            "group/project @ feature/x | develop | ctx 42% | Fable | $1.23 "
            "| 5h 24% | 7d 41% | eff high | 1h03m | +156/-23"
        )

    def test_lmer_statusline_can_drop_default_segments(self, tmp_path):
        repo = _git_repo(tmp_path / "checkout")
        payload = _payload(cwd=repo, window={"used_percentage": 42})
        payload["cost"] = {"total_cost_usd": 0.5}
        env = {"LMER_REPO_PROJECT": "group/project", "LMER_TASK": "develop",
               "LMER_STATUSLINE": "model,ctx,cost"}
        r = _run_script(payload, env)
        assert r.stdout.rstrip("\n") == "Fable | ctx 42% | $0.50"

    def test_lmer_statusline_unset_matches_previous_default(self, tmp_path):
        """A payload rich in optional data still renders the pre-#121 line
        when LMER_STATUSLINE is unset."""
        repo = _git_repo(tmp_path / "checkout")
        payload = _payload(cwd=repo, window={"used_percentage": 42})
        payload["cost"] = {"total_cost_usd": 1.23}
        payload["rate_limits"] = {"five_hour": {"used_percentage": 50}}
        r = _run_script(payload, {"LMER_REPO_PROJECT": "group/project",
                                  "LMER_TASK": "develop"})
        assert r.stdout.rstrip("\n") == "group/project @ feature/x | develop | ctx 42%"

    def test_lmer_statusline_garbage_value_degrades_to_default(self, tmp_path):
        repo = _git_repo(tmp_path / "checkout")
        payload = _payload(cwd=repo, window={"used_percentage": 42})
        r = _run_script(payload, {"LMER_REPO_PROJECT": "group/project",
                                  "LMER_TASK": "develop",
                                  "LMER_STATUSLINE": "bogus,,nope"})
        assert r.returncode == 0
        assert r.stdout.rstrip("\n") == "group/project @ feature/x | develop | ctx 42%"

    def test_limit_segments_omitted_without_rate_limits(self, tmp_path):
        """Non-subscription sessions carry no rate_limits — the 5h/7d
        segments drop out instead of erroring."""
        repo = _git_repo(tmp_path / "checkout")
        payload = _payload(cwd=repo, window={"used_percentage": 42})
        r = _run_script(payload, {"LMER_REPO_PROJECT": "group/project",
                                  "LMER_STATUSLINE": "repo,ctx,5h,7d"})
        assert r.stdout.rstrip("\n") == "group/project | ctx 42%"

    @pytest.mark.parametrize("raw,segments", [
        # json.loads admits NaN/Infinity literals — they must degrade to an
        # omitted segment, never a round()/int() traceback (exit 1).
        ('{"cost": {"total_duration_ms": NaN}}', "duration"),
        ('{"rate_limits": {"five_hour": {"used_percentage": Infinity}}}', "5h"),
        ('{"cost": {"total_lines_added": NaN}}', "lines"),
        ('{"cost": {"total_cost_usd": NaN}}', "cost"),
        ('{"context_window": {"used_percentage": NaN}}', "ctx"),
    ])
    def test_non_finite_numbers_never_break(self, raw, segments):
        r = _run_script(None, {"LMER_STATUSLINE": segments,
                               "LMER_REPO_PROJECT": "group/project"},
                        raw_stdin=raw)
        assert r.returncode == 0
        assert r.stderr == ""
        assert "nan" not in r.stdout.lower()

    @pytest.mark.parametrize("raw", ["", "   ", "not json", "[1,2,3]", '"str"'])
    def test_garbage_or_empty_stdin_never_breaks(self, tmp_path, raw):
        r = _run_script(None, {"LMER_REPO_PROJECT": "group/project"}, raw_stdin=raw)
        assert r.returncode == 0
        assert r.stderr == ""
        # cwd falls back to /workspace; repo comes from env regardless.
        assert r.stdout.rstrip("\n").startswith("group/project")

    def test_single_line_of_output(self, tmp_path):
        repo = _git_repo(tmp_path / "checkout")
        r = _run_script(_payload(cwd=repo, window={"used_percentage": 10}))
        assert r.stdout.count("\n") == 1


# ---- provisioning ---------------------------------------------------------------

# ---- the status document (issue #248) -------------------------------------

def _status_payload():
    """A payload carrying all three published facts."""
    return {
        "context_window": {"used_percentage": 9},
        "rate_limits": {
            "five_hour": {"used_percentage": 23.5, "resets_at": 1738425600},
            "seven_day": {"used_percentage": 41.2, "resets_at": 1738857600},
        },
        "effort": {"level": "high"},
    }


class TestLimitFields:
    """The structured seams the document is built from, beside the rendered
    segment that used to be the only reader of ``rate_limits``."""

    def test_percent_is_clamped_like_the_segment(self):
        payload = {"rate_limits": {"five_hour": {"used_percentage": 250}}}
        assert limit_percent(payload, "five_hour") == 100
        assert rate_limit_text(payload, "five_hour", "5h") == "5h 100%"

    def test_resets_at_is_epoch_seconds(self):
        payload = {"rate_limits": {"seven_day": {"resets_at": 1738857600}}}
        assert limit_resets_at(payload, "seven_day") == 1738857600

    @pytest.mark.parametrize("value", [0, -1, "1738857600", None, True, float("inf")])
    def test_unusable_reset_is_absent_not_zero(self, value):
        """An epoch of 0 would render as 1970 — a window that reset long ago,
        which is the opposite of what "not reported" means."""
        payload = {"rate_limits": {"seven_day": {"resets_at": value}}}
        assert limit_resets_at(payload, "seven_day") is None

    def test_a_window_may_report_percent_without_a_reset(self):
        payload = {"rate_limits": {"five_hour": {"used_percentage": 12}}}
        assert limit_percent(payload, "five_hour") == 12
        assert limit_resets_at(payload, "five_hour") is None

    def test_effort_level_is_the_bare_word(self):
        assert effort_level({"effort": {"level": " xhigh "}}) == "xhigh"
        assert effort_text({"effort": {"level": " xhigh "}}) == "eff xhigh"

    @pytest.mark.parametrize(
        "payload",
        [{}, {"effort": "x"}, {"effort": {}}, {"effort": {"level": ""}},
         {"effort": {"level": 3}}],
    )
    def test_effort_absent(self, payload):
        assert effort_level(payload) is None


class TestLimitKeys:
    """Which windows get published. A label table, not an allowlist: the keys of
    ``rate_limits`` are Claude Code's to name, so a renamed or newly-added window
    must not be dropped silently — a strip showing fewer numbers is
    indistinguishable from a session reporting none."""

    def test_labelled_windows_first_in_reading_order(self):
        payload = {"rate_limits": {"seven_day": {}, "five_hour": {}}}
        assert limit_keys(payload) == ["five_hour", "seven_day"]

    def test_a_window_this_renderer_never_heard_of_is_still_published(self):
        payload = {"rate_limits": {
            "five_hour": {"used_percentage": 23},
            "monthly_model_quota": {"used_percentage": 7},
        }}
        assert limit_keys(payload) == ["five_hour", "monthly_model_quota"]
        document = status_document(payload, updated="u")
        assert document["limits"] == [
            {"key": "five_hour", "percent": 23, "label": "5h"},
            # No label: this renderer has no short name for it, and the platform
            # falls back to the key rather than inventing one.
            {"key": "monthly_model_quota", "percent": 7},
        ]

    def test_unknown_windows_keep_the_payloads_own_order(self):
        payload = {"rate_limits": {"b_quota": {}, "a_quota": {}}}
        assert limit_keys(payload) == ["b_quota", "a_quota"]

    def test_the_number_published_is_capped(self):
        """The payload is somebody else's data structure, and the platform
        refuses a document for its size."""
        payload = {"rate_limits": {f"w{index}": {"used_percentage": 1}
                                   for index in range(STATUS_LIMIT_CAP + 5)}}
        assert len(limit_keys(payload)) == STATUS_LIMIT_CAP

    @pytest.mark.parametrize(
        "payload", [{}, {"rate_limits": {}}, {"rate_limits": "x"}, "junk", None],
    )
    def test_nothing_to_publish(self, payload):
        assert limit_keys(payload) == []


class TestStatusDocument:
    def test_all_three_facts(self):
        document = status_document(_status_payload(), updated="2026-09-03T14:31:07Z")
        assert document == {
            "schema": STATUS_SCHEMA,
            "harness": "claude",
            "updated": "2026-09-03T14:31:07Z",
            "context": {"percent": 9},
            "limits": [
                {"key": "five_hour", "label": "5h", "percent": 24,
                 "resets_at": 1738425600},
                {"key": "seven_day", "label": "7d", "percent": 41,
                 "resets_at": 1738857600},
            ],
            "effort": {"level": "high"},
        }

    def test_the_identifying_fields_are_always_there(self):
        """A session that has learned nothing yet still says who it is: the
        platform tells "publishes status, nothing to report" from "publishes
        nothing" by the file's existence."""
        document = status_document({}, updated="2026-09-03T14:31:07Z")
        assert document == {
            "schema": STATUS_SCHEMA,
            "harness": "claude",
            "updated": "2026-09-03T14:31:07Z",
        }

    def test_windows_are_published_in_reading_order(self):
        """5h before 7d, and the order survives a payload that lists them the
        other way round — the document is read left to right on a strip."""
        payload = {"rate_limits": {
            "seven_day": {"used_percentage": 41},
            "five_hour": {"used_percentage": 23},
        }}
        document = status_document(payload, updated="u")
        assert [entry["key"] for entry in document["limits"]] == [
            "five_hour", "seven_day",
        ]

    def test_one_window_alone(self):
        payload = {"rate_limits": {"seven_day": {"used_percentage": 41}}}
        document = status_document(payload, updated="u")
        assert document["limits"] == [
            {"key": "seven_day", "label": "7d", "percent": 41},
        ]

    def test_a_window_with_no_percent_is_dropped(self):
        payload = {"rate_limits": {"five_hour": {"resets_at": 1738425600}}}
        assert "limits" not in status_document(payload, updated="u")

    @pytest.mark.parametrize(
        "payload", [{}, {"rate_limits": {}}, {"rate_limits": "x"}],
    )
    def test_no_windows_publishes_no_limits_key(self, payload):
        """Absence is "not reported", never an empty list a client would have
        to tell from a zeroed one."""
        assert "limits" not in status_document(payload, updated="u")

    def test_the_document_ignores_the_segment_configuration(self):
        """LMER_STATUSLINE selects what the terminal *line* renders; the
        platform strip is composed platform-side (operator decision on #248),
        so a trimmed line must not blank it."""
        document = status_document(_status_payload(), updated="u")
        assert set(document) >= {"context", "limits", "effort"}

    def test_it_is_json_serialisable(self):
        """It crosses a mount as JSON, so nothing in it may be a Python
        object a client cannot read."""
        json.loads(json.dumps(status_document(_status_payload(), updated="u")))


class TestPublishStatus:
    def test_writes_the_document(self, tmp_path):
        assert publish_status({"schema": 1}, str(tmp_path)) is True
        assert json.loads((tmp_path / STATUS_FILENAME).read_text()) == {"schema": 1}

    def test_leaves_no_temporary_behind(self, tmp_path):
        """The reader has no lock and the file is rewritten on every render, so
        the write is a rename — and a rename that left its temporary would fill
        a mounted directory one render at a time."""
        publish_status({"schema": 1}, str(tmp_path))
        publish_status({"schema": 1}, str(tmp_path))
        assert [p.name for p in tmp_path.iterdir()] == [STATUS_FILENAME]

    def test_replaces_an_earlier_document(self, tmp_path):
        publish_status({"schema": 1, "context": {"percent": 9}}, str(tmp_path))
        publish_status({"schema": 1, "context": {"percent": 11}}, str(tmp_path))
        document = json.loads((tmp_path / STATUS_FILENAME).read_text())
        assert document["context"] == {"percent": 11}

    def test_mode_is_0600(self, tmp_path):
        publish_status({"schema": 1}, str(tmp_path))
        mode = (tmp_path / STATUS_FILENAME).stat().st_mode & 0o777
        assert mode == 0o600

    def test_a_missing_directory_is_false_not_an_exception(self, tmp_path):
        assert publish_status({"schema": 1}, str(tmp_path / "nope")) is False

    def test_an_unserialisable_document_is_false(self, tmp_path):
        assert publish_status({"bad": {1, 2}}, str(tmp_path)) is False
        assert not (tmp_path / STATUS_FILENAME).exists()
        assert list(tmp_path.iterdir()) == []


class TestPublishIfMounted:
    def test_publishes_when_the_platform_mounted_a_directory(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LMER_STATUS_DIR", str(tmp_path))
        assert publish_if_mounted(_status_payload()) is True
        document = json.loads((tmp_path / STATUS_FILENAME).read_text())
        assert document["context"] == {"percent": 9}
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
                            document["updated"])

    @pytest.mark.parametrize("value", ["", "   "])
    def test_a_blank_variable_publishes_nothing(self, value, monkeypatch):
        monkeypatch.setenv("LMER_STATUS_DIR", value)
        assert publish_if_mounted(_status_payload()) is False

    def test_an_unset_variable_publishes_nothing(self, monkeypatch):
        monkeypatch.delenv("LMER_STATUS_DIR", raising=False)
        assert publish_if_mounted(_status_payload()) is False

    def test_the_directory_is_never_created(self, tmp_path, monkeypatch):
        """It is one half of a mount the platform made: a directory created
        here would hold a document nobody can read, and the platform's "is
        there a directory" probe is what tells a mounted session from an image
        too old to have one."""
        target = tmp_path / "not-mounted"
        monkeypatch.setenv("LMER_STATUS_DIR", str(target))
        assert publish_if_mounted(_status_payload()) is False
        assert not target.exists()

    def test_a_file_where_the_directory_should_be_publishes_nothing(
        self, tmp_path, monkeypatch,
    ):
        target = tmp_path / "file"
        target.write_text("x")
        monkeypatch.setenv("LMER_STATUS_DIR", str(target))
        assert publish_if_mounted(_status_payload()) is False


class TestPublishedByMain:
    """End to end: what a real render leaves in the mounted directory."""

    def test_the_line_and_the_document(self, tmp_path):
        result = _run_script(
            {**_status_payload(), "model": {"display_name": "Opus 5"}},
            env_extra={"LMER_STATUS_DIR": str(tmp_path),
                       "LMER_STATUSLINE": "ctx", "LMER_REPO_PROJECT": "group/project"},
        )
        assert result.returncode == 0
        assert result.stdout.rstrip("\n") == "ctx 9%"
        document = json.loads((tmp_path / STATUS_FILENAME).read_text())
        assert document["limits"][0]["label"] == "5h"
        assert document["effort"] == {"level": "high"}

    def test_a_session_with_no_mount_still_renders(self, tmp_path):
        result = _run_script(_status_payload(), env_extra={"LMER_STATUSLINE": "ctx"})
        assert result.returncode == 0
        assert result.stdout.rstrip("\n") == "ctx 9%"

    def test_a_document_that_cannot_be_written_costs_the_line_nothing(self, tmp_path):
        """A mount the publish cannot land in costs the document, not the line.

        Staged as a directory in the document's place rather than as a mode bit:
        this runs the real script in a subprocess and root (what CI's pytest job
        runs as) ignores a 0o500 directory, while the kernel enforces this case
        for every uid.
        """
        target = tmp_path / "mounted"
        target.mkdir()
        (target / STATUS_FILENAME).mkdir()
        result = _run_script(
            _status_payload(),
            env_extra={"LMER_STATUS_DIR": str(target), "LMER_STATUSLINE": "ctx"},
        )
        assert result.returncode == 0
        assert result.stdout.rstrip("\n") == "ctx 9%"
        assert (target / STATUS_FILENAME).is_dir(), "nothing replaced it"
        # And no temporary left beside it: a publish that fails cleans up.
        assert [path.name for path in target.iterdir()] == [STATUS_FILENAME]

    @pytest.mark.skipif(
        os.geteuid() == 0, reason="root ignores the mode bit this test relies on"
    )
    def test_a_read_only_mount_costs_the_document_not_the_line(self, tmp_path):
        """The same property with the mode bit doing the enforcing — the shape a
        uid mismatch across the bind mount actually takes."""
        target = tmp_path / "mounted"
        target.mkdir(mode=0o500)
        try:
            result = _run_script(
                _status_payload(),
                env_extra={"LMER_STATUS_DIR": str(target), "LMER_STATUSLINE": "ctx"},
            )
            assert result.returncode == 0
            assert result.stdout.rstrip("\n") == "ctx 9%"
            assert not (target / STATUS_FILENAME).exists()
        finally:
            target.chmod(0o700)

    def test_torn_stdin_publishes_the_identifying_fields(self, tmp_path):
        """Unparseable input degrades to an empty payload, and the document
        that describes it still says who it is."""
        result = _run_script(
            None, env_extra={"LMER_STATUS_DIR": str(tmp_path)}, raw_stdin="{not json",
        )
        assert result.returncode == 0
        document = json.loads((tmp_path / STATUS_FILENAME).read_text())
        assert document["schema"] == STATUS_SCHEMA
        assert document["harness"] == "claude"
        assert "context" not in document

    def test_the_platform_reads_what_the_hook_wrote(self, tmp_path):
        """The round trip, which is the whole contract: the two halves cannot
        share an import (this renderer is stdlib-only by constraint), so the
        format is bound by a test that writes with one and reads with the
        other."""
        from lmer_platform import statusline as reader

        _run_script(_status_payload(), env_extra={"LMER_STATUS_DIR": str(tmp_path)})
        served = reader.read_document(tmp_path / STATUS_FILENAME)
        assert served["harness"] == "claude"
        assert served["context"] == {"percent": 9}
        assert served["effort"] == {"level": "high"}
        assert [entry["label"] for entry in served["limits"]] == ["5h", "7d"]
        assert served["limits"][0]["resets_at"] == "2025-02-01T16:00:00Z"


class TestStatuslineProvisioning:
    """Every lmer session must get the status line without user setup:
    settings.json points statusLine at claude-status (on PATH via
    /Agents/global/bin), and claude-status delegates to the renderer."""

    def test_settings_statusline_block(self):
        settings = json.loads(
            (REPO_ROOT / "agent-files" / "claude" / "settings.json").read_text()
        )
        assert settings["statusLine"] == {"type": "command", "command": "claude-status"}

    def test_claude_status_delegates_to_renderer(self):
        wrapper = (REPO_ROOT / "bin" / "claude-status").read_text()
        assert "hooks/statusline.py" in wrapper
        assert 'exec python3 "$STATUSLINE"' in wrapper

    def test_scripts_are_executable(self):
        assert os.access(REPO_ROOT / "bin" / "claude-status", os.X_OK)
        assert os.access(SCRIPT, os.X_OK)

    def test_cli_env_dict_declares_statusline(self):
        """Guard: LMER_STATUSLINE must be in cli.py's container env dict.

        Without this entry, setting LMER_STATUSLINE on the host has no
        effect because the var never reaches the container where the
        statusline renderer runs.
        """
        source = (REPO_ROOT / "src" / "lmer_cli" / "cli.py").read_text()
        pattern = re.compile(
            r"""["']LMER_STATUSLINE["']\s*:\s*os\.environ\.get\(\s*["']LMER_STATUSLINE["']\s*\)"""
        )
        assert pattern.search(source), \
            "LMER_STATUSLINE entry missing from cli.py container env dict"

    def test_cli_env_dict_declares_the_status_dir(self):
        """Guard: LMER_STATUS_DIR must be in cli.py's container env dict.

        The platform sets it on the child ``lmer``; without this entry it stops
        there and every strip is blank while both halves of the mount still look
        correctly wired.
        """
        source = (REPO_ROOT / "src" / "lmer_cli" / "cli.py").read_text()
        pattern = re.compile(
            r"""["']LMER_STATUS_DIR["']\s*:\s*os\.environ\.get\(\s*["']LMER_STATUS_DIR["']\s*\)"""
        )
        assert pattern.search(source), \
            "LMER_STATUS_DIR entry missing from cli.py container env dict"

    def test_the_status_dir_variable_is_documented(self):
        """Every LMER_ variable controlling user-visible behaviour carries a
        bullet in docs/LMER-CLI.md (the project's env-var convention)."""
        doc = (REPO_ROOT / "docs" / "LMER-CLI.md").read_text()
        assert "**`LMER_STATUS_DIR`**" in doc
        assert "SESSION-STATUS.md" in doc

    def test_wrapper_end_to_end(self, tmp_path):
        """bin/claude-status → hooks/statusline.py with a real payload."""
        repo = _git_repo(tmp_path / "checkout")
        payload = _payload(cwd=repo, window={"used_percentage": 7})
        env = {k: v for k, v in os.environ.items() if not k.startswith("LMER_")}
        env.pop("CLAUDE_CONTAINER", None)
        env["LMER_REPO_PROJECT"] = "group/project"
        env["LMER_TASK"] = "develop"
        r = subprocess.run(
            ["bash", str(REPO_ROOT / "bin" / "claude-status")],
            input=json.dumps(payload), capture_output=True, text=True, env=env,
        )
        assert r.returncode == 0
        assert r.stdout.rstrip("\n") == "group/project @ feature/x | develop | ctx 7%"
