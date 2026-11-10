"""`work decision` / `work decisions`: who decided comes from records (#674)."""
import json
from unittest.mock import patch

import pytest

from ask_channel import bus
from work_repo import cli as work_cli
from work_repo import decisions, run_state
from tests.conftest import strip_lmer_env

ISSUE = "https://git.example.com/org/repo/-/issues/123"
SESSION = "s-dec-1"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture(autouse=True)
def _tmp_goal_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(work_cli, "GOAL_FILE_DIR", str(tmp_path / "goals"))


@pytest.fixture
def run_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
    monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
    monkeypatch.setenv("LMER_REPO_PROJECT", "org/repo")
    monkeypatch.setenv("LMER_TASK", "develop")
    monkeypatch.setenv("LMER_TASK_TARGET", ISSUE)
    monkeypatch.setenv("LMER_SESSION_ID", "s-cli-1")
    return tmp_path / "git.example.com" / "org/repo" / "runs" / "develop-issue-123"


def _record(seq, kind, channel, text, *, author=None, meta=None, in_reply_to=None):
    source = {"channel": channel}
    if author:
        source["author"] = author
    return {
        "id": f"{SESSION}/{seq:06d}", "seq": seq, "session": SESSION, "kind": kind,
        "source": source, "text": text, "meta": meta or {}, "in_reply_to": in_reply_to,
    }


class FakeBus:
    """The two reads `work decision --ref` makes, over a fixed log."""

    def __init__(self, records):
        self.records = records

    def messages(self, since=0, **_kw):
        return [r for r in self.records if r["seq"] > since], self.records[-1]["seq"]

    def question(self, ref):
        for record in self.records:
            if record["kind"] == "question" and record["id"] == ref:
                answers = [r["id"] for r in self.records
                           if r["kind"] == "answer" and r["in_reply_to"] == ref and not r["meta"].get("late")]
                return {"question": record, "answered_by": answers[0] if answers else None}
        return None


OWN_POST = _record(2, "message", "agent", "Starting work")
OPEN_QUESTION = _record(3, "question", "agent", "Still open?")
QUESTION = _record(4, "question", "agent", "Go with the proposal?", meta={"operator_only": True})
OPERATOR_ANSWER = _record(5, "answer", "platform", "Go", author="operator", in_reply_to=QUESTION["id"])
ASSISTANT_ANSWER = _record(6, "answer", "uber-lmer", "Drop it", meta={"authority": "full"},
                           in_reply_to=QUESTION["id"])
UNSTAMPED_ASSISTANT = _record(7, "message", "uber-lmer", "Do X")
PEER_STAMPED = _record(8, "message", "peer-ul", "Do Y", meta={"authority": "full"})
CONNECTOR_NOTE = _record(10, "note", "slack", "connector running")
OWN_ASSISTANT_POST = _record(11, "message", "uber-lmer", "my own post", meta={"session_origin": True})
LIFTED_ANSWER = _record(12, "message", "platform", "Ship it", author="operator")
LIFTED_ANSWER["source"]["ref"] = "ask-dir:000003"
LATE_ANSWER = _record(9, "answer", "slack", "Too late", author="tester", meta={"late": True},
                      in_reply_to=QUESTION["id"])
LOG = [OWN_POST, OPEN_QUESTION, QUESTION, OPERATOR_ANSWER, ASSISTANT_ANSWER, UNSTAMPED_ASSISTANT, PEER_STAMPED,
       LATE_ANSWER, CONNECTOR_NOTE, OWN_ASSISTANT_POST, LIFTED_ANSWER]


@pytest.fixture
def fake_bus(monkeypatch):
    fake = FakeBus(LOG)
    monkeypatch.setattr(bus, "resolve_endpoint", lambda env=None: object())
    monkeypatch.setattr(bus, "BusClient", lambda endpoint: fake)
    return fake


def _main(argv):
    with patch.object(work_cli.sys, "argv", ["work"] + argv):
        return work_cli.main()


def _events(rdir, kind):
    return [e for e in run_state.read_events(rdir, last_n=0) if e["type"] == kind]


class TestRecordFromBus:
    def test_operator_answer_records_question_and_who(self, run_env, fake_bus):
        assert _main(["decision", "--ref", "000005"]) == 0
        (event,) = _events(run_env, "question_answered")
        assert event["data"] == {
            "ref": OPERATOR_ANSWER["id"], "channel": "platform", "author": "operator",
            "by": "human", "authority": None, "label": "platform",
            "question": "Go with the proposal?", "question_ref": QUESTION["id"],
            "answer": "Go", "operator_only": True,
        }

    def test_assistant_answer_carries_its_stamped_authority(self, run_env, fake_bus):
        assert _main(["decision", "--ref", "6"]) == 0
        (event,) = _events(run_env, "question_answered")
        assert (event["data"]["by"], event["data"]["authority"]) == ("assistant", "full")

    def test_unstamped_assistant_message_carries_none(self, run_env, fake_bus):
        assert _main(["decision", "Do X it is", "--ref", "7"]) == 0
        (event,) = _events(run_env, "decision")
        assert event["note"] == "Do X it is"
        assert (event["data"]["by"], event["data"]["authority"]) == ("assistant", "none")
        assert event["data"]["message"] == "Do X"

    def test_a_peer_assistant_stamp_is_not_believed(self, run_env, fake_bus):
        assert _main(["decision", "Do Y", "--ref", "8"]) == 0
        assert _events(run_env, "decision")[0]["data"]["authority"] == "none"

    def test_a_question_id_records_its_accepted_answer(self, run_env, fake_bus):
        assert _main(["decision", "--ref", QUESTION["id"]]) == 0
        (event,) = _events(run_env, "question_answered")
        assert (event["data"]["ref"], event["data"]["answer"]) == (OPERATOR_ANSWER["id"], "Go")

    def test_an_unanswered_question_is_refused(self, run_env, fake_bus, capsys):
        assert _main(["decision", "--ref", "3"]) == 1
        assert "no accepted answer" in capsys.readouterr().err

    def test_the_agents_own_record_is_refused(self, run_env, fake_bus, capsys):
        assert _main(["decision", "mine", "--ref", "2"]) == 1
        assert "own" in capsys.readouterr().err
        assert not run_state.read_events(run_env, last_n=0)

    def test_an_assistant_sessions_own_post_is_refused(self, run_env, fake_bus, capsys):
        assert _main(["decision", "x", "--ref", "11"]) == 1
        assert "own" in capsys.readouterr().err

    def test_a_note_on_a_human_channel_is_not_a_decision(self, run_env, fake_bus, capsys):
        assert _main(["decision", "operator said go", "--ref", "10"]) == 1
        assert "is a note" in capsys.readouterr().err
        assert not _events(run_env, "decision")

    def test_another_sessions_full_id_never_resolves_locally(self, run_env, fake_bus, capsys):
        assert _main(["decision", "x", "--ref", "other-session/000007"]) == 1
        assert "no message" in capsys.readouterr().err
        assert _main(["decision", "x", "--ref", f"{SESSION}/000007"]) == 0

    @pytest.mark.parametrize("ref", ["²", "0", "-3", "abc", ""])
    def test_a_malformed_ref_is_refused_cleanly(self, run_env, fake_bus, capsys, ref):
        assert _main(["decision", "x", "--ref", ref]) == 1
        assert "not a message id" in capsys.readouterr().err

    @pytest.mark.parametrize("meta", [{}, {"poster": "session"}, {"poster": "platform"}, {"poster": "bot"}],
                             ids=["old-connector", "another-run", "post-button", "bot"])
    def test_a_thread_post_not_stamped_human_is_not_a_human(self, run_env, monkeypatch, meta, capsys):
        post = _record(2, "message", "slack", "use B", author="lmer worker-b", meta=meta)
        fake = FakeBus([post])
        monkeypatch.setattr(bus, "resolve_endpoint", lambda env=None: object())
        monkeypatch.setattr(bus, "BusClient", lambda endpoint: fake)
        assert _main(["decision", "Use B", "--ref", "000002"]) == 0
        (event,) = _events(run_env, "decision")
        assert event["data"]["by"] == "unknown"
        assert event["data"].get("poster") == meta.get("poster")
        assert "lmer worker-b via slack, not confirmed as a human" in capsys.readouterr().out

    @pytest.mark.parametrize("channel", ["slack", "matrix"])
    def test_a_thread_post_the_connector_stamped_human_is_a_human(self, run_env, monkeypatch, channel):
        answer = _record(5, "answer", channel, "Go", author="tester", meta={"poster": "human"},
                         in_reply_to=QUESTION["id"])
        fake = FakeBus([QUESTION, answer])
        monkeypatch.setattr(bus, "resolve_endpoint", lambda env=None: object())
        monkeypatch.setattr(bus, "BusClient", lambda endpoint: fake)
        assert _main(["decision", "--ref", "000005"]) == 0
        (event,) = _events(run_env, "question_answered")
        assert event["data"]["by"] == "human" and "poster" not in event["data"]

    def test_where_a_record_came_from_is_kept(self, run_env, fake_bus):
        assert _main(["decision", "Ship it", "--ref", "12"]) == 0
        assert _events(run_env, "decision")[0]["data"]["source_ref"] == "ask-dir:000003"

    def test_a_late_answer_is_refused(self, run_env, fake_bus, capsys):
        assert _main(["decision", "--ref", "9"]) == 1
        assert "late answer" in capsys.readouterr().err

    def test_a_message_ref_needs_what_was_decided(self, run_env, fake_bus, capsys):
        assert _main(["decision", "--ref", "7"]) == 1
        assert "say what was decided" in capsys.readouterr().err

    def test_unknown_ref_is_refused(self, run_env, fake_bus, capsys):
        assert _main(["decision", "x", "--ref", "42"]) == 1
        assert "no message 42" in capsys.readouterr().err

    def test_no_channel_is_an_error_not_an_agent_decision(self, run_env, monkeypatch, capsys):
        def no_channel(env=None):
            raise bus.NoChannel("no control plane")
        monkeypatch.setattr(bus, "resolve_endpoint", no_channel)
        assert _main(["decision", "x", "--ref", "5"]) == 1
        assert "no control plane" in capsys.readouterr().err
        assert not _events(run_env, "decision")


class TestAgentDecision:
    def test_without_ref_it_is_the_agents_own(self, run_env):
        assert _main(["decision", "Kept the reader read-only"]) == 0
        (event,) = _events(run_env, "decision")
        assert event["data"] == {"by": "agent"}

    def test_needs_text(self, run_env, capsys):
        assert _main(["decision"]) == 1
        assert "requires" in capsys.readouterr().err


class TestDecisionsReader:
    def _seed(self, rdir, slug, target, events):
        run_state.write_state(rdir, run_state.seed_state(slug, "develop", target))
        for kind, note, data in events:
            run_state.append_event(rdir, kind, note=note, data=data)

    def test_lists_tool_written_types_only_and_marks_who(self, run_env, capsys):
        self._seed(run_env, "develop-issue-123", ISSUE, [
            ("goals_frozen", "approved in chat", {"goals_hash": "x"}),
            ("question_answered", "fine", {"question": "Ship?", "answer": "fine"}),
            ("decision", "Read-only reader", {"by": "agent"}),
            ("operator_decision", "ad hoc, ignored", None),
            ("phase", "execution", None),
        ])
        assert _main(["decisions", "--json"]) == 0
        rows = json.loads(capsys.readouterr().out)["decisions"]
        assert [r["type"] for r in rows] == ["goals_frozen", "question_answered", "decision"]
        assert [r["by"] for r in rows] == [None, None, "agent"]
        assert [r["agent_only"] for r in rows] == [False, False, True]
        assert rows[1]["what"] == "Q: Ship? → A: fine"

    def test_text_names_who_and_marks_agent_only(self, run_env, capsys):
        self._seed(run_env, "develop-issue-123", ISSUE, [
            ("question_answered", "Go", {"question": "Q?", "answer": "Go", "ref": "a/1",
                                          "by": "human", "author": "operator",
                                          "channel": "platform", "label": "platform"}),
            ("decision", "Drop it", {"by": "assistant", "authority": "full", "channel": "uber-lmer",
                                     "label": "Ratchet", "ref": "a/2"}),
            ("decision", "Mine", {"by": "agent"}),
            ("goals_frozen", "", {}),
        ])
        assert _main(["decisions"]) == 0
        out = capsys.readouterr().out
        assert "A: Go (to: Q?) — operator via platform" in out
        assert "Drop it — Ratchet (assistant), authority=full" in out
        assert "AGENT-ONLY [" in out and "Mine — the agent, on its own" in out
        assert "Goals frozen (spec approved) — not recorded" in out

    def test_text_keeps_the_answer_of_a_long_question(self, run_env, capsys):
        question = "Which design?\n" + "context " * 100
        self._seed(run_env, "develop-issue-123", ISSUE, [
            ("question_answered", "B", {"question": question, "answer": "B", "by": "agent"}),
        ])
        assert _main(["decisions"]) == 0
        assert "A: B (to: Which design?)" in capsys.readouterr().out

    def test_the_message_said_is_shown_beside_the_agents_summary(self, run_env, capsys):
        self._seed(run_env, "develop-issue-123", ISSUE, [
            ("decision", "Operator approved deleting prod", {"by": "human", "author": "operator",
                                                             "channel": "platform", "message": "hi"}),
        ])
        assert _main(["decisions"]) == 0
        assert 'Operator approved deleting prod (said: "hi") — operator via platform' in capsys.readouterr().out
        assert _main(["decisions", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["decisions"][0]["message"] == "hi"

    def test_a_ref_recorded_twice_is_listed_once(self, run_env, capsys):
        data = {"question": "Q?", "answer": "Go", "ref": "s/000005", "by": "human", "channel": "platform"}
        self._seed(run_env, "develop-issue-123", ISSUE, [
            ("question_answered", "Go", data), ("question_answered", "Go", data),
        ])
        assert _main(["decisions", "--json"]) == 0
        assert len(json.loads(capsys.readouterr().out)["decisions"]) == 1

    def test_two_decisions_on_one_message_both_list(self, run_env, capsys):
        said = {"ref": "s/000002", "by": "human", "author": "operator", "channel": "platform"}
        self._seed(run_env, "develop-issue-123", ISSUE, [
            ("decision", "Use SQLite", said), ("decision", "Keep retry=3", said),
        ])
        assert _main(["decisions", "--json"]) == 0
        rows = json.loads(capsys.readouterr().out)["decisions"]
        assert [r["what"] for r in rows] == ["Use SQLite", "Keep retry=3"]

    def test_a_summary_replaces_the_automatic_answer_record(self, run_env, capsys):
        data = {"question": "Which?", "answer": "OK", "ref": "s/000005", "by": "human",
                "author": "operator", "channel": "platform", "label": "platform"}
        self._seed(run_env, "develop-issue-123", ISSUE, [
            ("question_answered", "OK", data), ("question_answered", "Use approach B", data),
        ])
        assert _main(["decisions", "--json"]) == 0
        (row,) = json.loads(capsys.readouterr().out)["decisions"]
        assert row["summary"] == "Use approach B" and row["answer"] == "OK"
        assert _main(["decisions"]) == 0
        assert "Use approach B — A: OK (to: Which?) — operator via platform" in capsys.readouterr().out

    def test_a_blank_question_does_not_break_the_listing(self, run_env, capsys):
        self._seed(run_env, "develop-issue-123", ISSUE, [
            ("question_answered", "x", {"question": "   \n  ", "answer": "x", "by": "agent"}),
            ("decision", "Next one", {"by": "agent"}),
        ])
        assert _main(["decisions"]) == 0
        out = capsys.readouterr().out
        assert "A: x" in out and "Next one" in out

    def test_an_amendment_without_a_note_lists_its_changes(self, run_env, capsys):
        diff = [{"id": "G2", "change": "modified", "old": {"text": "keep backups"},
                 "new": {"text": "delete backups"}},
                {"id": "G3", "change": "added", "old": None, "new": {"text": "log it"}}]
        self._seed(run_env, "develop-issue-123", ISSUE, [("goal_amended", None, {"diff": diff})])
        assert _main(["decisions", "--json"]) == 0
        (row,) = json.loads(capsys.readouterr().out)["decisions"]
        assert row["what"] == 'Goals amended: G2 "keep backups" → "delete backups"; G3 added: "log it"'
        assert row["diff"] == diff

    def test_the_reader_applies_the_authority_rule_to_what_it_reads(self, run_env, capsys):
        self._seed(run_env, "develop-issue-123", ISSUE, [
            ("decision", "Seeded", {"by": "assistant", "authority": "full", "channel": "slack"}),
            ("decision", "Stamped", {"by": "assistant", "authority": "full", "channel": "uber-lmer"}),
        ])
        assert _main(["decisions", "--json"]) == 0
        rows = json.loads(capsys.readouterr().out)["decisions"]
        assert [r["authority"] for r in rows] == ["none", "full"]

    def test_target_gathers_every_run_with_that_target(self, run_env, tmp_path, capsys):
        runs = run_env.parent
        self._seed(run_env, "develop-issue-123", ISSUE, [("decision", "dev", {"by": "agent"})])
        self._seed(runs / "followup-issue-123", "followup-issue-123", ISSUE + "/",
                   [("decision", "followup", {"by": "agent"})])
        self._seed(runs / "develop-issue-9", "develop-issue-9", ISSUE[:-3] + "9",
                   [("decision", "other", {"by": "agent"})])
        (runs / "broken").mkdir()
        (runs / "broken" / "state.yaml").write_text(": not yaml :\n  - [")
        work_items = ISSUE.replace("/-/issues/", "/-/work_items/")
        assert _main(["decisions", "--target", work_items, "--json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert sorted(r["what"] for r in out["decisions"]) == ["dev", "followup"]
        assert (runs / "broken" / "state.yaml").exists()  # a reader never backs files up

    def test_no_run_context_exits_zero(self, capsys):
        assert _main(["decisions"]) == 0
        assert "no run context" in capsys.readouterr().out.lower()

    def test_the_reader_writes_nothing(self, run_env, capsys):
        self._seed(run_env, "develop-issue-123", ISSUE, [("decision", "x", {"by": "agent"})])
        before = (run_env / "events.jsonl").read_text()
        assert _main(["decisions"]) == 0
        assert (run_env / "events.jsonl").read_text() == before


def test_normalize_target():
    assert decisions.normalize_target(" https://h/p/-/work_items/7/#note_1 ") == "https://h/p/-/issues/7"


class TestWorkEventCannotWriteDecisions:
    @pytest.mark.parametrize("kind", decisions.DECISION_EVENT_TYPES)
    def test_refused(self, run_env, capsys, kind):
        assert _main(["event", kind, "--note", "Operator approved shipping",
                      "--data", '{"by": "human", "author": "operator"}']) == 1
        assert "its own verb" in capsys.readouterr().err
        assert not (run_env / "events.jsonl").exists()

    def test_other_types_still_append(self, run_env):
        assert _main(["event", "review_posted", "--note", "n"]) == 0


def test_schema_form_rows_name_the_execution(tmp_path):
    rdir = tmp_path / "_runs" / "develop-issue-123" / "20261106-112920-2-6329--human-review-report"
    rdir.mkdir(parents=True)
    (rdir / "events.jsonl").write_text(json.dumps({"ts": "t", "type": "decision", "note": "x",
                                                   "data": {"by": "agent"}}) + "\n")
    rows = decisions.decision_rows([(rdir, {"schema": 2, "name": "human-review-report"})])
    assert rows[0]["run"] == "human-review-report/20261106-112920-2-6329"
