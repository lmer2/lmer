"""The platform's HTTP client — one request, no opinions, injectable transport.

Why this module exists
----------------------
Every host-side client of a platform daemon needs the same three things: where
the daemon is, what opens it, and one function that issues a request without
acquiring a retry, a default or an interpretation of the reply. That code was
written once inside :mod:`lmer_platform.ctl` and lived there as private names,
which was correct while ``lmer-ctl`` was the only client. It no longer is —
``lmer-matrix-bridge`` speaks to the same daemon from the same host — and a
second client importing ``ctl._request`` would be a public dependency on a
private name, plus the whole argparse surface pulled in to make one call.

So the pieces move here and become public. ``ctl`` keeps what is genuinely its
own: the command table, the reply printing, and :func:`ctl.resolve_endpoint`,
which reads the environment pair the *host writes into a container* — a
host-side client has the daemon's secret file instead and builds its
:class:`Endpoint` from that.

The rule the old home was built to keep applies here unchanged: **the daemon is
the only enforcer, and this file grows no logic of its own.** No retries, no
client-side validation, no interpretation of a refusal.

The credential
--------------
It travels in the ``Authorization`` header and nowhere else — never in a URL,
never in a query string. ``requests`` echoes the URL it was given into its
exception messages, and those messages reach logs, transcripts and a browser;
a credential formatted into one is disclosed by the failure path of every call.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import requests


class PlatformError(RuntimeError):
    """Something a client can say without asking the daemon.

    Missing configuration, a transport failure, or an argument that cannot be
    turned into a request at all. Never a refusal — those belong to the daemon
    and are passed through with its status.
    """

    #: The ``error`` discriminator this failure prints. Distinguishing "the
    #: platform was never asked" from "there was nothing to ask it with" is worth
    #: a field: the first is a fleet the operator should hear about, the second is
    #: this client's own launch.
    error = "configuration"


class TransportError(PlatformError):
    """The platform was asked and did not answer."""

    error = "unreachable"


@dataclass(frozen=True)
class Endpoint:
    """Where the platform is and what opens it."""

    base_url: str
    credential: str

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.credential}"}


@dataclass(frozen=True)
class Call:
    """One request: the route, and what this invocation puts in it.

    Keeping the HTTP in a single place downstream is what stops a caller from
    acquiring a retry, a second request, or an opinion about the reply.
    """

    method: str
    path: str
    params: Optional[dict] = None
    body: Optional[dict] = None


def request(
    endpoint: Endpoint, call: Call, *, timeout: float, transport=None,
    headers: Optional[dict] = None,
) -> Any:
    """Issue *call*. Returns the response object; raises on a transport failure.

    *transport* is the seam the tests drive the real FastAPI app through, and the
    only reason this parameter exists. It is anything with ``requests``' own
    ``request(method, url, …)`` signature — the module itself by default.

    *headers* is for a client that carries something about *who asked* —
    ``lmer-matrix-bridge`` sends ``X-Lmer-Principal`` with the Matrix id whose
    message caused the call, because one platform credential is shared by every
    principal it bridges and the daemon's log would otherwise record only the
    bridge. The endpoint's own headers are merged **last**, so nothing passed
    here can replace the ``Authorization`` header with one of its own.

    The credential is a header and the query string is built by the transport
    from ``params``, both for :func:`lmer_platform.session_io._call`'s reason:
    ``requests`` echoes the URL it was given into its exception messages, so
    nothing secret may be formatted into one.
    """
    caller = transport if transport is not None else requests
    try:
        return caller.request(
            call.method,
            endpoint.url(call.path),
            params=call.params,
            json=call.body,
            headers={**(headers or {}), **endpoint.headers()},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        # Only failures that look like ``requests``' own are caught — including
        # an injected transport's, which is how the tests reach this path. Any
        # other exception from an in-process fake is a bug in the caller and
        # should look like one rather than arriving as a fleet problem.
        raise TransportError(
            f"cannot reach the platform at {endpoint.base_url} ({exc})"
        ) from exc
