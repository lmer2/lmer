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
exercised against a real homeserver in T9; everything above it is exercised now.

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

import asyncio
import json
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


class CryptoUnavailable(MatrixClientError):
    """The crypto store is not open, so nothing can be encrypted or decrypted."""


class SessionMissing(RuntimeError):
    """This device holds no room key for the event yet."""


class Undecryptable(RuntimeError):
    """The event or file will not decrypt, now or later."""


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

    async def open_database(self, path: Path):
        """The store's database, its crypto store and its state store.

        Split out of :meth:`open_store` so it can be exercised without a
        homeserver — everything after it in that method is network (device
        creation, key sharing). The first real deployment failed *here* while
        looking healthy everywhere else, so this is the part that needs a test
        driving the real thing rather than a fake.

        **Two upgrade tables, on one database.** ``Database.start()`` runs only
        the one it was created with — the crypto tables. The state store under
        ``PgCryptoStateStore`` is the *client* state store, whose
        ``mx_room_state`` / ``mx_user_profile`` come from a different table, so
        without the second call nothing creates them: every send then fails in
        member lookup with ``no such table: mx_room_state``, surfacing as
        ``EncryptionError: No group session created``. They coexist on one file
        because their version tables differ (``crypto_version``, ``mx_version``).
        """
        # deferred: optional dependency — mautrix ships in the `matrix` extra
        from mautrix.client.state_store.asyncpg import upgrade as client_upgrade
        from mautrix.crypto import PgCryptoStore
        from mautrix.util.async_db import Database

        database = Database.create(
            f"sqlite:///{path}", upgrade_table=PgCryptoStore.upgrade_table,
        )
        await database.start()
        await client_upgrade.upgrade_table.upgrade(database)

        store = PgCryptoStore("", _PICKLE_KEY, database)
        await store.open()
        state_store = _state_store_class()(database)
        self._state_store = state_store
        return database, store, state_store

    def _adopt_state_store(self, state_store) -> None:
        """Point an already-built appservice at the real store — *and its handler*.

        Reassigning ``az.state_store`` is not enough (!246 review): the
        constructor registers ``self.state_store.update_state`` as an event
        handler (``appservice.py:130``), and that is a **bound method of the
        object it was constructed with**. Leave it and inbound membership
        updates keep landing in the placeholder store while the crypto machine
        reads the database one — the two disagreeing about who is in the room,
        which is exactly what decides whether a group session is shared with
        the right devices.

        So the old handler is dropped and the new store's registered in its
        place.
        """
        if self._as is None:
            return
        previous = self._as.state_store
        self._as.state_store = state_store
        self._as.event_handlers = [
            handler for handler in self._as.event_handlers
            if getattr(handler, "__self__", None) is not previous
        ]
        self._as.matrix_event_handler(state_store.update_state)

    async def _reset_outbound_client(self) -> None:
        """Drop the outbound client so the next call rebuilds it — closing its
        session first.

        Dropping the reference alone orphans the ``aiohttp`` session it was
        built with (!246 review): a leak introduced by the commit that fixed a
        different leak. The session is ours precisely so it can be closed.
        """
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._client = None

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

    async def whoami(self) -> str:
        """The MXID the homeserver says this appservice is.

        The single most likely cause of a quiet room — a registration that was
        never installed, or an ``as_token`` that does not match — and one
        authenticated read with no side effects (!245 review).
        """
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

    # --- the crypto lent to other processes (matrix_bridge.crypto_api) -------

    async def crypto_identity(self) -> dict:
        """``{"user_id", "device_id"}`` of the device; raises :class:`CryptoUnavailable`."""
        raise NotImplementedError

    async def encrypt_content(self, room_id: str, event_type: str, content: dict) -> dict:
        """Megolm content for *room_id*; raises rather than return cleartext."""
        raise NotImplementedError

    async def decrypt_event(self, event: dict) -> dict:
        """The plaintext event; raises :class:`SessionMissing` or :class:`Undecryptable`."""
        raise NotImplementedError

    async def encrypt_attachment(self, data: bytes) -> tuple:
        """``(ciphertext, file)`` — *file* is the event's ``file`` object without ``url``."""
        raise NotImplementedError

    async def decrypt_attachment(self, data: bytes, file: dict) -> bytes:
        """The plaintext; raises :class:`Undecryptable`."""
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release anything this transport opened. Safe to call twice."""

    async def listen(self, host: str, port: int) -> None:
        """Start the appservice's HTTP server and return.

        The homeserver **pushes** to an appservice; without this there is no
        inbound path at all, and `run` would sit in its poll loop looking
        healthy while every reply in the room went nowhere (!245 review).

        Separate from :meth:`serve_forever` because of *when* it has to happen:
        ``mautrix``' ``AppService.intent`` raises ``AttributeError`` until
        ``start()`` has run, so every outbound call — including the first one
        ``MatrixClient.start`` makes — is downstream of this. A listener started
        inside the same ``gather`` as the poll loop is started too late to be
        reached at all (!245 review, iteration 2).
        """
        raise NotImplementedError

    async def serve_forever(self, stop: Optional[asyncio.Event] = None) -> None:
        """Keep the listener up until *stop* is set (or cancelled), then shut it
        down. The transport itself stays open: closing it is :meth:`aclose`,
        which the caller runs after **both** halves of the bridge have drained
        — an in-flight outbound send still needs the session this listener
        never owned (issue #349)."""
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

    async def listen(self, host: str, port: int) -> None:
        """Start the inbound listener. Runs **before** :meth:`start`.

        Order is the whole point (!245 review, iteration 2): the transport's
        outbound client used to come from the appservice, which does not exist
        until its web server is up — so a listener started after the first
        homeserver call could never be reached. The transport no longer couples
        the two, and this method keeps the order explicit at the call site
        rather than implicit in a ``gather``.
        """
        await self.homeserver.listen(host, port)

    async def serve_forever(self, stop: Optional[asyncio.Event] = None) -> None:
        """Keep the listener up until *stop* is set (or cancelled)."""
        await self.homeserver.serve_forever(stop)

    async def aclose(self) -> None:
        """Release what the transport opened."""
        await self.homeserver.aclose()

    async def start(self, *, back_up: bool = True) -> None:
        """Open the crypto store, then the room. Refuses rather than degrades.

        ``back_up=False`` (``run --no-backup``) is the one deliberate
        degradation: the store and the room are opened as usual, but the export
        is not refreshed, and the start says so at WARNING. Every other
        precondition — a store that cannot be read and has no backup, a backup
        whose existence cannot be determined, a room that is not encrypted —
        still refuses.
        """
        await self.open_store(back_up=back_up)
        await self.ensure_room()

    async def open_store(self, *, back_up: bool = True) -> str:
        """Apply :func:`store_plan` to what is on disk. Returns the plan taken.

        ``back_up=False`` skips the export refresh that follows a successful
        open, and logs at WARNING that it did — see :meth:`start`.
        """
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
        #
        # The refresh is **fatal by default** (MR !438 review, operator ruling).
        # A bridge that starts without a fresh export is a bridge running on a
        # backup it can no longer keep up to date — silently, since the only
        # trace is one line in the journal. On ``fresh`` there is not even an
        # earlier export to fall back on. ``--no-backup`` is the explicit way
        # to run without it, for a deployment whose export cannot be uploaded
        # (a 33 MB store's export gets a Cloudflare 502, #634) and whose
        # operator would rather have the service up.
        if not back_up:
            # With no export on the homeserver there is nothing to stay as it
            # was: a host lost from here finds no store and no backup, and
            # mints another device. Keyed on ``backup_present``, not ``plan``
            # (MR !438 review): the start after a ``--no-backup`` first start
            # is ``open`` with no backup, not ``fresh``.
            if not backup_present:
                consequence = (
                    "there is no backup on the homeserver. A host lost from "
                    "here cannot be restored."
                )
            else:
                consequence = (
                    "the backup on the homeserver stays as it was. A host "
                    "lost from here restores only as far as that backup."
                )
            logger.warning(
                "matrix_backup_disabled — --no-backup: this start does not "
                "refresh the store export at %s, so %s",
                status.path, consequence,
            )
            return plan
        try:
            await self.homeserver.back_up_store()
        except Exception as exc:
            raise MatrixClientError(
                f"the crypto store at {status.path} opened, but its export "
                f"could not be uploaded ({exc}). Refusing to start rather than "
                f"run on a backup that is no longer refreshed — the bridge "
                f"would look healthy while the export it restores from went "
                f"stale. Fix the upload, or start with --no-backup to run "
                f"without a fresh export."
            ) from exc
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
    """Persist the room id without overwriting concurrent Matrix settings."""
    def compute(current: dict) -> dict:
        matrix = dict(current.get("matrix") or {})
        matrix["room_id"] = room_id
        return {"matrix": matrix}

    platform_config.mutate_stored(compute)


def _state_store_class():
    """One store serving both roles the bridge needs, over one database.

    ``AppService`` wants an ``ASStateStore`` (membership, power levels) and
    ``OlmMachine`` wants a crypto ``StateStore`` (is this room encrypted, which
    rooms are shared). Both are backed by the *same* ``mx_room_state`` /
    ``mx_user_profile`` tables, so one object can be both — which is what stops
    the appservice falling back to a ``FileStateStore`` at a relative path,
    unwritable in the image's working directory (found on the first real
    deployment).

    Built here rather than at module scope because the base classes come from
    ``mautrix``, which this module never imports at import time.
    """
    # deferred: optional dependency — mautrix ships in the `matrix` extra
    from mautrix.appservice.state_store.asyncpg import PgASStateStore
    from mautrix.crypto import PgCryptoStateStore

    class BridgeStateStore(PgASStateStore, PgCryptoStateStore):
        pass

    return BridgeStateStore


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
        self._client = None
        self._session = None
        self._state_store = None
        self._listening = False
        self._crypto = None
        self._crypto_client = None
        self._database = None
        self._store = None
        self._ssss_key = None
        self._share_locks: dict = {}
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
        # deferred: optional dependency — mautrix ships in the `matrix` extra
        from mautrix.client import Client
        from mautrix.crypto import OlmMachine
        from mautrix.types import TrustState

        path.parent.mkdir(parents=True, exist_ok=True)
        if restore:
            await self._restore(path)

        database, store, state_store = await self.open_database(path)
        # Order-independence, deliberately: whichever of `listen()` and
        # `open_store()` ran first, both the appservice and the outbound client
        # must end up on *this* store rather than on the placeholder one.
        self._adopt_state_store(state_store)
        await self._reset_outbound_client()

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
        # deferred: optional dependency — mautrix ships in the `matrix` extra
        from mautrix.api import Method, Path as ApiPath
        from mautrix.crypto.ssss.types import EncryptedAccountDataEventContent

        machine, key = await self._ssss()
        # What `machine.set_encrypted_account_data` does, but the body goes to
        # the homeserver as **bytes**. Given a dict, `HTTPAPI.request` dumps it
        # to a `str`, and past its 25 MB `min_iter_size` hands that `str` to
        # `async_iter_bytes`, whose `memoryview()` takes bytes only — so any
        # store big enough for that threshold could never be backed up
        # (mautrix 0.21.1). Bytes are sent as-is, chunked or not.
        content = EncryptedAccountDataEventContent(
            encrypted={key.id: key.encrypt(BACKUP_EVENT_TYPE, blob)},
        )
        client = machine.client
        await client.api.request(
            Method.PUT,
            ApiPath.v3.user[client.mxid].account_data[BACKUP_EVENT_TYPE],
            json.dumps(content.serialize()).encode(),
        )

    async def create_room(self, *, invite: Sequence[str]) -> str:
        # deferred: optional dependency — mautrix ships in the `matrix` extra
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
        # deferred: optional dependency — mautrix ships in the `matrix` extra
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

    async def whoami(self) -> str:
        response = await self._appservice_client().api.request(
            "GET", "/_matrix/client/v3/account/whoami",
        )
        return (response or {}).get("user_id") or ""

    async def send_text(
        self, room_id: str, text: str, *, thread_root: Optional[str] = None,
    ) -> str:
        # deferred: optional dependency — mautrix ships in the `matrix` extra
        from mautrix.types import Format, MessageType, TextMessageEventContent

        content = TextMessageEventContent(msgtype=MessageType.TEXT, body=text)
        if thread_root:
            content.set_thread_parent(thread_root)
        return await self._send_encrypted(room_id, content)

    async def send_attachment(
        self, room_id: str, data: bytes, *, mime: str, filename: str,
        thread_root: Optional[str] = None,
    ) -> str:
        # deferred: optional dependency — mautrix ships in the `matrix` extra
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

    async def listen(self, host: str, port: int) -> None:
        # deferred: cycle — crypto_api imports this module
        from matrix_bridge import crypto_api

        appservice = self._appservice()
        # Before start(): aiohttp freezes the router when the app starts.
        crypto_api.register(appservice.app, self, token=self._secrets.as_token)
        await appservice.start(host, port)
        self._listening = True
        logger.info("matrix_appservice_listening host=%s port=%s", host, port)

    async def serve_forever(self, stop: Optional[asyncio.Event] = None) -> None:
        # Only the listener stops here. The session and the database outlive
        # this coroutine deliberately (issue #349): a stop request first
        # silences inbound — the homeserver redelivers unacknowledged
        # transactions, so refusing connections loses nothing — while an
        # in-flight outbound tick may still need both to finish its send.
        # ``serve()`` calls :meth:`aclose` once that drain is done.
        try:
            await (stop if stop is not None else asyncio.Event()).wait()
        finally:
            if self._listening:
                await self._appservice().stop()

    async def aclose(self) -> None:
        """Release what this transport opened: the HTTP session, the database.

        Called by ``serve()`` after both halves have drained, and by the CLI's
        short-lived verbs. Without it every ``check`` exited with aiohttp's
        "Unclosed client session" warning — harmless, and exactly the kind of
        noise that trains an operator to ignore a diagnostic's output.
        """
        await self._reset_outbound_client()
        if self._database is not None:
            await self._database.stop()
            self._database = None

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
        # deferred: optional dependency — mautrix ships in the `matrix` extra
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
        ``EncryptionError`` precisely when the store holds no outbound session
        for the room — the first send into it, since nothing here discards one
        (``handle_member_event`` is not wired, #395) — and sharing then
        retrying is the documented answer (``mautrix``' own bridge does exactly
        this).
        """
        # deferred: optional dependency — mautrix ships in the `matrix` extra
        from mautrix.types import EventType

        encrypted = await self._encrypt(room_id, EventType.ROOM_MESSAGE, content)
        return await self._appservice_client().send_message_event(
            room_id, EventType.ROOM_ENCRYPTED, encrypted,
        )

    async def _encrypt(self, room_id: str, event_type, content):
        # deferred: optional dependency — mautrix ships in the `matrix` extra
        from mautrix.errors import EncryptionError
        from mautrix.types import Membership

        if self._crypto is None:
            raise CryptoUnavailable(
                f"refusing to send into {room_id} unencrypted: the crypto store is not open"
            )
        if not await self.room_is_encrypted(room_id):
            # Never a fallback. An unencrypted send here is the leak the whole
            # design forbids, and it would happen silently at exactly the
            # moment something else had already gone wrong (!243 review).
            raise MatrixClientError(
                f"refusing to send into {room_id} unencrypted: the room is not encrypted"
            )
        try:
            return await self._crypto.encrypt_megolm_event(room_id, event_type, content)
        except EncryptionError:
            pass
        # mautrix refuses a second concurrent share for a room, and the crypto
        # API serves several connectors at once.
        async with self._share_locks.setdefault(room_id, asyncio.Lock()):
            try:
                return await self._crypto.encrypt_megolm_event(room_id, event_type, content)
            except EncryptionError:
                # Invited too, as mautrix's own bridge does: a DM goes out
                # before its recipient has accepted.
                members = await self._appservice_client().get_members(room_id)
                await self._crypto.share_group_session(room_id, [
                    member.state_key for member in members
                    if member.content.membership in (Membership.JOIN, Membership.INVITE)
                ])
            return await self._crypto.encrypt_megolm_event(room_id, event_type, content)

    # --- matrix_bridge.crypto_api ---------------------------------------------

    async def crypto_identity(self) -> dict:
        if self._crypto is None:
            raise CryptoUnavailable("the bridge's crypto store is not open yet")
        return {"user_id": self.config.sender, "device_id": self._crypto_client.device_id}

    async def encrypt_content(self, room_id: str, event_type: str, content: dict) -> dict:
        # deferred: optional dependency — mautrix ships in the `matrix` extra
        from mautrix.types import EventType

        kind = EventType.find(event_type, t_class=EventType.Class.MESSAGE)
        encrypted = await self._encrypt(room_id, kind, content)
        return encrypted.serialize()

    async def decrypt_event(self, event: dict) -> dict:
        # deferred: optional dependency — mautrix ships in the `matrix` extra
        from mautrix.errors import SessionNotFound
        from mautrix.types import EncryptedEvent

        if self._crypto is None:
            raise CryptoUnavailable("the bridge's crypto store is not open yet")
        try:
            encrypted = EncryptedEvent.deserialize(event)
        except Exception as exc:
            raise Undecryptable(f"not an encrypted event: {exc}") from None
        try:
            plain = (await self._crypto.decrypt_megolm_event(encrypted)).serialize()
        except SessionNotFound:
            raise SessionMissing(encrypted.content.session_id) from None
        except Exception as exc:
            # The class only: a payload that fails to parse is quoted in the
            # message, and that is plaintext a sender chose.
            raise Undecryptable(f"the event did not decrypt ({exc.__class__.__name__})") from None
        # Only the event's own fields: the library adds a trust annotation
        # that is neither JSON nor anyone else's business.
        return {key: plain[key] for key in (
            "room_id", "event_id", "sender", "origin_server_ts", "type", "content", "unsigned",
        ) if key in plain}

    async def encrypt_attachment(self, data: bytes) -> tuple:
        # deferred: optional dependency — mautrix ships in the `matrix` extra
        from mautrix.crypto.attachments import encrypt_attachment

        ciphertext, keys = encrypt_attachment(data)
        file = keys.serialize()
        file.pop("url", None)
        return ciphertext, file

    async def decrypt_attachment(self, data: bytes, file: dict) -> bytes:
        # deferred: optional dependency — mautrix ships in the `matrix` extra
        from mautrix.crypto.attachments import decrypt_attachment
        from mautrix.errors import DecryptionError
        from mautrix.types import EncryptedFile

        try:
            keys = EncryptedFile.deserialize(file)
            return decrypt_attachment(data, keys.key.key, keys.hashes["sha256"], keys.iv)
        except DecryptionError as exc:
            raise Undecryptable(str(exc)) from None
        except Exception as exc:
            raise Undecryptable(f"not an encrypted file: {exc}") from None

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
        # deferred: optional dependency — mautrix ships in the `matrix` extra
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
        # deferred: optional dependency — mautrix ships in the `matrix` extra
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
        # deferred: optional dependency — mautrix ships in the `matrix` extra
        from mautrix.appservice import AppService

        if self._as is None:
            self._as = AppService(
                state_store=self._appservice_state_store(),
                server=self.config.homeserver,
                domain=self.config.server_name,
                as_token=self._secrets.as_token,
                hs_token=self._secrets.hs_token,
                bot_localpart=f"lmer-{self.config.name}",
                id=f"lmer-{self.config.name}",
                # Both default to False, and both are the *service* side of an
                # opt-in whose other half is in the registration and the
                # homeserver's config (!245 review). Without `ephemeral_events`
                # mautrix drops the to-device messages MSC2409 delivers — the
                # room keys — and without `encryption_events` it drops the OTK
                # counts and device lists MSC3202 puts on each transaction, so
                # the three handlers wired in `open_store` would never be
                # called. Three places have to agree or none of it works.
                ephemeral_events=True,
                encryption_events=True,
            )
        return self._as

    def _appservice_state_store(self):
        """The shared store once the crypto store is open; memory before it.

        Never mautrix's default, which is a ``FileStateStore`` at the *relative*
        path ``mx-state.json`` — unwritable in the image's working directory,
        and wrong even where it is writable, since it would be a second copy of
        state the database already holds. An in-memory store is the honest
        placeholder for the one verb that runs without a database
        (``check``, which must not create one).
        """
        # deferred: optional dependency — mautrix ships in the `matrix` extra
        from mautrix.appservice.state_store import ASStateStore
        from mautrix.client.state_store import MemoryStateStore

        if self._state_store is not None:
            return self._state_store

        # Composed rather than imported: `ASStateStore` is abstract and mautrix
        # ships no in-memory concrete pairing of the two — `FileASStateStore` is
        # the file-backed one this exists to avoid.
        class MemoryASStateStore(MemoryStateStore, ASStateStore):
            pass

        return MemoryASStateStore()

    def _appservice_client(self):
        """The sender's intent: every room call the bridge makes as itself.

        Built here rather than taken from ``AppService.intent``, which raises
        ``AttributeError("the intent attribute can only be used after
        starting")`` until the web server is up. That coupling is what made the
        listener fix unreachable — ``MatrixClient.start`` calls the homeserver
        before any listener could exist — and it would have made
        ``lmer-matrix-bridge check`` impossible to write at all, since a
        diagnostic must not bind the port a running bridge is already on.

        This is the same object ``AppService.start`` constructs
        (``AppServiceAPI(...).bot_intent()``, appservice.py:163-171), with the
        same token and state store; the appservice is left to do what only it
        can, which is receive.
        """
        # deferred: import cost — aiohttp adds ~140 ms
        import aiohttp
        # deferred: optional dependency — mautrix ships in the `matrix` extra
        from mautrix.appservice import AppServiceAPI

        if self._client is None:
            appservice = self._appservice()
            self._session = aiohttp.ClientSession()
            self._client = AppServiceAPI(
                base_url=self.config.homeserver,
                bot_mxid=self.config.sender,
                token=self._secrets.as_token,
                # Ours, so it can be closed. Left to `AppServiceAPI` it makes
                # its own and nothing ever closes it, which `check` reported as
                # an "Unclosed client session" warning on every exit.
                client_session=self._session,
                # Not optional despite the signature's default: the constructor
                # calls `log.getChild(...)` unconditionally. Passing the
                # appservice's own logger also keeps both clients' output under
                # one name.
                log=appservice.log,
                state_store=appservice.state_store,
                bridge_name=appservice.bridge_name,
            ).bot_intent()
        return self._client

def connect(config: MatrixConfig, secrets: Secrets) -> MatrixClient:
    """A client wired to the real homeserver. The only construction path."""
    return MatrixClient(config, MautrixHomeserver(config, secrets))
