"""Exercise platform→CLI→container and independent harness→work boundaries."""

import json
import os
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from tests.conftest import strip_lmer_env

from tests.test_platform_answer import (
    platform_root, config, plant_schema_two_question, track_run,  # noqa: F401
    request_for, child_payload, HOST, PROJECT,
)
from tests.test_schema_v2_sessions import (  # noqa: F401
    session_tree, seed_launch, _run_container_session_start, _snapshot_record,
)
from work_repo import run_state, session_record


@pytest.fixture(autouse=True)
def isolated_launch_environment(monkeypatch, tmp_path):
    strip_lmer_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))


@pytest.fixture
def fake_lmer(tmp_path):
    """Real host argument/env assembly, with only host/runtime facilities stubbed."""
    script = tmp_path / "capturing-lmer"
    repo = Path(__file__).resolve().parents[1]
    script.write_text(f'''#!{sys.executable}
import json, os, sys
sys.path.insert(0, {str(repo)!r})
from tests.test_lmer_cli_slack_target import _make_main_mocks
from lmer_cli.cli import main
from lmer_cli.runtime import build_container_env
from tests.test_container_env_transport import _parsed_env_file
captured = {{}}
with _make_main_mocks(captured_env=captured):
    rc = main(sys.argv[1:])
transport = build_container_env(captured)
try:
    delivered = _parsed_env_file(transport)
    delivered.update(transport.client_env)
finally:
    transport.cleanup()
with open({str(tmp_path / 'delivered.json')!r}, 'w') as handle:
    json.dump({{'rc': rc, 'env': delivered}}, handle)
''')
    script.chmod(0o755)
    return script


@pytest.mark.parametrize("operation", ["resume", "answer"])
@pytest.mark.parametrize("signed_out", [True, False])
def test_platform_source_survives_real_cli_and_seeds_fresh_container_record(
    config, tmp_path, monkeypatch, operation, signed_out
):
    from lmer_platform import answer, resume
    from tests.test_lmer_cli_slack_target import _BASE_ENV

    source = plant_schema_two_question(config, signed_out=signed_out)
    if operation == "resume":
        path = source / "state.yaml"
        state = yaml.safe_load(path.read_text())
        state.update(stop_reason="yield", open_question=None)
        path.write_text(yaml.safe_dump(state))
    track_run()
    before = _snapshot_record(source)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for key, value in _BASE_ENV.items():
        if key != "HOME":
            monkeypatch.setenv(key, value)
    acknowledgement = None if signed_out else "a" * 32
    if operation == "answer":
        answer.answer_run(config, request_for(allow_unsigned_source=acknowledgement))
    else:
        resume.resume_run(config, resume.ResumeRequest(
            host=HOST, project=PROJECT, slug=source.parent.name,
            allow_unsigned_source=acknowledgement,
        ))
    delivered = child_payload(tmp_path / "delivered.json")
    assert delivered["rc"] == 0
    env = delivered["env"]
    assert env["LMER_SOURCE_SESSION_ID"] == "a" * 32
    assert env.get("LMER_SESSION_ID") != "a" * 32
    assert env.get("LMER_ALLOW_UNSIGNED_SOURCE") == acknowledgement
    work = tmp_path / "container-work"
    shutil.copytree(config.mirror_path, work)
    env.update(HOME=str(home), PATH=os.environ["PATH"],
               LMER_WORK_REPO_PATH=str(work), LMER_REPO_HOST=HOST,
               LMER_REPO_PROJECT=PROJECT, LMER_REPO_URL=f"https://{HOST}/{PROJECT}")
    captured = _run_container_session_start(env, tmp_path / "markers")
    assert captured["main_rc"] == 0
    assert captured["writer_uid"] != "a" * 32
    assert re.fullmatch(r"[0-9a-f]{32}", captured["writer_uid"])
    state = run_state.load_state(captured["record"])
    assert state["session"]["source_session_uid"] == "a" * 32
    if operation == "answer":
        events = run_state.read_events(captured["record"], last_n=0)
        answered = [event for event in events if event["type"] == "question_answered"]
        assert answered[-1]["data"]["question"]
        assert answered[-1]["data"]["answer"] == request_for().answer
    assert _snapshot_record(source) == before


def test_fresh_cli_drops_stale_source_and_acknowledgement(tmp_path):
    from tests.test_session_resume_loop import _delivered_container_env

    delivered = _delivered_container_env(host_env={
        "HOME": str(tmp_path), "LMER_SOURCE_SESSION_ID": "a" * 32,
        "LMER_ALLOW_UNSIGNED_SOURCE": "a" * 32,
    })
    assert not delivered.get("LMER_SOURCE_SESSION_ID")
    assert not delivered.get("LMER_ALLOW_UNSIGNED_SOURCE")


def test_independent_harness_children_have_own_identity_metadata_and_lifecycle(
    session_tree, tmp_path, monkeypatch
):
    from lmer_cli.container import spawn_harness

    parent, parent_dir, parent_state = seed_launch()
    # The parent is a NAMED run: its launch flags (`lmer --name/--goal`) are
    # still in its environment, and a child must not inherit them.
    parent_state["name"] = "parent-run"
    run_state.write_state(parent_dir, parent_state)
    before = _snapshot_record(parent_dir)
    env = dict(os.environ, HOME=str(tmp_path / "child-home"),
               LMER_SOURCE_SESSION_ID=parent["uid"],
               LMER_ALLOW_UNSIGNED_SOURCE=parent["uid"],
               LMER_SESSION_IS_SOURCE="1", LMER_ANSWER="stale answer",
               LMER_RUN_NAME="parent-run", LMER_RUN_GOAL="the parent's goal")
    env["LMER_SESSION_METADATA"] = json.dumps({
        **parent, "harness": "parent", "model": "parent-model",
        "extension": {"kept": True}, "config": {"future": {"x": 3}},
    })
    script = tmp_path / "child.py"
    script.write_text('''import json, os, sys
from pathlib import Path
from work_repo import cli, run_state
assert cli.cmd_log("The independent child wrote its own log", []) == 0
Path(sys.argv[1]).write_text(json.dumps({
    "uid": os.environ["LMER_SESSION_ID"], "pid": os.getpid(),
    "metadata": json.loads(os.environ["LMER_SESSION_METADATA"]),
    "source": os.environ.get("LMER_SOURCE_SESSION_ID"),
    "ack": os.environ.get("LMER_ALLOW_UNSIGNED_SOURCE"),
    "answer": os.environ.get("LMER_ANSWER"),
    "run_name": os.environ.get("LMER_RUN_NAME"),
    "run_goal": os.environ.get("LMER_RUN_GOAL"),
    "run_env": sorted(name for name in os.environ if name.startswith("LMER_RUN_")),
    "record": str(run_state.run_dir()),
}))
''')
    seen = []
    for number in range(2):
        child_env = spawn_harness.build_child_env(env, {
            "LMER_HARNESS": "codex", "LMER_LLM_NAME": f"child-model-{number}",
            "LMER_REASONING_EFFORT": "high",
        }, {})
        assert "LMER_RUN_NAME" not in child_env
        assert "LMER_RUN_GOAL" not in child_env
        dump = tmp_path / f"child-{number}.json"
        assert spawn_harness.run_child(
            [sys.executable, str(script), str(dump)], child_env,
            str(tmp_path / f"output-{number}.txt"), 20, heartbeat=0,
        ) == 0
        child = json.loads(dump.read_text())
        assert re.fullmatch(r"[0-9a-f]{32}", child["uid"])
        assert child["uid"] != parent["uid"]
        assert child["uid"] not in seen
        seen.append(child["uid"])
        assert child["source"] is child["ack"] is child["answer"] is None
        assert child["run_name"] is child["run_goal"] is None
        # The child's own environment dump, not just two lookups: a leaked
        # LMER_RUN_NAME would be refused as held by the parent and leave the
        # child's name null anyway, so absence of the variables is asserted
        # directly, and the goal (which a leak WOULD apply) is asserted null.
        assert child["run_env"] == []
        metadata = child["metadata"]
        assert metadata["pid"] == child["pid"]
        assert metadata["pid"] != parent["pid"]
        assert metadata["harness"] == "codex"
        assert metadata["model"] == f"child-model-{number}"
        assert metadata["effort"] == "high"
        assert metadata["extension"] == {"kept": True}
        assert metadata["config"]["future"] == {"x": 3}
        assert metadata["parent"]["uid"] == parent["uid"]
        directory = Path(child["record"])
        assert directory.name == child["uid"]
        assert (directory / "log.yaml").is_file()
        assert session_record.signed_out(run_state.read_events(directory, last_n=0))
        # The parent's name never reaches the child's record: the variables
        # were stripped, so the child's own session start applied nothing.
        child_state = run_state.load_state(directory)
        assert child_state["name"] is None
        assert child_state["goal"] is None
        assert run_state.load_state(parent_dir)["name"] == "parent-run"
        assert _snapshot_record(parent_dir) == before


def test_schema_one_child_retains_established_identity(session_tree, tmp_path):
    from lmer_cli.container import spawn_harness

    manifest = session_tree / "_plexus.yaml"
    manifest.write_text(manifest.read_text().replace("schema: 2", "schema: 1"))
    env = dict(os.environ, LMER_SESSION_ID="20260917-120000-1-42")
    result = spawn_harness.build_child_env(env, {}, {})
    assert result["LMER_SESSION_ID"] == env["LMER_SESSION_ID"]


@pytest.mark.parametrize("ack", [None, "b" * 32])
@pytest.mark.parametrize("operation", ["resume", "answer"])
def test_platform_unsigned_source_refuses_absent_or_wrong_ack(config, ack, operation):
    from lmer_platform import answer, resume, spawn

    source = plant_schema_two_question(config, signed_out=False)
    if operation == "resume":
        path = source / "state.yaml"
        state = yaml.safe_load(path.read_text())
        state.update(stop_reason="yield", open_question=None)
        path.write_text(yaml.safe_dump(state))
    track_run()
    before = _snapshot_record(source)
    with pytest.raises((resume.ResumeError, spawn.SpawnError)):
        if operation == "answer":
            answer.answer_run(config, request_for(allow_unsigned_source=ack))
        else:
            resume.resume_run(config, resume.ResumeRequest(
                host=HOST, project=PROJECT, slug=source.parent.name,
                allow_unsigned_source=ack,
            ))
    assert _snapshot_record(source) == before


def test_schema_two_lifecycle_output_does_not_hide_empty_harness_result(
    session_tree, tmp_path, capsys
):
    from lmer_cli.container import spawn_harness

    seed_launch()
    output = tmp_path / "empty.txt"
    child_env = spawn_harness.build_child_env(dict(os.environ), {}, {})
    assert spawn_harness.run_child(
        [sys.executable, "-c", "pass"], child_env, str(output), 10, heartbeat=0,
    ) == 0
    assert "NO USABLE OUTPUT" in output.read_text()


@pytest.mark.parametrize("continuation", [False, True], ids=["fresh", "continuation"])
def test_the_hook_prompt_carries_the_launch_name_and_goal(
    session_tree, monkeypatch, continuation
):
    """`lmer --name/--goal` reach the agent through the one channel it reads:
    `hooks/start.py` runs the real `work session-start` and renders its stdout
    into `taskdef/run-state.jinja2`. Persisting them to state.yaml is not
    enough — the prompt must name the run (so the naming step is skipped) and
    carry the launch goal as THE goal, a continuation's included."""
    import subprocess
    import types

    import jinja2

    from hooks import start as start_hook

    repo = Path(__file__).resolve().parents[1]
    if continuation:
        old, source, source_state = seed_launch()
        source_state["goal"] = "OLD goal"
        run_state.write_state(source, source_state)
        run_state.append_event(source, "session_end")
        monkeypatch.setenv("LMER_SOURCE_SESSION_ID", old["uid"])
    monkeypatch.setenv("LMER_RUN_NAME", "Fresh Run")
    monkeypatch.setenv("LMER_RUN_GOAL", "NEW goal")
    session_record.start_launch()

    real_run = subprocess.run

    def run_the_real_work_cli(argv, **kwargs):
        assert argv == ["work", "session-start"]
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(repo / "src"), str(repo), env.get("PYTHONPATH", "")]
        )
        return real_run([sys.executable, "-m", "work_repo.cli", *argv[1:]], env=env, **kwargs)

    monkeypatch.setattr(start_hook.shutil, "which", lambda _name: "/usr/bin/work")
    monkeypatch.setattr(start_hook, "subprocess", types.SimpleNamespace(run=run_the_real_work_cli))
    brief = start_hook.run_state_session_start()
    assert brief, "the hook dropped session-start's output"

    prompt = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(repo / "taskdef"))
    ).get_template("run-state.jinja2").render(
        LMER_REPO_HOST=os.environ["LMER_REPO_HOST"],
        LMER_REPO_PROJECT=os.environ["LMER_REPO_PROJECT"],
        run_state_brief=brief,
    )
    lines = prompt.splitlines()
    assert "Run name: fresh-run" in lines
    assert [line for line in lines if line.startswith("Goal:")] == ["Goal: NEW goal"]
    # The naming rule points at the line the agent now sees.
    assert "`Run name:`" in prompt
    if continuation:
        assert f"Context from session {old['uid']} (read-only):" in lines
        assert [line for line in lines if "OLD goal" in line] == [
            "Predecessor goal (context only, replaced by the launch goal): OLD goal"
        ]
    record = run_state.load_state(run_state.run_dir())
    assert (record["name"], record["goal"]) == ("fresh-run", "NEW goal")
