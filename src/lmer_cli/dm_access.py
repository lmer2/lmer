"""Who may exchange direct messages with a run (issues #441 and #476).

Each transport's host-side allow list governs both listener and signal DMs.
Unset or empty means nobody. See docs/COMMS.md and docs/LMER-CLI.md.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from .threads import (
    CHANNEL_MATRIX, CHANNEL_SLACK, MAX_RECIPIENT_CHARS, ThreadError,
)

__all__ = [
    "SLACK_ALLOW_LIST_ENV", "MATRIX_ALLOW_LIST_ENV", "MAX_RECIPIENT_CHARS",
    "allowed_recipients", "check_recipient", "checked_recipient", "permits",
    "recipient_channel", "slack_user_id",
]

SLACK_ALLOW_LIST_ENV = "LMER_SLACK_DM_ALLOWED_USERS"
MATRIX_ALLOW_LIST_ENV = "LMER_MATRIX_ALLOWED_USERS"

#: A matrix user id. The ``:server`` half is also what tells the two transports
#: apart here, so this pattern and :func:`recipient_channel` have to agree.
_MXID = re.compile(r"^@[^\s:]+:[^\s:]+(?::\d+)?$")
_SLACK_ID = re.compile(r"^[UW][A-Z0-9]{8,}$")


def slack_user_id(value: str) -> bool:
    return isinstance(value, str) and bool(_SLACK_ID.fullmatch(value))


def _allow_list_env(recipient: str) -> str:
    return MATRIX_ALLOW_LIST_ENV if recipient_channel(recipient) == CHANNEL_MATRIX else SLACK_ALLOW_LIST_ENV


def allowed_recipients(recipient: str, env: Optional[dict] = None) -> list:
    """The recipient's transport list as the operator wrote it, empty entries dropped."""
    raw = ((env if env is not None else os.environ).get(_allow_list_env(recipient)) or "")
    return [entry.strip() for entry in raw.split(",") if entry.strip()]


def checked_recipient(value: object) -> str:
    """*value* as a recipient this side is willing to address, or a refusal.

    Shape only — whether the person exists is the transport's answer. The MXID
    check earns its place: a malformed localpart would otherwise reach
    ``createRoom`` as an invite the homeserver rejects halfway through making a
    room.
    """
    if not isinstance(value, str) or not value.strip():
        raise ThreadError(
            "a direct message needs a recipient — a Slack user id or username, "
            "or a matrix user id (@name:server)",
            status=400,
        )
    recipient = value.strip()
    if len(recipient) > MAX_RECIPIENT_CHARS:
        raise ThreadError(
            f"that recipient is {len(recipient)} characters; the limit is "
            f"{MAX_RECIPIENT_CHARS}",
            status=400,
        )
    if any(char.isspace() or ord(char) < 32 for char in recipient):
        raise ThreadError(
            "a recipient is named without whitespace or control characters",
            status=400,
        )
    if recipient_channel(recipient) == CHANNEL_MATRIX and not _MXID.match(recipient):
        raise ThreadError(
            f"{recipient} is not a matrix user id — those are @name:server",
            status=400,
        )
    return recipient


def recipient_channel(recipient: str) -> str:
    """Which transport *recipient* names, by the reading `parse_channel` uses."""
    return CHANNEL_MATRIX if ":" in recipient else CHANNEL_SLACK


def _slack_handle(value: str) -> str:
    return value.strip().lstrip("@").casefold()


def permits(recipient: str, *, spellings=(), env: Optional[dict] = None, entries=None) -> bool:
    """Whether the list names a transport-confirmed ID or Slack handle.

    For Slack, *recipient* is the confirmed user ID and *spellings* contains
    confirmed handles. Caller-supplied names and display names cannot authorize
    another account.
    """
    entries = allowed_recipients(recipient, env) if entries is None else entries
    if not entries:
        return False
    if recipient_channel(recipient) == CHANNEL_MATRIX:
        wanted = {name.strip() for name in (recipient, *spellings) if name}
        return any(entry.strip() in wanted for entry in entries)
    confirmed_handles = {
        _slack_handle(name) for name in spellings
        if isinstance(name, str) and name and not slack_user_id(name)
    }
    return any(
        entry.strip() == recipient if slack_user_id(entry.strip())
        else _slack_handle(entry) in confirmed_handles
        for entry in entries
    )


def check_recipient(
    recipient: str, *, spellings=(), env: Optional[dict] = None,
    shown: Optional[str] = None,
) -> None:
    """Refuse unless the operator's list names *recipient*.

    *shown* is what the refusal calls them — the spelling the run used, when
    that differs from the one compared.
    """
    if permits(recipient, spellings=spellings, env=env):
        return
    named = shown or recipient
    allow_list_env = _allow_list_env(recipient)
    if not allowed_recipients(recipient, env):
        raise ThreadError(
            f"{allow_list_env} is unset or empty in the host's environment, so "
            "this run may not open a direct message with anyone. Nothing was "
            "sent and nothing was posted anywhere else — say what you need in "
            "the conversation you are already in",
            status=403,
        )
    raise ThreadError(
        f"{named} is not on {allow_list_env}, so this run may not open a "
        "direct message with them. Nothing was sent. The operator keeps that "
        "list in the host's environment",
        status=403,
    )
