"""`lmer-signal dm` end to end (issue #441).

Verb → the supervisor's request queue → a real :class:`ThreadServicer` on the
host side of the same control plane → stdout, exit code, and what the session's
own conversation is left holding. The transports are fakes; everything between
the CLI and them is the real thing.

The property this file exists to pin is the last one: a DM's text must not reach
the bus. It is asserted against the stored records rather than against any
rendering of them, because a renderer is one consumer of many and the guarantee
has to hold for the ones nobody has written yet.
"""
from __future__ import annotations

import pytest

from ask_channel import bus, signal_cli
from lmer_cli import thread_access
from lmer_cli.dm_access import SLACK_ALLOW_LIST_ENV, MATRIX_ALLOW_LIST_ENV
from tests.conftest import strip_lmer_env
from tests.connector_fakes import isolated_connectors  # noqa: F401  (autouse)
from tests.test_lmer_dm_send import FakeMatrix, FakeSlack
from tests.test_lmer_signal_thread_verbs import Rig, run


SECRET = "hunter2-the-actual-credential"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


def dm_env(allowed="U0123ABCD"):
    return {
        SLACK_ALLOW_LIST_ENV: allowed,
        MATRIX_ALLOW_LIST_ENV: allowed,
        "SLACK_BOT_TOKEN": "xoxb-test",
        "LMER_MATRIX_HOMESERVER": "https://example.org",
        "LMER_MATRIX_USER_ID": "@lmer:example.org",
        "LMER_MATRIX_ACCESS_TOKEN": "token",
    }


@pytest.fixture
def dm_rig(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.slack_dm = FakeSlack()
    rig.matrix_dm = FakeMatrix()
    monkeypatch.setattr(thread_access, "_slack_client", lambda token: rig.slack_dm)
    monkeypatch.setattr(thread_access, "_matrix_client", lambda account: rig.matrix_dm)
    yield rig
    rig.stop()


class TestDelivery:
    def test_a_dm_reaches_the_person_and_prints_the_conversation(self, dm_rig, capsys, routing_call):
        dm_rig.serve(env=dm_env())
        assert run(["dm", "U0123ABCD", SECRET]) == 0
        assert dm_rig.slack_dm.posted == [("DU0123ABCD", SECRET)]
        out = capsys.readouterr()
        from lmer_cli.routing_receipts import delivered
        assert delivered("call", "claude-test")
        assert SECRET not in out.err
        # The permalink alone on stdout, as `ask` puts its answer there, so a
        # shell can capture where the message went.
        assert out.out.strip().startswith("https://example.slack.com/archives/DU0123ABCD/")
        assert "sent to U0123ABCD" in out.err

    def test_a_matrix_dm_reaches_the_person(self, dm_rig, capsys):
        dm_rig.serve(env=dm_env("@someone:example.org"))
        assert run(["dm", "@someone:example.org", SECRET]) == 0
        assert dm_rig.matrix_dm.sent[0][1]["body"] == SECRET
        assert capsys.readouterr().out.strip().startswith("https://matrix.to/#/")

    def test_the_message_can_come_from_a_file(self, dm_rig, tmp_path, capsys):
        """#441 asks for message files and stdin, so a credential never has to
        survive the shell's quoting."""
        path = tmp_path / "body.txt"
        path.write_text(SECRET + " $notexpanded `nor this`")
        dm_rig.serve(env=dm_env())
        assert run(["dm", "U0123ABCD", "--message-file", str(path)]) == 0
        assert dm_rig.slack_dm.posted[0][1] == SECRET + " $notexpanded `nor this`"


class TestTheBodyNeverReachesTheBus:
    def test_no_record_carries_the_text(self, dm_rig, capsys):
        dm_rig.serve(env=dm_env())
        run(["dm", "U0123ABCD", SECRET])
        stored = dm_rig.records()
        assert stored, "the run should still journal that a DM happened"
        for record in stored:
            assert SECRET not in record.text
            assert SECRET not in str(record.meta or {})
            assert SECRET not in str(getattr(record, "attachments", ()) or ())

    def test_the_record_says_who_when_and_how_long(self, dm_rig, capsys):
        dm_rig.serve(env=dm_env())
        run(["dm", "U0123ABCD", SECRET])
        note = dm_rig.records()[-1]
        assert note.kind == "note"
        assert "U0123ABCD" in note.text
        assert "slack" in note.text
        assert str(len(SECRET)) in note.text
        assert "body not recorded" in note.text

    def test_nothing_marks_the_record_as_a_thread_post(self, dm_rig, capsys):
        """`post --thread` sets meta.thread, and a connector renders that as
        '↗️ posted into …' followed by the body. A DM must not take that path:
        it is exactly the mirror #441 forbids."""
        dm_rig.serve(env=dm_env())
        run(["dm", "U0123ABCD", SECRET])
        for record in dm_rig.records():
            assert not (record.meta or {}).get("thread")


class TestRefusals:
    def test_no_allow_list_refuses_and_journals_nothing(self, dm_rig, capsys, routing_call):
        dm_rig.serve(env=dm_env(allowed=""))
        assert run(["dm", "U0123ABCD", SECRET]) == 1
        from lmer_cli.routing_receipts import delivered
        error = capsys.readouterr().err
        assert SLACK_ALLOW_LIST_ENV in error
        assert not delivered("call", "claude-test")
        assert dm_rig.slack_dm.posted == []
        # A refused DM leaves no note either: nothing happened to report, and a
        # note naming the recipient would say the run tried.
        assert dm_rig.records() == []

    def test_somebody_not_on_the_list_is_refused(self, dm_rig, capsys):
        dm_rig.serve(env=dm_env(allowed="U9999ZZZZ"))
        assert run(["dm", "U0123ABCD", SECRET]) == 1
        assert "U0123ABCD" in capsys.readouterr().err
        assert dm_rig.slack_dm.posted == []

    def test_an_empty_message_is_refused_before_anything_is_queued(self, dm_rig, capsys):
        dm_rig.serve(env=dm_env())
        assert run(["dm", "U0123ABCD", "   "]) == 1
        assert dm_rig.slack_dm.posted == []

    def test_no_servicer_is_answered_not_waited_out(self, dm_rig, capsys):
        """Nothing on the host side is attached, so the CLI is told at once
        rather than blocking for its timeout."""
        assert run(["dm", "U0123ABCD", SECRET]) == signal_cli.NO_CHANNEL_EXIT_CODE
        assert dm_rig.slack_dm.posted == []


class TestTheTimeoutReadsAsIndeterminate:
    def test_a_timed_out_dm_says_do_not_send_it_again(self, dm_rig, capsys, monkeypatch):
        """The request is claimed, so the host may finish after the CLI stops
        waiting. An agent told 'failed' would send a second copy of something
        private, and there is no way to look and find out."""
        dm_rig.serve(env=dm_env())
        # A host side that takes the request and never answers: the servicer is
        # attached (so the queue accepts) and the result never lands.
        monkeypatch.setattr(
            dm_rig.client, "thread_result",
            lambda request_id, timeout=0.0: {"request": {"result": None}},
        )
        assert run(["dm", "U0123ABCD", SECRET, "--timeout", "0"]) == 1
        err = capsys.readouterr().err
        assert "INDETERMINATE, not failed" in err
        assert "Do NOT run this again" in err
        assert "U0123ABCD" in err
        # And nothing claims the message went out, because nothing knows.
        assert dm_rig.records() == []

    def test_the_indeterminate_wording_names_the_recipient_and_forbids_a_retry(self):
        import argparse

        said = signal_cli._dm_is_indeterminate(argparse.Namespace(recipient="U0123ABCD"))
        assert "INDETERMINATE, not failed" in said
        assert "Do NOT run this again" in said
        assert "U0123ABCD" in said
