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
from typing import ClassVar

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

    def __init__(self) -> None:
        self.targets: list[str] = []

    @classmethod
    def matches(cls, target: str) -> bool:
        """Whether this handler claims the given positional target."""
        raise NotImplementedError

    def add(self, target: str) -> None:
        """Record a target this handler claimed via :meth:`matches`."""
        self.targets.append(target)

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


class SlackThreadTargets(TargetHandler):
    """Slack thread permalinks passed as ``lmer`` targets (issue #54).

    Only the first Slack target drives the container's chat context
    (``LMER_SLACK_CHANNEL`` / ``LMER_SLACK_THREAD_TS`` /
    ``LMER_SLACK_PERMALINK``); additional Slack targets are retained but
    currently unused. ``SLACK_BOT_TOKEN`` is required whenever a Slack
    target is present; ``SLACK_APP_TOKEN`` is optional and forwarded
    when set. Repo-less sessions are allowed for the ``chat`` task only
    — it is the only taskdef adapted for repo-less mode; every other
    task assumes code in /workspace.
    """

    repoless_task_ids = frozenset({"chat"})
    container_env_keys = (
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "LMER_SLACK_CHANNEL",
        "LMER_SLACK_THREAD_TS",
        "LMER_SLACK_PERMALINK",
    )

    def __init__(self) -> None:
        super().__init__()
        self.channel: str | None = None
        self.thread_ts: str | None = None
        self.permalink: str | None = None

    @classmethod
    def matches(cls, target: str) -> bool:
        return is_slack_thread_url(target)

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
            "SLACK_BOT_TOKEN": os.environ.get("SLACK_BOT_TOKEN"),
            "SLACK_APP_TOKEN": os.environ.get("SLACK_APP_TOKEN"),
            "LMER_SLACK_CHANNEL": self.channel,
            "LMER_SLACK_THREAD_TS": self.thread_ts,
            "LMER_SLACK_PERMALINK": self.permalink,
        }

    def on_session_start(self) -> None:
        """Record this Slack-thread attachment in the host-side registry.

        Lets the Slack listener detect that an lmer is already connected to the
        thread — including a session it didn't spawn, e.g. a manual
        ``lmer chat <permalink>`` from a shell — and decline to connect a second
        one (issue #74). The registry call is best-effort and keyed on the
        parsed ``channel`` / ``thread_ts``; with no Slack target parsed it is a
        no-op.
        """
        register(self.channel, self.thread_ts, permalink=self.permalink)

    def on_session_end(self) -> None:
        """Clear the registry entry recorded by :meth:`on_session_start`."""
        deregister(self.channel, self.thread_ts)


#: Registry of special target types, in match-priority order.
TARGET_HANDLER_TYPES: tuple[type[TargetHandler], ...] = (SlackThreadTargets,)


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
