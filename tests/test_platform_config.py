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

from lmer_cli import tokens
from lmer_platform import config as cfg
from lmer_platform import store
from tests.conftest import strip_lmer_env

#: Stand-in for a generic PAT; never a real credential shape in use anywhere.
STUB_CREDENTIAL = "glpat-notarealcredential"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    for name in (
        cfg.ENV_BIND_ADDRESS, cfg.ENV_BIND_PORT, cfg.ENV_SECRET_FILE,
        cfg.ENV_WORK_REPO_MIRROR, cfg.ENV_AUTONOMOUS, cfg.ENV_WORK_REPO,
        cfg.ENV_WORK_REPO_FORGE,
        cfg.ENV_STALL_IDLE_SECONDS, cfg.ENV_STALL_BACKSTOP_SECONDS,
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


@pytest.fixture
def known_presets(tmp_path, monkeypatch):
    """Presets this host knows, for tests that name one.

    The launch-setting name rules ask ``load_presets()`` — the authority the
    spawned ``lmer`` itself consults — and with no presets file every preset
    name is *rightly* unusable, so a test that expects a preset value to
    resolve has to make the host know it first.
    """
    path = tmp_path / "presets.json"
    path.write_text(json.dumps({name: {} for name in (
        "file-preset", "env-preset", "kept", "old", "new", "dev", "review",
    )}), encoding="utf-8")
    monkeypatch.setenv("LMER_PRESETS_FILE", str(path))
    return path


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


class TestGitlabTokenIssuingHostSeeding:
    """The generic token's issuing host defaults to the resolved work repo.

    ``lmer_cli.tokens`` scopes a generic ``GITLAB_TOKEN`` to the host that
    issued it and defaults that host from ``LMER_WORK_REPO`` — a variable a
    config.json-configured deployment never exports, which would refuse the
    token for the work repo's own host (issue #161 review).
    """

    @pytest.fixture(autouse=True)
    def _clean_token_env(self, monkeypatch):
        for name in ("GITLAB_TOKEN", "GITLAB_TOKEN_git_example_com"):
            monkeypatch.delenv(name, raising=False)
        # The refusal notice is deduped process-wide; a leftover entry would
        # hide the very lookup these tests drive.
        tokens._warned.clear()
        yield
        tokens._warned.clear()

    def test_config_file_url_seeds_the_issuing_host(self, platform_root,
                                                    monkeypatch):
        cfg.save(cfg.PlatformConfig(
            work_repo_url="https://git.example.com/agents/work.git"))
        monkeypatch.setenv("GITLAB_TOKEN", STUB_CREDENTIAL)

        cfg.load()

        assert os.environ["LMER_GITLAB_TOKEN_HOST"] == "git.example.com"
        assert tokens._get_gitlab_token("git.example.com") == STUB_CREDENTIAL

    def test_ssh_form_seeds_the_host_too(self, platform_root, monkeypatch):
        cfg.save(cfg.PlatformConfig(
            work_repo_url="git@git.example.com:agents/work.git"))

        cfg.load()

        assert os.environ["LMER_GITLAB_TOKEN_HOST"] == "git.example.com"

    def test_explicit_setting_survives_load(self, platform_root, monkeypatch):
        monkeypatch.setenv("LMER_GITLAB_TOKEN_HOST", "gitlab.other.com")
        cfg.save(cfg.PlatformConfig(
            work_repo_url="https://git.example.com/agents/work.git"))

        cfg.load()

        assert os.environ["LMER_GITLAB_TOKEN_HOST"] == "gitlab.other.com"

    def test_no_work_repo_url_seeds_nothing(self, platform_root):
        cfg.load()

        assert "LMER_GITLAB_TOKEN_HOST" not in os.environ

    def test_unparseable_work_repo_url_seeds_nothing(self, platform_root):
        cfg.load({"work_repo_url": "/srv/local/work"})

        assert "LMER_GITLAB_TOKEN_HOST" not in os.environ

    def test_repeated_loads_are_idempotent(self, platform_root, monkeypatch):
        cfg.save(cfg.PlatformConfig(
            work_repo_url="https://git.example.com/agents/work.git"))
        cfg.load()
        # A second resolution must not overwrite what the first (or an
        # operator) put there.
        monkeypatch.setenv("LMER_GITLAB_TOKEN_HOST", "gitlab.other.com")

        cfg.load()

        assert os.environ["LMER_GITLAB_TOKEN_HOST"] == "gitlab.other.com"


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


# --- the assistant's launch settings (issue #234) ----------------------------
#
# Four keys, one rule set: resolution follows the module's chain with the
# provenance kept, unusable values warn and read as unset rather than taking
# the assistant's start down, and the settings write path edits the stored
# file only — an export must never be baked into config.json by a write that
# happened while it was set.

def test_assistant_settings_default_to_todays_behaviour(platform_root):
    """Unset everywhere reads as None/default for every key — no new behaviour."""
    config = cfg.load()
    assert config.assistant_model is None
    assert config.assistant_harness is None
    assert config.assistant_preset is None
    assert config.assistant_agents is None
    settings = cfg.assistant_settings()
    assert set(settings) == set(cfg.ASSISTANT_SETTING_KEYS)
    for key in cfg.ASSISTANT_SETTING_KEYS:
        assert settings[key].value is None
        assert settings[key].source == "default"


def test_assistant_settings_resolve_env_over_file_over_default(
    platform_root, known_presets, monkeypatch
):
    store.write_json(cfg.config_path(), {
        "assistant_model": "file-model",
        "assistant_preset": "file-preset",
    })
    monkeypatch.setenv(cfg.ENV_ASSISTANT_MODEL, "env-model")

    config = cfg.load()
    assert config.assistant_model == "env-model"
    assert config.assistant_preset == "file-preset"
    assert config.assistant_harness is None

    settings = cfg.assistant_settings()
    assert settings["model"].value == "env-model"
    assert settings["model"].source == "env"
    assert settings["preset"].value == "file-preset"
    assert settings["preset"].source == "config.json"
    assert settings["harness"].value is None
    assert settings["harness"].source == "default"


def test_a_blank_assistant_env_var_reads_as_unset(platform_root, monkeypatch):
    """Blank counts as unset, as for every other env var here — the layer
    below shows through instead of a whitespace model name reaching a flag."""
    store.write_json(cfg.config_path(), {"assistant_model": "file-model"})
    monkeypatch.setenv(cfg.ENV_ASSISTANT_MODEL, "   ")
    assert cfg.load().assistant_model == "file-model"
    assert cfg.assistant_settings()["model"].source == "config.json"


def test_an_unusable_stored_assistant_value_warns_and_reads_as_unset(
    platform_root, caplog
):
    """The _resolve_pids_limit house rule (issue #234): a typo in config.json
    costs that one setting, never the assistant's start — and never silently
    starts something the operator did not ask for."""
    store.write_json(cfg.config_path(), {
        "assistant_model": 7, "assistant_harness": "   ",
    })
    config = cfg.load()
    assert config.assistant_model is None
    assert config.assistant_harness is None
    settings = cfg.assistant_settings()
    assert settings["model"].value is None
    assert settings["model"].source == "default"
    assert any(
        "platform_assistant_setting_invalid" in record.message
        for record in caplog.records
    )


def test_assistant_values_are_stripped_not_refused(platform_root):
    store.write_json(cfg.config_path(), {"assistant_model": "  sonnet-5  "})
    assert cfg.load().assistant_model == "sonnet-5"
    assert cfg.assistant_settings()["model"].value == "sonnet-5"


def test_assistant_settings_read_fresh_without_a_reload(platform_root):
    """The whole reason the resolver exists: the daemon holds a boot-time
    config, and a persisted change must reach the *next* start anyway."""
    assert cfg.assistant_settings()["model"].value is None
    cfg.update_stored({"assistant_model": "late-model"})
    assert cfg.assistant_settings()["model"].value == "late-model"


# --- the settings write path (update_stored) ---------------------------------

def test_update_stored_edits_only_the_stored_layer(platform_root, monkeypatch):
    """An export shadowing the write must not be baked into the file: the file
    carries what was written, while the effective answer stays the env's."""
    monkeypatch.setenv(cfg.ENV_ASSISTANT_MODEL, "env-model")
    cfg.update_stored({"assistant_model": "file-model"})
    stored = store.read_json(cfg.config_path())
    config_keys = {k: v for k, v in stored.items() if k not in ("schema", "updated")}
    assert config_keys == {"assistant_model": "file-model"}
    settings = cfg.assistant_settings()
    assert settings["model"].value == "env-model"
    assert settings["model"].source == "env"


def test_update_stored_none_removes_the_key(platform_root):
    store.write_json(cfg.config_path(), {
        "assistant_model": "old", "assistant_preset": "kept",
    })
    cfg.update_stored({"assistant_model": None})
    stored = store.read_json(cfg.config_path())
    assert "assistant_model" not in stored
    assert stored["assistant_preset"] == "kept"


def test_update_stored_preserves_unknown_keys(platform_root):
    """The same tolerance load() has: a file written by a newer build keeps its
    keys through an older build's settings write."""
    store.write_json(cfg.config_path(), {"future_key": "future-value"})
    cfg.update_stored({"assistant_model": "m"})
    stored = store.read_json(cfg.config_path())
    assert stored["future_key"] == "future-value"
    assert stored["assistant_model"] == "m"


def test_update_stored_refuses_unknown_fields(platform_root):
    with pytest.raises(cfg.ConfigError, match="unknown config field"):
        cfg.update_stored({"assistant_effort": "high"})
    assert not cfg.config_path().exists()


def test_update_stored_refuses_a_write_that_would_break_the_next_boot(
    platform_root
):
    """The merged file is validated whole before it lands, so the refusal
    arrives on the write — with the caller attached — rather than at boot."""
    with pytest.raises(cfg.ConfigError):
        cfg.update_stored({"bind_port": 0})
    assert not cfg.config_path().exists()


def test_a_shadowed_setting_still_reports_its_stored_value(
    platform_root, monkeypatch
):
    """What a settings screen edits is the file's layer, so the file's value
    rides beside the effective one whichever layer won — prefilling a field
    from the env value and saving it back is how an export gets baked in."""
    store.write_json(cfg.config_path(), {"assistant_model": "file-model"})
    monkeypatch.setenv(cfg.ENV_ASSISTANT_MODEL, "env-model")
    setting = cfg.assistant_settings()["model"]
    assert setting.value == "env-model"
    assert setting.source == "env"
    assert setting.stored == "file-model"
    assert setting.to_dict() == {
        "value": "env-model", "source": "env", "stored": "file-model",
    }


def test_an_agents_value_naming_nobody_is_unusable_in_the_standing_layers(
    platform_root, caplog
):
    """The rule that was once missed: ',' passes text checks, and lmer refuses
    a fan-out that spawns nobody — stored, it made every start a 500."""
    store.write_json(cfg.config_path(), {"assistant_agents": " , ,"})
    assert cfg.load().assistant_agents is None
    assert cfg.assistant_settings()["agents"].value is None
    assert any(
        "platform_assistant_setting_invalid" in record.message
        for record in caplog.records
    )


def test_an_unusable_env_value_falls_through_to_the_file(
    platform_root, monkeypatch, caplog
):
    """The effective answer must be the one the start path will use: an export
    of '-x' contributes nothing, and the screen shows the file's value rather
    than affirming a value the spawn would have discarded."""
    store.write_json(cfg.config_path(), {"assistant_model": "file-model"})
    monkeypatch.setenv(cfg.ENV_ASSISTANT_MODEL, "-broken")
    setting = cfg.assistant_settings()["model"]
    assert setting.value == "file-model"
    assert setting.source == "config.json"
    assert any(
        "platform_assistant_setting_invalid" in record.message
        for record in caplog.records
    )


@pytest.mark.parametrize("key, value", [
    ("model", "   "),
    ("model", 7),
    ("model", "-x"),
    ("agents", ","),
    ("agents", " , ,"),
])
def test_validate_assistant_override_refuses_and_names_the_key(
    platform_root, key, value
):
    with pytest.raises(cfg.ConfigError, match=key):
        cfg.validate_assistant_override(key, value)


def test_validate_assistant_override_strips_a_usable_value(
    platform_root, known_presets
):
    assert cfg.validate_assistant_override("model", "  sonnet-5 ") == "sonnet-5"
    assert cfg.validate_assistant_override("agents", "dev,review") == "dev,review"


# --- the name rules: values the host would refuse at exit 2 -------------------
#
# Shape checks are not enough for three of the four settings: harness, preset
# and agents names have host-side authorities (known_harnesses(),
# load_presets()) and a name they do not know passes every shape check, spawns
# a session that exists on paper, and exits 2 — from a *stored* value, that is
# an assistant that cannot start, behind routes that answered 200.

def test_an_unknown_harness_is_unusable_in_the_standing_layers(
    platform_root, caplog
):
    store.write_json(cfg.config_path(), {"assistant_harness": "claud"})
    assert cfg.load().assistant_harness is None
    assert cfg.assistant_settings()["harness"].value is None
    assert any(
        "platform_assistant_setting_invalid" in record.message
        for record in caplog.records
    )


def test_a_known_harness_resolves(platform_root):
    store.write_json(cfg.config_path(), {"assistant_harness": "codex"})
    assert cfg.load().assistant_harness == "codex"


def test_an_unknown_preset_is_unusable_and_the_refusal_names_the_catalog(
    platform_root, known_presets
):
    with pytest.raises(cfg.ConfigError) as refused:
        cfg.validate_assistant_override("preset", "reviw")
    assert "reviw" in str(refused.value)
    assert "review" in str(refused.value), (
        "the refusal does not name the catalog, so a typo cannot be spotted"
    )


def test_with_no_presets_file_every_preset_name_is_unusable(platform_root):
    """An empty catalog is an authoritative answer, not an unavailable one:
    with the feature off, lmer exits 2 for every preset name."""
    with pytest.raises(cfg.ConfigError, match="LMER_PRESETS_FILE"):
        cfg.validate_assistant_override("preset", "anything")


def test_an_agents_selection_with_one_unknown_member_is_refused(
    platform_root, known_presets
):
    with pytest.raises(cfg.ConfigError, match="nosuch"):
        cfg.validate_assistant_override("agents", "dev,nosuch")


def test_an_unknown_model_is_deliberately_not_refused(platform_root):
    """No host-side authority exists for model ids — the harness is the only
    thing that knows its own — so the verbatim stance stays."""
    assert cfg.validate_assistant_override("model", "no-such-model") == "no-such-model"


def test_an_oversized_value_is_unusable_before_it_can_break_the_spawn(
    platform_root, caplog
):
    """One argv token past the kernel's MAX_ARG_STRLEN makes Popen raise E2BIG
    — a spawn that cannot even fail as a session. Bounded far below that."""
    huge = "m" * (cfg.MAX_ASSISTANT_SETTING_CHARS + 1)
    with pytest.raises(cfg.ConfigError, match="model"):
        cfg.validate_assistant_override("model", huge)
    store.write_json(cfg.config_path(), {"assistant_model": huge})
    assert cfg.load().assistant_model is None
    assert cfg.assistant_settings()["model"].value is None


def test_an_unavailable_authority_downgrades_to_shape_checks(
    platform_root, monkeypatch, caplog
):
    """The check must never break a start the spawned lmer would have accepted:
    an authority that cannot answer skips the name rule rather than refusing
    everything (the _require_taskdef posture)."""
    import lmer_cli.harness as harness_mod

    def broken():
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(harness_mod, "known_harnesses", broken)
    assert cfg.validate_assistant_override("harness", "claud") == "claud"
    assert any(
        "platform_assistant_authority_unavailable" in record.message
        for record in caplog.records
    )


def test_an_unusable_stored_value_is_still_served_as_stored(platform_root):
    """The settings screen edits the file's layer, so it has to see the file's
    text even when resolution discards it — served as null, an unusable stored
    value is invisible, unclearable from the screen, and warns forever."""
    store.write_json(cfg.config_path(), {"assistant_model": "-broken"})
    setting = cfg.assistant_settings()["model"]
    assert setting.value is None
    assert setting.source == "default"
    assert setting.stored == "-broken"


# --- round 2: the authority view must match what the child actually accepts ---

def test_an_agents_member_matching_no_preset_takes_the_model_route(
    platform_root
):
    """The regression class from review round 2: `--agents=fable` is the
    documented model-route form and, with no presets file, the only usable
    form of --agents at all — a catalog-membership check refused it and
    silently stripped a working fan-out from the standing layers."""
    assert cfg.validate_assistant_override("agents", "fable") == "fable"
    store.write_json(cfg.config_path(), {"assistant_agents": "fable"})
    assert cfg.load().assistant_agents == "fable"
    assert cfg.assistant_settings()["agents"].value == "fable"


def test_a_case_variant_of_a_defined_preset_is_refused_as_the_child_refuses_it(
    platform_root, known_presets
):
    """resolve_agent_presets' own trap, inherited by consulting it rather than
    re-deriving membership: 'Dev' must not silently take the model route when
    a 'dev' preset exists."""
    with pytest.raises(cfg.ConfigError, match="did you mean"):
        cfg.validate_assistant_override("agents", "Dev")


def test_a_harness_name_is_matched_case_insensitively(platform_root):
    """resolve_harness_selection lowercases its input ('LMER_HARNESS=Codex
    works'), so the membership check has to as well — 'Codex' ran codex before
    the name rules existed and must keep doing so."""
    assert cfg.validate_assistant_override("harness", "Codex") == "Codex"
    store.write_json(cfg.config_path(), {"assistant_harness": "Codex"})
    assert cfg.load().assistant_harness == "Codex"


@pytest.fixture
def dropin_harnesses(tmp_path, monkeypatch):
    """An initially-empty user-harness directory, primed into the load cache.

    The scenario the refresh exists for: the daemon has already read the
    directory once (caching "nothing there"), and a drop-in is installed
    afterwards — every freshly spawned lmer sees it, so the daemon's authority
    view must too.
    """
    from lmer_cli.user_harnesses import (
        clear_user_harness_cache, load_user_harnesses,
    )

    root = tmp_path / "harnesses"
    monkeypatch.setenv("LMER_HARNESSES_DIR", str(root))
    clear_user_harness_cache()
    assert load_user_harnesses() == {}, "the stale 'no directory' answer"

    def install(name):
        harness_dir = root / name
        harness_dir.mkdir(parents=True)
        (harness_dir / "harness.json").write_text(
            json.dumps({"schema": 1, "binary": name})
        )
        (harness_dir / "runner.sh").write_text("#!/bin/sh\nexit 0\n")
        return harness_dir

    yield install
    clear_user_harness_cache()


def test_a_dropin_installed_after_the_first_read_is_honoured(
    platform_root, dropin_harnesses
):
    """The behaviour, not the mechanism: a name every freshly spawned lmer
    accepts must not be refused by a long-lived daemon that read the directory
    before the drop-in existed."""
    with pytest.raises(cfg.ConfigError):
        cfg.validate_assistant_override("harness", "mycli")
    dropin_harnesses("mycli")
    assert cfg.validate_assistant_override("harness", "mycli") == "mycli"


def test_the_agents_path_sees_a_dropin_installed_after_the_first_read(
    platform_root, dropin_harnesses, tmp_path, monkeypatch
):
    """The residue round 3 named: resolve_agent_presets reaches
    known_harnesses() itself (a preset's --harness arg is validated there), so
    the refresh has to happen on the agents path too — a fresh child accepts
    this selection, and the daemon must agree."""
    presets_file = tmp_path / "presets.json"
    presets_file.write_text(json.dumps({
        "myagent": {"args": ["--harness", "mycli"]},
    }))
    monkeypatch.setenv("LMER_PRESETS_FILE", str(presets_file))
    with pytest.raises(cfg.ConfigError):
        cfg.validate_assistant_override("agents", "myagent")
    dropin_harnesses("mycli")
    assert cfg.validate_assistant_override("agents", "myagent") == "myagent"


def test_a_refresh_does_not_repeat_a_malformed_dropins_warning(
    platform_root, dropin_harnesses, capsys
):
    """The module's once-per-process warning promise has to survive a daemon
    refreshing per validation — a host with one malformed drop-in must not get
    the warning re-emitted on every config read and every start."""
    broken = dropin_harnesses("okcli").parent / "broken"
    broken.mkdir()
    (broken / "harness.json").write_text("{not json")
    cfg.validate_assistant_override("harness", "okcli")
    cfg.validate_assistant_override("harness", "okcli")
    warnings = capsys.readouterr().err.count("User harness")
    assert warnings == 1, (
        f"the malformed drop-in warned {warnings} times across two refreshes"
    )


def test_a_non_string_stored_value_is_serialized_not_hidden(platform_root):
    """The narrowed residue of the stored-value thread: a hand-written list or
    number must be visible and clearable from the screen, not warn forever
    behind a stored:null the dialog cannot act on."""
    store.write_json(cfg.config_path(), {
        "assistant_agents": ["dev", "review"], "assistant_model": 5,
    })
    settings = cfg.assistant_settings()
    assert settings["agents"].value is None
    assert settings["agents"].stored == '["dev", "review"]'
    assert settings["model"].stored == "5"


# --- service slots (issue #245) ----------------------------------------------

def test_slots_default_to_none_declared(platform_root):
    assert cfg.load().slots == ()


def test_slots_survive_a_save_load_round_trip(platform_root):
    entries = [{"name": "webapp", "preset": "webapp_dev", "description": "Web app"}]
    cfg.save(cfg.load(overrides={"slots": entries}))

    assert cfg.load().slots == tuple(entries)


def test_update_stored_writes_slots_without_baking_in_the_environment(
    platform_root, monkeypatch
):
    """The settings write path reaches slots like any other stored field."""
    monkeypatch.setenv(cfg.ENV_BIND_ADDRESS, "0.0.0.0")
    entries = [{"name": "webapp", "preset": "webapp_dev"}]

    stored = cfg.update_stored({"slots": entries})

    assert stored["slots"] == entries
    assert "bind_address" not in stored


def test_no_environment_variable_declares_slots(platform_root, monkeypatch):
    """Declaring slots is once-per-host file work; a JSON list in an export is
    a footgun, so there is deliberately no override for it."""
    monkeypatch.setenv("LMER_PLATFORM_SLOTS", '[{"name": "x", "preset": "y"}]')

    assert cfg.load().slots == ()


def test_a_slots_entry_is_not_parsed_at_the_config_layer(platform_root):
    """A daemon that refuses to boot over a mistyped slot cannot be used to fix
    the slot, so the entries arrive here exactly as the file spells them."""
    store.write_json(cfg.config_path(), {"slots": [{"nonsense": True}]})

    assert cfg.load().slots == ({"nonsense": True},)


# --- the check-in window (issue #244) ----------------------------------------
#
# Not a launch setting and deliberately not in ASSISTANT_SETTING_KEYS: those
# four become argv tokens on the assistant's own command line, while this is an
# integer the daemon reads on its detection tick. What it shares is the
# resolution chain, so these pin the same properties the four have — an export
# shadows the file, an unusable layer falls through rather than refusing — plus
# the one that is its own: 0 is a value, not a mistake.

def test_the_window_defaults_to_an_hour(platform_root):
    assert cfg.load().checkin_window_seconds == 3600
    setting = cfg.checkin_settings()["window_seconds"]
    assert setting.value == 3600
    assert setting.source == "default"
    assert setting.stored is None


def test_the_window_resolves_env_over_file_over_default(platform_root, monkeypatch):
    store.write_json(cfg.config_path(), {"checkin_window_seconds": 900})
    assert cfg.load().checkin_window_seconds == 900
    assert cfg.checkin_settings()["window_seconds"].source == "config.json"

    monkeypatch.setenv(cfg.ENV_CHECKIN_WINDOW, "120")
    assert cfg.load().checkin_window_seconds == 120
    setting = cfg.checkin_settings()["window_seconds"]
    assert setting.value == 120
    assert setting.source == "env"
    assert setting.stored == 900, "the screen edits the file, not the export"


def test_zero_disables_the_digest_and_is_not_a_mistake(platform_root):
    cfg.update_stored({"checkin_window_seconds": 0})
    assert cfg.load().checkin_window_seconds == 0
    assert cfg.checkin_settings()["window_seconds"].value == 0


def test_an_unusable_window_costs_its_layer_not_the_daemon(
    platform_root, monkeypatch, caplog
):
    """A typo in an export must not be a host that will not boot: the whole
    effect of this value is how often a reminder is spooled.

    The warning is said once per distinct bad value for the life of the process
    (it resolves on every fleet read, so per-resolve would flood the log), which
    makes it the one memo here with no stat key to expire it — so this test
    clears it rather than assuming nothing earlier in the session tripped it.
    """
    cfg._WARNED_WINDOWS.clear()
    store.write_json(cfg.config_path(), {"checkin_window_seconds": 900})
    monkeypatch.setenv(cfg.ENV_CHECKIN_WINDOW, "soon")
    # Both readers, and the same answer from each: the loaded config and the
    # fresh resolution the tick uses must not disagree about three layers.
    assert cfg.load().checkin_window_seconds == 900
    assert cfg.checkin_settings()["window_seconds"].source == "config.json"
    assert cfg.checkin_settings()["window_seconds"].value == 900
    assert any(
        "platform_checkin_window_invalid" in record.message
        for record in caplog.records
    )


def test_a_negative_window_in_the_file_falls_back_to_the_default(platform_root):
    store.write_json(cfg.config_path(), {"checkin_window_seconds": -60})
    assert cfg.load().checkin_window_seconds == 3600


def test_validate_checkin_window_refuses_an_explicit_bad_value(platform_root):
    """The other posture: the caller is attached and asking for this value."""
    for bad in (-1, "soon", 3.5, True, None):
        with pytest.raises(cfg.ConfigError) as excinfo:
            cfg.validate_checkin_window(bad)
        assert "checkin_window_seconds" in str(excinfo.value)


def test_validate_checkin_window_accepts_numeric_text(platform_root):
    assert cfg.validate_checkin_window("7200") == 7200
    assert cfg.validate_checkin_window(0) == 0


# --- halt-detection thresholds (#243) ----------------------------------------
#
# Two knobs and one relationship between them. The tests below pin the three
# things an operator can get wrong: which value means "off", which value means
# "later", and what happens when the two are set the wrong way round.


def test_the_halt_thresholds_default_to_ten_minutes_and_an_hour(platform_root):
    """The numbers the operator chose (#243), not derived ones.

    Ten minutes is when the precise paths may fire and an hour is when silence
    alone is enough. Pinned here because both are policy: nothing in the code can
    derive how long a run may legitimately be quiet, which is exactly why the
    module that raises the flag refused to invent one until it had an owner.
    """
    config = cfg.load()
    assert config.stall_idle_seconds == 600
    assert config.stall_backstop_seconds == 3600


def test_the_halt_thresholds_read_their_env_vars(platform_root, monkeypatch):
    monkeypatch.setenv(cfg.ENV_STALL_IDLE_SECONDS, "120")
    monkeypatch.setenv(cfg.ENV_STALL_BACKSTOP_SECONDS, "240")
    config = cfg.load()
    assert config.stall_idle_seconds == 120
    assert config.stall_backstop_seconds == 240


def test_the_halt_thresholds_come_from_the_config_file_too(platform_root):
    store.write_json(cfg.config_path(), {
        "stall_idle_seconds": 90, "stall_backstop_seconds": 900,
    })
    config = cfg.load()
    assert config.stall_idle_seconds == 90
    assert config.stall_backstop_seconds == 900


@pytest.mark.parametrize("field,env", [
    ("stall_idle_seconds", "ENV_STALL_IDLE_SECONDS"),
    ("stall_backstop_seconds", "ENV_STALL_BACKSTOP_SECONDS"),
])
def test_zero_disables_a_halt_path_rather_than_being_refused(
    platform_root, monkeypatch, field, env
):
    """``0`` is the off switch, and it must survive the loader.

    Its neighbours (``max_concurrent_sessions`` and friends) are validated as
    *positive* integers, so folding these in there would refuse the one value an
    operator needs to turn a path off without editing code. A zero that raised
    would also be a zero that took the daemon down at boot.
    """
    monkeypatch.setenv(getattr(cfg, env), "0")
    assert getattr(cfg.load(), field) == 0


@pytest.mark.parametrize("field", ["stall_idle_seconds", "stall_backstop_seconds"])
def test_a_negative_halt_threshold_is_refused_by_name(platform_root, field):
    with pytest.raises(cfg.ConfigError) as excinfo:
        cfg.load(overrides={field: -1})
    assert field in str(excinfo.value)
    assert "0 disables it" in str(excinfo.value)


def test_a_backstop_below_the_first_threshold_is_refused_not_reordered(
    platform_root,
):
    """An inverted pair means the operator has them the wrong way round.

    Silently swapping them would leave a host quietly running a configuration
    nobody wrote; silently accepting them would make the backstop fire first and
    the precise paths unreachable, so every halt would be reported as
    ``backstop`` — the least informative answer — while looking like it worked.
    """
    with pytest.raises(cfg.ConfigError) as excinfo:
        cfg.load(overrides={"stall_idle_seconds": 600, "stall_backstop_seconds": 60})
    message = str(excinfo.value)
    assert "stall_backstop_seconds" in message and "stall_idle_seconds" in message


def test_a_disabled_backstop_is_not_read_as_an_inverted_pair(platform_root):
    """0 is off, and off is not "earlier than the first threshold".

    The guard above compares two numbers, and the whole point of the off switch
    is that one of them may be zero — so the ordering check has to exempt it or
    the escape hatch would be refused by the safety rail.
    """
    config = cfg.load(overrides={
        "stall_idle_seconds": 600, "stall_backstop_seconds": 0,
    })
    assert config.stall_backstop_seconds == 0
