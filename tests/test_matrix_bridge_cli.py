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


@pytest.fixture(autouse=True)
def _on_a_host(monkeypatch):
    """Pin the container detection to "host" for every test but the one about it.

    This suite frequently runs *inside* a container (lmer's own session image
    leaves ``/run/.containerenv``), and since a loopback bind in a container is
    now a failure, the ambient answer would decide the verdict of every test
    with a default bind. Which environment a test means is part of the test, so
    it is stated rather than inherited — the same reason ``strip_lmer_env``
    exists above.
    """
    monkeypatch.setattr(mxcli, "_in_a_container", lambda: False)


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


def test_a_loopback_bind_inside_a_container_names_its_assumption(
    platform_root, secrets, monkeypatch,
):
    """!246 review, twice. Proposing the container shape turned
    ``DEFAULT_BIND_ADDRESS`` into a trap for anyone who runs it — the container
    reads the same config.json as the host install D1 specifies. But the first
    fix made it a **failure**, and that is wrong for a homeserver sharing this
    network namespace (the same pod), where a loopback listener is reached
    perfectly well. Config cannot tell those apart, so it names the assumption
    instead — the rule the reverse-proxy case already settled, and a check that
    refuses a correct deployment is worse than one that stays quiet.
    """
    config = mxcfg.load(dict(STORED, bind_address="127.0.0.1"))

    monkeypatch.setattr(mxcli, "_in_a_container", lambda: False)
    on_host = io.StringIO()
    assert mxcli.check(config, secrets, build_client(config), out=on_host) is True
    assert "note  reachability" in on_host.getvalue()

    monkeypatch.setattr(mxcli, "_in_a_container", lambda: True)
    inside = io.StringIO()
    assert mxcli.check(config, secrets, build_client(config), out=inside) is True, (
        "a same-pod homeserver is a legitimate deployment and must not fail"
    )
    line = next(l for l in inside.getvalue().splitlines() if "reachability" in l)
    assert line.startswith(mxcli.NOTE)
    assert "shares this network namespace" in line


def test_the_container_marker_is_read_from_the_filesystem():
    """Both runtimes leave one: podman ``/run/.containerenv``, docker
    ``/.dockerenv``.

    Read through the module rather than the fixture-patched name, since the
    autouse fixture above replaces the function this is about.
    """
    from pathlib import Path

    source = Path(mxcli.__file__).read_text()
    body = source.split("def _in_a_container()")[1].split("\ndef ")[0]
    assert "/run/.containerenv" in body
    assert "/.dockerenv" in body


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


# --- shutdown (#349) ----------------------------------------------------------
#
# The bridge is PID 1 in its container, where an uninstalled SIGTERM is not
# defaulted but dropped — before the handler existed, every stop was podman's
# 10-second grace and then SIGKILL, with the crypto store open. What is pinned
# here: a stop request drains and returns (listener down before the transport
# closes), the real signals take the same path, no tick starts after stop, an
# in-flight tick gets a bounded grace, and a second signal forfeits it.

def _bridge(configured, monkeypatch):
    """A serve()-able bridge on the fake transport, mirroring the listener test."""
    homeserver = FakeHomeserver(
        room_id=STORED["room_id"], whoami_as=configured.sender,
    )
    client = mxclient.MatrixClient(
        configured, homeserver, record_room_id=lambda _: None,
    )
    client.store_path.parent.mkdir(parents=True, exist_ok=True)
    client.store_path.write_bytes(b"a crypto store")

    monkeypatch.setattr(mxcli.mxclient, "connect", lambda config, secrets: client)
    monkeypatch.setattr(
        mxcli.mxout, "platform_endpoint",
        lambda: Endpoint("http://127.0.0.1:8765", "secret-value"),
    )
    monkeypatch.setattr(mxcli.mxout.Outbound, "snapshot", lambda self: _none())
    return homeserver


async def _until_running(task, homeserver):
    import asyncio

    for _ in range(200):
        await asyncio.sleep(0)
        if task.done() or (homeserver.callback and homeserver.joined):
            break
    if task.done() and task.exception():
        raise AssertionError(
            f"the bridge never reached its running state: {task.exception()!r}"
        )


async def test_a_stop_request_drains_and_serve_returns(
    configured, secrets, monkeypatch,
):
    """``stop.set()`` mid-run: ``serve`` returns on its own, having stopped the
    listener **before** closing the transport — an in-flight send still needs
    the session the listener never owned — and closed it exactly once."""
    import asyncio

    homeserver = _bridge(configured, monkeypatch)
    stop = asyncio.Event()
    task = asyncio.ensure_future(mxcli.serve(configured, secrets, stop=stop))
    await _until_running(task, homeserver)

    stop.set()
    await asyncio.wait_for(task, timeout=5)

    assert homeserver.shutdown_order == ["listener", "aclose"]
    assert homeserver.closed


def _assert_sigterm_handled(signal_module):
    """A guard in front of every real ``os.kill`` below (!247 review nit): if
    the handler ever stops being installed, SIGTERM's default disposition
    would terminate the whole pytest process instead of failing one test."""
    disposition = signal_module.getsignal(signal_module.SIGTERM)
    assert disposition not in (
        signal_module.SIG_DFL, signal_module.SIG_IGN,
    ), "no SIGTERM handler installed — sending the signal would kill pytest"


async def test_a_stop_during_startup_aborts_it(
    configured, secrets, monkeypatch, caplog,
):
    """The !247 review's blocking finding: startup is homeserver round trips
    with no timeout, and it is exactly when a bad deployment has the operator
    stopping the unit — so a stop request there must abort the startup and
    exit cleanly, not sit recorded-but-ignored until the SIGKILL."""
    import asyncio
    import logging

    caplog.set_level(logging.INFO, logger="matrix_bridge")
    homeserver = _bridge(configured, monkeypatch)
    release = asyncio.Event()

    async def unanswering_start(self):
        # A homeserver that accepts the connection and never answers.
        await release.wait()

    monkeypatch.setattr(mxclient.MatrixClient, "start", unanswering_start)
    stop = asyncio.Event()
    task = asyncio.ensure_future(mxcli.serve(configured, secrets, stop=stop))
    for _ in range(50):
        await asyncio.sleep(0)
    assert not task.done(), "the bridge must still be inside its startup"

    stop.set()
    await asyncio.wait_for(task, timeout=5)

    assert homeserver.closed, "the abort still releases the transport"
    messages = [record.getMessage() for record in caplog.records]
    assert not any(m.startswith("matrix_bridge_started") for m in messages), (
        "an aborted startup must not claim the bridge ran"
    )
    assert "matrix_bridge_stopped" in messages


async def test_sigterm_and_the_journal_take_the_same_stop_path(
    configured, secrets, monkeypatch, caplog,
):
    """A real SIGTERM through the loop's handler: same drain, plus the two
    journal lines an operator tells a requested stop from a crash by."""
    import asyncio
    import logging
    import os
    import signal as signal_module

    caplog.set_level(logging.INFO, logger="matrix_bridge")
    homeserver = _bridge(configured, monkeypatch)
    task = asyncio.ensure_future(mxcli.serve(configured, secrets))
    await _until_running(task, homeserver)

    _assert_sigterm_handled(signal_module)
    os.kill(os.getpid(), signal_module.SIGTERM)
    await asyncio.wait_for(task, timeout=5)

    assert homeserver.shutdown_order == ["listener", "aclose"]
    messages = [record.getMessage() for record in caplog.records]
    assert any(m == "matrix_bridge_stopping signal=SIGTERM" for m in messages)
    assert "matrix_bridge_stopped" in messages
    # And the handlers are gone with the serve that installed them: a False
    # return says the loop had nothing left to remove.
    loop = asyncio.get_running_loop()
    assert loop.remove_signal_handler(signal_module.SIGTERM) is False
    assert loop.remove_signal_handler(signal_module.SIGINT) is False


async def test_a_failing_half_still_tears_down_in_order(
    configured, secrets, monkeypatch,
):
    """One half crashing must not close the transport under the other: the
    group drains both halves first — listener down, then aclose — and the
    failure itself still reaches ``main()``'s error handling unwrapped."""
    import asyncio

    homeserver = _bridge(configured, monkeypatch)

    async def failing_poll(outbound, poll_seconds, stop, hurry, **kwargs):
        raise mxclient.MatrixClientError("the poll half died")

    monkeypatch.setattr(mxcli, "poll_forever", failing_poll)
    with pytest.raises(mxclient.MatrixClientError):
        await asyncio.wait_for(mxcli.serve(configured, secrets), timeout=5)
    assert homeserver.shutdown_order == ["listener", "aclose"]


async def test_a_second_sigterm_reaches_the_hurry_path(
    configured, secrets, monkeypatch, caplog,
):
    """Two real signals through the real handler: the second one journals
    ``immediate=1`` and sets the event the drain forfeits its grace on."""
    import asyncio
    import logging
    import os
    import signal as signal_module

    caplog.set_level(logging.INFO, logger="matrix_bridge")
    homeserver = _bridge(configured, monkeypatch)
    stop, hurry = asyncio.Event(), asyncio.Event()

    async def parked_poll(outbound, poll_seconds, stop, hurry, **kwargs):
        # Keep the bridge draining until the second signal, the way an
        # in-flight tick inside its grace would.
        await hurry.wait()

    monkeypatch.setattr(mxcli, "poll_forever", parked_poll)
    task = asyncio.ensure_future(
        mxcli.serve(configured, secrets, stop=stop, hurry=hurry)
    )
    await _until_running(task, homeserver)

    _assert_sigterm_handled(signal_module)
    os.kill(os.getpid(), signal_module.SIGTERM)
    await asyncio.wait_for(stop.wait(), timeout=5)
    os.kill(os.getpid(), signal_module.SIGTERM)
    await asyncio.wait_for(task, timeout=5)

    assert hurry.is_set()
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        m == "matrix_bridge_stopping signal=SIGTERM immediate=1"
        for m in messages
    )


class _TickProbe:
    """An outbound whose ticks the test controls and observes."""

    def __init__(self, *, park: bool = False, fail_first: bool = False):
        import asyncio

        self.park = park          # a tick that never finishes on its own
        self.fail_first = fail_first  # the first tick raises
        self.release = asyncio.Event()  # lets a parked tick finish
        self.started = 0
        self.finished = 0
        self.cancelled = 0

    async def tick(self):
        import asyncio

        self.started += 1
        if self.fail_first and self.started == 1:
            raise RuntimeError("this tick failed; the next may succeed")
        try:
            if self.park:
                await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        self.finished += 1
        return []


async def test_no_tick_starts_after_stop():
    """A stop during the sleep returns at once, with nothing new in flight."""
    import asyncio

    outbound = _TickProbe()
    stop, hurry = asyncio.Event(), asyncio.Event()
    task = asyncio.ensure_future(
        mxcli.poll_forever(outbound, 3600, stop, hurry)
    )
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert outbound.started == 0


async def test_a_stop_in_the_same_wakeup_as_the_interval_starts_no_tick():
    """!247 review: ``wait_for`` raises ``TimeoutError`` even when stop lands
    in the same wake-up as the interval expiring — without the post-timeout
    check, that one tick starts after stop and can then be cancelled
    mid-send. The event subclass delivers the stop at exactly that moment:
    inside the waiter's cancellation, after the timeout has already won."""
    import asyncio

    class StopAtExpiry(asyncio.Event):
        async def wait(self):
            try:
                return await super().wait()
            except asyncio.CancelledError:
                self.set()
                raise

    outbound = _TickProbe()
    stop, hurry = StopAtExpiry(), asyncio.Event()
    # A nonzero interval so the waiter is genuinely parked when the timeout
    # cancels it — timeout=0 short-circuits before the coroutine first runs
    # and the hook above would never fire.
    task = asyncio.ensure_future(
        mxcli.poll_forever(outbound, 0.01, stop, hurry)
    )
    await asyncio.wait_for(task, timeout=5)
    assert outbound.started == 0, "no tick may start after stop"


async def test_a_failing_tick_does_not_end_the_loop():
    """The pre-#349 promise, re-pinned through the rewritten loop: a tick that
    raises is logged and the next interval still ticks."""
    import asyncio

    outbound = _TickProbe(fail_first=True)
    stop, hurry = asyncio.Event(), asyncio.Event()
    task = asyncio.ensure_future(
        mxcli.poll_forever(outbound, 0, stop, hurry)
    )
    while outbound.finished == 0:
        await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert outbound.started >= 2, "the loop must survive the failing tick"


async def test_an_in_flight_tick_finishes_inside_the_grace():
    """A stop mid-tick lets the tick complete: this is the drain that closes
    the send-then-bind window in the common case."""
    import asyncio

    outbound = _TickProbe(park=True)
    stop, hurry = asyncio.Event(), asyncio.Event()
    task = asyncio.ensure_future(
        mxcli.poll_forever(outbound, 0, stop, hurry, tick_grace=30)
    )
    while outbound.started == 0:
        await asyncio.sleep(0)
    stop.set()
    await asyncio.sleep(0)
    outbound.release.set()
    await asyncio.wait_for(task, timeout=5)
    assert outbound.finished == 1
    assert outbound.cancelled == 0


async def test_a_slow_tick_is_cancelled_after_the_grace():
    import asyncio

    outbound = _TickProbe(park=True)
    stop, hurry = asyncio.Event(), asyncio.Event()
    task = asyncio.ensure_future(
        mxcli.poll_forever(outbound, 0, stop, hurry, tick_grace=0.05)
    )
    while outbound.started == 0:
        await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert outbound.cancelled == 1
    assert outbound.finished == 0


async def test_a_second_signal_forfeits_the_grace():
    """One signal is polite, two is now: the parked tick dies immediately even
    though its grace has barely started."""
    import asyncio

    outbound = _TickProbe(park=True)
    stop, hurry = asyncio.Event(), asyncio.Event()
    task = asyncio.ensure_future(
        mxcli.poll_forever(outbound, 0, stop, hurry, tick_grace=3600)
    )
    while outbound.started == 0:
        await asyncio.sleep(0)
    stop.set()
    await asyncio.sleep(0)
    hurry.set()
    await asyncio.wait_for(task, timeout=5)
    assert outbound.cancelled == 1
    assert outbound.finished == 0
