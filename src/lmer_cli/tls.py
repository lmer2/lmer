"""Making TLS verification work on hosts where the interpreter's CA path is wrong.

One narrow problem, hit from more than one direction, which is why it lives here
rather than beside its first caller.

Some Python builds — notably the standalone CPython that ``uv tool install``
fetches — are compiled with a default ``SSL_CERT_FILE`` location that does not
exist on every host. On such a system ``ssl.create_default_context()`` finds no CA
bundle at all, and *every* TLS handshake fails with::

    [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get
    local issuer certificate

The failure is confusing because it looks like a certificate problem at the remote
end. It is not: the remote is fine and the local trust store is simply absent.
It has already bitten the Slack socket-mode connection (aiohttp + slack_sdk) and
the pinned-Node download in :mod:`lmer_platform.ui_build`, which are unrelated
code paths sharing one cause.

``certifi`` ships a bundle and is a hard dependency of this project, so pointing
OpenSSL's default verify path at it fixes all callers at once. The alternative —
handing an explicit ``ssl_context`` to each call site — has to be remembered
separately every time somebody opens a socket, which is exactly how the second
occurrence happened.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

__all__ = ["ensure_ca_bundle"]


def ensure_ca_bundle() -> None:
    """Point OpenSSL's default verify path at certifi's bundle, if it needs it.

    Idempotent and safe to call from anywhere, but it must run **before** the
    first ``ssl`` context is created: OpenSSL reads ``SSL_CERT_FILE`` when
    ``load_default_certs()`` consults its default verify paths, so setting it
    afterwards changes nothing for a context that already exists.

    An existing ``SSL_CERT_FILE`` is left alone. That is the deliberate case, not
    an oversight: on a host behind a TLS-inspecting proxy the operator's corporate
    CA is the only bundle that can work, and overwriting it with certifi's would
    break every connection this is meant to fix.
    """
    if os.environ.get("SSL_CERT_FILE"):
        return
    try:
        import certifi
    except ImportError:
        # A declared dependency, so this should not happen — but guessing a path
        # would be worse than leaving the interpreter's own default in place and
        # letting the original error surface.
        logger.debug("certifi_not_available, leaving SSL_CERT_FILE unset")
        return
    os.environ["SSL_CERT_FILE"] = certifi.where()
