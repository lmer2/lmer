"""A local-path target is a plain project session; `--name`/`--goal` ride flag-only.

Two host-CLI changes land together and are pinned here, end to end:

- **The target a local checkout exports.** `lmer chat .` (or `./`, `../x`, an
  absolute checkout path) resolves the repository from the checkout's git
  origin, and the container used to receive the raw spelling as
  `LMER_TASK_TARGET` — so every slug copy (`run_state.derive_slug`,
  `clone_and_exec._derive_run_slug`) filed the run under `chat-.` instead of
  the bare taskdef `chat`. The host now exports an **empty** target for a
  local path — delivered empty, never absent, because every container reader
  treats `""` as "no target" while an absent variable falls back to whatever
  the environment holds — and the derivations themselves do not move.
- **A run's name and goal from the launch.** `--name`/`--goal` travel as
  `LMER_RUN_NAME`/`LMER_RUN_GOAL`, flag-only like `LMER_ANSWER`: a value the
  host happens to export is never forwarded, a multi-line goal rides the
  `-e NAME` marker leg, and the host refuses what the container could not
  record (an empty value, a newline in a name, a launch with no run context).

The delivered-env tests run the real `main()` under the standard mock stack
(`tests/test_lmer_cli_slack_target.py`) with the real repository resolver put
back, from a scratch git checkout carrying a fake origin, and hand the env
dict `main()` built to the real `build_container_env` — so what is asserted
is what the runtime would be given, after the None filtering, on the leg the
value actually takes.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from lmer_cli import resolve
from lmer_cli.cli import _validate_parsed_args, container_task_target, main, parse_args
from lmer_cli.container import clone_and_exec
from lmer_cli.runtime import build_container_env
from tests.schema2_fixtures import git
from tests.test_container_env_transport import _parsed_env_file
from tests.test_lmer_cli_slack_target import _BASE_ENV, _make_main_mocks
from work_repo import run_state

ORIGIN = "https://gitlab.example.com/group/project"
ISSUE_URL = f"{ORIGIN}/-/issues/12"
MR_URL = f"{ORIGIN}/-/merge_requests/7"
SSH_URL = "git@gitlab.example.com:group/project"
EXEC_NOTE = (
    "ℹ️  --name/--goal are recorded by work session-start; "
    "this --exec command must run it for them to apply"
)


@pytest.fixture
def host_checkout(tmp_path) -> Path:
    """A scratch git checkout whose `origin` names the project under test."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git(checkout, "init", "-q")
    git(checkout, "remote", "add", "origin", ORIGIN)
    return checkout


@pytest.fixture
def stub_taskdefs(tmp_path) -> Path:
    """An external taskdef root holding a `chat` taskdef, so the host resolves
    the task without the developer checkout's built-in `taskdef/`."""
    root = tmp_path / "taskdefs"
    (root / "chat").mkdir(parents=True)
    (root / "chat" / "instructions.txt").write_text("stub chat taskdef\n", encoding="utf-8")
    return root


def _delivered_launch(argv, *, cwd: Path, taskdefs: Path, host_env=None):
    """Run `lmer <argv>` from *cwd* and return `(rc, env-file leg, marker leg)`.

    The real `main()` under `_make_main_mocks`, with the repository resolver
    the mock stack replaces put back so a local path resolves to its origin
    **and** a host path (the shape `lmer chat .` really has). `HOME` points at
    a scratch directory so no developer `~/.lmer/.env` is merged into the
    launch, and the external taskdef root (`LMER_TASKDEF_PATHS`, the variable
    the host reads) carries the stub `chat` taskdef; the mount builder for it
    is faked in the real shape — no mount arguments, the container-side
    `/Agents/taskdefs/<n>` paths — because the mock stack's stub returns a
    bare list that `main()` cannot unpack.
    """
    captured: dict = {}
    real_normalize = resolve.normalize_repo_url

    def external_taskdef_mounts(_runtime, paths):
        return [], [f"/Agents/taskdefs/{index}" for index in range(len(paths))]

    env = {
        **_BASE_ENV,
        "HOME": str(cwd.parent / "home"),
        "LMER_TASKDEF_PATHS": str(taskdefs),
        **(host_env or {}),
    }
    with patch.dict(os.environ, env, clear=True):
        with _make_main_mocks(captured_env=captured):
            with (
                patch("lmer_cli.cli.resolve.normalize_repo_url", real_normalize),
                patch("lmer_cli.cli.build_external_taskdef_mounts", external_taskdef_mounts),
                contextlib.chdir(cwd),
            ):
                rc = main(list(argv))
    container_env = build_container_env(captured)
    try:
        return rc, _parsed_env_file(container_env), dict(container_env.client_env)
    finally:
        container_env.cleanup()


class TestContainerTaskTarget:
    """The one pure rule: a resolved host path means an empty exported target."""

    @pytest.mark.parametrize("target", [".", "./", "../x", "/abs/repo"])
    def test_a_local_path_exports_an_empty_target(self, target, tmp_path):
        assert container_task_target(target, tmp_path) == ""

    @pytest.mark.parametrize("target", [ORIGIN, SSH_URL, ISSUE_URL, MR_URL])
    def test_a_url_target_is_exported_unchanged(self, target):
        assert container_task_target(target, None) == target

    def test_no_target_stays_none(self):
        assert container_task_target(None, None) is None


class TestWhatALocalPathLaunchDelivers:
    def test_the_target_is_delivered_empty_not_absent(self, host_checkout, stub_taskdefs):
        rc, delivered, marker = _delivered_launch(
            ["chat", "."], cwd=host_checkout, taskdefs=stub_taskdefs
        )
        assert rc == 0
        # Present on the env-file leg as `LMER_TASK_TARGET=`: an absent variable
        # would let the container fall back to whatever its environment holds.
        assert "LMER_TASK_TARGET" in delivered
        assert delivered["LMER_TASK_TARGET"] == ""
        assert "LMER_TASK_TARGET" not in marker
        assert delivered["LMER_TASK"] == "chat"

    def test_the_repository_still_comes_from_the_checkouts_origin(
        self, host_checkout, stub_taskdefs
    ):
        _, delivered, _ = _delivered_launch(
            ["chat", "."], cwd=host_checkout, taskdefs=stub_taskdefs
        )
        assert delivered["LMER_REPO_HOST"] == "gitlab.example.com"
        assert delivered["LMER_REPO_PROJECT"] == "group/project"

    def test_a_url_target_is_delivered_as_given(self, host_checkout, stub_taskdefs):
        _, delivered, _ = _delivered_launch(
            ["chat", ISSUE_URL], cwd=host_checkout, taskdefs=stub_taskdefs
        )
        assert delivered["LMER_TASK_TARGET"] == ISSUE_URL


class TestWhatTheNameAndGoalFlagsDeliver:
    def test_the_flags_are_delivered_as_run_name_and_run_goal(
        self, host_checkout, stub_taskdefs
    ):
        rc, delivered, _ = _delivered_launch(
            ["chat", ".", "--name", "my-run", "--goal", "ship the index"],
            cwd=host_checkout, taskdefs=stub_taskdefs,
        )
        assert rc == 0
        assert delivered["LMER_RUN_NAME"] == "my-run"
        assert delivered["LMER_RUN_GOAL"] == "ship the index"

    def test_a_host_exported_value_is_never_forwarded_without_the_flag(
        self, host_checkout, stub_taskdefs
    ):
        """The `LMER_ANSWER` rule: one-shot launch data, never standing
        configuration — a stale export must not name every future run."""
        _, delivered, marker = _delivered_launch(
            ["chat", "."], cwd=host_checkout, taskdefs=stub_taskdefs,
            host_env={"LMER_RUN_NAME": "stale-name", "LMER_RUN_GOAL": "stale goal"},
        )
        for key in ("LMER_RUN_NAME", "LMER_RUN_GOAL"):
            assert key not in delivered
            assert key not in marker

    def test_a_multi_line_goal_rides_the_marker_leg(self, host_checkout, stub_taskdefs):
        goal = "first line\nsecond line"
        _, delivered, marker = _delivered_launch(
            ["chat", ".", "--goal", goal], cwd=host_checkout, taskdefs=stub_taskdefs,
        )
        assert marker["LMER_RUN_GOAL"] == goal
        assert "LMER_RUN_GOAL" not in delivered


class TestTheHostRefusesWhatTheContainerCannotRecord:
    @staticmethod
    def _args(*extra):
        ns, _ = parse_args(["chat", "https://example.com/x/y", *extra])
        return ns

    @pytest.mark.parametrize("flags", [
        pytest.param(("--name", ""), id="empty-name"),
        pytest.param(("--name", "   "), id="blank-name"),
        pytest.param(("--name", "two\nlines"), id="newline-in-name"),
        pytest.param(("--goal", ""), id="empty-goal"),
        pytest.param(("--goal", "  \n "), id="blank-goal"),
    ])
    def test_an_empty_value_or_a_newline_in_a_name_is_exit_2(self, flags, capsys):
        assert _validate_parsed_args(self._args(*flags)) == 2
        assert flags[0] in capsys.readouterr().out

    @pytest.mark.parametrize("flags", [
        pytest.param(("--name", "my-run"), id="name"),
        pytest.param(("--goal", "ship it"), id="goal"),
    ])
    def test_either_flag_with_no_task_is_exit_2(self, flags, capsys):
        ns, _ = parse_args(["--no-task", "--exec", "true", *flags])
        assert _validate_parsed_args(ns) == 2
        assert "--no-task" in capsys.readouterr().out

    @pytest.mark.parametrize("flags", [
        pytest.param(("--name", "my-run"), id="name"),
        pytest.param(("--goal", "ship it"), id="goal"),
    ])
    def test_either_flag_with_exec_is_accepted_with_a_note(self, flags, capsys):
        assert _validate_parsed_args(self._args("--exec", *flags)) is None
        assert EXEC_NOTE in capsys.readouterr().out

    def test_a_plain_launch_carries_no_note(self, capsys):
        assert _validate_parsed_args(self._args("--name", "my-run", "--goal", "ship it")) is None
        assert "--name/--goal" not in capsys.readouterr().out

    def test_the_flags_default_to_none(self):
        ns = self._args()
        assert ns.run_name is None and ns.run_goal is None


class TestTheDerivationsDidNotMove:
    """R3: the host exports a different value; no slug copy changes."""

    def test_an_empty_target_derives_the_bare_taskdef_in_both_copies(self):
        assert run_state.derive_slug("chat", "") == "chat"
        assert clone_and_exec._derive_run_slug("chat", "") == "chat"

    def test_a_dot_target_still_derives_the_dotted_slug(self):
        assert run_state.derive_slug("chat", ".") == "chat-."
        assert clone_and_exec._derive_run_slug("chat", ".") == "chat-."
