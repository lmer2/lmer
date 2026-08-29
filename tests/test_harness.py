"""Tests for the agent-harness registry (lmer_cli.harness) and its wiring.

Covers:
- registry entries are well-formed and consistent
- harness resolution precedence (explicit > LMER_HARNESS env > LMER_LLM_NAME
  model hint > default; the model hint is host-side only)
- unknown-harness failure modes (loud on host, fail-open in supervisor)
- per-harness credential mounts (mounts.build_user_mounts)
- per-harness supervisor profile resolution and env overrides
- cli.py source guards (LMER_HARNESS env dict entry, runner token dispatch)
- build cache-bust plumbing for --update-harness
"""

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from lmer_cli import harness as harness_mod
from lmer_cli import supervisor
from lmer_cli.build import build_image_local
from lmer_cli.harness import (
    DEFAULT_HARNESS,
    GENERIC_START_COMMAND,
    HARNESSES,
    MODEL_HARNESS_HINTS,
    UnknownHarnessError,
    get_harness,
    harness_for_model,
    implied_harness_name,
    missing_credential_mounts,
    resolve_harness,
    resolve_harness_name,
    resolve_harness_selection,
)
from lmer_cli.mounts import CONTAINER_HOME, build_user_mounts
from tests.conftest import strip_lmer_env
from work_repo.memory import agent_memory_dir

CLI_PY = Path(__file__).parent.parent / "src" / "lmer_cli" / "cli.py"


class TestRegistryShape:
    """Every registry entry must satisfy the framework's structural contract."""

    def test_default_harness_is_claude_and_registered(self):
        assert DEFAULT_HARNESS == "claude"
        assert DEFAULT_HARNESS in HARNESSES

    def test_expected_harnesses_present(self):
        assert set(HARNESSES) >= {"claude", "codex", "pi"}

    @pytest.mark.parametrize("name", sorted(HARNESSES))
    def test_entry_is_consistent(self, name):
        h = HARNESSES[name]
        assert h.name == name
        # The runner token/script naming convention is what clone_and_exec.py
        # keys dispatch on — every harness must follow it.
        assert h.runner_command == f"{name}-runner"
        assert h.runner_script == f"{name}-runner.sh"
        assert h.binary
        assert h.cache_bust_arg.endswith("_CACHE_BUST")
        assert isinstance(h.supervisor.ready_marker, bytes)
        assert h.supervisor.start_command
        assert isinstance(h.supervisor.quit_sequence, tuple)
        for step in h.supervisor.quit_sequence:
            assert isinstance(step, bytes) and step
        for cred in h.credential_mounts:
            assert not cred.host_path.startswith("/"), "host_path is home-relative"
            assert cred.container_path.startswith("/home/developer/")
            assert cred.mode in ("ro", "rw")

    def test_cache_bust_args_are_unique(self):
        args = [h.cache_bust_arg for h in HARNESSES.values()]
        assert len(args) == len(set(args))

    @pytest.mark.parametrize("name", sorted(HARNESSES))
    def test_runner_script_exists_and_is_executable(self, name):
        # dispatch_runner Popen([runner])s the script directly — a missing
        # exec bit (chmod regression) would 126 at session start.
        import os as _os

        script = Path(__file__).parent.parent / "libexec" / HARNESSES[name].runner_script
        assert script.is_file(), f"libexec/{HARNESSES[name].runner_script} missing"
        assert _os.access(script, _os.X_OK), f"{script} is not executable"

    def test_claude_profile_matches_historical_behavior(self):
        """The claude entry must reproduce the pre-registry constants exactly —
        this is the backward-compatibility contract for existing installs."""
        h = HARNESSES["claude"]
        assert h.runner_command == "claude-runner"
        assert h.supervisor.ready_marker == b"\xe2\x9d\xaf"  # ❯
        assert h.supervisor.start_command == "/start"
        assert h.supervisor.quit_sequence == (b"\x03", b"\x03")
        assert h.supervisor.ready_timeout is None
        hosts = [c.host_path for c in h.credential_mounts]
        assert hosts == [".claude/.credentials.json", ".claude.json"]

    def test_non_claude_harnesses_use_generic_start_command(self):
        for name in ("codex", "pi"):
            assert HARNESSES[name].supervisor.start_command == GENERIC_START_COMMAND
        assert "/Agents/global/hooks/start.sh" in GENERIC_START_COMMAND


class TestResolution:
    def test_default_when_nothing_configured(self, monkeypatch):
        monkeypatch.delenv("LMER_HARNESS", raising=False)
        assert resolve_harness_name() == "claude"

    def test_env_var_selects(self, monkeypatch):
        monkeypatch.setenv("LMER_HARNESS", "codex")
        assert resolve_harness_name() == "codex"

    def test_explicit_beats_env(self, monkeypatch):
        monkeypatch.setenv("LMER_HARNESS", "codex")
        assert resolve_harness_name("pi") == "pi"

    def test_name_is_case_insensitive_and_stripped(self, monkeypatch):
        monkeypatch.setenv("LMER_HARNESS", "  Codex ")
        assert resolve_harness_name() == "codex"

    def test_empty_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("LMER_HARNESS", "")
        assert resolve_harness_name() == "claude"

    def test_unknown_name_raises_with_known_list(self, monkeypatch):
        monkeypatch.delenv("LMER_HARNESS", raising=False)
        with pytest.raises(UnknownHarnessError) as exc:
            resolve_harness_name("aider")
        assert "aider" in str(exc.value)
        assert "claude" in str(exc.value)

    def test_get_harness_unknown_raises(self):
        with pytest.raises(UnknownHarnessError):
            get_harness("nope")

    def test_resolve_harness_returns_entry(self, monkeypatch):
        monkeypatch.setenv("LMER_HARNESS", "pi")
        assert resolve_harness().name == "pi"


class TestModelHintAutoselect:
    """LMER_LLM_NAME implies a harness when none is configured explicitly —
    word-bounded, case-insensitive matching on model family names."""

    @pytest.mark.parametrize(
        "model,expected",
        [
            ("anthropic/claude-sonnet-5", "claude"),
            ("claude-opus-4-8", "claude"),
            ("haiku", "claude"),
            ("fable", "claude"),
            ("Mythos-1", "claude"),
            ("gpt-5.2", "codex"),
            ("openai/gpt-5.2-codex", "codex"),
            ("GPT", "codex"),
            ("gpt-5.6-sol", "codex"),
            ("codex-mini-latest", "codex"),
            ("gpt-5.3-codex-spark", "codex"),
            ("o3", "codex"),
            ("o3-pro", "codex"),
            ("o4-mini", "codex"),
        ],
    )
    def test_word_bounded_matches(self, model, expected):
        assert harness_for_model(model) == expected

    @pytest.mark.parametrize(
        "model",
        [
            None,
            "",
            "gemini-2.5-pro",
            "chatgpt-like",  # no word boundary inside "chatgpt"
            "sonnetish",
            "corpus-1",  # "opus" embedded in a longer word must not match
            "turbo4",  # "o4" embedded in a longer word must not match
        ],
    )
    def test_no_match_yields_none(self, model):
        assert harness_for_model(model) is None

    def test_hint_targets_are_registered(self):
        for _, name in MODEL_HARNESS_HINTS:
            assert name in HARNESSES

    def test_model_hint_used_when_nothing_configured(self, monkeypatch):
        monkeypatch.delenv("LMER_HARNESS", raising=False)
        monkeypatch.setenv("LMER_LLM_NAME", "gpt-5.2")
        assert resolve_harness_selection() == ("codex", "model")

    def test_env_harness_beats_model_hint(self, monkeypatch):
        monkeypatch.setenv("LMER_HARNESS", "pi")
        monkeypatch.setenv("LMER_LLM_NAME", "gpt-5.2")
        assert resolve_harness_selection() == ("pi", "env")

    def test_flag_beats_model_hint(self, monkeypatch):
        monkeypatch.delenv("LMER_HARNESS", raising=False)
        monkeypatch.setenv("LMER_LLM_NAME", "anthropic/claude-sonnet-5")
        assert resolve_harness_selection("pi") == ("pi", "flag")

    def test_unmatched_model_falls_back_to_default(self, monkeypatch):
        monkeypatch.delenv("LMER_HARNESS", raising=False)
        monkeypatch.setenv("LMER_LLM_NAME", "gemini-2.5-pro")
        assert resolve_harness_selection() == (DEFAULT_HARNESS, "default")

    def test_selection_sources_without_model(self, monkeypatch):
        monkeypatch.delenv("LMER_HARNESS", raising=False)
        monkeypatch.delenv("LMER_LLM_NAME", raising=False)
        assert resolve_harness_selection() == (DEFAULT_HARNESS, "default")
        monkeypatch.setenv("LMER_HARNESS", "codex")
        assert resolve_harness_selection() == ("codex", "env")

    def test_container_side_resolver_ignores_model_hint(self, monkeypatch):
        """resolve_harness_name (lmer-supervisor's resolver) must not guess
        from LMER_LLM_NAME — the host forwards the resolved LMER_HARNESS, and
        a model-based guess in the container could mismatch the runner that
        was actually launched (e.g. an old host that only knows claude)."""
        monkeypatch.delenv("LMER_HARNESS", raising=False)
        monkeypatch.setenv("LMER_LLM_NAME", "gpt-5.2")
        assert resolve_harness_name() == DEFAULT_HARNESS


class TestImpliedHarnessName:
    """The shared fan-out precedence (issue #131): overlay LMER_HARNESS >
    model hint from the overlay's own LMER_LLM_NAME > inherited
    LMER_HARNESS > default. Shared between spawn-harness's select_harness
    and the host CLI's credential-mount computation."""

    def test_overlay_harness_wins_over_everything(self):
        overlay = {"LMER_HARNESS": "pi", "LMER_LLM_NAME": "gpt-5.6-sol"}
        merged = {**{"LMER_HARNESS": "claude"}, **overlay}
        assert implied_harness_name(overlay, merged) == "pi"

    def test_overlay_model_hint_beats_inherited_harness(self):
        # No overlay harness: the agent's own LLM name routes the child away
        # from the session.
        overlay = {"LMER_LLM_NAME": "gpt-5.6-sol"}
        merged = {"LMER_HARNESS": "claude", "LMER_LLM_NAME": "gpt-5.6-sol"}
        assert implied_harness_name(overlay, merged) == "codex"

    def test_inherited_model_never_outranks_inherited_harness(self):
        # An empty overlay runs the session's own harness. The inherited
        # LMER_LLM_NAME must NOT re-route it: at launch time the operator's
        # explicit harness beat that very model hint (--harness pi with
        # LMER_LLM_NAME=sonnet is a legitimate keys-via-API setup), and the
        # child inherits that settled choice, not a re-litigated one.
        merged = {"LMER_HARNESS": "pi", "LMER_LLM_NAME": "sonnet"}
        assert implied_harness_name({}, merged) == "pi"

    def test_inherited_harness_when_no_hint(self):
        merged = {"LMER_HARNESS": "pi", "LMER_LLM_NAME": "mystery-model"}
        assert implied_harness_name({}, merged) == "pi"

    def test_default_when_nothing_set(self):
        assert implied_harness_name({}, {}) == DEFAULT_HARNESS

    def test_overlay_name_normalized_but_not_validated(self):
        # Callers own validation — an unknown explicit name passes through
        # so each caller can fail in its own way.
        assert implied_harness_name({"LMER_HARNESS": " Codx "}, {}) == "codx"


class TestMissingCredentialMounts:
    """The warn-iff-ALL-missing policy is single-homed in
    missing_credential_mounts, shared by the launch-time (host) and
    spawn-time (container) credential warnings (issue #131)."""

    def test_all_missing_returns_every_mount(self):
        codex = get_harness("codex")
        missing = missing_credential_mounts(codex, lambda cred: False)
        assert missing == codex.credential_mounts

    def test_partial_credential_set_returns_empty(self):
        # pi's models.json is optional config — one present file silences.
        pi = get_harness("pi")
        missing = missing_credential_mounts(
            pi, lambda cred: cred.host_path == ".pi/agent/auth.json"
        )
        assert missing == ()

    def test_none_missing_returns_empty(self):
        assert missing_credential_mounts(get_harness("claude"), lambda cred: True) == ()


class TestCredentialMounts:
    def _fake_home(self, tmp_path, monkeypatch, files):
        for rel in files:
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{}")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)

    def test_default_harness_mounts_claude_credentials(self, tmp_path, monkeypatch):
        self._fake_home(tmp_path, monkeypatch, [".claude/.credentials.json", ".claude.json"])
        args, _ = build_user_mounts("docker")
        joined = " ".join(args)
        assert ".claude/.credentials.json:/home/developer/.claude/.credentials.json" in joined
        assert ".claude.json:/home/developer/.claude.json" in joined

    def test_codex_mounts_only_codex_auth(self, tmp_path, monkeypatch):
        self._fake_home(
            tmp_path, monkeypatch,
            [".claude/.credentials.json", ".codex/auth.json"],
        )
        args, _ = build_user_mounts("docker", get_harness("codex"))
        joined = " ".join(args)
        assert ".codex/auth.json:/home/developer/.codex/auth.json" in joined
        assert ".claude" not in joined.replace(".codex", "")

    def test_missing_host_files_are_skipped(self, tmp_path, monkeypatch):
        self._fake_home(tmp_path, monkeypatch, [])
        args, ssh = build_user_mounts("docker", get_harness("pi"))
        assert args == []
        assert ssh is False

    def test_pi_auth_path(self, tmp_path, monkeypatch):
        self._fake_home(tmp_path, monkeypatch, [".pi/agent/auth.json"])
        args, _ = build_user_mounts("docker", get_harness("pi"))
        assert any(
            ".pi/agent/auth.json:/home/developer/.pi/agent/auth.json" in a
            for a in args
        )

    def test_pi_models_json_mounted_read_only_when_present(self, tmp_path, monkeypatch):
        # pi's custom provider/model registry (e.g. a local llama.cpp
        # endpoint) must reach the container, or in-container pi rejects
        # models that only exist in the host's registry. It is hand-authored
        # config pi only reads, so it mounts ro (auth.json stays rw for
        # token refresh).
        self._fake_home(tmp_path, monkeypatch, [".pi/agent/models.json"])
        args, _ = build_user_mounts("docker", get_harness("pi"))
        assert any(
            ".pi/agent/models.json:/home/developer/.pi/agent/models.json:ro" in a
            for a in args
        )

    def test_pi_auth_json_stays_read_write(self, tmp_path, monkeypatch):
        self._fake_home(tmp_path, monkeypatch, [".pi/agent/auth.json"])
        args, _ = build_user_mounts("docker", get_harness("pi"))
        assert any(
            ".pi/agent/auth.json:/home/developer/.pi/agent/auth.json:rw" in a
            for a in args
        )

    def test_extra_harnesses_union_credential_mounts(self, tmp_path, monkeypatch):
        # --agents fan-out (issue #131): a codex-routed child from a claude
        # session needs ~/.codex/auth.json mounted alongside the session
        # harness's credentials.
        self._fake_home(
            tmp_path, monkeypatch,
            [".claude/.credentials.json", ".claude.json", ".codex/auth.json"],
        )
        args, _ = build_user_mounts(
            "docker", get_harness("claude"), [get_harness("codex")]
        )
        joined = " ".join(args)
        assert ".claude/.credentials.json:/home/developer/.claude/.credentials.json" in joined
        assert ".codex/auth.json:/home/developer/.codex/auth.json" in joined

    def test_extra_harnesses_missing_host_files_skipped(self, tmp_path, monkeypatch):
        self._fake_home(tmp_path, monkeypatch, [".claude/.credentials.json"])
        args, _ = build_user_mounts(
            "docker", get_harness("claude"), [get_harness("codex")]
        )
        joined = " ".join(args)
        assert ".claude/.credentials.json" in joined
        assert ".codex" not in joined

    def test_duplicate_harness_mounts_credentials_once(self, tmp_path, monkeypatch):
        self._fake_home(tmp_path, monkeypatch, [".codex/auth.json"])
        codex = get_harness("codex")
        args, _ = build_user_mounts("docker", codex, [codex, codex])
        assert len([a for a in args if ".codex/auth.json" in a]) == 1

    def test_no_extra_harnesses_matches_session_only_behavior(self, tmp_path, monkeypatch):
        self._fake_home(
            tmp_path, monkeypatch,
            [".claude/.credentials.json", ".claude.json", ".codex/auth.json"],
        )
        baseline, _ = build_user_mounts("docker", get_harness("claude"))
        with_empty_extra, _ = build_user_mounts("docker", get_harness("claude"), [])
        assert with_empty_extra == baseline
        assert ".codex" not in " ".join(baseline)


class TestSupervisorProfileResolution:
    def _ns(self, **over):
        import argparse

        defaults = dict(
            fastapi=False, manual_start=False, fastapi_port_range=None,
            fastapi_host=None, fastapi_token=None, auto_start_delay=None,
            auto_start_nudge_delay=None, auto_start_ready_timeout=None,
            start_prompt_delay=None,
        )
        defaults.update(over)
        return argparse.Namespace(**defaults)

    def test_claude_defaults_unchanged(self, monkeypatch):
        monkeypatch.delenv("LMER_HARNESS", raising=False)
        monkeypatch.delenv("LMER_AUTO_START_READY_MARKER", raising=False)
        monkeypatch.delenv("LMER_START_COMMAND", raising=False)
        monkeypatch.delenv("LMER_QUIT_SEQUENCE", raising=False)
        opts = supervisor._resolve_options(self._ns())
        assert opts["auto_start_ready_marker"] == supervisor.DEFAULT_AUTO_START_READY_MARKER
        assert opts["start_command"] == "/start"
        assert opts["quit_sequence"] == (b"\x03", b"\x03")
        assert opts["auto_start_ready_timeout"] == supervisor.DEFAULT_AUTO_START_READY_TIMEOUT

    def test_codex_profile_applies(self, monkeypatch):
        monkeypatch.setenv("LMER_HARNESS", "codex")
        monkeypatch.delenv("LMER_AUTO_START_READY_MARKER", raising=False)
        monkeypatch.delenv("LMER_START_COMMAND", raising=False)
        monkeypatch.delenv("LMER_QUIT_SEQUENCE", raising=False)
        opts = supervisor._resolve_options(self._ns())
        assert opts["auto_start_ready_marker"] == b"\xe2\x80\xba"  # ›
        assert opts["start_command"] == GENERIC_START_COMMAND
        assert opts["quit_sequence"] == (b"/quit\r",)

    def test_pi_profile_ready_timeout_default(self, monkeypatch):
        monkeypatch.setenv("LMER_HARNESS", "pi")
        monkeypatch.delenv("LMER_AUTO_START_READY_TIMEOUT", raising=False)
        opts = supervisor._resolve_options(self._ns())
        assert opts["auto_start_ready_timeout"] == 60.0

    def test_env_overrides_beat_profile(self, monkeypatch):
        monkeypatch.setenv("LMER_HARNESS", "codex")
        monkeypatch.setenv("LMER_AUTO_START_READY_MARKER", "$")
        monkeypatch.setenv("LMER_START_COMMAND", "/begin")
        monkeypatch.setenv("LMER_QUIT_SEQUENCE", r"\x03|\x03")
        monkeypatch.setenv("LMER_AUTO_START_READY_TIMEOUT", "3.5")
        opts = supervisor._resolve_options(self._ns())
        assert opts["auto_start_ready_marker"] == b"$"
        assert opts["start_command"] == "/begin"
        assert opts["quit_sequence"] == (b"\x03", b"\x03")
        assert opts["auto_start_ready_timeout"] == 3.5

    def test_ready_marker_override_decodes_escapes(self, monkeypatch):
        # The marker override shares decode_escape_bytes with
        # LMER_QUIT_SEQUENCE and the user-harness manifest fields: escapes
        # spell control bytes, plain UTF-8 text passes byte-for-byte.
        monkeypatch.setenv("LMER_HARNESS", "codex")
        monkeypatch.setenv("LMER_AUTO_START_READY_MARKER", r"\x1b[?25h")
        opts = supervisor._resolve_options(self._ns())
        assert opts["auto_start_ready_marker"] == b"\x1b[?25h"
        monkeypatch.setenv("LMER_AUTO_START_READY_MARKER", "❯")
        opts = supervisor._resolve_options(self._ns())
        assert opts["auto_start_ready_marker"] == b"\xe2\x9d\xaf"

    def test_cli_flag_beats_profile_ready_timeout(self, monkeypatch):
        monkeypatch.setenv("LMER_HARNESS", "pi")
        monkeypatch.delenv("LMER_AUTO_START_READY_TIMEOUT", raising=False)
        opts = supervisor._resolve_options(self._ns(auto_start_ready_timeout=7.0))
        assert opts["auto_start_ready_timeout"] == 7.0

    def test_unknown_harness_falls_back_to_claude_with_warning(self, monkeypatch, capsys):
        monkeypatch.setenv("LMER_HARNESS", "aider")
        opts = supervisor._resolve_options(self._ns())
        assert opts["start_command"] == "/start"
        assert "aider" in capsys.readouterr().err


class TestParseQuitSequence:
    def test_two_ctrl_c(self):
        assert supervisor._parse_quit_sequence(r"\x03|\x03") == (b"\x03", b"\x03")

    def test_typed_command(self):
        assert supervisor._parse_quit_sequence(r"/quit\r") == (b"/quit\r",)

    def test_empty_disables(self):
        assert supervisor._parse_quit_sequence("") == ()

    def test_blank_steps_are_dropped(self):
        assert supervisor._parse_quit_sequence(r"|\x04|") == (b"\x04",)

    def test_unicode_escape_above_latin1_encodes_utf8(self):
        # \uXXXX escapes above U+00FF must not crash the supervisor at
        # startup; they emit the character's UTF-8 bytes.
        assert supervisor._parse_quit_sequence("\\u276f") == ("❯".encode("utf-8"),)

    def test_literal_utf8_round_trips(self):
        # A literal multi-byte character (already UTF-8 on the wire) must
        # come out as its original bytes, not double-encoded.
        assert supervisor._parse_quit_sequence("❯") == ("❯".encode("utf-8"),)


class TestInjectionUsesProfile:
    def test_auto_start_types_custom_command(self):
        sink: list[bytes] = []
        supervisor._inject_auto_start(
            lambda d: (sink.append(d), len(d))[1],
            nudge_count=0, nudge_delay=0,
            start_command="do the thing",
        )
        assert sink == [b"do the thing\r"]

    def test_auto_start_default_is_start_slash_command(self):
        sink: list[bytes] = []
        supervisor._inject_auto_start(
            lambda d: (sink.append(d), len(d))[1], nudge_count=0, nudge_delay=0
        )
        assert sink == [b"/start\r"]

    def test_shutdown_chord_custom_sequence(self):
        sink: list[bytes] = []
        supervisor._inject_shutdown_chord(
            lambda d: (sink.append(d), len(d))[1], gap=0, sequence=(b"/quit\r",)
        )
        assert sink == [b"/quit\r"]

    def test_self_shutdown_passes_sequence(self, monkeypatch):
        seen: list[tuple] = []
        monkeypatch.setattr(
            supervisor, "_inject_shutdown_chord",
            lambda w, g, s: seen.append(s),
        )
        monkeypatch.setattr(supervisor, "_wait_child_exit", lambda pid, t: True)
        supervisor._self_shutdown(lambda d: len(d), 4321, quit_sequence=(b"/exit\r",))
        assert seen == [(b"/exit\r",)]


class TestCliSourceGuards:
    """Source-level guards against accidental removal of the harness wiring
    (same pattern as the LMER_REASONING_EFFORT passthrough guard)."""

    def test_cli_env_dict_declares_lmer_harness(self):
        source = CLI_PY.read_text()
        pattern = re.compile(r"""["']LMER_HARNESS["']\s*:\s*harness\.name""")
        assert pattern.search(source), "LMER_HARNESS entry missing from cli.py env dict"

    def test_cli_uses_registry_runner_command(self):
        source = CLI_PY.read_text()
        assert "harness.runner_command" in source, (
            "cli.py must dispatch the container runner via the harness registry"
        )
        assert re.search(r"""^\s*["']claude-runner["'],\s*$""", source, re.M) is None, (
            "cli.py must not hardcode the claude-runner token anymore"
        )

    def test_cli_resolves_harness_after_early_env_load(self):
        """LMER_HARNESS/LMER_LLM_NAME set only in a .env file must be honored:
        the docs promise ~/.lmer/.env works, which requires main() to resolve
        the harness AFTER the early .env-into-os.environ load, not before."""
        source = CLI_PY.read_text()
        load_pos = source.find("early_env_file_sources[key]")
        resolve_pos = source.find("resolve_harness_selection(ns.harness)")
        assert load_pos != -1 and resolve_pos != -1
        assert load_pos < resolve_pos, (
            "resolve_harness_selection(ns.harness) must run after the early "
            ".env load so LMER_HARNESS/LMER_LLM_NAME from .env files take effect"
        )


class TestShowEnvWithBadHarness:
    """A typo'd LMER_HARNESS must not kill `lmer --show-env` before the env
    table renders — the table is exactly the diagnostic for finding where the
    bad value comes from (host export vs. which .env file)."""

    @pytest.fixture(autouse=True)
    def _clean_lmer_env(self, monkeypatch):
        # These two drive the real main(), which resolves every launch input
        # from the ambient environment — so anything the session carries can
        # fail the launch before the harness error under test is reached
        # (a session launched with --agents leaves LMER_AGENTS behind, and
        # its names resolve against a presets file this process cannot see).
        strip_lmer_env(monkeypatch)

    def test_show_env_renders_table_before_harness_error(self, monkeypatch, tmp_path, capsys):
        from lmer_cli import cli

        monkeypatch.setenv("LMER_HARNESS", "not-a-harness")
        monkeypatch.chdir(tmp_path)  # no stray cwd .env in the early load

        rc = cli.main(["--show-env"])
        out = capsys.readouterr().out

        assert rc == 2
        assert "Unknown harness 'not-a-harness'" in out
        assert "LMER Environment Configuration" in out
        assert "LMER_HARNESS" in out

    def test_without_show_env_still_fails_fast(self, monkeypatch, tmp_path, capsys):
        from lmer_cli import cli

        monkeypatch.setenv("LMER_HARNESS", "not-a-harness")
        monkeypatch.chdir(tmp_path)

        # A task name is required to get past arg validation; harness
        # resolution fails before any taskdef/container work happens.
        rc = cli.main(["review"])
        out = capsys.readouterr().out

        assert rc == 2
        assert "Unknown harness 'not-a-harness'" in out
        assert "LMER Environment Configuration" not in out


class TestBuildCacheBust:
    def _build_cmd(self, tmp_path, **kwargs):
        with patch("lmer_cli.build.subprocess.call", return_value=0) as call, \
             patch("lmer_cli.build._build_provenance", return_value="test"):
            build_image_local("docker", "img:v1", tmp_path, pull=False, **kwargs)
            return call.call_args[0][0]

    def test_no_bust_args_by_default(self, tmp_path):
        cmd = self._build_cmd(tmp_path)
        assert not any("CACHE_BUST" in c for c in cmd)

    def test_update_claude_legacy_spelling(self, tmp_path):
        cmd = self._build_cmd(tmp_path, update_claude=True)
        assert any(c.startswith("CLAUDE_CACHE_BUST=") for c in cmd)

    def test_update_harnesses_bust_their_args(self, tmp_path):
        cmd = self._build_cmd(tmp_path, update_harnesses=["codex", "pi"])
        assert any(c.startswith("CODEX_CACHE_BUST=") for c in cmd)
        assert any(c.startswith("PI_CACHE_BUST=") for c in cmd)
        assert not any(c.startswith("CLAUDE_CACHE_BUST=") for c in cmd)

    def test_update_claude_and_harnesses_deduplicate(self, tmp_path):
        cmd = self._build_cmd(tmp_path, update_claude=True, update_harnesses=["claude"])
        assert sum(1 for c in cmd if c.startswith("CLAUDE_CACHE_BUST=")) == 1


class TestHandleBuildUpdateHarnessCli:
    """`lmer build --update-harness` validation at the CLI (_handle_build)
    level: unknown names must exit 2 before any runtime work, and 'all' must
    expand to the full registry."""

    def test_unknown_harness_exits_2_before_runtime_detection(self, capsys):
        # Validation runs before detect_runtime, so no mocks are needed —
        # a container runtime is never touched.
        from lmer_cli.cli import _handle_build

        rc = _handle_build(["--update-harness", "not-a-harness"])
        out = capsys.readouterr().out

        assert rc == 2
        assert "not-a-harness" in out
        assert "all" in out  # the error names the valid options

    def test_all_expands_to_full_registry(self, monkeypatch):
        from lmer_cli import cli

        monkeypatch.setenv("LMER_IMAGE", "img:test")
        with patch.object(cli, "detect_runtime", return_value="docker"), \
             patch.object(cli, "build_image", return_value=True) as build:
            rc = cli._handle_build(["--update-harness", "all"])

        assert rc == 0
        assert build.call_args.kwargs["update_harnesses"] == sorted(HARNESSES)

    def test_update_claude_flag_is_alias_for_claude(self, monkeypatch):
        from lmer_cli import cli

        monkeypatch.setenv("LMER_IMAGE", "img:test")
        with patch.object(cli, "detect_runtime", return_value="docker"), \
             patch.object(cli, "build_image", return_value=True) as build:
            rc = cli._handle_build(["--update-claude"])

        assert rc == 0
        assert build.call_args.kwargs["update_harnesses"] == ["claude"]


class TestExecProfile:
    """Non-interactive exec profiles (spawn-harness child invocations)."""

    @pytest.mark.parametrize("name", sorted(HARNESSES))
    def test_every_harness_has_exec_profile(self, name):
        profile = HARNESSES[name].exec_profile
        assert profile is not None
        assert profile.base_args
        # Placeholders must be present so the builder can substitute values.
        if profile.model_args:
            assert any("{model}" in arg for arg in profile.model_args)
        if profile.effort_args:
            assert any("{effort}" in arg for arg in profile.effort_args)
        assert profile.effort_max_value in ("max", *harness_mod.EXEC_EFFORT_TIERS)

    def test_unattended_children_can_run_permission_free(self):
        # Unattended children cannot answer prompts; each profile must carry
        # its harness's bypass posture (container is the boundary) — in the
        # dedicated permission_bypass_args field, NOT base_args, so only an
        # explicit build_exec_argv(unattended=True) caller gets it.
        assert HARNESSES["claude"].exec_profile.permission_bypass_args == (
            "--dangerously-skip-permissions",
        )
        assert HARNESSES["codex"].exec_profile.permission_bypass_args == (
            "--dangerously-bypass-approvals-and-sandbox",
        )
        assert HARNESSES["pi"].exec_profile.permission_bypass_args == ("--no-approve",)

    @pytest.mark.parametrize("name", sorted(HARNESSES))
    def test_no_bypass_flags_hide_in_base_args(self, name):
        # The security-posture decision must never ride in neutral registry
        # data a future consumer inherits silently.
        for arg in HARNESSES[name].exec_profile.base_args:
            assert "dangerous" not in arg
            assert arg != "--no-approve"

    def test_children_are_non_interactive(self):
        assert "-p" in HARNESSES["claude"].exec_profile.base_args
        assert HARNESSES["codex"].exec_profile.base_args[0] == "exec"
        assert "-p" in HARNESSES["pi"].exec_profile.base_args

    def test_stateless_children_leave_no_sessions(self):
        assert "--no-session-persistence" in HARNESSES["claude"].exec_profile.base_args
        assert "--ephemeral" in HARNESSES["codex"].exec_profile.base_args
        assert "--no-session" in HARNESSES["pi"].exec_profile.base_args


class TestMapExecEffort:
    @pytest.mark.parametrize("name", sorted(HARNESSES))
    @pytest.mark.parametrize("effort", [None, "", "auto", "AUTO"])
    def test_auto_and_unset_yield_no_value(self, name, effort):
        value, warning = harness_mod.map_exec_effort(HARNESSES[name], effort)
        assert value is None
        assert warning is None

    @pytest.mark.parametrize("name", sorted(HARNESSES))
    @pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh"])
    def test_shared_tiers_pass_through(self, name, effort):
        value, warning = harness_mod.map_exec_effort(HARNESSES[name], effort)
        assert value == effort
        assert warning is None

    def test_max_maps_per_harness(self):
        # claude accepts max natively; codex tops out at xhigh; pi mirrors
        # its interactive runner's conservative max→xhigh mapping.
        assert harness_mod.map_exec_effort(HARNESSES["claude"], "max") == ("max", None)
        assert harness_mod.map_exec_effort(HARNESSES["pi"], "max") == ("xhigh", None)
        assert harness_mod.map_exec_effort(HARNESSES["codex"], "max") == ("xhigh", None)

    def test_case_and_whitespace_normalized(self):
        value, warning = harness_mod.map_exec_effort(HARNESSES["claude"], "  HIGH ")
        assert value == "high"
        assert warning is None

    @pytest.mark.parametrize("name", sorted(HARNESSES))
    def test_unknown_tier_warns_and_skips(self, name):
        value, warning = harness_mod.map_exec_effort(HARNESSES[name], "turbo")
        assert value is None
        assert "turbo" in warning
        assert "low|medium|high|xhigh|max|auto" in warning


class TestBuildExecArgv:
    def test_claude_full_invocation(self):
        argv, warnings = harness_mod.build_exec_argv(
            HARNESSES["claude"], "review this", model="opus", effort="max",
            unattended=True,
        )
        assert argv == [
            "claude",
            "-p",
            "--no-session-persistence",
            "--dangerously-skip-permissions",
            "--model",
            "opus",
            "--effort",
            "max",
            "--",
            "review this",
        ]
        assert warnings == []

    def test_codex_full_invocation(self):
        argv, warnings = harness_mod.build_exec_argv(
            HARNESSES["codex"], "review this", model="gpt-5.2", effort="max",
            unattended=True,
        )
        assert argv == [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--dangerously-bypass-approvals-and-sandbox",
            "--model",
            "gpt-5.2",
            "-c",
            "model_reasoning_effort=xhigh",
            "--",
            "review this",
        ]
        assert warnings == []

    def test_pi_full_invocation(self):
        argv, warnings = harness_mod.build_exec_argv(
            HARNESSES["pi"], "review this", model="sonnet", effort="high",
            unattended=True,
        )
        assert argv == [
            "pi",
            "-p",
            "--no-session",
            "--no-approve",
            "--model",
            "sonnet",
            "--thinking",
            "high",
            "review this",
        ]
        assert warnings == []

    @pytest.mark.parametrize("name", sorted(HARNESSES))
    def test_attended_default_omits_permission_bypass(self, name):
        # unattended=True is an explicit opt-in: without it, no bypass flag
        # may appear — a future consumer must choose permission-free
        # children knowingly, never inherit them from the profile.
        h = HARNESSES[name]
        argv, _ = harness_mod.build_exec_argv(h, "p", model="m", effort="low")
        for flag in h.exec_profile.permission_bypass_args:
            assert flag not in argv

    @pytest.mark.parametrize("name", sorted(HARNESSES))
    def test_prompt_is_always_last(self, name):
        argv, _ = harness_mod.build_exec_argv(
            HARNESSES[name], "the prompt", model="m", effort="low"
        )
        assert argv[-1] == "the prompt"

    @pytest.mark.parametrize("name", sorted(HARNESSES))
    def test_bare_invocation_omits_model_and_effort(self, name):
        h = HARNESSES[name]
        argv, warnings = harness_mod.build_exec_argv(h, "p")
        sentinel = ["--"] if h.exec_profile.dashdash_before_prompt else []
        assert argv == [h.binary, *h.exec_profile.base_args, *sentinel, "p"]
        assert warnings == []

    def test_unknown_effort_warns_but_still_builds(self):
        h = HARNESSES["claude"]
        argv, warnings = harness_mod.build_exec_argv(h, "p", effort="turbo")
        assert argv == [h.binary, *h.exec_profile.base_args, "--", "p"]
        assert len(warnings) == 1 and "turbo" in warnings[0]

    @pytest.mark.parametrize("name", ["claude", "codex"])
    def test_dash_prompt_protected_by_sentinel(self, name):
        # A prompt starting with '-' must never be parsed as child flags.
        argv, _ = harness_mod.build_exec_argv(HARNESSES[name], "--version")
        assert argv[-2:] == ["--", "--version"]

    def test_dash_prompt_rejected_without_sentinel(self):
        # pi's parser has no documented '--' handling; fail instead of
        # letting the prompt rebind the child's command line.
        with pytest.raises(ValueError, match="starts with '-'"):
            harness_mod.build_exec_argv(HARNESSES["pi"], "  --version")


class TestMemoryDirDeclaration:
    """Where a harness keeps its agent memory (issue #325).

    Two things make the declaration usable: only a harness with a native memory
    feature declares one, and the path it declares is the path the work-repo
    route already reads and writes."""

    def test_only_claude_declares_one_today(self):
        """A declaration for a harness with no memory feature would have the
        platform mount a path nothing reads."""
        declared = {
            name for name, harness in HARNESSES.items() if harness.memory_dir
        }
        assert declared == {"claude"}

    def test_the_declaration_is_an_absolute_container_path(self):
        memory_dir = HARNESSES["claude"].memory_dir
        # Against the constant rather than the literal: the whole point of a
        # guard here is to fail when the container home moves, and a hardcoded
        # "/home/developer/" would still pass while the symlink landed at a path
        # nothing reads.
        assert memory_dir.startswith(f"{CONTAINER_HOME}/")
        # Below session_dir, which is exactly why it needs a mount of its own:
        # the transcript mount lands on the parent.
        assert memory_dir.startswith(f"{HARNESSES['claude'].session_dir}/")

    def test_it_names_the_directory_the_work_repo_route_uses(self, monkeypatch):
        """One path, two consumers (``work memory`` for workers, the platform mount
        for the assistant), so it is pinned against the writer.

        ``HOME`` is forced to :data:`CONTAINER_HOME`, not a literal: both sides
        derive from that constant, so moving it fails here rather than passing on
        a coincidence (review of !263)."""
        monkeypatch.setenv("HOME", CONTAINER_HOME)
        monkeypatch.delenv("LMER_AGENT_MEMORY_DIR", raising=False)
        assert str(agent_memory_dir()) == HARNESSES["claude"].memory_dir

    def test_the_prompt_fragment_names_the_same_directory(self):
        """The third spelling: unpinned, the fragment drifts into instructions
        naming a directory nobody mounts."""
        fragment = (
            Path(__file__).parent.parent / "prompts" / "agent-memory.md"
        ).read_text(encoding="utf-8")
        assert HARNESSES["claude"].memory_dir in fragment
