"""``lmer-matrix-listener``: route mentions or spawn a chat session per thread.

``LMER_ASSISTANT_MENTION_ROUTING`` selects the assistant's private inbox;
``docs/ASSISTANT-MENTIONS.md`` describes that mode. The default is below.

The mirror of :mod:`slack_chat.listener`. One host-side process logged in as
the bot account syncs the rooms it is joined to (or the ones
``LMER_MATRIX_ROOMS`` names), and when an **allowlisted** MXID mentions the
bot it spawns ``lmer chat <matrix.to permalink>`` for that message's thread —
the message itself as the thread root, or the thread it was posted in. The
connector inside that ``lmer`` process then carries the conversation; the
listener relays no content.

The allowlist (``LMER_MATRIX_ALLOWED_USERS``) is explicit MXIDs, never a
server, a glob or room membership: the homeserver federates and its
providers may be open-signup, so being in the room authorises nothing. Empty
means nobody, on both transports.

Capacity, per-thread dedup and the idle reaper are
:class:`slack_chat.sessions.SessionManager`, keyed on ``(room_id,
thread_root)`` where it used ``(channel, thread_ts)``; the ``LMER_SLACK_CHAT_*``
tunables it reads apply to both listeners.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Optional

from lmer_cli import assistant_mentions

from slack_chat.registry import is_thread_connected
from slack_chat.sessions import SessionManager, preset_ack_parts

from .client import MatrixAccount, MatrixClient, MatrixError
from .connector import ACCESS_TOKEN_ENV, HOMESERVER_ENV, USER_ID_ENV, resolve_thread_root
from .permalink import matrix_to_link

logger = logging.getLogger(__name__)

__all__ = ["Listener", "ListenerConfig", "is_mention", "is_assistant_mention", "main"]

ALLOWED_USERS_ENV = "LMER_MATRIX_ALLOWED_USERS"
ROOMS_ENV = "LMER_MATRIX_ROOMS"
SYNC_TIMEOUT_MS = 25000


def _csv(raw: Optional[str]) -> set:
    return {item.strip() for item in (raw or "").split(",") if item.strip()}


class ListenerConfig:
    def __init__(self, env=None) -> None:
        environ = os.environ if env is None else env
        self.homeserver = (environ.get(HOMESERVER_ENV) or "").strip()
        self.user_id = (environ.get(USER_ID_ENV) or "").strip()
        self.access_token = (environ.get(ACCESS_TOKEN_ENV) or "").strip()
        self.allowed = _csv(environ.get(ALLOWED_USERS_ENV))
        self.rooms = _csv(environ.get(ROOMS_ENV))
        self.mentions = assistant_mentions.Settings.from_env(environ)
        missing = [name for name, value in (
            (HOMESERVER_ENV, self.homeserver), (USER_ID_ENV, self.user_id), (ACCESS_TOKEN_ENV, self.access_token),
        ) if not value]
        if missing:
            raise ValueError(f"the matrix listener needs {', '.join(missing)}")

    @property
    def account(self) -> MatrixAccount:
        return MatrixAccount(homeserver=self.homeserver, user_id=self.user_id, access_token=self.access_token)


def is_mention(event: dict, user_id: str) -> bool:
    """Whether *event* mentions the bot: an ``m.mentions`` entry, or the MXID
    or its localpart in the body."""
    content = event.get("content") if isinstance(event.get("content"), dict) else {}
    mentions = content.get("m.mentions") if isinstance(content.get("m.mentions"), dict) else {}
    if user_id in (mentions.get("user_ids") or []):
        return True
    body = str(content.get("body") or "")
    localpart = user_id[1:].split(":", 1)[0]
    return user_id in body or f"@{localpart}" in body


def is_assistant_mention(event: dict, user_id: str) -> bool:
    """Require an explicit Matrix mention identity for assistant admission."""
    content = event.get("content")
    mentions = content.get("m.mentions") if isinstance(content, dict) else None
    users = mentions.get("user_ids") if isinstance(mentions, dict) else None
    return isinstance(users, list) and user_id in users


class Listener:
    def __init__(self, config: ListenerConfig, sessions: SessionManager, *, matrix: Optional[MatrixClient] = None) -> None:
        self.config = config
        self.sessions = sessions
        self._matrix = matrix
        self._next_batch: Optional[str] = None
        self._lock = asyncio.Lock()

    @property
    def matrix(self) -> MatrixClient:
        if self._matrix is None:
            self._matrix = MatrixClient(self.config.account)
        return self._matrix

    def _filter(self) -> dict:
        room_filter: dict = {
            "timeline": {"limit": 20, "types": ["m.room.message"]},
            "state": {"lazy_load_members": True, "types": []},
            "ephemeral": {"types": []},
            "account_data": {"types": []},
        }
        if self.config.rooms:
            room_filter["rooms"] = sorted(self.config.rooms)
        return {"room": room_filter, "presence": {"types": []}, "account_data": {"types": []}}

    def sync_once(self, *, timeout_ms: int = SYNC_TIMEOUT_MS) -> list:
        """One sync; returns the ``(room_id, event)`` pairs worth acting on."""
        body = self.matrix.sync(since=self._next_batch, filter_body=self._filter(), timeout_ms=timeout_ms)
        first = self._next_batch is None
        self._next_batch = body.get("next_batch") or self._next_batch
        if first:
            return []  # the initial sync is history, not a request
        found = []
        for room_id, room in ((body.get("rooms") or {}).get("join") or {}).items():
            for event in ((room.get("timeline") or {}).get("events") or []):
                if not isinstance(event, dict) or event.get("type") != "m.room.message":
                    continue
                sender = event.get("sender")
                if sender == self.config.user_id:
                    continue
                threaded = resolve_thread_root(event) != event.get("event_id")
                mentioned = (is_assistant_mention if self.config.mentions.enabled else is_mention)(
                    event, self.config.user_id,
                )
                if not mentioned and not (self.config.mentions.enabled and threaded):
                    continue
                if sender not in self.config.allowed:
                    logger.info(
                        "matrix_listener_denied room=%s sender=%s allow_list=%s",
                        room_id, sender, ALLOWED_USERS_ENV,
                    )
                    continue
                found.append((room_id, event))
        return found

    async def handle(self, room_id: str, event: dict) -> None:
        root = resolve_thread_root(event)
        permalink = matrix_to_link(room_id, root)
        async with self._lock:
            if self.sessions.touch(room_id, root):
                return
            if is_thread_connected(room_id, root, kind="matrix"):
                return
            if self.config.mentions.enabled:
                try:
                    await asyncio.to_thread(assistant_mentions.forward, "matrix", room_id, event)
                except Exception as exc:
                    logger.error("assistant_routing_failed transport=matrix error=%s", exc)
                return
            if self.sessions.at_capacity():
                await self._say(room_id, root, "I can't take on another conversation right now — all session slots are busy.")
                return
            try:
                await self.sessions.spawn(room_id, root, permalink)
            except Exception as exc:
                logger.exception("matrix_listener_spawn_failed room=%s error=%s", room_id, exc)
                await self._say(room_id, root, "❌ Could not start a session for this thread.")
                return
        # The preset clause is the Slack listener's, imported rather than
        # rewritten: a session silently getting a preset nobody asked for is the
        # confusion issue #181 exists to end, and it is as invisible from a
        # matrix room as it is from Slack.
        clause, warning = preset_ack_parts(None, self.sessions)
        ack = f"Connecting a session to this thread{clause}… ⏳ (the first reply can take a minute)"
        if warning:
            ack += f"\n{warning}"
        await self._say(room_id, root, ack)

    async def _say(self, room_id: str, root: str, text: str) -> None:
        try:
            await asyncio.to_thread(self.matrix.send_thread_text, room_id, root, text, msgtype="m.notice")
        except MatrixError as exc:
            logger.warning("matrix_listener_say_failed room=%s error=%s", room_id, exc)

    async def run(self) -> None:
        me = await asyncio.to_thread(self.matrix.whoami)
        if me != self.config.user_id:
            raise MatrixError(f"the access token belongs to {me}, not {self.config.user_id}")
        for room in sorted(self.config.rooms):
            try:
                await asyncio.to_thread(self.matrix.join, room)
            except MatrixError as exc:
                logger.warning("matrix_listener_join_failed room=%s error=%s", room, exc)
        reaper = asyncio.create_task(self.sessions.run_reaper())
        logger.info(
            "matrix_listener_started user=%s allowed=%d rooms=%s max_sessions=%s",
            self.config.user_id, len(self.config.allowed), sorted(self.config.rooms) or "all joined",
            self.sessions.max_sessions,
        )
        failures = 0
        try:
            while True:
                try:
                    found = await asyncio.to_thread(self.sync_once)
                    failures = 0
                except MatrixError as exc:
                    failures += 1
                    logger.warning("matrix_listener_sync_failed error=%s", exc)
                    await asyncio.sleep(min(30.0, 0.5 * (2 ** min(failures, 6))))
                    continue
                for room_id, event in found:
                    await self.handle(room_id, event)
        finally:
            reaper.cancel()
            await self.sessions.shutdown_all()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="lmer-matrix-listener",
        description=(
            "Host-side matrix listener that spawns an 'lmer chat' session per thread "
            "when an allowlisted user mentions the bot. Requires LMER_MATRIX_HOMESERVER, "
            "LMER_MATRIX_USER_ID, LMER_MATRIX_ACCESS_TOKEN and LMER_MATRIX_ALLOWED_USERS, "
            "plus the lmer CLI on PATH."
        ),
    )
    parser.add_argument("--log-level", default=os.getenv("LMER_MATRIX_LOG_LEVEL", "INFO"))
    parser.add_argument("--lmer-env-file", default=None, help="Forwarded to each spawned 'lmer chat' as 'lmer --env-file'.")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    from lmer_cli.runtime import apply_env_file_defaults, default_env_file_candidates
    from lmer_cli.tls import ensure_ca_bundle

    apply_env_file_defaults(default_env_file_candidates(), references_from_environ=True)
    ensure_ca_bundle()
    try:
        config = ListenerConfig()
    except ValueError as exc:
        print(f"lmer-matrix-listener: {exc}", file=sys.stderr)
        return 1
    if not config.allowed:
        print(
            f"lmer-matrix-listener: {ALLOWED_USERS_ENV} is unset or empty — "
            "refusing inbound messages; name the MXIDs allowed to mention the bot",
            file=sys.stderr,
        )
        return 1
    listener = Listener(config, SessionManager(lmer_env_file=args.lmer_env_file, transport="matrix"))
    try:
        asyncio.run(listener.run())
    except MatrixError as exc:
        print(f"lmer-matrix-listener: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
