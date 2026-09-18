"""Daemon-owned transport ingress for the assistant's private request inbox."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import os
import threading
import time

from lmer_cli.assistant_mentions import Settings
from matrix_chat.client import MatrixAccount, MatrixClient, MatrixError
from slack_chat.client import SlackClient

from . import mention_routing
from .store import utc_now_iso

logger = logging.getLogger(__name__)
IO_TIMEOUT = 10
SYNC_TIMEOUT_MS = 5000
RETRY_MAX = 30


class IngressError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


async def _blocking(fn, *args, **kwargs):
    # Finish the bounded transport call before its session is closed on shutdown.
    task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


class Ingress:
    def __init__(self, settings: Settings | None = None, *, env=None):
        self.env = dict(os.environ if env is None else env)
        self.settings = settings or Settings.from_env(self.env)
        self._lock = threading.Lock()
        self._tasks = []
        self._events = set()
        self._stopping = False
        self._matrix_since = None
        self._matrix_synced_rooms = set()
        self._states = {}
        for transport, required in (
            ("slack", ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")),
            ("matrix", ("LMER_MATRIX_HOMESERVER", "LMER_MATRIX_USER_ID", "LMER_MATRIX_ACCESS_TOKEN")),
        ):
            present = [bool(self.env.get(name, "").strip()) for name in required]
            error = None
            state = "stopped"
            if not self.settings.enabled:
                state = "disabled"
            elif not any(present):
                state = "unconfigured"
            elif not all(present):
                state = "misconfigured"
                error = "missing " + ", ".join(name for name, ok in zip(required, present) if not ok)
            self._states[transport] = {
                "up": False, "state": state, "last_event_at": None, "last_error": error,
            }

    def snapshot(self) -> dict:
        with self._lock:
            return {name: dict(state) for name, state in self._states.items()}

    def _update(self, transport: str, **values) -> None:
        with self._lock:
            self._states[transport].update(values)

    def _failure(self, transport: str, exc: Exception) -> None:
        reason = mention_routing.error_reason(exc)
        self._update(transport, up=False, state="retrying", last_error=reason)
        logger.warning("assistant_ingress_error transport=%s reason=%s", transport, reason)

    @asynccontextmanager
    async def lifespan(self, app):
        self._stopping = False
        for transport in ("slack", "matrix"):
            state = self.snapshot()[transport]
            if state["state"] == "stopped":
                self._update(transport, state="starting")
                self._tasks.append(asyncio.create_task(self._supervise(transport)))
            else:
                logger.info("assistant_ingress transport=%s state=%s error=%s",
                            transport, state["state"], state["last_error"])
        try:
            yield
        finally:
            self._stopping = True
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            await asyncio.gather(*self._events, return_exceptions=True)
            self._tasks.clear()

    async def _supervise(self, transport: str) -> None:
        failures = 0
        try:
            while True:
                try:
                    await getattr(self, f"_{transport}")()
                except Exception as exc:
                    self._failure(transport, exc)
                failures += 1
                await asyncio.sleep(min(RETRY_MAX, 2 ** min(failures - 1, 5)))
        finally:
            self._update(transport, up=False, state="stopped")

    async def _receive(self, transport: str, conversation: str, event: dict, client=None):
        self._update(transport, last_event_at=utc_now_iso())
        if self._stopping:
            mention_routing.drop(transport, conversation, event, "stopping")
            return
        try:
            return await _blocking(
                mention_routing.receive,
                {"transport": transport, "conversation": conversation, "event": event},
                self.settings, env=self.env, client=client,
            )
        except Exception as exc:
            # receive logs the event's refusal once; retain its reason in health.
            self._update(transport, last_error=mention_routing.error_reason(exc))

    async def _slack_event(self, socket, request):
        from slack_sdk.socket_mode.response import SocketModeResponse

        task = asyncio.current_task()
        self._events.add(task)
        reader = None
        event = (request.payload or {}).get("event") or {}
        conversation = event.get("channel") if isinstance(event, dict) else None
        self._update("slack", last_event_at=utc_now_iso())
        try:
            await socket.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
            if request.type != "events_api" or not isinstance(event, dict):
                mention_routing.drop("slack", conversation, event, "unsupported_event")
                return
            if event.get("type") != "app_mention" and not (
                event.get("type") == "message" and event.get("channel_type") == "im"
            ):
                mention_routing.drop("slack", conversation, event, "unsupported_event")
                return
            reader = SlackClient(self.env["SLACK_BOT_TOKEN"], timeout=IO_TIMEOUT, retries=0)
            await self._receive("slack", conversation, event, reader)
        except Exception as exc:
            reason = mention_routing.error_reason(exc)
            mention_routing.drop("slack", conversation, event, reason)
            self._update("slack", last_error=reason)
        finally:
            if reader is not None:
                reader.session.close()
            self._events.discard(task)

    async def _slack(self):
        import aiohttp
        from slack_sdk.socket_mode.aiohttp import SocketModeClient
        from slack_sdk.web.async_client import AsyncWebClient

        owner = self

        class ReportingSocket(SocketModeClient):
            def __init__(self, **kwargs):
                self.dispatches = set()
                super().__init__(**kwargs)

            async def run_message_listeners(self, message, raw_message):
                task = asyncio.current_task()
                self.dispatches.add(task)
                try:
                    await super().run_message_listeners(message, raw_message)
                finally:
                    self.dispatches.discard(task)

            async def close(self):
                await super().close()
                # SDK close leaves per-message futures (including reconnects) alive.
                tasks = list(self.dispatches)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

            async def issue_new_wss_url(self):
                try:
                    return await super().issue_new_wss_url()
                except Exception as exc:
                    owner._update("slack", up=False, state="reconnecting",
                                  last_error=mention_routing.error_reason(exc))
                    raise

        class Errors(logging.Handler):
            reason = None

            def emit(self, record):
                if record.exc_info and isinstance(record.exc_info[1], Exception):
                    self.reason = mention_routing.error_reason(record.exc_info[1])
                    owner._failure("slack", IngressError(self.reason))

        # SDK connect swallows exceptions; inspect exc_info without formatting its
        # message or traceback, which can contain signed URLs and message bodies.
        errors = Errors()
        sdk_logger = logging.Logger(__name__ + ".slack_sdk", level=logging.ERROR)
        sdk_logger.propagate = False
        sdk_logger.addHandler(errors)
        async with aiohttp.ClientSession() as session:
            web = AsyncWebClient(token=self.env["SLACK_BOT_TOKEN"], session=session,
                                 timeout=IO_TIMEOUT, logger=sdk_logger)
            socket = ReportingSocket(app_token=self.env["SLACK_APP_TOKEN"], web_client=web,
                                     logger=sdk_logger)
            socket.socket_mode_request_listeners.append(self._slack_event)
            try:
                await web.auth_test()
                socket.wss_uri = await socket.issue_new_wss_url()
                try:
                    await asyncio.wait_for(socket.connect(), timeout=IO_TIMEOUT)
                except TimeoutError:
                    raise IngressError(errors.reason or "TimeoutError") from None
                while True:
                    up = await socket.is_connected()
                    was_up = self.snapshot()["slack"]["up"]
                    self._update("slack", up=up, state="up" if up else "reconnecting")
                    if was_up and not up:
                        self._update("slack", last_error="disconnected")
                    await asyncio.sleep(1)
            finally:
                await socket.close()

    async def _matrix(self):
        account = MatrixAccount(
            homeserver=self.env["LMER_MATRIX_HOMESERVER"],
            user_id=self.env["LMER_MATRIX_USER_ID"],
            access_token=self.env["LMER_MATRIX_ACCESS_TOKEN"],
        )
        reader = MatrixClient(account, timeout=IO_TIMEOUT)
        rooms = {room.strip() for room in self.env.get("LMER_MATRIX_ROOMS", "").split(",") if room.strip()}
        try:
            if await _blocking(reader.whoami) != account.user_id:
                raise IngressError("matrix_account_mismatch")
            joined = {}
            retry_at = 0.0
            room_filter = {
                "timeline": {"limit": 20, "types": ["m.room.message"]},
                "state": {"lazy_load_members": True, "types": []},
                "ephemeral": {"types": []}, "account_data": {"types": []},
            }
            sync_filter = {"room": room_filter, "presence": {"types": []}, "account_data": {"types": []}}
            while True:
                if rooms - joined.keys() and time.monotonic() >= retry_at:
                    for room in sorted(rooms - joined.keys()):
                        try:
                            joined[room] = await _blocking(reader.join, room)
                        except MatrixError as exc:
                            reason = mention_routing.error_reason(exc)
                            self._update("matrix", last_error=reason)
                            logger.warning("assistant_ingress_join_failed transport=matrix conversation=%s reason=%s",
                                           room, reason)
                    retry_at = time.monotonic() + RETRY_MAX
                if rooms and not joined:
                    self._update("matrix", up=False, state="retrying")
                    await asyncio.sleep(max(0, retry_at - time.monotonic()))
                    continue
                selected = sorted(set(joined.values()))
                if rooms:
                    room_filter["rooms"] = selected
                body = await _blocking(reader.sync, since=self._matrix_since,
                                       filter_body=sync_filter, timeout_ms=SYNC_TIMEOUT_MS)
                first = self._matrix_since is None
                initial_rooms = set(selected) - self._matrix_synced_rooms
                cursor = body.get("next_batch")
                if not isinstance(cursor, str) or not cursor:
                    raise IngressError("matrix_missing_sync_cursor")
                self._matrix_since = cursor
                self._matrix_synced_rooms.update(selected)
                self._update("matrix", up=True, state="up")
                for room_id, room in ((body.get("rooms") or {}).get("join") or {}).items():
                    timeline = room.get("timeline") or {}
                    if timeline.get("limited"):
                        logger.warning("assistant_ingress_gap transport=matrix conversation=%s reason=limited_sync", room_id)
                        self._update("matrix", last_error="limited_sync")
                    for event in timeline.get("events") or []:
                        self._update("matrix", last_event_at=utc_now_iso())
                        if first or room_id in initial_rooms:
                            mention_routing.drop("matrix", room_id, event, "initial_sync")
                        elif rooms and room_id not in selected:
                            mention_routing.drop("matrix", room_id, event, "room_not_selected")
                        else:
                            await self._receive("matrix", room_id, event, reader)
        finally:
            reader._session.close()
