"""The speaker tag on a session's thread posts (issue #574).

Several sessions share one Slack app or one matrix bot account, so every post a
session makes into a thread names which session it is: ``[run]`` for a run,
``[Ratchet · <project>]`` for a project's assistant, under its configured name.
"""
from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from lmer_cli import connector_lifecycle, thread_access, thread_posts
from lmer_cli.threads import CHANNEL_MATRIX, CHANNEL_SLACK
from lmer_platform import api, projects, registry, session_threads
from lmer_platform import project_context as project_scope
from lmer_platform import project_requests
from matrix_chat import connector as matrix_connector
from slack_chat import connector as slack_connector
from slack_chat.client import SlackError
from tests.connector_fakes import ROOM, MatrixRig, SlackRig, isolated_connectors  # noqa: F401
from tests.timing_budgets import DRAIN

#: Before tests/conftest.py's autouse ``untagged_speaker`` replaces it.
RESOLVE_SPEAKER = connector_lifecycle.resolve_speaker


def _agent(kind, text, **extra):
    return {"id": f"s/{kind}", "seq": 1, "kind": kind, "text": text,
            "source": {"channel": "agent"}, "meta": {}, **extra}


class TestTheTag:
    def test_slack_escapes_and_matrix_does_not(self):
        assert thread_posts.speaker_tag("a<!channel>&b", slack_markup=True) == "[a&lt;!channel&gt;&amp;b]"
        assert thread_posts.speaker_tag("a<b>", slack_markup=False) == "[a<b>]"

    def test_one_line_and_nothing_for_no_label(self):
        assert thread_posts.speaker_tag("two\n  lines", slack_markup=False) == "[two lines]"
        assert thread_posts.speaker_tag(None, slack_markup=True) == ""
        assert thread_posts.speaker_tag("  ", slack_markup=True) == ""


class TestRendering:
    @pytest.mark.parametrize("record, expected", [
        (_agent("message", "hello"), "[run-a] hello"),
        (_agent("milestone", "pushed"), "🏁 [run-a] pushed"),
        (_agent("status", "busy"), "[run-a] [status] busy"),
        (_agent("message", "hi", meta={"thread": "the slack thread C9:1.0"}),
         "↗️ posted into the slack thread C9:1.0:\n[run-a] hi"),
    ])
    def test_what_the_session_says_is_tagged(self, record, expected):
        assert slack_connector.render_outbound(record, speaker="run-a") == expected

    def test_a_question_names_who_asks(self):
        text = slack_connector.render_outbound(
            _agent("question", "branch?", options=["main"]), speaker="run-a",
        )
        assert text.startswith("❓ [run-a] branch?\n1. main")

    @pytest.mark.parametrize("kind, expected", [
        ("message", "[Ratchet · Alpha] on it"),
        ("milestone", "🏁 [Ratchet · Alpha] on it"),
        ("question", "❓ [Ratchet · Alpha] on it"),
    ])
    def test_the_assistant_says_which_project(self, kind, expected):
        record = {"kind": kind, "text": "on it", "source": {"channel": "uber-lmer"},
                  "meta": {"session_origin": True}}
        assert slack_connector.render_outbound(record, speaker="Ratchet · Alpha").startswith(expected)

    def test_the_assistant_goes_by_its_name_before_its_project_is_known(self):
        record = {"kind": "message", "text": "on it", "source": {"channel": "uber-lmer"},
                  "meta": {"session_origin": True}}
        assert slack_connector.render_outbound(record) == "[Ratchet] on it"

    def test_the_fallback_follows_the_configured_name(self, monkeypatch):
        monkeypatch.setenv("LMER_PLATFORM_ASSISTANT_NAME", "Sprocket")
        record = {"kind": "message", "text": "on it", "source": {"channel": "uber-lmer"},
                  "meta": {"session_origin": True}}
        assert slack_connector.render_outbound(record) == "[Sprocket] on it"

    def test_the_assistant_writing_to_a_worker_is_not_the_worker_speaking(self):
        """The assistant posts onto a worker's bus as ``uber-lmer`` without the
        supervisor's own-record mark; the worker's tag must not land on it."""
        record = {"kind": "message", "text": "Review the behavior", "source": {"channel": "uber-lmer"}, "meta": {}}
        assert slack_connector.render_outbound(record, speaker="develop-issue-574") == "💬 [Ratchet] Review the behavior"

    def test_other_channels_keep_their_own_label(self):
        record = {"kind": "message", "text": "go", "source": {"channel": "terminal"}, "meta": {}}
        assert slack_connector.render_outbound(record, speaker="run-a") == "💬 [terminal] go"

    def test_no_speaker_renders_as_before(self):
        assert slack_connector.render_outbound(_agent("message", "hello")) == "hello"

    def test_matrix_carries_the_tag_as_plain_text(self):
        assert matrix_connector.render_outbound(_agent("message", "x"), speaker="a<b") == "[a<b] x"
        assert slack_connector.render_outbound(_agent("message", "x"), speaker="a<b") == "[a&lt;b] x"


def test_a_thread_post_escapes_its_tag_on_slack_only(monkeypatch):
    posted = []
    monkeypatch.setattr(thread_access, "_post_slack", lambda ref, client, text, named, **kw: posted.append(text) or {})
    monkeypatch.setattr(thread_access, "_post_matrix", lambda ref, client, text, named, **kw: posted.append(text) or {})
    monkeypatch.setattr(thread_access, "_thread_summary", lambda ref: {})
    for channel in (CHANNEL_SLACK, CHANNEL_MATRIX):
        thread_access.post_thread(None, "hi", label="<!channel> run", ref=SimpleNamespace(channel=channel), client=object())
    assert posted == ["[&lt;!channel&gt; run] hi", "[<!channel> run] hi"]


class TestNotices:
    def test_attach_and_detach_name_the_session(self):
        assert slack_connector.attachment_notice(None, speaker="run-a") == "🔗 lmer is listening in this thread as [run-a]."
        assert slack_connector.detach_notice("run-a") == "🔌 lmer has stopped listening in this thread as [run-a]."
        launched = slack_connector.attachment_notice({"preset": "p"}, slack_markup=False, speaker="a<b")
        assert launched == "🔗 lmer is listening in this thread as [a<b]. Preset: p; model: harness default."

    def test_untagged_until_the_session_is_known(self):
        assert slack_connector.attachment_notice(None) == slack_connector.ATTACH_ACK
        assert slack_connector.detach_notice(None) == slack_connector.DETACH_NOTE


class TestWhoTheSpeakerIs:
    def test_a_run_is_its_title_else_its_slug(self, monkeypatch):
        monkeypatch.setattr(registry, "read_session", lambda session: {"kind": registry.WORKER_KIND})
        monkeypatch.setattr(session_threads.runs, "run_for_session", lambda session: ("h", "p", "slug-a"))
        title = {"value": "Title A"}
        monkeypatch.setattr(session_threads.meta, "read", lambda *identity: SimpleNamespace(title=title["value"]))
        assert session_threads.speaker_label("s1") == "Title A"
        title["value"] = None
        assert session_threads.speaker_label("s1") == "slug-a"

    def test_the_assistant_names_its_project(self, monkeypatch):
        monkeypatch.setattr(registry, "read_session", lambda session: {"kind": registry.ASSISTANT_KIND})
        monkeypatch.setattr(projects, "session_home", lambda session: "p1")
        monkeypatch.setattr(projects, "get", lambda workspace: {"name": "  Alpha \n"})
        assert session_threads.speaker_label("s1") == "Ratchet · Alpha"

    def test_the_assistant_without_projects(self, monkeypatch):
        monkeypatch.setattr(registry, "read_session", lambda session: {"kind": registry.ASSISTANT_KIND})
        monkeypatch.setattr(projects, "session_home", lambda session: None)
        assert session_threads.speaker_label("s1") == "Ratchet"

    def test_the_assistant_tag_follows_the_configured_name(self, monkeypatch):
        """#581's name, on every path that names the assistant: the registry
        lookup, its failure fallback, and the platform's own label."""
        monkeypatch.setenv("LMER_PLATFORM_ASSISTANT_NAME", "Sprocket")
        monkeypatch.setattr(registry, "read_session", lambda session: {"kind": registry.ASSISTANT_KIND})
        monkeypatch.setattr(projects, "session_home", lambda session: "p1")
        monkeypatch.setattr(projects, "get", lambda workspace: {"name": "Alpha"})
        assert session_threads.speaker_label("s1") == "Sprocket · Alpha"
        assert session_threads.assistant_label(None) == "Sprocket"
        assert session_threads.assistant_label("p1", name="Cog") == "Cog · Alpha"

        def fail(workspace):
            raise OSError("database is locked")
        monkeypatch.setattr(projects, "get", fail)
        assert session_threads.speaker_label("s1") == "Sprocket"

    def test_an_unreadable_project_costs_only_the_project_name(self, monkeypatch):
        def missing(workspace):
            raise projects.ProjectError("gone")
        monkeypatch.setattr(projects, "get", missing)
        assert session_threads.assistant_label("p1") == "Ratchet"

    def test_a_session_nothing_registered_is_named_by_its_session(self, monkeypatch):
        monkeypatch.setattr(registry, "read_session", lambda session: None)
        monkeypatch.setattr(session_threads.runs, "run_for_session", lambda session: None)
        assert session_threads.speaker_label("s1") == "lmer session s1"
        assert session_threads.speaker_label("s1", known_only=True) is None

    @pytest.mark.parametrize("broken", ["registry", "session_home", "projects.get"])
    def test_known_only_says_nothing_when_the_assistant_cannot_be_identified(self, monkeypatch, broken):
        def fail(*args):
            raise OSError("database is locked")
        monkeypatch.setattr(registry, "read_session",
                            fail if broken == "registry" else lambda session: {"kind": registry.ASSISTANT_KIND})
        monkeypatch.setattr(projects, "session_home", fail if broken == "session_home" else lambda session: "p1")
        monkeypatch.setattr(projects, "get", fail if broken == "projects.get" else lambda workspace: {"name": "Alpha"})
        assert session_threads.speaker_label("s1", known_only=True) is None

    def test_known_only_says_nothing_when_a_run_cannot_be_read(self, monkeypatch):
        def fail(session):
            raise OSError("unreadable")
        monkeypatch.setattr(registry, "read_session", lambda session: {"kind": registry.WORKER_KIND})
        monkeypatch.setattr(session_threads.runs, "run_for_session", fail)
        assert session_threads.speaker_label("s1", known_only=True) is None
        assert session_threads.speaker_label("s1") == "lmer session s1"

    def test_known_only_keeps_the_answers_that_are_not_failures(self, monkeypatch):
        monkeypatch.setattr(registry, "read_session", lambda session: {"kind": registry.WORKER_KIND})
        monkeypatch.setattr(session_threads.runs, "run_for_session", lambda session: None)
        assert session_threads.speaker_label("s1", known_only=True) == "lmer session s1"
        monkeypatch.setattr(registry, "read_session", lambda session: {"kind": registry.ASSISTANT_KIND})
        monkeypatch.setattr(projects, "session_home", lambda session: None)
        assert session_threads.speaker_label("s1", known_only=True) == "Ratchet"

    def test_the_platform_post_verb_names_the_project(self, monkeypatch):
        monkeypatch.setattr(projects, "enabled", lambda: True)
        monkeypatch.setattr(projects, "get", lambda workspace: {"name": "Alpha"})
        config = SimpleNamespace(assistant_name="Sprocket")
        with project_scope.bound("p1"):
            assert api._thread_label(SimpleNamespace(is_assistant=True), config) == "Sprocket · Alpha"


class TestProjectRequestReply:
    """The request-reply path posted bare text with no poster stamp, which is how
    a project's assistant and its worker became indistinguishable in #574."""

    def _row(self, transport):
        return {"transport": transport, "conversation": "C1" if transport == "slack" else "!r:example.org",
                "root": "1.0", "project_id": "p1", "payload": {"permalink": "unused"}}

    def test_slack_reply_is_tagged_and_attributed(self, monkeypatch):
        monkeypatch.setattr(projects, "get", lambda workspace: {"name": "Alpha"})
        client = SimpleNamespace(posted=[])
        client.post_message = lambda channel, root, text: client.posted.append(text) or "9.1"
        assert project_requests._reply_as_assistant(self._row("slack"), "done", client) == "9.1"
        assert client.posted == ["[Ratchet · Alpha] done"]
        poster = thread_posts.lookup("C1", "9.1")
        assert poster.author == "platform · Ratchet · Alpha"

    def test_the_reply_follows_the_configured_name(self, monkeypatch):
        monkeypatch.setenv("LMER_PLATFORM_ASSISTANT_NAME", "Sprocket")
        monkeypatch.setattr(projects, "get", lambda workspace: {"name": "Alpha"})
        client = SimpleNamespace(posted=[])
        client.post_message = lambda channel, root, text: client.posted.append(text) or "9.2"
        project_requests._reply_as_assistant(self._row("slack"), "done", client)
        assert client.posted == ["[Sprocket · Alpha] done"]
        assert thread_posts.lookup("C1", "9.2").author == "platform · Sprocket · Alpha"

    def test_matrix_reply_is_tagged_and_attributed(self, monkeypatch):
        monkeypatch.setattr(projects, "get", lambda workspace: {"name": "Alpha"})
        sent = []
        client = SimpleNamespace(send_thread_text=lambda room, root, text, extra=None: sent.append((text, extra)) or "$e")
        project_requests._reply_as_assistant(self._row("matrix"), "done", client)
        (text, extra), = sent
        assert text == "[Ratchet · Alpha] done"
        assert thread_posts.poster_from_matrix(extra).author == "platform · Ratchet · Alpha"


def test_the_connector_asks_the_platform(monkeypatch):
    monkeypatch.setattr(session_threads, "speaker_label", lambda session, known_only=False: f"label of {session}")
    assert RESOLVE_SPEAKER("s1") == "label of s1"


@pytest.mark.parametrize("failure", ["entry gone", "projects locked"])
def test_a_failed_lookup_keeps_the_tag_the_connector_has(tmp_path, monkeypatch, failure):
    """A rotated assistant's entry is removed before its last posts and its
    detach notice; neither that nor a locked database may relabel it."""
    monkeypatch.setattr(connector_lifecycle, "resolve_speaker", RESOLVE_SPEAKER)
    monkeypatch.setattr(connector_lifecycle, "SPEAKER_REFRESH_SECONDS", 0.0)
    registered = {"entry": {"kind": registry.ASSISTANT_KIND}}
    monkeypatch.setattr(registry, "read_session", lambda session: registered["entry"])
    monkeypatch.setattr(projects, "session_home", lambda session: "p1")
    name = {"value": "Alpha"}

    def get(workspace):
        if name["value"] is None:
            raise OSError("database is locked")
        return {"name": name["value"]}
    monkeypatch.setattr(projects, "get", get)
    connector = SlackRig(tmp_path).connector
    try:
        connector._ensure_ready()
        assert connector._speaker_label() == "Ratchet · Alpha"
        if failure == "entry gone":
            registered["entry"] = None
        else:
            name["value"] = None
        assert connector._speaker_label() == "Ratchet · Alpha"
        assert connector._detach_notice().endswith("as [Ratchet · Alpha].")
    finally:
        connector.stop(timeout=DRAIN)


def test_a_session_nothing_registered_is_still_tagged(tmp_path, monkeypatch):
    monkeypatch.setattr(connector_lifecycle, "resolve_speaker", RESOLVE_SPEAKER)
    monkeypatch.setattr(registry, "read_session", lambda session: None)
    monkeypatch.setattr(session_threads.runs, "run_for_session", lambda session: None)
    connector = SlackRig(tmp_path).connector
    try:
        connector._ensure_ready()
        session = connector._state.session
        assert connector._speaker_label() == f"lmer session {session}"
    finally:
        connector.stop(timeout=DRAIN)


@pytest.mark.parametrize("channel", ["slack", "matrix"])
@pytest.mark.parametrize("kind", ["assistant", "run"])
def test_the_platform_session_id_names_the_speaker_not_the_bus_one(channel, kind, tmp_path, monkeypatch):
    """The supervisor reports the container's own session id ("sess-1" here);
    the platform knows the session as another id, and only that one is in its
    registry and runs index. Looked up by the bus id, every platform session
    fell back to ``lmer session <bus id>`` (#574, found live)."""
    monkeypatch.setattr(connector_lifecycle, "resolve_speaker", RESOLVE_SPEAKER)
    known = "s-plat"
    entry = {"kind": registry.ASSISTANT_KIND if kind == "assistant" else registry.WORKER_KIND}
    monkeypatch.setattr(registry, "read_session", lambda session: entry if session == known else None)
    monkeypatch.setattr(projects, "session_home", lambda session: "p1" if session == known else None)
    monkeypatch.setattr(projects, "get", lambda workspace: {"name": "Alpha"})
    monkeypatch.setattr(session_threads.runs, "run_for_session",
                        lambda session: ("h", "p", "develop-issue-574") if session == known else None)
    monkeypatch.setattr(session_threads.meta, "read", lambda *identity: SimpleNamespace(title=None))
    rig = (SlackRig if channel == "slack" else MatrixRig)(tmp_path)
    connector = rig.connector
    connector.config = dataclasses.replace(connector.config, platform_session=known)
    if channel == "matrix":
        connector.connect()
    expected = "Ratchet · Alpha" if kind == "assistant" else "develop-issue-574"
    try:
        connector._send_record(_agent("message", "hello"))
        connector._state.finish(lambda: None)
        assert connector._state.session == "sess-1"
        sent = rig.slack.posted if channel == "slack" else rig.matrix.sent
        assert sent[-1][2] == f"[{expected}] hello"
    finally:
        connector.stop(timeout=DRAIN)


@pytest.mark.parametrize("module", [slack_connector, matrix_connector])
def test_the_platform_session_id_is_read_from_the_environment(module):
    env = {"LMER_SUPERVISOR_URL": "http://127.0.0.1:1", "LMER_FASTAPI_TOKEN": "tok",
           "LMER_SLACK_CHANNEL": "C1", "LMER_SLACK_THREAD_TS": "1.0", "SLACK_BOT_TOKEN": "xoxb-test",
           "LMER_MATRIX_ROOM_ID": ROOM, "LMER_MATRIX_THREAD_EVENT": "$root",
           "LMER_MATRIX_HOMESERVER": "https://matrix.example.org", "LMER_MATRIX_USER_ID": "@bot:example.org",
           "LMER_MATRIX_ACCESS_TOKEN": "fixture-token"}
    assert module.ConnectorConfig.from_env({**env, "LMER_CHAT_PLATFORM_SESSION": " s-plat "}).platform_session == "s-plat"
    assert module.ConnectorConfig.from_env({**env, "LMER_CHAT_PLATFORM_SESSION": ""}).platform_session is None
    assert module.ConnectorConfig.from_env(env).platform_session is None


@pytest.fixture
def speaker(monkeypatch):
    current = {"label": "develop-issue-574"}
    monkeypatch.setattr(connector_lifecycle, "resolve_speaker", lambda session, **kwargs: current["label"])
    return current


@pytest.mark.parametrize("channel", ["slack", "matrix"])
def test_a_connector_tags_what_its_session_says(channel, tmp_path, speaker):
    rig = (SlackRig if channel == "slack" else MatrixRig)(tmp_path)
    connector = rig.connector
    if channel == "matrix":
        connector.connect()
    try:
        connector._send_record(_agent("message", "hello"))
        connector._state.finish(lambda: None)
        sent = rig.slack.posted if channel == "slack" else rig.matrix.sent
        assert sent[-1][2] == "[develop-issue-574] hello"
        assert connector._attachment_notice().endswith("as [develop-issue-574].")
    finally:
        connector.stop(timeout=DRAIN)


def test_a_withheld_attachment_note_names_the_session_on_matrix(tmp_path, speaker):
    """On matrix the note is its own event, not part of the tagged text."""
    store = tmp_path / "uploads"
    store.mkdir()
    (store / "report.txt").write_text("all green", encoding="utf-8")
    rig = MatrixRig(tmp_path, uploads=str(store))
    rig.connector.room_id = ROOM
    record = {**_agent("message", "see"),
              "attachments": [{"name": "report.txt", "media_type": "text/plain", "bytes": 9,
                               "path": "/home/developer/.lmer-uploads/report.txt"}]}
    try:
        rig.connector._post_outbound(record, {})
        notes = [sent[2] for sent in rig.matrix.sent if "withheld" in sent[2]]
        assert notes and all(note.startswith("[develop-issue-574] 📎 report.txt withheld:") for note in notes)
    finally:
        rig.connector.stop(timeout=DRAIN)


def test_a_thread_posts_withheld_note_names_the_session_on_matrix(monkeypatch):
    sent = []
    client = SimpleNamespace(send_thread_text=lambda room, event, text, **kw: sent.append(text) or "$e")
    monkeypatch.setattr(thread_access, "_matrix_attachment", lambda *args, **kw: "not readable")
    ref = SimpleNamespace(channel=CHANNEL_MATRIX, key={"room": "!r:example.org", "event": "$root"})
    thread_access._post_matrix(ref, client, "[run-a] here", [{"name": "a.md"}], store=None, env={}, label="run-a")
    assert sent == ["[run-a] here", "[run-a] 📎 a.md withheld: not readable"]


def test_a_retried_send_keeps_the_text_it_committed_to(tmp_path, speaker, monkeypatch):
    """Delivery refuses changed content on a retry, so a run titled between a
    rejected send and its retry must re-send the first rendering, not block."""
    monkeypatch.setattr(connector_lifecycle, "SPEAKER_REFRESH_SECONDS", 0.0)
    rig = SlackRig(tmp_path)
    connector = rig.connector
    record = _agent("message", "one")
    try:
        rig.slack.fail_post = True
        with pytest.raises(SlackError):
            connector._send_record(record)
        speaker["label"] = "Renamed run"
        rig.slack.fail_post = False
        connector._send_record(record)
        connector._state.finish(lambda: None)
        assert [text for _, _, text in rig.slack.posted] == ["[develop-issue-574] one"]
        connector._send_record({**_agent("message", "two"), "id": "s/two", "seq": 2})
        assert rig.slack.posted[-1][2] == "[Renamed run] two"
    finally:
        connector.stop(timeout=DRAIN)
