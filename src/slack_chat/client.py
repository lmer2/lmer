"""Slack Web API client for chat operations."""

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

from lmer_cli import runref


# Maximum number of times to retry after an HTTP 429 response.
_MAX_RETRIES = 3
REACTION_BUDGET_SECONDS = 5.0
_REACTION_WORKER = Path(__file__).with_name("_reaction_request.py")
# Slack's replies response schema includes messages[].blocks[].block_id;
# the upstream references for this transport marker are in docs/COMMS.md §9.
CONNECTION_NOTICE_BLOCK_PREFIX = "lmer-connection-status:"


def strip_run_references(text: str) -> str:
    r"""Reduce every run reference in *text* to the label it shows (issue #241).

    Slack's `mrkdwn` has no `[label](url)` form (it spells links `<url|label>`)
    and nothing here converts markdown to it, so a reference posted verbatim
    arrives as raw characters where the run's name should be. Converted rather
    than left to the assistant, which cannot know whether its chat is bridged.

    **The substitution is context-free** — it does not know what a code span or
    a fenced block is, so a reference written as an *example* is reduced too. A
    thin name over `lmer_cli.runref` (#383 item 3), kept because the bridge's
    callers reach for it; the rest is in `docs/PLATFORM-QUICKSTART.md`, "Run
    references in chat".
    """
    return runref.strip_references(text)


class SlackError(Exception):
    """Typed exception for Slack API errors.

    ``code`` is Slack's own ``error`` field when the failure was an ``ok: false``
    reply, and ``None`` for a transport or HTTP failure. Carried as a field
    because callers classify on it — "your token cannot see this channel" and
    "Slack is having a bad day" are different answers with different statuses —
    and the message text is not a contract anything should match on.
    """

    def __init__(self, message: str, *, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code


class SlackClient:
    """Hand-rolled Slack Web API client using requests.Session with Bearer auth."""

    BASE_URL = "https://slack.com/api"

    def __init__(self, bot_token: Optional[str] = None):
        """Initialize SlackClient.

        Args:
            bot_token: Slack bot token. Defaults to the SLACK_BOT_TOKEN
                       environment variable.

        Raises:
            SlackError: If no token is available.
        """
        self.bot_token = bot_token or os.getenv("SLACK_BOT_TOKEN")

        if not self.bot_token:
            raise SlackError(
                "Slack bot token required. Set SLACK_BOT_TOKEN environment "
                "variable or pass bot_token parameter."
            )

        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        })

    def _request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        """Make an API request to the Slack Web API with 429 retry handling.

        Args:
            method: HTTP method (GET, POST, etc.).
            endpoint: Slack API method name (e.g. 'chat.postMessage').
            **kwargs: Passed through to requests.Session.request.

        Returns:
            Parsed JSON response body.

        Raises:
            SlackError: On {ok: false} responses or persistent HTTP 429 errors.
        """
        return self._request_with_response(method, endpoint, **kwargs)[0]

    def _request_with_response(self, method: str, endpoint: str, **kwargs):
        """The same call, with the response beside the parsed body.

        Slack states the token's **granted scopes** in a response header
        (`x-oauth-scopes`) rather than in any body, and one caller has to read
        them: opening a thread refuses an app carrying `chat:write.public`,
        because that scope is what makes `not_in_channel` — lmer's only evidence
        that a channel is off limits — stop firing (issue #429).
        """
        url = f"{self.BASE_URL}/{endpoint}"

        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = self.session.request(method, url, **kwargs)
                response.raise_for_status()
            except requests.HTTPError as exc:
                resp = exc.response
                if resp is not None and resp.status_code == 429:
                    if attempt < _MAX_RETRIES:
                        # Slack sends integer seconds, but Retry-After may
                        # legally be an HTTP-date — fall back rather than raise.
                        try:
                            retry_after = int(resp.headers.get("Retry-After", 1))
                        except ValueError:
                            retry_after = 1
                        time.sleep(retry_after)
                        continue
                    raise SlackError(
                        f"Slack API rate limited after {_MAX_RETRIES} retries "
                        f"(HTTP 429)"
                    ) from exc
                raise SlackError(f"Slack API HTTP error: {exc}") from exc
            except requests.RequestException as exc:
                raise SlackError(f"Request failed: {exc}") from exc

            data: Dict[str, Any] = response.json()
            if not data.get("ok"):
                error_code = data.get("error", "unknown_error")
                raise SlackError(
                    f"Slack API error: {error_code}", code=error_code
                )
            return data, response

        # Should not be reached — the loop either returns or raises.
        raise SlackError("Slack API request failed after retries")  # pragma: no cover

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_reaction(self, channel: str, timestamp: str, name: str) -> None:
        """Best-effort reaction with a killable deadline, including DNS and cleanup."""
        try:
            result = subprocess.run(
                [sys.executable, "-I", str(_REACTION_WORKER)],
                input=json.dumps({"url": f"{self.BASE_URL}/reactions.add",
                                  "headers": dict(self.session.headers),
                                  "payload": {"channel": channel, "timestamp": timestamp, "name": name},
                                  "timeout": REACTION_BUDGET_SECONDS}),
                text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=REACTION_BUDGET_SECONDS, check=True, close_fds=True,
            )
            result = json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise SlackError(f"Reaction request failed: {exc.__class__.__name__}") from exc
        if "failure" in result:
            raise SlackError(f"Reaction request failed: {result['failure']}")
        data = result["data"]
        if not data.get("ok") and data.get("error") != "already_reacted":
            error = data.get("error", "unknown_error")
            raise SlackError(f"Slack API error: {error}", code=error)

    def post_message(
        self,
        channel: str,
        thread_ts: str,
        text: str,
        *,
        connection_notice: bool = False,
    ) -> str:
        """Post a message to a channel thread.

        Args:
            channel: Slack channel ID.
            thread_ts: Thread timestamp to reply in.
            text: Message text.
            connection_notice: Mark a connector lifecycle notice for later attach snapshots.

        Returns:
            The ``ts`` of the posted message.

        Raises:
            SlackError: On Slack API errors.
        """
        payload = {
            "channel": channel,
            "thread_ts": thread_ts,
            # The single egress: converting per caller would leave the next
            # poster added here a surface that leaks raw markdown (#241).
            "text": strip_run_references(text),
        }
        if connection_notice:
            payload["blocks"] = [{
                "type": "section",
                "block_id": CONNECTION_NOTICE_BLOCK_PREFIX + uuid.uuid4().hex,
                "text": {"type": "mrkdwn", "text": payload["text"]},
            }]
        data = self._request("POST", "chat.postMessage", json=payload)
        ts = data.get("ts")
        if ts is None:
            raise SlackError("Slack chat.postMessage response missing 'ts' field")
        return ts

    def post_root(self, channel: str, text: str) -> Dict[str, Any]:
        """Post a message that is not a reply, opening a thread on it (#429).

        Args:
            channel: Slack channel ID, or a channel name (``#general``), which
                     ``chat.postMessage`` resolves itself.
            text: Message text.

        Returns:
            ``{"channel": <the id Slack resolved>, "ts": <the root's ts>}``.

        Raises:
            SlackError: On Slack API errors — ``not_in_channel`` and
                        ``channel_not_found`` among them, which is how a channel
                        the bot is not in is refused by name.
        """
        payload = {"channel": channel, "text": strip_run_references(text)}
        data = self._request("POST", "chat.postMessage", json=payload)
        ts = data.get("ts")
        resolved = data.get("channel")
        if ts is None or not isinstance(resolved, str):
            raise SlackError(
                "Slack chat.postMessage response missing 'ts' or 'channel' field"
            )
        return {"channel": resolved, "ts": ts}

    def open_dm(self, user_id: str) -> str:
        """The id of the 1:1 conversation with *user_id*, opening it if needed.

        ``conversations.open`` is idempotent: asked twice for the same person it
        returns the same channel, so a DM never stacks up conversations. Needs
        ``im:write``; Slack answers ``cannot_dm_bot`` for another app and
        ``user_not_found`` for an id nobody holds, and both are passed through
        by code (issue #441).
        """
        data = self._request("POST", "conversations.open", json={"users": user_id})
        channel = (data.get("channel") or {}).get("id")
        if not isinstance(channel, str) or not channel:
            raise SlackError("Slack conversations.open response carries no channel id")
        return channel

    def find_user(self, name: str) -> list:
        """Every user whose handle or display name is *name*, case-insensitively.

        A list rather than one user because the caller has to be able to tell
        "nobody" from "two people" — a DM sent to the wrong one of two matches is
        not recoverable. Needs ``users:read``; Slack answers ``missing_scope``
        without it, which the caller translates rather than swallows.
        """
        wanted = name.lstrip("@").casefold()
        found, cursor = [], None
        while True:
            params = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = self._request("GET", "users.list", params=params)
            for member in data.get("members") or []:
                if member.get("deleted"):
                    continue
                profile = member.get("profile") or {}
                names = {
                    str(member.get("name") or "").casefold(),
                    str(profile.get("display_name") or "").casefold(),
                }
                if wanted in names - {""}:
                    found.append({"id": member.get("id"), "name": member.get("name")})
            cursor = ((data.get("response_metadata") or {}).get("next_cursor") or "").strip()
            if not cursor:
                return found

    def workspace(self) -> Dict[str, Any]:
        """``{"host": …, "scopes": [...]}`` — the workspace, and what this token may do.

        One ``auth.test``, which needs no scope of its own, answering both
        questions opening a thread has to ask: which host to compose the new
        thread's permalink on, and whether the app carries a scope that would
        let it post into a channel nobody invited it to. ``scopes`` is ``None``
        when the header is absent — a proxy that strips it, an older API — which
        is *not knowing* rather than "no scopes".
        """
        data, response = self._request_with_response("POST", "auth.test")
        url = data.get("url")
        host = urlparse(url).hostname if isinstance(url, str) else None
        if not host:
            raise SlackError("Slack auth.test response carries no workspace url")
        granted = response.headers.get("x-oauth-scopes")
        return {
            "host": host,
            "scopes": (
                [scope.strip() for scope in granted.split(",") if scope.strip()]
                if isinstance(granted, str) else None
            ),
        }

    def get_replies(
        self,
        channel: str,
        thread_ts: str,
        oldest: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve all replies in a thread, following cursor pagination.

        Args:
            channel: Slack channel ID.
            thread_ts: Thread timestamp (parent message ``ts``).
            oldest: Optional lower bound timestamp for messages.

        Returns:
            Flat list of message dicts across all pages.

        Raises:
            SlackError: On Slack API errors.
        """
        params: Dict[str, Any] = {
            "channel": channel,
            "ts": thread_ts,
        }
        if oldest is not None:
            params["oldest"] = oldest

        messages: List[Dict[str, Any]] = []

        while True:
            data = self._request("GET", "conversations.replies", params=params)
            messages.extend(data.get("messages", []))

            next_cursor = (
                data.get("response_metadata", {}).get("next_cursor") or ""
            )
            if not next_cursor:
                break
            params["cursor"] = next_cursor

        return messages

    def history_before(self, channel: str, before: str, limit: int) -> list:
        """Up to *limit* messages strictly before *before*, oldest first."""
        if limit == 0:
            return []
        found: list = []
        cursor = ""
        seen: set = set()
        while len(found) < limit:
            params = {"channel": channel, "latest": before, "inclusive": False,
                      "limit": limit - len(found)}
            if cursor:
                params["cursor"] = cursor
            data = self._request("GET", "conversations.history", params=params)
            found.extend(item for item in data.get("messages", [])
                         if isinstance(item, dict) and item.get("ts", "") < before)
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
            if cursor in seen:
                raise SlackError("history pagination repeated a cursor")
            seen.add(cursor)
        return list(reversed(found[:limit]))

    def auth_test(self) -> str:
        """Verify the bot token and return the bot's user ID.

        Returns:
            The ``user_id`` from auth.test. (Slack's auth.test response has
            no ``bot_user_id`` field — for an xoxb token, ``user_id`` is the
            bot's own user ID, which is what message ``user`` fields carry.)

        Raises:
            SlackError: On Slack API errors.
        """
        data = self._request("POST", "auth.test")
        user_id = data.get("user_id")
        if user_id is None:
            raise SlackError("Slack auth.test response missing 'user_id' field")
        return user_id
