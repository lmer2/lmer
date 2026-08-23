"""The crypto store's lifecycle (D7, G3): open, restore, or refuse to start.

The claim being pinned is narrow and worth stating exactly. A bridge that
cannot read its crypto store must **not** quietly mint a new device: it would
start, look healthy, encrypt to keys nobody in the room shares, and the symptom
would be a room that went quiet for no visible reason. The refusal is what turns
that into a message an operator can act on — which is why the test asserts the
refusal *names* ``LMER_MATRIX_RECOVERY_KEY`` and the store path.

The second claim is the cold start: a host wiped down to its environment, with
the recovery key still exported, restores rather than starts fresh.

What this file does not do is exercise ``mautrix``' own store. The spike settled
that the library implements no server-side key backup, so what ships is D7's
stated fallback — an export of the store, encrypted with an SSSS key derived
from the same env var, in the appservice user's account data. That transport is
:class:`~matrix_bridge.client.MautrixHomeserver`'s, and T9 is where it meets a
real homeserver. Everything that *decides* what happens to the store is here.
"""

import os

import pytest

from lmer_platform import store
from matrix_bridge import client as mxclient
from matrix_bridge import config as mxcfg
from tests.conftest import strip_lmer_env
from tests.matrix_fakes import FakeHomeserver

STORED = {
    "name": "bridge-a",
    "homeserver": "https://matrix.example.net",
    "room_id": "!room:matrix.example.net",
    "allow": {"@alice:matrix.example.net": ["read", "answer-live"]},
}


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


@pytest.fixture
def config(platform_root):
    return mxcfg.load(STORED)


@pytest.fixture
def homeserver():
    return FakeHomeserver(room_id=STORED["room_id"])


@pytest.fixture
def client(config, homeserver):
    return mxclient.MatrixClient(config, homeserver, record_room_id=lambda _: None)


def write_store(client, content=b"a crypto store"):
    client.store_path.parent.mkdir(parents=True, exist_ok=True)
    client.store_path.write_bytes(content)
    return client.store_path


def unreadable_store(client):
    """A store that is **present and unreadable to every uid, root included**.

    Not ``chmod 000``: root bypasses file modes, and CI runs as root in the
    ``python:3.12`` image — so the mode-based version of these tests passed
    locally and quietly stopped proving anything in the pipeline, which is
    worse than a red one (pipeline 1743). A directory where the store belongs
    exists, is not a readable file, and makes :func:`inspect_store`'s
    ``path.open`` raise ``IsADirectoryError`` — the same ``OSError`` branch a
    permission failure takes, for every user.

    It is also a real shape: a half-restored backup or a bind-mount pointed one
    level too high leaves exactly this on disk.
    """
    client.store_path.parent.mkdir(parents=True, exist_ok=True)
    client.store_path.mkdir()
    return client.store_path


# --- the plan, as a rule -----------------------------------------------------

@pytest.mark.parametrize("present, readable, backup, expected", [
    (True, True, True, mxclient.STORE_OPEN),
    (True, True, False, mxclient.STORE_OPEN),
    (True, False, True, mxclient.STORE_RESTORE),
    (True, False, False, mxclient.STORE_REFUSE),
    (False, False, True, mxclient.STORE_RESTORE),
    (False, False, False, mxclient.STORE_FRESH),
])
def test_the_store_plan_covers_every_state_of_the_disk(
    present, readable, backup, expected,
):
    assert mxclient.store_plan(
        present=present, readable=readable, backup_present=backup,
    ) == expected


def test_a_first_start_mints_a_device_and_a_broken_one_does_not():
    """The two rows worth arguing about, side by side: absent-with-no-backup is
    a first run and there is nothing else it could be; present-but-unreadable
    with no backup is a broken deployment, and starting hides it."""
    assert mxclient.store_plan(
        present=False, readable=False, backup_present=False,
    ) == mxclient.STORE_FRESH
    assert mxclient.store_plan(
        present=True, readable=False, backup_present=False,
    ) == mxclient.STORE_REFUSE


# --- inspecting the disk -----------------------------------------------------

def test_a_missing_store_reads_as_absent(client):
    status = mxclient.inspect_store(client.store_path)
    assert (status.present, status.readable) == (False, False)


def test_a_present_store_reads_as_readable(client):
    path = write_store(client)
    status = mxclient.inspect_store(path)
    assert (status.present, status.readable) == (True, True)


def test_a_store_that_cannot_be_opened_reads_as_unreadable(client):
    """Any ``OSError`` on the read, not just a permission one."""
    path = unreadable_store(client)
    status = mxclient.inspect_store(path)
    assert (status.present, status.readable) == (True, False)


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason=(
        "root bypasses file modes, so chmod 000 leaves the file readable — the "
        "permission case cannot be produced as root. CI runs as root in the "
        "python:3.12 image; the uid-independent case above covers the same "
        "branch everywhere."
    ),
)
def test_a_store_this_process_lacks_permission_for_reads_as_unreadable(client):
    path = write_store(client)
    path.chmod(0o000)
    try:
        status = mxclient.inspect_store(path)
        assert (status.present, status.readable) == (True, False)
    finally:
        path.chmod(0o600)


# --- what start() does with each ---------------------------------------------

async def test_a_persisted_store_is_opened_as_it_stands(client, homeserver):
    """The ordinary restart: no restore, and no new device."""
    write_store(client)
    assert await client.open_store() == mxclient.STORE_OPEN
    assert homeserver.opened == [{"path": client.store_path, "restore": False}]


async def test_crypto_store_survives_restart(client, homeserver):
    """G3's named test at this layer: the second start reuses the first's
    store rather than replacing it."""
    write_store(client)
    await client.open_store()
    second = mxclient.MatrixClient(
        client.config, homeserver, record_room_id=lambda _: None,
    )
    assert await second.open_store() == mxclient.STORE_OPEN
    assert client.store_path.read_bytes() == b"a crypto store"


async def test_a_cold_start_with_only_the_env_restores_from_backup(
    client, homeserver,
):
    """The wiped host: no store on disk, but the recovery key is still
    exported and the backup is where it left it."""
    homeserver.backup = b"an encrypted export"
    assert await client.open_store() == mxclient.STORE_RESTORE
    assert homeserver.opened == [{"path": client.store_path, "restore": True}]


async def test_a_first_start_creates_the_store_and_backs_it_up(client, homeserver):
    """A fresh device is correct exactly once, and the backup written here is
    what makes the *next* wiped host restorable."""
    assert await client.open_store() == mxclient.STORE_FRESH
    assert homeserver.opened == [{"path": client.store_path, "restore": False}]
    assert homeserver.backups_written == 1


async def test_an_unreadable_store_with_no_backup_refuses_to_start(
    client, homeserver,
):
    """The refusal G3 asks for, and the reason it exists: the alternative is a
    bridge that looks healthy while the room goes quiet."""
    unreadable_store(client)
    with pytest.raises(mxclient.MatrixClientError) as excinfo:
        await client.open_store()

    message = str(excinfo.value)
    assert mxcfg.ENV_RECOVERY_KEY in message
    assert str(client.store_path) in message
    assert homeserver.opened == [], "nothing was opened, so no device was minted"


async def test_a_restore_that_fails_refuses_rather_than_starting_fresh(
    client, homeserver,
):
    """The other half of the same rule: the backup existed, unlocking it did
    not work, and a fresh device would hide that."""
    homeserver.backup = b"an encrypted export"
    homeserver.open_store_error = RuntimeError("recovery key does not decrypt it")
    with pytest.raises(mxclient.MatrixClientError) as excinfo:
        await client.open_store()
    assert mxcfg.ENV_RECOVERY_KEY in str(excinfo.value)


async def test_the_refusal_never_quotes_the_recovery_key(client, homeserver,
                                                         monkeypatch):
    """It names the variable. Naming the value would put a live key into the
    log the operator pastes into a ticket."""
    monkeypatch.setenv(mxcfg.ENV_RECOVERY_KEY, "EsTx-recovery-key-value")
    unreadable_store(client)
    with pytest.raises(mxclient.MatrixClientError) as excinfo:
        await client.open_store()
    assert "EsTx-recovery-key-value" not in str(excinfo.value)


async def test_a_backup_that_cannot_be_read_refuses_rather_than_starting_fresh(
    client, homeserver,
):
    """!243 review: "cannot tell" resolved to "no backup", and for an absent
    store that is a *destructive* answer — mint a device, then overwrite the
    export nobody could read. A homeserver having a bad minute would have cost
    the bridge its restorable history."""
    homeserver.backup_error = RuntimeError("the homeserver answered 500")
    with pytest.raises(mxclient.MatrixClientError) as excinfo:
        await client.open_store()
    assert "cannot tell" in str(excinfo.value)
    assert homeserver.opened == []
    assert homeserver.backups_written == 0


async def test_every_successful_start_refreshes_the_export(client, homeserver):
    """Named for what it now checks (!243 review). The previous version set
    ``homeserver.backup``, which makes the plan a *restore* rather than a first
    start, and then asserted that a write happened — the opposite of the
    no-overwrite guard it was named for. That guard is gone, for the reason the
    reviewer gave: it made a short first export permanent, so a restore from it
    would produce a store that decrypts nothing.

    The contract now is simply that a store this process opened successfully is
    exported, whichever plan got it there.
    """
    for plan, setup in (
        (mxclient.STORE_OPEN, lambda: write_store(client)),
        (mxclient.STORE_FRESH, lambda: None),
    ):
        homeserver.sent.clear()
        homeserver.backups_written = 0
        homeserver.backup = None
        if client.store_path.exists():
            client.store_path.unlink()
        setup()
        assert await client.open_store() == plan
        assert homeserver.backups_written == 1, plan
        assert homeserver.backup, "the export is kept, not just counted"


async def test_a_restore_writes_the_export_back_too(client, homeserver):
    homeserver.backup = b"an encrypted export"
    assert await client.open_store() == mxclient.STORE_RESTORE
    assert homeserver.backups_written == 1


async def test_start_opens_the_store_before_it_touches_the_room(
    client, homeserver,
):
    """Order matters: joining an encrypted room without a store would leave the
    bridge unable to read the messages it is joining for."""
    unreadable_store(client)
    with pytest.raises(mxclient.MatrixClientError):
        await client.start()
    assert homeserver.joined == []


async def test_start_opens_the_store_and_then_the_room(client, homeserver):
    write_store(client)
    await client.start()
    assert homeserver.opened
    assert homeserver.joined == [STORED["room_id"]]


# --- the export itself -------------------------------------------------------

async def test_the_export_folds_in_the_write_ahead_log(tmp_path):
    """!243 review: ``mautrix`` opens the crypto store in WAL mode, so a
    ``read_bytes()`` of ``store.db`` can be missing the very rows the backup
    exists to preserve — the device identity is written moments before the
    backup runs, and nothing has checkpointed it.

    Driven against stdlib ``sqlite3`` rather than the real store, so it proves
    the behaviour on every host and needs neither ``mautrix`` nor a homeserver.
    The first assertion is the bug: without a checkpoint the row is simply not
    in the file.
    """
    import sqlite3

    path = tmp_path / "store.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE crypto_account(device_id TEXT)")
    connection.execute("INSERT INTO crypto_account VALUES ('DEVICEIDENTITY')")
    connection.commit()

    assert b"DEVICEIDENTITY" not in path.read_bytes(), (
        "the premise: WAL keeps the newest rows out of the main file"
    )

    async def execute(sql):
        connection.execute(sql)

    exported = await mxclient.export_store_bytes(execute, path)
    assert b"DEVICEIDENTITY" in exported


async def test_an_empty_export_never_replaces_a_backup(client, homeserver):
    """The one way this call can make things worse. An empty export means the
    store is wrong, not that the backup should be thrown away."""
    homeserver.backup = b"a good export"
    homeserver.export = b""
    await client.open_store()
    assert homeserver.backup == b"a good export"
    assert homeserver.backups_written == 0, "nothing was written at all"
