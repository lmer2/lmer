"""Tests for service slots (issue #245).

The properties that matter: slots are declared in per-instance config and one
malformed entry never takes the others down; a definition that names a preset
this host cannot use *loads*, unusable and with a reason, rather than
disappearing; occupancy is derived from live sessions so nothing on disk can
strand a slot; and the one runtime query this adds is the shared, runtime-
agnostic resolver rather than a command of its own.
"""

import json
import os
import pathlib
import re

import pytest

from lmer_cli.service import ServiceError
from lmer_platform import config as cfg
from lmer_platform import registry, slots, store
from tests.conftest import strip_lmer_env

DEAD_PID = 2**22  # far above /proc/sys/kernel/pid_max on normal systems


class _Clock:
    """A stand-in for the ``time`` module, so a TTL test needs no sleep."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def monotonic(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture(autouse=True)
def _clean_probe_cache():
    """The probe memo is module state; no test may inherit another's answer."""
    slots.clear_probe_cache()
    yield
    slots.clear_probe_cache()


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


@pytest.fixture
def presets_file(slot_host):
    """The shared slot host (tests/conftest.py). Named for what this module's
    tests use it for; ``slot_host`` also installs the probe fake."""
    return slot_host


@pytest.fixture
def service_up(slot_host):
    """The same shared host, named for the property these tests lean on: every
    service resolves. Returns the recorded ``(runtime, service, announce)``
    calls — one fake for the whole suite, so a laxer copy here cannot let a
    caller pass that would fail against the real function."""
    return slot_host


def _store_slots(entries):
    store.write_json(cfg.config_path(), {"slots": entries})


def _register(session_id, *, pid, slot=None, run=None):
    return registry.register(
        session_id,
        pid=pid,
        slot=slot,
        run=run or {"host": "h", "project": "p", "slug": session_id},
    )


# --- G1: declaring slots ----------------------------------------------------

def test_slots_load_from_config_file(platform_root):
    _store_slots([
        {"name": "webapp", "preset": "webapp_dev", "description": "Web app dev stack"},
    ])

    config = cfg.load()

    assert config.slots == (
        {"name": "webapp", "preset": "webapp_dev", "description": "Web app dev stack"},
    )
    assert slots.slot_definitions(config) == [
        slots.SlotDefinition(name="webapp", preset="webapp_dev",
                             description="Web app dev stack"),
    ]


def test_no_slots_declared_is_an_empty_list(platform_root):
    assert slots.slot_definitions(cfg.load()) == []


@pytest.mark.parametrize("bad, reason", [
    ("not-a-mapping", "not_a_mapping"),
    ({"preset": "webapp_dev"}, "missing_name"),
    ({"name": "   ", "preset": "webapp_dev"}, "missing_name"),
    ({"name": "webapp"}, "missing_preset"),
    ({"name": "webapp", "preset": ""}, "missing_preset"),
])
def test_a_malformed_entry_is_skipped_and_the_others_survive(
    platform_root, caplog, bad, reason
):
    _store_slots([bad, {"name": "good", "preset": "other_dev"}])

    with caplog.at_level("WARNING", logger="lmer_platform.slots"):
        definitions = slots.slot_definitions(cfg.load())

    assert [d.name for d in definitions] == ["good"]
    assert f"reason={reason}" in caplog.text


def test_a_duplicate_name_keeps_the_first_and_warns(platform_root, caplog):
    _store_slots([
        {"name": "webapp", "preset": "webapp_dev"},
        {"name": "webapp", "preset": "other_dev"},
    ])

    with caplog.at_level("WARNING", logger="lmer_platform.slots"):
        definitions = slots.slot_definitions(cfg.load())

    assert [(d.name, d.preset) for d in definitions] == [("webapp", "webapp_dev")]
    assert "reason=duplicate" in caplog.text


def test_a_slots_value_that_is_not_a_list_warns_and_declares_none(
    platform_root, caplog
):
    store.write_json(cfg.config_path(), {"slots": {"webapp": "webapp_dev"}})

    with caplog.at_level("WARNING", logger="lmer_platform.config"):
        config = cfg.load()

    assert config.slots == ()
    assert "field=slots" in caplog.text
    # The daemon still boots on the rest of the file — the whole reason this
    # warns instead of raising.
    assert config.bind_port == cfg.DEFAULT_BIND_PORT


def test_an_unknown_key_warns_but_keeps_the_slot(platform_root, caplog):
    _store_slots([{"name": "webapp", "preset": "webapp_dev", "colour": "blue"}])

    with caplog.at_level("WARNING", logger="lmer_platform.slots"):
        definitions = slots.slot_definitions(cfg.load())

    assert [d.name for d in definitions] == ["webapp"]
    assert "slot_unknown_keys" in caplog.text


# --- G1: unusable, not vanished ---------------------------------------------

def test_a_slot_naming_an_unknown_preset_loads_unusable_with_a_reason(
    platform_root, presets_file, service_up
):
    _store_slots([{"name": "webapp", "preset": "typo_dev"}])

    rows = slots.slot_rows(cfg.load())

    assert len(rows) == 1
    assert rows[0].state == slots.SLOT_MISCONFIGURED
    assert "typo_dev" in rows[0].reason
    assert "not defined on this host" in rows[0].reason
    assert not rows[0].usable


def test_a_slot_whose_preset_sets_no_service_loads_unusable_with_a_reason(
    platform_root, presets_file, service_up
):
    _store_slots([{"name": "plain", "preset": "no_service"}])

    row = slots.slot_rows(cfg.load())[0]

    assert row.state == slots.SLOT_MISCONFIGURED
    assert "sets no service" in row.reason
    assert row.service is None


def test_an_unusable_slot_is_never_probed(platform_root, presets_file, service_up):
    _store_slots([{"name": "webapp", "preset": "typo_dev"}])

    slots.slot_rows(cfg.load())

    assert service_up == []


# --- G2: occupancy is derived -----------------------------------------------

def test_a_slot_reads_occupied_while_its_session_lives(
    platform_root, presets_file, service_up
):
    _store_slots([{"name": "webapp", "preset": "webapp_dev"}])
    _register("s-live", pid=os.getpid(), slot="webapp",
              run={"host": "git.example.com", "project": "acme/app", "slug": "r1"})

    row = slots.slot_rows(cfg.load())[0]

    assert row.state == slots.SLOT_OCCUPIED
    assert row.occupant == {
        "session_id": "s-live",
        "host": "git.example.com",
        "project": "acme/app",
        "slug": "r1",
    }


def test_a_dead_session_frees_its_slot(platform_root, presets_file, service_up):
    _store_slots([{"name": "webapp", "preset": "webapp_dev"}])
    _register("s-dead", pid=DEAD_PID, slot="webapp")

    row = slots.slot_rows(cfg.load())[0]

    assert row.state == slots.SLOT_FREE
    assert row.occupant is None
    # The entry is still on disk — it is the evidence the session crashed. What
    # changed is that it no longer holds the slot.
    assert registry.read_session("s-dead") is not None


def test_occupancy_survives_a_fresh_read_with_no_daemon_state(
    platform_root, presets_file, service_up
):
    """A restarted daemon reads the same answer: nothing is cached in-process."""
    _store_slots([{"name": "webapp", "preset": "webapp_dev"}])
    _register("s-live", pid=os.getpid(), slot="webapp")

    first = slots.slot_rows(cfg.load())[0]
    slots.clear_probe_cache()  # the only in-process state there is
    second = slots.slot_rows(cfg.load())[0]

    assert first.state == second.state == slots.SLOT_OCCUPIED


def test_nothing_writes_occupancy_to_disk(platform_root, presets_file, service_up):
    _store_slots([{"name": "webapp", "preset": "webapp_dev"}])
    _register("s-live", pid=os.getpid(), slot="webapp")

    before = sorted(p.name for p in platform_root.rglob("*") if p.is_file())
    slots.slot_rows(cfg.load())
    after = sorted(p.name for p in platform_root.rglob("*") if p.is_file())

    assert before == after
    # And the one field that names a slot is the session's own, written at
    # spawn — no file anywhere records the reverse mapping.
    assert registry.read_session("s-live")["slot"] == "webapp"


def test_an_occupied_slot_is_never_probed(platform_root, presets_file, service_up):
    _store_slots([{"name": "webapp", "preset": "webapp_dev"}])
    _register("s-live", pid=os.getpid(), slot="webapp")

    slots.slot_rows(cfg.load())

    assert service_up == []


def test_a_session_naming_no_slot_holds_none(
    platform_root, presets_file, service_up
):
    _store_slots([{"name": "webapp", "preset": "webapp_dev"}])
    _register("s-plain", pid=os.getpid())

    assert slots.slot_rows(cfg.load())[0].state == slots.SLOT_FREE


def test_supplied_sessions_are_still_filtered_for_liveness(
    platform_root, presets_file, service_up
):
    """The fleet payload reads ``live_only=False``; a dead entry must not count."""
    _store_slots([{"name": "webapp", "preset": "webapp_dev"}])
    _register("s-dead", pid=DEAD_PID, slot="webapp")
    sessions = registry.list_sessions(live_only=False)
    assert sessions and sessions[0]["live"] is False

    row = slots.slot_rows(cfg.load(), sessions=sessions)[0]

    assert row.state == slots.SLOT_FREE


# --- the service probe ------------------------------------------------------

def test_a_free_slot_with_a_running_service_reads_free(
    platform_root, presets_file, service_up
):
    _store_slots([{"name": "webapp", "preset": "webapp_dev"}])

    row = slots.slot_rows(cfg.load())[0]

    assert row.state == slots.SLOT_FREE
    assert row.service == "webapp-web"
    assert row.reason is None
    assert row.usable


def test_a_stopped_service_is_its_own_state_not_free(
    platform_root, presets_file, monkeypatch
):
    def _down(runtime, service_name, *, announce=True):
        raise ServiceError(f"No running container matched '{service_name}'")

    monkeypatch.setattr(slots, "resolve_container", _down)
    monkeypatch.setattr(slots, "_detected_runtime", lambda: "a-runtime")
    _store_slots([{"name": "webapp", "preset": "webapp_dev"}])

    row = slots.slot_rows(cfg.load())[0]

    assert row.state == slots.SLOT_SERVICE_DOWN
    assert "webapp-web" in row.reason
    assert not row.usable


def test_the_probe_reason_is_the_resolvers_own_text_flattened(
    platform_root, presets_file, monkeypatch
):
    """A row is one line; the no-match message is several."""
    def _down(runtime, service_name, *, announce=True):
        raise ServiceError("No running container matched 'webapp-web'\n"
                           "   Running containers: other-web")

    monkeypatch.setattr(slots, "resolve_container", _down)
    monkeypatch.setattr(slots, "_detected_runtime", lambda: "a-runtime")
    _store_slots([{"name": "webapp", "preset": "webapp_dev"}])

    reason = slots.slot_rows(cfg.load())[0].reason

    assert "\n" not in reason
    assert "Running containers: other-web" in reason


def test_no_runtime_on_the_host_is_a_named_reason(
    platform_root, presets_file, monkeypatch
):
    monkeypatch.setattr(slots, "_detected_runtime", lambda: None)
    _store_slots([{"name": "webapp", "preset": "webapp_dev"}])

    row = slots.slot_rows(cfg.load())[0]

    assert row.state == slots.SLOT_SERVICE_DOWN
    assert "no container runtime" in row.reason


def test_the_probe_answer_is_reused_within_its_ttl(
    platform_root, presets_file, service_up
):
    """One slot read repeatedly costs one query, not one per read.

    Deliberately *not* two slots sharing a service, which used to be this
    fixture: that shape is a collision fault now, so the second slot would
    never be probed and this would pass without the cache doing anything.
    """
    _store_slots([{"name": "a", "preset": "webapp_dev"}])

    slots.slot_rows(cfg.load())
    slots.slot_rows(cfg.load())
    slots.slot_rows(cfg.load())

    assert len(service_up) == 1


def test_an_expired_probe_answer_is_refreshed(
    platform_root, presets_file, service_up, monkeypatch
):
    _store_slots([{"name": "webapp", "preset": "webapp_dev"}])
    clock = _Clock()
    monkeypatch.setattr(slots, "time", clock)

    slots.slot_rows(cfg.load())
    clock.now += slots.PROBE_TTL_SECONDS + 1
    slots.slot_rows(cfg.load())

    assert len(service_up) == 2


def test_the_spawn_path_asks_for_a_fresh_answer(
    platform_root, presets_file, service_up
):
    """An action taken on the answer cannot afford a stale one."""
    _store_slots([{"name": "webapp", "preset": "webapp_dev"}])

    slots.slot_rows(cfg.load())
    slots.slot_status(cfg.load(), "webapp", cached=False)

    assert len(service_up) == 2


def test_distinct_services_are_cached_separately(
    platform_root, presets_file, service_up
):
    _store_slots([
        {"name": "a", "preset": "webapp_dev"},
        {"name": "b", "preset": "other_dev"},
    ])

    slots.slot_rows(cfg.load())

    assert sorted(service for _, service, _ in service_up) == [
        "other-web", "webapp-web"
    ]


# --- G5: runtime neutrality --------------------------------------------------

def test_the_probe_goes_through_the_shared_resolver_with_the_detected_runtime(
    platform_root, presets_file, service_up
):
    _store_slots([{"name": "webapp", "preset": "webapp_dev"}])

    slots.slot_rows(cfg.load())

    assert service_up == [("a-runtime", "webapp-web", False)]


def test_the_probe_does_not_announce(platform_root, presets_file, service_up):
    """A success line per slot per poll would bury the daemon's log."""
    _store_slots([{"name": "webapp", "preset": "webapp_dev"}])

    slots.slot_rows(cfg.load())

    assert all(announce is False for _, _, announce in service_up)


def test_resolve_container_still_announces_by_default(monkeypatch, capsys):
    """The interactive path is unchanged — the flag defaults to the old behaviour."""
    from lmer_cli import service

    monkeypatch.setattr(
        service, "_docker_ps", lambda runtime, filter_arg: [("cid123456789", "web")]
    )

    service.resolve_container("a-runtime", "web")

    assert "Resolved service 'web'" in capsys.readouterr().err


def test_slots_module_spells_no_runtime_literal_and_no_format_string():
    """A source guard, because the neutrality rule is easy to break by hand.

    The runtime reaches this module as a parameter and nowhere else; a literal
    naming one, or a private output-format string, would be this module having
    an opinion it is not allowed to have.
    """
    source = pathlib.Path(slots.__file__).read_text(encoding="utf-8")

    offenders = re.findall(r"docker|podman|--format", source, flags=re.IGNORECASE)

    assert offenders == []


# --- slot_status -------------------------------------------------------------

def test_slot_status_returns_none_for_a_name_nobody_declared(
    platform_root, presets_file, service_up
):
    """A typo'd slot must not resolve to something."""
    _store_slots([{"name": "webapp", "preset": "webapp_dev"}])

    assert slots.slot_status(cfg.load(), "wbeapp") is None


def test_slot_status_carries_the_facts_under_the_state(
    platform_root, presets_file, service_up
):
    """The spawn gate refuses in its own order, so it reads facts not a word."""
    _store_slots([{"name": "webapp", "preset": "typo_dev"}])
    _register("s-live", pid=os.getpid(), slot="webapp")

    status = slots.slot_status(cfg.load(), "webapp")

    # Occupied wins the display, and the configuration fault is still there.
    assert status.state == slots.SLOT_OCCUPIED
    assert status.occupant["session_id"] == "s-live"
    assert "not defined on this host" in status.unusable_reason


def test_two_live_sessions_on_one_slot_are_both_kept_and_warned_about(
    platform_root, presets_file, service_up, caplog
):
    _store_slots([{"name": "webapp", "preset": "webapp_dev"}])
    _register("s-one", pid=os.getpid(), slot="webapp")
    _register("s-two", pid=os.getpid(), slot="webapp")

    with caplog.at_level("WARNING", logger="lmer_platform.slots"):
        row = slots.slot_rows(cfg.load())[0]

    assert [h["session_id"] for h in row.occupants] == ["s-one", "s-two"]
    assert row.occupant["session_id"] == "s-one", "the first is still the one"
    assert "slot_double_occupancy" in caplog.text


def test_to_dict_is_the_row_the_payload_carries(
    platform_root, presets_file, service_up
):
    _store_slots([{"name": "webapp", "preset": "webapp_dev", "description": "Web app"}])

    assert slots.slot_rows(cfg.load())[0].to_dict() == {
        "name": "webapp",
        "preset": "webapp_dev",
        "description": "Web app",
        "service": "webapp-web",
        "state": "free",
        "reason": None,
        "occupant": None,
        "occupants": [],
        "service_occupants": [],
    }


# --- the resource is the service, not the name --------------------------------

def test_two_slots_on_one_service_do_not_both_grant(
    platform_root, presets_file, service_up
):
    """The finding this rule exists for: three slots, one dev container.

    Name-keyed exclusion let every slot resolving to one service read free and
    grant independently — reachable by a plain config edit, and exactly the
    "two agents against one database" the feature exists to prevent.
    """
    _store_slots([
        {"name": "a", "preset": "webapp_dev"},
        {"name": "b", "preset": "webapp_alt"},   # a different preset…
        {"name": "c", "preset": "webapp_dev"},   # …and the same one again
    ])

    rows = {row.definition.name: row for row in slots.slot_rows(cfg.load())}

    assert rows["a"].state == slots.SLOT_FREE
    assert [name for name, row in rows.items() if row.usable] == ["a"]
    for loser in ("b", "c"):
        assert rows[loser].state == slots.SLOT_MISCONFIGURED
        assert "already bound by slot 'a'" in rows[loser].reason


def test_the_first_declaration_wins_the_service(
    platform_root, presets_file, service_up
):
    """Order-stable, like the duplicate-name rule: the answer must not depend
    on how far down the file a reader got."""
    _store_slots([
        {"name": "second", "preset": "webapp_alt"},
        {"name": "first", "preset": "webapp_dev"},
    ])

    rows = {row.definition.name: row for row in slots.slot_rows(cfg.load())}

    assert rows["second"].usable
    assert not rows["first"].usable


def test_the_spawn_gate_applies_the_service_rule_too(
    platform_root, presets_file, service_up
):
    """slot_status resolves every definition, not just the one asked for —
    otherwise the gate would grant what the row refuses."""
    _store_slots([
        {"name": "a", "preset": "webapp_dev"},
        {"name": "b", "preset": "webapp_alt"},
    ])

    assert slots.slot_status(cfg.load(), "a").usable
    status = slots.slot_status(cfg.load(), "b")
    assert not status.usable
    assert "already bound by slot 'a'" in status.unusable_reason


def test_distinct_services_are_both_usable(platform_root, presets_file, service_up):
    """The rule must not fire on the ordinary two-stack host."""
    _store_slots([
        {"name": "a", "preset": "webapp_dev"},
        {"name": "b", "preset": "other_dev"},
    ])

    assert all(row.usable for row in slots.slot_rows(cfg.load()))


# --- a preset's args must not rebind the slot ---------------------------------

@pytest.mark.parametrize("args, flag", [
    (["--service", "other-web"], "--service"),
    (["--service=other-web"], "--service"),
    (["--checkout", "/srv/elsewhere"], "--checkout"),
    (["--checkout=/srv/elsewhere"], "--checkout"),
    (["--ports", "2", "--service", "other-web"], "--service"),
])
def test_a_preset_whose_args_override_its_binding_is_unusable(
    platform_root, tmp_path, monkeypatch, service_up, args, flag
):
    """Verified against the real CLI: `Preset.cli_tokens()` emits the preset's
    own `--service` first and appends `args`, and lmer re-parses
    `preset_tokens + argv` where argparse's last occurrence wins. The slot would
    probe, display and guard one service while the session ran against another.
    """
    path = tmp_path / "presets.json"
    path.write_text(json.dumps({
        "sneaky": {"checkout": "/srv/w", "service": "webapp-web", "args": args},
    }), encoding="utf-8")
    monkeypatch.setenv("LMER_PRESETS_FILE", str(path))
    _store_slots([{"name": "webapp", "preset": "sneaky"}])

    row = slots.slot_rows(cfg.load())[0]

    assert row.state == slots.SLOT_MISCONFIGURED
    assert flag in row.reason
    assert "overrides the preset's own value" in row.reason
    assert row.service is None


def test_the_rebinding_guard_matches_the_real_last_wins_parse(tmp_path):
    """The guard's premise, pinned against the code it is protecting against —
    if `cli_tokens()` or the parse order changed, this fails rather than the
    guard quietly protecting nothing."""
    from lmer_cli.cli import parse_args
    from lmer_cli.presets import Preset

    preset = Preset(
        name="sneaky", checkout="/srv/w", service="webapp-web",
        args=["--service", "other-web"],
    )
    namespace, _ = parse_args(preset.cli_tokens() + ["chat"])

    assert namespace.service == "other-web" != preset.service
    assert slots._rebinding_arg(preset) == "--service"


def test_harmless_preset_args_are_left_alone(
    platform_root, tmp_path, monkeypatch, service_up
):
    _store_slots([{"name": "webapp", "preset": "fine"}])
    path = tmp_path / "presets.json"
    path.write_text(json.dumps({
        "fine": {"checkout": "/srv/w", "service": "webapp-web",
                 "args": ["--ports", "2", "--verbose"]},
    }), encoding="utf-8")
    monkeypatch.setenv("LMER_PRESETS_FILE", str(path))

    assert slots.slot_rows(cfg.load())[0].usable


# --- a collision is visible on the surface ------------------------------------

def test_a_contended_slot_reports_every_holder(
    platform_root, presets_file, service_up
):
    """The race in _claim_slot is disclosed, not prevented — so the surface
    whose job is 'who has my dev service' must not name one and look settled."""
    _store_slots([{"name": "webapp", "preset": "webapp_dev"}])
    _register("s-one", pid=os.getpid(), slot="webapp")
    _register("s-two", pid=os.getpid(), slot="webapp")

    row = slots.slot_rows(cfg.load())[0]

    assert row.state == slots.SLOT_OCCUPIED
    assert row.contended
    assert [h["session_id"] for h in row.occupants] == ["s-one", "s-two"]
    assert row.to_dict()["occupants"] == list(row.occupants)
    # The single-holder reader still gets a sensible answer.
    assert row.occupant["session_id"] == "s-one"


def test_one_holder_is_not_contended(platform_root, presets_file, service_up):
    _store_slots([{"name": "webapp", "preset": "webapp_dev"}])
    _register("s-one", pid=os.getpid(), slot="webapp")

    row = slots.slot_rows(cfg.load())[0]

    assert not row.contended
    assert row.to_dict()["occupants"] == [row.occupant]


def test_a_collision_warns_once_per_change_not_once_per_poll(
    platform_root, presets_file, service_up, caplog
):
    """The payload is rebuilt every ten seconds for the life of an overlap, and
    a line repeated three hundred times an hour is a line operators filter."""
    _store_slots([{"name": "webapp", "preset": "webapp_dev"}])
    _register("s-one", pid=os.getpid(), slot="webapp")
    _register("s-two", pid=os.getpid(), slot="webapp")

    with caplog.at_level("WARNING", logger="lmer_platform.slots"):
        for _ in range(5):
            slots.slot_rows(cfg.load())

    assert caplog.text.count("slot_double_occupancy") == 1

    # A change in *who* is colliding is news again.
    _register("s-three", pid=os.getpid(), slot="webapp")
    with caplog.at_level("WARNING", logger="lmer_platform.slots"):
        slots.slot_rows(cfg.load())

    assert caplog.text.count("slot_double_occupancy") == 2


# --- the presets file the daemon never opened ---------------------------------

def test_no_presets_loaded_says_so_instead_of_blaming_the_name(
    platform_root, monkeypatch, service_up
):
    """`load_presets()` returns {} when the *daemon's* environment names no
    presets file, which made every slot read "preset 'X' is not defined" and
    sent the operator to inspect a file the daemon never opened."""
    monkeypatch.delenv("LMER_PRESETS_FILE", raising=False)
    _store_slots([{"name": "webapp", "preset": "webapp_dev"}])

    reason = slots.slot_rows(cfg.load())[0].reason

    assert "no presets are loaded on this host" in reason
    assert "LMER_PRESETS_FILE" in reason


def test_a_genuinely_unknown_name_still_says_that(
    platform_root, presets_file, service_up
):
    """The paired half: with presets loaded, a typo is still a typo."""
    _store_slots([{"name": "webapp", "preset": "nope"}])

    reason = slots.slot_rows(cfg.load())[0].reason

    assert "is not defined on this host" in reason
    assert "LMER_PRESETS_FILE" not in reason


# --- abbreviations cannot evade the rebinding guard ---------------------------

@pytest.mark.parametrize("args, flag", [
    # The two spellings iteration 1 covered…
    (["--service", "other-web"], "--service"),
    (["--service=other-web"], "--service"),
    (["--checkout", "/srv/elsewhere"], "--checkout"),
    (["--checkout=/srv/elsewhere"], "--checkout"),
    # …and the abbreviations that evaded it. `lmer` leaves allow_abbrev on and
    # nothing else shares the --se/--che prefixes, so every one of these rebinds
    # exactly as the full spelling does.
    (["--serv", "other-web"], "--service"),
    (["--serv=other-web"], "--service"),
    (["--se", "other-web"], "--service"),
    (["--servic=other-web"], "--service"),
    (["--check", "/srv/elsewhere"], "--checkout"),
    (["--che=/srv/elsewhere"], "--checkout"),
    (["--checkou", "/srv/elsewhere"], "--checkout"),
    # Position must not matter either.
    (["--ports", "2", "--se", "other-web"], "--service"),
])
def test_every_spelling_that_rebinds_is_caught(
    platform_root, tmp_path, monkeypatch, service_up, args, flag
):
    """Decided by parsing, not by matching token text — so the guard covers
    every abbreviation argparse accepts, including ones it may learn later."""
    path = tmp_path / "presets.json"
    path.write_text(json.dumps({
        "sneaky": {"checkout": "/srv/w", "service": "webapp-web", "args": args},
    }), encoding="utf-8")
    monkeypatch.setenv("LMER_PRESETS_FILE", str(path))
    _store_slots([{"name": "webapp", "preset": "sneaky"}])

    row = slots.slot_rows(cfg.load())[0]

    assert row.state == slots.SLOT_MISCONFIGURED, f"{args} evaded the guard"
    assert flag in row.reason
    assert row.service is None


@pytest.mark.parametrize("args", [
    ["--serv", "other-web"], ["--se", "other-web"], ["--che=/x"],
])
def test_the_abbreviations_really_do_rebind(args):
    """The premise, pinned against the real parser rather than asserted.

    If argparse ever stopped accepting these, this test would fail and the
    parametrisation above could be trimmed — rather than the guard quietly
    over-refusing forever.
    """
    from lmer_cli.cli import parse_args
    from lmer_cli.presets import Preset

    preset = Preset(
        name="sneaky", checkout="/srv/w", service="webapp-web", args=args,
    )
    namespace, _ = parse_args(preset.cli_tokens() + ["chat"])

    rebound = (
        namespace.service != preset.service
        or namespace.checkout != preset.checkout
    )
    assert rebound, f"{args} no longer rebinds; the guard may be over-refusing"
    assert slots._rebinding_arg(preset) is not None


def test_args_that_do_not_parse_make_the_slot_unusable(
    platform_root, tmp_path, monkeypatch, service_up
):
    """A preset lmer would exit 2 on cannot back a slot that reads free."""
    path = tmp_path / "presets.json"
    path.write_text(json.dumps({
        "broken": {"checkout": "/srv/w", "service": "webapp-web",
                   "args": ["--ports"]},
    }), encoding="utf-8")
    monkeypatch.setenv("LMER_PRESETS_FILE", str(path))
    _store_slots([{"name": "webapp", "preset": "broken"}])

    row = slots.slot_rows(cfg.load())[0]

    assert row.state == slots.SLOT_MISCONFIGURED
    assert "cannot parse" in row.reason


def test_the_rebinding_check_prints_nothing(
    platform_root, tmp_path, monkeypatch, service_up, capsys
):
    """argparse writes usage to stderr on a bad token, and the daemon rebuilds
    this payload every ten seconds — that must not reach its log."""
    path = tmp_path / "presets.json"
    path.write_text(json.dumps({
        "broken": {"checkout": "/srv/w", "service": "webapp-web",
                   "args": ["--ports"]},
    }), encoding="utf-8")
    monkeypatch.setenv("LMER_PRESETS_FILE", str(path))
    _store_slots([{"name": "webapp", "preset": "broken"}])

    slots.slot_rows(cfg.load())
    captured = capsys.readouterr()

    assert captured.out == "" and captured.err == ""


def test_the_rebinding_answer_is_memoised_per_args(
    platform_root, tmp_path, monkeypatch, service_up
):
    """`parse_args` builds a parser per call (~11ms); too much to spend per slot
    per poll. Content-addressed on the tokens, so no TTL is needed."""
    calls = []
    real = slots._binding_fault
    monkeypatch.setattr(
        slots, "_binding_fault",
        lambda preset, args: (calls.append(args), real(preset, args))[1],
    )
    path = tmp_path / "presets.json"
    path.write_text(json.dumps({
        "p": {"checkout": "/srv/w", "service": "webapp-web",
              "args": ["--ports", "2"]},
    }), encoding="utf-8")
    monkeypatch.setenv("LMER_PRESETS_FILE", str(path))
    _store_slots([{"name": "webapp", "preset": "p"}])

    for _ in range(4):
        slots.slot_rows(cfg.load())

    assert len(calls) == 1


# --- a hot presets edit cannot flip past the exclusion ------------------------

def _write_presets(path, monkeypatch, spec):
    path.write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setenv("LMER_PRESETS_FILE", str(path))
    slots.clear_probe_cache()


def test_fixing_an_earlier_slot_cannot_take_a_service_from_a_live_session(
    platform_root, tmp_path, monkeypatch, service_up
):
    """The residual the reviewer found, end to end.

    The one-service-one-slot rule is derived from a file that is hot by design,
    so it alone could be flipped: slot `a` unusable while `b` takes the service,
    then the operator fixes `a` — the advertised recovery path — and `a` becomes
    the service's first resolver, with no occupant under its own name.
    """
    path = tmp_path / "presets.json"
    _write_presets(path, monkeypatch, {
        "clean": {"checkout": "/srv/w", "service": "web"},
        "p_a": {"checkout": "/srv/w"},          # sets no service: unusable
    })
    _store_slots([
        {"name": "a", "preset": "p_a"},
        {"name": "b", "preset": "clean"},
    ])
    rows = {r.definition.name: r for r in slots.slot_rows(cfg.load())}
    assert rows["a"].state == slots.SLOT_MISCONFIGURED
    assert rows["b"].usable

    # A session takes b, recording the preset it launched with (as spawn does).
    _register("s-b", pid=os.getpid(), slot="b")
    registry.update("s-b", task={"preset": "clean"})

    # The operator fixes a's preset while that session is still running.
    _write_presets(path, monkeypatch, {
        "clean": {"checkout": "/srv/w", "service": "web"},
        "p_a": {"checkout": "/srv/w", "service": "web"},
    })

    rows = {r.definition.name: r for r in slots.slot_rows(cfg.load())}
    assert rows["a"].state == slots.SLOT_OCCUPIED, (
        "the fixed slot must not read free while a live session holds its service"
    )
    assert not rows["a"].usable
    assert "s-b" in rows["a"].reason and "web" in rows["a"].reason

    status = slots.slot_status(cfg.load(), "a", cached=False)
    assert not status.usable
    assert status.service_occupants


def test_service_occupancy_comes_from_what_the_session_launched_with(
    platform_root, tmp_path, monkeypatch, service_up
):
    """Measured, not predicted: the slot definition can be repointed or deleted
    under a running session, so the entry's own preset is the authority."""
    path = tmp_path / "presets.json"
    _write_presets(path, monkeypatch, {
        "one": {"checkout": "/srv/w", "service": "web-one"},
        "two": {"checkout": "/srv/w", "service": "web-two"},
    })
    _store_slots([{"name": "a", "preset": "one"}, {"name": "b", "preset": "two"}])

    # A session took slot b when b meant web-two…
    _register("s-b", pid=os.getpid(), slot="b")
    registry.update("s-b", task={"preset": "two", "slot_service": "web-two"})
    # …and the operator then repoints b at web-one, where a already lives.
    _write_presets(path, monkeypatch, {
        "one": {"checkout": "/srv/w", "service": "web-one"},
        "two": {"checkout": "/srv/w", "service": "web-one"},
    })

    rows = {r.definition.name: r for r in slots.slot_rows(cfg.load())}
    # a keeps web-one: the live session is bound to web-two, whatever the file
    # now says slot b means.
    assert rows["a"].usable, (
        "a session's service must come from the preset it launched with"
    )


def test_a_holder_of_another_slot_does_not_block_a_different_service(
    platform_root, presets_file, service_up
):
    """The check must not over-refuse the ordinary two-stack host."""
    _store_slots([
        {"name": "a", "preset": "webapp_dev"},
        {"name": "b", "preset": "other_dev"},
    ])
    _register("s-b", pid=os.getpid(), slot="b")
    registry.update("s-b", task={"preset": "other_dev"})

    rows = {r.definition.name: r for r in slots.slot_rows(cfg.load())}

    assert rows["a"].usable
    assert rows["b"].state == slots.SLOT_OCCUPIED


def test_the_payload_carries_the_foreign_service_holders(
    platform_root, tmp_path, monkeypatch, service_up
):
    path = tmp_path / "presets.json"
    _write_presets(path, monkeypatch, {
        "clean": {"checkout": "/srv/w", "service": "web"},
        "p_a": {"checkout": "/srv/w", "service": "web"},
    })
    _store_slots([{"name": "a", "preset": "p_a"}, {"name": "b", "preset": "clean"}])
    _register("s-b", pid=os.getpid(), slot="b")
    registry.update("s-b", task={"preset": "clean"})

    row = {r.definition.name: r for r in slots.slot_rows(cfg.load())}["a"]

    assert row.to_dict()["service_occupants"] == [
        {"slot": "b", "session_id": "s-b", "host": "h", "project": "p",
         "slug": "s-b"},
    ]


# --- which cause emptied the presets ------------------------------------------

def test_a_broken_presets_path_is_not_blamed_on_an_unset_variable(
    platform_root, tmp_path, monkeypatch, service_up
):
    """`load_presets()` answers empty for several causes; naming the wrong one
    sends the operator to check the thing that is right."""
    monkeypatch.setenv("LMER_PRESETS_FILE", str(tmp_path / "gone.json"))
    _store_slots([{"name": "webapp", "preset": "webapp_dev"}])

    reason = slots.slot_rows(cfg.load())[0].reason

    assert "gone.json" in reason
    assert "missing, unreadable" in reason
    assert "must set" not in reason


def test_invalid_json_is_reported_as_a_broken_file_not_an_unset_variable(
    platform_root, tmp_path, monkeypatch, service_up
):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("LMER_PRESETS_FILE", str(path))
    _store_slots([{"name": "webapp", "preset": "webapp_dev"}])

    reason = slots.slot_rows(cfg.load())[0].reason

    assert "bad.json" in reason
    assert "must set" not in reason


# --- the guard judges outcomes, quietly ---------------------------------------

def _preset_file(tmp_path, monkeypatch, spec):
    path = tmp_path / "presets.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setenv("LMER_PRESETS_FILE", str(path))
    slots.clear_probe_cache()
    return path


@pytest.mark.parametrize("args, flag", [
    # An empty value is a rebinding, and the sharpest case: `lmer` reads service
    # mode off this value's truthiness, so the session would hold a service slot
    # while running in ordinary mode.
    (["--service", ""], "--service"),
    (["--service="], "--service"),
    (["--se="], "--service"),
    (["--checkout", ""], "--checkout"),
])
def test_an_emptied_binding_is_a_rebinding(
    platform_root, tmp_path, monkeypatch, service_up, args, flag
):
    _preset_file(tmp_path, monkeypatch, {
        "sneaky": {"checkout": "/srv/w", "service": "webapp-web", "args": args},
    })
    _store_slots([{"name": "webapp", "preset": "sneaky"}])

    row = slots.slot_rows(cfg.load())[0]

    assert row.state == slots.SLOT_MISCONFIGURED, f"{args} evaded the guard"
    assert flag in row.reason


def test_an_emptied_service_really_would_leave_ordinary_mode(tmp_path):
    """The premise, pinned: this is why an empty value is not "no rebinding"."""
    from lmer_cli.cli import parse_args
    from lmer_cli.presets import Preset

    preset = Preset(
        name="sneaky", checkout="/srv/w", service="webapp-web",
        args=["--service", ""],
    )
    namespace, _ = parse_args(preset.cli_tokens(), quiet=True)

    assert namespace.service == ""
    assert not namespace.service, (
        "lmer decides service mode on truthiness, so this session would run "
        "in ordinary mode while holding a service slot"
    )


def test_restating_the_same_binding_is_not_a_fault(
    platform_root, tmp_path, monkeypatch, service_up
):
    """Redundant config, not a broken slot — only an outcome comparison can
    tell the two apart."""
    _preset_file(tmp_path, monkeypatch, {
        "verbose_but_fine": {
            "checkout": "/srv/w", "service": "webapp-web",
            "args": ["--service", "webapp-web", "--checkout", "/srv/w"],
        },
    })
    _store_slots([{"name": "webapp", "preset": "verbose_but_fine"}])

    row = slots.slot_rows(cfg.load())[0]

    assert row.usable, row.reason
    assert row.service == "webapp-web"


@pytest.mark.parametrize("args", [
    ["--no-such-flag"],
    ["stray-positional"],
    ["--ports", "2", "stray"],
    ["--exec", "--", "cmd"],       # the CLI refuses `--` in preset args
    ["--ports"],
])
def test_args_lmer_would_refuse_make_the_slot_unusable(
    platform_root, tmp_path, monkeypatch, service_up, args
):
    """Mirrors `_resolve_and_apply_preset`'s own three rules, so a slot cannot
    read usable where lmer would exit 2 — every spawn into it would die at
    launch with nothing on the slot to explain why."""
    _preset_file(tmp_path, monkeypatch, {
        "bad": {"checkout": "/srv/w", "service": "webapp-web", "args": args},
    })
    _store_slots([{"name": "webapp", "preset": "bad"}])

    row = slots.slot_rows(cfg.load())[0]

    assert row.state == slots.SLOT_MISCONFIGURED, f"{args} read usable"
    assert "cannot parse" in row.reason


def test_the_guard_never_swaps_a_process_global_stream(
    platform_root, tmp_path, monkeypatch, service_up
):
    """The major from iteration 3.

    `contextlib.redirect_stdout` saves and restores the process-global stream
    and is not thread-safe; two overlapping calls on the threadpool route that
    serves GET /api/state could interleave their save/restore and leave
    `sys.stdout` an in-memory buffer for the life of the daemon. The parser
    refusing quietly at the source removes the class rather than narrowing it.
    """
    import sys

    _preset_file(tmp_path, monkeypatch, {
        "bad": {"checkout": "/srv/w", "service": "webapp-web",
                "args": ["--ports"]},
    })
    _store_slots([{"name": "webapp", "preset": "bad"}])
    before_out, before_err = sys.stdout, sys.stderr

    slots.slot_rows(cfg.load())

    assert sys.stdout is before_out and sys.stderr is before_err
    # Structural, not textual: the docstring above explains *why* the redirect
    # is gone and names it, so matching the word would fail on its own
    # explanation. The import and the call are what would actually come back.
    source = pathlib.Path(slots.__file__).read_text(encoding="utf-8")
    assert "import contextlib" not in source, "contextlib is imported again"
    assert not re.search(r"redirect_std(out|err)\s*\(", source), (
        "the module swaps a process-global stream again"
    )


def test_concurrent_guard_calls_leave_the_streams_alone(
    platform_root, tmp_path, monkeypatch, service_up
):
    """The interleave itself, driven from two threads with uncached keys."""
    import sys
    import threading

    _preset_file(tmp_path, monkeypatch, {
        f"p{n}": {"checkout": "/srv/w", "service": f"svc-{n}",
                  "args": ["--ports"] if n % 2 else ["--nope"]}
        for n in range(8)
    })
    _store_slots([{"name": f"s{n}", "preset": f"p{n}"} for n in range(8)])
    before_out, before_err = sys.stdout, sys.stderr

    errors = []

    def read():
        try:
            slots.slot_rows(cfg.load(), cached=False)
        except Exception as exc:      # pragma: no cover - a failure is the point
            errors.append(exc)

    threads = [threading.Thread(target=read) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sys.stdout is before_out and sys.stderr is before_err


# --- a legacy entry widens its block, never moves it --------------------------

def test_a_legacy_entry_blocks_both_candidate_services(
    platform_root, tmp_path, monkeypatch, service_up
):
    """An entry written before `task.slot_service` existed has its service
    inferred. Blocking the first candidate that matched *moved* the block when
    the slot was repointed — leaving the service actually in use reading free.
    """
    _preset_file(tmp_path, monkeypatch, {
        "pa": {"checkout": "/srv/w", "service": "X"},
        "pb": {"checkout": "/srv/w", "service": "X"},
        "pc": {"checkout": "/srv/w", "service": "Z"},
    })
    _store_slots([{"name": "a", "preset": "pa"}, {"name": "b", "preset": "pb"}])
    # No slot_service: the legacy shape.
    _register("s-legacy", pid=os.getpid(), slot="b")
    registry.update("s-legacy", task={"preset": "pb"})

    # The slot is repointed at a preset on a different service, mid-session.
    _store_slots([{"name": "a", "preset": "pa"}, {"name": "b", "preset": "pc"}])
    slots.clear_probe_cache()

    rows = {r.definition.name: r for r in slots.slot_rows(cfg.load())}

    assert not rows["a"].usable, (
        "service X is what the legacy session holds; it must stay blocked"
    )
    assert rows["a"].service_occupants


def test_a_recorded_entry_blocks_exactly_one_service(
    platform_root, tmp_path, monkeypatch, service_up
):
    """The paired half: with the service recorded there is nothing to infer, so
    the widening must not happen and an unrelated slot stays usable."""
    _preset_file(tmp_path, monkeypatch, {
        "pa": {"checkout": "/srv/w", "service": "X"},
        "pb": {"checkout": "/srv/w", "service": "Y"},
    })
    _store_slots([{"name": "a", "preset": "pa"}, {"name": "b", "preset": "pb"}])
    _register("s-b", pid=os.getpid(), slot="b")
    registry.update("s-b", task={"preset": "pb", "slot_service": "Y"})

    rows = {r.definition.name: r for r in slots.slot_rows(cfg.load())}

    assert rows["a"].usable
    assert rows["b"].state == slots.SLOT_OCCUPIED


def test_a_valid_presets_file_with_no_entries_is_not_called_malformed(
    platform_root, tmp_path, monkeypatch, service_up
):
    """`load_presets()` also answers empty for a well-formed file defining
    nothing usable, so the reason must not assert only the other causes."""
    _preset_file(tmp_path, monkeypatch, {})
    _store_slots([{"name": "webapp", "preset": "webapp_dev"}])

    reason = slots.slot_rows(cfg.load())[0].reason

    assert "defines no valid entries" in reason
