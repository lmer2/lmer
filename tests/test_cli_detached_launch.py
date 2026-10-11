"""``lmer``'s launch is create, then follow (#444).

Runs the real ``main()`` with the runtime faked (the harness from
``test_lmer_cli_slack_target``); here the create call and the follow are
observed rather than stubbed away.
"""

import json
import os
import re
from pathlib import Path
from unittest.mock import patch

from lmer_cli import cli, container_follow
from lmer_cli.runtime import build_container_env
from work_repo import run_state
from tests.test_lmer_cli_slack_target import _BASE_ENV, _make_main_mocks, REPO_URL

ARGV = ["--no-task", "--exec", "true", REPO_URL]


def _cidfile(run):
    return Path(run[run.index("--cidfile") + 1])


_read_container_id = container_follow.read_container_id


def launch(*, create_rc=0, write_cid=True, follow_rc=0, extra_env=None, conflict=False):
    """Run main() and return (rc, what was seen). With *conflict*, the first
    create finds its name taken."""
    seen = {"removed": [], "followed": [], "creates": []}

    def real_transport(env):
        container_env = build_container_env({**env, "LMER_PROBE": "secret-ish"})
        seen["env_dir"] = container_env.env_file_dir
        return container_env

    def create(run, **kwargs):
        seen["creates"].append(run[run.index("--name") + 1])
        if conflict and len(seen["creates"]) == 1:
            return 125
        seen["create"] = run
        seen["env_file_during_create"] = (
            seen["env_dir"] is not None and seen["env_dir"].exists()
        )
        if write_cid:
            _cidfile(run).write_text("c0ffee")
        return create_rc

    def follow(runtime, container, **kwargs):
        seen["followed"].append((container, kwargs))
        seen["env_file_during_follow"] = seen["env_dir"].exists()
        return follow_rc

    with patch.dict(os.environ, {**_BASE_ENV, **(extra_env or {})}, clear=True):
        with _make_main_mocks():
            with patch("lmer_cli.cli.read_container_id", side_effect=_read_container_id), \
                 patch("lmer_cli.container_follow.read_container_id",
                       side_effect=_read_container_id), \
                 patch("lmer_cli.cli.build_container_env", side_effect=real_transport), \
                 patch("lmer_cli.cli.subprocess.call", side_effect=create), \
                 patch("lmer_cli.container_follow.name_taken",
                       side_effect=lambda rt, name: conflict), \
                 patch("lmer_cli.cli.follow_container", side_effect=follow), \
                 patch("lmer_cli.cli.remove_container",
                       side_effect=lambda rt, c: seen["removed"].append(c)):
                from lmer_cli.cli import main

                rc = main(list(ARGV))
    return rc, seen


def test_the_container_is_created_labelled_then_followed():
    rc, seen = launch(follow_rc=7)
    assert rc == 7, "lmer exits with the container's code"
    run = seen["create"]
    assert "--rm" not in run and "--detach-keys" not in run
    assert "io.lmer.managed=1" in run and "io.lmer.launcher=cli" in run
    name = run[run.index("--name") + 1]
    assert seen["followed"] == [
        ("c0ffee", {"started": False, "interactive": False, "detach_note": None,
                    "name": name})
    ]


def test_a_no_task_cli_container_is_named_with_a_random_suffix():
    _, seen = launch()
    run = seen["create"]
    name = run[run.index("--name") + 1]
    assert re.fullmatch(r"lmer-[0-9a-f]{6}", name)
    assert run.index("--name") < run.index("lmer-test-image:latest")


def test_a_cli_run_is_named_by_the_slug_the_container_will_derive():
    assert cli._cli_run_slug("develop", "https://gitlab.example.com/a/b/-/issues/7") == (
        run_state.derive_slug("develop", "https://gitlab.example.com/a/b/-/issues/7")
    )
    assert cli._cli_run_slug("develop", None) == "develop"
    assert cli._cli_run_slug(None, "https://gitlab.example.com/a/b/-/issues/7") is None


def test_a_platform_launch_reports_its_container_to_the_platform(tmp_path):
    facts = tmp_path / "facts.json"
    _, seen = launch(extra_env={
        "LMER_PLATFORM_SESSION_ID": "s-20261009T152516-ff2b",
        "LMER_PLATFORM_RUN_SLUG": "chat-mr-389",
        "LMER_PLATFORM_PORTS_FILE": str(facts),
    })
    run = seen["create"]
    assert run[run.index("--name") + 1] == "lmer-chat-mr-389-ff2b"
    assert json.loads(facts.read_text())["container"] == {
        "id": "c0ffee", "name": "lmer-chat-mr-389-ff2b",
    }
    assert seen["followed"][0][1]["name"] == "lmer-chat-mr-389-ff2b"


def test_a_failed_create_reports_no_container(tmp_path):
    facts = tmp_path / "facts.json"
    launch(create_rc=125, extra_env={"LMER_PLATFORM_PORTS_FILE": str(facts)})
    assert not facts.exists() or "container" not in json.loads(facts.read_text())


def test_the_env_file_is_gone_before_the_session_runs():
    """It held every forwarded secret for the whole session; now only create
    needs it."""
    _, seen = launch()
    assert seen["env_file_during_create"] is True
    assert seen["env_file_during_follow"] is False


def test_a_failed_create_removes_what_it_left_and_never_follows():
    rc, seen = launch(create_rc=125)
    assert rc == 125
    assert seen["removed"] == ["c0ffee"]
    assert seen["followed"] == []
    assert not seen["env_dir"].exists()


def test_a_create_without_a_container_id_is_an_error():
    rc, seen = launch(write_cid=False)
    assert rc == 125 and seen["followed"] == []


def test_platform_launches_carry_the_session_labels():
    _, seen = launch(extra_env={
        "LMER_PLATFORM_SESSION_ID": "s-1", "LMER_PLATFORM_RUN_SLUG": "develop-1",
        "LMER_PLATFORM_INSTANCE": "abc",
    })
    run = seen["create"]
    for label in ("io.lmer.launcher=platform", "io.lmer.platform.session=s-1",
                  "io.lmer.run.slug=develop-1", "io.lmer.platform.instance=abc"):
        assert label in run


def test_a_detach_note_only_when_this_process_hosts_thread_services():
    class Handler:
        hosts_connectors = True

    assert cli._detach_note(True, [Handler()], None)
    assert cli._detach_note(True, [], object())
    assert cli._detach_note(True, [], None) is None
    assert cli._detach_note(False, [Handler()], object()) is None


def test_a_taken_name_is_retried_and_the_new_name_is_the_one_used(tmp_path):
    facts = tmp_path / "facts.json"
    rc, seen = launch(conflict=True, extra_env={
        "LMER_PLATFORM_SESSION_ID": "s-20261009T152516-ff2b",
        "LMER_PLATFORM_RUN_SLUG": "chat-mr-389",
        "LMER_PLATFORM_PORTS_FILE": str(facts),
    })
    first, second = seen["creates"]
    assert first == "lmer-chat-mr-389-ff2b"
    assert second.startswith("lmer-chat-mr-389-") and second != first
    assert rc == 0
    assert json.loads(facts.read_text())["container"]["name"] == second
    assert seen["followed"][0][1]["name"] == second
