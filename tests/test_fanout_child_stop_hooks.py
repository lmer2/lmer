"""A fan-out child's Stop hooks stay silent (#323).

A `claude -p` child shares the parent's settings, so it runs the same Stop
hooks, and its captured output is only its last turn: a hook that blocks the
stop replaces the child's findings with its answer to the hook. Each hook is
run here as Claude Code would run it, first in the environment
:func:`spawn_harness.build_child_env` gives a child, then in the parent
environment it is derived from, where the same input must block (proving the
silence is the child marker's doing and not a setup that never triggers).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lmer_cli.container import spawn_harness
from tests import test_run_state_guard as run_state_tests
from tests import test_signal_guard as signal_tests
from tests import test_slack_reply_guard as slack_tests

REPO_ROOT = Path(__file__).parent.parent
SETTINGS = REPO_ROOT / "agent-files" / "claude" / "settings.json"


def _stop_hooks():
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    return [
        Path(hook["command"].split()[-1]).name
        for group in settings["hooks"]["Stop"]
        for hook in group["hooks"]
    ]


def _run_state_setup(tmp_path, session):
    ws = run_state_tests._make_workspace(tmp_path, active=True)
    bin_dir = run_state_tests._make_work_cli(tmp_path, run_state_tests._decision(name=None))
    env = run_state_tests._guard_env(bin_dir, session, tmp_path / "no-work-repo")
    return {"cwd": str(ws)}, env


def _slack_reply_setup(tmp_path, session):
    transcript = slack_tests._write_transcript(
        tmp_path, [slack_tests._user_text("q?"), slack_tests._assistant_text(slack_tests.LONG)]
    )
    return {"transcript_path": transcript}, {"LMER_CHAT_CHANNEL": "C123", "LMER_SESSION_ID": session}


def _signal_setup(tmp_path, session):
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    transcript = signal_tests._write_transcript(tmp_path, signal_tests.MILESTONE_TURN)
    env = signal_tests._guard_env(empty_bin, session, signal_tests._channel_dir(tmp_path))
    return {"transcript_path": transcript}, env


SETUPS = {
    "run_state_guard.py": _run_state_setup,
    "slack_reply_guard.py": _slack_reply_setup,
    "signal_guard.py": _signal_setup,
}

MARKERS = (
    run_state_tests.SENTINEL_TEMPLATE,
    run_state_tests.COUNTER_TEMPLATE,
    run_state_tests.LEDGER_SENTINEL_TEMPLATE,
    signal_tests.MARKER_TEMPLATE,
)


@pytest.fixture
def session():
    sid = f"pytest-fanout-{os.getpid()}-{id(object())}"
    yield sid
    for template in MARKERS:
        try:
            os.unlink(template.format(session=sid))
        except OSError:
            pass


def _run(hook, payload, env):
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "hooks" / hook)],
        input=json.dumps(payload),
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=60,
    )


def test_every_stop_hook_has_a_setup_here():
    """A new Stop hook must be checked against fan-out children too."""
    assert sorted(_stop_hooks()) == sorted(SETUPS)


def test_no_overlay_can_clear_the_child_marker(tmp_path):
    off = {spawn_harness.FANOUT_CHILD_ENV: "0"}
    parent = {**off, "LMER_WORK_REPO_PATH": str(tmp_path / "no-work-repo")}
    child = spawn_harness.build_child_env(parent, off, off)
    assert child[spawn_harness.FANOUT_CHILD_ENV] == "1"


@pytest.mark.parametrize("hook", sorted(SETUPS))
def test_a_fan_out_childs_stop_hook_stays_silent(tmp_path, session, hook):
    payload, extra = SETUPS[hook](tmp_path, session)
    parent = {k: v for k, v in os.environ.items() if not k.startswith("LMER_")}
    parent["LMER_WORK_REPO_PATH"] = str(tmp_path / "no-work-repo")
    parent.update(extra)
    child = spawn_harness.build_child_env(parent, {}, {})

    in_child = _run(hook, payload, child)
    assert in_child.returncode == 0, in_child.stderr
    assert in_child.stdout.strip() == "", in_child.stdout

    in_parent = _run(hook, payload, parent)
    assert json.loads(in_parent.stdout)["decision"] == "block"


@pytest.mark.parametrize("hook", ["run_state_guard.py", "slack_reply_guard.py"])
def test_a_headless_top_level_session_is_still_nudged(tmp_path, session, hook):
    """LMER_NONINTERACTIVE also marks cron launches, which are full sessions
    with a transcript; only the fan-out marker stands these hooks down."""
    payload, extra = SETUPS[hook](tmp_path, session)
    env = {k: v for k, v in os.environ.items() if not k.startswith("LMER_")}
    env.update(extra, LMER_NONINTERACTIVE="1")
    assert json.loads(_run(hook, payload, env).stdout)["decision"] == "block"
