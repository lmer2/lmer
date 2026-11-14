"""Ratchet is told how to set up time tracking for whoever asked (issue #668).

The operator wants to say "set up time tracking for me" and have it done. What
the prose must carry (docs/TIME-TRACKING.md#settings): the asker is listed with ``time me``, whose
id comes from the request; registry edits beyond the asker are the operator's;
a handler definition is written in Ratchet's own work-repo clone, checked with
the daemon's parser before it is pushed, and counts once the mirror has it; and
no token ever passes through Ratchet or the bus.
"""
import shlex
import textwrap

import pytest

from lmer_platform import assistant, ctl
from lmer_timesheet import parse_handler
from tests.test_orchestrate_time_tracking import (
    PLATFORM_SECTION, REACHABLE, WORKSPACE, _clean_lmer_env, render, rendered_section,  # noqa: F401
)
from tests.test_platform_assistant import orchestrate_section_of

HEADING = "### Time: set up time tracking for whoever asked"


def setup_prose():
    section = orchestrate_section_of(HEADING)
    body = section.split(HEADING, 1)[1]
    return " ".join(body.split(" ### ", 1)[0].split())


@pytest.mark.parametrize("environment", [{**REACHABLE, **WORKSPACE}, REACHABLE], ids=["workspace", "no-workspace"])
def test_every_assistant_with_the_api_reads_the_setup_section(monkeypatch, environment):
    rendered = render(monkeypatch, environment)
    assert HEADING in rendered
    section = rendered_section(rendered, HEADING)
    assert section.startswith(PLATFORM_SECTION)
    assert "skip the rest of this section" not in section


@pytest.mark.parametrize("environment", [{"LMER_PLATFORM_UNREACHABLE": "no route"}, {}],
                         ids=["unreachable", "no-env-file"])
def test_a_session_without_the_api_is_not_told_to_set_up_time(monkeypatch, environment):
    assert HEADING not in render(monkeypatch, environment)


def test_the_asker_is_listed_from_the_request_and_the_rest_is_the_operators():
    prose = setup_prose()
    assert "ask only for what is missing" in prose
    assert "lmer-ctl time me --key <key> --name '<name>'" in prose
    assert "Their id is taken from the request" in prose
    assert "Platform settings → Time tracking" in prose
    assert "Finish with one line" in prose


def test_a_handler_definition_is_written_checked_pushed_and_confirmed_from_the_mirror():
    prose = setup_prose()
    assert "timesheet.d/.handlers/<name>.yaml" in prose and "/work" in prose
    assert "lmer-ctl time handler check /work/timesheet.d/.handlers/<name>.yaml" in prose
    assert "`git pull --rebase` and push" in prose
    assert "once the mirror has pulled" in prose and "`handler_problems`" in prose


def test_the_example_handler_is_one_the_parser_accepts():
    raw = (assistant.taskdef_dir() / "instructions.txt").read_text(encoding="utf-8")
    block = raw.split(HEADING, 1)[1].split("```yaml\n", 1)[1].split("```", 1)[0]
    example = textwrap.dedent(block)
    handler, problems = parse_handler(example, path="board.yaml")
    assert handler is not None and problems == [], problems


def test_no_secret_goes_through_ratchet():
    prose = setup_prose()
    assert "Secrets never go through you or the bus" in prose
    assert "LMER_PLATFORM_TIME_WRITER_TOKEN=<token>" in prose and "~/.lmer/.env" in prose
    assert "Never ask them to paste either in the thread" in prose
    assert "clickup" not in prose.lower()


@pytest.mark.parametrize("argv", [
    "time settings",
    "time me --key sam --name 'Sam'",
    'time project set --handler board --client acme --idle-grace-minutes 20',
    "time handler check /work/timesheet.d/.handlers/board.yaml",
])
def test_the_command_lines_it_teaches_are_ones_the_cli_accepts(argv, tmp_path, monkeypatch):
    if argv.startswith("time handler check"):
        target = tmp_path / "board.yaml"
        target.write_text("name: board\n", encoding="utf-8")
        argv = f"time handler check {target}"
    args = ctl.create_parser().parse_args(shlex.split(argv))
    assert args.call(args).path.startswith("/api/time/")
