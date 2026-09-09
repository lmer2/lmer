"""``--no-thread-connectors`` and the platform spawn that emits it (issue #412, T3).

The flag is what leaves one mechanism on the platform side: a session the daemon
spawned still carries its thread permalinks as positional targets — that is what
sets the container's chat context and what lets a repo-less ``chat`` session
start — but the connectors, the thread claim and the upload store are the
daemon's, because a connection has to be something that can be made and broken
while the session runs.

What is pinned here is the split: with the flag, the handler contributes context
and nothing else; without it, a CLI session behaves exactly as it did before.
"""
from __future__ import annotations

from lmer_cli import targets as target_handlers
from lmer_platform import spawn

SLACK_LINK = "https://example.slack.com/archives/C0123ABCDEF/p1712345678000900"
MATRIX_LINK = "https://matrix.to/#/%21room%3Aexample.org/%24event"


def handler(link, *, hosts_connectors):
    _, handlers = target_handlers.partition_targets([link])
    built = handlers[0]
    built.hosts_connectors = hosts_connectors
    built.attach_control_plane("http://127.0.0.1:8700", "control-token")
    return built


class TestTheHandlerUnderTheFlag:
    def test_the_container_context_is_unchanged(self):
        """The flag moves who runs the connector, never what the container is
        told — the chat taskdef and the reply guard key on these."""
        hosted = handler(SLACK_LINK, hosts_connectors=True).container_env()
        delegated = handler(SLACK_LINK, hosts_connectors=False).container_env()
        assert hosted == delegated
        assert delegated["LMER_CHAT_CHANNEL"] == "slack"
        assert delegated["LMER_SLACK_CHANNEL"] == "C0123ABCDEF"
        # And the credentials are still blocked, which is the rule the flag must
        # not be able to weaken.
        assert delegated["SLACK_BOT_TOKEN"] is None

    def test_no_connector_is_started_and_no_thread_is_claimed(self, monkeypatch):
        claimed, started = [], []
        monkeypatch.setattr(target_handlers, "register", lambda *a, **k: claimed.append(a))
        monkeypatch.setattr(target_handlers, "deregister", lambda *a, **k: claimed.append(a))

        import slack_chat.connector as slack_connector

        monkeypatch.setattr(
            slack_connector, "SlackConnector", lambda *a, **k: started.append(a),
        )
        built = handler(SLACK_LINK, hosts_connectors=False)
        built.on_session_start()
        built.on_session_end()

        assert claimed == [] and started == []
        assert built.connector is None

    def test_matrix_is_the_same_in_both_directions(self, monkeypatch):
        claimed = []
        monkeypatch.setattr(target_handlers, "register", lambda *a, **k: claimed.append(a))
        monkeypatch.setattr(target_handlers, "deregister", lambda *a, **k: claimed.append(a))
        built = handler(MATRIX_LINK, hosts_connectors=False)
        built.on_session_start()
        built.on_session_end()
        assert claimed == []

    def test_a_cli_session_still_claims_its_thread(self, monkeypatch):
        """Without the flag nothing changes: this is the path a shell
        ``lmer chat <permalink>`` and both listeners take."""
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        claimed = []
        monkeypatch.setattr(target_handlers, "register", lambda *a, **k: claimed.append(a))
        built = handler(SLACK_LINK, hosts_connectors=True)
        monkeypatch.setattr(built, "_start_additional_targets", lambda: None)
        import slack_chat.connector as slack_connector

        monkeypatch.setattr(slack_connector, "SlackConnector", _Recorder)
        built.on_session_start()
        assert claimed == [("C0123ABCDEF", "1712345678.000900")]
        assert isinstance(built.connector, _Recorder)

    def test_a_second_thread_is_delegated_too(self, monkeypatch):
        """A handler may hold several threads, each with its own connector. The
        flag has to reach every one of them, or the daemon and this process would
        both serve the second."""
        claimed = []
        monkeypatch.setattr(target_handlers, "register", lambda *a, **k: claimed.append(a))
        built = handler(SLACK_LINK, hosts_connectors=False)
        built.add("https://example.slack.com/archives/C0123ABCDEF/p1712345678000901")
        built.on_session_start()
        assert claimed == [] and built._additional_handlers == []


class _Recorder:
    def __init__(self, config, notify=None):
        self.config = config

    def start(self):
        pass

    def stop(self, timeout=5.0):
        pass


class TestTheSpawnCommand:
    def test_thread_targets_bring_the_flag(self):
        command = _command(targets=[SLACK_LINK])
        assert SLACK_LINK in command
        assert spawn.NO_THREAD_CONNECTORS_FLAG in command
        # After --fastapi, so the platform's own host flags still insert ahead of
        # the caller's extra_args.
        assert command.index("--fastapi") < command.index(spawn.NO_THREAD_CONNECTORS_FLAG)

    def test_every_spawn_gets_it_including_one_with_no_thread_targets(self):
        """The flag says *the host side of this session belongs to somebody
        else*, which is true of every session the daemon spawns.

        Emitted only under `targets` it was harmless while it governed connectors
        alone — a session with no thread has none to run — and became a second
        thread servicer on every target-less spawn once #414 hung one off the
        same condition, with the run's posts reaching third-party threads under
        whichever of the two names claimed the request first.
        """
        assert spawn.NO_THREAD_CONNECTORS_FLAG in _command(targets=None)


def _command(*, targets):
    from lmer_platform import config as cfg

    request = spawn.SpawnRequest(
        taskdef="chat", target="https://gitlab.example.com/o/p", targets=targets,
    )
    return spawn._build_command(cfg.PlatformConfig(), request)


class TestWhoStartsTheThreadServicer:
    """Which process answers `lmer-signal read` / `post --thread` for a session
    (issue #414).

    Two servicers on one session is not a redundancy: each holds its own idea of
    what the run is called, so the same run's posts reach a third-party thread
    under two different names depending on which one claimed the request. And a
    session with none is told "nothing here can reach Slack or matrix". The gate
    is one line in `lmer_cli.cli`; nothing exercised it until this class.
    """

    def _decision(self, source: str, *, fastapi: bool, token: bool, delegated: bool) -> bool:
        """The gate's own condition, read out of the source it is written in."""
        import re

        match = re.search(
            r"if (ns\.fastapi and control_token and not ns\.no_thread_connectors):",
            source,
        )
        assert match, "the thread servicer's start gate is not where this test reads it"
        return eval(  # noqa: S307 - the expression under test, with named values
            match.group(1),
            {"ns": type("N", (), {"fastapi": fastapi, "no_thread_connectors": delegated})(),
             "control_token": "token" if token else None},
        )

    def _source(self):
        from pathlib import Path

        import lmer_cli.cli as cli_module

        return Path(cli_module.__file__).read_text(encoding="utf-8")

    def test_a_platform_spawned_session_leaves_it_to_the_daemon(self):
        """Every daemon spawn carries `--no-thread-connectors` *and* a control
        token in its environment, so the gate must key on the flag and not on
        the token."""
        source = self._source()
        assert self._decision(source, fastapi=True, token=True, delegated=True) is False

    def test_a_shell_session_with_a_control_plane_gets_one(self):
        source = self._source()
        assert self._decision(source, fastapi=True, token=True, delegated=False) is True

    def test_a_session_with_no_control_plane_gets_none(self):
        source = self._source()
        assert self._decision(source, fastapi=False, token=False, delegated=False) is False

    def test_every_fastapi_session_mints_a_control_token(self):
        """Which is what makes the case above reachable: a plain `lmer --fastapi`
        used to mint none, so the servicer could not have been started for it
        however the gate read."""
        source = self._source()
        assert "if ns.fastapi and not control_token:" in source
