"""Admit listener events to UL without connecting or broadcasting a conversation."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

from lmer_cli import dm_access, dm_parents, thread_access
from lmer_cli.assistant_mentions import Settings
from lmer_cli.threads import ThreadError, parse_thread
from matrix_chat.connector import resolve_thread_root, strip_reply_fallback
from matrix_chat.client import MatrixError
from matrix_chat.listener import is_assistant_mention
from matrix_chat.permalink import matrix_to_link
from slack_chat.permalink import slack_thread_link
from slack_chat.client import SlackError
from slack_chat.registry import is_thread_connected

from . import assistant, connectors, projects
from . import project_requests

logger = logging.getLogger(__name__)
MAX_TEXT = 8000


class _MentionLabels(HTMLParser):
    def __init__(self, user_id):
        super().__init__()
        self.user_id = user_id
        self.labels = []
        self.label = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            try:
                link = urlsplit(dict(attrs).get("href") or "")
            except ValueError:
                return
            if link.hostname == "matrix.to" and unquote(link.fragment).split("?")[0].lstrip("/") == self.user_id:
                self.label = ""

    def handle_data(self, data):
        if self.label is not None:
            self.label += data

    def handle_endtag(self, tag):
        if tag == "a" and self.label is not None:
            self.labels.append(self.label)
            self.label = None


def _request_text(transport, event, env, reader):
    if transport == "slack":
        text = str(event.get("text") or "")
        prefix = re.match(r"^\s*<@([A-Z0-9]+)>[:,]?(?:\s+|$)", text)
        if prefix:
            try:
                bot_user_id = reader.auth_test()
            except SlackError as exc:
                raise ThreadError("could not verify the Slack bot identity", status=502) from exc
            if prefix[1] == bot_user_id:
                return text[prefix.end():]
        return text
    content = event["content"]
    text = strip_reply_fallback(str(content.get("body") or "")).lstrip()
    user_id = thread_access.matrix_account(env).user_id
    labels = _MentionLabels(user_id)
    labels.feed(str(content.get("formatted_body") or ""))
    for label in sorted({user_id, user_id.split(":")[0], *labels.labels}, key=len, reverse=True):
        if label:
            match = re.match(re.escape(label) + r"[:,]?(?:\s+|$)", text)
            if match:
                return text[match.end():]
    return text


def _required(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ThreadError(f"{name} must be a non-empty identifier of at most 1024 characters", status=400)
    return value


def _message(transport: str, message: dict) -> dict:
    content = (message.get("content") or {}) if transport == "matrix" else message
    text = str(content.get("body" if transport == "matrix" else "text") or "")
    return {
        "id": message.get("event_id" if transport == "matrix" else "ts"),
        "sender": message.get("sender" if transport == "matrix" else "user"),
        "text": text[:MAX_TEXT], "clipped": len(text) > MAX_TEXT,
    }


def _slack_allowed(client, sender: str, env) -> bool:
    entries = dm_access.allowed_recipients(sender, env)
    if dm_access.permits(sender, entries=entries):
        return True
    if all(dm_access.slack_user_id(entry) for entry in entries):
        return False
    reply = client._request("GET", "users.info", params={"user": sender})
    member = reply.get("user") or {}
    return member.get("id") == sender and dm_access.permits(
        sender, spellings=(member.get("name"),), entries=entries,
    )


def _held(transport: str, conversation: str, root: str, permalink: str) -> bool:
    return bool(
        is_thread_connected(conversation, root, kind=transport)
        or connectors.ensure_connectors().live_holder(parse_thread(permalink))
    )


def drop(transport, conversation, event, reason: str) -> None:
    """Record identifiers and a reason, never transport message bodies."""
    event = event if isinstance(event, dict) else {}
    allow_list = (dm_access.SLACK_ALLOW_LIST_ENV if transport == "slack"
                  else dm_access.MATRIX_ALLOW_LIST_ENV)
    logger.info(
        "assistant_ingress_dropped transport=%s conversation=%s event=%s sender=%s reason=%s allow_list=%s",
        transport, conversation, event.get("event_id") or event.get("ts"),
        event.get("sender") or event.get("user"), reason, allow_list,
    )


def error_reason(exc: Exception) -> str:
    """Prefer transport codes; exception prose may contain request contents."""
    cause = exc
    while True:
        code = getattr(cause, "code", None) or getattr(cause, "errcode", None)
        if code:
            return str(code)
        response = getattr(cause, "response", None)
        if response is not None and hasattr(response, "get"):
            code = response.get("error")
            if code:
                return str(code)
        if cause.__cause__ is None:
            break
        cause = cause.__cause__
    return type(cause).__name__


class _Ignored(Exception):
    pass


def receive(body: dict, settings: Settings, *, env=None, client=None) -> dict:
    try:
        result = _receive(body, settings, env=env, client=client)
    except _Ignored as exc:
        result = {"status": "ignored", "reason": str(exc)}
    except Exception as exc:
        drop(body.get("transport"), body.get("conversation"), body.get("event"), error_reason(exc))
        raise
    if result["status"] != "accepted":
        drop(body.get("transport"), body.get("conversation"), body.get("event"),
             result.get("reason", result["status"]))
    return result


def _receive(body: dict, settings: Settings, *, env=None, client=None) -> dict:
    """Return accepted/duplicate/ignored; failure leaves the event retryable."""
    if not settings.enabled:
        return {"status": "disabled"}
    environ = os.environ if env is None else env
    transport = body.get("transport")
    if not isinstance(transport, str) or transport not in {"slack", "matrix"}:
        raise ThreadError("transport must be slack or matrix", status=400)
    conversation = _required(body.get("conversation"), "conversation")
    event = body.get("event")
    if not isinstance(event, dict):
        raise ThreadError("event must be an object", status=400)
    try:
        if transport == "slack":
            prepared = _slack(conversation, event, environ, client)
        else:
            prepared = _matrix(conversation, event, environ, client)
    except (SlackError, MatrixError) as exc:
        raise ThreadError(f"could not prepare the {transport} request: {exc}", status=502) from exc
    sender, event_id, root, permalink, parent, reader = prepared
    if not projects.enabled() and _held(transport, conversation, root, permalink):
        return {"status": "connected"}
    identity = hashlib.sha256(json.dumps(
        [transport, conversation, event_id], separators=(",", ":"),
    ).encode()).hexdigest()
    if not projects.enabled() and identity in assistant.read_state().conversation_seen:
        return {"status": "duplicate"}
    try:
        backlog = reader.history_before(conversation, event_id, settings.backlog) if not parent and root == event_id else []
    except (SlackError, MatrixError) as exc:
        raise ThreadError(f"could not read the {transport} context: {exc}", status=502) from exc
    payload = {
        "entry": "dm_reply" if parent else "mention",
        "source": {"channel": transport, "author": sender, "ref": event_id},
        "sender": sender, "conversation": conversation, "root": root,
        "permalink": permalink, "message": _message(transport, event),
        "backlog": [_message(transport, item) for item in backlog],
        "backlog_limit": settings.backlog,
        "parent": parent,
        "instruction": (
            f"[{transport} · {sender}] Read this request and its thread. "
            "Reply only to the permalink with lmer-ctl thread post, or attach a worker "
            "there under the operator's run-start policy. Do not broadcast its contents."
        ),
    }
    if projects.enabled():
        if not parent:
            message = payload["message"]
            message["original_text"] = message["text"]
            message["text"] = _request_text(transport, event, environ, reader)[:MAX_TEXT]
        return project_requests.admit(identity, payload, reader)
    # A connection may have been established while history was being fetched.
    if _held(transport, conversation, root, permalink):
        return {"status": "connected"}
    accepted = assistant.notify_conversation(identity, payload)
    live = assistant._live_assistant()
    return {"status": "accepted" if accepted else "duplicate",
            "session": live.get("id") if live else None, "permalink": permalink}


def _slack(conversation: str, event: dict, env, client):
    if event.get("bot_id") or event.get("subtype"):
        raise _Ignored("bot_or_subtype")
    sender = _required(event.get("user"), "sender")
    event_id = _required(event.get("ts"), "message id")
    root = _required(event.get("thread_ts") or event_id, "thread root")
    parent = dm_parents.lookup("slack", conversation, root) if event.get("thread_ts") else None
    is_dm = conversation.startswith("D") or event.get("channel_type") == "im"
    if is_dm and not parent:
        raise _Ignored("unmatched_dm_parent")
    if not parent and event.get("type") != "app_mention":
        raise _Ignored("not_mentioned")
    reader = client or thread_access._slack_client(thread_access.slack_token(env))
    if not _slack_allowed(reader, sender, env):
        raise _Ignored("denied")
    host = reader.workspace()["host"]
    permalink = slack_thread_link(host, conversation, root)
    return sender, event_id, root, permalink, parent, reader


def _matrix(conversation: str, event: dict, env, client):
    if event.get("type") != "m.room.message":
        raise _Ignored("not_message")
    sender = _required(event.get("sender"), "sender")
    event_id = _required(event.get("event_id"), "message id")
    content = event.get("content")
    if not isinstance(content, dict):
        raise ThreadError("matrix message content must be an object", status=400)
    relation = content.get("m.relates_to") or {}
    if not isinstance(relation, dict):
        raise ThreadError("matrix message relation must be an object", status=400)
    if relation.get("rel_type") == "m.replace":
        raise _Ignored("edit")
    account = thread_access.matrix_account(env)
    if sender == account.user_id:
        raise _Ignored("self")
    root = resolve_thread_root(event)
    parent = dm_parents.lookup("matrix", conversation, root) if root != event_id else None
    if not parent and not is_assistant_mention(event, account.user_id):
        raise _Ignored("not_mentioned")
    if not dm_access.permits(sender, env=env):
        raise _Ignored("denied")
    reader = client or thread_access._matrix_client(account)
    if not parent:
        try:
            known_dm = dm_parents.known_conversation("matrix", conversation)
        except (OSError, ValueError) as exc:
            raise ThreadError("could not read the assistant DM registry", status=503) from exc
        if known_dm or reader.is_direct_room(conversation):
            raise _Ignored("unmatched_dm_parent")
    return sender, event_id, root, matrix_to_link(conversation, root), parent, reader
