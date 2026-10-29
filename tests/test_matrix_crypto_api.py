"""The bridge lends its device; the connector's client borrows it (issue #478).

Both ends are real here: :mod:`matrix_bridge.crypto_api` serves on a loopback
port and :class:`matrix_chat.crypto.BridgeCrypto` / :class:`MatrixClient` call it
over HTTP, so a header, prefix or status the two sides disagree on fails here.
Only the crypto itself is fake (``FakeHomeserver``'s literal Megolm); the
real library's attachment ciphers are pinned at the bottom where mautrix is
installed.
"""
from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from urllib.parse import quote, unquote

import pytest
from aiohttp import web

from lmer_cli import thread_access, thread_posts
from lmer_platform import store
from matrix_bridge import client as mxclient
from matrix_bridge import config as mxcfg
from matrix_bridge import crypto_api
from lmer_cli.connector_ownership import ThreadKey
from lmer_cli.connector_recovery import Recovery
from matrix_bridge.client import CryptoUnavailable, MatrixClientError, Undecryptable
from matrix_chat import crypto as client_crypto
from matrix_chat.client import MatrixAccount, MatrixClient, MatrixError
from matrix_chat.crypto import BridgeCrypto, CryptoError
from matrix_chat.permalink import matrix_to_link
from tests.conftest import strip_lmer_env
from tests.matrix_fakes import FakeHomeserver

AS_TOKEN = "as-token-for-tests"
BRIDGE_USER = "@lmer-xo:example.org"
ROOM = "!secret:example.org"
PLAIN_ROOM = "!plain:example.org"
ROOT = "$root"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


class _Bridge:
    """The crypto routes on a real loopback listener, in a thread of their own."""

    def __init__(self, homeserver):
        self.homeserver = homeserver
        self.loop = asyncio.new_event_loop()
        self._started = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._started.wait(10), "the crypto API did not start"

    def _run(self):
        asyncio.set_event_loop(self.loop)
        app = web.Application()
        crypto_api.register(app, self.homeserver, token=AS_TOKEN)
        self.runner = web.AppRunner(app)
        self.loop.run_until_complete(self.runner.setup())
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        self.loop.run_until_complete(site.start())
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        self._started.set()
        self.loop.run_forever()

    def close(self):
        asyncio.run_coroutine_threadsafe(self.runner.cleanup(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(10)


@pytest.fixture
def homeserver():
    return FakeHomeserver(whoami_as=BRIDGE_USER)


@pytest.fixture
def bridge(homeserver):
    running = _Bridge(homeserver)
    yield running
    running.close()


class _Reply:
    def __init__(self, status, body=b"", headers=None):
        self.status_code = status
        self.content = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.headers = headers or {}
        self.text = self.content.decode(errors="replace")

    def json(self):
        return json.loads(self.content)

    def iter_content(self, chunk_size):
        yield self.content

    def close(self):
        pass


class _Homeserver:
    """Just enough client-server API: room encryption state, sends, media."""

    def __init__(self):
        self.calls: list = []
        self.sends: list = []
        self.media: dict = {}
        #: event id → event, for reads; the thread's replies in order.
        self.events: dict = {}
        self.thread: list = []
        self.upload_params: list = []
        #: A room that turned encryption on after the client first asked.
        self.now_encrypted = "!none:example.org"
        #: A status for the encryption-state read, to make the probe fail.
        self.state_error = None

    def request(self, method, url, *, json=None, data=None, params=None, **_kwargs):
        path = url.split("://", 1)[1].split("/", 1)[1]
        self.calls.append((method, "/" + path))
        if path.endswith("/state/m.room.encryption"):
            if self.state_error is not None:
                return _Reply(self.state_error, {"errcode": "M_UNKNOWN"})
            return _Reply(200, {"algorithm": "m.megolm.v1.aes-sha2"}) if quote(ROOM, safe="") in path \
                or quote(self.now_encrypted, safe="") in path else _Reply(404, {"errcode": "M_NOT_FOUND"})
        if "/send/" in path:
            event_type = path.split("/send/", 1)[1].split("/", 1)[0]
            self.sends.append((event_type, json))
            event_id = f"$sent{len(self.sends)}"
            self.events[event_id] = {"event_id": event_id, "sender": BRIDGE_USER, "type": event_type,
                                     "origin_server_ts": 1700000000000 + len(self.sends), "content": json}
            return _Reply(200, {"event_id": event_id})
        if path.startswith("_matrix/media/v3/upload"):
            mxc = f"mxc://example.org/m{len(self.media)}"
            self.media[mxc] = data
            self.upload_params.append(params)
            return _Reply(200, {"content_uri": mxc})
        if "/event/" in path:
            return _Reply(200, self.events[unquote(path.rsplit("/", 1)[1])])
        if path.endswith("/m.thread"):
            return _Reply(200, {"chunk": self.thread})
        if "/profile/" in path:
            return _Reply(404, {"errcode": "M_NOT_FOUND"})
        if path.endswith("/joined_rooms"):
            return _Reply(200, {"joined_rooms": [ROOM, PLAIN_ROOM]})
        if "/media/download/" in path:
            media_id = path.rsplit("/", 1)[1]
            return _Reply(200, self.media[f"mxc://example.org/{media_id}"],
                          headers={"Content-Type": "application/octet-stream"})
        raise AssertionError(f"unexpected call {method} {path}")


def _client(bridge, homeserver_http, *, token=AS_TOKEN):
    account = MatrixAccount(homeserver="https://hs.example.org", user_id=BRIDGE_USER,
                            access_token=token, bridge_url=bridge.url if bridge else None)
    return MatrixClient(account, session=homeserver_http)


# ---------------------------------------------------------------------------
# the API's own policy
# ---------------------------------------------------------------------------


def test_only_the_appservice_token_is_answered(bridge):
    with pytest.raises(CryptoError) as caught:
        BridgeCrypto(bridge.url, "syt_some_other_users_token").whoami()
    assert caught.value.status == 401 and "as_token" in str(caught.value)
    assert BridgeCrypto(bridge.url, AS_TOKEN).whoami() == {"user_id": BRIDGE_USER, "device_id": "BRIDGEDEV"}


def test_a_closed_store_is_unavailable_and_worth_retrying(bridge, homeserver):
    homeserver.crypto_open = False
    with pytest.raises(CryptoError) as caught:
        BridgeCrypto(bridge.url, AS_TOKEN).encrypt(ROOM, "m.room.message", {"body": "x"})
    assert (caught.value.status, caught.value.transient) == (503, True)


def test_an_unreachable_bridge_is_transient():
    with pytest.raises(CryptoError) as caught:
        BridgeCrypto("http://127.0.0.1:9", AS_TOKEN, timeout=2).whoami()
    assert caught.value.transient is True


def test_a_body_over_the_bound_is_refused_unread(bridge, monkeypatch):
    monkeypatch.setattr(crypto_api, "MAX_FILE_BYTES", 16)
    with pytest.raises(CryptoError) as caught:
        BridgeCrypto(bridge.url, AS_TOKEN).encrypt_attachment(b"x" * 64)
    assert caught.value.status == 413


def test_a_header_that_is_not_utf8_is_refused_not_crashed(bridge):
    with pytest.raises(CryptoError) as caught:
        BridgeCrypto(bridge.url, "t\u00f6ken").whoami()  # requests sends latin-1
    assert caught.value.status == 401


def test_one_bad_event_does_not_fail_its_batch(bridge, homeserver, monkeypatch):
    homeserver.room_keys.add("known")
    real = homeserver.decrypt_event

    async def poisoned(event):
        if event["event_id"] == "$bad":
            raise ValueError("Failed to deserialize {'body': 'the secret'}")
        return await real(event)

    monkeypatch.setattr(homeserver, "decrypt_event", poisoned)
    good = {"event_id": "$good", "type": "m.room.encrypted", "content": {
        "session_id": "known", "ciphertext": json.dumps({"type": "m.room.message", "content": {"body": "ok"}})}}
    bad = {**good, "event_id": "$bad"}
    results = BridgeCrypto(bridge.url, AS_TOKEN).decrypt(ROOM, [good, bad])
    assert results[0]["event"]["content"] == {"body": "ok"}
    assert results[1]["errcode"] == client_crypto.UNDECRYPTABLE and "secret" not in json.dumps(results[1])


def test_the_client_speaks_the_apis_wire_names():
    assert client_crypto._PREFIX == crypto_api.PREFIX
    assert client_crypto._FILE_HEADER == crypto_api.FILE_HEADER
    assert client_crypto._MAX_EVENTS == crypto_api.MAX_EVENTS
    assert (client_crypto.NO_SESSION, client_crypto.UNDECRYPTABLE) == (crypto_api.NO_SESSION, crypto_api.UNDECRYPTABLE)


# ---------------------------------------------------------------------------
# MatrixClient over it
# ---------------------------------------------------------------------------


def test_a_send_into_an_encrypted_room_goes_out_sealed(bridge, homeserver):
    http = _Homeserver()
    client = _client(bridge, http)
    client.send_thread_text(ROOM, ROOT, "first")
    client.send_thread_text(ROOM, ROOT, "second")
    assert [event_type for event_type, _ in http.sends] == ["m.room.encrypted", "m.room.encrypted"]
    sealed = http.sends[0][1]
    assert "first" not in json.dumps({k: v for k, v in sealed.items() if k != "ciphertext"})
    assert sealed["m.relates_to"]["rel_type"] == "m.thread"  # threads must stay findable
    assert homeserver.encrypted_for == [(ROOM, "m.room.message")] * 2
    probes = [call for call in http.calls if call[1].endswith("/state/m.room.encryption")]
    assert len(probes) == 1, "the room's encryption is asked once and remembered"


def test_reactions_are_sealed_too(bridge, homeserver):
    http = _Homeserver()
    _client(bridge, http).send_reaction(ROOM, "$human", "📥", transaction="txn-1")
    assert http.sends[0][0] == "m.room.encrypted"
    assert homeserver.encrypted_for == [(ROOM, "m.reaction")]


def test_without_a_bridge_an_encrypted_room_gets_nothing(homeserver):
    http = _Homeserver()
    client = _client(None, http)
    assert client.room_is_encrypted(ROOM)  # what every entry point asks first
    with pytest.raises(MatrixError, match="unencrypted") as caught:
        client.send_thread_text(ROOM, ROOT, "would leak")
    assert http.sends == [] and caught.value.not_sent is True


def test_without_a_bridge_a_plaintext_send_asks_nothing_new():
    """The plaintext path of an operator with no bridge is byte-for-byte as before."""
    http = _Homeserver()
    _client(None, http).send_thread_text(PLAIN_ROOM, ROOT, "hello")
    assert [method for method, _path in http.calls] == ["PUT"]


def test_a_bridge_that_cannot_encrypt_means_no_send(bridge, homeserver):
    homeserver.crypto_open = False
    http = _Homeserver()
    with pytest.raises(MatrixError) as caught:
        _client(bridge, http).send_thread_text(ROOM, ROOT, "would leak")
    assert caught.value.transient is True and http.sends == []
    assert caught.value.not_sent is True  # nothing reached the homeserver


def test_a_failed_encryption_probe_sends_nothing(bridge):
    http = _Homeserver()
    http.state_error = 502
    with pytest.raises(MatrixError) as caught:
        _client(bridge, http).send_thread_text(PLAIN_ROOM, ROOT, "hello")
    assert http.sends == [] and caught.value.not_sent is True


def test_a_room_that_turns_encryption_on_is_noticed(bridge, homeserver):
    """Review of #478: no timed re-check. The probe's answer stands until the
    room's own m.room.encryption state says otherwise."""
    http = _Homeserver()
    client = _client(bridge, http)
    client.send_thread_text(PLAIN_ROOM, ROOT, "before")
    http.now_encrypted = PLAIN_ROOM
    client.note_encrypted(PLAIN_ROOM)  # the connector's sync delivered the state event
    client.send_thread_text(PLAIN_ROOM, ROOT, "after it")
    assert [event_type for event_type, _ in http.sends] == ["m.room.message", "m.room.encrypted"]
    probes = [call for call in http.calls if call[1].endswith("/state/m.room.encryption")]
    assert len(probes) == 1


def test_without_a_bridge_the_upload_choice_asks_nothing():
    """Review of #478: attachments decide through the same predicate as sends."""
    http = _Homeserver()
    assert _client(None, http).encrypts(PLAIN_ROOM) is False
    assert http.calls == []


def test_a_plaintext_room_is_untouched_by_the_bridge(bridge, homeserver):
    http = _Homeserver()
    _client(bridge, http).send_thread_text(PLAIN_ROOM, ROOT, "hello")
    assert http.sends[0][0] == "m.room.message" and http.sends[0][1]["body"] == "hello"
    assert homeserver.encrypted_for == []


def test_decrypting_a_mixed_batch(bridge, homeserver):
    homeserver.room_keys.add("known")
    sealed = lambda event_id, session: {  # noqa: E731
        "event_id": event_id, "sender": "@h:example.org", "type": "m.room.encrypted", "origin_server_ts": 1,
        "content": {"algorithm": "m.megolm.v1.aes-sha2", "session_id": session,
                    "ciphertext": json.dumps({"type": "m.room.message", "content": {"body": event_id}})},
    }
    plain = {"event_id": "$p", "type": "m.room.message", "content": {"body": "clear"}}
    results = _client(bridge, _Homeserver()).decrypt_events(ROOM, [plain, sealed("$k", "known"), sealed("$u", "unknown")])
    assert results[0] == (plain, None)
    assert results[1][0]["content"] == {"body": "$k"} and results[1][0]["room_id"] == ROOM
    assert results[2] == (None, client_crypto.NO_SESSION)


def test_a_long_history_is_decrypted_in_bounded_batches(bridge, homeserver, monkeypatch):
    calls = []
    real = homeserver.decrypt_event

    async def counted(event):
        calls.append(event["event_id"])
        return await real(event)

    monkeypatch.setattr(homeserver, "decrypt_event", counted)
    events = [{"event_id": f"$e{n}", "type": "m.room.encrypted", "content": {"session_id": "s"}}
              for n in range(crypto_api.MAX_EVENTS + 5)]
    results = _client(bridge, _Homeserver()).decrypt_events(ROOM, events)
    assert len(results) == len(events) == len(calls)


def test_an_attachment_round_trips_as_ciphertext(bridge):
    http = _Homeserver()
    client = _client(bridge, http)
    file = client.upload_encrypted(b"all green")
    assert http.media[file["url"]] == b"neerg lla"  # only ciphertext reached the homeserver
    assert http.upload_params == [None]  # and no filename, which it would keep in the clear
    data, media_type = client.download_content({"file": file, "info": {"mimetype": "text/plain"}})
    assert (data, media_type) == (b"all green", "text/plain")


def test_a_tampered_attachment_is_refused(bridge):
    http = _Homeserver()
    client = _client(bridge, http)
    file = client.upload_encrypted(b"all green")
    http.media[file["url"]] = b"tampered!"
    with pytest.raises(MatrixError) as caught:
        client.download_content({"file": file})
    assert caught.value.status == 422


# ---------------------------------------------------------------------------
# #414: `lmer-signal read`/`post --thread` through the same client
# ---------------------------------------------------------------------------


@pytest.fixture
def thread_host(bridge, monkeypatch, tmp_path):
    """thread_access building its client from the environment, as it does in
    production — only the homeserver's HTTP is fake."""
    monkeypatch.setattr(thread_posts, "REGISTRY_DIR", str(tmp_path / "thread-posts"))
    http = _Homeserver()
    monkeypatch.setattr(thread_access, "_matrix_client", lambda account: MatrixClient(account, session=http))
    env = {"LMER_MATRIX_HOMESERVER": "https://hs.example.org", "LMER_MATRIX_USER_ID": BRIDGE_USER,
           "LMER_MATRIX_ACCESS_TOKEN": AS_TOKEN, "LMER_MATRIX_BRIDGE_URL": bridge.url}
    return thread_access, http, env


def _sealed(event_id, body, session="known"):
    return {"event_id": event_id, "sender": "@h:example.org", "type": "m.room.encrypted",
            "origin_server_ts": 1712345679000,
            "content": {"algorithm": "m.megolm.v1.aes-sha2", "session_id": session,
                        "ciphertext": json.dumps({"type": "m.room.message", "content": {"body": body}})}}


def test_a_thread_read_in_an_encrypted_room_is_decrypted(thread_host, homeserver):
    thread_access, http, env = thread_host
    homeserver.room_keys.add("known")
    http.events[ROOT] = _sealed(ROOT, "the root")
    http.thread = [_sealed("$r1", "a secret reply"), _sealed("$r2", "before the bridge", session="gone")]
    payload = thread_access.read_thread(matrix_to_link(ROOM, ROOT), env=env)
    assert [record["text"] for record in payload["messages"]] == [
        "the root", "a secret reply", thread_access.UNDECRYPTABLE_TEXT,
    ]


def test_a_thread_post_in_an_encrypted_room_goes_out_sealed(thread_host, homeserver):
    thread_access, http, env = thread_host
    thread_access.post_thread(matrix_to_link(ROOM, ROOT), "the review is up", label="run", env=env)
    assert [event_type for event_type, _ in http.sends] == ["m.room.encrypted"]
    assert homeserver.encrypted_for == [(ROOM, "m.room.message")]


def test_a_thread_opened_in_an_encrypted_room_goes_out_sealed(thread_host, homeserver):
    thread_access, http, env = thread_host
    thread_access.open_thread(ROOM, "the run", env=env)
    assert [event_type for event_type, _ in http.sends] == ["m.room.encrypted"]


def test_a_bridge_refusing_the_token_is_named_as_the_bridge(thread_host, homeserver):
    thread_access, http, env = thread_host
    env = {**env, "LMER_MATRIX_ACCESS_TOKEN": "syt_not_the_as_token"}
    with pytest.raises(thread_access.ThreadError) as caught:
        thread_access.post_thread(matrix_to_link(ROOM, ROOT), "x", label="run", env=env)
    assert caught.value.status == 403 and "lmer-matrix-bridge refused" in str(caught.value)
    assert "the homeserver refused" not in str(caught.value)
    assert http.sends == []


def test_recovery_reads_an_encrypted_echo_through_the_bridge(bridge, homeserver):
    http = _Homeserver()
    client = _client(bridge, http)
    reference = client.send_thread_text(ROOM, ROOT, "the pending text")
    homeserver.room_keys.add("fake-session")
    recovery = Recovery.__new__(Recovery)
    recovery.key = ThreadKey("matrix", ROOM, ROOT)
    recovery._verify_remote(reference, {"text": "the pending text"}, client, BRIDGE_USER)
    with pytest.raises(Exception, match="does not match"):
        recovery._verify_remote(reference, {"text": "something else"}, client, BRIDGE_USER)


def _post_attachment(client, room, tmp_path):
    """`post --thread` with one file, the verb deciding how it goes up."""
    host = tmp_path / "uploads"
    host.mkdir()
    (host / "notes.md").write_text("all green")
    store = thread_access.UploadStore(host_dir=str(host), container_dir="/home/developer/.lmer-uploads")
    item = {"name": "notes.md", "path": "/home/developer/.lmer-uploads/notes.md", "media_type": "text/plain"}
    link = matrix_to_link(room, ROOT)
    return thread_access.post_thread(
        link, "see", label="run", env={"LMER_MATRIX_AUTHENTICATED_MEDIA": "1"}, attachments=[item],
        store=store, client=client, ref=thread_access.parse_thread(link),
    )


def test_a_thread_verbs_plaintext_attachment_asks_nothing_new(tmp_path):
    """Review of #478: the verb's upload choice is the client's predicate, so
    without a bridge it adds no request."""
    http = _Homeserver()
    assert _post_attachment(_client(None, http), PLAIN_ROOM, tmp_path)["posted"]["withheld"] == []
    assert [method for method, _path in http.calls] == ["PUT", "POST", "PUT"]
    assert [event_type for event_type, _ in http.sends] == ["m.room.message", "m.room.message"]
    assert http.media[http.sends[1][1]["url"]] == b"all green"


def test_a_thread_verbs_attachment_after_the_switch_goes_up_as_ciphertext(bridge, homeserver, tmp_path):
    http = _Homeserver()
    client = _client(bridge, http)
    client.send_thread_text(PLAIN_ROOM, ROOT, "before")
    http.now_encrypted = PLAIN_ROOM
    client.note_encrypted(PLAIN_ROOM)
    assert _post_attachment(client, PLAIN_ROOM, tmp_path)["posted"]["withheld"] == []
    assert [event_type for event_type, _ in http.sends] == ["m.room.message", "m.room.encrypted", "m.room.encrypted"]
    assert http.upload_params == [None] and list(http.media.values()) == [b"neerg lla"]
    assert homeserver.encrypted_for == [(PLAIN_ROOM, "m.room.message")] * 2


def test_without_a_bridge_the_thread_verbs_refuse_with_the_pointer(thread_host):
    thread_access, http, env = thread_host
    env = {key: value for key, value in env.items() if key != "LMER_MATRIX_BRIDGE_URL"}
    for attempt in (
        lambda: thread_access.read_thread(matrix_to_link(ROOM, ROOT), env=env),
        lambda: thread_access.post_thread(matrix_to_link(ROOM, ROOT), "x", label="run", env=env),
        lambda: thread_access.open_thread(ROOM, "x", env=env),
    ):
        with pytest.raises(thread_access.ThreadError) as caught:
            attempt()
        assert "end-to-end encrypted" in str(caught.value) and "LMER_MATRIX_BRIDGE_URL" in str(caught.value)
    assert http.sends == []


# ---------------------------------------------------------------------------
# the real library, where it is installed (CI's `--extra matrix`)
# ---------------------------------------------------------------------------


@pytest.fixture
def mautrix_homeserver(tmp_path, monkeypatch):
    pytest.importorskip("mautrix.crypto.attachments")
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
    config = mxcfg.load({"name": "xo", "homeserver": "https://matrix.example.org",
                         "allow": {"@alice:example.org": ["read"]}})
    return mxclient.MautrixHomeserver(config, mxcfg.Secrets(AS_TOKEN, "hs", "recovery"))


async def test_mautrix_attachments_round_trip_and_refuse_tampering(mautrix_homeserver):
    ciphertext, file = await mautrix_homeserver.encrypt_attachment(b"all green")
    assert ciphertext != b"all green" and "url" not in file
    assert await mautrix_homeserver.decrypt_attachment(ciphertext, file) == b"all green"
    with pytest.raises(Undecryptable):
        await mautrix_homeserver.decrypt_attachment(b"x" + ciphertext[1:], file)


async def test_mautrix_refuses_crypto_before_the_store_is_open(mautrix_homeserver):
    with pytest.raises(CryptoUnavailable):
        await mautrix_homeserver.decrypt_event({"type": "m.room.encrypted", "content": {}})
    with pytest.raises(CryptoUnavailable):
        await mautrix_homeserver.crypto_identity()


async def test_mautrix_never_encrypts_for_a_room_that_is_not_encrypted(mautrix_homeserver, monkeypatch):
    with pytest.raises(CryptoUnavailable):
        await mautrix_homeserver.encrypt_content(ROOM, "m.room.message", {"body": "x"})

    async def not_encrypted(_room_id):
        return False

    mautrix_homeserver._crypto = object()  # past the store check; never reached beyond it
    monkeypatch.setattr(mautrix_homeserver, "room_is_encrypted", not_encrypted)
    with pytest.raises(MatrixClientError, match="the room is not encrypted"):
        await mautrix_homeserver.encrypt_content(PLAIN_ROOM, "m.room.message", {"body": "x"})


def test_a_real_megolm_round_trip_through_the_wire(mautrix_homeserver, tmp_path, monkeypatch):
    """encrypt_content → homeserver → decrypt_event with libolm and the real
    SQLite store. The bridge reads its own outbound session back, which needs no
    to-device delivery and so no homeserver: what this pins is everything lmer
    wrote around the library — the type lookup, the relation left in the clear,
    the event shape the connector sends, JSON all the way."""
    # deferred: optional dependency — the fixture above skips without the `matrix` extra
    from mautrix.client import Client
    from mautrix.crypto import OlmMachine

    hs = mautrix_homeserver

    async def encrypted(_room_id):
        return True

    monkeypatch.setattr(hs, "room_is_encrypted", encrypted)
    bridge = _Bridge(hs)
    try:
        async def open_device():
            database, store, state_store = await hs.open_database(tmp_path / "store.db")
            hs._database = database
            client = Client(base_url="https://matrix.example.org", mxid=hs.config.sender,
                            device_id="BRIDGEDEV", sync_store=store, state_store=state_store)
            machine = OlmMachine(client, store, state_store)
            await machine.load()
            session = await machine._new_outbound_group_session(ROOM)
            session.shared = True
            await store.add_outbound_group_session(session)
            hs._crypto, hs._crypto_client = machine, client

        asyncio.run_coroutine_threadsafe(open_device(), bridge.loop).result(30)
        http = _Homeserver()
        client = _client(bridge, http)
        client.send_thread_text(ROOM, ROOT, "ship it", extra={"net.20c.lmer.poster": {"session": "s1"}})
        ((event_type, sealed),) = http.sends
        assert event_type == "m.room.encrypted" and "ship it" not in json.dumps(sealed)
        assert sealed["m.relates_to"]["event_id"] == ROOT

        event = {"event_id": "$sent1", "sender": hs.config.sender, "origin_server_ts": 1700000000000,
                 "type": "m.room.encrypted", "content": sealed}
        stranger = {**event, "event_id": "$other", "content": {**sealed, "session_id": "not-a-session"}}
        ((plain, problem), (none, missing)) = client.decrypt_events(ROOM, [event, stranger])
        assert problem is None and plain["type"] == "m.room.message"
        assert plain["content"]["body"] == "ship it"
        assert plain["content"]["net.20c.lmer.poster"] == {"session": "s1"}
        assert plain["content"]["m.relates_to"]["rel_type"] == "m.thread"
        assert (none, missing) == (None, client_crypto.NO_SESSION)
        # A second read of the same event is not a replay attack.
        assert client.decrypt_events(ROOM, [event])[0][0]["content"]["body"] == "ship it"
    finally:
        asyncio.run_coroutine_threadsafe(hs.aclose(), bridge.loop).result(30)
        bridge.close()


def _real_device(hs, tmp_path, *, share=None):
    """A real OlmMachine on the real SQLite store; *share* replaces the network
    half of sharing a group session."""
    # deferred: optional dependency — callers sit behind the mautrix fixture
    from mautrix.client import Client
    from mautrix.crypto import OlmMachine

    async def open_device():
        database, store, state_store = await hs.open_database(tmp_path / "store.db")
        hs._database = database
        client = Client(base_url="https://matrix.example.org", mxid=hs.config.sender,
                        device_id="BRIDGEDEV", sync_store=store, state_store=state_store)
        machine = OlmMachine(client, store, state_store)
        await machine.load()
        if share is None:
            session = await machine._new_outbound_group_session(ROOM)
            session.shared = True
            await store.add_outbound_group_session(session)
        else:
            async def shared(room_id, users):
                await share(users)
                session = await machine._new_outbound_group_session(room_id)
                session.shared = True
                await store.add_outbound_group_session(session)
            machine._share_group_session = shared
        hs._crypto, hs._crypto_client = machine, client

    return open_device


def test_a_malformed_payload_is_undecryptable_not_a_failed_batch(mautrix_homeserver, tmp_path, monkeypatch):
    hs = mautrix_homeserver

    async def encrypted(_room_id):
        return True

    monkeypatch.setattr(hs, "room_is_encrypted", encrypted)
    bridge = _Bridge(hs)
    try:
        asyncio.run_coroutine_threadsafe(_real_device(hs, tmp_path)(), bridge.loop).result(30)
        http = _Homeserver()
        client = _client(bridge, http)
        client.send_thread_text(ROOM, ROOT, "good")
        client.send_event(ROOM, "m.room.message", {"msgtype": "m.file", "body": "the secret", "info": 5,
                                                   "url": "mxc://a/b",
                                                   "m.relates_to": {"rel_type": "m.thread", "event_id": ROOT}})
        good, bad = (http.events[f"$sent{n}"] for n in (1, 2))
        ((plain, _), (none, problem)) = client.decrypt_events(ROOM, [good, bad])
        assert plain["content"]["body"] == "good"
        assert (none, problem) == (None, client_crypto.UNDECRYPTABLE)
    finally:
        asyncio.run_coroutine_threadsafe(hs.aclose(), bridge.loop).result(30)
        bridge.close()


def _member_events(**by_membership):
    """``/members`` as mautrix parses it: one m.room.member event per user."""
    # deferred: optional dependency — callers sit behind the mautrix fixture
    from mautrix.types import Membership

    return [SimpleNamespace(state_key=user, content=SimpleNamespace(membership=Membership(kind)))
            for kind, users in by_membership.items() for user in users]


def test_an_invited_recipient_is_given_the_session(mautrix_homeserver, tmp_path, monkeypatch):
    """Review of #478: a DM goes out before the recipient accepts, so the
    session is shared with invited members too, as mautrix's bridge does."""
    hs = mautrix_homeserver
    shared_with = []

    async def encrypted(_room_id):
        return True

    class _Members:
        async def get_members(self, _room_id):
            return _member_events(join=[BRIDGE_USER], invite=["@recipient:example.org"],
                                  leave=["@gone:example.org"])

        async def get_joined_members(self, _room_id):
            return {BRIDGE_USER: None}

    async def share(users):
        shared_with.append(sorted(users))

    monkeypatch.setattr(hs, "room_is_encrypted", encrypted)
    monkeypatch.setattr(hs, "_appservice_client", lambda: _Members())
    bridge = _Bridge(hs)
    try:
        asyncio.run_coroutine_threadsafe(_real_device(hs, tmp_path, share=share)(), bridge.loop).result(30)
        BridgeCrypto(bridge.url, AS_TOKEN).encrypt(ROOM, "m.room.message", {"body": "hi"})
        assert shared_with == [sorted([BRIDGE_USER, "@recipient:example.org"])]
    finally:
        asyncio.run_coroutine_threadsafe(hs.aclose(), bridge.loop).result(30)
        bridge.close()


def test_concurrent_first_sends_share_the_group_session_once(mautrix_homeserver, tmp_path, monkeypatch):
    hs = mautrix_homeserver
    shares = []

    async def encrypted(_room_id):
        return True

    class _Members:
        async def get_members(self, _room_id):
            return _member_events(join=["@a:example.org"])

    async def slow_share(_users):
        shares.append(1)
        await asyncio.sleep(0.5)  # the network round trips of a real share

    monkeypatch.setattr(hs, "room_is_encrypted", encrypted)
    monkeypatch.setattr(hs, "_appservice_client", lambda: _Members())
    bridge = _Bridge(hs)
    try:
        asyncio.run_coroutine_threadsafe(_real_device(hs, tmp_path, share=slow_share)(), bridge.loop).result(30)
        results = []

        def send(n):
            try:
                BridgeCrypto(bridge.url, AS_TOKEN).encrypt(ROOM, "m.room.message", {"body": f"m{n}"})
                results.append("ok")
            except CryptoError as exc:
                results.append(str(exc))

        threads = [threading.Thread(target=send, args=(n,)) for n in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert results == ["ok", "ok"] and len(shares) == 1
    finally:
        asyncio.run_coroutine_threadsafe(hs.aclose(), bridge.loop).result(30)
        bridge.close()
