"""Host listener configuration and delivery to the local assistant inbox."""
from __future__ import annotations

import os
from dataclasses import dataclass

ENABLED_ENV = "LMER_ASSISTANT_MENTION_ROUTING"
BACKLOG_ENV = "LMER_ASSISTANT_MENTION_BACKLOG"
DEFAULT_BACKLOG = 10
MAX_BACKLOG = 100


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    backlog: int = DEFAULT_BACKLOG

    @classmethod
    def from_env(cls, env=None) -> "Settings":
        environ = os.environ if env is None else env
        raw = environ.get(ENABLED_ENV, "false").strip().lower()
        if raw not in {"", "0", "false", "no", "off", "1", "true", "yes", "on"}:
            raise ValueError(f"{ENABLED_ENV} must be true or false (1/0, yes/no, on/off)")
        try:
            backlog = int(environ.get(BACKLOG_ENV, str(DEFAULT_BACKLOG)))
        except (TypeError, ValueError):
            raise ValueError(f"{BACKLOG_ENV} must be an integer from 0 to {MAX_BACKLOG}") from None
        if not 0 <= backlog <= MAX_BACKLOG:
            raise ValueError(f"{BACKLOG_ENV} must be an integer from 0 to {MAX_BACKLOG}")
        return cls(raw in {"1", "true", "yes", "on"}, backlog)


def forward(transport: str, conversation: str, event: dict) -> dict:
    """Ask this host's daemon to admit an event; never fall back to spawning."""
    from lmer_platform import config
    from lmer_platform.client import Call, Endpoint, PlatformError, request

    resolved = config.load()
    credential = config.read_secret(resolved)
    if not credential:
        raise PlatformError("assistant routing needs the local platform daemon and its secret file")
    address = resolved.bind_address
    if address in {"0.0.0.0", "::"}:
        address = "127.0.0.1" if address == "0.0.0.0" else "::1"
    host = f"[{address}]" if ":" in address else address
    endpoint = Endpoint(f"http://{host}:{resolved.bind_port}", credential)
    response = request(endpoint, Call("POST", "/api/assistant/requests", body={
        "transport": transport, "conversation": conversation, "event": event,
    }), timeout=60.0)
    if response.status_code != 200:
        raise PlatformError(f"assistant request refused (HTTP {response.status_code})")
    result = response.json()
    if result.get("status") == "disabled":
        raise PlatformError(
            f"assistant routing is disabled on the daemon; align {ENABLED_ENV} "
            "and restart the daemon and listeners"
        )
    return result


def _matrix_mention(event, bot_id):
    from matrix_chat.listener import is_assistant_mention

    return is_assistant_mention(event, bot_id)


def route_connected_mention(transport, conversation, event, *, bot_id):
    """A mention targets the supervisor while ordinary worker replies stay connected."""
    if not Settings.from_env().enabled:
        return False
    if transport == "slack":
        mentioned = bool(bot_id and f"<@{bot_id}>" in str(event.get("text") or ""))
        event = {**event, "type": "app_mention", "channel": conversation}
    else:
        mentioned = _matrix_mention(event, bot_id)
    if not mentioned:
        return False
    forward(transport, conversation, event)
    return True
