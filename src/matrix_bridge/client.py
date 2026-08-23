"""The one seam every homeserver call goes through (spec D3).

Two things live here, and the split is the point.

**Policy** — what the bridge will and will not do — is plain functions and a
class that holds no library: whether an upload may happen
(:func:`upload_decision`), what to do with a crypto store that is present,
absent or unreadable (:func:`store_plan`), what a message looks like once it has
been scrubbed and truncated. It is all reachable from a test with no homeserver
and no ``mautrix`` installed, which is what makes the guards in it worth
believing.

**Transport** — the ``mautrix-python`` appservice, its crypto machine and its
store — sits behind :class:`Homeserver`, a protocol with nine methods. The
library is imported inside :class:`MautrixHomeserver`, never at module scope, so
importing this module costs nothing and works anywhere. That adapter is first
exercised for real against charasis in T9; everything above it is exercised now.

The three rules this file exists to keep
----------------------------------------
1. **Nothing reaches the homeserver unscrubbed.** Text and attachments go
   through :mod:`matrix_bridge.scrub` on the way out — one rule, the project's
   own.
2. **An upload happens only into an encrypted room with authenticated media
   confirmed on.** Not "assumed on", not "on unless we couldn't check":
   unconfirmed is refused. Media uploaded while the flag is off stays
   anonymously fetchable by its mxc id forever, and a room's history does not
   expire.
3. **A crypto store the bridge cannot read is a refusal, not a fresh device.**
   Silently minting a new device leaves the room encrypted to keys the bridge
   does not have, and the symptom is a bridge that appears fine and says
   nothing. The refusal names :data:`~matrix_bridge.config.ENV_RECOVERY_KEY`
   and the store path, because those are the two things an operator can act on.

Why the recovery key protects an *export* rather than a server-side backup:
``mautrix`` implements no ``/_matrix/client/v3/room_keys`` (measured in the
run's ``spike.md``), so D7's stated fallback is what ships — the store is
exported, encrypted with an SSSS key derived from
``LMER_MATRIX_RECOVERY_KEY``, and kept in the appservice user's own account
data. Same env var, same operator story, same failure mode: store and key both
lost means a new device and unreadable history, and slice 1 only ever needs new
messages.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from lmer_platform import config as platform_config
from matrix_bridge import scrub
from matrix_bridge.config import ENV_RECOVERY_KEY, MatrixConfig, Secrets

logger = logging.getLogger(__name__)

#: Where the encrypted store export lives: account data on the appservice's own
#: user. Namespaced like every other custom event type this project invents.
BACKUP_EVENT_TYPE = "net.20c.lmer.crypto_store_backup"

#: The store itself, under ``~/.lmer/platform/matrix/crypto/``.
CRYPTO_DIRNAME = "crypto"
STORE_FILENAME = "store.db"

#: ``mautrix``' own pickle key for a bridge's crypto store. Not a secret —
#: it is a constant in the library and the store's confidentiality comes
#: from the file's mode, not from this string. Spelled with the same value
#: so a store written by one version is readable by the next.
_PICKLE_KEY = "mautrix.bridge.e2ee"

#: How much text goes into one room message. Matrix has no hard limit worth
#: relying on and a phone has a screen: past this, the message is truncated and
#: the control-UI link (which the caller has already put in the text) is what
#: carries the rest. D8's rule — a link is one tap, an attachment is a download.
MAX_MESSAGE_CHARS = 3000

#: What :func:`store_plan` can answer.
STORE_OPEN = "open"
STORE_RESTORE = "restore"
STORE_FRESH = "fresh"
STORE_REFUSE = "refuse"


class MatrixClientError(RuntimeError):
    """The bridge cannot do this, and says which precondition failed."""


@dataclass(frozen=True)
class UploadDecision:
    """Whether an attachment may be sent, and what to say if it may not.

    ``note`` goes into the room, appended to the message the attachment would
    have accompanied — the reader is told the file was dropped rather than left
    wondering. ``reason`` goes into the log and names the precondition.
    """

    allowed: bool
    note: Optional[str] = None
    reason: Optional[str] = None


def upload_decision(
    *, room_encrypted: bool, authenticated_media: Optional[bool],
) -> UploadDecision:
    """D8's three preconditions, minus the scrub (which the caller applies).

    *authenticated_media* is tri-state on purpose: ``True`` confirmed on,
    ``False`` confirmed off, ``None`` could not be confirmed. The last is a
    refusal and not a maybe — an unreachable media-config endpoint is exactly
    the case where guessing is most tempting and most expensive.
    """
    if not room_encrypted:
        return UploadDecision(
            False,
            note="(attachment dropped: this room is not encrypted)",
            reason="upload refused: room is not encrypted",
        )
    if authenticated_media is not True:
        detail = (
            "authenticated media is off on this homeserver"
            if authenticated_media is False
            else "authenticated media could not be confirmed"
        )
        return UploadDecision(
            False,
            note=f"(attachment dropped: {detail})",
            reason=f"upload refused: {detail}",
        )
    return UploadDecision(True)


def store_plan(
    *, present: bool, readable: bool, backup_present: bool,
) -> str:
    """What to do with the crypto store found on disk.

    ============  ==============  =================  ==================
    present       readable        backup             plan
    ============  ==============  =================  ==================
    yes           yes             —                  ``open``
    yes           no              yes                ``restore``
    yes           no              no                 ``refuse``
    no            —               yes                ``restore``
    no            —               no                 ``fresh``
    ============  ==============  =================  ==================

    The two rows worth arguing about: a store that is *absent* with no backup is
    a first start, and minting a device there is the only thing that can happen.
    A store that is *present and unreadable* with no backup is a broken
    deployment, and minting a device there hides it — the bridge would run,
    encrypt to keys nobody else has, and look healthy while the room went quiet.
    """
    if present and readable:
        return STORE_OPEN
    if backup_present:
        return STORE_RESTORE
    return STORE_FRESH if not present else STORE_REFUSE


@dataclass(frozen=True)
class StoreStatus:
    """What is on disk where the crypto store belongs."""

    path: Path
    present: bool
    readable: bool


def inspect_store(path: Path) -> StoreStatus:
    """Is there a store, and can this process read it?

    Deliberately a file check rather than a database open: a store whose file
    is unreadable and a store whose schema is broken are the same problem for
    the operator (restore it or lose it), and the transport reports the second
    when it tries.
    """
    if not path.exists():
        return StoreStatus(path, present=False, readable=False)
    try:
        with path.open("rb") as handle:
            handle.read(1)
    except OSError as exc:
        logger.warning("matrix_crypto_store_unreadable path=%s error=%s", path, exc)
        return StoreStatus(path, present=True, readable=False)
    return StoreStatus(path, present=True, readable=True)


async def export_store_bytes(execute, path: Path) -> bytes:
    """The store's bytes, **after** its write-ahead log is folded into the file.

    ``mautrix``' aiosqlite backend opens the crypto store in WAL mode unless the
    caller supplies a journal mode, so recent commits live in ``store.db-wal``
    until a checkpoint — and the backup runs right after the device identity is
    written, which is exactly when the newest rows are least likely to have been
    checkpointed. Reading the file without this produced an export that could be
    missing the one thing it exists to preserve (!243 review).

    *execute* is the open database's ``execute``, taken as a parameter so the
    behaviour is testable against stdlib ``sqlite3`` with no ``mautrix`` and no
    homeserver — the test writes a row in WAL mode, checks the main file does
    **not** contain it, and then checks that this function's output does.
    """
    await execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return path.read_bytes()


def truncate(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    """Cut *text* to *limit*, marking the cut. Never silently.

    The bridge's messages carry a control-UI link, so what is lost here is
    recoverable in one tap — which is why this is a truncation rather than an
    attached file (D8).
    """
    if len(text) <= limit:
        return text
    marker = "… (truncated)"
    return text[: max(0, limit - len(marker))].rstrip() + marker


class Homeserver:
    """What the bridge needs from Matrix, and nothing else.

    Implemented for real by :class:`MautrixHomeserver` and for tests by
    ``tests/matrix_fakes.FakeHomeserver``. Methods raise
    :class:`NotImplementedError` here rather than being a ``Protocol`` so that a
    fake which forgets one fails loudly in the test that needed it.
    """

    async def open_store(self, path: Path, *, restore: bool) -> None:
        """Open the crypto store at *path*, restoring from backup first."""
        raise NotImplementedError

    async def has_backup(self) -> bool:
        """Is there an encrypted store export to restore from?"""
        raise NotImplementedError

    async def back_up_store(self) -> None:
        """Encrypt the store with the recovery key and store the export."""
        raise NotImplementedError

    async def create_room(self, *, invite: Sequence[str]) -> str:
        """Create the room, encrypted, inviting *invite*. Returns its id."""
        raise NotImplementedError

    async def join_room(self, room_id: str) -> None:
        raise NotImplementedError

    async def room_is_encrypted(self, room_id: str) -> bool:
        raise NotImplementedError

    async def authenticated_media(self) -> Optional[bool]:
        """``True``/``False``/``None`` — on, off, or could not be confirmed."""
        raise NotImplementedError

    async def send_text(
        self, room_id: str, text: str, *, thread_root: Optional[str] = None,
    ) -> str:
        raise NotImplementedError

    async def send_attachment(
        self, room_id: str, data: bytes, *, mime: str, filename: str,
        thread_root: Optional[str] = None,
    ) -> str:
        raise NotImplementedError

    def on_event(self, callback: Callable[[Any], Any]) -> None:
        """Deliver inbound room events to *callback*."""
        raise NotImplementedError


class MatrixClient:
    """The bridge's whole relationship with the homeserver.

    Constructed with a :class:`Homeserver`; the default one is built by
    :func:`connect`, which is also the only place ``mautrix`` is imported.
    """

    def __init__(
        self,
        config: MatrixConfig,
        homeserver: Homeserver,
        *,
        record_room_id: Optional[Callable[[str], None]] = None,
    ):
        self.config = config
        self.homeserver = homeserver
        self.room_id: Optional[str] = config.room_id
        self._record_room_id = (
            record_room_id if record_room_id is not None else _record_room_id
        )

    @property
    def store_path(self) -> Path:
        return self.config.state_dir / CRYPTO_DIRNAME / STORE_FILENAME

    async def start(self) -> None:
        """Open the crypto store, then the room. Refuses rather than degrades."""
        await self.open_store()
        await self.ensure_room()

    async def open_store(self) -> str:
        """Apply :func:`store_plan` to what is on disk. Returns the plan taken."""
        status = inspect_store(self.store_path)
        try:
            backup_present = await self.homeserver.has_backup()
        except Exception as exc:
            # "Cannot tell" is not "no" (!243 review): for an absent store, "no"
            # means mint a device and write a fresh export over whatever is
            # there, so a homeserver having a bad minute would cost this bridge
            # its restorable history.
            raise MatrixClientError(
                f"cannot tell whether a crypto-store backup exists ({exc}). "
                f"Refusing to start rather than mint a new device and overwrite "
                f"one that may be there — the store is {status.path}, and "
                f"{ENV_RECOVERY_KEY} is what unlocks the backup."
            ) from exc
        plan = store_plan(
            present=status.present,
            readable=status.readable,
            backup_present=backup_present,
        )
        if plan == STORE_REFUSE:
            raise MatrixClientError(
                f"the crypto store at {status.path} cannot be read and there is "
                f"no backup to restore from. Starting anyway would mint a new "
                f"device and leave this room's history unreadable to the bridge "
                f"while it looked healthy. Restore the file, or set "
                f"{ENV_RECOVERY_KEY} to the key whose backup should be restored."
            )
        try:
            await self.homeserver.open_store(
                self.store_path, restore=plan == STORE_RESTORE,
            )
        except Exception as exc:
            raise MatrixClientError(
                f"the crypto store at {status.path} could not be "
                f"{'restored' if plan == STORE_RESTORE else 'opened'} ({exc}). "
                f"{ENV_RECOVERY_KEY} is what unlocks the backup; a bridge that "
                f"started without one would encrypt to keys nobody can read."
            ) from exc
        logger.info(
            "matrix_crypto_store plan=%s path=%s", plan, status.path,
        )
        # Every successful start refreshes the export, including ``open``.
        # The earlier cut backed up only on a fresh or restored store *and*
        # refused to overwrite, which made the first export the only one there
        # would ever be — so a short first export stayed short forever and the
        # restore it exists for would have produced a store that decrypts
        # nothing (!243 review). A backup written from a store this process has
        # just opened is never worse than the one it replaces.
        await self.homeserver.back_up_store()
        return plan

    async def ensure_room(self) -> str:
        """Join the configured room, or create one and record its id (D5).

        A configured room is **checked**, not assumed: joining one that is not
        encrypted and carrying on would put every announcement, every question
        and every answer into the room in cleartext, on a homeserver whose
        identity providers are open-signup (!243 review). The bridge refuses
        instead, and says which room.
        """
        if self.room_id:
            await self.homeserver.join_room(self.room_id)
            if not await self.homeserver.room_is_encrypted(self.room_id):
                raise MatrixClientError(
                    f"room {self.room_id} is not encrypted. The bridge posts "
                    f"questions, answers and run titles into it, so it will not "
                    f"run in a room that stores them in the clear — enable "
                    f"encryption in that room, or unset `matrix.room_id` and "
                    f"let the bridge create one."
                )
            return self.room_id

        invited = sorted(self.config.allow)
        room_id = await self.homeserver.create_room(invite=invited)
        if not room_id:
            raise MatrixClientError(
                "the homeserver did not return a room id for the room it was "
                "asked to create; refusing to run without one rather than "
                "creating a second room on the next start"
            )
        self.room_id = room_id
        self._record_room_id(room_id)
        logger.info(
            "matrix_room_created room=%s invited=%d", room_id, len(invited),
        )
        return room_id

    async def send_thread_root(self, text: str) -> str:
        """Open a thread. Returns the root event id — the run's address (D5)."""
        return await self._send(self._room(), text, thread_root=None)

    async def send_in_thread(self, root: str, text: str) -> str:
        """Say something more about the run whose thread *root* is."""
        return await self._send(self._room(), text, thread_root=root)

    async def upload(
        self, root: str, data: bytes, mime: str, filename: str, *,
        text: str = "",
    ) -> Optional[str]:
        """Attach a file to *root*'s thread — or drop it and say why.

        Returns the attachment's event id, or ``None`` when it was refused. A
        refusal still posts *text* (with a one-line note appended), because the
        message is what the reader needed and the file was the extra.
        """
        room_id = self._room()
        decision = upload_decision(
            room_encrypted=await self.homeserver.room_is_encrypted(room_id),
            authenticated_media=await self.homeserver.authenticated_media(),
        )
        if decision.allowed:
            cleaned = scrub.scrub_bytes(data, mime)
            if cleaned.refused:
                decision = UploadDecision(
                    False,
                    note="(attachment dropped: it carried a secret)",
                    reason=cleaned.reason,
                )
            else:
                data = cleaned.data
                if cleaned.changed:
                    logger.info(
                        "matrix_attachment_scrubbed filename=%s mime=%s",
                        filename, mime,
                    )

        if not decision.allowed:
            logger.warning(
                "matrix_upload_refused filename=%s mime=%s reason=%s",
                filename, mime, decision.reason,
            )
            note = decision.note or "(attachment dropped)"
            await self._send(
                room_id, f"{text}\n{note}".strip(), thread_root=root,
            )
            return None

        # The message first, then the file. An attachment's body is its
        # filename, so returning here without sending *text* posted a bare
        # `m.file` and dropped what the message was for (!243 review).
        if text:
            await self._send(room_id, text, thread_root=root)
        return await self.homeserver.send_attachment(
            room_id, data, mime=mime, filename=filename, thread_root=root,
        )

    def on_event(self, callback: Callable[[Any], Any]) -> None:
        self.homeserver.on_event(callback)

    async def _send(
        self, room_id: str, text: str, *, thread_root: Optional[str],
    ) -> str:
        return await self.homeserver.send_text(
            room_id, truncate(scrub.scrub_text(text)), thread_root=thread_root,
        )

    def _room(self) -> str:
        if not self.room_id:
            raise MatrixClientError(
                "no room yet: `ensure_room()` runs before anything is sent, so "
                "a message here would have nowhere to go"
            )
        return self.room_id


def _record_room_id(room_id: str) -> None:
    """Write the created room's id back into ``config.json`` (D5).

    Through the platform's own writer, which merges into the stored mapping and
    preserves what it does not know — the reason ``matrix`` is a declared field
    rather than a key the loader tolerates (see the run's ``spike.md``).
    """
    stored = dict(getattr(platform_config.load(), "matrix", None) or {})
    stored["room_id"] = room_id
    platform_config.update_stored({"matrix": stored})


class MautrixHomeserver(Homeserver):
    """:class:`Homeserver` over ``mautrix-python``'s appservice and crypto.

    Every ``mautrix`` import is inside a method. That is not shyness about the
    dependency: this module is imported by ``lmer-matrix-bridge check``, which
    an operator runs precisely when the deployment is broken, and by the test
    suite on hosts that have no libolm. Neither should fail at import time.

    This class is the part of the bridge that T9 exercises against the real
    homeserver for the first time; the policy above it is tested here.
    """

    def __init__(self, config: MatrixConfig, secrets: Secrets):
        self.config = config
        self._secrets = secrets
        self._as = None
        self._crypto = None
        self._crypto_client = None
        self._database = None
        self._store = None
        self._ssss_key = None
        self._callback: Optional[Callable[[Any], Any]] = None

    async def open_store(self, path: Path, *, restore: bool) -> None:
        """Open the SQLite crypto store and bring up the appservice's device.

        The sequence is ``mautrix``' own (``mautrix/bridge/e2ee.py``), narrowed
        to the appservice path this bridge runs on: MSC4190 device creation (no
        ``/login``), the store as the sync store, and the homeserver pushing
        one-time-key counts, device lists and to-device events into the crypto
        machine. Deviating from that sequence is how a bridge ends up with a
        device the homeserver does not know about.
        """
        from mautrix.client import Client
        from mautrix.crypto import OlmMachine, PgCryptoStateStore, PgCryptoStore
        from mautrix.types import TrustState
        from mautrix.util.async_db import Database

        path.parent.mkdir(parents=True, exist_ok=True)
        if restore:
            await self._restore(path)

        database = Database.create(
            f"sqlite:///{path}", upgrade_table=PgCryptoStore.upgrade_table,
        )
        await database.start()
        store = PgCryptoStore("", _PICKLE_KEY, database)
        await store.open()
        state_store = PgCryptoStateStore(database)

        appservice = self._appservice()
        client = Client(
            base_url=self.config.homeserver,
            mxid=self.config.sender,
            sync_store=store,
            state_store=state_store,
        )
        machine = OlmMachine(client, store, state_store)
        # Trust is the allowlist's job, not device verification's (D7): an
        # operator answering from a phone they just set up must not have to
        # verify it before the bridge will talk to them.
        machine.share_keys_min_trust = TrustState.UNVERIFIED
        machine.send_keys_min_trust = TrustState.UNVERIFIED
        # Appservice (MSC3202) mode: there is no /sync loop here, so these three
        # arrive on the transactions the homeserver pushes.
        appservice.otk_handler = machine.handle_as_otk_counts
        appservice.device_list_handler = machine.handle_as_device_lists
        appservice.to_device_handler = machine.handle_as_to_device_event

        device_id = await store.get_device_id()
        client.api.token = self._secrets.as_token
        await client.create_device_msc4190(
            device_id=device_id, initial_display_name=f"lmer-{self.config.name}",
        )
        await machine.load()
        if not device_id:
            await store.put_device_id(client.device_id)
            await machine.share_keys()

        self._database = database
        self._store = store
        self._crypto = machine
        self._crypto_client = client

    async def has_backup(self) -> bool:
        """Is there a store export to restore from? Raises when it cannot tell.

        Deliberately not "no" on an error (!243 review). "No backup" is a
        *destructive* answer for an absent store: it means mint a new device and
        write a fresh export over whatever is there — so a homeserver that
        answers 500 for ten seconds would cost the bridge its restorable
        history. An unknown is an unknown, and :meth:`MatrixClient.open_store`
        refuses to start on it.
        """
        return await self._read_backup() is not None

    async def back_up_store(self) -> None:
        """Encrypt this device's store and keep it in the appservice's account data.

        Written on every successful start rather than only the first: the store
        gains keys as the bridge runs, and an export frozen at first start is a
        restore that decrypts nothing later. The one thing that is refused is
        writing an **empty** export over whatever is there — that is the only
        way this call can make the situation worse, and it means something is
        wrong with the store rather than with the backup.
        """
        blob = await self._export_store()
        if not blob:
            logger.warning(
                "matrix_backup_refused — the store exported zero bytes; keeping "
                "the existing backup",
            )
            return
        machine, key = await self._ssss()
        await machine.set_encrypted_account_data(BACKUP_EVENT_TYPE, blob, key)

    async def create_room(self, *, invite: Sequence[str]) -> str:
        from mautrix.types import EventType, RoomCreatePreset

        client = self._appservice_client()
        return await client.create_room(
            preset=RoomCreatePreset.PRIVATE,
            invitees=list(invite),
            name=f"lmer — {self.config.name}",
            initial_state=[{
                "type": str(EventType.ROOM_ENCRYPTION),
                "state_key": "",
                "content": {"algorithm": "m.megolm.v1.aes-sha2"},
            }],
        )

    async def join_room(self, room_id: str) -> None:
        await self._appservice_client().join_room(room_id)

    async def room_is_encrypted(self, room_id: str) -> bool:
        from mautrix.errors import MNotFound
        from mautrix.types import EventType

        try:
            state = await self._appservice_client().get_state_event(
                room_id, EventType.ROOM_ENCRYPTION,
            )
        except MNotFound:
            return False
        return bool(state and getattr(state, "algorithm", None))

    async def authenticated_media(self) -> Optional[bool]:
        """Whether ``matrix_enable_authenticated_media`` is on — **as asserted**.

        This used to probe ``/_matrix/client/v1/media/config`` and treat "the
        endpoint answers" as "the setting is on". That was wrong in the way that
        matters (!243 review): those endpoints are served by every Synapse from
        spec v1.11 regardless of the setting, which governs whether the *legacy*
        unauthenticated download route still serves local media. The probe
        therefore passed on precisely the homeserver the guard exists to refuse.

        There is no client-visible signal for the setting — it is a server
        configuration fact, and the client API does not report server
        configuration. So the bridge does not pretend to measure it: the value
        is ``matrix.authenticated_media``, which the operator sets in the same
        change that sets the homeserver flag and installs the registration, and
        which defaults to false. ``lmer-matrix-bridge check`` prints it as an
        assertion rather than as a measurement.

        The guard is not the only thing standing between an attachment and an
        anonymously-fetchable blob: uploads also require the room to be
        encrypted (D8), which is now enforced on the join path too, so the bytes
        that reach the homeserver are ciphertext whatever the flag says.
        """
        return self.config.authenticated_media

    async def send_text(
        self, room_id: str, text: str, *, thread_root: Optional[str] = None,
    ) -> str:
        from mautrix.types import Format, MessageType, TextMessageEventContent

        content = TextMessageEventContent(msgtype=MessageType.TEXT, body=text)
        if thread_root:
            content.set_thread_parent(thread_root)
        return await self._send_encrypted(room_id, content)

    async def send_attachment(
        self, room_id: str, data: bytes, *, mime: str, filename: str,
        thread_root: Optional[str] = None,
    ) -> str:
        from mautrix.crypto.attachments import encrypt_attachment
        from mautrix.types import (
            FileInfo, MediaMessageEventContent, MessageType,
        )

        ciphertext, keys = encrypt_attachment(data)
        mxc = await self._appservice_client().upload_media(
            ciphertext, mime_type="application/octet-stream",
        )
        keys.url = mxc
        content = MediaMessageEventContent(
            msgtype=MessageType.FILE, body=filename, file=keys,
            info=FileInfo(mimetype=mime, size=len(data)),
        )
        if thread_root:
            content.set_thread_parent(thread_root)
        return await self._send_encrypted(room_id, content)

    def on_event(self, callback: Callable[[Any], Any]) -> None:
        """Deliver every room event the homeserver pushes to *callback*.

        Registered with the appservice rather than merely stored (!243 review):
        the earlier cut kept the callback in an attribute nothing read, so the
        inbound tests passed against the fake while the real bridge would never
        have delivered an event — the worst shape a bug can take.

        Encrypted events are decrypted first, because that is what the room is:
        an inbound reply arrives as ``m.room.encrypted`` and the routing above
        reads ``m.room.message``.
        """
        self._callback = callback
        appservice = self._appservice()

        async def handle(event) -> None:
            decrypted = await self._decrypt(event)
            if decrypted is not None:
                await callback(decrypted)

        appservice.matrix_event_handler(handle)

    # --- the parts that only the real transport has ---------------------------

    async def _decrypt(self, event) -> Optional[dict]:
        """One inbound event as a plain mapping, or ``None`` to ignore it.

        A mapping rather than the library's object, so
        :meth:`matrix_bridge.inbound.Message.from_event` stays testable without
        ``mautrix`` and a change of client cannot change what a threaded reply
        means.
        """
        from mautrix.errors import DecryptionError
        from mautrix.types import EventType

        try:
            if event.type == EventType.ROOM_ENCRYPTED:
                if self._crypto is None:
                    logger.warning(
                        "matrix_inbound_undecryptable — the crypto store is "
                        "not open",
                    )
                    return None
                event = await self._crypto.decrypt_megolm_event(event)
            elif event.type != EventType.ROOM_MESSAGE:
                return None
        except DecryptionError as exc:
            logger.warning("matrix_inbound_decryption_failed error=%s", exc)
            return None
        return event.serialize()

    async def _send_encrypted(self, room_id: str, content) -> str:
        """Encrypt and send, sharing the group session when there is not one.

        The retry is not defensive coding: ``encrypt_megolm_event`` raises
        ``EncryptionError`` precisely when the room has no current outbound
        session — a new room, a rotated session, a member who just joined — and
        sharing then retrying is the documented answer (``mautrix``' own
        bridge does exactly this).
        """
        from mautrix.errors import EncryptionError
        from mautrix.types import EventType

        client = self._appservice_client()
        if self._crypto is None or not await self.room_is_encrypted(room_id):
            # Never a fallback. An unencrypted send here is the leak the whole
            # design forbids, and it would happen silently at exactly the
            # moment something else had already gone wrong (!243 review).
            raise MatrixClientError(
                f"refusing to send into {room_id} unencrypted: "
                + ("the crypto store is not open" if self._crypto is None
                   else "the room is not encrypted")
            )

        try:
            encrypted = await self._crypto.encrypt_megolm_event(
                room_id, EventType.ROOM_MESSAGE, content,
            )
        except EncryptionError:
            members = await client.get_joined_members(room_id)
            await self._crypto.share_group_session(room_id, list(members))
            encrypted = await self._crypto.encrypt_megolm_event(
                room_id, EventType.ROOM_MESSAGE, content,
            )
        return await client.send_message_event(
            room_id, EventType.ROOM_ENCRYPTED, encrypted,
        )

    async def _ssss(self):
        """The SSSS machine and the key the recovery passphrase unlocks.

        ``mautrix`` implements no server-side room-key backup (spike.md), so
        this is D7's stated fallback: the same env value, protecting an export
        of the store, kept in the appservice user's own account data.

        **The key is provisioned here on first use** (!243 review). The earlier
        cut read ``m.secret_storage.default_key`` and would have died on a first
        start, because nothing ever wrote one. ``LMER_MATRIX_RECOVERY_KEY`` is
        used as the *passphrase*: the first start generates a key from it,
        uploads the metadata (salt and iterations, no secret) and makes it the
        default; every later start reads that metadata back and re-derives the
        same key from the same passphrase. So a wiped host with only the
        environment can restore — which is precisely G3's claim.
        """
        from mautrix.crypto.ssss import Machine

        machine = Machine(self._appservice_client())
        if self._ssss_key is not None:
            return machine, self._ssss_key

        key_id = await machine.get_default_key_id()
        if key_id:
            metadata = await machine.get_key_data(key_id)
            self._ssss_key = metadata.verify_passphrase(
                key_id, self._secrets.recovery_key,
            )
        else:
            logger.info("matrix_ssss_key_provisioned — first start")
            key = await machine.generate_and_upload_key(
                passphrase=self._secrets.recovery_key,
            )
            await machine.set_default_key_id(key.id)
            self._ssss_key = key
        return machine, self._ssss_key

    async def _read_backup(self) -> Optional[bytes]:
        from mautrix.errors import MNotFound

        machine, key = await self._ssss()
        try:
            return await machine.get_decrypted_account_data(BACKUP_EVENT_TYPE, key)
        except MNotFound:
            return None

    async def _restore(self, path: Path) -> None:
        blob = await self._read_backup()
        if blob is None:
            raise MatrixClientError(
                f"no store backup to restore: {ENV_RECOVERY_KEY} unlocked the "
                f"account data but there was nothing in it"
            )
        path.write_bytes(blob)

    async def _export_store(self) -> bytes:
        if self._database is None:
            raise MatrixClientError(
                "the crypto store is not open, so there is nothing to export"
            )
        return await export_store_bytes(
            self._database.execute, self._store_path(),
        )

    def _store_path(self) -> Path:
        return self.config.state_dir / CRYPTO_DIRNAME / STORE_FILENAME

    def _appservice(self):
        """The appservice itself — the transaction handlers hang off it."""
        from mautrix.appservice import AppService

        if self._as is None:
            self._as = AppService(
                server=self.config.homeserver,
                domain=self.config.server_name,
                as_token=self._secrets.as_token,
                hs_token=self._secrets.hs_token,
                bot_localpart=f"lmer-{self.config.name}",
                id=f"lmer-{self.config.name}",
            )
        return self._as

    def _appservice_client(self):
        """The sender's intent: every room call the bridge makes as itself."""
        return self._appservice().intent

def connect(config: MatrixConfig, secrets: Secrets) -> MatrixClient:
    """A client wired to the real homeserver. The only construction path."""
    return MatrixClient(config, MautrixHomeserver(config, secrets))
