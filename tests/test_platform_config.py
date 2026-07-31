"""Tests for platform config and the shared secret (issue #141, slice M1 / T3).

The properties that matter: resolution order is override > env > file > default,
a bad bind port fails loudly rather than silently landing somewhere else, the
secret never enters config.json and is never briefly world-readable, and a
corrupt config leaves the daemon bootable.
"""

import inspect
import json
import os
import stat

import pytest

from lmer_platform import config as cfg
from lmer_platform import store
from tests.conftest import strip_lmer_env


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    for name in (
        cfg.ENV_BIND_ADDRESS, cfg.ENV_BIND_PORT, cfg.ENV_SECRET_FILE,
        cfg.ENV_WORK_REPO_MIRROR, cfg.ENV_AUTONOMOUS, cfg.ENV_WORK_REPO,
        cfg.ENV_WORK_REPO_FORGE,
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


# --- defaults ---------------------------------------------------------------

def test_defaults_bind_loopback(platform_root):
    config = cfg.load()
    assert config.bind_address == "127.0.0.1"
    assert config.bind_port == cfg.DEFAULT_BIND_PORT
    assert config.is_loopback is True
    assert config.max_followup_rounds == 5
    assert config.autonomous_default is False
    assert config.park_idle_side is False
    assert config.work_repo_forge is None, (
        "unset is the default: detect the work repo's host, then assume GitLab"
    )


def test_default_port_avoids_existing_lmer_ranges():
    """Supervisor FastAPI uses 8700-8799 and port passthrough 8800-8899."""
    assert not 8700 <= cfg.DEFAULT_BIND_PORT <= 8899


def test_the_default_worker_cap_is_the_one_the_operator_raised_it_to(platform_root):
    """Eight, from a live fleet: four was a queue on a host that was not busy.

    Pinned as a number rather than left to the constant, because this one is a
    *decision* — an operator's reading of what a machine can bear — and the
    fleet view draws its occupancy against it. A silent drop back to four is a
    host that starts refusing spawns at half the load it was configured for.
    """
    assert cfg.DEFAULT_MAX_CONCURRENT_SESSIONS == 8
    assert cfg.load().max_concurrent_sessions == 8


def test_the_worker_cap_says_what_it_counts_beside_its_own_number(platform_root):
    """The cap's own comment is where the off-by-one lives or dies (T75/T97).

    ``max_concurrent_sessions`` bounds workers and the orchestrating session holds a
    slot beside them, so the sentence next to the default has to name a *worker*
    count and the count it is compared against. Raising the number meant correcting
    that prose too, and the silent failure is a comment left quoting the number the
    constant used to be — the one thing here an operator reads before editing their
    config.
    """
    source = inspect.getsource(cfg)
    block = source[:source.index("DEFAULT_MAX_CONCURRENT_SESSIONS =")]
    comment = block[block.rindex("#: What ``max_concurrent_sessions``"):]
    # As one line of prose: the sentence wraps, so a phrase that has to be there
    # is otherwise split across two comment markers.
    prose = " ".join(word for word in comment.split() if word != "#:")
    assert "_live_worker_count" in prose, (
        "the default no longer says which count it is compared against, which is "
        "the whole of what makes it a worker cap rather than a session cap"
    )
    assert "runs eight workers" in prose, (
        f"the prose beside the default no longer describes what it grants: {prose!r}"
    )


def test_derived_paths_follow_the_state_dir(platform_root):
    config = cfg.load()
    assert config.secret_path == platform_root / "secret"
    assert config.mirror_path == platform_root / "work"


def test_explicit_paths_win_over_derived(platform_root, tmp_path):
    config = cfg.PlatformConfig(
        secret_file=str(tmp_path / "s"), work_repo_mirror=str(tmp_path / "m")
    )
    assert config.secret_path == tmp_path / "s"
    assert config.mirror_path == tmp_path / "m"


def test_base_url_brackets_ipv6(platform_root):
    config = cfg.PlatformConfig(bind_address="::1", bind_port=8600)
    assert config.base_url == "http://[::1]:8600"
    assert config.is_loopback is True


# --- resolution order -------------------------------------------------------

def test_file_values_are_loaded(platform_root):
    cfg.save(cfg.PlatformConfig(bind_address="0.0.0.0", bind_port=9100))
    config = cfg.load()
    assert config.bind_address == "0.0.0.0"
    assert config.bind_port == 9100
    assert config.is_loopback is False


def test_env_overrides_file(platform_root, monkeypatch):
    cfg.save(cfg.PlatformConfig(bind_address="0.0.0.0", bind_port=9100))
    monkeypatch.setenv(cfg.ENV_BIND_ADDRESS, "10.0.0.5")
    monkeypatch.setenv(cfg.ENV_BIND_PORT, "9200")

    config = cfg.load()
    assert config.bind_address == "10.0.0.5"
    assert config.bind_port == 9200


def test_explicit_override_beats_env(platform_root, monkeypatch):
    monkeypatch.setenv(cfg.ENV_BIND_PORT, "9200")
    assert cfg.load({"bind_port": 9300}).bind_port == 9300


def test_override_of_none_does_not_clobber(platform_root, monkeypatch):
    """CLI flags arrive as None when unset; they must not erase env or file."""
    monkeypatch.setenv(cfg.ENV_BIND_PORT, "9200")
    assert cfg.load({"bind_port": None}).bind_port == 9200


def test_unknown_override_is_rejected(platform_root):
    with pytest.raises(cfg.ConfigError, match="unknown config override"):
        cfg.load({"bind_prot": 9300})


def test_work_repo_url_falls_back_to_lmer_work_repo(platform_root, monkeypatch):
    monkeypatch.setenv(cfg.ENV_WORK_REPO, "git@git.example.com:agents/work.git")
    assert cfg.load().work_repo_url == "git@git.example.com:agents/work.git"


def test_config_file_work_repo_url_wins_over_env(platform_root, monkeypatch):
    cfg.save(cfg.PlatformConfig(work_repo_url="https://git.example.com/a/work.git"))
    monkeypatch.setenv(cfg.ENV_WORK_REPO, "git@git.example.com:other/work.git")
    assert cfg.load().work_repo_url == "https://git.example.com/a/work.git"


def test_work_repo_forge_resolves_override_over_env_over_file(
    platform_root, monkeypatch
):
    """T86's knob follows the module's one resolution order, because an operator who
    exported it and then edited config.json has to be able to predict which won."""
    cfg.save(cfg.PlatformConfig(work_repo_forge="none"))
    assert cfg.load().work_repo_forge == "none"

    monkeypatch.setenv(cfg.ENV_WORK_REPO_FORGE, "github")
    assert cfg.load().work_repo_forge == "github"

    assert cfg.load({"work_repo_forge": "gitlab"}).work_repo_forge == "gitlab"
    assert cfg.load({"work_repo_forge": None}).work_repo_forge == "github", (
        "an unset flag must not erase the export"
    )


@pytest.mark.parametrize("value", ["GitLab", "  none  ", "GITHUB"])
def test_work_repo_forge_is_case_folded_rather_than_refused(platform_root, value):
    """A capitalised forge name is the operator saying the right thing, so it is
    normalised — refusing it would be a bad afternoon over a shift key."""
    assert cfg.load({"work_repo_forge": value}).work_repo_forge == value.strip().lower()


def test_blank_work_repo_forge_reads_as_unset(platform_root):
    """Blank is unset everywhere else in this module (``_env_str``), so a cleared
    field in config.json means "detect it" rather than being a refused value."""
    assert cfg.load({"work_repo_forge": "   "}).work_repo_forge is None


@pytest.mark.parametrize("value", ["bitbucket", "gitea", "off", "true", 3])
def test_unknown_work_repo_forge_is_refused_by_name(platform_root, value):
    """Loudly, and naming the three: a misspelling would otherwise fall back to
    detection and leave the operator staring at the links they set this to change."""
    with pytest.raises(cfg.ConfigError, match="work_repo_forge must be one of") as exc:
        cfg.load({"work_repo_forge": value})

    for name in cfg.WORK_REPO_FORGE_VALUES:
        assert name in str(exc.value)


def test_the_forge_values_are_the_url_builders_own_names(platform_root):
    """One definition of "gitlab": what this knob accepts has to be exactly what
    ``forge_web_url`` can build paths for, or a valid setting would build no link."""
    from work_repo import git_ops

    assert cfg.WORK_REPO_FORGE_VALUES == (
        git_ops.FORGE_GITLAB, git_ops.FORGE_GITHUB, cfg.WORK_REPO_FORGE_NONE,
    )
    assert git_ops.forge_web_url(
        "https://git.example.com/g/w", "main", "f.md", forge=cfg.WORK_REPO_FORGE_NONE
    ) is None


def test_autonomous_env_uses_bool_parsing(platform_root, monkeypatch):
    monkeypatch.setenv(cfg.ENV_AUTONOMOUS, "true")
    assert cfg.load().autonomous_default is True
    monkeypatch.setenv(cfg.ENV_AUTONOMOUS, "no")
    assert cfg.load().autonomous_default is False


def test_blank_env_is_treated_as_unset(platform_root, monkeypatch):
    cfg.save(cfg.PlatformConfig(bind_address="0.0.0.0"))
    monkeypatch.setenv(cfg.ENV_BIND_ADDRESS, "   ")
    assert cfg.load().bind_address == "0.0.0.0"


# --- validation -------------------------------------------------------------

def test_bad_env_port_raises_rather_than_falling_back(platform_root, monkeypatch):
    """A mistyped port must not quietly bind somewhere unexpected."""
    monkeypatch.setenv(cfg.ENV_BIND_PORT, "not-a-port")
    with pytest.raises(cfg.ConfigError, match="is not an integer"):
        cfg.load()


@pytest.mark.parametrize("port", [0, -1, 65536, 99999])
def test_out_of_range_port_rejected(platform_root, port):
    with pytest.raises(cfg.ConfigError, match="outside 1-65535"):
        cfg.load({"bind_port": port})


def test_non_integer_port_rejected(platform_root):
    with pytest.raises(cfg.ConfigError, match="must be an integer"):
        cfg.load({"bind_port": "8600"})


def test_empty_bind_address_rejected(platform_root):
    with pytest.raises(cfg.ConfigError, match="bind_address"):
        cfg.load({"bind_address": "  "})


@pytest.mark.parametrize(
    "field", ["work_repo_pull_interval", "max_concurrent_sessions",
              "max_followup_rounds"]
)
@pytest.mark.parametrize("value", [0, -3, True, "2"])
def test_positive_integer_fields_validated(platform_root, field, value):
    with pytest.raises(cfg.ConfigError, match="positive integer"):
        cfg.load({field: value})


def test_with_overrides_revalidates(platform_root):
    config = cfg.load()
    assert cfg.with_overrides(config, bind_port=9400).bind_port == 9400
    with pytest.raises(cfg.ConfigError):
        cfg.with_overrides(config, bind_port=0)


# --- persistence ------------------------------------------------------------

def test_save_writes_only_known_fields(platform_root):
    cfg.save(cfg.PlatformConfig(bind_port=9100))
    stored = json.loads(cfg.config_path().read_text(encoding="utf-8"))
    extra = set(stored) - {f for f in cfg.PlatformConfig().to_dict()} - {"schema", "updated"}
    assert extra == set()


def test_save_rejects_non_config_objects(platform_root):
    with pytest.raises(cfg.ConfigError, match="expects a PlatformConfig"):
        cfg.save({"bind_port": 9100})


def test_unknown_keys_in_file_are_ignored(platform_root):
    """A config written by a newer version must stay loadable."""
    store.write_json(cfg.config_path(), {"bind_port": 9100, "future_knob": True})
    assert cfg.load().bind_port == 9100


# --- the cap that could not be enforced (T75) --------------------------------
#
# ``max_concurrent_assistant_spawns`` was loaded, validated, persisted and served
# under ``GET /api/state``, and nothing read it: spec §6.4/§8.2 meant it to bound
# the sessions the *assistant* initiates, which needs a spawn to be attributable to
# the assistant, and there is one shared secret with no per-caller identity behind
# it. Deleted rather than served, because a number an operator can lower and
# nothing reads reads as a control.

def test_the_unenforceable_assistant_spawn_cap_is_gone(platform_root):
    """Field, validation and default all, not the field alone.

    A retired setting that still validated would still be a setting: ``load``
    raising on it would tell an operator it means something.
    """
    assert not hasattr(cfg.PlatformConfig(), "max_concurrent_assistant_spawns")
    assert not hasattr(cfg, "DEFAULT_MAX_CONCURRENT_ASSISTANT_SPAWNS")
    assert cfg.load({"max_concurrent_sessions": 2}).max_concurrent_sessions == 2
    with pytest.raises(cfg.ConfigError, match="unknown config override"):
        cfg.load({"max_concurrent_assistant_spawns": 2})


def test_the_served_config_block_no_longer_advertises_it(platform_root):
    """``GET /api/state``'s config block is the copy an operator actually reads."""
    from lmer_platform import api

    served = api._config_summary(cfg.load(), {"url": None})
    assert "max_concurrent_assistant_spawns" not in served
    assert served["max_concurrent_sessions"] == cfg.DEFAULT_MAX_CONCURRENT_SESSIONS


def test_a_config_written_before_the_cap_was_retired_still_loads(platform_root):
    """The upgrade path, and the one thing that must not break for a live host.

    A ``config.json`` on disk carries the retired key, and the daemon that reads it
    is the new one. It loads on the same rule that keeps a file written by a
    *newer* build loadable — ``load`` keeps known fields and ignores the rest — so
    this is a verification rather than an assumption, and the assertion is that the
    *other* settings in that file still arrive.
    """
    store.write_json(cfg.config_path(), {
        "bind_port": 9100,
        "max_concurrent_sessions": 3,
        "max_concurrent_assistant_spawns": 2,
    })

    config = cfg.load()

    assert config.bind_port == 9100
    assert config.max_concurrent_sessions == 3
    assert "max_concurrent_assistant_spawns" not in config.to_dict()


def test_corrupt_config_leaves_the_daemon_bootable(platform_root, caplog):
    """A daemon that will not start cannot be reconfigured through its own UI."""
    cfg.config_path().parent.mkdir(parents=True, exist_ok=True)
    cfg.config_path().write_text("{not json", encoding="utf-8")

    config = cfg.load()
    assert config.bind_port == cfg.DEFAULT_BIND_PORT
    assert any("platform_config_unreadable" in r.message for r in caplog.records)


# --- secret -----------------------------------------------------------------

def test_read_secret_absent_is_none(platform_root):
    assert cfg.read_secret(cfg.load()) is None


def test_ensure_secret_creates_strong_secret_mode_0600(platform_root):
    config = cfg.load()
    token = cfg.ensure_secret(config)

    assert len(token) >= 32
    mode = stat.S_IMODE(config.secret_path.stat().st_mode)
    assert mode == 0o600, f"secret file mode is {mode:o}"


def test_ensure_secret_is_idempotent(platform_root):
    config = cfg.load()
    assert cfg.ensure_secret(config) == cfg.ensure_secret(config)


def test_read_secret_strips_trailing_newline(platform_root):
    config = cfg.load()
    config.secret_path.parent.mkdir(parents=True, exist_ok=True)
    config.secret_path.write_text("hunter2\n", encoding="utf-8")
    assert cfg.read_secret(config) == "hunter2"


def test_empty_secret_file_reads_as_missing(platform_root):
    config = cfg.load()
    config.secret_path.parent.mkdir(parents=True, exist_ok=True)
    config.secret_path.write_text("\n  \n", encoding="utf-8")
    assert cfg.read_secret(config) is None


def test_secret_never_lands_in_config_json(platform_root):
    config = cfg.load()
    token = cfg.ensure_secret(config)
    cfg.save(config)
    assert token not in cfg.config_path().read_text(encoding="utf-8")


def test_ensure_secret_tightens_permissive_preexisting_file(platform_root):
    config = cfg.load()
    config.secret_path.parent.mkdir(parents=True, exist_ok=True)
    config.secret_path.write_text("", encoding="utf-8")
    config.secret_path.chmod(0o644)

    cfg.ensure_secret(config)
    assert stat.S_IMODE(config.secret_path.stat().st_mode) == 0o600


def test_read_secret_warns_when_permissive(platform_root, caplog):
    config = cfg.load()
    cfg.ensure_secret(config)
    config.secret_path.chmod(0o644)

    cfg.read_secret(config)
    assert any("platform_secret_permissive" in r.message for r in caplog.records)


def test_secret_file_env_override(platform_root, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere" / "sec"
    monkeypatch.setenv(cfg.ENV_SECRET_FILE, str(elsewhere))
    config = cfg.load()
    cfg.ensure_secret(config)
    assert elsewhere.is_file()


def test_read_secret_reports_unreadable_file(platform_root, monkeypatch):
    config = cfg.load()
    cfg.ensure_secret(config)

    def boom(*_args, **_kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr("pathlib.Path.read_text", boom)
    with pytest.raises(cfg.ConfigError, match="cannot read secret file"):
        cfg.read_secret(config)


# --- binding notice ---------------------------------------------------------

def test_binding_notice_loopback_mentions_proxy(platform_root):
    notice = cfg.binding_notice(cfg.load())
    assert "loopback only" in notice
    assert "http://127.0.0.1" in notice


def test_binding_notice_network_bind_warns_plaintext(platform_root):
    notice = cfg.binding_notice(cfg.load({"bind_address": "0.0.0.0"}))
    assert "PLAINTEXT" in notice
    assert "nginx" in notice


def test_binding_notice_names_env_as_source(platform_root, monkeypatch):
    """An export shadowing the UI's value must not be invisible."""
    monkeypatch.setenv(cfg.ENV_BIND_ADDRESS, "10.0.0.5")
    assert "bind from environment" in cfg.binding_notice(cfg.load())


def test_binding_notice_names_file_as_source(platform_root):
    cfg.save(cfg.PlatformConfig(bind_address="0.0.0.0"))
    assert "bind from config.json" in cfg.binding_notice(cfg.load())


def test_binding_notice_names_default_as_source(platform_root):
    assert "bind from default" in cfg.binding_notice(cfg.load())


# --- the docker bridge gateway probe ----------------------------------------
#
# `container_base_url`'s rules are asserted in tests/test_platform_assistant.py,
# beside the session that is handed their answer. What lives here is the probe
# underneath rule 3: which command is asked first, what each output shape parses
# to, and the promise that "no answer" comes back as None rather than a guess.
#
# Every one of these injects `run`, and not only because a probe has to be
# assertable on a CI host with no docker: this container HAS the docker CLI and
# no daemon behind it, so a test that let the real probe run would assert the
# failure path while looking like it asserted the derivation.

#: `docker network inspect bridge --format '{{range .IPAM.Config}}{{.Gateway}}…'`
#: on a stock host. Captured rather than invented — the format string emits one
#: line per IPAM entry and nothing else.
INSPECT_DEFAULT = "172.17.0.1\n"
#: A daemon.json that moved the subnet (`bip`, or default-address-pools). The
#: case a hardcoded 172.17.0.1 would get confidently wrong.
INSPECT_CUSTOM_BIP = "10.200.0.1\n"
#: A daemon with ip6tables on: two IPAM entries, v4 first.
INSPECT_DUAL_STACK = "172.17.0.1\nfd00:dead:beef::1\n"
#: `ip -4 -oneline addr show docker0`. The `\` and run of spaces are what
#: -oneline does to the continuation line, and `brd` is the trap: a broadcast
#: address parses as a perfectly good routable IPv4.
IP_ADDR_DOCKER0 = (
    "3: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\\"
    "       valid_lft forever preferred_lft forever\n"
)


def scripted_probe(*answers):
    """A ``run`` seam answering *answers* in order, recording what it was asked.

    Recording the argv is half the point: the order of the two probes is a
    decision this module documents, and "it returned the right address" would
    pass with the two swapped.
    """
    calls = []
    replies = list(answers)

    def run(args):
        calls.append(list(args))
        return replies.pop(0) if replies else ""

    run.calls = calls
    return run


def test_the_gateway_comes_from_the_runtime_rather_than_a_constant(platform_root):
    """A host whose daemon.json moved the subnet is the whole reason to ask.

    ``172.17.0.1`` is the answer on a stock host and a wrong answer on a
    configured one, so the address is read from the runtime every time.
    """
    run = scripted_probe(INSPECT_CUSTOM_BIP)
    assert cfg._docker_bridge_gateway(run=run) == "10.200.0.1"
    assert "172.17.0.1" not in str(run.calls), "no address may be baked into the probe"


def test_the_daemon_is_asked_before_the_interface(platform_root):
    """Order, stated as a value: the daemon's answer wins even when both answer.

    A daemon started on another bridge (``-b br0``) can leave a docker0 whose
    address is not the gateway, so the authoritative source goes first — and the
    second command must not run at all once the first has answered.
    """
    run = scripted_probe(INSPECT_CUSTOM_BIP, IP_ADDR_DOCKER0)
    assert cfg._docker_bridge_gateway(run=run) == "10.200.0.1"
    assert len(run.calls) == 1, "the interface must not be read once docker answered"
    first = run.calls[0]
    assert first[:2] == ["docker", "network"]
    assert cfg.DOCKER_BRIDGE_NETWORK in first
    assert cfg.DOCKER_GATEWAY_FORMAT in first


def test_the_interface_is_read_when_the_daemon_cannot_be_asked(platform_root):
    """The CLI can fail while the bridge is up: no docker group, a remote DOCKER_HOST."""
    run = scripted_probe("", IP_ADDR_DOCKER0)
    assert cfg._docker_bridge_gateway(run=run) == "172.17.0.1"
    assert [call[0] for call in run.calls] == ["docker", "ip"]
    assert cfg.DOCKER_BRIDGE_INTERFACE in run.calls[1]


def test_an_interface_broadcast_address_is_not_mistaken_for_the_gateway(platform_root):
    """``brd 172.17.255.255`` is on that line and is not a destination.

    Which is why the address after ``inet`` is located rather than the first
    thing on the line that parses.
    """
    assert cfg._interface_gateway(IP_ADDR_DOCKER0) == "172.17.0.1"


def test_only_an_ipv4_gateway_is_derived(platform_root):
    """The bridge is v4 unless ip6tables is on; a v6 gateway may have no route."""
    assert cfg._usable_gateway(INSPECT_DUAL_STACK) == "172.17.0.1"
    assert cfg._usable_gateway("fd00:dead:beef::1\n") is None


@pytest.mark.parametrize("output", [
    "",
    "\n",
    "\n\n",
    # docker's Go template renders a missing field like this rather than failing.
    "<no value>\n",
    # An internal network, or one with no IPAM gateway configured at all.
    "0.0.0.0\n",
    "127.0.0.1\n",
    "Error response from daemon: network bridge not found\n",
])
def test_an_output_with_no_usable_address_produces_no_gateway(platform_root, output):
    """A derivation must never produce a URL it has no evidence for."""
    assert cfg._docker_bridge_gateway(run=scripted_probe(output, output)) is None


def test_both_probes_silent_is_a_supported_outcome_not_an_exception(
    platform_root, caplog
):
    """``container_base_url`` needs a ``None`` here to fall through honestly.

    Logged at warning because on a docker host it means an assistant is about to
    be told it has no platform — but it is a return value, not a raise.
    """
    run = scripted_probe("", "")
    assert cfg._docker_bridge_gateway(run=run) is None
    assert len(run.calls) == 2, "both probes are tried before giving up"
    assert any("platform_bridge_gateway_unknown" in r.message for r in caplog.records)


def test_a_missing_command_is_not_an_error(platform_root):
    """The real subprocess plumbing, exercised without needing docker.

    ``_probe_output`` swallowing ``FileNotFoundError`` is what makes the second
    probe safe on a host with no iproute2 — this container is one.
    """
    assert cfg._probe_output(["lmer-no-such-command-exists"]) == ""


def test_a_command_that_fails_contributes_no_output(platform_root):
    """A non-zero exit means the answer is unknown, whatever landed on stdout."""
    assert cfg._probe_output(["sh", "-c", "echo 172.17.0.1; exit 7"]) == ""
    assert cfg._probe_output(["sh", "-c", "echo 172.17.0.1"]).strip() == "172.17.0.1"
