"""The bridge's device, lent to lmer's other matrix processes (issue #478).

mautrix keeps Olm state in process memory, so exactly one process may own the
device; the thread connector and the host's thread verbs ask this one. Bearer is
the ``as_token``. Why, and what that grants: docs/MATRIX-CHAT.md, "Encrypted
threads". Policy lives here; the crypto is
:class:`~matrix_bridge.client.MautrixHomeserver`'s.
"""
from __future__ import annotations

import hmac
import json
import logging
from typing import Any

from aiohttp import web

from matrix_bridge.client import CryptoUnavailable, SessionMissing, Undecryptable

logger = logging.getLogger(__name__)

__all__ = [
    "PREFIX", "FILE_HEADER", "NO_SESSION", "UNDECRYPTABLE", "MAX_EVENTS",
    "CryptoUnavailable", "SessionMissing", "Undecryptable", "register",
]

PREFIX = "/_lmer/crypto/v1"

#: Carries an attachment's key material beside its raw bytes, so a file never
#: has to be base64-inflated into a JSON body.
FILE_HEADER = "X-Lmer-Encrypted-File"

#: Per-event decrypt outcomes. ``no_session`` is worth retrying: the room key
#: usually arrives on a to-device message shortly after the event.
NO_SESSION = "no_session"
UNDECRYPTABLE = "undecryptable"

MAX_EVENTS = 100
MAX_JSON_BYTES = 8 * 1024 * 1024
#: Above lmer's 25 MiB file cap plus AES-CTR's zero overhead, with room to spare.
MAX_FILE_BYTES = 32 * 1024 * 1024


def register(app: web.Application, homeserver: Any, *, token: str) -> None:
    """Add the crypto routes to *app*, answering only *token*'s bearer."""
    if not token:
        raise ValueError("the crypto API needs the appservice token to authenticate callers")
    api = _Api(homeserver, token)
    app.router.add_route("GET", f"{PREFIX}/whoami", api.whoami)
    app.router.add_route("POST", f"{PREFIX}/encrypt", api.encrypt)
    app.router.add_route("POST", f"{PREFIX}/decrypt", api.decrypt)
    app.router.add_route("POST", f"{PREFIX}/attachment/encrypt", api.encrypt_attachment)
    app.router.add_route("POST", f"{PREFIX}/attachment/decrypt", api.decrypt_attachment)


def _error(status: int, errcode: str, message: str) -> web.Response:
    return web.json_response({"errcode": errcode, "error": message}, status=status)


class _Api:
    def __init__(self, homeserver: Any, token: str) -> None:
        self.homeserver = homeserver
        self._expected = f"Bearer {token}".encode("utf-8")

    def _authorised(self, request: web.Request) -> bool:
        # aiohttp decodes header bytes with surrogateescape; reverse it exactly.
        offered = request.headers.get("Authorization", "").encode("utf-8", "surrogateescape")
        return hmac.compare_digest(offered, self._expected)

    async def _guarded(self, request: web.Request, handler) -> web.StreamResponse:
        if not self._authorised(request):
            logger.warning("matrix_crypto_api_unauthorised path=%s", request.path)
            return _error(401, "M_UNKNOWN_TOKEN", "this API answers the appservice token only")
        try:
            return await handler(request)
        except CryptoUnavailable as exc:
            return _error(503, "LMER_CRYPTO_UNAVAILABLE", str(exc))
        except _BadRequest as exc:
            return _error(exc.status, "M_BAD_JSON" if exc.status == 400 else "M_TOO_LARGE", str(exc))

    # -- routes ------------------------------------------------------------------

    async def whoami(self, request):
        async def handle(_request):
            return web.json_response(await self.homeserver.crypto_identity())
        return await self._guarded(request, handle)

    async def encrypt(self, request):
        async def handle(request):
            body = await _json(request)
            room_id, event_type, content = body.get("room_id"), body.get("type"), body.get("content")
            if not (isinstance(room_id, str) and isinstance(event_type, str) and isinstance(content, dict)):
                raise _BadRequest(400, "encrypt needs room_id, type and a content object")
            try:
                encrypted = await self.homeserver.encrypt_content(room_id, event_type, content)
            except CryptoUnavailable:
                raise
            except Exception as exc:
                # Never a plaintext fallback: the caller refuses the send.
                logger.warning("matrix_crypto_api_encrypt_failed room=%s error=%s", room_id, exc)
                return _error(409, "LMER_ENCRYPT_FAILED", f"cannot encrypt for {room_id}: {exc}")
            return web.json_response({"content": encrypted})
        return await self._guarded(request, handle)

    async def decrypt(self, request):
        async def handle(request):
            events = (await _json(request)).get("events")
            if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
                raise _BadRequest(400, "decrypt needs an events list")
            if len(events) > MAX_EVENTS:
                raise _BadRequest(413, f"decrypt takes at most {MAX_EVENTS} events per call")
            results = []
            for event in events:
                try:
                    results.append({"event": await self.homeserver.decrypt_event(event)})
                except SessionMissing:
                    results.append({"errcode": NO_SESSION})
                except Undecryptable as exc:
                    results.append({"errcode": UNDECRYPTABLE, "error": str(exc)})
                except CryptoUnavailable:
                    raise
                except Exception as exc:
                    # One event must not fail its batch: the caller would retry
                    # the same batch forever.
                    logger.warning("matrix_crypto_api_decrypt_failed error=%s", exc.__class__.__name__)
                    results.append({"errcode": UNDECRYPTABLE, "error": "the bridge could not decrypt it"})
            return web.json_response({"results": results})
        return await self._guarded(request, handle)

    async def encrypt_attachment(self, request):
        async def handle(request):
            data = await _bytes(request, MAX_FILE_BYTES)
            ciphertext, file = await self.homeserver.encrypt_attachment(data)
            return web.Response(
                body=ciphertext, content_type="application/octet-stream",
                headers={FILE_HEADER: json.dumps(file, separators=(",", ":"))},
            )
        return await self._guarded(request, handle)

    async def decrypt_attachment(self, request):
        async def handle(request):
            try:
                file = json.loads(request.headers.get(FILE_HEADER) or "")
            except ValueError:
                file = None
            if not isinstance(file, dict):
                raise _BadRequest(400, f"attachment/decrypt needs the file's keys in {FILE_HEADER}")
            data = await _bytes(request, MAX_FILE_BYTES)
            try:
                plaintext = await self.homeserver.decrypt_attachment(data, file)
            except Undecryptable as exc:
                return _error(422, UNDECRYPTABLE, str(exc))
            return web.Response(body=plaintext, content_type="application/octet-stream")
        return await self._guarded(request, handle)


class _BadRequest(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


async def _bytes(request: web.Request, limit: int) -> bytes:
    """The body, read from the stream so aiohttp's 1 MiB ``client_max_size``
    — which also guards the homeserver's own transaction route — stays as is."""
    body = bytearray()
    while True:
        chunk = await request.content.read(65536)
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > limit:
            raise _BadRequest(413, f"the body is over {limit} bytes")


async def _json(request: web.Request) -> dict:
    try:
        body = json.loads(await _bytes(request, MAX_JSON_BYTES))
    except ValueError:
        raise _BadRequest(400, "the body is not JSON") from None
    if not isinstance(body, dict):
        raise _BadRequest(400, "the body is not a JSON object")
    return body
