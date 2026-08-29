"""A reply in a thread → the right verb on the platform (spec D4, D6).

The path is short and every step of it is a refusal waiting to happen, so each
one is written out:

1. The message must be **in a thread the bridge opened**. A message in the room
   with no thread relation, or in a thread this bridge does not know, is logged
   and ignored — never matched to a run by guesswork.
2. The run must be **waiting on a question right now**, and which question it is
   waiting on decides the verb: ``live_question`` answers a session that is
   running and blocked on its ask channel; ``question`` answers a run that
   *exited*, which **starts a container**. The two are never merged, because
   merging them would put an operator one tap from starting a container when
   they meant to reply to a waiting session.
3. The sender must be **allowlisted for the capability that verb needs**
   (:func:`matrix_bridge.allow.permits`), where ``answer-stopped`` is its own
   capability precisely because it spawns.
4. The platform is called with ``X-Lmer-Principal`` naming the Matrix id, and
   the **bridge** logs it beside the call. Being exact about this, because the
   comments here used to claim more (!244 review): the daemon does not read or
   record the header today — it has no per-principal anything, which is why D4
   puts authorization in the bridge at all. The header ships now because a
   request that arrives without one cannot be attributed retroactively, and
   because the per-principal work (#202, slice 2) reads it rather than inventing
   it. Until then, the bridge's own log line is the attribution.
5. The room is told what happened — in the thread, in the words of the thing
   that actually happened: the session continued, a session started, the
   concurrency cap refused it, or the platform's own ``detail``.

A denial produces nothing in the room. A refusal posted into a federated room
tells a stranger that they found something; the log is where denials go.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from lmer_platform.client import Call, Endpoint, PlatformError, request
from matrix_bridge import allow as mxallow
from matrix_bridge.config import MatrixConfig
from matrix_bridge.threads import RunKey, ThreadMap

logger = logging.getLogger(__name__)

#: The header that names the Matrix principal on every platform call. The
#: platform neither authorizes on it nor records it today; it is sent so that
#: the attribution exists at the moment of the request, and read by the
#: per-principal work when that lands. The attribution that ships *now* is the
#: bridge's own ``matrix_inbound_answered`` log line.
PRINCIPAL_HEADER = "X-Lmer-Principal"

#: Answering is not instant on either route: the live one writes into a channel
#: a container is polling, the stopped one starts a container.
ANSWER_TIMEOUT_SECONDS = 120.0

#: Which capability each attention reason's answer needs, and which route it
#: takes. The mapping *is* D6: routing on the reason rather than on anything
#: about the message is what keeps the two verbs apart.
ROUTES = {
    "live_question": "answer-live",
    "question": "answer-stopped",
}


@dataclass(frozen=True)
class Message:
    """One inbound room message, reduced to what the decision needs."""

    sender: str
    body: str
    thread_root: Optional[str]

    @classmethod
    def from_event(cls, event: Mapping) -> Optional["Message"]:
        """Parse an ``m.room.message`` event, or ``None`` if it is not one.

        Reads the raw event mapping rather than a library object so the routing
        below is testable without a homeserver, and so a change of Matrix client
        cannot change what "a threaded reply" means.
        """
        if event.get("type") not in (None, "m.room.message"):
            return None
        content = event.get("content") or {}
        if content.get("msgtype") not in (None, "m.text", "m.notice"):
            return None
        body = content.get("body")
        sender = event.get("sender")
        if not isinstance(body, str) or not isinstance(sender, str):
            return None

        relation = content.get("m.relates_to") or {}
        root = None
        if relation.get("rel_type") == "m.thread":
            root = relation.get("event_id")
        # The fallback is stripped only when the event says one is there.
        # Deciding from the body's shape ate the first line of any answer that
        # legitimately began with a quote, and the person got no sign of it
        # (!244/!245 review) — the failure the strip was meant to prevent,
        # pointed the other way.
        return cls(
            sender=sender,
            body=_without_fallback(body) if _has_reply_fallback(content) else body.strip(),
            thread_root=root,
        )


def _has_reply_fallback(content: Mapping) -> bool:
    """Does this event actually carry a rich-reply fallback?

    Read off the event, never guessed from the text (!244/!245 review). A
    fallback exists only when the content carries an ``m.in_reply_to`` relation
    — and a thread reply that sets ``is_falling_back: true`` is the spec's way
    of saying the ``m.in_reply_to`` beside it is a *thread* fallback aimed at
    clients that cannot render threads, which is not quoted text at all.

    ``formatted_body``'s ``<mx-reply>`` block is the other half of the same
    signal, and is accepted here for clients that send it without the relation.
    """
    relation = content.get("m.relates_to") or {}
    if relation.get("is_falling_back") is True:
        return False
    if isinstance(relation.get("m.in_reply_to"), Mapping):
        return True
    formatted = content.get("formatted_body")
    return isinstance(formatted, str) and formatted.lstrip().startswith("<mx-reply>")


def _without_fallback(body: str) -> str:
    """Strip a rich reply's quoted fallback from *body*.

    Called only when :func:`_has_reply_fallback` says there is one. A client
    replying to one message prepends the quoted original as
    ``> <@someone:server> …`` lines followed by a blank line, for clients that
    cannot render the relation; sending that to the platform would answer a
    question with a copy of the question plus the answer.
    """
    lines = body.splitlines()
    index = 0
    while index < len(lines) and lines[index].startswith(">"):
        index += 1
    if index == 0:
        return body.strip()
    while index < len(lines) and not lines[index].strip():
        index += 1
    return "\n".join(lines[index:]).strip()


@dataclass(frozen=True)
class Outcome:
    """What happened to one inbound message, for the tests and the log."""

    handled: bool
    reason: str
    acknowledgement: Optional[str] = None


class Inbound:
    """Threaded replies in, platform calls out."""

    def __init__(
        self,
        config: MatrixConfig,
        client,
        threads: ThreadMap,
        endpoint: Endpoint,
        *,
        transport=None,
    ):
        self.config = config
        self.client = client
        self.threads = threads
        self.endpoint = endpoint
        self.transport = transport

    async def handle(self, event: Mapping) -> Outcome:
        """One event, all five steps. Never raises for an ordinary refusal."""
        message = Message.from_event(event)
        if message is None:
            return self._ignored("not a text message", event.get("sender"))
        if message.sender == self.config.sender:
            # The bridge's own messages come back on the same transactions.
            return self._ignored("the bridge's own message", message.sender)
        if not message.thread_root:
            return self._ignored("not in a thread", message.sender)
        if not message.body:
            return self._ignored("empty message", message.sender)

        run = self.threads.run_for(message.thread_root)
        if run is None:
            return self._ignored("thread belongs to no run", message.sender)

        row = await self._row_for(run)
        if row is None:
            return self._ignored(f"no row for {run}", message.sender)

        reason = ((row.get("attention") or {}).get("reason")) or ""
        capability = ROUTES.get(reason)
        if capability is None:
            return self._ignored(
                f"{run} is not waiting on a question (attention: "
                f"{reason or 'none'})", message.sender,
            )

        if not mxallow.permits(self.config.allow, message.sender, capability):
            # Logged, not answered. The room says nothing.
            logger.warning(
                "matrix_inbound_denied sender=%s capability=%s run=%s",
                message.sender, capability, run,
            )
            return Outcome(False, f"denied: {capability}")

        call = self._call_for(reason, run, row, message.body)
        if call is None:
            return self._ignored(
                f"{run} says {reason} but the row carries no question to answer",
                message.sender,
            )

        acknowledgement = await self._answer(call, message.sender, reason)
        await self.client.send_in_thread(message.thread_root, acknowledgement)
        logger.info(
            "matrix_inbound_answered sender=%s run=%s reason=%s",
            message.sender, run, reason,
        )
        return Outcome(True, reason, acknowledgement)

    # --- the two routes -------------------------------------------------------

    def _call_for(
        self, reason: str, run: RunKey, row: Mapping, text: str,
    ) -> Optional[Call]:
        if reason == "live_question":
            session_id = (row.get("session") or {}).get("id")
            questions = row.get("questions") or []
            question_id = questions[0].get("id") if questions else None
            if not session_id or not question_id:
                return None
            # The oldest unanswered question, because that is the one the
            # session is blocked in — the same one the attention note renders.
            return Call(
                "POST",
                f"/api/sessions/{session_id}/ask/{question_id}/answer",
                body={"answer": text},
            )
        return Call(
            "POST", "/api/runs/answer",
            body={
                "host": run.host, "project": run.project, "slug": run.slug,
                "answer": text,
            },
        )

    async def _answer(self, call: Call, principal: str, reason: str) -> str:
        """Make the call and turn its reply into one line for the thread.

        In a thread, because ``requests`` blocks and this coroutine shares its
        loop with the appservice's HTTP server: answering a stopped run starts a
        container, which is not quick, and the loop must keep taking the
        homeserver's transactions while it happens (!244 review).
        """
        try:
            response = await asyncio.to_thread(
                request,
                self.endpoint, call, timeout=ANSWER_TIMEOUT_SECONDS,
                transport=self.transport,
                headers={PRINCIPAL_HEADER: principal},
            )
        except PlatformError as exc:
            logger.warning("matrix_inbound_unreachable detail=%s", exc)
            return "could not reach the platform; nothing was answered"

        status = response.status_code
        if 200 <= status < 300:
            return (
                "answered; session continuing" if reason == "live_question"
                else "answered; session starting"
            )
        if status == 429:
            # Information, never a retry: the cap is the operator's to raise.
            return "at the concurrency cap; nothing started"
        return _detail(response) or f"the platform refused it ({status})"

    # --- looking the run up ---------------------------------------------------

    async def _row_for(self, run: RunKey) -> Optional[Mapping]:
        """This run's row, read fresh.

        Fresh rather than from the poll loop's last snapshot: a reply arrives
        whenever a person types it, and up to ``poll_seconds`` of staleness is
        exactly enough to answer a question that has already been answered — or
        to route to the wrong verb across the moment a live session exits.
        """
        try:
            response = await asyncio.to_thread(
                request,
                self.endpoint, Call("GET", "/api/state"),
                timeout=ANSWER_TIMEOUT_SECONDS, transport=self.transport,
            )
        except PlatformError as exc:
            logger.warning("matrix_inbound_state_unreachable detail=%s", exc)
            return None
        if response.status_code != 200:
            logger.warning(
                "matrix_inbound_state_status status=%s", response.status_code,
            )
            return None
        for row in response.json().get("runs") or []:
            try:
                if RunKey.from_row(row) == run:
                    return row
            except (KeyError, TypeError):
                continue
        return None

    def _ignored(self, why: str, sender: Optional[Any]) -> Outcome:
        logger.info("matrix_inbound_ignored sender=%s why=%s", sender, why)
        return Outcome(False, why)


def _detail(response) -> Optional[str]:
    """The platform's own words for a refusal, if it gave any.

    Relayed rather than reworded: the person asked, and the daemon's sentence is
    the answer. Truncated only by the client seam that sends it.
    """
    try:
        payload = response.json()
    except Exception:
        return None
    detail = payload.get("detail") if isinstance(payload, Mapping) else None
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    return None
