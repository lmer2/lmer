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
    build_line,
    context_percent,
    cost_text,
    ctx_text,
    duration_text,
    effort_text,
    lines_text,
    model_name,
    parse_segments,
    payload_cwd,
    rate_limit_text,
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
