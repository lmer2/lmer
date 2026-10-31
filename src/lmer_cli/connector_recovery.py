"""Inspect or resolve one connector operation under its exclusive ownership lease."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
import uuid

from ask_channel import bus
from slack_chat.client import strip_run_references

from . import thread_access
from .connector_delivery import RecoveryRequired, checkpoint_path, claim_receipt, digest, marker_path, receipt_path
from .connector_ownership import Lease, ThreadKey, read_json, sync_directory, write_json
from .threads import parse_thread


AUTOMATIC_RETRY_LIMIT = 1


def _slack_text(text):
    text = html.unescape(strip_run_references(text))
    return re.sub(r"<(https?://[^>|]+)(?:\|[^>]+)?>", r"\1", text)


class Recovery:
    def __init__(self, lease: Lease):
        self.lease = lease
        self.key = lease.key

    def read(self, operation_id=None):
        self.lease.validate()
        marker = read_json(marker_path(self.key))
        if not marker:
            raise RecoveryRequired("no pending operation; selection may be stale")
        if (marker.get("version") != 1 or marker.get("thread") != self.key.parts
                or not isinstance(marker.get("session"), str) or not marker["session"]):
            raise RecoveryRequired("incompatible pending marker; state preserved")
        if operation_id is not None and marker.get("operation") != operation_id:
            raise RecoveryRequired("wrong or stale pending operation selection")
        state = read_json(checkpoint_path(self.key, marker["session"]))
        if not state or state.get("session") != marker["session"] or state.get("thread") != self.key.parts:
            raise RecoveryRequired("pending supervisor checkpoint is missing or incompatible")
        pending = state.get("pending") or {}
        part = (pending.get("parts") or {}).get(marker.get("part"))
        if (not part or pending.get("source_digest") != marker.get("source_digest")
                or digest(pending.get("source")) != marker.get("source_digest")
                or part.get("identity") != marker.get("identity")
                or digest(part.get("payload")) != marker.get("identity")):
            raise RecoveryRequired("pending source or attachment identity changed")
        if part.get("id") != marker.get("operation") and not (
            part.get("status") in ("retry-authorized", "confirmed")
            and part.get("retry_of") == marker.get("operation")
            and (part.get("authorization") or {}).get("operation") == marker.get("operation")
        ):
            raise RecoveryRequired("pending attempt changed; selection is stale")
        for path in (self.key.directory / "receipts").glob("*.json"):
            owner = read_json(path)
            if owner is None or owner.get("operation") != marker["operation"]:
                continue
            reference = owner.get("receipt")
            if (not isinstance(reference, str) or not reference
                    or owner != {**marker, "receipt": reference}
                    or path != receipt_path(self.key, reference)):
                raise RecoveryRequired("saved operation receipt has incompatible identity; state preserved")
            if part.get("receipt") and part["receipt"] != reference:
                raise RecoveryRequired("conflicting saved operation receipts; state preserved")
            part.update(receipt=reference, status="confirmed")
        return marker, state, part

    def inspect(self):
        marker, state, part = self.read()
        retries = part.get("automatic_retries", 0)
        if type(retries) is not int or retries < 0:
            raise RecoveryRequired("invalid automatic retry count; state preserved")
        authorized = part["status"] == "retry-authorized" and (
            part.get("authorization") or {}
        ).get("known_not_sent") is True
        certain = part["status"] in ("not-sent", "rejected") or authorized
        action = None
        if part.get("receipt"):
            action = "receipt"
        elif certain and (retries < AUTOMATIC_RETRY_LIMIT or authorized):
            action = "retry"
        return {
            "operation": marker["operation"], "session": marker["session"],
            "thread": self.key.parts, "source_seq": state["pending"]["source"].get("seq"),
            "part": marker["part"], "status": part["status"],
            "receipt": part.get("receipt"), "payload": part["payload"],
            "possible_duplicate": not part.get("receipt") and not certain,
            "automatic_action": action, "automatic_retries": retries,
        }

    def _validate_source(self, marker, state, client):
        session = client.health().get("session")
        if not isinstance(session, str) or not session or session != marker["session"]:
            raise RecoveryRequired("current supervisor identity does not match the pending operation")
        source = state["pending"]["source"]
        seq = source.get("seq")
        if seq is None and (source.get("notice") or source.get("attachment")):
            return
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            raise RecoveryRequired("pending source has no valid supervisor sequence")
        records, _ = client.messages(seq - 1, timeout=0)
        record = next((item for item in records if item.get("seq") == seq), None)
        if record is None or digest(record) != marker["source_digest"]:
            raise RecoveryRequired("original source record is missing or changed; context preserved")

    def _complete(self, marker, state):
        self.lease.validate()
        state["generation"] += 1
        write_json(checkpoint_path(self.key, marker["session"]), state)
        latest = read_json(marker_path(self.key))
        if latest != marker:
            raise RecoveryRequired("pending operation changed before completion")
        marker_path(self.key).unlink()
        sync_directory(self.key.directory)

    def retry(self, operation_id, client, *, inspected=False, accept_duplicate=False, automatic=False):
        if automatic and self.inspect()["automatic_action"] != "retry":
            raise RecoveryRequired("inspection does not permit an automatic retry")
        marker, state, part = self.read(operation_id)
        self._validate_source(marker, state, client)
        if part.get("receipt") or part.get("status") == "confirmed":
            raise RecoveryRequired("confirmed sends cannot be retried; resolve the saved or verified receipt")
        if part.get("status") != "retry-authorized":
            certain = part.get("status") in ("not-sent", "rejected")
            if not certain and not (inspected and accept_duplicate):
                raise RecoveryRequired(
                    "Retry may create a duplicate. Inspect the remote thread, then select this operation "
                    "with --inspected-thread --accept-possible-duplicate."
                )
            part.update(
                id=uuid.uuid4().hex, status="retry-authorized", retry_of=operation_id,
                authorization={"operation": operation_id, "at": time.time(),
                               "inspected": inspected, "possible_duplicate_accepted": accept_duplicate,
                               "known_not_sent": certain},
            )
            if automatic:
                part["automatic_retries"] = part.get("automatic_retries", 0) + 1
        self._complete(marker, state)
        return "one retry authorized; the connector will use its normal durable send protocol"

    def clear(self, operation_id, *, acknowledge_duplicate=False):
        """Explicitly remove one marker without changing its checkpoint or cursors."""
        if acknowledge_duplicate is not True:
            raise RecoveryRequired("clear requires acknowledgement that the send may be duplicated")
        marker, _, _ = self.read(operation_id)
        self.lease.validate()
        if read_json(marker_path(self.key)) != marker:
            raise RecoveryRequired("pending operation changed before clear")
        marker_path(self.key).unlink()
        sync_directory(self.key.directory)
        return "pending marker cleared; the connector may duplicate the selected send"

    def receipt(self, operation_id, client, *, remote_ref=None, remote=None, sender=None):
        marker, state, part = self.read(operation_id)
        self._validate_source(marker, state, client)
        saved = part.get("receipt")
        if remote_ref:
            owner = read_json(receipt_path(self.key, remote_ref))
            if owner is not None and owner != {**marker, "receipt": remote_ref}:
                raise RecoveryRequired("that receipt already belongs to another delivery operation")
        if saved:
            if remote_ref is not None and remote_ref != saved:
                raise RecoveryRequired("selected reference disagrees with the saved confirmed receipt")
            remote_ref = saved
        else:
            if not remote_ref or remote is None:
                raise RecoveryRequired("supply a verified remote reference, or explicitly retry an uncertain operation")
            previous = (state.get("transport", {}).get("posters") or {}).get("wrote", [])
            if remote_ref in previous:
                raise RecoveryRequired("that receipt already belongs to a completed delivery")
            for name, other in state["pending"]["parts"].items():
                if name != marker["part"] and other.get("receipt") == remote_ref:
                    raise RecoveryRequired("that receipt already belongs to another attachment or text operation")
            self._verify_remote(remote_ref, part["payload"], remote, sender)
        claim_receipt(self.key, marker, remote_ref)
        # A retry still behind the original marker has never been sent.
        part.update(id=marker["operation"], receipt=remote_ref, status="confirmed")
        self._complete(marker, state)
        return "receipt saved; normal bookkeeping and remaining attachments may resume"

    def _verify_remote(self, reference, payload, remote, sender):
        if self.key.transport == "slack":
            messages = remote.get_replies(self.key.conversation, self.key.root)
            message = next((item for item in messages if item.get("ts") == reference), None)
            if (not message or reference == self.key.root or message.get("user") != remote.auth_test()
                    or message.get("thread_ts", self.key.root) != self.key.root):
                raise RecoveryRequired("Slack reference does not match this operation's thread, sender and content")
            if "file_id" in payload:
                if not any(isinstance(item, dict) and item.get("id") == payload["file_id"]
                           for item in message.get("files") or []):
                    raise RecoveryRequired("Slack reference does not share this operation's file")
            elif _slack_text(message.get("text", "")) != _slack_text(payload.get("text", "")):
                raise RecoveryRequired("Slack reference does not match this operation's thread, sender and content")
            return
        event = remote.get_event(self.key.conversation, reference)
        if event.get("type") == "m.room.encrypted":
            ((event, problem),) = remote.decrypt_events(self.key.conversation, [event])
            if event is None:
                raise RecoveryRequired(f"Matrix reference is encrypted and lmer-matrix-bridge cannot read it ({problem})")
        content = event.get("content") or {}
        relation = content.get("m.relates_to") or {}
        if (event.get("event_id") != reference or event.get("sender") != sender
                or relation.get("rel_type") != "m.thread" or relation.get("event_id") != self.key.root):
            raise RecoveryRequired("Matrix reference does not match this operation's thread and sender")
        if "text" in payload:
            if content.get("body") != payload["text"]:
                raise RecoveryRequired("Matrix text does not match the pending operation")
            return
        if content.get("filename", content.get("body")) != payload["name"]:
            raise RecoveryRequired("Matrix attachment name does not match the selected index")
        if isinstance(content.get("file"), dict):
            data, _ = remote.download_content(content, max_bytes=payload["size"])
        else:
            data, _ = remote.download(content.get("url"), max_bytes=payload["size"])
        if data is None or len(data) != payload["size"] or hashlib.sha256(data).hexdigest() != payload["sha256"]:
            raise RecoveryRequired("Matrix attachment bytes do not match the selected index")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Inspect or safely resolve one pending connector operation.")
    parser.add_argument("--permalink", required=True, help="Canonical thread-root permalink.")
    parser.add_argument("action", choices=("inspect", "receipt", "retry"))
    parser.add_argument("--operation", help="Exact pending operation id from inspect.")
    parser.add_argument("--remote-ref", help="Operator-selected Slack timestamp or Matrix event id.")
    parser.add_argument("--inspected-thread", action="store_true")
    parser.add_argument("--accept-possible-duplicate", action="store_true")
    args = parser.parse_args(argv)
    lease = None
    try:
        ref = parse_thread(args.permalink)
        conversation, root = ref.registry_key()
        remote = None
        sender = None
        if ref.channel == "matrix" and conversation.startswith("#"):
            remote = thread_access._matrix_client(thread_access.matrix_account())
            conversation = remote.resolve_room(conversation)
        key = ThreadKey(ref.channel, conversation, root)
        lease = Lease.acquire(key, owner="recovery")
        recovery = Recovery(lease)
        if args.action == "inspect":
            print(json.dumps(recovery.inspect(), indent=2))
            return 0
        if not args.operation:
            raise RecoveryRequired("select exactly one --operation from inspect")
        client = bus.BusClient(bus.resolve_endpoint())
        if args.action == "retry":
            print("Warning: retrying an uncertain send may create a duplicate.", file=sys.stderr)
            result = recovery.retry(args.operation, client, inspected=args.inspected_thread,
                                    accept_duplicate=args.accept_possible_duplicate)
        else:
            if args.remote_ref:
                if key.transport == "slack":
                    remote = thread_access._slack_client(thread_access.slack_token())
                else:
                    account = thread_access.matrix_account()
                    remote = remote or thread_access._matrix_client(account)
                    sender = account.user_id
            result = recovery.receipt(args.operation, client, remote_ref=args.remote_ref,
                                      remote=remote, sender=sender)
        print(result)
        return 0
    except Exception as exc:
        print(f"connector recovery: {exc}", file=sys.stderr)
        return 2
    finally:
        if lease is not None:
            lease.close()


if __name__ == "__main__":
    sys.exit(main())
