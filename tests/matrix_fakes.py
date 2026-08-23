"""A homeserver that records instead of sending, for the bridge's tests.

Behind :class:`matrix_bridge.client.Homeserver`, so every test above the seam —
T4's guards, T5's cadence, T6's routing — runs with no network, no
``mautrix`` and no libolm. That is the whole reason D3 put the seam there.

The fake is deliberately literal: it answers what it is told to answer and
records what it was asked to do. It has no opinion about encryption, media
flags or thread relations, because a fake with opinions is a second
implementation of the thing under test.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from matrix_bridge.client import Homeserver


@dataclass
class SentMessage:
    """One thing the bridge asked the homeserver to send."""

    room_id: str
    text: Optional[str] = None
    thread_root: Optional[str] = None
    data: Optional[bytes] = None
    mime: Optional[str] = None
    filename: Optional[str] = None

    @property
    def is_attachment(self) -> bool:
        return self.data is not None


@dataclass
class FakeHomeserver(Homeserver):
    """Records sends; answers preconditions from its own attributes."""

    #: What :meth:`create_room` returns, and what the room is then called.
    room_id: str = "!room:matrix.example.net"
    encrypted: bool = True
    #: Tri-state, exactly as the real one: on, off, or could not be confirmed.
    authenticated_media_flag: Optional[bool] = True
    #: What the homeserver says this appservice's token belongs to.
    whoami_as: str = ""
    backup: Optional[bytes] = None
    #: Set to raise from :meth:`open_store`, for the unreadable-store cases.
    open_store_error: Optional[Exception] = None
    #: Set to raise from :meth:`has_backup`, for "the homeserver cannot say".
    backup_error: Optional[Exception] = None
    #: What the store exports. ``b""`` is the "the store gave us nothing" case
    #: the real transport refuses to write over a backup with.
    export: Optional[bytes] = None
    #: Refuse to send into an unencrypted room, the way the real transport does.
    #:
    #: Off by default, and deliberately: the upload-guard tests need a fake that
    #: *reports* the room as unencrypted so :func:`upload_decision` can refuse,
    #: and they still expect the accompanying note to be posted. A running
    #: bridge never reaches that state — ``ensure_room`` refuses to start in an
    #: unencrypted room — so the guard there is defensive. The tests that pin
    #: the **send** path turn this on, because that is where the fake would
    #: otherwise disagree with :meth:`MautrixHomeserver._send_encrypted`.
    strict_encryption: bool = False

    sent: list = field(default_factory=list)
    joined: list = field(default_factory=list)
    created: list = field(default_factory=list)
    opened: list = field(default_factory=list)
    backups_written: int = 0
    callback: Optional[Callable[[Any], Any]] = None
    #: Whether :meth:`aclose` has run — the fake models the real transport's
    #: release path so a test can tell a leaked session from a closed one.
    closed: bool = False
    #: ``(host, port)`` once :meth:`listen` has been awaited — the fact that
    #: proves the bridge is listening at all (!245 review).
    listening: Optional[tuple] = None
    #: Refuse every homeserver call until :meth:`listen` has run, the way
    #: ``mautrix``' ``AppService.intent`` does. See :meth:`_require_listener`.
    requires_listener: bool = False
    _event_serial: int = 0

    # --- the store ------------------------------------------------------------

    async def open_store(self, path: Path, *, restore: bool) -> None:
        if self.open_store_error is not None:
            raise self.open_store_error
        self.opened.append({"path": path, "restore": restore})

    async def has_backup(self) -> bool:
        self._require_listener()
        if self.backup_error is not None:
            raise self.backup_error
        return self.backup is not None

    async def back_up_store(self) -> None:
        """Keep the blob, and refuse an empty one — the real contract.

        Modelled rather than counted-only (!243 review): a fake that agrees with
        an implementation the transport does not have is what let the
        ``on_event`` no-op ship. Two guarantees are modelled here, both of them
        :meth:`MautrixHomeserver.back_up_store`'s: a successful start leaves a
        non-empty export, and an **empty** export never replaces one that is
        already there.
        """
        if self.export == b"":
            return
        self.backups_written += 1
        self.backup = self.export or self.backup or b"an encrypted export"

    # --- the room -------------------------------------------------------------

    async def create_room(self, *, invite: Sequence[str]) -> str:
        self._require_listener()
        self.created.append(list(invite))
        return self.room_id

    async def join_room(self, room_id: str) -> None:
        self._require_listener()
        self.joined.append(room_id)

    async def room_is_encrypted(self, room_id: str) -> bool:
        return self.encrypted

    async def authenticated_media(self) -> Optional[bool]:
        return self.authenticated_media_flag

    async def whoami(self) -> str:
        self._require_listener()
        return self.whoami_as

    # --- sending --------------------------------------------------------------

    async def send_text(
        self, room_id: str, text: str, *, thread_root: Optional[str] = None,
    ) -> str:
        self._refuse_cleartext()
        self.sent.append(SentMessage(room_id, text=text, thread_root=thread_root))
        return self._event_id()

    def _refuse_cleartext(self) -> None:
        if self.strict_encryption and not self.encrypted:
            from matrix_bridge.client import MatrixClientError

            raise MatrixClientError(
                "refusing to send into an unencrypted room (fake, strict mode)"
            )

    async def send_attachment(
        self, room_id: str, data: bytes, *, mime: str, filename: str,
        thread_root: Optional[str] = None,
    ) -> str:
        self.sent.append(SentMessage(
            room_id, data=data, mime=mime, filename=filename,
            thread_root=thread_root,
        ))
        return self._event_id()

    def on_event(self, callback: Callable[[Any], Any]) -> None:
        self.callback = callback

    async def listen(self, host: str, port: int) -> None:
        """Record the bind, the way the real listener does."""
        self.listening = (host, port)

    async def serve_forever(self) -> None:
        import asyncio

        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True

    def _require_listener(self) -> None:
        """Raise the way ``AppService.intent`` does before ``start()``.

        Off by default because the tests above the seam are about
        :class:`MatrixClient`'s decisions, where the listener is irrelevant.
        The end-to-end ordering test turns it on, because ordering is the one
        thing a fake that never complains cannot catch — and not catching it is
        exactly how a listener started after the first homeserver call shipped
        twice (!245 review).
        """
        if self.requires_listener and self.listening is None:
            raise AttributeError(
                "the intent attribute can only be used after starting"
            )

    async def deliver(self, event: Any) -> Any:
        """Push *event* at whoever registered, the way the homeserver would.

        The fake **delivers** rather than only remembering the callback (!243
        review): storing it was enough to make the inbound tests pass against a
        real transport whose ``on_event`` was a no-op, which is the worst shape
        a bug can take — a green suite over a bridge that would never have
        received anything. A test that never calls this proves nothing about
        delivery, and one that does fails if the wiring is dropped.
        """
        if self.callback is None:
            raise AssertionError(
                "nothing registered a callback — the bridge is not listening"
            )
        return await self.callback(event)

    # --- what a test reads ----------------------------------------------------

    @property
    def texts(self) -> list:
        return [message.text for message in self.sent if not message.is_attachment]

    @property
    def attachments(self) -> list:
        return [message for message in self.sent if message.is_attachment]

    def _event_id(self) -> str:
        self._event_serial += 1
        return f"$event-{self._event_serial}"
