"""Delayed acceptance reactions; independent of the harness's turn state."""
from __future__ import annotations

import hashlib
import logging
import time

from .threads import ThreadError, parse_thread

logger = logging.getLogger(__name__)
GRACE_SECONDS = 20.0
POLL_SECONDS = 0.5


def is_reply(record: dict, thread) -> bool:
    meta = record.get("meta") or {}
    if not (
        ((record.get("source") or {}).get("channel") in (None, "agent")
         or meta.get("session_origin") is True)
        and record.get("kind") in ("message", "question", "milestone")
    ):
        return False
    if not meta.get("thread"):
        return True
    try:
        destination = parse_thread(meta["thread"])
    except ThreadError:
        return False
    return (destination.channel == thread.transport
            and destination.registry_key() == (thread.conversation, thread.root))


class ReceiptReactions:
    def __init__(self, connector):
        self.connector = connector
        self.clock = time.time

    def accepted(self, ref, message):
        self.connector._receipt_pending.setdefault(message["id"], {
            "ref": ref, "seq": message["seq"], "due": self.clock() + GRACE_SECONDS,
        })

    def step(self):
        connector = self.connector
        with connector._state.lock:
            pending = dict(connector._receipt_pending)
        if not pending:
            return
        for ident, item in pending.items():
            if connector._stop.is_set():
                return
            records, _ = connector.bus.messages(item["seq"], timeout=0)
            reply = max((record["seq"] for record in records
                         if is_reply(record, connector._lease.key)), default=0)
            if reply <= item["seq"] and self.clock() < item["due"]:
                continue
            if reply <= item["seq"]:
                transaction = "lmer-receipt-" + hashlib.sha256(ident.encode()).hexdigest()
                try:
                    connector._send_receipt(item["ref"], transaction)
                except Exception as exc:
                    logger.warning("connector_receipt_failed ref=%s error=%s", item["ref"], exc)
            with connector._state.lock:
                connector._receipt_pending.pop(ident, None)
                connector._state.save()

    def run(self):
        connector = self.connector
        while not connector._stop.wait(POLL_SECONDS):
            try:
                self.step()
            except Exception:
                logger.exception("connector_receipt_check_failed")
