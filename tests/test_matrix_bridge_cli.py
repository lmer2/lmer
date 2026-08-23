"""``lmer-matrix-bridge`` — the two verbs that exist because of how this fails.

An appservice that is misconfigured does not crash. It sits in a room saying
nothing, or never receives a transaction, and the operator's evidence is an
absence. ``register`` and ``check` are the answer to that, so what is pinned
here is:

- **the registration and the bridge cannot drift**, because both are derived
  from the same config — a sender localpart typed twice is typed differently
  eventually, and the symptom is a bridge whose messages are rejected as coming
  from a user it does not own;
- **`register` does not print secrets by default**, because its output is a
  file that lands in an Ansible role's repository;
- **`check` reports every refusal `run` can hit and changes nothing**, because
  it is run when something is already wrong.

The console script is declared unconditionally while its dependencies live in
the `matrix` extra, so a missing extra costs an ImportError from this one
command rather than a failed install of lmer — which is why nothing in this
module may import `mautrix` at module scope.
"""

import io
import re

import pytest

from lmer_platform import store
from lmer_platform.client import Endpoint
from matrix_bridge import cli as mxcli
from matrix_bridge import client as mxclient
from matrix_bridge import config as mxcfg
from tests.conftest import strip_lmer_env
from tests.matrix_fakes import FakeHomeserver

STORED = {
    "name": "bridge-a",
    "homeserver": "https://matrix.example.net",
    "url": "https://bridge.example.net",
    "room_id": "!room:matrix.example.net",
    "allow": {
        "@alice:matrix.example.net": ["read", "answer-live", "answer-stopped"],
        "@reader:matrix.example.net": ["read"],
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
def configured(platform_root):
    path = __import__("lmer_platform.config", fromlist=["x"]).config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    store.write_json(path, {"matrix": dict(STORED)})
    return mxcfg.load()


@pytest.fixture
def secrets(monkeypatch):
    monkeypatch.setenv(mxcfg.ENV_AS_CREDENTIAL, "as-token-value")
    monkeypatch.setenv(mxcfg.ENV_HS_CREDENTIAL, "hs-token-value")
    monkeypatch.setenv(mxcfg.ENV_RECOVERY_KEY, "recovery-key-value")
    return mxcfg.load_secrets()


# --- register ----------------------------------------------------------------

def test_the_registration_is_derived_from_the_same_config_the_bridge_reads(
    configured,
):
    text = mxcli.registration(configured)
    assert "id: lmer-bridge-a" in text
    assert "sender_localpart: lmer-bridge-a" in text
    assert f"regex: '{configured.user_namespace}'" in text
    assert "url: https://bridge.example.net" in text


def test_the_namespace_in_the_registration_is_the_one_the_bridge_claims(
    configured,
):
    """The drift this verb exists to prevent: a namespace typed into the role by
    hand and a sender the bridge sends as are two facts that must be one."""
    assert configured.user_namespace in mxcli.registration(configured)
    assert configured.sender == "@lmer-bridge-a:matrix.example.net"


def test_the_registration_carries_the_service_side_opt_ins(configured):
    """The three MSCs are opted into in two places, and the registration is one
    of them: MSC2409's ``receive_ephemeral`` and MSC3202's key make the
    homeserver's own flags do anything at all."""
    text = mxcli.registration(configured)
    assert "receive_ephemeral: true" in text
    assert "org.matrix.msc3202: true" in text


def experimental_block(text):
    """The lines the header tells an operator to put under ``experimental_features``.

    Extracted rather than sliced at a string position: the previous version of
    this test cut the file at the first mention of ``io.element.msc4190``, which
    is inside a *sentence*, so `msc4190_enabled` under the block would have been
    below the cut and the assertion passed for a reason unrelated to its name
    (!245 review). This reads the block itself, so moving a line into it fails.
    """
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines)
                 if line.strip().endswith("experimental_features:"))
    block = []
    for line in lines[start + 1:]:
        stripped = line.lstrip("#").strip()
        if not stripped or ":" not in stripped:
            break
        block.append(stripped)
    return block


def test_msc4190_is_never_offered_as_a_homeserver_flag(configured):
    """The T9 run's correction, read out of Synapse 1.158.0's source: there is
    no ``experimental_features`` switch for MSC4190, and Synapse reads that
    section with ``.get()`` — so an invented ``msc4190_enabled`` is silently
    ignored and the deployment looks configured while nothing happens."""
    text = mxcli.registration(configured)
    block = experimental_block(text)

    assert block, "the header names an experimental_features block"
    assert not any("msc4190" in line for line in block), block
    assert "io.element.msc4190: true" in text, "and it is a registration key"


def test_every_homeserver_key_the_header_names_exists_in_synapse(configured):
    """Checked against matrix-synapse 1.158.0's own source, which is where the
    previous version's ``msc3202_device_masquerading`` turned out to be a
    setting Synapse has never had (!245 review). These two are read in
    ``synapse/config/experimental.py``."""
    block = experimental_block(mxcli.registration(configured))
    named = {line.split(":")[0].strip() for line in block}
    assert named == {
        "msc2409_to_device_messages_enabled",
        "msc3202_transaction_extensions",
    }, named


def test_the_media_setting_is_named_the_way_synapse_names_it(configured):
    """``enable_authenticated_media`` is Synapse's key (``config/repository.py``,
    default true since 1.120); ``matrix_enable_authenticated_media`` is the
    Ansible role variable that sets it, and the header says which is which."""
    text = mxcli.registration(configured)
    assert "enable_authenticated_media: true" in text
    assert "matrix_enable_authenticated_media" in text, "the role variable is named too"


def test_secrets_are_placeholders_by_default(configured, secrets):
    text = mxcli.registration(configured)
    assert "as-token-value" not in text
    assert "hs-token-value" not in text
    assert "CHANGE-ME" in text


def test_a_placeholder_could_never_be_mistaken_for_a_token(configured):
    """A registration installed with the placeholder still in it must fail at
    the homeserver rather than authenticate as something unexpected."""
    text = mxcli.registration(configured)
    as_token = re.search(r"^as_token: (.+)$", text, re.M).group(1)
    assert as_token.startswith("CHANGE-ME")


def test_with_secrets_prints_the_real_tokens(configured, secrets):
    text = mxcli.registration(configured, secrets)
    assert "as_token: as-token-value" in text
    assert "hs_token: hs-token-value" in text


def test_register_falls_back_to_the_bind_address_when_no_url_is_set(
    platform_root,
):
    config = mxcfg.load({k: v for k, v in STORED.items() if k != "url"})
    assert "url: http://127.0.0.1:29331" in mxcli.registration(config)


def test_the_register_verb_prints_placeholders_and_exits_zero(
    configured, secrets, capsys,
):
    assert mxcli.main(["register"]) == 0
    out, err = capsys.readouterr()
    assert "id: lmer-bridge-a" in out
    assert "as-token-value" not in out
    assert err == ""


def test_with_secrets_says_on_stderr_that_it_did(configured, secrets, capsys):
    assert mxcli.main(["register", "--with-secrets"]) == 0
    out, err = capsys.readouterr()
    assert "as_token: as-token-value" in out
    assert "vault" in err, "the operator is told what they are now holding"


def test_with_secrets_refuses_rather_than_printing_a_half_registration(
    configured, monkeypatch, capsys,
):
    monkeypatch.delenv(mxcfg.ENV_AS_CREDENTIAL, raising=False)
    assert mxcli.main(["register", "--with-secrets"]) == mxcli.EXIT_FAILURE
    out, err = capsys.readouterr()
    assert out == ""
    assert mxcfg.ENV_AS_CREDENTIAL in err


# --- check -------------------------------------------------------------------

def build_client(config, homeserver=None):
    return mxclient.MatrixClient(
        config, homeserver or FakeHomeserver(), record_room_id=lambda _: None,
    )


def test_check_reports_every_precondition(configured, secrets):
    out = io.StringIO()
    assert mxcli.check(configured, secrets, build_client(configured), out=out) is True
    report = out.getvalue()
    for line in ("config", "secrets", "url", "room", "crypto store", "allowlist"):
        assert line in report


def test_check_names_the_missing_secrets_by_variable(configured):
    out = io.StringIO()
    assert mxcli.check(configured, None, build_client(configured), out=out) is False
    report = out.getvalue()
    assert mxcfg.ENV_AS_CREDENTIAL in report
    assert mxcfg.ENV_RECOVERY_KEY in report


def test_check_counts_who_may_start_a_session(configured, secrets):
    """The number an operator most wants to be surprised by: ``answer-stopped``
    spawns a container."""
    out = io.StringIO()
    mxcli.check(configured, secrets, build_client(configured), out=out)
    assert "1 may start a session" in out.getvalue()


@pytest.mark.parametrize("label, url, bind, port, verdict, expected", [
    # FAIL is now reserved for a registration that would carry an address
    # nothing can dial. Both rounds of this check got the boundary wrong in
    # opposite directions (!245 review), so each row names the shape.
    ("a wildcard bind with no url", None, "0.0.0.0", 29331, mxcli.FAIL,
     "not an address anything can dial"),
    ("a url that names a wildcard", "http://0.0.0.0:29331", "0.0.0.0", 29331,
     mxcli.FAIL, "not an address the homeserver can dial"),
    # Reverse proxies bind a PORT, not an address. Round two called these
    # failures, which would have refused a correct deployment.
    ("a proxy on the same address", "http://10.0.0.5:443", "10.0.0.5", 29331,
     mxcli.NOTE, "forwards that port to this one"),
    ("a proxy on https' default port", "https://10.0.0.5", "10.0.0.5", 29331,
     mxcli.NOTE, "names port 443"),
    ("a proxy on loopback", "http://127.0.0.1:443", "127.0.0.1", 29331,
     mxcli.NOTE, "forwards that port to this one"),
    ("a proxy in front of a wildcard bind", "https://bridge.example.org",
     "0.0.0.0", 29331, mxcli.NOTE, "names port 443"),
    # Exact matches.
    ("the direct bind", "http://10.0.0.5:29331", "10.0.0.5", 29331, mxcli.OK,
     "10.0.0.5:29331"),
    ("a published container port", "http://10.0.0.5:29331", "0.0.0.0", 29331,
     mxcli.OK, "0.0.0.0:29331"),
    ("a wildcard bind with a loopback url", "http://127.0.0.1:29331", "0.0.0.0",
     29331, mxcli.OK, "every interface"),
    ("a routable bind with no url", None, "10.0.0.5", 29331, mxcli.OK,
     "the registration will say"),
    # Indirections nobody can verify from config alone.
    ("a bind on a different interface", "http://10.0.0.5:29331", "10.0.0.9",
     29331, mxcli.NOTE, "something forwarding one to the other"),
    ("a proxy in front of loopback", "https://bridge.example.org", "127.0.0.1",
     29331, mxcli.NOTE, "forwards that address"),
    ("a loopback url against a routable bind", "http://127.0.0.1:29331",
     "10.0.0.5", 29331, mxcli.NOTE, "its own loopback"),
    ("a host install with no url", None, "127.0.0.1", 29331, mxcli.NOTE,
     "reachable only from this host"),
])
def test_check_says_whether_the_homeserver_can_reach_the_bridge(
    platform_root, secrets, label, url, bind, port, verdict, expected,
):
    """`url` and the bind pair are two halves of one fact, and nothing else
    checks them against each other — a homeserver in a container POSTing at a
    loopback listener produces an empty room and no error anywhere.

    The verdict column is the point, and it has been wrong in both directions:
    round one answered `ok` to a port mismatch, a wrong-interface bind and a
    wildcard bind with no url; round two fixed those by inferring that the same
    address means no indirection, which failed every reverse proxy — the
    ordinary topology. A check that refuses to start a correct deployment is
    worse than one that stays quiet, because the operator believes it.

    So `FAIL` is only for a registration that would carry an address nothing can
    dial, `ok` is an exact match, and everything else is a `note` naming the
    assumption it rests on.
    """
    stored = dict(STORED, bind_address=bind, bind_port=port)
    if url is None:
        stored.pop("url", None)
    else:
        stored["url"] = url
    config = mxcfg.load(stored)

    out = io.StringIO()
    mxcli.check(config, secrets, build_client(config), out=out)

    line = next(l for l in out.getvalue().splitlines() if "reachability" in l)
    assert line.startswith(verdict), f"{label}: {line}"
    assert expected in line, f"{label}: {line}"


def test_a_working_reverse_proxy_never_fails_the_verb(platform_root, secrets):
    """The regression round two introduced: nginx on :443 in front of the bridge
    on :29331, same interface, is a correct deployment and `check` must not
    refuse it."""
    config = mxcfg.load(dict(STORED, url="http://10.0.0.5:443",
                             bind_address="10.0.0.5", bind_port=29331))
    out = io.StringIO()
    assert mxcli.check(config, secrets, build_client(config), out=out) is True


def test_a_check_that_cannot_work_at_all_fails_the_verb(platform_root, secrets):
    """A `FAIL` line has to make `check` exit non-zero, or the verdict is
    decoration."""
    config = mxcfg.load(dict(
        {k: v for k, v in STORED.items() if k != "url"}, bind_address="0.0.0.0",
    ))
    out = io.StringIO()
    assert mxcli.check(config, secrets, build_client(config), out=out) is False


def test_check_reports_an_unreadable_store_without_touching_it(
    configured, secrets, tmp_path,
):
    """The store is made unreadable with a **directory**, not ``chmod 000``:
    root bypasses file modes and CI runs as root, so the mode-based version
    passed locally and quietly stopped proving anything in the pipeline
    (pipeline 1748 — the same fault the crypto tests had, in the one file I
    missed when fixing them)."""
    client = build_client(configured)
    client.store_path.parent.mkdir(parents=True, exist_ok=True)
    client.store_path.mkdir()

    out = io.StringIO()
    assert mxcli.check(configured, secrets, client, out=out) is False
    assert "unreadable" in out.getvalue()
    assert client.store_path.exists(), "check created or replaced nothing"


def test_check_is_content_with_an_absent_store(configured, secrets):
    """A first start mints a device; that is not a failure to report as one."""
    out = io.StringIO()
    assert mxcli.check(configured, secrets, build_client(configured), out=out) is True
    assert "absent" in out.getvalue()


def test_an_unset_room_is_a_note_and_nothing_is_created(platform_root, secrets):
    """A first start legitimately has no room. A check that failed here would
    teach an operator to ignore it, which is the one thing a diagnostic must
    never do — and it must certainly not create the room to make itself pass."""
    config = mxcfg.load({k: v for k, v in STORED.items() if k != "room_id"})
    homeserver = FakeHomeserver()
    out = io.StringIO()
    assert mxcli.check(config, secrets, build_client(config, homeserver),
                       out=out) is True
    assert "note  room" in out.getvalue()
    assert homeserver.created == [], "a diagnostic that creates a room is not one"


def test_an_empty_allowlist_is_a_note_that_says_what_it_means(
    platform_root, secrets,
):
    config = mxcfg.load({k: v for k, v in STORED.items() if k != "allow"})
    out = io.StringIO()
    assert mxcli.check(config, secrets, build_client(config), out=out) is True
    assert "answer nobody" in out.getvalue()


def test_check_reports_the_media_flag_as_an_assertion(platform_root, secrets):
    """The one that decides whether an upload can ever happen — and ``check``
    says whose claim it is, because it is not something the bridge can
    measure (!243 review)."""
    on = io.StringIO()
    mxcli.check(mxcfg.load(dict(STORED, authenticated_media=True)), secrets,
                build_client(mxcfg.load(STORED)), out=on)
    assert "assertion" in on.getvalue()

    off = io.StringIO()
    mxcli.check(mxcfg.load(STORED), secrets, build_client(mxcfg.load(STORED)),
                out=off)
    assert "attach nothing" in off.getvalue()


async def test_check_remote_passes_when_the_homeserver_and_the_daemon_agree(
    configured, secrets,
):
    homeserver = FakeHomeserver(whoami_as=configured.sender, encrypted=True)
    out = io.StringIO()
    ok = await mxcli.check_remote(
        configured, build_client(configured, homeserver),
        Endpoint("http://127.0.0.1:8765", "secret-value"),
        transport=_platform(200), out=out,
    )
    assert ok is True, out.getvalue()


async def test_check_remote_reports_an_appservice_the_homeserver_rejects(
    configured, secrets,
):
    """!245 review: the most likely cause of the quiet room this verb exists
    for — a registration never installed, or an as_token that does not match —
    and the verb could not see it."""
    homeserver = FakeHomeserver(whoami_as="@someone-else:matrix.example.net")
    out = io.StringIO()
    ok = await mxcli.check_remote(
        configured, build_client(configured, homeserver),
        Endpoint("http://127.0.0.1:8765", "secret-value"),
        transport=_platform(200), out=out,
    )
    assert ok is False
    assert "not @lmer-bridge-a:matrix.example.net" in out.getvalue()


async def test_check_remote_reports_a_room_that_is_not_encrypted(
    configured, secrets,
):
    """``ensure_room`` refuses to start on it, and ``check`` promises every
    refusal ``run`` can hit."""
    homeserver = FakeHomeserver(whoami_as=configured.sender, encrypted=False)
    out = io.StringIO()
    ok = await mxcli.check_remote(
        configured, build_client(configured, homeserver),
        Endpoint("http://127.0.0.1:8765", "secret-value"),
        transport=_platform(200), out=out,
    )
    assert ok is False
    assert "NOT encrypted" in out.getvalue()


async def test_the_daemon_is_still_checked_without_matrix_secrets(configured):
    """An operator missing one env var should not also lose the answer to
    "does the daemon answer?", which needs no Matrix identity (!245 review)."""
    out = io.StringIO()
    ok = await mxcli.check_remote(
        configured, None, Endpoint("http://127.0.0.1:8765", "secret-value"),
        transport=_platform(200), out=out,
    )
    assert ok is True
    report = out.getvalue()
    assert "no secrets to ask it with" in report
    assert "platform:" in report


async def test_check_remote_reports_a_daemon_that_will_not_answer(
    configured, secrets,
):
    homeserver = FakeHomeserver(whoami_as=configured.sender)
    out = io.StringIO()
    ok = await mxcli.check_remote(
        configured, build_client(configured, homeserver),
        Endpoint("http://127.0.0.1:8765", "secret-value"),
        transport=_platform(503), out=out,
    )
    assert ok is False
    assert "503" in out.getvalue()


def _platform(status_code):
    class Reply:
        def __init__(self):
            self.status_code = status_code
            self.text = "{}"

        def json(self):
            return {}

    class Transport:
        def request(self, *args, **kwargs):
            return Reply()

    return Transport()


# --- the surface -------------------------------------------------------------

def test_a_bad_config_is_a_refusal_naming_the_key(platform_root, capsys):
    from lmer_platform import config as platform_config

    path = platform_config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    store.write_json(path, {"matrix": dict(STORED, allow={"nobody": ["read"]})})

    assert mxcli.main(["check"]) == mxcli.EXIT_FAILURE
    assert "MXID" in capsys.readouterr().err


def test_no_verb_prints_help_rather_than_doing_something(platform_root, capsys):
    assert mxcli.main([]) == mxcli.EXIT_FAILURE
    assert "lmer-matrix-bridge" in capsys.readouterr().err


def test_the_console_script_is_declared(project_root=None):
    """Declared unconditionally although the dependencies are an extra: a
    missing extra then costs an ImportError from this command rather than a
    failed install of lmer."""
    from pathlib import Path

    text = Path("pyproject.toml").read_text()
    assert 'lmer-matrix-bridge = "matrix_bridge.cli:main"' in text


def test_the_cli_imports_without_the_matrix_extra():
    """This module is what an operator runs when the deployment is broken, and
    the suite runs on hosts with no libolm."""
    import ast
    from pathlib import Path

    top_level = set()
    for node in ast.parse(Path(mxcli.__file__).read_text()).body:
        if isinstance(node, ast.Import):
            top_level.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.add(node.module.split(".")[0])
    assert "mautrix" not in top_level


def test_the_matrix_secrets_do_not_reach_session_containers():
    """The plan asked for these three in ``lmer_cli.cli``'s container-env
    passthrough. They are deliberately **not** there: the bridge is host-side
    (D1) and never runs in a session container, so passing them in would hand
    every agent the bridge's Matrix identity — its appservice tokens and the key
    that unlocks its crypto-store backup — for no benefit at all.

    Pinned rather than left to a comment so that adding them later is a
    deliberate act with this test in front of it.
    """
    from pathlib import Path

    source = Path("src/lmer_cli/cli.py").read_text()
    for name in (mxcfg.ENV_AS_CREDENTIAL, mxcfg.ENV_HS_CREDENTIAL,
                 mxcfg.ENV_RECOVERY_KEY):
        assert name not in source, (
            f"{name} reaches session containers; the bridge is host-side and "
            f"these are its identity"
        )


# --- the bridge has to be listening ------------------------------------------

async def test_run_reaches_the_running_state_with_the_listener_up(
    configured, secrets, monkeypatch,
):
    """!245 review, twice. First round: the appservice's HTTP server was never
    started, so the bridge announced runs and could receive nothing. Second
    round: it was started, but *inside the gather* — thirteen lines after
    ``client.start()``, whose first homeserver call goes through
    ``AppService.intent``, which raises ``AttributeError`` until the server is
    up. The fix existed and was unreachable.

    So this test does not assert that a call exists. The fake refuses every
    homeserver call before ``listen()`` exactly as ``AppService.intent`` does
    (``requires_listener``), and the test runs ``serve()`` until the bridge is
    *in* its running state — listener bound, store opened, room joined, inbound
    callback registered. Wrong order and it raises instead.
    """
    import asyncio

    from lmer_platform.client import Endpoint

    homeserver = FakeHomeserver(room_id=STORED["room_id"], whoami_as=configured.sender,
                                requires_listener=True)
    client = mxclient.MatrixClient(configured, homeserver,
                                   record_room_id=lambda _: None)
    client.store_path.parent.mkdir(parents=True, exist_ok=True)
    client.store_path.write_bytes(b"a crypto store")

    monkeypatch.setattr(mxcli.mxclient, "connect", lambda config, secrets: client)
    monkeypatch.setattr(
        mxcli.mxout, "platform_endpoint",
        lambda: Endpoint("http://127.0.0.1:8765", "secret-value"),
    )
    monkeypatch.setattr(mxcli.mxout.Outbound, "snapshot",
                        lambda self: _none())

    task = asyncio.ensure_future(mxcli.serve(configured, secrets))
    for _ in range(200):
        await asyncio.sleep(0)
        if task.done() or (homeserver.callback and homeserver.joined):
            break
    if task.done() and task.exception():
        raise AssertionError(
            f"the bridge never reached its running state: {task.exception()!r}"
        )
    task.cancel()

    assert homeserver.listening == (configured.bind_address, configured.bind_port)
    assert homeserver.opened, "the crypto store was opened after the listener"
    assert homeserver.joined == [STORED["room_id"]], "and the room was joined"
    assert homeserver.callback is not None, "and inbound is wired to it"


async def _none():
    return None
