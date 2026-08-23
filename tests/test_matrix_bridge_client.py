"""``matrix_bridge.client`` — the guard on what reaches the homeserver.

The rule this file is mostly about (D8/G4): **an attachment is sent only into
an encrypted room, with authenticated media confirmed on, and only after it has
been scrubbed.** The cost of getting it wrong is not a bad message — it is a
credential sitting in a room's history, or a blob anonymously fetchable by its
mxc id, and neither expires.

So the refusals are tested one precondition at a time, including the one that is
easiest to wave through: *could not confirm* is a refusal, not a maybe.

The rest is the seam behaving: text scrubbed and truncated on every path, a
created room's id recorded so the next start joins instead of creating a second
room, and nothing sent before there is a room to send it to.
"""

import io

import pytest

from lmer_platform import config as platform_config
from lmer_platform import store
from matrix_bridge import client as mxclient
from matrix_bridge import config as mxcfg
from matrix_bridge import scrub
from tests.conftest import strip_lmer_env
from tests.matrix_fakes import FakeHomeserver

ROOM = "!room:matrix.example.net"
STORED = {
    "name": "bridge-a",
    "homeserver": "https://matrix.example.net",
    "authenticated_media": True,
    "allow": {
        "@alice:matrix.example.net": ["read", "answer-live"],
        "@peer-bridge:matrix.example.net": ["read"],
    },
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
    return mxcfg.load(dict(STORED, room_id=ROOM))


@pytest.fixture
def homeserver():
    return FakeHomeserver(room_id=ROOM)


@pytest.fixture
def client(config, homeserver):
    return mxclient.MatrixClient(config, homeserver, record_room_id=lambda _: None)


@pytest.fixture
def configured_room_client(config, homeserver):
    """A client and the fake behind it, for tests that read the fake back."""
    return (
        mxclient.MatrixClient(config, homeserver, record_room_id=lambda _: None),
        homeserver,
    )


# --- the upload decision, as a rule ------------------------------------------

def test_an_encrypted_room_with_the_flag_on_may_carry_an_attachment():
    assert mxclient.upload_decision(
        room_encrypted=True, authenticated_media=True,
    ).allowed is True


@pytest.mark.parametrize("room_encrypted, authenticated_media, expected", [
    (False, True, "not encrypted"),
    (True, False, "authenticated media is off"),
    (True, None, "could not be confirmed"),
    (False, None, "not encrypted"),
])
def test_every_other_combination_is_refused_and_says_why(
    room_encrypted, authenticated_media, expected,
):
    decision = mxclient.upload_decision(
        room_encrypted=room_encrypted, authenticated_media=authenticated_media,
    )
    assert decision.allowed is False
    assert expected in decision.reason
    assert decision.note, "the room is told the attachment was dropped"


def test_unconfirmed_is_a_refusal_not_a_maybe():
    """The tempting case. An unreachable media-config endpoint is exactly when
    a bridge is most likely to shrug and upload — and media uploaded while the
    flag is off stays anonymously fetchable by its mxc id forever."""
    decision = mxclient.upload_decision(
        room_encrypted=True, authenticated_media=None,
    )
    assert decision.allowed is False


# --- the upload path ---------------------------------------------------------

async def test_upload_requires_e2ee_and_authenticated_media(client, homeserver):
    """G4's named test, both preconditions, through the client."""
    homeserver.encrypted = False
    assert await client.upload("$root", b"payload", "text/plain", "log.txt") is None

    homeserver.encrypted = True
    homeserver.authenticated_media_flag = False
    assert await client.upload("$root", b"payload", "text/plain", "log.txt") is None

    homeserver.authenticated_media_flag = None
    assert await client.upload("$root", b"payload", "text/plain", "log.txt") is None

    assert homeserver.attachments == [], "nothing reached the homeserver"


async def test_a_refused_upload_still_says_what_it_had_to_say(client, homeserver):
    """The message is what the reader needed; the file was the extra. Dropping
    both would make a precondition failure look like the bridge going quiet."""
    homeserver.encrypted = False
    await client.upload(
        "$root", b"payload", "text/plain", "log.txt", text="the run stalled",
    )
    assert len(homeserver.texts) == 1
    posted = homeserver.texts[0]
    assert "the run stalled" in posted
    assert "attachment dropped" in posted
    assert homeserver.sent[0].thread_root == "$root"


async def test_an_allowed_upload_is_sent_in_the_thread(client, homeserver):
    event_id = await client.upload("$root", b"payload", "text/plain", "log.txt")
    assert event_id
    assert len(homeserver.attachments) == 1
    attachment = homeserver.attachments[0]
    assert attachment.data == b"payload"
    assert attachment.filename == "log.txt"
    assert attachment.thread_root == "$root"


async def test_upload_is_scrubbed(client, homeserver, monkeypatch):
    """G4's other named test: attachment bytes pass the same scrub as text."""
    monkeypatch.setenv("SOME_API_TOKEN", "s3cret-token-value-12345")
    await client.upload(
        "$root", b"authorization: s3cret-token-value-12345\n", "text/plain",
        "log.txt",
    )
    sent = homeserver.attachments[0].data.decode()
    assert "s3cret-token-value-12345" not in sent
    assert scrub.REDACTION_MARKER in sent


async def test_a_binary_carrying_a_secret_is_refused_rather_than_mangled(
    client, homeserver, monkeypatch,
):
    """A regex substitution inside a compressed stream produces a corrupt file,
    not a safe one — so the attachment goes nowhere."""
    monkeypatch.setenv("SOME_API_TOKEN", "s3cret-token-value-12345")
    result = await client.upload(
        "$root", b"\x89PNG\r\n\x1a\n s3cret-token-value-12345", "image/png",
        "shot.png",
    )
    assert result is None
    assert homeserver.attachments == []
    assert "attachment dropped" in homeserver.texts[0]


async def test_an_allowed_upload_still_says_what_it_came_with(client, homeserver):
    """!243 review: an attachment's body is its filename, so returning the
    attachment and dropping the text posted a bare `m.file` and lost the message
    the file was evidence for."""
    await client.upload(
        "$root", b"payload", "text/plain", "log.txt", text="the run stalled",
    )
    assert [message.text for message in homeserver.sent if not message.is_attachment] \
        == ["the run stalled"]
    assert len(homeserver.attachments) == 1
    assert all(message.thread_root == "$root" for message in homeserver.sent)


def test_the_media_flag_is_an_assertion_not_a_measurement():
    """!243 review: the old probe asked whether the authenticated media
    *endpoints* are served, which is true on every Synapse from spec v1.11
    whatever ``matrix_enable_authenticated_media`` says — so it passed on
    exactly the homeserver the guard exists to refuse. There is no client-visible
    signal for a server setting, so the value is the operator's assertion, and
    it defaults to false."""
    import inspect

    source = inspect.getsource(mxclient.MautrixHomeserver.authenticated_media)
    body = source.split('"""')[2]  # past the docstring, which explains the probe
    assert "/_matrix/client/v1/media/config" not in body, (
        "the endpoint probe is back"
    )
    assert "self.config.authenticated_media" in body
    assert mxcfg.load(
        {k: v for k, v in STORED.items() if k != "authenticated_media"}
    ).authenticated_media is False


async def test_an_unasserted_media_flag_refuses_the_upload(platform_root):
    config = mxcfg.load(dict(
        {k: v for k, v in STORED.items() if k != "authenticated_media"},
        room_id=ROOM,
    ))
    homeserver = FakeHomeserver(room_id=ROOM, authenticated_media_flag=False)
    client = mxclient.MatrixClient(config, homeserver, record_room_id=lambda _: None)
    assert await client.upload("$root", b"payload", "text/plain", "log.txt") is None
    assert homeserver.attachments == []


# --- text on every path ------------------------------------------------------

async def test_a_thread_root_is_scrubbed_and_returns_its_event_id(
    client, homeserver, monkeypatch,
):
    monkeypatch.setenv("SOME_API_TOKEN", "s3cret-token-value-12345")
    event_id = await client.send_thread_root(
        "run stalled; token s3cret-token-value-12345",
    )
    assert event_id == "$event-1"
    assert "s3cret-token-value-12345" not in homeserver.texts[0]
    assert homeserver.sent[0].thread_root is None


async def test_a_threaded_reply_carries_the_root(client, homeserver):
    await client.send_in_thread("$root", "answered; session continuing")
    assert homeserver.sent[0].thread_root == "$root"


async def test_a_long_message_is_truncated_rather_than_attached(client, homeserver):
    """D8: a link is one tap, an attachment is a download."""
    await client.send_thread_root("x" * (mxclient.MAX_MESSAGE_CHARS + 500))
    posted = homeserver.texts[0]
    assert len(posted) <= mxclient.MAX_MESSAGE_CHARS
    assert posted.endswith("(truncated)")


def test_truncate_leaves_a_short_message_alone():
    assert mxclient.truncate("short") == "short"


async def test_nothing_is_sent_before_there_is_a_room(config, homeserver):
    client = mxclient.MatrixClient(
        mxcfg.load(STORED), homeserver, record_room_id=lambda _: None,
    )
    with pytest.raises(mxclient.MatrixClientError, match="no room yet"):
        await client.send_thread_root("hello")


# --- the room ----------------------------------------------------------------

async def test_a_configured_room_is_joined_not_recreated(client, homeserver):
    assert await client.ensure_room() == ROOM
    assert homeserver.joined == [ROOM]
    assert homeserver.created == []


async def test_an_unconfigured_room_is_created_encrypted_with_the_allowlist(
    platform_root, homeserver,
):
    recorded = []
    client = mxclient.MatrixClient(
        mxcfg.load(STORED), homeserver, record_room_id=recorded.append,
    )
    assert await client.ensure_room() == ROOM
    assert homeserver.created == [[
        "@alice:matrix.example.net", "@peer-bridge:matrix.example.net",
    ]]
    assert recorded == [ROOM], "the id is recorded, or the next start makes a second room"


async def test_the_created_room_id_lands_in_the_platform_config(
    platform_root, homeserver,
):
    """D5, end to end: through the platform's own writer, which preserves the
    rest of the mapping."""
    path = platform_config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    store.write_json(path, {"matrix": dict(STORED)})

    client = mxclient.MatrixClient(mxcfg.load(), homeserver)
    await client.ensure_room()

    stored = platform_config.load().matrix
    assert stored["room_id"] == ROOM
    assert stored["allow"] == STORED["allow"], "the rest of the section survived"


async def test_a_homeserver_that_returns_no_room_id_is_a_refusal(
    platform_root, homeserver,
):
    homeserver.room_id = ""
    client = mxclient.MatrixClient(
        mxcfg.load(STORED), homeserver, record_room_id=lambda _: None,
    )
    with pytest.raises(mxclient.MatrixClientError, match="room id"):
        await client.ensure_room()


async def test_a_configured_room_that_is_not_encrypted_is_refused(
    config, homeserver,
):
    """!243 review: joining and carrying on would put every question, answer and
    run title into a room in cleartext, on a homeserver whose identity providers
    are open-signup."""
    homeserver.encrypted = False
    client = mxclient.MatrixClient(config, homeserver, record_room_id=lambda _: None)
    with pytest.raises(mxclient.MatrixClientError, match="not encrypted"):
        await client.ensure_room()


async def test_nothing_is_sent_into_a_room_that_lost_its_encryption(config):
    """The **send** path, not the join path (!243 review): the previous version
    of this test called ``ensure_room`` and so asserted the same thing as the
    test above it — nothing was sent because nothing tried to send.

    Driven through a fake in strict mode, which models
    ``_send_encrypted``'s refusal, so the seam's two sides agree about what
    happens when a room stops being encrypted underneath a running bridge.
    """
    homeserver = FakeHomeserver(room_id=ROOM, strict_encryption=True)
    client = mxclient.MatrixClient(config, homeserver, record_room_id=lambda _: None)
    await client.ensure_room()

    homeserver.encrypted = False
    with pytest.raises(mxclient.MatrixClientError):
        await client.send_thread_root("a run is waiting")
    assert homeserver.sent == []


def test_the_real_transport_never_sends_cleartext():
    """A source-level pin, because the fake cannot reach ``_send_encrypted``:
    the branch for "not encrypted" must raise rather than fall through to an
    unencrypted send (!243 review)."""
    import inspect

    source = inspect.getsource(mxclient.MautrixHomeserver._send_encrypted)
    assert "raise MatrixClientError" in source
    unencrypted_branch = source.split("if self._crypto is None")[1].split("try:")[0]
    assert "send_message(" not in unencrypted_branch


# --- the seam itself ---------------------------------------------------------

def test_the_callback_reaches_the_homeserver(client, homeserver):
    def callback(event):
        return event

    client.on_event(callback)
    assert homeserver.callback is callback


def test_the_sqlite_crypto_store_is_importable():
    """``asyncpg`` is in the ``matrix`` extra for a reason a comment cannot
    enforce (!242 review): ``mautrix.crypto.store`` swallows the ImportError and
    turns :class:`PgCryptoStore` into ``None``, so dropping the dependency
    breaks nothing until the bridge tries to open its store on a real host.

    Skipped where the extra is not installed — which, since the extra is
    optional, is the ordinary state of a checkout. The failure this guards
    against can only exist where ``mautrix`` is installed and ``asyncpg`` is
    not, so the skip costs nothing.
    """
    mautrix_crypto = pytest.importorskip("mautrix.crypto")
    assert mautrix_crypto.PgCryptoStore is not None, (
        "asyncpg is missing: mautrix's SQLite-backed crypto store cannot import"
    )


def test_importing_the_client_needs_no_matrix_library():
    """The module is imported by ``check``, which an operator runs when the
    deployment is broken, and by this suite on hosts with no libolm."""
    import ast
    from pathlib import Path

    source = Path(mxclient.__file__).read_text()
    top_level = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            top_level.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.add(node.module.split(".")[0])
    assert "mautrix" not in top_level


# --- the callback has to actually be wired -----------------------------------

async def test_an_event_reaches_the_registered_callback(client, homeserver):
    """!243 review: the real transport stored the callback and nothing read it,
    so the inbound tests were green over a bridge that would never have
    received an event. The fake now delivers, and this is what proves the seam
    carries an event rather than merely remembering who wanted one."""
    seen = []

    async def callback(event):
        seen.append(event)

    client.on_event(callback)
    await homeserver.deliver({"type": "m.room.message", "sender": "@a:b"})
    assert seen == [{"type": "m.room.message", "sender": "@a:b"}]


async def test_delivering_with_nothing_registered_is_a_loud_failure(homeserver):
    """The fake fails the way :class:`Homeserver`'s ``NotImplementedError``
    intends: a test that forgot to wire the bridge must not look wired."""
    with pytest.raises(AssertionError, match="not listening"):
        await homeserver.deliver({"type": "m.room.message"})


def test_the_real_transport_registers_its_handler():
    """A source-level pin for the wiring the fake cannot exercise: ``on_event``
    must hand the appservice a handler, not just keep the callback."""
    import inspect

    source = inspect.getsource(mxclient.MautrixHomeserver.on_event)
    assert "matrix_event_handler" in source
    assert "_decrypt" in source


# --- the outbound client does not depend on the listener ---------------------

async def test_the_outbound_client_is_built_without_starting_a_server():
    """!245 review, iteration 2. ``AppService.intent`` raises
    ``AttributeError("the intent attribute can only be used after starting")``
    until the web server is up, and the transport used to take its outbound
    client from there. Two things broke on that: ``run`` died on its first
    homeserver call before the listener could exist, and ``check`` could not ask
    the homeserver anything at all without binding the port a running bridge is
    already on.

    Exercised against the real library, since that is where the coupling was —
    skipped only where the ``matrix`` extra is not installed (CI installs it).
    Async because ``AppServiceAPI`` builds an ``aiohttp`` session and needs a
    running loop, which every real caller of this method has.
    """
    mautrix_appservice = pytest.importorskip("mautrix.appservice")

    homeserver = mxclient.MautrixHomeserver(
        mxcfg.load(dict(STORED, room_id=ROOM)),
        mxcfg.Secrets(as_token="as", hs_token="hs", recovery_key="rk"),
    )

    intent = homeserver._appservice_client()

    assert isinstance(intent, mautrix_appservice.IntentAPI)
    assert homeserver._listening is False, "nothing was started to get it"
    with pytest.raises(AttributeError):
        homeserver._appservice().intent  # the coupling that used to be used


# --- what the transport opens, it closes -------------------------------------

async def test_check_closes_the_session_it_opened(configured_room_client):
    """The claim the `closed` flag exists for, now actually asserted (!246
    review): the fake modelled a release path nothing tested, which is how a
    second leak shipped in the commit that fixed the first one."""
    from lmer_platform.client import Endpoint

    from matrix_bridge import cli as mxcli

    client, homeserver = configured_room_client
    homeserver.whoami_as = client.config.sender

    await mxcli.check_remote(
        client.config, client, Endpoint("http://127.0.0.1:8600", "secret"),
        transport=_ok_transport(), out=io.StringIO(),
    )

    assert homeserver.closed is True, "check left the transport open"


def _ok_transport():
    class Reply:
        status_code = 200
        text = "{}"

        def json(self):
            return {}

    class Transport:
        def request(self, *args, **kwargs):
            return Reply()

    return Transport()


async def test_resetting_the_outbound_client_closes_its_session():
    """The bug this pins is one line: dropping the client reference without
    closing its session orphans it (!246 review). Driven against the real
    transport, since the session is the real library's."""
    pytest.importorskip("mautrix")

    homeserver = mxclient.MautrixHomeserver(
        mxcfg.load(dict(STORED, room_id=ROOM)),
        mxcfg.Secrets(as_token="as", hs_token="hs", recovery_key="rk"),
    )
    homeserver._appservice_client()
    session = homeserver._session
    assert session is not None and not session.closed

    await homeserver._reset_outbound_client()

    assert session.closed is True, "the session was orphaned, not closed"
    assert homeserver._session is None and homeserver._client is None


async def test_adopting_the_state_store_moves_the_event_handler_too():
    """Reassigning ``az.state_store`` leaves the constructor's handler bound to
    the old object, so inbound membership updates would keep landing in the
    placeholder store while the crypto machine read the database one (!246
    review)."""
    pytest.importorskip("mautrix")

    homeserver = mxclient.MautrixHomeserver(
        mxcfg.load(dict(STORED, room_id=ROOM)),
        mxcfg.Secrets(as_token="as", hs_token="hs", recovery_key="rk"),
    )
    appservice = homeserver._appservice()
    placeholder = appservice.state_store
    assert any(
        getattr(h, "__self__", None) is placeholder
        for h in appservice.event_handlers
    ), "the constructor registers a handler bound to the store it was given"

    class Adopted:
        async def update_state(self, event):
            return None

    adopted = Adopted()
    homeserver._adopt_state_store(adopted)

    assert appservice.state_store is adopted
    assert not any(
        getattr(h, "__self__", None) is placeholder
        for h in appservice.event_handlers
    ), "the old store still receives events"
    assert any(
        getattr(h, "__self__", None) is adopted
        for h in appservice.event_handlers
    ), "the new store receives none"
