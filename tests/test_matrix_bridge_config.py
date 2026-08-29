"""``matrix_bridge.config`` — the bridge refuses rather than guesses.

The allowlist in this file is the only authorization the Matrix feature has:
both platform credentials open every route, so a config the bridge
*misunderstood* is a config that grants the wrong thing to the wrong MXID. That
is why almost every test here is about a refusal, and why each refusal has to
name the key — an operator reading "invalid config" at 2am learns nothing.

The other half is the secrets discipline: the three tokens come from the
environment and nowhere else, and a token-shaped key found in ``config.json``
is itself a refusal. That file is state the daemon serves back through its own
API.
"""

import pytest

from lmer_platform import config as platform_config
from lmer_platform import store
from matrix_bridge import config as mxcfg
from tests.conftest import strip_lmer_env

VALID = {
    "name": "bridge-a",
    "homeserver": "https://matrix.example.net",
    "allow": {
        "@alice:matrix.example.net": ["read", "answer-live", "answer-stopped"],
        "@lmer-peer:matrix.example.net": ["read"],
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


def stored(**changes):
    """``VALID`` with *changes* applied; ``None`` removes a key."""
    merged = dict(VALID)
    for key, value in changes.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


# --- the happy path, and what it resolves to ---------------------------------

def test_a_valid_section_loads_with_the_documented_defaults():
    config = mxcfg.load(stored())
    assert config.name == "bridge-a"
    assert config.homeserver == "https://matrix.example.net"
    assert config.room_id is None
    assert config.poll_seconds == 15
    assert config.remind_seconds == 1800


def test_the_server_name_defaults_to_the_homeservers_host():
    """The common case. An operator only sets it under well-known delegation."""
    assert mxcfg.load(stored()).server_name == "matrix.example.net"
    assert mxcfg.load(
        stored(server_name="example.net")
    ).server_name == "example.net"


def test_the_sender_and_namespace_come_from_the_name():
    """D2: two bridges on one homeserver cannot claim overlapping namespaces,
    because both are derived from a name that is already unique per daemon."""
    config = mxcfg.load(stored())
    assert config.sender == "@lmer-bridge-a:matrix.example.net"
    assert config.user_namespace == "@lmer-bridge-a_.*"

    other = mxcfg.load(stored(name="peer"))
    assert other.user_namespace != config.user_namespace


def test_a_trailing_slash_on_the_homeserver_is_dropped():
    """Every request path this URL is joined with starts with one."""
    assert mxcfg.load(
        stored(homeserver="https://matrix.example.net/")
    ).homeserver == "https://matrix.example.net"


def test_the_allowlist_is_normalised_to_sets():
    """So the authorization check is a membership test and never a parse."""
    allow = mxcfg.load(stored()).allow
    assert allow["@alice:matrix.example.net"] == frozenset(
        {"read", "answer-live", "answer-stopped"}
    )
    assert allow["@lmer-peer:matrix.example.net"] == frozenset({"read"})


def test_a_bot_is_an_ordinary_entry():
    """Matrix is an agent bus: a peer bridge is an MXID with capabilities, not
    a second kind of principal."""
    config = mxcfg.load(stored(allow={"@lmer-peer:matrix.example.net": ["answer-live"]}))
    assert config.allow == {"@lmer-peer:matrix.example.net": frozenset({"answer-live"})}


def test_the_state_dir_is_inside_the_platform_dir(platform_root):
    config = mxcfg.load(stored())
    assert config.state_dir == platform_root / "matrix"


# --- refusals: the section itself --------------------------------------------

def test_a_missing_section_is_refused_by_name():
    with pytest.raises(mxcfg.MatrixConfigError) as excinfo:
        mxcfg.load({}.get("matrix"))
    assert "`matrix`" in str(excinfo.value)


def test_a_section_that_is_not_a_mapping_is_refused():
    with pytest.raises(mxcfg.MatrixConfigError, match="must be a mapping"):
        mxcfg.load(["@alice:matrix.example.net"])


@pytest.mark.parametrize("key", ["name", "homeserver"])
def test_a_missing_required_key_names_itself(key):
    with pytest.raises(mxcfg.MatrixConfigError) as excinfo:
        mxcfg.load(stored(**{key: None}))
    assert f"`matrix.{key}`" in str(excinfo.value)


def test_a_name_that_cannot_be_an_mxid_localpart_is_refused():
    """The name becomes ``@lmer-<name>:<server>``; a space or a colon in it
    would produce an MXID the homeserver rejects at registration time, which is
    a worse place to find out."""
    with pytest.raises(mxcfg.MatrixConfigError, match="matrix.name"):
        mxcfg.load(stored(name="Alice Example"))


def test_a_homeserver_that_is_not_an_http_url_is_refused():
    with pytest.raises(mxcfg.MatrixConfigError, match="matrix.homeserver"):
        mxcfg.load(stored(homeserver="matrix.example.net"))


def test_a_room_alias_is_not_a_room_id():
    """``#room:server`` and ``!abc:server`` are different things, and the one
    the bridge records after creating a room is the id."""
    with pytest.raises(mxcfg.MatrixConfigError, match="matrix.room_id"):
        mxcfg.load(stored(room_id="#lmer:matrix.example.net"))


@pytest.mark.parametrize("value", [0, -1, "15", 15.0, True])
def test_a_cadence_that_is_not_a_positive_whole_number_is_refused(value):
    with pytest.raises(mxcfg.MatrixConfigError, match="matrix.poll_seconds"):
        mxcfg.load(stored(poll_seconds=value))


# --- refusals: the allowlist -------------------------------------------------

def test_an_unknown_capability_is_refused_and_lists_the_known_ones():
    with pytest.raises(mxcfg.MatrixConfigError) as excinfo:
        mxcfg.load(stored(allow={"@alice:matrix.example.net": ["answer"]}))
    message = str(excinfo.value)
    assert "answer" in message
    for capability in mxcfg.CAPABILITIES:
        assert capability in message


@pytest.mark.parametrize("capability", mxcfg.RESERVED_CAPABILITIES)
def test_a_slice_two_capability_is_refused_as_reserved(capability):
    """Not as a typo. Granting ``spawn`` today would read as permission to
    start containers from the room, and nothing enforces it yet."""
    with pytest.raises(mxcfg.MatrixConfigError) as excinfo:
        mxcfg.load(stored(allow={"@alice:matrix.example.net": [capability]}))
    assert "reserved" in str(excinfo.value)
    assert capability in str(excinfo.value)


@pytest.mark.parametrize("key", [
    "matrix.example.net",          # a bare domain
    "@bridge-a",                      # no server
    "bridge-a:matrix.example.net",    # no sigil
    "@*:matrix.example.net",       # a pattern
    "@bridge-a:*",                    # a pattern on the server half
])
def test_only_an_explicit_mxid_is_an_allowlist_key(key):
    """The deliberate divergence from ``LMER_PUSH_ALLOW_LIST``: this homeserver
    federates and its identity providers are open-signup, so a domain is not a
    trust unit and a wildcard is an open door."""
    with pytest.raises(mxcfg.MatrixConfigError) as excinfo:
        mxcfg.load(stored(allow={key: ["read"]}))
    assert "MXID" in str(excinfo.value)


def test_capabilities_must_be_a_list_not_a_string():
    """``"read"`` would otherwise iterate into single characters and grant
    nothing while looking like it granted something."""
    with pytest.raises(mxcfg.MatrixConfigError, match="must be a list"):
        mxcfg.load(stored(allow={"@alice:matrix.example.net": "read"}))


def test_an_absent_allowlist_means_nobody_answers():
    """A bridge that only announces is a coherent thing to run, and it is the
    safe end of the range — so this is a default, not a refusal."""
    assert mxcfg.load(stored(allow=None)).allow == {}
    assert mxcfg.load(stored(allow={})).allow == {}


# --- secrets -----------------------------------------------------------------

def test_the_three_secrets_come_from_the_environment(monkeypatch):
    monkeypatch.setenv(mxcfg.ENV_AS_CREDENTIAL, "as-token-value")
    monkeypatch.setenv(mxcfg.ENV_HS_CREDENTIAL, "hs-token-value")
    monkeypatch.setenv(mxcfg.ENV_RECOVERY_KEY, "recovery-key-value")
    secrets = mxcfg.load_secrets()
    assert secrets.as_token == "as-token-value"
    assert secrets.hs_token == "hs-token-value"
    assert secrets.recovery_key == "recovery-key-value"


@pytest.mark.parametrize("absent", [
    mxcfg.ENV_AS_CREDENTIAL, mxcfg.ENV_HS_CREDENTIAL, mxcfg.ENV_RECOVERY_KEY,
])
def test_a_missing_secret_is_refused_by_variable_name(monkeypatch, absent):
    """Half-running is the state hardest to diagnose from the room."""
    for name in (mxcfg.ENV_AS_CREDENTIAL, mxcfg.ENV_HS_CREDENTIAL, mxcfg.ENV_RECOVERY_KEY):
        monkeypatch.setenv(name, "value")
    monkeypatch.delenv(absent)
    with pytest.raises(mxcfg.MatrixConfigError) as excinfo:
        mxcfg.load_secrets()
    assert absent in str(excinfo.value)


def test_a_secret_is_never_echoed_by_the_dataclass():
    """``repr`` of a config object reaches logs and tracebacks."""
    secrets = mxcfg.Secrets(as_token="as-token-value", hs_token="hs-token-value",
                            recovery_key="recovery-key-value")
    assert "as-token-value" not in repr(secrets)
    assert "recovery-key-value" not in repr(secrets)


@pytest.mark.parametrize("key", ["as_token", "hs_token", "recovery_key",
                                 "access_token", "secret"])
def test_a_secret_in_the_config_file_is_a_refusal(key):
    """Not ignored: ``config.json`` is served back by the daemon's own API, so
    finding a token there means one has already been disclosed."""
    with pytest.raises(mxcfg.MatrixConfigError) as excinfo:
        mxcfg.load(stored(**{key: "value"}))
    message = str(excinfo.value)
    assert key in message
    assert mxcfg.ENV_AS_CREDENTIAL in message
    assert "value" not in message.replace(key, ""), "the refusal quoted the secret"


# --- the platform config carries the section ---------------------------------

def test_the_platform_config_keeps_a_matrix_mapping(platform_root):
    """Spike item 3: ``load()`` ignored an unknown key, ``update_stored()``
    refused it and ``save()`` deleted it — which would have taken the
    operator's allowlist with it."""
    path = platform_config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    store.write_json(path, {"matrix": stored()})

    assert platform_config.load().matrix == stored()

    platform_config.update_stored({"matrix": stored(room_id="!room:matrix.example.net")})
    assert platform_config.load().matrix["room_id"] == "!room:matrix.example.net"

    platform_config.save(platform_config.load())
    assert platform_config.load().matrix["room_id"] == "!room:matrix.example.net"


def test_a_matrix_section_that_is_not_a_mapping_does_not_stop_the_daemon(
    platform_root, caplog,
):
    """The daemon's house rule: it does not read this mapping, so a broken one
    must not take the UI down. The bridge is where it becomes a refusal."""
    path = platform_config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    store.write_json(path, {"matrix": "not-a-mapping"})

    assert platform_config.load().matrix is None


def test_the_bridge_reads_the_section_through_the_platform_loader(platform_root):
    """One reader of ``config.json``, not two."""
    path = platform_config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    store.write_json(path, {"matrix": stored(room_id="!room:matrix.example.net")})

    config = mxcfg.load()
    assert config.name == "bridge-a"
    assert config.room_id == "!room:matrix.example.net"
