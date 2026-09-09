"""Special (non-repository) target types accepted by the lmer CLI.

Most positional ``<target>`` arguments to ``lmer`` are repository
references (repo URLs, MR/PR/issue URLs, ``~name`` shortcuts). Some
target types are not repositories at all — today, Slack thread
permalinks. Each such type is owned by a :class:`TargetHandler`
subclass that keeps every piece of type-specific host-side behavior in
one place:

- recognising its targets (:meth:`TargetHandler.matches`)
- validating required credentials (:meth:`TargetHandler.validate_environment`)
- whether a session may run without a repository when its targets are
  the only ones given (:attr:`TargetHandler.repoless_task_ids`)
- the env vars it forwards into the container
  (:meth:`TargetHandler.container_env`)

:func:`partition_targets` is the single entry point used by
``lmer_cli.cli``: it splits the raw target list into repository targets
and one handler instance per target type that claimed at least one
target. Adding a new target type means writing a handler subclass and
appending it to :data:`TARGET_HANDLER_TYPES` — the CLI orchestration
needs no new branches.
"""

import os
import sys
from typing import ClassVar

from matrix_chat.permalink import is_matrix_thread_url, parse_matrix_permalink
from slack_chat.permalink import is_slack_thread_url, parse_slack_permalink
from slack_chat.registry import deregister, register


class TargetHandler:
    """Base class owning the host-side logic for one special target type."""

    #: Task ids allowed to run a repo-less session when this handler's
    #: targets are the only targets given and no git origin can be
    #: inferred from the working directory.
    repoless_task_ids: ClassVar[frozenset[str]] = frozenset()

    #: Every container env key this type can contribute. Used by
    #: :func:`special_target_env` to seed inactive types' keys with None
    #: so .env files cannot forward them without a matching target.
    container_env_keys: ClassVar[tuple[str, ...]] = ()

    #: Whether this target type runs a connector against the session's bus
    #: (issue #407). The CLI then turns the supervisor control plane on for the
    #: session, mints its token host-side when the environment did not supply
    #: one, and hands both to :meth:`attach_control_plane` before
    #: :meth:`on_session_start`.
    needs_control_plane: ClassVar[bool] = False

    def __init__(self) -> None:
        self.targets: list[str] = []
        self.control_plane: tuple[str, str] | None = None
        self.uploads: tuple[str, str] | None = None
        #: Whether this process runs the connectors for its threads and claims
        #: them in the host registry. ``False`` under ``--no-thread-connectors``,
        #: where the platform daemon owns both (issue #412) and all this handler
        #: contributes is the container's chat context.
        self.hosts_connectors: bool = True
        self._additional_handlers: list[TargetHandler] = []

    def attach_control_plane(self, url: str, token: str) -> None:
        """Where the session's bus will answer, from the host side."""
        self.control_plane = (url, token)

    def attach_uploads(self, host_dir: str, container_dir: str) -> None:
        """The session's upload store: its host directory and its container path."""
        self.uploads = (host_dir, container_dir)

    @classmethod
    def matches(cls, target: str) -> bool:
        """Whether this handler claims the given positional target."""
        raise NotImplementedError

    def add(self, target: str) -> None:
        """Record a target this handler claimed via :meth:`matches`."""
        self.targets.append(target)

    @staticmethod
    def thread_key(target: str) -> tuple[str, str]:
        """The conversation and root event this permalink connects to."""
        raise NotImplementedError

    def _start_additional_targets(self) -> None:
        """Each additional thread owns its connector, cursor and registration."""
        seen = {self.thread_key(target) for target in self.targets[:1]}
        for target in self.targets[1:]:
            key = self.thread_key(target)
            if key in seen:
                continue
            seen.add(key)
            handler = type(self)()
            handler.add(target)
            handler.control_plane = self.control_plane
            handler.uploads = self.uploads
            handler.hosts_connectors = self.hosts_connectors
            self._additional_handlers.append(handler)
            handler.on_session_start()

    def _stop_additional_targets(self) -> None:
        for handler in self._additional_handlers:
            handler.on_session_end()
        self._additional_handlers.clear()

    def validate_environment(self) -> str | None:
        """Return an error message when required credentials are missing."""
        return None

    def supports_repoless_session(self, task_id: str | None) -> bool:
        return task_id in self.repoless_task_ids

    def repoless_start_message(self) -> str:
        """Info message announcing a repo-less session for this type.

        Only handlers that opt into repo-less sessions (non-empty
        :attr:`repoless_task_ids`) need to override this.
        """
        raise NotImplementedError

    def repoless_unsupported_reason(self, task_id: str | None) -> str:
        """Why a repo-less session is refused for the given task.

        The default covers handlers that never opt into repo-less mode,
        so a new target type fails with a clean error rather than a
        traceback; opt-in handlers should override it with a message
        naming their supported tasks.
        """
        return (
            "This target type requires a repository target for the "
            f"'{task_id}' task (or run it from inside a git checkout)."
        )

    def container_env(self) -> dict[str, str | None]:
        """Env vars this handler forwards into the container."""
        return {}

    def on_session_start(self) -> None:
        """Hook fired just before a real session launches for these targets.

        Default no-op. A handler overrides it to announce or register the
        session — :class:`SlackThreadTargets` records its Slack-thread
        attachment so the listener won't connect a second lmer to a thread that
        already has one. Called only for the actual interactive session launch
        (not ``--exec``/``--no-task`` one-shots), and paired with
        :meth:`on_session_end`.
        """

    def on_session_end(self) -> None:
        """Teardown counterpart to :meth:`on_session_start`. Default no-op."""


def _warn(message: str) -> None:
    """What a connector says when it gives up. The connectors log it too, but
    the host `lmer` process configures no logging, so this is the copy a human
    sees."""
    print(f"⚠️  {message}", file=sys.stderr)


class SlackThreadTargets(TargetHandler):
    """Slack thread permalinks passed as ``lmer`` targets (issue #54).

    Only the first Slack target drives the container's chat context
    (``LMER_SLACK_CHANNEL`` / ``LMER_SLACK_THREAD_TS`` /
    ``LMER_SLACK_PERMALINK`` / ``LMER_CHAT_CHANNEL``); additional Slack targets
    each get their own connector. ``SLACK_BOT_TOKEN`` is required on the
    **host** whenever a Slack target is present — since issue #407 the thread is
    served by a connector running in this process
    (:mod:`slack_chat.connector`), so the token never enters the container: both
    Slack tokens are seeded ``None`` in the container env, which is what keeps a
    ``.env`` file from forwarding them. Repo-less sessions are allowed for the
    ``chat`` task only — it is the only taskdef adapted for repo-less mode; every
    other task assumes code in /workspace.
    """

    repoless_task_ids = frozenset({"chat"})
    container_env_keys = (
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "LMER_SLACK_CHANNEL",
        "LMER_SLACK_THREAD_TS",
        "LMER_SLACK_PERMALINK",
        "LMER_CHAT_CHANNEL",
    )
    needs_control_plane = True

    def __init__(self) -> None:
        super().__init__()
        self.channel: str | None = None
        self.thread_ts: str | None = None
        self.permalink: str | None = None
        self.connector = None

    @classmethod
    def matches(cls, target: str) -> bool:
        return is_slack_thread_url(target)

    @staticmethod
    def thread_key(target: str) -> tuple[str, str]:
        return parse_slack_permalink(target)

    def add(self, target: str) -> None:
        super().add(target)
        if self.permalink is None:
            self.permalink = target
            self.channel, self.thread_ts = parse_slack_permalink(target)

    def validate_environment(self) -> str | None:
        if not os.environ.get("SLACK_BOT_TOKEN"):
            return (
                "SLACK_BOT_TOKEN is required when a Slack thread URL is given. "
                "Set it in your .env file (SLACK_BOT_TOKEN=xoxb-...)."
            )
        return None

    def repoless_start_message(self) -> str:
        return "💬 Slack thread is the only target — starting a session without a repository"

    def repoless_unsupported_reason(self, task_id: str | None) -> str:
        return (
            "A Slack thread as the only target is supported for the 'chat' "
            f"task only; the '{task_id}' task needs a repository target "
            "(or run it from inside a git checkout)."
        )

    def container_env(self) -> dict[str, str | None]:
        return {
            # Blocked, not forwarded: the connector on the host holds them.
            "SLACK_BOT_TOKEN": None,
            "SLACK_APP_TOKEN": None,
            "LMER_SLACK_CHANNEL": self.channel,
            "LMER_SLACK_THREAD_TS": self.thread_ts,
            "LMER_SLACK_PERMALINK": self.permalink,
            "LMER_CHAT_CHANNEL": "slack",
        }

    def on_session_start(self) -> None:
        """Record the attachment in the host-side registry and start the connector.

        The registry lets the Slack listener detect that an lmer is already
        connected to the thread — including a session it didn't spawn, e.g. a
        manual ``lmer chat <permalink>`` from a shell — and decline to connect a
        second one (issue #74). The connector is what carries the conversation
        (issue #407): it needs the control plane the CLI attached; without one
        the session runs and the thread stays silent, and that is said out loud.
        """
        if not self.hosts_connectors:
            return
        self._start_additional_targets()
        register(self.channel, self.thread_ts, permalink=self.permalink)
        if self.control_plane is None or not self.channel or not self.thread_ts:
            print(
                "⚠️  Slack thread target without a supervisor control plane — the "
                "connector cannot run, so the thread will not hear this session",
                file=sys.stderr,
            )
            return
        try:
            from slack_chat.connector import ConnectorConfig, SlackConnector

            url, token = self.control_plane
            host_dir, container_dir = self.uploads or (None, None)
            config = ConnectorConfig.from_env(
                channel=self.channel, thread_ts=self.thread_ts,
                supervisor_url=url, supervisor_token=token,
                uploads_dir=host_dir, container_uploads_dir=container_dir,
            )
            self.connector = SlackConnector(config, notify=_warn)
            self.connector.start()
        except Exception as exc:  # a dead connector must not stop the session
            print(f"⚠️  Slack connector did not start: {exc}", file=sys.stderr)
            self.connector = None

    def on_session_end(self) -> None:
        """Stop the connector and clear the registry entry."""
        if not self.hosts_connectors:
            return
        self._stop_additional_targets()
        if self.connector is not None:
            self.connector.stop()
            self.connector = None
        deregister(self.channel, self.thread_ts)


class MatrixThreadTargets(TargetHandler):
    """Matrix thread permalinks passed as ``lmer`` targets (issue #407, §5.5).

    The mirror of :class:`SlackThreadTargets`: the first matrix target drives the
    container's chat context (``LMER_MATRIX_ROOM_ID`` / ``LMER_MATRIX_THREAD_EVENT``
    / ``LMER_MATRIX_PERMALINK`` / ``LMER_CHAT_CHANNEL``), the bot account's
    credentials are required on the **host** and never enter the container, and
    the connector runs in this process (:mod:`matrix_chat.connector`).
    """

    repoless_task_ids = frozenset({"chat"})
    container_env_keys = (
        "LMER_MATRIX_HOMESERVER",
        "LMER_MATRIX_USER_ID",
        "LMER_MATRIX_ACCESS_TOKEN",
        "LMER_MATRIX_ROOM_ID",
        "LMER_MATRIX_THREAD_EVENT",
        "LMER_MATRIX_PERMALINK",
        "LMER_CHAT_CHANNEL",
    )
    needs_control_plane = True

    def __init__(self) -> None:
        super().__init__()
        self.room: str | None = None
        self.thread_event: str | None = None
        self.permalink: str | None = None
        self.connector = None

    @classmethod
    def matches(cls, target: str) -> bool:
        return is_matrix_thread_url(target)

    @staticmethod
    def thread_key(target: str) -> tuple[str, str]:
        parsed = parse_matrix_permalink(target)
        if parsed is None:
            raise ValueError("invalid matrix thread permalink")
        return parsed.room, parsed.event_id

    def add(self, target: str) -> None:
        super().add(target)
        if self.permalink is None:
            parsed = parse_matrix_permalink(target)
            if parsed is not None:
                self.permalink = target
                self.room, self.thread_event = parsed.room, parsed.event_id

    def validate_environment(self) -> str | None:
        missing = [
            name for name in ("LMER_MATRIX_HOMESERVER", "LMER_MATRIX_USER_ID", "LMER_MATRIX_ACCESS_TOKEN")
            if not os.environ.get(name)
        ]
        if missing:
            return (
                f"{', '.join(missing)} required when a matrix thread URL is given — the bot "
                "account the connector acts as. Set them in your .env file."
            )
        return None

    def repoless_start_message(self) -> str:
        return "💬 matrix thread is the only target — starting a session without a repository"

    def repoless_unsupported_reason(self, task_id: str | None) -> str:
        return (
            "A matrix thread as the only target is supported for the 'chat' task "
            f"only; the '{task_id}' task needs a repository target (or run it from "
            "inside a git checkout)."
        )

    def container_env(self) -> dict[str, str | None]:
        return {
            "LMER_MATRIX_HOMESERVER": None,
            "LMER_MATRIX_USER_ID": None,
            "LMER_MATRIX_ACCESS_TOKEN": None,
            "LMER_MATRIX_ROOM_ID": self.room,
            "LMER_MATRIX_THREAD_EVENT": self.thread_event,
            "LMER_MATRIX_PERMALINK": self.permalink,
            "LMER_CHAT_CHANNEL": "matrix",
        }

    def on_session_start(self) -> None:
        if not self.hosts_connectors:
            return
        self._start_additional_targets()
        register(self.room, self.thread_event, permalink=self.permalink, kind="matrix")
        if self.control_plane is None or not self.room or not self.thread_event:
            print(
                "⚠️  matrix thread target without a supervisor control plane — the "
                "connector cannot run, so the thread will not hear this session",
                file=sys.stderr,
            )
            return
        try:
            from matrix_chat.connector import ConnectorConfig, MatrixConnector

            url, token = self.control_plane
            host_dir, container_dir = self.uploads or (None, None)
            config = ConnectorConfig.from_env(
                room=self.room, thread_event=self.thread_event,
                supervisor_url=url, supervisor_token=token,
                uploads_dir=host_dir, container_uploads_dir=container_dir,
            )
            self.connector = MatrixConnector(config, notify=_warn)
            self.connector.start()
        except Exception as exc:  # a dead connector must not stop the session
            print(f"⚠️  matrix connector did not start: {exc}", file=sys.stderr)
            self.connector = None

    def on_session_end(self) -> None:
        if not self.hosts_connectors:
            return
        self._stop_additional_targets()
        if self.connector is not None:
            self.connector.stop()
            self.connector = None
        deregister(self.room, self.thread_event, kind="matrix")


#: Registry of special target types, in match-priority order.
TARGET_HANDLER_TYPES: tuple[type[TargetHandler], ...] = (SlackThreadTargets, MatrixThreadTargets)


def partition_targets(
    raw_targets: list[str],
) -> tuple[list[str], list[TargetHandler]]:
    """Split positional targets into repository targets and special handlers.

    Args:
        raw_targets: The raw positional target list from the CLI.

    Returns:
        A two-tuple ``(repo_targets, handlers)`` where:
        - ``repo_targets``: entries no special target type claimed. Used
          verbatim as primary/secondary for repo-clone logic.
        - ``handlers``: one :class:`TargetHandler` instance per registered
          type that claimed at least one target, in first-match order.

    Ordering within ``repo_targets`` and within each handler's ``targets``
    is stable (preserves input order).
    """
    repo_targets: list[str] = []
    handlers: dict[type[TargetHandler], TargetHandler] = {}
    for target in raw_targets:
        for handler_cls in TARGET_HANDLER_TYPES:
            if handler_cls.matches(target):
                handlers.setdefault(handler_cls, handler_cls()).add(target)
                break
        else:
            repo_targets.append(target)
    return repo_targets, list(handlers.values())


def special_target_env(handlers: list[TargetHandler]) -> dict[str, str | None]:
    """Container env contributed by special target types.

    Every key any registered type can contribute is present in the
    result; keys of types with no matching target stay None. The CLI's
    .env merge only fills keys absent from the container env dict, so
    the None seeding keeps e.g. a .env-file SLACK_BOT_TOKEN from being
    forwarded into sessions that have no Slack target.
    """
    env: dict[str, str | None] = {
        key: None
        for handler_cls in TARGET_HANDLER_TYPES
        for key in handler_cls.container_env_keys
    }
    for handler in handlers:
        env.update(handler.container_env())
    return env
