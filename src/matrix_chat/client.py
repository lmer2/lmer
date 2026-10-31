"""A small Matrix client-server API client over ``requests``.

Exactly what the chat connector and listener need — whoami, alias
resolution, joining, sync with a filter, thread relations, sending into a
thread, media up and down — over an access token. In an encrypted room every
send is encrypted by ``lmer-matrix-bridge``'s device (:mod:`matrix_chat.crypto`)
or refused; there is no plaintext fallback.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote

import requests

from .crypto import UNDECRYPTABLE, BridgeCrypto, CryptoError

logger = logging.getLogger(__name__)

__all__ = ["MatrixError", "MatrixAccount", "MatrixClient", "DEFAULT_SYNC_TIMEOUT_MS"]

DEFAULT_SYNC_TIMEOUT_MS = 25000
_REQUEST_TIMEOUT = 30.0


class MatrixError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None, errcode: Optional[str] = None,
                 *, transient: bool = False, retry_after: Optional[float] = None,
                 not_sent: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.errcode = errcode
        #: Failed before the send request: the event certainly does not exist.
        self.not_sent = not_sent
        #: No answer, a 429 or a 5xx: worth retrying later. Set where the
        #: transport failed, since a definite answer can lack a status too.
        self.transient = transient
        #: A 429's ``retry_after_ms`` in seconds, when the homeserver sent one.
        self.retry_after = retry_after


@dataclass(frozen=True)
class MatrixAccount:
    homeserver: str
    user_id: str
    access_token: str = field(repr=False)
    #: ``LMER_MATRIX_BRIDGE_URL``: where the bridge lends its device, for
    #: encrypted rooms. Here so every path that builds a client gets it.
    bridge_url: Optional[str] = None

    @property
    def base(self) -> str:
        return self.homeserver.rstrip("/")


def _q(value: str) -> str:
    return quote(value, safe="")


class MatrixClient:
    def __init__(self, account: MatrixAccount, session=None, *, timeout: float = _REQUEST_TIMEOUT,
                 crypto: Optional[BridgeCrypto] = None) -> None:
        self.account = account
        self._session = session if session is not None else requests.Session()
        self._timeout = timeout
        if crypto is None and account.bridge_url:
            crypto = BridgeCrypto(account.bridge_url, account.access_token)
        self.crypto = crypto
        #: room id → whether its ``m.room.encryption`` state was last seen set.
        self._encrypted: dict = {}

    @property
    def can_encrypt(self) -> bool:
        return self.crypto is not None

    def _crypto(self, room_id: str, call):
        if self.crypto is None:
            raise MatrixError(f"refusing to send into {room_id} unencrypted: "
                              "no lmer-matrix-bridge to encrypt with", not_sent=True)
        try:
            return call()
        except CryptoError as exc:
            raise MatrixError(str(exc), status=exc.status, transient=exc.transient, not_sent=True) from exc

    # -- transport -------------------------------------------------------------

    def _call(self, method: str, path: str, *, params=None, json_body=None, data=None,
              headers=None, timeout=None, raw=False, stream=False):
        url = f"{self.account.base}{path}"
        merged = {"Authorization": f"Bearer {self.account.access_token}"}
        if headers:
            merged.update(headers)
        try:
            response = self._session.request(
                method, url, params=params, json=json_body, data=data, headers=merged,
                timeout=self._timeout if timeout is None else timeout, stream=stream,
            )
        except requests.RequestException as exc:
            raise MatrixError(f"cannot reach the homeserver at {self.account.base} ({exc.__class__.__name__})",
                              transient=True) from exc
        if response.status_code >= 400:
            errcode = None
            detail = ""
            try:
                body = response.json()
                errcode = body.get("errcode")
                detail = body.get("error") or ""
            except ValueError:
                detail = (response.text or "")[:200]
            retry = None
            if response.status_code == 429:
                try:
                    retry = float(response.json()["retry_after_ms"]) / 1000.0
                except (KeyError, ValueError, TypeError, AttributeError):
                    pass
                if retry is not None and not 0 <= retry <= 86400:
                    retry = None
                time.sleep(min(1.0 if retry is None else retry, 10.0))
            raise MatrixError(
                f"{method} {path} → {response.status_code} {errcode or ''} {detail}".strip(),
                status=response.status_code, errcode=errcode,
                transient=response.status_code == 429 or response.status_code >= 500,
                retry_after=retry,
            )
        if raw:
            return response
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            raise MatrixError(f"{method} {path}: unreadable reply")

    # -- identity and rooms ----------------------------------------------------

    def whoami(self) -> str:
        return str(self._call("GET", "/_matrix/client/v3/account/whoami").get("user_id") or "")

    def resolve_room(self, room: str) -> str:
        """A room id for *room*, resolving an alias through the directory."""
        if room.startswith("!"):
            return room
        body = self._call("GET", f"/_matrix/client/v3/directory/room/{_q(room)}")
        room_id = body.get("room_id")
        if not isinstance(room_id, str):
            raise MatrixError(f"alias {room} resolves to no room")
        return room_id

    def join(self, room: str) -> str:
        body = self._call("POST", f"/_matrix/client/v3/join/{_q(room)}", json_body={})
        return str(body.get("room_id") or room)

    def joined_rooms(self) -> list:
        return list(self._call("GET", "/_matrix/client/v3/joined_rooms").get("joined_rooms") or [])

    def room_is_encrypted(self, room_id: str) -> bool:
        try:
            self._call("GET", f"/_matrix/client/v3/rooms/{_q(room_id)}/state/m.room.encryption")
        except MatrixError as exc:
            if exc.status == 404:
                self._encrypted[room_id] = False
                return False
            raise
        self._encrypted[room_id] = True
        return True

    def note_encrypted(self, room_id: str) -> None:
        """The room's ``m.room.encryption`` state arrived: it is encrypted now."""
        self._encrypted[room_id] = True

    def encrypts(self, room_id: str) -> bool:
        """Whether what goes into *room_id* must be encrypted.

        Without a bridge nothing extra is asked: only a room already known to
        be encrypted counts, and the callers that can meet one probe first.
        With one, an unknown room is probed once; later changes reach a
        long-lived client through :meth:`note_encrypted`."""
        known = self._encrypted.get(room_id)
        if known is not None or self.crypto is None:
            return known is True
        try:
            return self.room_is_encrypted(room_id)
        except MatrixError as exc:
            exc.not_sent = True
            raise

    def direct_room(self, user_id: str) -> Optional[str]:
        """The room this account already uses to talk to *user_id*, or ``None``.

        Read from ``m.direct`` in the account's own data, which is where every
        matrix client records its DMs — so a message sent here lands in the
        conversation the person already has open rather than in a second room
        beside it. A room named there that this account has since left does not
        count: the id would still be a valid address and nothing would arrive.
        """
        try:
            body = self._call(
                "GET", f"/_matrix/client/v3/user/{_q(self.account.user_id)}/account_data/m.direct")
        except MatrixError as exc:
            if exc.status == 404:
                return None
            raise
        rooms = body.get(user_id)
        if not isinstance(rooms, list):
            return None
        joined = set(self.joined_rooms())
        for room in rooms:
            if not isinstance(room, str) or room not in joined:
                continue
            # That this account is in the room says nothing about whether the
            # recipient still is: a direct room may carry other members, and the
            # m.direct entry outlives the person leaving. Sending anyway would
            # deliver to whoever remains and report it as delivery to them
            # (MR !315, review round 1).
            if self.membership(room, user_id) in ("join", "invite"):
                return room
        return None

    def is_direct_room(self, room_id: str) -> bool:
        """Whether this account's m.direct directory identifies *room_id* as a DM."""
        try:
            body = self._call(
                "GET", f"/_matrix/client/v3/user/{_q(self.account.user_id)}/account_data/m.direct")
        except MatrixError as exc:
            if exc.status == 404:
                return False
            raise
        if not isinstance(body, dict) or any(
            not isinstance(rooms, list) or any(not isinstance(room, str) for room in rooms)
            for rooms in body.values()
        ):
            raise MatrixError("m.direct returned invalid room lists")
        return any(room_id in rooms for rooms in body.values())

    def membership(self, room_id: str, user_id: str) -> Optional[str]:
        """*user_id*'s membership in *room_id* — ``join``, ``invite``, ``leave``
        — or ``None`` when this account cannot read that state."""
        try:
            body = self._call(
                "GET",
                f"/_matrix/client/v3/rooms/{_q(room_id)}/state/m.room.member/{_q(user_id)}")
        except MatrixError as exc:
            if exc.status in (403, 404):
                return None
            raise
        membership = body.get("membership")
        return membership if isinstance(membership, str) else None

    def create_direct_room(self, user_id: str) -> str:
        """A fresh DM room with *user_id*, invited and flagged ``is_direct``.

        ``trusted_private_chat`` is the preset matrix clients use for a DM, and
        the flag is what makes the other side file it as one rather than as a
        room invitation. The invite is not waited on — a message can be sent to
        a room the person has not accepted yet, and it appears when they do.
        """
        body = self._call("POST", "/_matrix/client/v3/createRoom", json_body={
            "preset": "trusted_private_chat",
            "is_direct": True,
            "invite": [user_id],
        })
        room_id = body.get("room_id")
        if not isinstance(room_id, str) or not room_id:
            raise MatrixError("createRoom returned no room id")
        self._record_direct(user_id, room_id)
        return room_id

    def _record_direct(self, user_id: str, room_id: str) -> None:
        """File *room_id* under *user_id* in this account's own ``m.direct``.

        The spec puts this on the inviting client rather than the homeserver,
        which is why a DM a person opens with the bot is findable and one the
        bot opens is not. Without it :meth:`direct_room` never sees the room
        again and every send makes another one, each a fresh invite the
        recipient has to accept (MR !315, review round 1).

        A failure here costs reuse, not the message: the room exists and the
        caller is about to write to it, so this is logged rather than raised.
        """
        path = f"/_matrix/client/v3/user/{_q(self.account.user_id)}/account_data/m.direct"
        try:
            current = self._call("GET", path)
        except MatrixError as exc:
            if exc.status != 404:
                logger.warning("matrix_direct_read_failed error=%s", exc)
                return
            current = {}
        if not isinstance(current, dict):
            current = {}
        rooms = current.get(user_id)
        rooms = [room for room in rooms if isinstance(room, str)] if isinstance(rooms, list) else []
        if room_id in rooms:
            return
        current[user_id] = rooms + [room_id]
        try:
            self._call("PUT", path, json_body=current)
        except MatrixError as exc:
            logger.warning("matrix_direct_write_failed room=%s error=%s", room_id, exc)

    def display_name(self, user_id: str) -> str:
        try:
            body = self._call("GET", f"/_matrix/client/v3/profile/{_q(user_id)}/displayname")
        except MatrixError:
            return user_id
        name = body.get("displayname")
        return name if isinstance(name, str) and name.strip() else user_id

    # -- reading ---------------------------------------------------------------

    def sync(self, *, since: Optional[str] = None, filter_body: Optional[dict] = None,
             timeout_ms: int = DEFAULT_SYNC_TIMEOUT_MS) -> dict:
        params: dict = {"timeout": int(timeout_ms)}
        if since:
            params["since"] = since
        if filter_body is not None:
            params["filter"] = json.dumps(filter_body, separators=(",", ":"))
        return self._call(
            "GET", "/_matrix/client/v3/sync", params=params,
            timeout=(self._timeout, timeout_ms / 1000.0 + self._timeout),
        )

    def get_event(self, room_id: str, event_id: str) -> dict:
        return self._call("GET", f"/_matrix/client/v3/rooms/{_q(room_id)}/event/{_q(event_id)}")

    def history_before(self, room_id: str, event_id: str, limit: int) -> list:
        """Up to *limit* room messages before the event, in timeline order."""
        if limit == 0:
            return []
        prefix = f"/_matrix/client/v3/rooms/{_q(room_id)}"
        context = self._call("GET", f"{prefix}/context/{_q(event_id)}", params={"limit": 0})
        cursor = context.get("start")
        if not cursor:
            raise MatrixError("event context returned no backwards pagination token")
        found: list = []
        seen: set = set()
        while cursor and len(found) < limit:
            if cursor in seen:
                raise MatrixError("room history pagination repeated a token")
            seen.add(cursor)
            page = self._call("GET", f"{prefix}/messages", params={
                "from": cursor, "dir": "b", "limit": limit - len(found),
                "filter": json.dumps({"types": ["m.room.message"]}),
            })
            found.extend(item for item in page.get("chunk", [])
                         if isinstance(item, dict) and item.get("type") == "m.room.message"
                         and item.get("event_id") != event_id)
            cursor = page.get("end")
        return list(reversed(found[:limit]))

    def thread_events(self, room_id: str, root_event_id: str, *, limit: Optional[int] = 100) -> list:
        """Thread replies, oldest first; ``limit=None`` follows every page."""
        found: list = []
        params: dict = {"limit": min(limit, 100) if limit is not None else 100, "dir": "f"}
        while True:
            body = self._call(
                "GET", f"/_matrix/client/v1/rooms/{_q(room_id)}/relations/{_q(root_event_id)}/m.thread",
                params=params,
            )
            chunk = body.get("chunk") or []
            found.extend(event for event in chunk if isinstance(event, dict))
            token = body.get("next_batch")
            if not token or not chunk or (limit is not None and len(found) >= limit):
                break
            params["from"] = token
        found.sort(key=lambda event: event.get("origin_server_ts", 0))
        return found

    # -- writing ---------------------------------------------------------------

    def send_event(self, room_id: str, event_type: str, content: dict, *,
                   transaction: Optional[str] = None) -> str:
        """Send one room event, encrypted when :meth:`encrypts` says so."""
        if self.encrypts(room_id):
            content = self._crypto(room_id, lambda: self.crypto.encrypt(room_id, event_type, content))
            event_type = "m.room.encrypted"
        txn = transaction or f"lmer-{uuid.uuid4().hex}"
        body = self._call(
            "PUT", f"/_matrix/client/v3/rooms/{_q(room_id)}/send/{_q(event_type)}/{_q(txn)}",
            json_body=content,
        )
        event_id = body.get("event_id")
        if not isinstance(event_id, str):
            raise MatrixError("send returned no event_id")
        return event_id

    def send_reaction(self, room_id: str, event_id: str, key: str, *, transaction: str) -> str:
        """Annotate a message using a stable transaction ID for retries."""
        return self.send_event(
            room_id, "m.reaction",
            {"m.relates_to": {"rel_type": "m.annotation", "event_id": event_id, "key": key}},
            transaction=transaction,
        )

    def send_message(self, room_id: str, content: dict) -> str:
        return self.send_event(room_id, "m.room.message", content)

    def send_thread_text(self, room_id: str, root_event_id: str, text: str, *,
                         last_event_id: Optional[str] = None, msgtype: str = "m.text",
                         extra: Optional[dict] = None) -> str:
        """*extra* adds custom content keys — how a post says which lmer session
        made it (:mod:`lmer_cli.thread_posts`), since the bot account is shared."""
        content = {
            **(extra or {}),
            "msgtype": msgtype,
            "body": text,
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": root_event_id,
                "is_falling_back": True,
                "m.in_reply_to": {"event_id": last_event_id or root_event_id},
            },
        }
        return self.send_message(room_id, content)

    def send_thread_file(self, room_id: str, root_event_id: str, *, mxc: Optional[str] = None,
                         name: str, media_type: str, size: int, last_event_id: Optional[str] = None,
                         extra: Optional[dict] = None, file: Optional[dict] = None) -> str:
        """*extra* as in :meth:`send_thread_text`: a file event needs the same
        stamp, or the receiving run is told about a post whose file it never
        sees (review of !300). *file* replaces *mxc* for an encrypted upload
        (:meth:`upload_encrypted`)."""
        if (mxc is None) == (file is None):
            raise ValueError("send_thread_file needs exactly one of mxc and file")
        msgtype = "m.image" if media_type.startswith("image/") else "m.file"
        content = {
            **(extra or {}),
            "msgtype": msgtype,
            "body": name,
            "filename": name,
            **({"file": file} if file is not None else {"url": mxc}),
            "info": {"mimetype": media_type, "size": size},
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": root_event_id,
                "is_falling_back": True,
                "m.in_reply_to": {"event_id": last_event_id or root_event_id},
            },
        }
        return self.send_message(room_id, content)

    # -- media -----------------------------------------------------------------

    def upload(self, data: bytes, *, content_type: str, filename: Optional[str]) -> str:
        body = self._call(
            "POST", "/_matrix/media/v3/upload", params={"filename": filename} if filename else None,
            data=data, headers={"Content-Type": content_type},
        )
        mxc = body.get("content_uri")
        if not isinstance(mxc, str):
            raise MatrixError("upload returned no content_uri")
        return mxc

    def upload_encrypted(self, data: bytes) -> dict:
        """Upload *data* as ciphertext; the event's ``file`` object, ``url`` set.

        No filename: the media repository keeps it in the clear."""
        ciphertext, file = self._crypto("an encrypted room", lambda: self.crypto.encrypt_attachment(data))
        file["url"] = self.upload(ciphertext, content_type="application/octet-stream", filename=None)
        return file

    def download_content(self, content: dict, *, max_bytes: Optional[int] = None) -> tuple:
        """A media event's bytes — decrypted when it carries a ``file`` — and type.

        ``(None, type)`` past *max_bytes*, as :meth:`download`.
        """
        file = content.get("file")
        info = content.get("info") if isinstance(content.get("info"), dict) else {}
        if not isinstance(file, dict):
            return self.download(str(content.get("url") or ""), max_bytes=max_bytes)
        if self.crypto is None:
            raise MatrixError("an encrypted file, and no lmer-matrix-bridge to decrypt it with")
        data, _ = self.download(str(file.get("url") or ""), max_bytes=max_bytes)
        media_type = str(info.get("mimetype") or "application/octet-stream")
        if data is None:
            return None, media_type
        return self._crypto("an encrypted room", lambda: self.crypto.decrypt_attachment(data, file)), media_type

    def decrypt_events(self, room_id: str, events: list) -> list:
        """``(event, problem)`` per event, in order.

        A plaintext event comes back as itself. An encrypted one comes back
        decrypted, or as ``(None, problem)``: :data:`~matrix_chat.crypto.NO_SESSION`
        while its room key has not reached the bridge yet, ``undecryptable``
        for good, ``no_crypto`` without a bridge.
        """
        out: list = [(event, None) for event in events]
        encrypted = [index for index, event in enumerate(events)
                     if isinstance(event, dict) and event.get("type") == "m.room.encrypted"]
        if not encrypted:
            return out
        if self.crypto is None:
            for index in encrypted:
                out[index] = (None, "no_crypto")
            return out
        results = self._crypto(room_id, lambda: self.crypto.decrypt(room_id, [events[index] for index in encrypted]))
        for index, result in zip(encrypted, results):
            event = result.get("event")
            out[index] = (event, None) if isinstance(event, dict) else (None, result.get("errcode") or UNDECRYPTABLE)
        return out

    def download(self, mxc: str, *, max_bytes: Optional[int] = None) -> tuple:
        """``(bytes, content_type)`` for an ``mxc://server/media`` URI.

        The authenticated endpoint (spec v1.11) first; a homeserver that does
        not serve it answers 404 and the legacy unauthenticated one is tried.

        *max_bytes* streams the body and stops counting past it, answering
        ``(None, content_type)``: the file is posted by whoever is in the room,
        so "how big is it" is not a question the caller can ask first.
        """
        if not mxc.startswith("mxc://"):
            raise MatrixError(f"not an mxc URI: {mxc!r}")
        server, _, media = mxc[len("mxc://"):].partition("/")
        if not server or not media:
            raise MatrixError(f"malformed mxc URI: {mxc!r}")
        for path in (
            f"/_matrix/client/v1/media/download/{_q(server)}/{_q(media)}",
            f"/_matrix/media/v3/download/{_q(server)}/{_q(media)}",
        ):
            try:
                response = self._call(
                    "GET", path, raw=True, timeout=60.0, stream=max_bytes is not None,
                )
            except MatrixError as exc:
                if exc.status in (404, 400) and path.startswith("/_matrix/client/v1/"):
                    continue
                raise
            content_type = (
                response.headers.get("Content-Type") or "application/octet-stream"
            ).split(";")[0]
            if max_bytes is None:
                return response.content, content_type
            body = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                body.extend(chunk)
                if len(body) > max_bytes:
                    response.close()
                    return None, content_type
            return bytes(body), content_type
        raise MatrixError(f"cannot download {mxc}")
