"""Ratchet is told how to start, stop and check a person's time (#604, T11, G4).

The prose is only worth anything if the session that can act on it reads it, so
the section is checked rendered, in every environment an assistant is launched
with, and by the ``##`` section that holds it — a paragraph can render and still
sit under an instruction to skip the rest of its section.

What it has to say is the operator's decision P8: the daemon works out who asked
from the request, so Ratchet never names the person, and ``--record`` only picks
out a record on its own session. And spec D19's manual-start refusal: a missing
handler field is asked for in the thread, then retried with ``--field``.
"""
import re
import shlex

import pytest

from hooks.start import render_taskdef_template
from lmer_platform import assistant, ctl
from tests.conftest import strip_lmer_env
from tests.test_platform_assistant import orchestrate_section_of

HEADING = "### Time: start, stop or check it for whoever asked"
PLATFORM_SECTION = "The platform's API is how you see and steer the fleet"

REACHABLE = {"LMER_PLATFORM_URL": "http://host.containers.internal:8600"}
WORKSPACE = {"LMER_WORKSPACE_ID": "w-1", "LMER_WORKSPACE_NAME": "webshop"}


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


def render(monkeypatch, environment):
    """The orchestrate instructions as a session launched with *environment* reads them."""
    monkeypatch.setenv("LMER_TASKDEF_ROOT", str(assistant.taskdef_dir().parent))
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    return render_taskdef_template(
        assistant.taskdef_dir() / "instructions.txt",
        extra_context={"instructions_file": "/x"},
    )


def rendered_section(rendered, phrase):
    """The rendered ``##`` section holding *phrase*, whitespace-joined."""
    sections = re.split(r"^## ", rendered, flags=re.M)
    holding = [s for s in sections if phrase in " ".join(s.split())]
    assert len(holding) == 1, f"{phrase!r} is in {len(holding)} rendered sections, expected 1"
    return " ".join(holding[0].split())


def time_prose():
    """The time subsection on its own: from its heading to the next heading."""
    section = orchestrate_section_of(HEADING)
    body = section.split(HEADING, 1)[1]
    return re.split(r" #{2,3} ", body, maxsplit=1)[0]


@pytest.mark.parametrize("environment", [
    {**REACHABLE, **WORKSPACE},
    # An assistant with no named workspace still has the API and the CLI; the
    # section must not hide inside the `{% if LMER_WORKSPACE_ID %}` block.
    REACHABLE,
], ids=["workspace", "no-workspace"])
def test_every_assistant_with_the_api_reads_the_section(monkeypatch, environment):
    rendered = render(monkeypatch, environment)
    assert HEADING in rendered
    section = rendered_section(rendered, HEADING)
    assert section.startswith(PLATFORM_SECTION), (
        "the time section left the platform-API section, the one every session "
        "with a platform URL is given"
    )
    assert "skip the rest of this section" not in section, (
        "the section a session acts on tells it to skip the time instructions"
    )
    for verb in ("lmer-ctl time start", "lmer-ctl time stop", "lmer-ctl time status",
                 "lmer-ctl time set", "lmer-ctl time auto off", "lmer-ctl time auto on"):
        assert verb in section, f"{verb!r} does not reach a session that can run it"


@pytest.mark.parametrize("environment", [
    {"LMER_PLATFORM_UNREACHABLE": "bound to 127.0.0.1 and the runtime is docker"},
    {},
], ids=["unreachable", "no-env-file"])
def test_a_session_without_the_api_is_not_told_to_track_time(monkeypatch, environment):
    """Same rule as every other `lmer-ctl` verb: no URL, no route list."""
    rendered = render(monkeypatch, environment)
    assert HEADING not in rendered
    assert "/api/time/" not in rendered


def test_the_section_sits_in_the_platform_section_not_the_project_block():
    """Raw-file membership: the `## Your project` section is Jinja-guarded on a
    workspace id, so prose there would reach only some assistants."""
    section = orchestrate_section_of(HEADING)
    assert section.startswith(PLATFORM_SECTION)


def test_ratchet_never_names_the_person():
    prose = time_prose()
    assert "Never pass a person's name" in prose
    assert "the daemon resolves who asked from the request" in prose
    assert "--person" not in prose and "--name" not in prose, (
        "a flag for the person contradicts the rule it sits next to"
    )
    assert "`--record <id>` only names a specific record on your own session" in prose


def test_a_missing_handler_field_is_asked_for_then_retried():
    prose = time_prose()
    assert "`missing`" in prose, "the refusal's field list is not named"
    assert "shows none, ask the person for it in the thread" in prose
    assert "then retry with `--field key=value`" in prose
    assert "Never invent a value" in prose


def test_the_route_list_names_each_time_verb():
    """The route→verb list stays the fallback when the CLI is older than the daemon."""
    prose = orchestrate_section_of("`GET /api/time/status`")
    for route, verb in (
        ("GET /api/time/status", "lmer-ctl time status"),
        ("POST /api/time/start", "lmer-ctl time start"),
        ("POST /api/time/stop", "lmer-ctl time stop"),
        ("PATCH /api/time/current", "lmer-ctl time set"),
        ("PUT /api/time/auto", "lmer-ctl time auto"),
        ("GET /api/time/entries", "lmer-ctl time entries"),
    ):
        assert f"`{route}` " in prose and verb in prose, f"{route} is not paired with {verb}"


@pytest.mark.parametrize("argv", [
    'time start --project "$LMER_WORKSPACE_ID" --field task_id=abc123',
    'time status --project "$LMER_WORKSPACE_ID"',
    "time stop --record s-a/000004",
    "time set --title '#604'",
    "time auto off",
    "time auto on",
    "time start --prompt 7 --field task_id=abc123",
])
def test_the_command_lines_it_teaches_are_ones_the_cli_accepts(argv):
    """The prose's flags against the real parser, so the two cannot drift."""
    args = ctl.create_parser().parse_args(shlex.split(argv.replace('"$LMER_WORKSPACE_ID"', "w-1")))
    call = args.call(args)
    assert call.path.startswith("/api/time/")


def test_the_section_stays_tracker_neutral():
    assert "clickup" not in time_prose().lower()
    assert "clickup" not in orchestrate_section_of("`GET /api/time/status`").lower()


def test_the_run_issue_command_it_teaches_is_one_the_cli_accepts():
    argv = "runs meta set git.example.com/acme/widgets/fix-12 --issue https://git.example.com/acme/widgets/-/issues/12"
    args = ctl.create_parser().parse_args(shlex.split(argv))
    call = args.call(args)
    assert (call.path, call.body["issues"]) == (
        "/api/runs/meta", ["https://git.example.com/acme/widgets/-/issues/12"])
    assert "lmer-ctl runs meta set <key> --issue URL" in " ".join(
        orchestrate_section_of("`GET|POST /api/runs/meta`").split())


def test_prompts_per_issue_fields_and_a_persons_messages_are_taught():
    """#665: a prompt is answered with --prompt; a per_issue field is never
    retried from a recent value; a person's messages to Ratchet never split or retitle."""
    prose = " ".join(time_prose().split())
    assert "lmer-ctl time start --prompt ID --field key=value" in prose
    assert "A field marked `per_issue` belongs to one issue: never retry it with a recent value, only ask" in prose
    assert "A person's messages to you extend the running entry and never retitle it" in prose
    assert "once time has accrued, a retitle starts a new entry" in prose
