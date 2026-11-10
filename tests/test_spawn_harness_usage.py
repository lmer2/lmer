"""Fan-out children's usage (#647 Q6).

claude and pi children keep text mode and keep their transcript in the child's
own ``.lmer-children/<child>/`` directory; codex children run in JSON mode,
whose output must rebuild the bytes text mode prints. Either way the caller
sees what it saw before, the parent's record counts the child, and the
parent's own transcripts and chat never include it. Fixtures and the
per-harness evidence: ``tests/fixtures/usage/children/README.md``.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from lmer_cli.container import spawn_harness
from lmer_cli.harness import HARNESSES, build_exec_argv
from lmer_usage import buckets, children, record
from lmer_usage.summarise import summarise
from tests.test_schema_v2_sessions import seed_launch, session_tree  # noqa: F401
from tests.test_work_usage import LEGACY_SESSION, _legacy, no_gate, transcripts  # noqa: F401
from tests.timing_budgets import SETTLE
from work_repo import cli as work_cli
from work_repo import run_state, session_record, usage

BIN_WRAPPER = Path(__file__).parent.parent / "bin" / "spawn-harness"
USAGE_FIXTURES = Path(__file__).parent / "fixtures" / "usage"
FIXTURES = USAGE_FIXTURES / "children"
CODEX = FIXTURES / "codex"
#: A claude child's transcript, with ids distinct from the parent's fixture so
#: that reading it as the parent's would change the parent's counts.
CLAUDE_MAIN = FIXTURES / "claude" / "main.jsonl"
CLAUDE_SUB = FIXTURES / "claude" / "subagents" / "agent-abc123"
PI_SESSION = USAGE_FIXTURES / "pi" / "session-tree.jsonl"
CASES = json.loads((CODEX / "cases.json").read_text(encoding="utf-8"))
MODEL = "gpt-5.6-sol"
CONFIG = {
    "sol-review": {"env": {"LMER_HARNESS": "codex", "LMER_LLM_NAME": MODEL}},
    "opus-review": {"env": {"LMER_HARNESS": "claude", "LMER_LLM_NAME": "opus"}},
    "pi-review": {"env": {"LMER_HARNESS": "pi"}},
}
#: The ``answer`` case's buckets, by the codex adapter's mapping.
ANSWER_COUNTERS = {"requests": 1, "input": 24763 - 24448, "cache_create": 0,
                   "cache_read": 24448, "output": 122, "thinking": 64}


def _case(name):
    case = CASES[name]
    return ((CODEX / case["stdout"]).read_bytes(), case["last_message"].encode(),
            case["text_mode_stdout"].encode())


def _extract(stdout: bytes, last_message, **kw):
    kw.setdefault("model", MODEL)
    return children.extract_codex(io.BytesIO(stdout), last_message, agent="sol-review",
                                  child_session="c" * 32, **kw)


def _counters(row):
    return {name: row[name] for name in buckets.COUNTERS}


class TestCodexExtraction:
    @pytest.mark.parametrize("name", sorted(CASES))
    def test_the_json_output_rebuilds_the_text_mode_bytes(self, name):
        stdout, last_message, expected = _case(name)
        assert _extract(stdout, last_message).text == expected

    def test_a_message_ending_in_a_newline_gets_a_second_one(self):
        # println! adds its newline unconditionally (README, template 0xf6f405e).
        _, last_message, expected = _case("trailing-newline")
        assert last_message.endswith(b"\n") and expected == last_message + b"\n"

    def test_usage_lands_in_the_codex_adapter_buckets(self):
        stdout, last_message, _ = _case("answer")
        [row] = _extract(stdout, last_message).rows
        assert _counters(row) == ANSWER_COUNTERS
        assert row["status"] == "collected"
        assert (row["harness"], row["model"], row["agent"], row["child_session"]) == (
            "codex", MODEL, "sol-review", "c" * 32)
        assert children.valid_row(row) == row

    def test_no_requested_model_is_unknown_and_an_event_model_wins(self):
        stdout, last_message, _ = _case("answer")
        assert _extract(stdout, last_message, model=None).rows[0]["model"] == "unknown"
        context = json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.5"}}).encode()
        assert _extract(context + b"\n" + stdout, last_message).rows[0]["model"] == "gpt-5.5"

    def test_the_last_cumulative_snapshot_wins(self):
        stdout, last_message, _ = _case("answer")
        earlier = json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 1}}).encode()
        [row] = _extract(earlier + b"\n" + stdout, last_message).rows
        assert {k: v for k, v in _counters(row).items() if k != "requests"} == {
            k: v for k, v in ANSWER_COUNTERS.items() if k != "requests"}

    def test_rollout_style_token_count_lines_are_read_too(self):
        line = json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 100, "cached_input_tokens": 40,
                                  "output_tokens": 7, "reasoning_output_tokens": 2}}}})
        [row] = _extract(line.encode() + b"\n", b"done here").rows
        assert _counters(row) == {"requests": 1, "input": 60, "cache_create": 0,
                                  "cache_read": 40, "output": 7, "thinking": 2}

    def test_no_usage_keeps_the_text_and_is_not_collected(self):
        stdout, last_message, expected = _case("no-usage")
        result = _extract(stdout, last_message)
        assert result.text == expected
        [row] = result.rows
        assert (row["status"], row["reason"]) == ("not_collected", "no usage in child output")
        assert buckets.is_zero(row) and row["requests"] == 0

    def test_unparseable_output_passes_raw_and_is_not_collected(self):
        result = _extract(b"plain text answer\n", None)
        assert result.text is None
        assert result.rows[0]["status"] == "not_collected"

    def test_unparseable_output_still_uses_a_written_final_message(self):
        assert _extract(b"not json\n", b"the answer").text == b"the answer\n"

    def test_an_empty_final_message_prints_nothing(self):
        stdout, _, _ = _case("answer")
        assert _extract(stdout, b"").text == b""

    def test_without_the_file_the_last_agent_message_is_the_answer(self):
        # A missing or unreadable last-message file must not drop a successful answer.
        stdout, last_message, expected = _case("answer")
        assert _extract(stdout, None).text == expected

    def test_a_failed_turn_names_its_cause_as_text_mode_does(self):
        """JSON mode reports a failure only as stdout events; text mode prints
        ``ERROR: <message>`` on stderr. The cause must still reach the caller."""
        stdout = (CODEX / "turn-failed.jsonl").read_bytes()
        said = []
        result = _extract(stdout, None, on_error=said.append)
        assert result.text == b""
        assert said[-1] == "ERROR: unexpected status 401 Unauthorized: Missing bearer or basic authentication"
        assert said[0].startswith("ERROR: Reconnecting... 1/5 (unexpected status 401")
        assert result.rows[0]["status"] == "not_collected"

    def test_a_last_line_cut_off_mid_write_keeps_the_answer_and_usage(self):
        """A child killed by a timeout leaves a partial last event and, most
        likely, no last-message file."""
        stdout, _, expected = _case("answer")
        assert stdout.endswith(b"\n")
        result = _extract(stdout + b'{"type":"turn.completed","usage":{"input_tokens":10', None)
        assert result.text == expected
        [row] = result.rows
        assert row["status"] == "collected" and _counters(row) == ANSWER_COUNTERS

    def test_the_reviewers_repro_keeps_the_parsed_message(self):
        s = children.CodexStream()
        s.feed(b'{"type":"item.completed","item":{"type":"agent_message","text":"ANSWER"}}\n')
        s.feed(b'{"type":"turn.completed","usage":{"input_tokens":10')
        result = s.finish(None, model="m", agent="a", child_session="x" * 32)
        assert result.text == b"ANSWER\n"
        assert result.rows[0]["reason"] == "no usage in child output"

    def test_output_that_never_was_events_still_passes_raw(self):
        assert _extract(b"plain text answer, no newline", None).text is None
        stdout, _, _ = _case("answer")
        assert _extract(b"not json\n" + stdout, None).text is None

    def test_no_partial_line_is_kept_once_the_output_is_not_events(self):
        s = children.CodexStream()
        s.feed(b"not json\n")
        for _ in range(3):
            s.feed(b"x" * 1000)
        assert s._partial == b""

    def test_the_stream_is_parsed_as_it_arrives_not_held(self, monkeypatch):
        monkeypatch.setattr(children, "RAW_LIMIT", 64)
        stdout, last_message, expected = _case("answer")
        capture = children.stream("codex")
        for n in range(0, len(stdout), 7):
            capture.feed(stdout[n:n + 7])
        assert capture.raw is None
        result = capture.finish(None, model=MODEL, agent="sol-review", child_session="c" * 32)
        assert result.text == expected and _counters(result.rows[0]) == ANSWER_COUNTERS


class TestArgv:
    def test_codex_capture_flags_sit_before_the_prompt_sentinel(self):
        argv, _ = build_exec_argv(HARNESSES["codex"], "p", model=MODEL, unattended=True,
                                  last_message_file="/tmp/x/last-message")
        assert argv[:7] == ["codex", "exec", "--skip-git-repo-check", "--ephemeral",
                            "--dangerously-bypass-approvals-and-sandbox", "--json",
                            "--output-last-message"]
        assert argv[7] == "/tmp/x/last-message"
        assert argv[-2:] == ["--", "p"]

    def test_without_a_last_message_file_the_argv_is_unchanged(self):
        argv, _ = build_exec_argv(HARNESSES["codex"], "p", unattended=True)
        assert "--json" not in argv

    @pytest.mark.parametrize("name", sorted(HARNESSES))
    def test_capture_flags_exist_exactly_where_a_parser_does(self, name):
        assert bool(HARNESSES[name].exec_profile.usage_capture_args) == children.supports(name)

    @pytest.mark.parametrize("name,dropped,added", [
        ("claude", "--no-session-persistence", ["--session-id", "SID"]),
        ("pi", "--no-session", ["--session-dir", "/d/.lmer-children/x"]),
    ])
    def test_keeping_a_transcript_swaps_only_the_persistence_flag(self, name, dropped, added):
        plain, _ = build_exec_argv(HARNESSES[name], "p", unattended=True)
        kept, _ = build_exec_argv(HARNESSES[name], "p", unattended=True, transcript={
            "session_id": "SID", "session_dir": "/d/.lmer-children/x"})
        assert dropped in plain and dropped not in kept
        assert [a for a in kept if a not in added] == [a for a in plain if a != dropped]
        start = kept.index(added[0])
        assert kept[start:start + 2] == added


HOME_DIRS = {"claude": ".claude/projects", "pi": ".pi/agent/sessions", "codex": ".codex/sessions"}


def _session_dir(tmp_path, harness):
    return tmp_path / "home" / HOME_DIRS[harness]


def _manifests(tmp_path, harness):
    root = _session_dir(tmp_path, harness) / children.CHILDREN_DIRNAME
    return {p.parent.name: json.loads(p.read_text()) for p in sorted(root.glob("*/child.json"))}


def _fake_codex(tmp_path, case="answer", *, json_mode=True, exit_code=0, sleep=0,
                stderr="", last_message=None, write_file=True):
    """A codex on PATH: JSON mode prints the fixture stream and writes the
    final-message file; otherwise it prints what text mode would."""
    stdout, message, text = _case(case)
    if last_message is not None:
        message, text = last_message.encode(), last_message.encode() + b"\n"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    data = tmp_path / "fake-codex.json"
    data.write_text(json.dumps({
        "stdout": stdout.decode(), "message": message.decode(), "text": text.decode(),
        "json_mode": json_mode, "exit": exit_code, "sleep": sleep, "stderr": stderr,
        "write_file": write_file,
    }))
    script = fake_bin / "codex"
    script.write_text(f"""#!{sys.executable}
import json, os, sys, time
spec = json.load(open({str(data)!r}))
args = sys.argv[1:]
if spec["json_mode"] and "--json" in args:
    if spec["write_file"]:
        with open(args[args.index("--output-last-message") + 1], "wb") as handle:
            handle.write(spec["message"].encode())
    sys.stdout.buffer.write(spec["stdout"].encode())
else:
    sys.stdout.buffer.write(spec["text"].encode())
sys.stdout.flush()
# Recorded after the stream is flushed, and atomically, so a waiter knows both.
call = {str(tmp_path / 'codex-call.json')!r}
json.dump({{"argv": args, "env": dict(os.environ)}}, open(call + ".tmp", "w"))
os.replace(call + ".tmp", call)
if spec["stderr"]:
    print(spec["stderr"], file=sys.stderr)
time.sleep(spec["sleep"])
sys.exit(spec["exit"])
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return fake_bin, text


ANSWER = b"the full answer from the child\n\n"


def _fake_transcript_harness(tmp_path, binary):
    """A claude or pi on PATH that writes a fixture transcript where its flags
    say and prints a text-mode answer."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    script = fake_bin / binary
    script.write_text(f"""#!{sys.executable}
import json, os, shutil, sys
from pathlib import Path
args = sys.argv[1:]
json.dump({{"argv": args, "env": dict(os.environ)}}, open({str(tmp_path / (binary + '-call.json'))!r}, "w"))
if "--session-id" in args:
    sid = args[args.index("--session-id") + 1]
    project = Path(os.environ["HOME"]) / ".claude" / "projects" / "-workspace"
    project.mkdir(parents=True, exist_ok=True)
    shutil.copy({str(CLAUDE_MAIN)!r}, project / (sid + ".jsonl"))
    subagents = project / sid / "subagents"
    subagents.mkdir(parents=True)
    shutil.copy({str(CLAUDE_SUB) + '.jsonl'!r}, subagents / "agent-abc123.jsonl")
    shutil.copy({str(CLAUDE_SUB) + '.meta.json'!r}, subagents / "agent-abc123.meta.json")
if "--session-dir" in args:
    directory = Path(args[args.index("--session-dir") + 1])
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copy({str(PI_SESSION)!r}, directory / "2026-11-05T08-00-00-000Z_sess.jsonl")
sys.stdout.buffer.write({ANSWER!r})
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return fake_bin


def _spawn(tmp_path, args, fake_bin, *, keep_lmer=False, extra_env=None):
    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "LMER_SPAWN_AGENTS": ",".join(CONFIG),
        "LMER_SPAWN_AGENTS_CONFIG": json.dumps(CONFIG),
        **(extra_env or {}),
    }
    (tmp_path / "home").mkdir(exist_ok=True)
    with pytest.MonkeyPatch.context() as mp:
        if not keep_lmer:
            for key in list(os.environ):
                if key.startswith("LMER_"):
                    mp.delenv(key, raising=False)
        for key, value in env.items():
            mp.setenv(key, value)
        with pytest.raises(SystemExit) as exc:
            spawn_harness.main(args)
    return exc.value.code


def _expected_claude_rows(tmp_path, agent, child_session):
    """The fixture transcript's rows, by the claude adapter, attributed to the child."""
    lay = tmp_path / "expected" / "-workspace"
    (lay / "s" / "subagents").mkdir(parents=True)
    shutil.copy(CLAUDE_MAIN, lay / "s.jsonl")
    shutil.copy(f"{CLAUDE_SUB}.jsonl", lay / "s" / "subagents" / "agent-abc123.jsonl")
    shutil.copy(f"{CLAUDE_SUB}.meta.json", lay / "s" / "subagents" / "agent-abc123.meta.json")
    rows = summarise([lay / "s.jsonl", lay / "s" / "subagents" / "agent-abc123.jsonl"]).row_list()
    return _attributed(rows, agent, child_session)


def _expected_pi_rows(agent, child_session):
    return _attributed(summarise([PI_SESSION]).row_list(), agent, child_session)


def _attributed(rows, agent, child_session):
    for row in rows:
        row["agent"] = agent if row["agent"] is None else f"{agent}/{row['agent']}"
        row["child_session"] = child_session
    return sorted(rows, key=lambda r: (r["agent"], r["model"]))


def _sorted(rows):
    return sorted(rows, key=lambda r: (r["agent"], r["model"]))


class TestCodexEndToEnd:
    def test_output_file_gets_the_text_mode_bytes(self, tmp_path):
        fake_bin, text = _fake_codex(tmp_path)
        out = tmp_path / "out.md"
        assert _spawn(tmp_path, ["sol-review", "--prompt", "p", "--output", str(out)],
                      fake_bin, extra_env={"LMER_SESSION_ID": "parent-1"}) == 0
        assert out.read_bytes() == text
        call = json.loads((tmp_path / "codex-call.json").read_text())
        assert "--json" in call["argv"]
        assert call["env"]["LMER_USAGE_CHILD"] == "1"
        [(child, data)] = _manifests(tmp_path, "codex").items()
        assert (data["parent_session"], data["child_session"], data["transcript"]) == (
            "parent-1", child, False)
        [row] = data["rows"]
        assert _counters(row) == ANSWER_COUNTERS
        assert (row["agent"], row["model"], row["child_session"]) == ("sol-review", MODEL, child)

    def test_stdout_gets_the_text_mode_bytes(self, tmp_path, capfdbinary):
        fake_bin, text = _fake_codex(tmp_path)
        assert _spawn(tmp_path, ["sol-review", "--prompt", "p"], fake_bin) == 0
        assert capfdbinary.readouterr().out == text
        assert len(_manifests(tmp_path, "codex")) == 1

    def test_text_mode_and_json_mode_children_look_the_same(self, tmp_path, capfdbinary):
        fake_bin, _ = _fake_codex(tmp_path, json_mode=False)
        _spawn(tmp_path, ["sol-review", "--prompt", "p"], fake_bin)
        plain = capfdbinary.readouterr().out
        fake_bin, _ = _fake_codex(tmp_path)
        _spawn(tmp_path, ["sol-review", "--prompt", "p"], fake_bin)
        assert capfdbinary.readouterr().out == plain

    def test_a_child_that_ignores_json_mode_passes_through_raw(self, tmp_path):
        fake_bin, text = _fake_codex(tmp_path, json_mode=False)
        out = tmp_path / "out.md"
        assert _spawn(tmp_path, ["sol-review", "--prompt", "p", "--output", str(out)], fake_bin) == 0
        assert out.read_bytes() == text
        [data] = _manifests(tmp_path, "codex").values()
        assert data["rows"][0]["status"] == "not_collected"

    def test_no_usage_keeps_the_text(self, tmp_path):
        fake_bin, text = _fake_codex(tmp_path, "no-usage")
        out = tmp_path / "out.md"
        assert _spawn(tmp_path, ["sol-review", "--prompt", "p", "--output", str(out)], fake_bin) == 0
        assert out.read_bytes() == text
        [data] = _manifests(tmp_path, "codex").values()
        assert data["rows"][0]["reason"] == "no usage in child output"

    def test_the_degenerate_check_reads_the_rebuilt_text(self, tmp_path, capsys):
        fake_bin, _ = _fake_codex(tmp_path, last_message="ok")
        out = tmp_path / "out.md"
        assert _spawn(tmp_path, ["sol-review", "--prompt", "p", "--output", str(out)], fake_bin) == 0
        assert out.read_text().startswith("ok\n\n---\n[spawn-harness] child produced NO USABLE OUTPUT")
        assert "below the" in capsys.readouterr().err

    def test_the_uncaptured_degenerate_check_reads_the_rebuilt_text(self, tmp_path, capsys):
        fake_bin, _ = _fake_codex(tmp_path, last_message="ok")
        assert _spawn(tmp_path, ["sol-review", "--prompt", "p"], fake_bin) == 0
        assert "no --output was given" in capsys.readouterr().err

    def test_a_failed_child_footer_follows_the_text(self, tmp_path):
        fake_bin, text = _fake_codex(tmp_path, exit_code=3, stderr="boom from codex")
        out = tmp_path / "out.md"
        assert _spawn(tmp_path, ["sol-review", "--prompt", "p", "--output", str(out)], fake_bin) == 3
        content = out.read_bytes()
        assert content.startswith(text + b"\n---\n[spawn-harness] child FAILED: exit code 3")
        assert b"boom from codex" in content

    def test_a_failed_turn_names_its_cause_in_the_footer(self, tmp_path, capsys):
        fake_bin, _ = _fake_codex(tmp_path, "turn-failed", exit_code=1, write_file=False)
        out = tmp_path / "out.md"
        assert _spawn(tmp_path, ["sol-review", "--prompt", "p", "--output", str(out)], fake_bin) == 1
        content = out.read_text()
        assert content.startswith("\n---\n[spawn-harness] child FAILED: exit code 1")
        assert "ERROR: unexpected status 401 Unauthorized" in content.split("stderr tail:")[1]
        assert "ERROR: unexpected status 401 Unauthorized" in capsys.readouterr().err

    def test_an_answer_survives_a_missing_last_message_file(self, tmp_path):
        fake_bin, text = _fake_codex(tmp_path, write_file=False)
        out = tmp_path / "out.md"
        assert _spawn(tmp_path, ["sol-review", "--prompt", "p", "--output", str(out)], fake_bin) == 0
        assert out.read_bytes() == text

    def test_a_timed_out_child_footer_follows_the_text(self, tmp_path):
        fake_bin, text = _fake_codex(tmp_path, sleep=30)
        out = tmp_path / "out.md"
        args = ["sol-review", "--prompt", "p", "--output", str(out), "--timeout", "1"]
        assert _spawn(tmp_path, args, fake_bin) == 124
        assert out.read_bytes().startswith(text + b"\n---\n[spawn-harness] child FAILED: timed out")

    def test_sigterm_still_kills_and_footers_a_captured_child(self, tmp_path):
        fake_bin, text = _fake_codex(tmp_path, sleep=30)
        out = tmp_path / "out.md"
        (tmp_path / "home").mkdir()
        wrapper = subprocess.Popen(
            [str(BIN_WRAPPER), "sol-review", "--prompt", "p", "--output", str(out)],
            env={"PATH": f"{fake_bin}:/usr/bin:/bin", "HOME": str(tmp_path / "home"),
                 "LMER_PYTHON": sys.executable, "LMER_SPAWN_AGENTS_CONFIG": json.dumps(CONFIG)},
            stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + SETTLE
            while not (tmp_path / "codex-call.json").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            wrapper.send_signal(signal.SIGTERM)
            _, stderr = wrapper.communicate(timeout=SETTLE)
        finally:
            if wrapper.poll() is None:
                wrapper.kill()
                wrapper.wait()
        assert wrapper.returncode == 128 + signal.SIGTERM
        assert "interrupted — child killed" in stderr
        assert out.read_bytes().startswith(text + b"\n---\n[spawn-harness] child FAILED: spawn-harness interrupted")
        assert len(_manifests(tmp_path, "codex")) == 1

    def test_the_capture_directory_is_removed(self, tmp_path, monkeypatch):
        made = []
        real = spawn_harness.tempfile.mkdtemp
        monkeypatch.setattr(spawn_harness.tempfile, "mkdtemp",
                            lambda **kw: made.append(real(**kw)) or made[-1])
        fake_bin, _ = _fake_codex(tmp_path)
        _spawn(tmp_path, ["sol-review", "--prompt", "p"], fake_bin)
        assert len(made) == 1 and not Path(made[0]).exists()


class TestTranscriptChildren:
    def test_a_claude_child_keeps_text_mode_and_its_transcript_moves_into_its_dir(self, tmp_path):
        fake_bin = _fake_transcript_harness(tmp_path, "claude")
        out = tmp_path / "out.md"
        assert _spawn(tmp_path, ["opus-review", "--prompt", "p", "--output", str(out)], fake_bin) == 0
        assert out.read_bytes() == ANSWER
        [(child, data)] = _manifests(tmp_path, "claude").items()
        argv = json.loads((tmp_path / "claude-call.json").read_text())["argv"]
        assert "--no-session-persistence" not in argv and "--output-format" not in argv
        sid = argv[argv.index("--session-id") + 1]
        assert sid == children.transcript_id(child)
        projects = _session_dir(tmp_path, "claude")
        # Nothing is left where the parent's own conversation lives.
        assert sorted(p.name for p in (projects / "-workspace").iterdir()) == []
        moved = projects / children.CHILDREN_DIRNAME / child / "-workspace"
        assert (moved / f"{sid}.jsonl").is_file()
        assert (moved / sid / "subagents" / "agent-abc123.jsonl").is_file()
        assert (data["transcript"], data["rows"], data["agent"]) == (True, [], "opus-review")
        rows = children.read_rows([projects / children.CHILDREN_DIRNAME], parent_session=None)
        assert _sorted(rows) == _expected_claude_rows(tmp_path, "opus-review", child)

    def test_a_pi_child_writes_straight_into_its_dir(self, tmp_path):
        fake_bin = _fake_transcript_harness(tmp_path, "pi")
        assert _spawn(tmp_path, ["pi-review", "--prompt", "p", "--output", str(tmp_path / "o")],
                      fake_bin) == 0
        assert (tmp_path / "o").read_bytes() == ANSWER
        [(child, data)] = _manifests(tmp_path, "pi").items()
        argv = json.loads((tmp_path / "pi-call.json").read_text())["argv"]
        root = _session_dir(tmp_path, "pi") / children.CHILDREN_DIRNAME
        assert "--no-session" not in argv
        assert argv[argv.index("--session-dir") + 1] == str(root / child)
        assert data["transcript"] is True
        rows = children.read_rows([root], parent_session=None)
        assert _sorted(rows) == _expected_pi_rows("pi-review", child)

    def test_a_claude_childs_stdout_is_untouched(self, tmp_path, capfdbinary):
        fake_bin = _fake_transcript_harness(tmp_path, "claude")
        assert _spawn(tmp_path, ["opus-review", "--prompt", "p"], fake_bin) == 0
        assert capfdbinary.readouterr().out == ANSWER

    def test_a_child_that_wrote_no_transcript_is_not_collected(self, tmp_path):
        root = tmp_path / "projects" / children.CHILDREN_DIRNAME
        children.write_manifest(root / ("a" * 32), {
            "parent_session": "p", "child_session": "a" * 32, "harness": "claude",
            "agent": "opus-review", "model": "opus", "transcript": True, "rows": []})
        [row] = children.read_rows([root], parent_session="p")
        assert (row["status"], row["reason"], row["model"]) == (
            "not_collected", "no child transcript", "opus")

    def test_an_unsupported_harness_is_one_not_collected_row(self, tmp_path):
        capture = spawn_harness.ChildUsage(harness="drop-in", agent="x", child_session="b" * 32,
                                           root=tmp_path / "fallback")
        assert spawn_harness.run_child([sys.executable, "-c", "print('a real answer here')"], {
            **os.environ, "LMER_WORK_REPO_PATH": str(tmp_path / "nowhere")},
            str(tmp_path / "out"), 30, heartbeat=0, usage=capture) == 0
        [row] = children.read_rows([tmp_path / "fallback"], parent_session=os.environ.get("LMER_SESSION_ID"))
        assert (row["status"], row["reason"]) == ("not_collected", "usage capture unsupported for drop-in")

    def test_a_child_that_never_started_leaves_no_record(self, tmp_path):
        capture = spawn_harness.ChildUsage(harness="claude", agent="x", child_session="c" * 32,
                                           root=tmp_path / "root")
        with pytest.raises(SystemExit):
            spawn_harness.run_child([str(tmp_path / "missing-binary")], {
                **os.environ, "LMER_WORK_REPO_PATH": str(tmp_path / "nowhere")},
                None, 30, heartbeat=0, usage=capture)
        assert not (tmp_path / "root" / ("c" * 32)).exists()


def _parent_with_claude_transcript(monkeypatch, tmp_path):
    """A legacy-form parent whose own claude transcripts and children's dirs
    live where spawn-harness puts them for this ``HOME``."""
    rdir = _legacy(monkeypatch, tmp_path)
    home = tmp_path / "home"
    projects = home / HOME_DIRS["claude"]
    shutil.copytree(USAGE_FIXTURES / "claude" / "session-a", projects / "-workspace" / "session-a")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(usage, "session_dirs", lambda: [
        projects, home / HOME_DIRS["pi"], home / HOME_DIRS["codex"]])
    return rdir


def _day_sum(data):
    total = buckets.zero()
    for counters in data["days"].values():
        buckets.add(total, counters)
    return total


def _parent_rows(data):
    return [r for r in data["models"] if not r.get("child_session")]


class TestParentRecord:
    def test_spawned_children_are_counted_exactly_and_only_as_children(
        self, monkeypatch, tmp_path, no_gate,
    ):
        _parent_with_claude_transcript(monkeypatch, tmp_path)
        before = yaml_load(usage.checkpoint())
        _fake_transcript_harness(tmp_path, "pi")
        fake_bin = _fake_transcript_harness(tmp_path, "claude")
        _fake_codex(tmp_path)
        for agent in ("opus-review", "pi-review", "sol-review"):
            assert _spawn(tmp_path, [agent, "--prompt", "p", "--output", str(tmp_path / agent)],
                          fake_bin, keep_lmer=True) == 0
        data = yaml_load(usage.checkpoint())
        assert record.validate(data) == []
        # The parent's own rows are untouched: no child file was read as the parent's.
        assert _parent_rows(data) == _parent_rows(before)
        [claude_child] = _manifests(tmp_path, "claude")
        [pi_child] = _manifests(tmp_path, "pi")
        [codex_child] = _manifests(tmp_path, "codex")
        got = {cs: _sorted([r for r in data["models"] if r.get("child_session") == cs])
               for cs in (claude_child, pi_child, codex_child)}
        assert got[claude_child] == _expected_claude_rows(tmp_path, "opus-review", claude_child)
        assert got[pi_child] == _expected_pi_rows("pi-review", pi_child)
        assert [_counters(r) for r in got[codex_child]] == [ANSWER_COUNTERS]
        expected = dict(before["totals"])
        for rows in got.values():
            for row in rows:
                buckets.add(expected, row)
        assert data["totals"] == expected
        # Children are in the per-day split too, which is what the rollups sum.
        assert _day_sum(before) == before["totals"] and _day_sum(data) == data["totals"]

    def test_a_final_pass_that_cannot_recount_keeps_children_once(self, monkeypatch, tmp_path, no_gate):
        _parent_with_claude_transcript(monkeypatch, tmp_path)
        fake_bin, _ = _fake_codex(tmp_path)
        assert _spawn(tmp_path, ["sol-review", "--prompt", "p", "--output", str(tmp_path / "out")],
                      fake_bin, keep_lmer=True) == 0
        checkpointed = yaml_load(usage.checkpoint())
        assert any(r.get("child_session") for r in checkpointed["models"])

        def slow(files):
            time.sleep(5)

        data = yaml_load(usage.finalise(0, summarise_fn=slow, timeout=0.2))
        assert data["partial"] is True and record.validate(data) == []
        assert (data["totals"], data["days"]) == (checkpointed["totals"], checkpointed["days"])
        assert _day_sum(data) == data["totals"]

    def test_an_in_flight_claude_child_is_the_childs_not_the_parents(
        self, monkeypatch, tmp_path, no_gate,
    ):
        _parent_with_claude_transcript(monkeypatch, tmp_path)
        before = yaml_load(usage.checkpoint())
        projects = tmp_path / "home" / HOME_DIRS["claude"]
        child = "d" * 32
        sid = children.transcript_id(child)
        children.write_manifest(projects / children.CHILDREN_DIRNAME / child, {
            "parent_session": LEGACY_SESSION, "child_session": child, "harness": "claude",
            "agent": "opus-review", "model": "opus", "transcript": True, "rows": []})
        shutil.copy(CLAUDE_MAIN, projects / "-workspace" / f"{sid}.jsonl")
        assert all(sid not in str(p) for p in usage.session_files())
        data = yaml_load(usage.checkpoint())
        assert _parent_rows(data) == _parent_rows(before)
        rows = [r for r in data["models"] if r.get("child_session") == child]
        assert rows and {r["agent"] for r in rows} == {"opus-review"}

    def test_another_parents_children_are_not_counted(self, monkeypatch, tmp_path, no_gate):
        _parent_with_claude_transcript(monkeypatch, tmp_path)
        root = tmp_path / "home" / HOME_DIRS["codex"] / children.CHILDREN_DIRNAME
        stdout, last_message, _ = _case("answer")
        for child, parent in (("e" * 32, LEGACY_SESSION), ("f" * 32, "another-session")):
            rows = [dict(r, child_session=child) for r in _extract(stdout, last_message).rows]
            children.write_manifest(root / child, {
                "parent_session": parent, "child_session": child, "harness": "codex",
                "agent": "sol-review", "model": MODEL, "transcript": False, "rows": rows})
        data = yaml_load(usage.checkpoint())
        assert record.validate(data) == []
        assert [r["child_session"] for r in data["models"] if r.get("child_session")] == ["e" * 32]

    def test_a_childs_session_end_writes_no_record(self, monkeypatch, tmp_path, transcripts):
        rdir = _legacy(monkeypatch, tmp_path)
        child_env = spawn_harness.build_child_env(dict(os.environ), {}, {})
        assert child_env["LMER_USAGE_CHILD"] == "1"
        monkeypatch.setenv("LMER_USAGE_CHILD", child_env["LMER_USAGE_CHILD"])
        with patch("work_repo.cli.commit_work_path", return_value=0):
            assert work_cli.cmd_session_end(0) == 0
        assert not (rdir / "ai-usage").exists()

    @pytest.mark.parametrize("source", ["preset", "flag"])
    def test_the_child_marker_cannot_be_unset(self, source):
        overlay = {"LMER_USAGE_CHILD": "0"}
        env = spawn_harness.build_child_env(
            {}, overlay if source == "preset" else {}, overlay if source == "flag" else {})
        assert env["LMER_USAGE_CHILD"] == "1"


def yaml_load(path):
    assert path is not None, "checkpoint wrote nothing"
    return yaml.safe_load(Path(path).read_text())


def test_a_schema2_child_is_named_by_its_own_uid_and_writes_no_record(
    session_tree, tmp_path, monkeypatch,  # noqa: F811
):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LMER_SESSION_ID", "")
    monkeypatch.setenv(session_record.METADATA_ENV, "{}")
    metadata, _, _ = seed_launch()
    child_env = spawn_harness.build_child_env(dict(os.environ), {}, {})
    root = tmp_path / "home" / ".lmer-usage" / "children"
    capture = spawn_harness.ChildUsage(harness="drop-in", agent="opus-review",
                                       child_session="9" * 32, root=root)
    assert spawn_harness.run_child(
        [sys.executable, "-c", "print('a real answer here')"], child_env,
        str(tmp_path / "out.txt"), 60, heartbeat=0, agent_name="opus-review", usage=capture,
    ) == 0
    data = json.loads((root / ("9" * 32) / children.MANIFEST_NAME).read_text())
    assert data["parent_session"] == metadata["uid"]
    [child_dir] = [p for p in session_tree.rglob("9" * 32) if p.is_dir()]
    assert session_record.signed_out(run_state.read_events(child_dir, last_n=0))
    assert list(session_tree.rglob("ai-usage")) == []


class TestManifestSafety:
    def _row(self, **kw):
        return {"harness": "codex", "model": MODEL, "agent": "a", "child_session": "c" * 32,
                "status": "collected", **ANSWER_COUNTERS, **kw}

    def _write(self, root, rows, *, child="c" * 32, parent="p", **extra):
        return children.write_manifest(root / child, {
            "parent_session": parent, "child_session": child, "harness": "codex",
            "agent": "a", "model": MODEL, "transcript": False, "rows": rows, **extra})

    def test_files_and_directories_are_private(self, tmp_path):
        path = self._write(tmp_path / "s" / children.CHILDREN_DIRNAME, [self._row()])
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700
        assert [p.name for p in path.parent.iterdir()] == [children.MANIFEST_NAME]

    def test_an_unsafe_child_directory_is_refused(self, tmp_path):
        with pytest.raises(ValueError):
            children.write_manifest(tmp_path / "x" / "..", {})

    def test_the_fallback_root_is_resolved_at_call_time(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
        assert children.children_root(None) == tmp_path / "elsewhere" / ".lmer-usage" / "children"

    def test_symlinked_garbage_foreign_and_mismatched_manifests_are_skipped(self, tmp_path):
        root = tmp_path / "root"
        self._write(root, [self._row()])
        outside = tmp_path / "planted"
        self._write(outside, [self._row(child_session="e" * 32)], child="e" * 32)
        (root / ("e" * 32)).symlink_to(outside / ("e" * 32))
        (root / ("1" * 32)).mkdir()
        (root / ("1" * 32) / children.MANIFEST_NAME).write_text("{not json")
        self._write(root, [self._row(child_session="2" * 32)], child="2" * 32, parent="other")
        self._write(root, [self._row(child_session="4" * 32)], child="3" * 32)
        (root / "not-a-child").mkdir()
        rows = children.read_rows([root], parent_session="p")
        assert [r["child_session"] for r in rows] == ["c" * 32]

    def test_a_symlinked_root_is_refused(self, tmp_path):
        real = tmp_path / "real"
        self._write(real, [self._row()])
        (tmp_path / "link").symlink_to(real)
        assert children.read_rows([tmp_path / "link"], parent_session="p") == []

    @pytest.mark.parametrize("bad", [
        {"input": -1},
        {"thinking": 10_000},
        {"input": 1.5},
        {"input": True},
        {"status": "maybe"},
        {"agent": "x" * 501},
        {"model": 7},
        {"child_session": None},
        {"status": "not_collected"},
    ])
    def test_invalid_rows_are_skipped_and_valid_ones_kept(self, bad, tmp_path):
        self._write(tmp_path / "r", [self._row(**bad), self._row(agent="kept")])
        assert [r["agent"] for r in children.read_rows([tmp_path / "r"], parent_session="p")] == ["kept"]

    def test_rows_are_normalised_to_the_record_shape(self, tmp_path):
        self._write(tmp_path / "r", [self._row(extra="dropped")])
        [row] = children.read_rows([tmp_path / "r"], parent_session="p")
        assert "extra" not in row and set(row) == {
            "harness", "model", "agent", "child_session", "status", *buckets.COUNTERS}

    def test_manifest_rows_are_dated_by_the_manifest(self, tmp_path):
        path = self._write(tmp_path / "r", [self._row(), self._row(status="not_collected", **buckets.zero())])
        when = datetime(2026, 11, 4, 23, 30, tzinfo=timezone.utc).timestamp()
        os.utime(path, (when, when))
        rows, days = children.read([tmp_path / "r"], parent_session="p")
        assert len(rows) == 2 and days == {"2026-11-04": {**buckets.zero(), **ANSWER_COUNTERS}}

    def test_a_missing_root_reads_as_no_rows(self, tmp_path):
        assert children.read_rows([tmp_path / "absent"], parent_session="p") == []

    def test_moving_a_transcript_skips_a_symlink(self, tmp_path):
        session_dir = tmp_path / "projects"
        child = "c" * 32
        sid = children.transcript_id(child)
        (session_dir / "-workspace").mkdir(parents=True)
        (session_dir / "-workspace" / f"{sid}.jsonl").symlink_to(CLAUDE_MAIN)
        assert children.move_transcript(session_dir, child, session_dir / ".lmer-children" / child) == 0


def test_children_module_stays_neutral():
    source = (Path(children.__file__)).read_text()
    for package in ("lmer_platform", "work_repo", "lmer_cli"):
        assert f"import {package}" not in source and f"from {package}" not in source
