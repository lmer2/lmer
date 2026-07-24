"""Tests for user-installed harness definitions (lmer_cli.user_harnesses).

Covers:
- manifest loading: defaults, full round-trip, per-entry graceful degradation
- validation: schema gate, name rules, built-in shadowing, credential mounts
- registry integration: known_harnesses merge, get_harness, resolution
  precedence, UnknownHarnessError listing, model-hint ordering
- spawn-harness child selection of a user harness
- exec-profile assembly (build_exec_argv) from a manifest
- container wiring: definitions/cache mounts, user-runner dispatch in
  clone_and_exec, cli.py env-dict source guards
"""

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

import lmer_cli.user_harnesses as user_harnesses_mod
from lmer_cli.container import clone_and_exec
from lmer_cli.container.spawn_harness import (
    apply_harness_extra_env,
    prepend_user_harness_path,
    select_harness,
)
from lmer_cli.harness import (
    GENERIC_START_COMMAND,
    HARNESSES,
    UnknownHarnessError,
    build_exec_argv,
    get_harness,
    harness_for_model,
    known_harnesses,
    resolve_harness_selection,
)
from lmer_cli.mounts import (
    PlannedCredentialMount,
    build_user_harness_mounts,
    build_user_mounts,
    plan_credential_mounts,
)
from lmer_cli.user_harnesses import (
    CONTAINER_HARNESSES_DIR,
    CONTAINER_HARNESS_CACHE_DIR,
    DEFAULT_HARNESSES_DIR,
    HARNESSES_DIR_ENV,
    clear_user_harness_cache,
    load_user_harnesses,
    user_harnesses_dir,
)
from tests.conftest import strip_lmer_env

CLI_PY = Path(__file__).parent.parent / "src" / "lmer_cli" / "cli.py"


@pytest.fixture(autouse=True)
def _clean_env_and_cache(monkeypatch):
    strip_lmer_env(monkeypatch)
    clear_user_harness_cache()
    yield
    clear_user_harness_cache()


def write_harness(root, name, manifest=None, runner=True):
    """Drop a user-harness directory under *root*; returns its path."""
    harness_dir = root / name
    harness_dir.mkdir(parents=True)
    if manifest is not None:
        (harness_dir / "harness.json").write_text(json.dumps(manifest))
    if runner:
        (harness_dir / "runner.sh").write_text("#!/bin/bash\nexit 0\n")
    return harness_dir


MINIMAL = {"schema": 1, "binary": "acme"}

FULL = {
    "schema": 1,
    "description": "ACME agent CLI",
    "binary": "acme",
    "credential_mounts": [
        {
            "host_path": ".acme/auth.json",
            "container_path": "/home/developer/.acme/auth.json",
        },
        {
            "host_path": ".acme/models.json",
            "container_path": "/home/developer/.acme/models.json",
            "mode": "ro",
        },
    ],
    "supervisor": {
        "ready_marker": "\\u276f",
        "start_command": "/go",
        "quit_sequence": ["\\x03", "/quit\\r"],
        "ready_timeout": 45,
    },
    "exec": {
        "base_args": ["-p"],
        "permission_bypass_args": ["--yolo"],
        "model_args": ["--model", "{model}"],
        "effort_args": ["--effort", "{effort}"],
        "effort_max_value": "xhigh",
        "dashdash_before_prompt": True,
    },
    "model_hints": ["acme"],
    "extra_env": {"ACME_NO_UPDATE": "1"},
}


class TestLoader:
    def test_missing_dir_yields_nothing(self, tmp_path):
        assert load_user_harnesses(tmp_path / "nope") == {}

    def test_minimal_manifest_defaults(self, tmp_path):
        write_harness(tmp_path, "acme", MINIMAL)
        loaded = load_user_harnesses(tmp_path)
        assert set(loaded) == {"acme"}
        h = loaded["acme"]
        assert h.name == "acme"
        assert h.binary == "acme"
        assert h.runner_command == "acme-runner"
        assert h.runner_script == "runner.sh"
        assert h.credential_mounts == ()
        assert h.cache_bust_arg == ""
        assert h.source_dir == str(tmp_path / "acme")
        # Safest-possible supervisor defaults: no marker gating, generic
        # start instruction, no quit chord (straight to SIGTERM).
        assert h.supervisor.ready_marker == b""
        assert h.supervisor.start_command == GENERIC_START_COMMAND
        assert h.supervisor.quit_sequence == ()
        assert h.supervisor.ready_timeout is None
        assert h.exec_profile.base_args == ()
        assert h.description  # never empty — falls back to a generated one

    def test_full_manifest_round_trip(self, tmp_path):
        write_harness(tmp_path, "acme", FULL)
        h = load_user_harnesses(tmp_path)["acme"]
        assert h.description == "ACME agent CLI"
        assert [c.host_path for c in h.credential_mounts] == [
            ".acme/auth.json",
            ".acme/models.json",
        ]
        assert h.credential_mounts[0].mode == "rw"
        assert h.credential_mounts[1].mode == "ro"
        # ❯ is ❯ — decoded to the same UTF-8 bytes the claude profile uses.
        assert h.supervisor.ready_marker == b"\xe2\x9d\xaf"
        assert h.supervisor.start_command == "/go"
        assert h.supervisor.quit_sequence == (b"\x03", b"/quit\r")
        assert h.supervisor.ready_timeout == 45.0
        assert h.exec_profile.permission_bypass_args == ("--yolo",)
        assert h.exec_profile.effort_max_value == "xhigh"
        assert h.exec_profile.dashdash_before_prompt is True
        assert h.model_hints == ("acme",)
        assert h.extra_env == (("ACME_NO_UPDATE", "1"),)

    def test_broken_entry_skipped_others_load(self, tmp_path, capsys):
        broken = write_harness(tmp_path, "broken", None)
        (broken / "harness.json").write_text("{not json")
        write_harness(tmp_path, "acme", MINIMAL)
        loaded = load_user_harnesses(tmp_path)
        assert set(loaded) == {"acme"}
        assert "broken" in capsys.readouterr().err

    def test_unsupported_schema_skipped(self, tmp_path, capsys):
        write_harness(tmp_path, "acme", {"schema": 2, "binary": "acme"})
        assert load_user_harnesses(tmp_path) == {}
        assert "unsupported schema" in capsys.readouterr().err

    def test_builtin_shadow_skipped(self, tmp_path, capsys):
        write_harness(tmp_path, "claude", {"schema": 1, "binary": "evil"})
        assert load_user_harnesses(tmp_path) == {}
        assert "shadow" in capsys.readouterr().err

    def test_invalid_name_skipped(self, tmp_path, capsys):
        write_harness(tmp_path, "Bad.Name", MINIMAL)
        assert load_user_harnesses(tmp_path) == {}
        assert "invalid name" in capsys.readouterr().err

    def test_missing_binary_skipped(self, tmp_path, capsys):
        write_harness(tmp_path, "acme", {"schema": 1})
        assert load_user_harnesses(tmp_path) == {}
        assert "binary" in capsys.readouterr().err

    def test_dir_without_manifest_ignored_silently(self, tmp_path, capsys):
        write_harness(tmp_path, "scratch", None, runner=False)
        assert load_user_harnesses(tmp_path) == {}
        assert capsys.readouterr().err == ""

    def test_missing_runner_warns_but_loads(self, tmp_path, capsys):
        write_harness(tmp_path, "acme", MINIMAL, runner=False)
        assert set(load_user_harnesses(tmp_path)) == {"acme"}
        assert "runner.sh" in capsys.readouterr().err

    def test_absolute_credential_host_path_skips_entry(self, tmp_path, capsys):
        manifest = dict(
            MINIMAL,
            credential_mounts=[
                {"host_path": "/etc/passwd", "container_path": "/home/developer/x"}
            ],
        )
        write_harness(tmp_path, "acme", manifest)
        assert load_user_harnesses(tmp_path) == {}
        assert "relative" in capsys.readouterr().err

    def test_dot_credential_host_path_skips_entry(self, tmp_path, capsys):
        # host_path="." would mount the entire host home read-write.
        manifest = dict(
            MINIMAL,
            credential_mounts=[
                {"host_path": ".", "container_path": "/home/developer/x"}
            ],
        )
        write_harness(tmp_path, "acme", manifest)
        assert load_user_harnesses(tmp_path) == {}
        assert "relative" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "bad",
        [
            {"host_path": ".a/auth.json", "container_path": "/y:rw,z"},
            {"host_path": ".a/auth:json", "container_path": "/y"},
            {"host_path": ".a/au th.json", "container_path": "/y"},
        ],
    )
    def test_mount_option_smuggling_skips_entry(self, tmp_path, capsys, bad):
        # ':'/','/whitespace in either path would smuggle extra fields or
        # options into the runtime's -v string, bypassing the mode gate.
        write_harness(tmp_path, "acme", dict(MINIMAL, credential_mounts=[bad]))
        assert load_user_harnesses(tmp_path) == {}
        assert "must not" in capsys.readouterr().err

    @pytest.mark.parametrize("key", ["HOME", "PATH", "LMER_HARNESS", "LMER_ANYTHING"])
    def test_reserved_extra_env_key_skips_entry(self, tmp_path, capsys, key):
        write_harness(tmp_path, "acme", dict(MINIMAL, extra_env={key: "x"}))
        assert load_user_harnesses(tmp_path) == {}
        assert "reserved" in capsys.readouterr().err

    @pytest.mark.parametrize("hint", ["", "   ", "-", "."])
    def test_wordless_model_hint_skips_entry(self, tmp_path, capsys, hint):
        # A hint with no word character produces a catch-all pattern in
        # harness_for_model ("" → \b\b matches everything, "-" → \b\-\b
        # matches every hyphenated id) — silently rerouting all
        # otherwise-unhinted models to this harness.
        write_harness(tmp_path, "acme", dict(MINIMAL, model_hints=[hint]))
        assert load_user_harnesses(tmp_path) == {}
        assert "model_hints" in capsys.readouterr().err

    def test_non_utf8_manifest_skipped_others_load(self, tmp_path, capsys):
        # A manifest that isn't valid UTF-8 must be warned-and-skipped like
        # any broken entry — it must never propagate out of the loader and
        # crash registry resolution for unrelated (built-in) sessions.
        broken = write_harness(tmp_path, "broken", None)
        (broken / "harness.json").write_bytes(b'{"schema": 1, "binary": "\xe9"}')
        write_harness(tmp_path, "acme", MINIMAL)
        loaded = load_user_harnesses(tmp_path)
        assert set(loaded) == {"acme"}
        assert "broken" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "field,value",
        [
            ("schema", True),
            ("schema", 1.0),  # 1.0 == 1 would pass a membership check
            ("supervisor", {"start_command": False}),
            ("supervisor", {"start_command": 0}),
            ("supervisor", {"ready_timeout": True}),
        ],
    )
    def test_falsey_or_bool_typed_values_skip_entry(self, tmp_path, capsys, field, value):
        # Falsey wrong types must be rejected like truthy ones (`false` is
        # not a request for the default), and non-exact-int schema values
        # (bool, float) must not pass the schema gate (True == 1, 1.0 == 1).
        write_harness(tmp_path, "acme", dict(MINIMAL, **{field: value}))
        assert load_user_harnesses(tmp_path) == {}
        assert capsys.readouterr().err

    def test_load_is_cached_per_directory(self, tmp_path):
        write_harness(tmp_path, "acme", MINIMAL)
        first = load_user_harnesses(tmp_path)
        (tmp_path / "acme" / "harness.json").unlink()
        assert load_user_harnesses(tmp_path) == first
        clear_user_harness_cache()
        assert load_user_harnesses(tmp_path) == {}


class TestDirResolution:
    def test_env_overrides_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv(HARNESSES_DIR_ENV, str(tmp_path))
        assert user_harnesses_dir() == tmp_path

    def test_default_dir(self):
        # Compare against the module attribute, not the import-time binding:
        # the session-scoped _isolate_user_harnesses fixture (conftest)
        # repoints DEFAULT_HARNESSES_DIR for the whole suite.
        assert user_harnesses_dir() == user_harnesses_mod.DEFAULT_HARNESSES_DIR
        assert DEFAULT_HARNESSES_DIR == Path.home() / ".lmer" / "harnesses"


class TestRegistryIntegration:
    @pytest.fixture
    def acme_dir(self, monkeypatch, tmp_path):
        write_harness(tmp_path, "acme", FULL)
        monkeypatch.setenv(HARNESSES_DIR_ENV, str(tmp_path))
        return tmp_path

    def test_known_harnesses_merges_user_entries(self, acme_dir):
        merged = known_harnesses()
        assert set(merged) == set(HARNESSES) | {"acme"}
        # HARNESSES itself stays built-ins-only.
        assert "acme" not in HARNESSES

    def test_get_harness_finds_user_harness(self, acme_dir):
        assert get_harness("acme").binary == "acme"

    def test_resolution_via_env(self, acme_dir, monkeypatch):
        monkeypatch.setenv("LMER_HARNESS", "acme")
        assert resolve_harness_selection() == ("acme", "env")

    def test_resolution_via_flag(self, acme_dir):
        assert resolve_harness_selection("acme") == ("acme", "flag")

    def test_unknown_error_lists_user_harness(self, acme_dir):
        with pytest.raises(UnknownHarnessError, match="acme"):
            get_harness("nope")

    def test_user_model_hint_autoselects(self, acme_dir):
        assert harness_for_model("acme-large-1") == "acme"
        assert harness_for_model("nothing-known") is None

    def test_builtin_hints_beat_user_hints(self, monkeypatch, tmp_path):
        manifest = dict(FULL, model_hints=["gpt", "acme"])
        write_harness(tmp_path, "acme", manifest)
        monkeypatch.setenv(HARNESSES_DIR_ENV, str(tmp_path))
        # A user harness declaring a built-in family name never wins it.
        assert harness_for_model("gpt-5.2") == "codex"
        assert harness_for_model("acme-large-1") == "acme"

    def test_spawn_harness_selects_user_harness(self, acme_dir):
        child_env = {"LMER_HARNESS": "claude"}
        selected = select_harness(child_env, {"LMER_HARNESS": "acme"})
        assert selected.name == "acme"
        assert child_env["LMER_HARNESS"] == "acme"

    def test_spawn_harness_child_gets_manifest_extra_env(self, acme_dir):
        # Children exec the harness binary directly — no runner runs — so
        # the manifest's fixed env must be merged into the child env.
        acme = get_harness("acme")
        child_env = {}
        apply_harness_extra_env(child_env, acme)
        assert child_env["ACME_NO_UPDATE"] == "1"
        # Existing values win (same lose-to-existing precedence as the
        # launch-time env dict in cli.py).
        child_env = {"ACME_NO_UPDATE": "0"}
        apply_harness_extra_env(child_env, acme)
        assert child_env["ACME_NO_UPDATE"] == "0"

    def test_spawn_harness_pi_child_gets_skip_version_check(self):
        # The built-in half of the same gap: a pi child of a non-pi session
        # never runs pi-runner.sh, which is what exports this var.
        child_env = {}
        apply_harness_extra_env(child_env, HARNESSES["pi"])
        assert child_env["PI_SKIP_VERSION_CHECK"] == "1"

    def test_spawn_harness_child_path_gets_cache_bin(self, acme_dir):
        # Fan-out children never run runner.sh, so the child harness's
        # install-cache bin dir must reach the child PATH.
        acme = get_harness("acme")
        child_env = {"PATH": "/usr/bin"}
        prepend_user_harness_path(child_env, acme)
        assert child_env["PATH"] == f"{CONTAINER_HARNESS_CACHE_DIR}/acme/bin:/usr/bin"
        prepend_user_harness_path(child_env, acme)  # idempotent
        assert child_env["PATH"].count(f"{CONTAINER_HARNESS_CACHE_DIR}/acme/bin") == 1
        builtin_env = {"PATH": "/usr/bin"}
        prepend_user_harness_path(builtin_env, HARNESSES["claude"])
        assert builtin_env["PATH"] == "/usr/bin"

    def test_update_harness_rejects_user_harness_with_guidance(self, acme_dir, capsys):
        # User harnesses have no image layer to bust; --update-harness must
        # exit 2 with the wipe-the-cache guidance (and never treat them as
        # unknown names). Validation runs before runtime detection.
        # cli imports stay function-scoped in this module (matching
        # test_harness.py's convention): cli.py is a heavyweight import only
        # these few tests need, and TestCliSourceGuards deliberately reads
        # its source without importing it.
        from lmer_cli.cli import _handle_build

        rc = _handle_build(["--update-harness", "acme"])
        out = capsys.readouterr().out
        assert rc == 2
        assert "user-installed" in out
        assert "harness-cache" in out

    def test_update_harness_all_does_not_swallow_user_name(self, acme_dir, capsys):
        # 'all' must not bypass validation of the other requested names:
        # `--update-harness all --update-harness acme` is an error, not a
        # silent drop of acme.
        from lmer_cli.cli import _handle_build

        rc = _handle_build(["--update-harness", "all", "--update-harness", "acme"])
        out = capsys.readouterr().out
        assert rc == 2
        assert "user-installed" in out

    def test_update_harness_all_excludes_user_harnesses(self, acme_dir, monkeypatch):
        # 'all' means all BUILT-INS — a user harness must not sneak into the
        # image build's cache-bust list.
        from lmer_cli import cli

        monkeypatch.setenv("LMER_IMAGE", "img:test")
        with patch.object(cli, "detect_runtime", return_value="docker"), patch.object(
            cli, "build_image", return_value=True
        ) as build:
            rc = cli._handle_build(["--update-harness", "all"])
        assert rc == 0
        assert build.call_args.kwargs["update_harnesses"] == sorted(HARNESSES)

    def test_agents_child_harnesses_include_user_harness(self, acme_dir):
        # The host-side mount-union must see a user-harness child (the
        # in-container select_harness accepts it, so the launch-time
        # credential/cache computation must agree) — issue #131 contract.
        from lmer_cli.cli import _agents_child_harnesses

        resolved = {"acme-agent": {"env": {"LMER_HARNESS": "acme"}}}
        extra = _agents_child_harnesses(resolved, HARNESSES["claude"])
        assert [h.name for h in extra] == ["acme"]
        assert extra[0].source_dir is not None

    def test_build_exec_argv_from_manifest(self, acme_dir):
        h = get_harness("acme")
        argv, warnings = build_exec_argv(
            h, "do the thing", model="acme-large-1", effort="max", unattended=True
        )
        assert argv == [
            "acme",
            "-p",
            "--yolo",
            "--model",
            "acme-large-1",
            "--effort",
            "xhigh",
            "--",
            "do the thing",
        ]
        assert warnings == []

    def test_build_exec_argv_warns_when_model_unpassable(self, tmp_path):
        # An env-configured harness (no model_args/effort_args) must not
        # SILENTLY drop a supplied model/effort — a fan-out child would then
        # run the harness default instead of what the agent asked for.
        manifest = dict(
            MINIMAL,
            binary="kimiish",
            exec={"base_args": ["run"]},  # no model_args, no effort_args
        )
        write_harness(tmp_path, "kimiish", manifest)
        h = load_user_harnesses(tmp_path)["kimiish"]
        argv, warnings = build_exec_argv(h, "go", model="some/model", effort="high")
        assert argv == ["kimiish", "run", "go"]
        # Informational (not alarming): names the model not added to argv and
        # that an env-configured wrapper still delivers it.
        assert any("some/model" in w and "no model flag" in w for w in warnings)
        assert any("no effort flag" in w for w in warnings)
        assert all("LMER_LLM_NAME" in w or "LMER_REASONING_EFFORT" in w for w in warnings)


class TestUserHarnessMounts:
    @pytest.fixture(autouse=True)
    def _no_selinux(self):
        from lmer_cli.runtime import _is_selinux_enforcing

        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            _is_selinux_enforcing.cache_clear()
            yield

    def _acme(self, monkeypatch, tmp_path):
        root = tmp_path / "harnesses"
        write_harness(root, "acme", MINIMAL)
        monkeypatch.setenv(HARNESSES_DIR_ENV, str(root))
        return root, load_user_harnesses(root)["acme"]

    def test_no_dir_no_mounts(self, monkeypatch, tmp_path):
        monkeypatch.setenv(HARNESSES_DIR_ENV, str(tmp_path / "nope"))
        assert build_user_harness_mounts("docker", HARNESSES["claude"]) == ([], False)

    def test_definitions_dir_mounted_ro(self, monkeypatch, tmp_path):
        root, _ = self._acme(monkeypatch, tmp_path)
        args, cache_mounted = build_user_harness_mounts("docker", HARNESSES["claude"])
        assert args == ["-v", f"{root}:{CONTAINER_HARNESSES_DIR}:ro"]
        assert cache_mounted is False

    def test_cache_mounted_for_user_session_harness(self, monkeypatch, tmp_path):
        root, acme = self._acme(monkeypatch, tmp_path)
        cache = tmp_path / "cache"
        monkeypatch.setattr(user_harnesses_mod, "DEFAULT_HARNESS_CACHE_DIR", cache)
        args, cache_mounted = build_user_harness_mounts("docker", acme)
        assert args == [
            "-v",
            f"{root}:{CONTAINER_HARNESSES_DIR}:ro",
            "-v",
            f"{cache}:{CONTAINER_HARNESS_CACHE_DIR}:rw",
        ]
        assert cache_mounted is True
        assert cache.is_dir()  # created on demand

    def test_cache_mounted_for_user_child_harness(self, monkeypatch, tmp_path):
        root, acme = self._acme(monkeypatch, tmp_path)
        cache = tmp_path / "cache"
        monkeypatch.setattr(user_harnesses_mod, "DEFAULT_HARNESS_CACHE_DIR", cache)
        args, cache_mounted = build_user_harness_mounts(
            "docker", HARNESSES["claude"], extra_harnesses=(acme,)
        )
        assert f"{cache}:{CONTAINER_HARNESS_CACHE_DIR}:rw" in args
        assert cache_mounted is True

    def test_no_cache_for_builtin_only_session(self, monkeypatch, tmp_path):
        root, _ = self._acme(monkeypatch, tmp_path)
        args, cache_mounted = build_user_harness_mounts("docker", HARNESSES["codex"])
        assert not any(CONTAINER_HARNESS_CACHE_DIR in a for a in args)
        assert cache_mounted is False

    def test_cache_mkdir_failure_reports_unmounted(self, monkeypatch, tmp_path, capsys):
        root, acme = self._acme(monkeypatch, tmp_path)
        blocker = tmp_path / "blocker"
        blocker.write_text("")  # a file where the cache dir should go
        monkeypatch.setattr(
            user_harnesses_mod, "DEFAULT_HARNESS_CACHE_DIR", blocker / "cache"
        )
        args, cache_mounted = build_user_harness_mounts("docker", acme)
        # The definitions mount survives; the cache mount is skipped and the
        # flag says so — LMER_HARNESS_CACHE must not be exported (cli.py).
        assert cache_mounted is False
        assert not any(CONTAINER_HARNESS_CACHE_DIR in a for a in args)
        assert "Cannot create harness cache dir" in capsys.readouterr().err


class TestUserHarnessCredentialFileOnly:
    """User-harness credential mounts bind regular files only — a manifest
    naming a home-relative directory (`.ssh`) must be refused at mount time,
    not silently bound rw."""

    def test_directory_credential_mount_skipped_with_warning(
        self, monkeypatch, tmp_path, capsys
    ):
        home = tmp_path / "home"
        (home / ".acme").mkdir(parents=True)
        (home / ".acme" / "auth.json").write_text("{}")
        (home / ".ssh").mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        root = tmp_path / "harnesses"
        manifest = dict(
            MINIMAL,
            credential_mounts=[
                {"host_path": ".acme/auth.json", "container_path": "/home/developer/.acme/auth.json"},
                {"host_path": ".ssh", "container_path": "/home/developer/.ssh-copy"},
            ],
        )
        write_harness(root, "acme", manifest)
        acme = load_user_harnesses(root)["acme"]
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            from lmer_cli.runtime import _is_selinux_enforcing

            _is_selinux_enforcing.cache_clear()
            args, _ = build_user_mounts("docker", acme)
        joined = " ".join(args)
        assert ".acme/auth.json" in joined
        assert ".ssh" not in joined
        assert "not a regular file" in capsys.readouterr().err

    def test_out_of_home_symlink_credential_skipped(self, monkeypatch, tmp_path, capsys):
        # is_file() follows symlinks, so a home-relative symlink whose
        # target is a regular file OUTSIDE $HOME must still be refused — the
        # documented boundary is "any regular file under the host home".
        home = tmp_path / "home"
        (home / ".acme").mkdir(parents=True)
        outside = tmp_path / "outside-secret"
        outside.write_text("sensitive")
        (home / ".acme" / "link.json").symlink_to(outside)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        root = tmp_path / "harnesses"
        manifest = dict(
            MINIMAL,
            credential_mounts=[
                {"host_path": ".acme/link.json", "container_path": "/home/developer/x"}
            ],
        )
        write_harness(root, "acme", manifest)
        acme = load_user_harnesses(root)["acme"]
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            from lmer_cli.runtime import _is_selinux_enforcing

            _is_selinux_enforcing.cache_clear()
            args, _ = build_user_mounts("docker", acme)
        assert not any("link.json" in a for a in args)
        assert "not a regular file under the host home" in capsys.readouterr().err

    def test_in_home_symlink_credential_allowed(self, monkeypatch, tmp_path):
        # A symlink to a regular file that stays UNDER $HOME is fine.
        home = tmp_path / "home"
        (home / ".acme").mkdir(parents=True)
        (home / ".acme" / "real.json").write_text("{}")
        (home / ".acme" / "auth.json").symlink_to(home / ".acme" / "real.json")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        root = tmp_path / "harnesses"
        manifest = dict(
            MINIMAL,
            credential_mounts=[
                {"host_path": ".acme/auth.json", "container_path": "/home/developer/x"}
            ],
        )
        write_harness(root, "acme", manifest)
        acme = load_user_harnesses(root)["acme"]
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            from lmer_cli.runtime import _is_selinux_enforcing

            _is_selinux_enforcing.cache_clear()
            args, _ = build_user_mounts("docker", acme)
        assert any("auth.json" in a for a in args)

    def test_builtin_file_mounts_unchanged(self, monkeypatch, tmp_path):
        # Built-ins keep the historical exists() behavior (their registry
        # entries are all files; no manifest can alter them).
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / ".credentials.json").write_text("{}")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            from lmer_cli.runtime import _is_selinux_enforcing

            _is_selinux_enforcing.cache_clear()
            args, _ = build_user_mounts("docker", HARNESSES["claude"])
        assert any(".credentials.json" in a for a in args)

    def test_announce_and_mount_agree_dedup(self, monkeypatch, tmp_path):
        # The launch-time 🔑 announce and build_user_mounts share one
        # predicate (plan_credential_mounts): a duplicate credential mounts
        # once AND announces once — they cannot drift.
        home = tmp_path / "home"
        (home / ".acme").mkdir(parents=True)
        (home / ".acme" / "auth.json").write_text("{}")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        root = tmp_path / "harnesses"
        dup = {"host_path": ".acme/auth.json", "container_path": "/home/developer/.acme/auth.json"}
        write_harness(root, "acme", dict(MINIMAL, credential_mounts=[dup, dict(dup)]))
        acme = load_user_harnesses(root)["acme"]
        to_mount, skipped = plan_credential_mounts(acme)
        assert skipped == []
        user_mounts = [m for m in to_mount if m.is_user]
        assert len(user_mounts) == 1
        assert user_mounts[0].host_path == ".acme/auth.json"
        assert user_mounts[0].is_user is True

    def test_build_user_mounts_consumes_precomputed_plan(self, monkeypatch, tmp_path):
        # cli.py computes the plan once and passes it to build_user_mounts so
        # the mount and the 🔑 announce read one evaluation, not two. A plan
        # passed in must be used verbatim (no recompute).
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        sentinel = PlannedCredentialMount(
            "acme", True, ".acme/auth.json", home / ".acme/auth.json",
            "/home/developer/.acme/auth.json", "rw",
        )
        args, _ = build_user_mounts("docker", HARNESSES["claude"], plan=([sentinel], []))
        # The sentinel came only from the passed plan — a recompute would
        # have found nothing (no files on disk).
        assert any(str(sentinel.host_file) in a for a in args)


class TestUserRunnerDispatch:
    def test_find_user_runner_found(self, monkeypatch, tmp_path):
        write_harness(tmp_path, "acme", MINIMAL)
        monkeypatch.setenv("LMER_HARNESSES_DIR", str(tmp_path))
        assert clone_and_exec.find_user_runner("acme") == str(
            tmp_path / "acme" / "runner.sh"
        )

    def test_find_user_runner_missing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LMER_HARNESSES_DIR", str(tmp_path))
        assert clone_and_exec.find_user_runner("acme") is None

    def test_find_user_runner_default_root(self, monkeypatch):
        monkeypatch.delenv("LMER_HARNESSES_DIR", raising=False)
        # The default is the container mount point — nonexistent on hosts.
        assert clone_and_exec.find_user_runner("acme") is None

    def _run_main_dispatch(self, tmp_path, env, token):
        """Run clone_and_exec.main in no-repo mode up to the dispatch step.

        Mirrors tests/test_clone_and_exec_no_repo.py's harness: clone/exec
        side effects are mocked; returns (rc, dispatched, execv_calls).
        """
        import os as _os

        dispatched = []
        execv_calls = []
        base_env = {
            "PATH": _os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path / "home"),
            "LMER_NO_REPO": "1",
            "LMER_WORK_REPO": "https://gitlab.example.com/example/work.git",
            "LMER_WORK_REPO_PATH": str(tmp_path / "work-repo"),
            **env,
        }
        with patch.dict(_os.environ, base_env, clear=True):
            with patch.object(clone_and_exec, "ensure_clone"), patch.object(
                clone_and_exec, "ensure_work_repo_directory"
            ), patch.object(clone_and_exec, "provision_documentation"), patch.object(
                clone_and_exec, "dispatch_runner", lambda r: dispatched.append(r) or 0
            ), patch.object(
                clone_and_exec.os, "execv", lambda p, a: execv_calls.append((p, a))
            ):
                rc = clone_and_exec.main(["--", token])
        return rc, dispatched, execv_calls

    def test_selected_user_harness_token_dispatches_via_bash(self, tmp_path):
        root = tmp_path / "harnesses"
        write_harness(root, "acme", MINIMAL)
        rc, dispatched, execv_calls = self._run_main_dispatch(
            tmp_path,
            {"LMER_HARNESS": "acme", "LMER_HARNESSES_DIR": str(root)},
            "acme-runner",
        )
        assert rc == 0
        assert dispatched == [["bash", str(root / "acme" / "runner.sh")]]
        assert execv_calls == []

    def test_selected_user_harness_missing_runner_exits_3(self, tmp_path, capsys):
        rc, dispatched, execv_calls = self._run_main_dispatch(
            tmp_path,
            {"LMER_HARNESS": "acme", "LMER_HARNESSES_DIR": str(tmp_path / "empty")},
            "acme-runner",
        )
        assert rc == 3
        assert dispatched == [] and execv_calls == []
        assert "No runner for harness 'acme'" in capsys.readouterr().err

    def test_unselected_runner_suffix_token_falls_through_to_bash(self, tmp_path):
        # A repo's own `test-runner` binary run via --exec must NOT be
        # intercepted as harness dispatch when it isn't the selected harness.
        rc, dispatched, execv_calls = self._run_main_dispatch(
            tmp_path, {"LMER_HARNESS": "claude"}, "test-runner"
        )
        assert dispatched == []
        assert len(execv_calls) == 1
        assert execv_calls[0][0] == "/bin/bash"
        assert "test-runner" in execv_calls[0][1][-1]

    def test_dispatch_runner_accepts_argv_list(self, monkeypatch):
        popen_calls = []

        class FakeProc:
            def wait(self):
                return 0

        monkeypatch.setattr(clone_and_exec, "mint_session_id", lambda: None)
        monkeypatch.setattr(clone_and_exec, "run_state_session_end", lambda: None)
        monkeypatch.setattr(clone_and_exec, "_forward_signals", lambda proc: None)
        monkeypatch.setattr(
            clone_and_exec.subprocess,
            "Popen",
            lambda cmd: popen_calls.append(cmd) or FakeProc(),
        )
        assert clone_and_exec.dispatch_runner(["bash", "/x/runner.sh"]) == 0
        assert clone_and_exec.dispatch_runner("/y/claude-runner.sh") == 0
        assert popen_calls == [["bash", "/x/runner.sh"], ["/y/claude-runner.sh"]]


class TestOpencodeWalkthroughDoc:
    """docs/USER-HARNESS-OPENCODE.md is paste-ready: its harness.json block
    must load through the real loader and produce the documented behavior
    (doc↔schema sync guard, same spirit as the mirror-guard tests)."""

    DOC = Path(__file__).parent.parent / "docs" / "USER-HARNESS-OPENCODE.md"

    def _manifest(self):
        text = self.DOC.read_text()
        block = re.search(r"```json\n(\{.*?\n)```", text, re.DOTALL)
        assert block, "no JSON block found in the walkthrough doc"
        return json.loads(block.group(1))

    def test_doc_manifest_loads(self, tmp_path):
        manifest = self._manifest()
        write_harness(tmp_path, "opencode", manifest)
        loaded = load_user_harnesses(tmp_path)
        assert set(loaded) == {"opencode"}
        h = loaded["opencode"]
        assert h.binary == "opencode"
        # The field-verified supervisor profile from the built-in evaluation
        # (#86): placeholder-prefix marker, Esc + double Ctrl-C quit chord.
        assert h.supervisor.ready_marker == b"Ask anything..."
        assert h.supervisor.quit_sequence == (b"\x1b", b"\x03", b"\x03")
        assert h.credential_mounts[0].host_path == ".local/share/opencode/auth.json"
        assert h.extra_env == (("OPENCODE_DISABLE_AUTOUPDATE", "1"),)

    def test_doc_manifest_exec_argv(self, tmp_path):
        write_harness(tmp_path, "opencode", self._manifest())
        h = load_user_harnesses(tmp_path)["opencode"]
        argv, warnings = build_exec_argv(
            h,
            "review this",
            model="anthropic/claude-sonnet-5",
            effort="max",
            unattended=True,
        )
        # opencode run --auto --model <m> --variant max <prompt> — the
        # non-interactive form verified against opencode 1.18.4.
        assert argv == [
            "opencode",
            "run",
            "--auto",
            "--model",
            "anthropic/claude-sonnet-5",
            "--variant",
            "max",
            "review this",
        ]
        assert warnings == []

    def test_doc_runner_provisions_via_fallback_arg(self):
        # The runner must use the three-arg harness_provision_config form so
        # work-repo agent-files still override the shipped base config.
        text = self.DOC.read_text()
        assert 'harness_provision_config "opencode/opencode.json"' in text
        assert "$HARNESS_DIR/agent-files/opencode.json" in text
        assert "harness_render_prompt_templates" in text


class TestCliSourceGuards:
    """Source guards against removal of the container passthrough entries
    (same pattern as the LMER_HARNESS guard in test_harness.py)."""

    def test_cli_env_dict_declares_harnesses_dir(self):
        source = CLI_PY.read_text()
        pattern = re.compile(
            r"""["']LMER_HARNESSES_DIR["']\s*:\s*CONTAINER_HARNESSES_DIR"""
        )
        assert pattern.search(source), (
            "LMER_HARNESSES_DIR entry missing from cli.py env dict"
        )

    def test_cli_env_dict_declares_harness_cache(self):
        source = CLI_PY.read_text()
        pattern = re.compile(r"""["']LMER_HARNESS_CACHE["']\s*:""")
        assert pattern.search(source), (
            "LMER_HARNESS_CACHE entry missing from cli.py env dict"
        )
