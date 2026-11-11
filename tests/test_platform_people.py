"""Tests for the person registry and the platform time settings (issue #604, T3).

The properties that matter: a listed Slack id, MXID or the web chat maps to one
person key and nothing else does; assistant, supervisor, peer and daemon
announcement records never map; a broken registry or time setting warns and
turns tracking off instead of stopping the daemon; and the time settings resolve
env > config.json > default like every other group.
"""

import logging

import pytest
from fastapi.testclient import TestClient

from lmer_cli import comms
from lmer_platform import api, store, timetrack, timetrack_activity
from lmer_platform import config as cfg
from lmer_platform import people
from tests.conftest import strip_lmer_env
from tests.test_platform_comms import SECRET, BusPlane, agent_posts, auth, bearer, live_session

ALICE = {
    "key": "alice", "name": "Alice",
    "slack": ["U0ALICE"], "matrix": ["@alice:example.org"],
}
BOB = {"key": "bob", "name": "Bob", "slack": ["U0BOB"]}
#: What the daemon stamps on a post the operator credential made through its API.
BY_OPERATOR = {people.META_CREDENTIAL: people.CREDENTIAL_OPERATOR}
BY_CONNECTOR = {people.META_CREDENTIAL: people.CREDENTIAL_CONNECTOR}


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


def _write(values):
    store.write_json(cfg.config_path(), values)


def _registry(platform_root, **values):
    _write({"people": [ALICE, BOB], "default_person": "alice", **values})
    return people.Registry.from_config(cfg.load())


def _message(channel, *, author=None, ref=None, author_id=None, meta=None, kind="message", seq=1):
    return comms.Message(
        id=comms.message_id("s1", seq), seq=seq, session="s1", at="2026-10-27T10:00:00Z",
        source=comms.Source(channel, author=author, ref=ref, author_id=author_id),
        kind=kind, text="hello", meta=meta or {},
    )


# --- mapping -----------------------------------------------------------------

def test_slack_id_mxid_and_web_chat_map_to_one_person(platform_root):
    registry = _registry(platform_root)

    slack = _message("slack", author="Alice A.", ref="C1:1.0:2.0", author_id="U0ALICE")
    matrix = _message("matrix", author="Alice", ref="$event", author_id="@alice:example.org")
    web = _message("platform", author="operator", meta=BY_OPERATOR)

    assert [registry.person_for(m) for m in (slack, matrix, web)] == ["alice"] * 3
    assert [registry.unmapped(m) for m in (slack, matrix, web)] == [None] * 3


def test_another_listed_person_maps_to_their_own_key(platform_root):
    registry = _registry(platform_root)
    assert registry.person_for(_message("slack", author_id="U0BOB")) == "bob"


def test_the_record_dict_the_hub_carries_maps_the_same(platform_root):
    registry = _registry(platform_root)
    record = _message("slack", author="Alice", ref="C1:1.0:2.0", author_id="U0ALICE").to_dict()
    assert registry.person_for(record) == "alice"


def test_matrix_falls_back_to_an_mxid_author_on_older_records(platform_root):
    registry = _registry(platform_root)

    assert registry.person_for(_message("matrix", author="@alice:example.org", ref="$e")) == "alice"
    assert registry.person_for(_message("matrix", author="Alice", ref="$e")) is None, (
        "a display name is not an identity"
    )


def test_an_unlisted_slack_user_is_unmapped_and_named(platform_root):
    registry = _registry(platform_root)
    stranger = _message("slack", author="Someone", ref="C1:1.0:3.0", author_id="U0OTHER")

    assert registry.person_for(stranger) is None
    assert registry.unmapped(stranger) == "slack:U0OTHER"
    assert registry.unmapped(_message("matrix", author_id="@eve:example.org")) == "matrix:@eve:example.org"


def test_a_slack_record_without_an_id_is_nobody_and_lists_nothing(platform_root):
    registry = _registry(platform_root)
    old = _message("slack", author="Alice A.", ref="C1:1.0:2.0")
    assert registry.person_for(old) is None
    assert registry.unmapped(old) is None


@pytest.mark.parametrize("channel,author", [
    (comms.CHANNEL_UBER, None),
    (comms.CHANNEL_AGENT, None),
    (comms.CHANNEL_SUPERVISOR, None),
    (comms.CHANNEL_PEER_UL, "other platform"),
    (comms.CHANNEL_TERMINAL, "operator"),
    (comms.CHANNEL_AGENT, "operator"),
])
def test_assistant_supervisor_peer_and_terminal_records_never_map(platform_root, channel, author):
    registry = _registry(platform_root)
    record = _message(channel, author=author, author_id="U0ALICE")
    assert registry.person_for(record) is None
    assert registry.unmapped(record) is None


def test_a_thread_service_announcement_never_maps(platform_root):
    """``thread_service._tell`` posts on ``platform`` as ``operator`` with a ref."""
    registry = _registry(platform_root)
    announcement = _message("platform", author="operator", ref="reference:r1", meta=BY_OPERATOR)
    assert registry.person_for(announcement) is None
    assert registry.unmapped(announcement) is None


def test_a_classified_platform_notice_never_maps(platform_root):
    registry = _registry(platform_root)
    notice = _message("platform", author="operator",
                      meta={**BY_OPERATOR, "notification_class": "connector_health"})
    assert registry.person_for(notice) is None


def test_a_platform_record_nobody_authored_or_another_author_does_not_map(platform_root):
    registry = _registry(platform_root)
    assert registry.person_for(_message("platform", meta=BY_OPERATOR)) is None
    assert registry.person_for(_message("platform", author="Alice", meta=BY_OPERATOR)) is None


def test_only_the_daemons_operator_stamp_makes_a_platform_record_the_web_chat(platform_root):
    """A bus caller that typed ``operator`` as its author is not the web chat."""
    registry = _registry(platform_root)
    for meta in ({}, BY_CONNECTOR, {people.META_CREDENTIAL: "assistant"}):
        record = _message("platform", author="operator", meta=meta)
        assert registry.person_for(record) is None, meta
        assert registry.unmapped(record) is None, meta


def test_a_bridge_answer_maps_to_its_matrix_sender_or_to_nobody(platform_root):
    """The Matrix bridge answers with the operator credential and names the sender."""
    registry = _registry(platform_root)
    listed = _message("platform", author="@alice:example.org", meta=BY_OPERATOR, kind="answer")
    stranger = _message("platform", author="@bob:example.org", meta=BY_OPERATOR, kind="answer")

    assert registry.person_for(listed) == "alice"
    assert registry.person_for(stranger) is None, "Bob has no MXID listed, so the default person is not him"
    assert registry.unmapped(stranger) == "matrix:@bob:example.org"


def test_a_connector_naming_an_mxid_author_is_nobody(platform_root):
    """Through the daemon's API the author is the caller's text, not a verified sender."""
    registry = _registry(platform_root)
    record = _message("matrix", author="@alice:example.org", meta=BY_CONNECTOR)
    assert registry.person_for(record) is None
    assert registry.unmapped(record) is None


# --- what the daemon's routes stamp (over a real supervisor bus) -------------

@pytest.fixture
def plane(tmp_path, platform_root):
    server = BusPlane(tmp_path)
    yield server
    server.stop()


@pytest.fixture
def daemon(platform_root):
    _write({"people": [ALICE, BOB], "default_person": "alice"})
    return TestClient(api.create_app(cfg.load(), SECRET)), people.Registry.from_config(cfg.load())


def _answered(plane, http, session, body, headers):
    question = agent_posts(plane, "question", "which branch?")
    answered = http.post(f"/api/sessions/{session}/ask/{question.seq:06d}/answer", json=body, headers=headers)
    assert answered.status_code == 200, answered.text
    return plane.log.get(question.seq + 1)


def test_the_web_chat_and_its_answers_map_to_the_default_person(plane, daemon):
    http, registry = daemon
    session = live_session(plane)
    posted = http.post(f"/api/sessions/{session}/comms", json={"kind": "message", "text": "start my time"},
                       headers=auth())
    assert posted.status_code == 200, posted.text
    assert registry.person_for(posted.json()["message"]) == "alice"
    assert registry.person_for(_answered(plane, http, session, {"answer": "main"}, auth())) == "alice"


@pytest.mark.parametrize("sender, person", [("@alice:example.org", "alice"), ("@bob:example.org", None)])
def test_a_matrix_bridge_answer_maps_to_its_sender_never_the_default_person(plane, daemon, sender, person):
    """The bridge's live answer (``matrix_bridge.inbound``): the operator credential, the sender as author."""
    http, registry = daemon
    session = live_session(plane)
    record = _answered(plane, http, session, {"answer": "main", "author": sender}, auth())
    assert (record.kind, record.source.channel) == ("answer", "platform")
    assert registry.person_for(record) == person


@pytest.mark.parametrize("channel, author", [
    ("platform", "operator"), ("platform", "@alice:example.org"),
    ("matrix", "operator"), ("matrix", "@alice:example.org"),
])
def test_a_connector_credential_names_nobody_whatever_author_it_passes(plane, daemon, channel, author):
    http, registry = daemon
    session = live_session(plane)
    connector = bearer(cfg.mint_connector_credential("side-bridge", channel))
    posted = http.post(f"/api/sessions/{session}/comms", headers=connector, json={
        "kind": "message", "text": "start my time", "author": author,
        "meta": {people.META_CREDENTIAL: people.CREDENTIAL_OPERATOR}})
    assert posted.status_code == 200, posted.text
    record = posted.json()["message"]
    assert registry.person_for(record) is None
    assert record["meta"][people.META_CREDENTIAL] == people.CREDENTIAL_CONNECTOR, (
        "the stamp is the daemon's, never the caller's copy")
    answer = _answered(plane, http, session, {"answer": "main", "author": author}, connector)
    assert registry.person_for(answer) is None


def test_one_listed_person_is_the_default_person(platform_root):
    _write({"people": [ALICE]})
    config = cfg.load()
    registry = people.Registry.from_config(config)

    assert config.time_tracking_enabled
    assert config.default_person is None, "the implied default is not written back"
    assert registry.default_person == "alice"
    assert people.parse_registry([ALICE], None)[1] == "alice"
    assert registry.person_for(_message("platform", author="operator", meta=BY_OPERATOR)) == "alice"


def test_without_a_default_person_the_web_chat_is_unmapped(platform_root):
    _write({"people": [ALICE, BOB]})
    config = cfg.load()
    registry = people.Registry.from_config(config)
    web = _message("platform", author="operator", meta=BY_OPERATOR)

    assert config.time_tracking_enabled, "no default person is a valid registry"
    assert registry.person_for(web) is None
    assert registry.unmapped(web) == "platform:operator"
    assert registry.person_for(_message("slack", author_id="U0ALICE")) == "alice"


def test_a_conversation_request_sender_maps_by_transport(platform_root):
    registry = _registry(platform_root)

    assert registry.person_for_sender("slack", "U0ALICE") == "alice"
    assert registry.person_for_sender("matrix", "@alice:example.org") == "alice"
    assert registry.person_for_sender("slack", "@alice:example.org") is None
    assert registry.person_for_sender("slack", "U0OTHER") is None
    assert registry.person_for_sender("platform", "operator") is None
    assert registry.person_for_sender("slack", None) is None


def test_no_registry_maps_nobody_and_tracking_does_not_run(platform_root):
    config = cfg.load()
    registry = people.Registry.from_config(config)

    assert config.people == [] and config.default_person is None
    assert config.time_tracking_enabled, "nobody listed is not a problem"
    assert not config.time_tracking_runs
    assert registry.person_for(_message("slack", author_id="U0ALICE")) is None


# --- registry validation (plan P16) -------------------------------------------

@pytest.mark.parametrize("values,fragment", [
    ({"people": [ALICE, {**BOB, "key": "alice"}]}, "listed more than once"),
    ({"people": [{**ALICE, "key": "Alice"}]}, "is not a slug"),
    ({"people": [{**ALICE, "key": "_alice"}]}, "is not a slug"),
    ({"people": [{**ALICE, "key": "runs"}]}, "is reserved"),
    ({"people": [ALICE, {**BOB, "slack": ["U0ALICE"]}]}, "listed under both 'alice' and 'bob'"),
    ({"people": [ALICE, {**BOB, "matrix": ["@alice:example.org"]}]}, "an identity belongs to one person"),
    ({"people": [{**ALICE, "matrix": ["alice"]}]}, "not an MXID"),
    ({"people": [{**ALICE, "slack": "U0ALICE"}]}, "slack must be a list"),
    ({"people": [ALICE], "default_person": "carol"}, "default_person 'carol' is not a listed person"),
    ({"people": {"alice": ALICE}}, "people must be a list"),
    ({"people": ["alice"]}, "must be an object"),
])
def test_a_broken_registry_warns_and_disables_instead_of_raising(platform_root, caplog, values, fragment):
    _write(values)

    with caplog.at_level(logging.WARNING, logger="lmer_platform.config"):
        config = cfg.load()

    assert not config.time_tracking_enabled
    assert any(fragment in problem for problem in config.time_problems), config.time_problems
    assert "platform_people_invalid" in caplog.text
    assert "time tracking is disabled" in caplog.text
    registry = people.Registry.from_config(config)
    assert not registry.enabled
    assert registry.person_for(_message("slack", author_id="U0ALICE")) is None
    assert registry.person_for(_message("platform", author="operator", meta=BY_OPERATOR)) is None


def test_problems_are_derived_never_stored(platform_root):
    _write({"people": [ALICE], "default_person": "carol", "time_problems": []})
    config = cfg.load()
    assert config.time_problems, "a file cannot clear the problems it causes"

    _write({"people": [ALICE], "default_person": "alice", "time_problems": ["stale"]})
    assert cfg.load().time_problems == ()
    assert "time_problems" not in cfg.load().to_dict()
    with pytest.raises(cfg.ConfigError, match="unknown config override"):
        cfg.load({"time_problems": ("x",)})


def test_a_saved_config_round_trips_the_registry(platform_root):
    cfg.save(cfg.PlatformConfig(people=[ALICE], default_person="alice"))
    config = cfg.load()
    assert config.people == [ALICE]
    assert config.default_person == "alice"
    assert config.time_tracking_enabled


def test_a_person_without_a_name_is_shown_by_key(platform_root):
    _write({"people": [{"key": "carol", "slack": ["U0CAROL"]}]})
    registry = people.Registry.from_config(cfg.load())
    assert registry.person("carol").name == "carol"


# --- time settings --------------------------------------------------------------

def test_time_settings_defaults(platform_root):
    settings = cfg.time_settings()

    assert {key: setting.value for key, setting in settings.items()} == {
        "idle_grace_minutes": 15,
        "min_entry_seconds": 60,
        "pause_hours": 0,
        "title_max_age_days": 30,
        "sweep_seconds": 30,
        "writer_name": "lmer timesheet",
        "writer_email": "timesheet@lmer.invalid",
    }
    assert {setting.source for setting in settings.values()} == {"default"}
    config = cfg.load()
    assert (config.time_idle_grace_minutes, config.time_min_entry_seconds, config.time_pause_hours,
            config.time_title_max_age_days, config.time_sweep_seconds) == (15, 60, 0, 30, 30)
    assert (config.time_writer_name, config.time_writer_email) == (
        "lmer timesheet", "timesheet@lmer.invalid",
    )


#: A non-default value for every time setting, as config.json or an export holds it.
TIME_VALUES = {
    "idle_grace_minutes": 7, "min_entry_seconds": 5, "pause_hours": 2, "title_max_age_days": 9,
    "sweep_seconds": 11, "writer_name": "Timesheet Bot", "writer_email": "bot@example.org",
}


@pytest.mark.parametrize("layer", ["config.json", "env"])
@pytest.mark.parametrize("key", sorted(cfg.TIME_SETTING_KEYS))
def test_every_reader_gets_each_time_setting_unwrapped(platform_root, monkeypatch, caplog, key, layer):
    """The settings are AssistantSetting wrappers; the tracker and the idle sweep
    act on the raw value, from either layer, and nothing warns."""
    field, env_var = cfg.TIME_SETTING_KEYS[key]
    value = TIME_VALUES[key]
    if layer == "env":
        monkeypatch.setenv(env_var, str(value))
    else:
        _write({field: value})
    monkeypatch.setattr(timetrack_activity, "_SWEEP_WARNED", set())

    with caplog.at_level(logging.WARNING):
        setting = cfg.time_settings()[key]
        assert (setting.value, setting.source) == (value, layer)
        assert timetrack._settings()[key] == value
        if key == "sweep_seconds":
            assert timetrack_activity._sweep_seconds() == value
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING], caplog.text


def test_every_time_setting_has_a_platform_time_env_var():
    for key, (field, env_var) in cfg.TIME_SETTING_KEYS.items():
        assert field == f"time_{key}"
        assert env_var == f"LMER_PLATFORM_TIME_{key.upper()}"
    assert cfg.ENV_TIME_WRITER_CREDENTIAL == "LMER_PLATFORM_TIME_WRITER_TOKEN"


def test_time_settings_read_config_json(platform_root):
    _write({"time_idle_grace_minutes": 5, "time_sweep_seconds": "10",
            "time_writer_name": "Timesheet Bot", "time_writer_email": "bot@example.org"})

    settings = cfg.time_settings()
    config = cfg.load()

    assert settings["idle_grace_minutes"].value == 5
    assert settings["idle_grace_minutes"].source == "config.json"
    assert settings["sweep_seconds"].value == 10
    assert settings["writer_name"].value == "Timesheet Bot"
    assert settings["writer_email"].source == "config.json"
    assert config.time_idle_grace_minutes == 5
    assert config.time_sweep_seconds == 10
    assert config.time_writer_email == "bot@example.org"
    assert config.time_tracking_enabled


def test_the_environment_beats_config_json(platform_root, monkeypatch):
    _write({"time_pause_hours": 2, "time_writer_name": "From File"})
    monkeypatch.setenv(cfg.ENV_TIME_PAUSE_HOURS, "0")
    monkeypatch.setenv(cfg.ENV_TIME_WRITER_NAME, "From Env")

    settings = cfg.time_settings()
    config = cfg.load()

    assert settings["pause_hours"].value == 0, "0 turns the pause off and is usable"
    assert settings["pause_hours"].source == "env"
    assert settings["pause_hours"].stored == 2
    assert settings["writer_name"].value == "From Env"
    assert config.time_pause_hours == 0
    assert config.time_writer_name == "From Env"


def test_an_unusable_export_falls_through_to_the_file(platform_root, monkeypatch, caplog):
    _write({"time_min_entry_seconds": 120})
    monkeypatch.setenv(cfg.ENV_TIME_MIN_ENTRY_SECONDS, "soon")
    monkeypatch.setenv(cfg.ENV_TIME_WRITER_EMAIL, "not an address")

    settings = cfg.time_settings()
    config = cfg.load()

    assert settings["min_entry_seconds"].value == 120
    assert settings["min_entry_seconds"].source == "config.json"
    assert settings["writer_email"].value == "timesheet@lmer.invalid"
    assert config.time_min_entry_seconds == 120
    assert config.time_tracking_enabled, "the export costs its own layer only"
    assert "platform_time_min_entry_invalid" in caplog.text


@pytest.mark.parametrize("values,field", [
    ({"time_sweep_seconds": 0}, "time_sweep_seconds"),
    ({"time_idle_grace_minutes": "soon"}, "time_idle_grace_minutes"),
    ({"time_title_max_age_days": True}, "time_title_max_age_days"),
    ({"time_min_entry_seconds": -1}, "time_min_entry_seconds"),
    ({"time_writer_email": "nobody"}, "time_writer_email"),
    ({"time_writer_name": "Bot <bot@example.org>"}, "time_writer_name"),
    ({"time_writer_name": ""}, "time_writer_name"),
])
def test_an_unusable_time_setting_in_config_json_disables_tracking(platform_root, caplog, values, field):
    _write({"people": [ALICE], "default_person": "alice", **values})

    with caplog.at_level(logging.WARNING, logger="lmer_platform.config"):
        config = cfg.load()

    assert not config.time_tracking_enabled
    assert [problem.split(" ", 1)[0] for problem in config.time_problems] == [field]
    assert getattr(config, field) == (
        cfg.TIME_INT_SETTINGS[field].default if field in cfg.TIME_INT_SETTINGS
        else cfg.TIME_STR_SETTINGS[field]
    ), "the field keeps a usable value of its type"
    assert "time tracking is disabled" in caplog.text
    registry = people.Registry.from_config(config)
    assert not registry.enabled
    assert registry.person_for(_message("slack", author_id="U0ALICE")) is None


def test_time_integers_have_their_own_log_keys_and_units():
    keys = {rule.log_key for rule in cfg.TIME_INT_SETTINGS.values()}
    assert len(keys) == len(cfg.TIME_INT_SETTINGS)
    assert not keys & {rule.log_key for rule in cfg.INT_SETTINGS.values()}
    for field, rule in cfg.TIME_INT_SETTINGS.items():
        assert rule.unit and rule.singular_unit, field
        assert (rule.maximum is None) == (rule.ceiling_reason is None), field


def test_time_integers_are_not_writable_through_the_assistant_config_route():
    """That route accepts exactly :data:`cfg.INT_SETTINGS`; these are file/env only."""
    assert not set(cfg.TIME_INT_SETTINGS) & set(cfg.INT_SETTINGS)


def test_an_explicit_time_value_is_validated_by_the_shared_validator():
    assert cfg.validate_int_setting("time_idle_grace_minutes", "20") == 20
    with pytest.raises(cfg.ConfigError, match="at least a minute"):
        cfg.validate_int_setting("time_idle_grace_minutes", 0)


def test_the_writer_token_comes_from_the_environment_only(platform_root, monkeypatch, caplog):
    assert cfg.time_writer_token() is None
    _write({"time_writer_token": "from-file"})
    assert cfg.time_writer_token() is None
    assert not hasattr(cfg.load(), "time_writer_token")

    monkeypatch.setenv(cfg.ENV_TIME_WRITER_CREDENTIAL, "stub-writer-credential")
    assert cfg.time_writer_token() == "stub-writer-credential"
    with caplog.at_level(logging.DEBUG):
        cfg.load()
        cfg.time_settings()
    assert "stub-writer-credential" not in caplog.text
    assert "stub-writer-credential" not in str(cfg.load().to_dict())
