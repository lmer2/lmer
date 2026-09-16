"""The session's end of the supervisor bus: a loopback HTTP client.

Inside the container the wrapped harness inherits ``LMER_FASTAPI_PORT`` and
``LMER_FASTAPI_TOKEN`` from the supervisor (it exports both before it forks),
so every command the agent runs can reach the bus at
``http://127.0.0.1:<port>/messages`` with that bearer token and nothing else.
No mount, no directory, no credential of the world's — the bus is the only
port, and what the supervisor does with a record is the supervisor's business.

A standalone connector process (a Slack or matrix connector run as a sidecar,
spec §5.4) uses the same client with ``LMER_SUPERVISOR_URL`` naming the
supervisor instead of the loopback default.

"No channel" is one condition with several causes — no port in the
environment, no token, a supervisor that does not answer, or one whose
``/healthz`` carries no ``comms`` (a pre-#407 image) — and every one of them
is :class:`NoChannel`, because the caller's move is the same for all of them:
say what you need in ordinary output and carry on (exit 3).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

import requests

__all__ = [
    "PORT_ENV", "TOKEN_ENV", "URL_ENV", "DEFAULT_HOST", "REQUEST_TIMEOUT",
    "MAX_POLL_SECONDS", "CONNECT_BUDGET_ENV", "DEFAULT_CONNECT_BUDGET",
    "BusError", "NoChannel", "Endpoint", "BusClient", "resolve_endpoint",
    "connect_budget",
]

PORT_ENV = "LMER_FASTAPI_PORT"
TOKEN_ENV = "LMER_FASTAPI_TOKEN"

#: Overrides the loopback default: a connector running beside, not inside.
URL_ENV = "LMER_SUPERVISOR_URL"

DEFAULT_HOST = "127.0.0.1"

#: Seconds a plain request may take. Generous for loopback; the point is that a
#: wedged supervisor becomes an error the agent can read, not a hang.
REQUEST_TIMEOUT = 10.0

#: The supervisor caps a long-poll at 30 s (``GET /messages?timeout=``); a wait
#: longer than that is several polls.
MAX_POLL_SECONDS = 30.0

#: How long a client started *before* its supervisor waits for one to appear.
CONNECT_BUDGET_ENV = "LMER_COMMS_CONNECT_BUDGET"

#: Seconds of that wait by default. A connector is started by the host CLI
#: before the container is launched, and a first ``podman run`` of a new image
#: can spend minutes remapping UIDs before the supervisor binds its port.
DEFAULT_CONNECT_BUDGET = 900.0


class BusError(RuntimeError):
    """The supervisor answered and refused, or the reply was unusable."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class NoChannel(BusError):
    """There is no bus to talk to. See the module docstring.

    ``transient`` separates "nothing is listening yet" from "this supervisor
    has answered and has no bus". Only the first is worth waiting on, and the
    difference is invisible in the message: both read as no channel.
    """

    def __init__(
        self, message: str, status: Optional[int] = None, *, transient: bool = False,
    ) -> None:
        super().__init__(message, status)
        self.transient = transient


@dataclass(frozen=True)
class Endpoint:
    url: str
    token: str = field(repr=False)

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}


def resolve_endpoint(env: Optional[Mapping[str, str]] = None) -> Endpoint:
    """Where the bus is, from the environment. :class:`NoChannel` if it is not."""
    environ = os.environ if env is None else env
    token = (environ.get(TOKEN_ENV) or "").strip()
    url = (environ.get(URL_ENV) or "").strip()
    if not url:
        port = (environ.get(PORT_ENV) or "").strip()
        if not port:
            raise NoChannel(
                f"{PORT_ENV} is not set: this session runs without a supervisor "
                "control plane, so there is no channel to the world — say what "
                "you need in your ordinary output"
            )
        if not port.isdigit():
            raise NoChannel(f"{PORT_ENV}={port!r} is not a port number")
        url = f"http://{DEFAULT_HOST}:{port}"
    if not token:
        raise NoChannel(
            f"{TOKEN_ENV} is not set: the supervisor's control plane needs its "
            "bearer token, which the supervisor exports to the session it wraps"
        )
    return Endpoint(url=url.rstrip("/"), token=token)


class BusClient:
    """Typed calls over the supervisor's ``/messages`` routes.

    *session* is anything with a ``request(method, url, **kw)`` returning an
    object with ``status_code`` and ``json()`` — a :class:`requests.Session`
    in production, a FastAPI ``TestClient`` in tests.
    """

    def __init__(self, endpoint: Endpoint, session=None, *, timeout: float = REQUEST_TIMEOUT) -> None:
        self.endpoint = endpoint
        self._session = session if session is not None else requests.Session()
        self._timeout = timeout

    # -- transport -------------------------------------------------------------

    def _call(self, method: str, path: str, *, json_body=None, params=None, timeout=None,
              bus_routes: bool = True):
        url = f"{self.endpoint.url}{path}"
        try:
            response = self._session.request(
                method, url, json=json_body, params=params,
                headers=self.endpoint.headers,
                timeout=self._timeout if timeout is None else timeout,
            )
        except requests.RequestException as exc:
            raise NoChannel(
                f"the supervisor at {self.endpoint.url} does not answer ({exc.__class__.__name__}) "
                "— the control plane is gone, so there is no channel",
                transient=True,
            ) from exc
        if response.status_code == 401:
            raise NoChannel(
                "the supervisor refused the bearer token; the session image may predate "
                "digest credentials. Roll the session image first, then the platform "
                "and host CLI, and start a new session; also check the control credential"
            )
        # Only the bus reads these two codes as "no channel". Every other route
        # on this plane has its own meaning for them and maps them itself.
        if bus_routes and response.status_code == 503:
            raise NoChannel("this supervisor keeps no message bus (it predates the unified channel)")
        if bus_routes and response.status_code == 404:
            raise NoChannel("this supervisor has no /messages routes (it predates the unified channel)")
        try:
            body = response.json()
        except ValueError:
            raise BusError(f"unreadable reply from the supervisor ({response.status_code})",
                           status=response.status_code)
        if response.status_code >= 400:
            detail = body.get("detail") if isinstance(body, dict) else None
            if isinstance(detail, list):  # pydantic validation shape
                detail = "; ".join(str(item.get("msg", item)) for item in detail)
            raise BusError(str(detail or body), status=response.status_code)
        return body

    # -- verbs -----------------------------------------------------------------

    def health(self) -> dict:
        """``/healthz``'s ``comms`` block. :class:`NoChannel` if absent."""
        body = self._call("GET", "/healthz")
        comms = body.get("comms") if isinstance(body, dict) else None
        if not isinstance(comms, dict):
            raise NoChannel("this supervisor keeps no message bus (it predates the unified channel)")
        return comms

    def wait_ready(
        self,
        *,
        budget: float = DEFAULT_CONNECT_BUDGET,
        stop=None,
        interval: float = 1.0,
    ) -> bool:
        """Block until the supervisor answers with a bus, the budget lapses, or
        ``stop`` (a :class:`threading.Event`) is set. Returns whether it came up.

        A client can legitimately start before its supervisor: the host CLI
        starts a chat connector *before* it launches the container. Until
        something is listening every call raises :class:`NoChannel` — the same
        exception a supervisor that has gone away raises — so a caller that
        cannot tell the two apart gives up on a session that is merely still
        booting. :attr:`NoChannel.transient` is that distinction; a supervisor
        that answers without a bus is a definite no and ends the wait.
        """
        deadline = time.monotonic() + max(0.0, budget)
        while True:
            try:
                self.health()
                return True
            except NoChannel as exc:
                if not exc.transient:
                    return False
            except BusError:
                pass
            if stop is not None and stop.is_set():
                return False
            if time.monotonic() >= deadline:
                return False
            if stop is not None:
                stop.wait(interval)
            else:
                time.sleep(interval)

    def post(
        self,
        kind: str,
        text: str,
        *,
        source: Optional[Mapping] = None,
        in_reply_to: Optional[str] = None,
        options: Iterable[str] = (),
        attachments: Iterable[Mapping] = (),
        meta: Optional[Mapping] = None,
        wait: Optional[float] = None,
    ) -> dict:
        """``POST /messages``. Returns ``{"message", "accepted_as", "relay"}``.

        *wait* asks the supervisor to hold a question's answer for this caller
        for that many seconds, from now rather than from the first poll.
        """
        body = {
            "kind": kind,
            "text": text,
            "source": dict(source or {}),
            "in_reply_to": in_reply_to,
            "options": list(options),
            "attachments": [dict(item) for item in attachments],
            "meta": dict(meta or {}),
        }
        if wait is not None:
            body["wait"] = wait
        return self._call("POST", "/messages", json_body=body)

    def claim(self, question: str, ttl: float) -> dict:
        """Hold this question's answer for this process for *ttl* seconds."""
        return self._call("POST", "/messages/claim", json_body={"question": question, "ttl": ttl})

    def release(self, question: str) -> dict:
        """Give up a claim on a question's answer, so a later one is typed into
        the harness instead of held for a process that is no longer waiting."""
        return self._call("POST", "/messages/release", json_body={"question": question})

    def messages(
        self,
        since: int = 0,
        *,
        timeout: float = 0.0,
        kinds: Iterable[str] = (),
        waiter: Optional[str] = None,
    ) -> tuple[list, int]:
        """``GET /messages``: records past *since* and the new cursor."""
        params: dict = {"since": int(since), "timeout": min(max(0.0, timeout), MAX_POLL_SECONDS)}
        kinds = list(kinds)
        if kinds:
            params["kinds"] = ",".join(kinds)
        if waiter:
            params["waiter"] = waiter
        body = self._call(
            "GET", "/messages", params=params, timeout=params["timeout"] + self._timeout
        )
        return list(body.get("messages") or []), int(body.get("cursor") or since)

    def questions(self, *, open_only: bool = False) -> list:
        body = self._call("GET", "/messages/questions", params={"open": "true" if open_only else "false"})
        return list(body.get("questions") or [])

    def question(self, ref: str) -> Optional[dict]:
        """The fold entry whose question is *ref* (a seq, padded or not, or an id)."""
        wanted = _seq_of(ref)
        for state in self.questions():
            question = state.get("question") or {}
            if question.get("id") == ref or (wanted is not None and question.get("seq") == wanted):
                return state
        return None

    # -- threads this session is not connected to (issue #414) -----------------

    def _thread_call(self, method: str, path: str, **kw):
        """Like :meth:`_call`, with this facility's reading of 503 and 404.

        A 503 here is *no servicer attached* and a 404 is *a supervisor that
        predates the facility* — two different facts from the bus's, pointing at
        different fixes. Both are still :class:`NoChannel`, because the caller's
        move is the same for all three: say what you need in ordinary output and
        carry on (exit 3).
        """
        try:
            return self._call(method, path, bus_routes=False, **kw)
        except BusError as exc:
            if exc.status == 503:
                raise NoChannel(str(exc)) from None
            if exc.status == 404:
                raise NoChannel(
                    "this supervisor has no thread routes (it predates reading and "
                    "posting threads by permalink)"
                ) from None
            raise

    def thread_request(
        self, op: str, permalink: str = "", *, channel: str = "",
        recipient: str = "", limit: Optional[int] = None, text: str = "",
        attachments: Iterable[Mapping] = (),
    ) -> dict:
        """``POST /thread/requests``: queue one piece of thread work for the host."""
        body: dict = {"op": op, "permalink": permalink, "channel": channel,
                      "recipient": recipient, "text": text,
                      "attachments": [dict(item) for item in attachments]}
        if limit is not None:
            body["limit"] = int(limit)
        return self._thread_call("POST", "/thread/requests", json_body=body)

    def thread_result(self, request_id: str, *, timeout: float = 0.0) -> dict:
        """``GET /thread/requests/<id>``, long-polling for the result."""
        poll = min(max(0.0, timeout), MAX_POLL_SECONDS)
        return self._thread_call(
            "GET", f"/thread/requests/{request_id}", params={"timeout": poll},
            timeout=poll + self._timeout,
        )

    def thread_claim(self, since: int = 0, *, timeout: float = 0.0) -> tuple[list, int]:
        """A servicer's poll: the requests it now owns, and its new cursor."""
        poll = min(max(0.0, timeout), MAX_POLL_SECONDS)
        body = self._thread_call(
            "GET", "/thread/requests", params={"since": int(since), "timeout": poll},
            timeout=poll + self._timeout,
        )
        return list(body.get("requests") or []), int(body.get("cursor") or since)

    def thread_complete(self, request_id: str, result: Mapping) -> dict:
        return self._thread_call(
            "POST", f"/thread/requests/{request_id}/result", json_body={"result": dict(result)},
        )

    def ack(self, subscriber: str, seq: int, *, final: bool = False) -> dict:
        """Take delivery up to *seq*. An ack of 0 registers without holding
        anything; *final* says this subscriber is leaving for good."""
        return self._call("POST", "/messages/ack", json_body={
            "subscriber": subscriber, "seq": int(seq), "final": bool(final),
        })


def _seq_of(ref: str) -> Optional[int]:
    text = (ref or "").strip().rpartition("/")[2]
    return int(text) if text.isdigit() else None


def connect_budget(env: Optional[Mapping[str, str]] = None) -> float:
    """Seconds :meth:`BusClient.wait_ready` waits by default, from the
    environment. Unreadable or negative values fall back to the default."""
    raw = (env if env is not None else os.environ).get(CONNECT_BUDGET_ENV)
    if raw is None:
        return DEFAULT_CONNECT_BUDGET
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_CONNECT_BUDGET
    return value if value >= 0 else DEFAULT_CONNECT_BUDGET
