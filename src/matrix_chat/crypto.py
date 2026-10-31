"""The bridge's device, from the processes that borrow it (issue #478).

``lmer-matrix-bridge`` owns the one device its appservice user has, and serves
encrypt/decrypt to the thread connector and the host's thread reads and posts
(:mod:`matrix_bridge.crypto_api` says why there is only one owner). This side
needs no ``matrix`` extra: it is HTTP to the bridge.
"""
from __future__ import annotations

import json
from typing import Optional

import requests

__all__ = [
    "BRIDGE_URL_ENV", "BridgeCrypto", "CryptoError", "NO_SESSION", "UNDECRYPTABLE",
    "encrypted_room_refusal",
]

BRIDGE_URL_ENV = "LMER_MATRIX_BRIDGE_URL"

# Mirrors of matrix_bridge.crypto_api's wire names; that module imports aiohttp
# and the bridge, which this side has no other reason to load.
_PREFIX = "/_lmer/crypto/v1"
_FILE_HEADER = "X-Lmer-Encrypted-File"
_MAX_EVENTS = 100
NO_SESSION = "no_session"
UNDECRYPTABLE = "undecryptable"

_TIMEOUT = 30.0


def encrypted_room_refusal(room_id: str) -> str:
    """The one phrasing for an encrypted room nothing here can work in."""
    return (
        f"room {room_id} is end-to-end encrypted, and lmer reads and writes "
        "plaintext events only — use an unencrypted room, or set "
        f"{BRIDGE_URL_ENV} so lmer-matrix-bridge's device does the crypto "
        '(docs/MATRIX-CHAT.md, "Encrypted threads")'
    )


class CryptoError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None, *, transient: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.transient = transient


class BridgeCrypto:
    """Encrypt and decrypt through the bridge, authenticated as its appservice user."""

    def __init__(self, url: str, access_token: str, session=None, *, timeout: float = _TIMEOUT) -> None:
        self.url = url.rstrip("/")
        self._token = access_token
        self._session = session if session is not None else requests.Session()
        self._timeout = timeout

    def _call(self, method: str, path: str, *, json_body=None, data=None, headers=None):
        merged = {"Authorization": f"Bearer {self._token}", **(headers or {})}
        try:
            response = self._session.request(
                method, f"{self.url}{_PREFIX}{path}", json=json_body, data=data,
                headers=merged, timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise CryptoError(
                f"cannot reach lmer-matrix-bridge at {self.url} ({exc.__class__.__name__})",
                transient=True,
            ) from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("error") or ""
            except ValueError:
                detail = (response.text or "")[:200]
            if response.status_code == 401:
                detail = (
                    "it answers its own appservice token only — act as the bridge's user "
                    "(LMER_MATRIX_ACCESS_TOKEN = its as_token)"
                )
            raise CryptoError(
                f"lmer-matrix-bridge refused {path}: {response.status_code} {detail}".strip(),
                status=response.status_code,
                transient=response.status_code >= 500,
            )
        return response

    def whoami(self) -> dict:
        return self._call("GET", "/whoami").json()

    def encrypt(self, room_id: str, event_type: str, content: dict) -> dict:
        body = self._call("POST", "/encrypt", json_body={
            "room_id": room_id, "type": event_type, "content": content,
        }).json()
        encrypted = body.get("content")
        if not isinstance(encrypted, dict):
            raise CryptoError("lmer-matrix-bridge returned no encrypted content")
        return encrypted

    def decrypt(self, room_id: str, events: list) -> list:
        """One result per event: ``{"event": …}`` or ``{"errcode": …}``."""
        results: list = []
        for start in range(0, len(events), _MAX_EVENTS):
            batch = [{**event, "room_id": room_id} for event in events[start:start + _MAX_EVENTS]]
            answer = self._call("POST", "/decrypt", json_body={"events": batch}).json().get("results")
            if not isinstance(answer, list) or len(answer) != len(batch):
                raise CryptoError("lmer-matrix-bridge answered a different number of events")
            results.extend(item if isinstance(item, dict) else {"errcode": UNDECRYPTABLE} for item in answer)
        return results

    def encrypt_attachment(self, data: bytes) -> tuple:
        """``(ciphertext, file)``; the caller uploads the ciphertext and sets ``file["url"]``."""
        response = self._call(
            "POST", "/attachment/encrypt", data=data,
            headers={"Content-Type": "application/octet-stream"},
        )
        try:
            file = json.loads(response.headers.get(_FILE_HEADER) or "")
        except ValueError:
            file = None
        if not isinstance(file, dict):
            raise CryptoError("lmer-matrix-bridge returned no key for the file")
        return response.content, file

    def decrypt_attachment(self, data: bytes, file: dict) -> bytes:
        keys = {key: value for key, value in file.items() if key != "url"}
        return self._call(
            "POST", "/attachment/decrypt", data=data,
            headers={"Content-Type": "application/octet-stream",
                     _FILE_HEADER: json.dumps(keys, separators=(",", ":"))},
        ).content
