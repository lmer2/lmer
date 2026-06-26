"""Slack Web API client for chat operations."""

import os
import time
import requests
from typing import Any, Dict, List, Optional


# Maximum number of times to retry after an HTTP 429 response.
_MAX_RETRIES = 3


class SlackError(Exception):
    """Typed exception for Slack API errors."""
    pass


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
                    f"Slack API error: {error_code}"
                )
            return data

        # Should not be reached — the loop either returns or raises.
        raise SlackError("Slack API request failed after retries")  # pragma: no cover

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def post_message(
        self,
        channel: str,
        thread_ts: str,
        text: str,
    ) -> str:
        """Post a message to a channel thread.

        Args:
            channel: Slack channel ID.
            thread_ts: Thread timestamp to reply in.
            text: Message text.

        Returns:
            The ``ts`` of the posted message.

        Raises:
            SlackError: On Slack API errors.
        """
        payload = {
            "channel": channel,
            "thread_ts": thread_ts,
            "text": text,
        }
        data = self._request("POST", "chat.postMessage", json=payload)
        ts = data.get("ts")
        if ts is None:
            raise SlackError("Slack chat.postMessage response missing 'ts' field")
        return ts

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
