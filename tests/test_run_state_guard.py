"""Tests for the run-state + push-before-stop Stop hook (hooks/run_state_guard.py)."""
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from hooks.run_state_guard import (
    COUNTER_TEMPLATE,
    PUSH_NUDGE_CAP,
    SENTINEL_TEMPLATE,
    STATE_COMMANDS,
    STATE_FIELDS,
    build_push_reason,
    build_state_reason,
    derive_run_dir,
    detect_activity,
    env_flag,
    evaluate,
    missing_state_fields,
    parse_resume_json,
    run_dir_noncompliance,
)

REPO_ROOT = Path(__file__).parent.parent
HOOK = REPO_ROOT / "hooks" / "run_state_guard.py"

HOST = "git.example.com"
PROJECT = "group/project"
SLUG = "develop-run-naming"

# Every non-empty subset of the trigger-1 state fields, for matrix tests.
FIELD_COMBOS = [
    ("phase",),
    ("goal",),
    ("name",),
    ("phase", "goal"),
    ("phase", "name"),
    ("goal", "name"),
    ("phase", "goal", "name"),
]


def _decision(phase="develop", goal="ship the guard", name="run-naming",
              slug=SLUG, kind="run", **extra):
    decision = {"kind": kind, "slug": slug, "phase": phase, "goal": goal, "name": name}
    decision.update(extra)
    return decision


# ---- env_flag -------------------------------------------------------------------

class TestEnvFlag:
    @pytest.mark.parametrize("value", ["1", "yes", "true", "TRUE", " Yes "])
    def test_truthy(self, value):
        assert env_flag(value) is True

    @pytest.mark.parametrize("value", ["0", "no", "false", "FALSE", " No "])
    def test_falsy(self, value):
        assert env_flag(value) is False

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_unset_or_blank_returns_default(self, value):
        assert env_flag(value) is True
        assert env_flag(value, default=False) is False

    def test_unrecognized_returns_default(self):
        assert env_flag("maybe") is True
        assert env_flag("maybe", default=False) is False


# ---- parse_resume_json ----------------------------------------------------------

class TestParseResumeJson:
    def test_single_json_line(self):
        decision = _decision()
        assert parse_resume_json(json.dumps(decision)) == decision

    def test_stray_leading_lines_before_json(self):
        decision = _decision()
        stdout = "warning: something\n\n" + json.dumps(decision) + "\n"
        assert parse_resume_json(stdout) == decision

    def test_last_parsable_line_wins(self):
        first, second = _decision(slug="a"), _decision(slug="b")
        stdout = json.dumps(first) + "\n" + json.dumps(second) + "\n"
        assert parse_resume_json(stdout) == second

    def test_trailing_junk_after_json_line(self):
        decision = _decision()
        stdout = json.dumps(decision) + "\nnot json at all\n"
        assert parse_resume_json(stdout) == decision

    @pytest.mark.parametrize("stdout", [
        "",
        "   \n  ",
        "No run context for this container.\n",
        "[1, 2, 3]",
        '"just a string"',
        "42",
    ])
    def test_unrecoverable_returns_none(self, stdout):
        assert parse_resume_json(stdout) is None


# ---- missing_state_fields -------------------------------------------------------

class TestMissingStateFields:
    @pytest.mark.parametrize("nulled", FIELD_COMBOS)
    def test_null_combinations(self, nulled):
        decision = _decision()
        for field in nulled:
            decision[field] = None
        expected = [field for field in STATE_FIELDS if field in nulled]
        assert missing_state_fields(decision) == expected

    def test_absent_fields_count_as_missing(self):
        decision = _decision()
        del decision["name"]
        assert missing_state_fields(decision) == ["name"]

    def test_all_set_is_compliant(self):
        assert missing_state_fields(_decision()) == []

    def test_empty_string_values_are_not_missing(self):
        # Compliance is null/absent only; content quality is not the guard's job.
        assert missing_state_fields(_decision(phase="", goal="", name="")) == []

    @pytest.mark.parametrize("decision", [
        None,
        "junk",
        [],
        {},
        {"kind": "fresh", "phase": None, "goal": None, "name": None},
        {"phase": None, "goal": None, "name": None},  # no kind at all
    ])
    def test_non_run_decisions_are_compliant(self, decision):
        assert missing_state_fields(decision) == []


# ---- detect_activity ------------------------------------------------------------

class TestDetectActivity:
    def test_feature_branch_is_activity(self):
        assert detect_activity("feature/x", "main", "", 0) is True

    def test_dirty_tree_is_activity(self):
        assert detect_activity("main", "main", " M src/a.py\n", 0) is True

    def test_ahead_commits_are_activity(self):
        assert detect_activity("main", "main", "", 2) is True

    def test_clean_default_branch_is_not_activity(self):
        assert detect_activity("main", "main", "", 0) is False

    def test_whitespace_only_status_is_clean(self):
        assert detect_activity("main", "main", "  \n ", 0) is False

    def test_all_none_inputs_fail_open(self):
        assert detect_activity(None, None, None, None) is False

    def test_branch_comparison_needs_both_names(self):
        assert detect_activity("feature/x", None, "", 0) is False
        assert detect_activity(None, "main", "", 0) is False


# ---- run_dir_noncompliance ------------------------------------------------------

class TestRunDirNoncompliance:
    @pytest.mark.parametrize("status,ahead,expected", [
        ("?? notes.md\n", 0, (True, False)),
        ("", 2, (False, True)),
        (" M state.yaml\n", 1, (True, True)),
        ("", 0, (False, False)),
        ("   \n", 0, (False, False)),
    ])
    def test_matrix(self, status, ahead, expected):
        assert run_dir_noncompliance(status, ahead) == expected

    def test_none_inputs_fail_open(self):
        assert run_dir_noncompliance(None, None) == (False, False)
        assert run_dir_noncompliance(None, 3) == (False, True)
        assert run_dir_noncompliance("?? f\n", None) == (True, False)


# ---- derive_run_dir -------------------------------------------------------------

class TestDeriveRunDir:
    def test_explicit_run_dir_in_decision_wins(self):
        decision = _decision(run_dir="/elsewhere/runs/foo")
        assert derive_run_dir(decision, HOST, PROJECT, "/w") == "/elsewhere/runs/foo"

    def test_slug_path_under_work_repo(self):
        assert derive_run_dir(_decision(), HOST, PROJECT, "/w") == \
            f"/w/{HOST}/{PROJECT}/runs/{SLUG}"

    def test_default_work_repo_path(self):
        assert derive_run_dir(_decision(), HOST, PROJECT) == \
            f"/work/{HOST}/{PROJECT}/runs/{SLUG}"

    @pytest.mark.parametrize("slug", [None, "", "   "])
    def test_missing_slug_falls_back_to_runs_base(self, slug):
        assert derive_run_dir(_decision(slug=slug), HOST, PROJECT, "/w") == \
            f"/w/{HOST}/{PROJECT}/runs"

    def test_non_dict_decision_falls_back_to_runs_base(self):
        assert derive_run_dir(None, HOST, PROJECT, "/w") == f"/w/{HOST}/{PROJECT}/runs"

    @pytest.mark.parametrize("host,project", [
        (None, PROJECT), ("", PROJECT), (HOST, None), (HOST, ""),
    ])
    def test_no_run_context_returns_none(self, host, project):
        assert derive_run_dir(_decision(), host, project, "/w") is None


# ---- build_state_reason / build_push_reason --------------------------------------

class TestBuildReasons:
    def test_state_reason_lists_exactly_the_missing_pieces(self):
        reason = build_state_reason(["phase"])
        assert "phase" in reason
        assert STATE_COMMANDS["phase"] in reason
        assert STATE_COMMANDS["goal"] not in reason
        assert STATE_COMMANDS["name"] not in reason

    def test_state_reason_all_fields(self):
        reason = build_state_reason(list(STATE_FIELDS))
        for field in STATE_FIELDS:
            assert field in reason
            assert STATE_COMMANDS[field] in reason

    def test_push_reason_dirty_only(self):
        reason = build_push_reason(True, False)
        assert "uncommitted changes" in reason
        assert "upstream lacks" not in reason
        assert "work commit" in reason

    def test_push_reason_unpushed_only(self):
        reason = build_push_reason(False, True)
        assert "local commits its upstream lacks" in reason
        assert "uncommitted changes" not in reason

    def test_push_reason_both_and_run_dir(self):
        reason = build_push_reason(True, True, "/w/runs/foo")
        assert "uncommitted changes and local commits" in reason
        assert "/w/runs/foo" in reason


# ---- evaluate --------------------------------------------------------------------

class TestEvaluateStateTrigger:
    @staticmethod
    def _evaluate(missing, activity, nudged=False):
        return evaluate(
            missing_fields=list(missing),
            activity=activity,
            state_already_nudged=nudged,
            run_dir_dirty=False,
            run_dir_unpushed=False,
            push_nudge_count=0,
        )

    @pytest.mark.parametrize("missing", FIELD_COMBOS)
    @pytest.mark.parametrize("activity", [True, False])
    def test_activity_by_missing_field_matrix(self, missing, activity):
        verdict = self._evaluate(missing, activity)
        assert verdict["push_reason"] is None
        if activity:
            assert verdict["state_reason"] is not None
            for field in missing:
                assert STATE_COMMANDS[field] in verdict["state_reason"]
        else:
            assert verdict["state_reason"] is None

    def test_no_missing_fields_never_fires(self):
        assert self._evaluate([], True)["state_reason"] is None

    def test_sentinel_suppresses_state_nudge(self):
        assert self._evaluate(["name"], True, nudged=True)["state_reason"] is None


class TestEvaluatePushTrigger:
    @staticmethod
    def _evaluate(dirty=False, unpushed=False, count=0, **kwargs):
        return evaluate(
            missing_fields=[],
            activity=False,
            state_already_nudged=False,
            run_dir_dirty=dirty,
            run_dir_unpushed=unpushed,
            push_nudge_count=count,
            **kwargs,
        )

    @pytest.mark.parametrize("dirty,unpushed", [(True, False), (False, True), (True, True)])
    def test_noncompliant_run_dir_fires(self, dirty, unpushed):
        verdict = self._evaluate(dirty=dirty, unpushed=unpushed)
        assert verdict["push_reason"] is not None
        assert "work commit" in verdict["push_reason"]

    def test_compliant_run_dir_is_silent(self):
        assert self._evaluate()["push_reason"] is None

    @pytest.mark.parametrize("count", range(PUSH_NUDGE_CAP))
    def test_fires_below_cap(self, count):
        assert self._evaluate(dirty=True, count=count)["push_reason"] is not None

    @pytest.mark.parametrize("count", [PUSH_NUDGE_CAP, PUSH_NUDGE_CAP + 1])
    def test_capped_at_cap(self, count):
        assert self._evaluate(dirty=True, count=count)["push_reason"] is None

    def test_cap_override(self):
        assert self._evaluate(dirty=True, count=1, push_cap=1)["push_reason"] is None

    def test_run_dir_included_in_reason(self):
        verdict = self._evaluate(dirty=True, run_dir="/w/runs/foo")
        assert "/w/runs/foo" in verdict["push_reason"]

    def test_fires_even_when_state_already_nudged(self):
        verdict = evaluate(
            missing_fields=["name"],
            activity=True,
            state_already_nudged=True,
            run_dir_dirty=True,
            run_dir_unpushed=False,
            push_nudge_count=0,
        )
        assert verdict["state_reason"] is None
        assert verdict["push_reason"] is not None


class TestEvaluateBothTriggers:
    def test_both_reasons_returned_independently(self):
        verdict = evaluate(
            missing_fields=["phase", "name"],
            activity=True,
            state_already_nudged=False,
            run_dir_dirty=True,
            run_dir_unpushed=True,
            push_nudge_count=0,
        )
        assert "Run-state check" in verdict["state_reason"]
        assert "Push-before-stop check" in verdict["push_reason"]


# ---- main() via subprocess -------------------------------------------------------

# Env vars the harness controls per test; anything inherited is scrubbed first
# so a dev container's own run context cannot leak into the assertions.
GUARD_ENV_VARS = (
    "LMER_RUN_STATE_GUARD",
    "LMER_REPO_HOST",
    "LMER_REPO_PROJECT",
    "LMER_SESSION_ID",
    "LMER_WORK_REPO_PATH",
)


@pytest.fixture
def session():
    """Unique per-test session id; removes the /tmp sentinel/counter afterwards.

    The hook keys its marker files on LMER_SESSION_ID with hardcoded /tmp
    templates, so isolation between tests (and parallel CI workers) comes
    from the uuid, and cleanup keeps /tmp from accumulating."""
    sid = f"pytest-{uuid.uuid4().hex}"
    yield sid
    for template in (SENTINEL_TEMPLATE, COUNTER_TEMPLATE):
        try:
            os.unlink(template.format(session=sid))
        except OSError:
            pass


def _run_hook(payload, env_extra, raw_stdin=None):
    env = os.environ.copy()
    for var in GUARD_ENV_VARS:
        env.pop(var, None)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=raw_stdin if raw_stdin is not None else json.dumps(payload),
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd),
         "-c", "user.name=test", "-c", "user.email=test@example.com",
         *args],
        check=True, capture_output=True, text=True,
    )


def _make_work_cli(tmp_path, decision=None, exit_code=0, raw=None):
    """Fake `work` CLI on PATH that prints a canned resume payload."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    body = raw if raw is not None else json.dumps(decision)
    script = bin_dir / "work"
    script.write_text(f"#!/bin/sh\ncat <<'WORK_EOF'\n{body}\nWORK_EOF\nexit {exit_code}\n")
    script.chmod(0o755)
    return bin_dir


def _make_workspace(tmp_path, active=True):
    """Git workspace: clean `main` (no activity) or a feature branch (activity)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "init", "-q", "-b", "main")
    (ws / "README.md").write_text("hello\n")
    _git(ws, "add", ".")
    _git(ws, "commit", "-q", "-m", "init")
    if active:
        _git(ws, "checkout", "-q", "-b", "feature/x")
    return ws


def _make_work_repo(tmp_path, slug=SLUG):
    """Work-repo clone with a committed-and-pushed run dir (upstream set)."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)],
                   check=True, capture_output=True, text=True)
    clone = tmp_path / "workrepo"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)],
                   check=True, capture_output=True, text=True)
    run_dir = clone / HOST / PROJECT / "runs" / slug
    run_dir.mkdir(parents=True)
    (run_dir / "notes.md").write_text("artifact\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-q", "-m", "run dir")
    _git(clone, "push", "-q", "-u", "origin", "main")
    return clone, run_dir


def _guard_env(bin_dir, session, work_repo):
    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "LMER_REPO_HOST": HOST,
        "LMER_REPO_PROJECT": PROJECT,
        "LMER_SESSION_ID": session,
        "LMER_WORK_REPO_PATH": str(work_repo),
    }


class TestMainStateTrigger:
    def _env(self, tmp_path, session, decision):
        bin_dir = _make_work_cli(tmp_path, decision)
        # Nonexistent work repo: trigger 2 sees no run dir and stays silent.
        return _guard_env(bin_dir, session, tmp_path / "no-work-repo")

    def test_blocks_on_active_workspace_with_missing_fields(self, tmp_path, session):
        ws = _make_workspace(tmp_path, active=True)
        env = self._env(tmp_path, session, _decision(phase=None, name=None))
        r = _run_hook({"cwd": str(ws)}, env)
        assert r.returncode == 0
        out = json.loads(r.stdout)
        assert out["decision"] == "block"
        assert "Run-state check" in out["reason"]
        assert STATE_COMMANDS["phase"] in out["reason"]
        assert STATE_COMMANDS["name"] in out["reason"]
        assert STATE_COMMANDS["goal"] not in out["reason"]

    def test_second_stop_allowed_by_sentinel(self, tmp_path, session):
        ws = _make_workspace(tmp_path, active=True)
        env = self._env(tmp_path, session, _decision(goal=None))
        first = _run_hook({"cwd": str(ws)}, env)
        assert json.loads(first.stdout)["decision"] == "block"
        assert os.path.exists(SENTINEL_TEMPLATE.format(session=session))
        second = _run_hook({"cwd": str(ws)}, env)
        assert second.returncode == 0
        assert second.stdout.strip() == ""

    def test_noop_without_activity(self, tmp_path, session):
        ws = _make_workspace(tmp_path, active=False)
        env = self._env(tmp_path, session, _decision(phase=None, goal=None, name=None))
        r = _run_hook({"cwd": str(ws)}, env)
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_noop_when_state_compliant(self, tmp_path, session):
        ws = _make_workspace(tmp_path, active=True)
        env = self._env(tmp_path, session, _decision())
        r = _run_hook({"cwd": str(ws)}, env)
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_noop_for_non_run_decision(self, tmp_path, session):
        ws = _make_workspace(tmp_path, active=True)
        env = self._env(tmp_path, session,
                        {"kind": "fresh", "phase": None, "goal": None, "name": None})
        r = _run_hook({"cwd": str(ws)}, env)
        assert r.returncode == 0
        assert r.stdout.strip() == ""


class TestMainGates:
    def _blocking_setup(self, tmp_path, session):
        ws = _make_workspace(tmp_path, active=True)
        bin_dir = _make_work_cli(tmp_path, _decision(name=None))
        env = _guard_env(bin_dir, session, tmp_path / "no-work-repo")
        return ws, env

    def test_kill_switch_disables(self, tmp_path, session):
        ws, env = self._blocking_setup(tmp_path, session)
        env["LMER_RUN_STATE_GUARD"] = "0"
        r = _run_hook({"cwd": str(ws)}, env)
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_explicitly_enabled_still_blocks(self, tmp_path, session):
        ws, env = self._blocking_setup(tmp_path, session)
        env["LMER_RUN_STATE_GUARD"] = "1"
        r = _run_hook({"cwd": str(ws)}, env)
        assert json.loads(r.stdout)["decision"] == "block"

    def test_noop_when_stop_hook_active(self, tmp_path, session):
        ws, env = self._blocking_setup(tmp_path, session)
        r = _run_hook({"cwd": str(ws), "stop_hook_active": True}, env)
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    @pytest.mark.parametrize("dropped", ["LMER_REPO_HOST", "LMER_REPO_PROJECT"])
    def test_noop_without_run_context(self, tmp_path, session, dropped):
        ws, env = self._blocking_setup(tmp_path, session)
        del env[dropped]
        r = _run_hook({"cwd": str(ws)}, env)
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_noop_without_work_on_path(self, tmp_path, session):
        ws, env = self._blocking_setup(tmp_path, session)
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()
        env["PATH"] = str(empty_bin)  # no `work` reachable
        r = _run_hook({"cwd": str(ws)}, env)
        assert r.returncode == 0
        assert r.stdout.strip() == ""


class TestMainPushTrigger:
    def _env(self, tmp_path, session, work_repo, decision=None):
        bin_dir = _make_work_cli(tmp_path, decision or _decision())
        return _guard_env(bin_dir, session, work_repo)

    def test_dirty_run_dir_blocks(self, tmp_path, session):
        work_repo, run_dir = _make_work_repo(tmp_path)
        (run_dir / "scratch.md").write_text("uncommitted\n")
        r = _run_hook({"cwd": str(tmp_path)}, self._env(tmp_path, session, work_repo))
        assert r.returncode == 0
        out = json.loads(r.stdout)
        assert out["decision"] == "block"
        assert "Push-before-stop check" in out["reason"]
        assert "uncommitted changes" in out["reason"]
        assert "work commit" in out["reason"]

    def test_unpushed_commits_block(self, tmp_path, session):
        work_repo, run_dir = _make_work_repo(tmp_path)
        (run_dir / "notes.md").write_text("amended\n")
        _git(work_repo, "add", ".")
        _git(work_repo, "commit", "-q", "-m", "local only")  # no push
        r = _run_hook({"cwd": str(tmp_path)}, self._env(tmp_path, session, work_repo))
        out = json.loads(r.stdout)
        assert out["decision"] == "block"
        assert "local commits its upstream lacks" in out["reason"]

    def test_clean_and_pushed_passes(self, tmp_path, session):
        work_repo, _ = _make_work_repo(tmp_path)
        r = _run_hook({"cwd": str(tmp_path)}, self._env(tmp_path, session, work_repo))
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_dirt_outside_run_dir_does_not_block(self, tmp_path, session):
        work_repo, _ = _make_work_repo(tmp_path)
        (work_repo / HOST / PROJECT / "unrelated.md").write_text("elsewhere\n")
        r = _run_hook({"cwd": str(tmp_path)}, self._env(tmp_path, session, work_repo))
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_fires_repeatedly_then_caps_at_three(self, tmp_path, session):
        work_repo, run_dir = _make_work_repo(tmp_path)
        (run_dir / "scratch.md").write_text("uncommitted\n")
        env = self._env(tmp_path, session, work_repo)
        outcomes = [_run_hook({"cwd": str(tmp_path)}, env).stdout.strip()
                    for _ in range(PUSH_NUDGE_CAP + 1)]
        for blocked in outcomes[:PUSH_NUDGE_CAP]:  # unlike trigger 1: every stop
            assert json.loads(blocked)["decision"] == "block"
        assert outcomes[PUSH_NUDGE_CAP] == ""  # capped
        counter = Path(COUNTER_TEMPLATE.format(session=session))
        assert counter.read_text() == str(PUSH_NUDGE_CAP)

    def test_preseeded_counter_at_cap_is_silent(self, tmp_path, session):
        work_repo, run_dir = _make_work_repo(tmp_path)
        (run_dir / "scratch.md").write_text("uncommitted\n")
        Path(COUNTER_TEMPLATE.format(session=session)).write_text(str(PUSH_NUDGE_CAP))
        r = _run_hook({"cwd": str(tmp_path)}, self._env(tmp_path, session, work_repo))
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_both_triggers_combine_into_one_block(self, tmp_path, session):
        ws = _make_workspace(tmp_path, active=True)
        work_repo, run_dir = _make_work_repo(tmp_path)
        (run_dir / "scratch.md").write_text("uncommitted\n")
        env = self._env(tmp_path, session, work_repo, decision=_decision(name=None))
        r = _run_hook({"cwd": str(ws)}, env)
        out = json.loads(r.stdout)
        assert out["decision"] == "block"
        assert "Run-state check" in out["reason"]
        assert "Push-before-stop check" in out["reason"]
        assert "\n\n" in out["reason"]


class TestMainFailOpen:
    def test_work_exit_nonzero(self, tmp_path, session):
        ws = _make_workspace(tmp_path, active=True)
        bin_dir = _make_work_cli(tmp_path, _decision(name=None), exit_code=1)
        r = _run_hook({"cwd": str(ws)},
                      _guard_env(bin_dir, session, tmp_path / "no-work-repo"))
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_work_prints_no_run_context(self, tmp_path, session):
        ws = _make_workspace(tmp_path, active=True)
        bin_dir = _make_work_cli(tmp_path, raw="No run context for this container.")
        r = _run_hook({"cwd": str(ws)},
                      _guard_env(bin_dir, session, tmp_path / "no-work-repo"))
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_work_prints_unparsable_json(self, tmp_path, session):
        ws = _make_workspace(tmp_path, active=True)
        bin_dir = _make_work_cli(tmp_path, raw='{"kind": "run", broken')
        r = _run_hook({"cwd": str(ws)},
                      _guard_env(bin_dir, session, tmp_path / "no-work-repo"))
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_workspace_not_a_git_repo(self, tmp_path, session):
        not_git = tmp_path / "plain"
        not_git.mkdir()
        (not_git / "junk.txt").write_text("dirty-looking but ungoverned\n")
        bin_dir = _make_work_cli(tmp_path, _decision(name=None))
        r = _run_hook({"cwd": str(not_git)},
                      _guard_env(bin_dir, session, tmp_path / "no-work-repo"))
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_run_dir_outside_git_control(self, tmp_path, session):
        # Run dir exists but is not in a git repo: status/upstream unreadable.
        work_repo = tmp_path / "plain-work"
        run_dir = work_repo / HOST / PROJECT / "runs" / SLUG
        run_dir.mkdir(parents=True)
        (run_dir / "notes.md").write_text("looks dirty\n")
        bin_dir = _make_work_cli(tmp_path, _decision())
        r = _run_hook({"cwd": str(tmp_path)}, _guard_env(bin_dir, session, work_repo))
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_run_dir_without_upstream(self, tmp_path, session):
        # Clean repo with commits but no upstream: ahead count unreadable.
        work_repo = tmp_path / "no-upstream"
        run_dir = work_repo / HOST / PROJECT / "runs" / SLUG
        run_dir.mkdir(parents=True)
        _git(work_repo, "init", "-q", "-b", "main")
        (run_dir / "notes.md").write_text("artifact\n")
        _git(work_repo, "add", ".")
        _git(work_repo, "commit", "-q", "-m", "unpushable")
        bin_dir = _make_work_cli(tmp_path, _decision())
        r = _run_hook({"cwd": str(tmp_path)}, _guard_env(bin_dir, session, work_repo))
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_malformed_stdin_payload(self, tmp_path, session):
        bin_dir = _make_work_cli(tmp_path, _decision())
        env = _guard_env(bin_dir, session, tmp_path / "no-work-repo")
        r = _run_hook(None, env, raw_stdin="not json{")
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_non_dict_stdin_payload(self, tmp_path, session):
        # Even with a would-block decision, a non-dict payload exits early.
        bin_dir = _make_work_cli(tmp_path, _decision(name=None))
        env = _guard_env(bin_dir, session, tmp_path / "no-work-repo")
        r = _run_hook(None, env, raw_stdin="[1, 2, 3]")
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_empty_stdin(self, tmp_path, session):
        bin_dir = _make_work_cli(tmp_path, _decision())
        env = _guard_env(bin_dir, session, tmp_path / "no-work-repo")
        r = _run_hook(None, env, raw_stdin="")
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_sentinel_and_counter_io_errors_drop_both_nudges(self, tmp_path):
        # A session id with a slash points both marker files into a directory
        # that does not exist, so both bookkeeping writes raise OSError; the
        # hook must drop both reasons rather than nudge unboundedly.
        session = f"no-such-dir-{uuid.uuid4().hex}/x"
        ws = _make_workspace(tmp_path, active=True)
        work_repo, run_dir = _make_work_repo(tmp_path)
        (run_dir / "scratch.md").write_text("uncommitted\n")
        bin_dir = _make_work_cli(tmp_path, _decision(name=None))
        r = _run_hook({"cwd": str(ws)}, _guard_env(bin_dir, session, work_repo))
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_unreadable_counter_suppresses_push_nudge(self, tmp_path, session):
        work_repo, run_dir = _make_work_repo(tmp_path)
        (run_dir / "scratch.md").write_text("uncommitted\n")
        Path(COUNTER_TEMPLATE.format(session=session)).write_text("not-a-number")
        bin_dir = _make_work_cli(tmp_path, _decision())
        r = _run_hook({"cwd": str(tmp_path)}, _guard_env(bin_dir, session, work_repo))
        assert r.returncode == 0
        assert r.stdout.strip() == ""


# ---- settings.json wiring (drift guard) -------------------------------------------

class TestSettingsWiring:
    def test_stop_hook_registered_in_settings(self):
        settings = json.loads(
            (REPO_ROOT / "agent-files" / "claude" / "settings.json").read_text())
        stop_hooks = settings.get("hooks", {}).get("Stop", [])
        commands = [
            h.get("command", "")
            for group in stop_hooks for h in group.get("hooks", [])
        ]
        assert any("run_state_guard.py" in c for c in commands), (
            "Stop hook for run_state_guard.py missing from settings.json")
