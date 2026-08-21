"""Tests for the --agents fan-out flag (issue #130).

Contract under test
-------------------
``lmer <task> <target> --agents=a,b`` (or ``LMER_AGENTS=a,b``; the flag wins,
matching --preset/LMER_PRESET) resolves each name against LMER_PRESETS_FILE
host-side and forwards only the resolved per-agent config into the
container, under scoped names the container has no other source for:
``LMER_SPAWN_AGENTS`` (names, csv) and ``LMER_SPAWN_AGENTS_CONFIG`` (JSON
``{name: {"env": {...}, "prompt": "..."?}}``), consumed in-container by
spawn-harness. A name matching no preset falls back to the model route when
its model family implies a harness (a case-variant of a defined preset is
rejected with a did-you-mean instead); a name that is neither a preset nor a
routable model fails fast (exit 2) — spawning fewer agents than asked is
never silent. ``--harness``/``--prompt`` in a preset's args fold into the
child config; other launch-shaping preset config (checkout/service,
remaining args) is ignored with a warning: children are subprocesses of the
session container, not new sessions.

Behavioral tests run the real ``main()`` with the container runtime mocked
out (the test_lmer_cli_slack_target harness) and inspect the env dict the CLI
hands to ``build_container_env``.
"""

import json
import os
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lmer_cli.harness import get_harness
from lmer_cli.presets import Preset, resolve_agent_presets
from tests.test_lmer_cli_slack_target import (
    _BASE_ENV,
    _make_main_mocks,
    REPO_URL,
)

CLI_PY = Path(__file__).parent.parent / "src" / "lmer_cli" / "cli.py"

_EXEC_ARGS = ["--no-task", "--exec", "true", REPO_URL]

PRESETS = {
    "opus-review": Preset(name="opus-review", env={"LMER_LLM_NAME": "opus"}),
    "sol-review": Preset(
        name="sol-review",
        env={"LMER_HARNESS": "codex", "LMER_LLM_NAME": "gpt-5.6-sol"},
    ),
    "svc": Preset(name="svc", checkout="/srv/x", service="mysvc", args=["--ports", "2"]),
    "typo": Preset(name="typo", env={"LMER_HARNESS": "codx"}),
    # Dual-use presets: configure the harness via args (the --preset
    # spelling) and optionally carry a canned prompt — the shape that
    # surfaced in real usage.
    "sol-cli": Preset(
        name="sol-cli",
        env={"LMER_LLM_NAME": "gpt-5.6-sol"},
        args=["--harness", "codex"],
    ),
    "sol-second": Preset(
        name="sol-second",
        env={"LMER_LLM_NAME": "gpt-5.6-sol"},
        args=["--harness", "codex", "--prompt", "Second pass from scratch."],
    ),
}


class TestResolveAgentPresets:
    def test_resolves_in_selection_order(self):
        resolved, warnings, err = resolve_agent_presets("sol-review,opus-review", PRESETS)
        assert err is None and warnings == []
        assert list(resolved) == ["sol-review", "opus-review"]
        assert resolved["opus-review"] == {"env": {"LMER_LLM_NAME": "opus"}}

    def test_whitespace_and_empty_segments_tolerated(self):
        resolved, _, err = resolve_agent_presets(" opus-review , ,sol-review ", PRESETS)
        assert err is None
        assert list(resolved) == ["opus-review", "sol-review"]

    def test_duplicate_warns_and_keeps_first(self):
        resolved, warnings, err = resolve_agent_presets(
            "opus-review,opus-review", PRESETS
        )
        assert err is None
        assert list(resolved) == ["opus-review"]
        assert any("duplicate" in w for w in warnings)

    def test_unknown_hintless_name_errors_listing_available(self):
        resolved, _, err = resolve_agent_presets("opus-review,nope", PRESETS)
        assert resolved is None
        assert "nope" in err
        assert "opus-review" in err and "sol-review" in err
        assert "model name" in err

    def test_non_preset_model_name_uses_model_route(self):
        # `--agents=fable` without a preset entry: the model family implies
        # the harness, so a synthesized model-only agent is resolved.
        resolved, warnings, err = resolve_agent_presets("fable", PRESETS)
        assert err is None
        assert resolved == {"fable": {"env": {"LMER_LLM_NAME": "fable"}}}
        assert any("model route" in w and "claude" in w for w in warnings)

    def test_model_route_works_for_codex_families(self):
        resolved, _, err = resolve_agent_presets("gpt-5.6-luna", PRESETS)
        assert err is None
        assert resolved["gpt-5.6-luna"] == {"env": {"LMER_LLM_NAME": "gpt-5.6-luna"}}

    def test_mixed_selection_resolves_both_paths_in_order(self):
        # The documented form: a model-route name and a real preset in one
        # selection — both paths must compose in a single pass.
        resolved, warnings, err = resolve_agent_presets("fable,sol-review", PRESETS)
        assert err is None
        assert list(resolved) == ["fable", "sol-review"]
        assert resolved["fable"] == {"env": {"LMER_LLM_NAME": "fable"}}
        assert resolved["sol-review"] == {
            "env": {"LMER_HARNESS": "codex", "LMER_LLM_NAME": "gpt-5.6-sol"}
        }
        assert any("model route" in w for w in warnings)

    def test_case_variant_of_preset_rejected_not_model_routed(self):
        # '--agents=Fable' with a 'fable' preset defined must fail loudly
        # (did-you-mean), never silently drop the preset's env via the
        # case-insensitive model hint.
        presets = {"fable": Preset(name="fable", env={"LMER_REASONING_EFFORT": "high"})}
        resolved, _, err = resolve_agent_presets("Fable", presets)
        assert resolved is None
        assert "did you mean 'fable'" in err
        assert "case-sensitive" in err

    def test_defined_preset_wins_over_model_route(self):
        # A preset literally named after a model must resolve as the preset.
        presets = {
            "fable": Preset(name="fable", env={"LMER_REASONING_EFFORT": "high"}),
        }
        resolved, warnings, err = resolve_agent_presets("fable", presets)
        assert err is None and warnings == []
        assert resolved["fable"] == {"env": {"LMER_REASONING_EFFORT": "high"}}

    def test_empty_selection_errors(self):
        resolved, _, err = resolve_agent_presets(" , ,", PRESETS)
        assert resolved is None
        assert err is not None

    def test_launch_shaping_fields_warn_but_resolve(self):
        resolved, warnings, err = resolve_agent_presets("svc", PRESETS)
        assert err is None
        assert list(resolved) == ["svc"]
        note = " ".join(warnings)
        assert "checkout" in note and "service" in note and "args" in note

    def test_unknown_harness_in_preset_env_errors_at_resolve_time(self):
        # A typo'd LMER_HARNESS must fail at launch, not hours later when
        # spawn-harness runs inside the session.
        resolved, _, err = resolve_agent_presets("typo", PRESETS)
        assert resolved is None
        assert "codx" in err and "claude" in err

    def test_args_harness_folds_into_env(self):
        # The dual-use shape: --preset applies args' --harness as a flag;
        # --agents must honor the same configuration via the env overlay.
        resolved, warnings, err = resolve_agent_presets("sol-cli", PRESETS)
        assert err is None and warnings == []
        assert resolved["sol-cli"] == {
            "env": {"LMER_LLM_NAME": "gpt-5.6-sol", "LMER_HARNESS": "codex"}
        }

    def test_args_harness_folds_for_hintless_model(self):
        # The case that actually bit: a custom model name matches no harness
        # hint, so without the fold the child would inherit the session's
        # harness instead of the preset's.
        presets = {
            "a": Preset(
                name="a",
                env={"LMER_LLM_NAME": "my-custom-model"},
                args=["--harness", "codex"],
            ),
        }
        resolved, warnings, err = resolve_agent_presets("a", presets)
        assert err is None and warnings == []
        assert resolved["a"]["env"]["LMER_HARNESS"] == "codex"

    def test_args_harness_equals_form_and_last_wins(self):
        presets = {
            "a": Preset(name="a", args=["--harness=claude", "--harness", "codex"]),
        }
        resolved, warnings, err = resolve_agent_presets("a", presets)
        assert err is None and warnings == []
        assert resolved["a"]["env"]["LMER_HARNESS"] == "codex"

    def test_args_harness_beats_preset_env(self):
        # Mirrors the CLI consumer: an args flag wins over a preset env var.
        presets = {
            "a": Preset(
                name="a", env={"LMER_HARNESS": "claude"}, args=["--harness", "codex"]
            ),
        }
        resolved, _, err = resolve_agent_presets("a", presets)
        assert err is None
        assert resolved["a"]["env"]["LMER_HARNESS"] == "codex"

    def test_unknown_harness_in_args_errors(self):
        presets = {"a": Preset(name="a", args=["--harness", "nope"])}
        resolved, _, err = resolve_agent_presets("a", presets)
        assert resolved is None
        assert "nope" in err

    def test_args_prompt_carried_as_preamble(self):
        resolved, warnings, err = resolve_agent_presets("sol-second", PRESETS)
        assert err is None and warnings == []
        assert resolved["sol-second"] == {
            "env": {"LMER_LLM_NAME": "gpt-5.6-sol", "LMER_HARNESS": "codex"},
            "prompt": "Second pass from scratch.",
        }

    def test_leftover_args_warn_and_are_ignored(self):
        presets = {
            "a": Preset(name="a", args=["--harness", "codex", "--ports", "2"]),
        }
        resolved, warnings, err = resolve_agent_presets("a", presets)
        assert err is None
        assert resolved["a"]["env"]["LMER_HARNESS"] == "codex"
        assert any("--ports 2" in w for w in warnings)


@pytest.fixture
def presets_file(tmp_path):
    path = tmp_path / "presets.json"
    path.write_text(
        json.dumps(
            {
                "opus-review": {"env": {"LMER_LLM_NAME": "opus"}},
                "sol-review": {
                    "env": {"LMER_HARNESS": "codex", "LMER_LLM_NAME": "gpt-5.6-sol"}
                },
                "svc": {"checkout": str(tmp_path), "service": "mysvc"},
                "typo": {"env": {"LMER_HARNESS": "codx"}},
                "sol-second": {
                    "env": {"LMER_LLM_NAME": "gpt-5.6-sol"},
                    "args": ["--harness", "codex", "--prompt", "Second pass."],
                },
            }
        )
    )
    return path


def _container_env_names(captured):
    """The names the container actually receives, from a captured env dict.

    ``captured`` is what main() hands to ``build_container_env``, which drops
    None values — so a key seeded None in cli.py's env dict (the guard that
    stops the .env merge and the preset-env seeding from filling a host input
    name in) is present here but never inside the container. Composing the
    real transport is what tells the two apart.
    """
    from lmer_cli.runtime import _ENV_FILE_NAME, build_container_env

    container_env = build_container_env(dict(captured))
    try:
        names = set(container_env.client_env)
        if container_env.env_file_dir is not None:
            text = (container_env.env_file_dir / _ENV_FILE_NAME).read_text(
                encoding="utf-8"
            )
            names |= {line.partition("=")[0] for line in text.split("\n") if line}
        return names
    finally:
        container_env.cleanup()


def _run_main(argv, env_in=None, captured_env=None, home=None):
    env = {**_BASE_ENV, **(env_in or {})}
    if home is not None:
        env["HOME"] = str(home)
    with patch.dict(os.environ, env, clear=True):
        with _make_main_mocks(captured_env=captured_env):
            from lmer_cli.cli import main

            return main(argv)


class TestAgentsFlagCli:
    def test_flag_forwards_names_and_config(self, presets_file, tmp_path):
        captured: dict = {}
        rc = _run_main(
            ["--agents", "opus-review,sol-review", *_EXEC_ARGS],
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            captured_env=captured,
            home=tmp_path,
        )
        assert rc == 0
        assert captured.get("LMER_SPAWN_AGENTS") == "opus-review,sol-review"
        config = json.loads(captured["LMER_SPAWN_AGENTS_CONFIG"])
        assert config == {
            "opus-review": {"env": {"LMER_LLM_NAME": "opus"}},
            "sol-review": {
                "env": {"LMER_HARNESS": "codex", "LMER_LLM_NAME": "gpt-5.6-sol"}
            },
        }

    def test_env_var_selects_agents(self, presets_file, tmp_path):
        captured: dict = {}
        rc = _run_main(
            _EXEC_ARGS,
            env_in={
                "LMER_PRESETS_FILE": str(presets_file),
                "LMER_AGENTS": "opus-review",
            },
            captured_env=captured,
            home=tmp_path,
        )
        assert rc == 0
        assert captured.get("LMER_SPAWN_AGENTS") == "opus-review"

    def test_flag_wins_over_env_var(self, presets_file, tmp_path):
        captured: dict = {}
        rc = _run_main(
            ["--agents", "sol-review", *_EXEC_ARGS],
            env_in={
                "LMER_PRESETS_FILE": str(presets_file),
                "LMER_AGENTS": "opus-review",
            },
            captured_env=captured,
            home=tmp_path,
        )
        assert rc == 0
        assert captured.get("LMER_SPAWN_AGENTS") == "sol-review"

    def test_dotenv_sourced_selection_honored(self, presets_file, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text(
            f"LMER_AGENTS=opus-review\nLMER_PRESETS_FILE={presets_file}\n"
        )
        captured: dict = {}
        rc = _run_main(_EXEC_ARGS, captured_env=captured, home=tmp_path)
        assert rc == 0
        assert captured.get("LMER_SPAWN_AGENTS") == "opus-review"

    def test_dotenv_sourced_selection_does_not_reach_the_container(
        self, presets_file, tmp_path, monkeypatch
    ):
        """A .env-file LMER_AGENTS stays host-side (issue #283).

        The .env merge forwards any key the container env dict does not
        already carry, so the selection would otherwise travel into the
        container under its host input name — the ambient value the scoped
        pair replaced.
        """
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text(
            f"LMER_AGENTS=opus-review\n"
            f"LMER_AGENTS_CONFIG=/host/only.json\n"
            f"LMER_PRESETS_FILE={presets_file}\n"
        )
        captured: dict = {}
        rc = _run_main(_EXEC_ARGS, captured_env=captured, home=tmp_path)
        assert rc == 0
        assert captured.get("LMER_SPAWN_AGENTS") == "opus-review"
        assert json.loads(captured["LMER_SPAWN_AGENTS_CONFIG"]) == {
            "opus-review": {"env": {"LMER_LLM_NAME": "opus"}}
        }
        names = _container_env_names(captured)
        assert "LMER_SPAWN_AGENTS" in names and "LMER_SPAWN_AGENTS_CONFIG" in names
        assert "LMER_AGENTS" not in names
        assert "LMER_AGENTS_CONFIG" not in names

    def test_preset_env_selection_does_not_reach_the_container(self, tmp_path):
        """A preset's env carrying LMER_AGENTS stays host-side (issue #283).

        The preset-env seeding loop copies every applied key the container
        env dict does not already carry, the second route by which the host
        input name would go ambient inside the container.
        """
        presets_file = tmp_path / "presets.json"
        presets_file.write_text(
            json.dumps(
                {
                    "fanout": {
                        "env": {
                            "LMER_AGENTS": "opus-review",
                            "LMER_AGENTS_CONFIG": "/host/only.json",
                        }
                    },
                    "opus-review": {"env": {"LMER_LLM_NAME": "opus"}},
                }
            )
        )
        captured: dict = {}
        rc = _run_main(
            ["--preset", "fanout", *_EXEC_ARGS],
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            captured_env=captured,
            home=tmp_path,
        )
        assert rc == 0
        assert captured.get("LMER_SPAWN_AGENTS") == "opus-review"
        assert json.loads(captured["LMER_SPAWN_AGENTS_CONFIG"]) == {
            "opus-review": {"env": {"LMER_LLM_NAME": "opus"}}
        }
        names = _container_env_names(captured)
        assert "LMER_SPAWN_AGENTS" in names and "LMER_SPAWN_AGENTS_CONFIG" in names
        assert "LMER_AGENTS" not in names
        assert "LMER_AGENTS_CONFIG" not in names

    def test_unknown_hintless_name_exits_2(self, presets_file, tmp_path):
        rc = _run_main(
            ["--agents", "nope", *_EXEC_ARGS],
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            home=tmp_path,
        )
        assert rc == 2

    def test_no_presets_file_hintless_name_exits_2(self, tmp_path):
        rc = _run_main(
            ["--agents", "nonexistent", *_EXEC_ARGS],
            home=tmp_path,
        )
        assert rc == 2

    def test_model_route_needs_no_presets_file(self, tmp_path):
        # `--agents=fable` with no presets file at all: the model route
        # covers it, so trivial model-only reviewers need zero preset setup.
        captured: dict = {}
        rc = _run_main(
            ["--agents", "fable", *_EXEC_ARGS],
            captured_env=captured,
            home=tmp_path,
        )
        assert rc == 0
        config = json.loads(captured["LMER_SPAWN_AGENTS_CONFIG"])
        assert config == {"fable": {"env": {"LMER_LLM_NAME": "fable"}}}

    def test_unknown_harness_in_preset_env_exits_2(self, presets_file, tmp_path):
        rc = _run_main(
            ["--agents", "typo", *_EXEC_ARGS],
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            home=tmp_path,
        )
        assert rc == 2

    def test_empty_selection_exits_2(self, presets_file, tmp_path):
        rc = _run_main(
            ["--agents", ",,", *_EXEC_ARGS],
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            home=tmp_path,
        )
        assert rc == 2

    def test_launch_shaping_preset_forwards_env_only(self, presets_file, tmp_path):
        captured: dict = {}
        rc = _run_main(
            ["--agents", "svc", *_EXEC_ARGS],
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            captured_env=captured,
            home=tmp_path,
        )
        assert rc == 0
        config = json.loads(captured["LMER_SPAWN_AGENTS_CONFIG"])
        assert config == {"svc": {"env": {}}}
        # The agent preset must NOT shape the launch: no service mode.
        assert captured.get("LMER_SERVICE_MODE") is None

    def test_dual_use_preset_forwards_folded_config(self, presets_file, tmp_path):
        captured: dict = {}
        rc = _run_main(
            ["--agents", "sol-second", *_EXEC_ARGS],
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            captured_env=captured,
            home=tmp_path,
        )
        assert rc == 0
        config = json.loads(captured["LMER_SPAWN_AGENTS_CONFIG"])
        assert config == {
            "sol-second": {
                "env": {"LMER_LLM_NAME": "gpt-5.6-sol", "LMER_HARNESS": "codex"},
                "prompt": "Second pass.",
            }
        }

    def test_host_input_names_do_not_reach_the_container(self, presets_file, tmp_path):
        """The container sees the scoped pair only (issue #283).

        ``LMER_AGENTS`` is a host input; ambient inside the container it made
        every nested ``lmer`` and every test run inherit the outer session's
        fan-out selection — resolved against a presets file that never crosses
        the boundary. Selected here through the env var, the spelling that
        would have travelled by inheritance if the launch forwarded nothing.
        """
        captured: dict = {}
        rc = _run_main(
            _EXEC_ARGS,
            env_in={
                "LMER_PRESETS_FILE": str(presets_file),
                "LMER_AGENTS": "opus-review",
            },
            captured_env=captured,
            home=tmp_path,
        )
        assert rc == 0
        assert captured.get("LMER_SPAWN_AGENTS") == "opus-review"
        assert json.loads(captured["LMER_SPAWN_AGENTS_CONFIG"]) == {
            "opus-review": {"env": {"LMER_LLM_NAME": "opus"}}
        }
        names = _container_env_names(captured)
        assert "LMER_SPAWN_AGENTS" in names and "LMER_SPAWN_AGENTS_CONFIG" in names
        assert "LMER_AGENTS" not in names
        assert "LMER_AGENTS_CONFIG" not in names

    def test_no_selection_forwards_nothing(self, presets_file, tmp_path):
        captured: dict = {}
        rc = _run_main(
            _EXEC_ARGS,
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            captured_env=captured,
            home=tmp_path,
        )
        assert rc == 0
        assert captured.get("LMER_SPAWN_AGENTS") is None
        assert captured.get("LMER_SPAWN_AGENTS_CONFIG") is None


class TestAgentsCrossHarnessCreds:
    """Cross-harness credential propagation (issue #131): a fan-out child
    routed to a non-session harness gets that harness's credential files
    mounted, and a child harness with no host credential file warns at
    launch (never errors — keys-via-env harnesses can authenticate without
    the mount)."""

    def _run_main_capturing_mounts(self, argv, env_in, home):
        env = {**_BASE_ENV, **(env_in or {}), "HOME": str(home)}
        mock_mounts = MagicMock(return_value=([], False))
        with patch.dict(os.environ, env, clear=True):
            with _make_main_mocks():
                with patch("lmer_cli.cli.build_user_mounts", mock_mounts):
                    from lmer_cli.cli import main

                    rc = main(argv)
        return rc, mock_mounts

    def test_codex_routed_agent_unions_codex_harness_into_mounts(
        self, presets_file, tmp_path
    ):
        rc, mock_mounts = self._run_main_capturing_mounts(
            ["--agents", "sol-review", *_EXEC_ARGS],
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            home=tmp_path,
        )
        assert rc == 0
        (runtime, harness, extras) = mock_mounts.call_args.args
        assert harness.name == "claude"
        assert [h.name for h in extras] == ["codex"]

    def test_session_harness_agents_add_no_extras(self, presets_file, tmp_path):
        rc, mock_mounts = self._run_main_capturing_mounts(
            ["--agents", "opus-review", *_EXEC_ARGS],
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            home=tmp_path,
        )
        assert rc == 0
        (_, harness, extras) = mock_mounts.call_args.args
        assert harness.name == "claude"
        assert extras == []

    def test_no_agents_passes_no_extras(self, presets_file, tmp_path):
        rc, mock_mounts = self._run_main_capturing_mounts(
            _EXEC_ARGS,
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            home=tmp_path,
        )
        assert rc == 0
        (_, _, extras) = mock_mounts.call_args.args
        assert extras == []

    def test_missing_child_credentials_warn_at_launch(
        self, presets_file, tmp_path, capsys
    ):
        rc = _run_main(
            ["--agents", "sol-review", *_EXEC_ARGS],
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            home=tmp_path,
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "agent 'sol-review' routes to codex" in out
        assert "~/.codex/auth.json" in out
        assert "may fail to authenticate" in out

    def test_present_child_credentials_do_not_warn(
        self, presets_file, tmp_path, capsys
    ):
        auth = tmp_path / ".codex" / "auth.json"
        auth.parent.mkdir(parents=True)
        auth.write_text("{}")
        rc = _run_main(
            ["--agents", "sol-review", *_EXEC_ARGS],
            env_in={"LMER_PRESETS_FILE": str(presets_file)},
            home=tmp_path,
        )
        assert rc == 0
        assert "may fail to authenticate" not in capsys.readouterr().out


class TestAgentsChildHarnesses:
    """Unit tests for cli._agents_child_harnesses — the host-side mirror of
    spawn-harness's selection, feeding the credential-mount union."""

    def _call(self, resolved, tmp_path, monkeypatch, session="claude", session_model=""):
        # lmer_cli.cli imports stay deferred in this file (the main() tests
        # import it only under a patched environment); this one is env-free
        # but keeps the file's single convention.
        from lmer_cli.cli import _agents_child_harnesses

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        if session_model:
            monkeypatch.setenv("LMER_LLM_NAME", session_model)
        else:
            monkeypatch.delenv("LMER_LLM_NAME", raising=False)
        return _agents_child_harnesses(resolved, get_harness(session))

    def test_session_harness_child_never_warns(self, tmp_path, monkeypatch, capsys):
        # Mirrors warn_missing_credentials's spawn-time exemption: a child on
        # the session's own harness authenticates exactly as well as the
        # session — no mounts to add, and no credential warning even when the
        # host has no claude credential files (env-key setups stay silent,
        # exactly like a plain launch).
        extras = self._call(
            {"opus-review": {"env": {"LMER_LLM_NAME": "opus"}}},
            tmp_path,
            monkeypatch,
        )
        assert extras == []
        assert "may fail to authenticate" not in capsys.readouterr().out

    def test_model_route_agent_implies_extra_harness(self, tmp_path, monkeypatch):
        extras = self._call(
            {"gpt-5.6-sol": {"env": {"LMER_LLM_NAME": "gpt-5.6-sol"}}},
            tmp_path,
            monkeypatch,
        )
        assert [h.name for h in extras] == ["codex"]

    def test_empty_overlay_child_stays_on_session_harness(
        self, tmp_path, monkeypatch, capsys
    ):
        # The model hint only fires on the agent's own overlay: a session
        # launched --harness pi with LMER_LLM_NAME=sonnet (Anthropic model
        # via API keys) must not route an env-less agent to claude — the
        # inherited model never outranks the inherited explicit harness.
        # The host-side mirror must agree with select_harness here.
        extras = self._call(
            {"plain": {"env": {}}},
            tmp_path,
            monkeypatch,
            session="pi",
            session_model="sonnet",
        )
        assert extras == []
        assert "may fail to authenticate" not in capsys.readouterr().out

    def test_duplicate_harnesses_dedupe(self, tmp_path, monkeypatch):
        extras = self._call(
            {
                "a": {"env": {"LMER_HARNESS": "codex"}},
                "b": {"env": {"LMER_LLM_NAME": "gpt-5.6-luna"}},
            },
            tmp_path,
            monkeypatch,
        )
        assert [h.name for h in extras] == ["codex"]

    def test_warning_lists_all_paths_only_when_none_exist(
        self, tmp_path, monkeypatch, capsys
    ):
        # pi has two credential mounts; one present ⇒ no warning (partial
        # setups are normal — models.json is optional config).
        (tmp_path / ".pi" / "agent").mkdir(parents=True)
        (tmp_path / ".pi" / "agent" / "auth.json").write_text("{}")
        extras = self._call(
            {"pi-agent": {"env": {"LMER_HARNESS": "pi"}}}, tmp_path, monkeypatch
        )
        assert [h.name for h in extras] == ["pi"]
        assert "may fail to authenticate" not in capsys.readouterr().out

    def test_warning_names_agent_harness_and_path(self, tmp_path, monkeypatch, capsys):
        extras = self._call(
            {"sol": {"env": {"LMER_HARNESS": "codex"}}}, tmp_path, monkeypatch
        )
        assert [h.name for h in extras] == ["codex"]
        out = capsys.readouterr().out
        assert "agent 'sol' routes to codex" in out
        assert "~/.codex/auth.json" in out


class TestEnvDictGuard:
    """Guard: the fan-out vars must stay in cli.py's container env dict —
    without them the resolved selection never reaches spawn-harness. Under
    the scoped names: the host-input spelling is ambient in the container
    and was inherited by everything running there (issue #283).

    The host input names must stay declared too, seeded None: their presence
    in the dict is what blocks the preset-env seeding and .env merge from
    filling them in (build_container_env drops the None)."""

    @pytest.mark.parametrize("const", ["SPAWN_AGENTS_ENV", "SPAWN_AGENTS_CONFIG_ENV"])
    def test_cli_env_dict_declares_fanout_var(self, const):
        source = CLI_PY.read_text()
        # Keyed by the presets constants, not literals: presets.py's every-
        # consumer-imports rule is what keeps a rename from splitting the pair.
        pattern = re.compile(rf"\b{const}\s*:")
        assert pattern.search(source), (
            f"{const} entry missing from cli.py container env dict"
        )

    @pytest.mark.parametrize("const", ["AGENTS_ENV", "AGENTS_CONFIG_ENV"])
    def test_cli_env_dict_seeds_host_input_name_none(self, const):
        source = CLI_PY.read_text()
        pattern = re.compile(rf"\b{const}\s*:\s*None")
        assert pattern.search(source), (
            f"{const} None seed missing from cli.py container env dict — "
            "the .env merge and preset-env seeding can leak the host input "
            "name into the container without it"
        )
