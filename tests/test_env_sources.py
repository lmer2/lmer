"""Source records cannot demote exports replaced after default loading."""

import os
from unittest.mock import patch

import pytest

from lmer_cli.env_sources import (
    DEFAULT_SOURCES_ENV, consume_env_defaults, drop_env_file_defaults,
    env_with_overrides, remember_env_defaults,
)


@pytest.mark.parametrize("key,default,exported", [
    ("LMER_LLM_NAME", "opus", "sonnet"),
    ("LMER_REASONING_EFFORT", "xhigh", "low"),
])
def test_later_export_invalidates_recorded_default(key, default, exported):
    with patch.dict(os.environ, {key: default}, clear=True):
        remember_env_defaults({key: ".env (host)"})
        os.environ[key] = exported
        child_env = env_with_overrides({})
    with patch.dict(os.environ, child_env, clear=True):
        assert consume_env_defaults() == {}
        assert os.environ[key] == exported
        assert DEFAULT_SOURCES_ENV not in os.environ


@pytest.mark.parametrize("payload", ["broken", "[]", '{"LMER_LLM_NAME": null}',
                                     '{"LMER_LLM_NAME": ["file", "stale"]}'])
def test_invalid_provenance_does_not_demote_exports(payload):
    with patch.dict(os.environ, {DEFAULT_SOURCES_ENV: payload, "LMER_LLM_NAME": "sonnet"}, clear=True):
        assert consume_env_defaults() == {}
        assert os.environ["LMER_LLM_NAME"] == "sonnet"
        assert DEFAULT_SOURCES_ENV not in os.environ


@pytest.mark.parametrize("source", ["file", "preset"])
def test_provenance_carrier_cannot_be_forwarded_from_launch_config(tmp_path, monkeypatch, source):
    from tests.test_preset_cli import _EXEC_ARGS, _run_main

    monkeypatch.chdir(tmp_path)
    args = list(_EXEC_ARGS)
    env = {}
    if source == "file":
        (tmp_path / ".env").write_text(f"{DEFAULT_SOURCES_ENV}={{}}\n")
    else:
        presets = tmp_path / "presets.json"
        presets.write_text('{"demo": {"env": {"' + DEFAULT_SOURCES_ENV + '": "{}"}}}')
        env["LMER_PRESETS_FILE"] = str(presets)
        args = ["--preset", "demo", *args]
    captured = {}
    assert _run_main(args, env_in=env, captured_env=captured, home=tmp_path) == 0
    assert captured[DEFAULT_SOURCES_ENV] is None


def test_drop_env_file_defaults_forgets_only_recorded_defaults():
    env = {"LMER_RELEASE_GITHUB_TOKEN": "from-file", "OTHER": "exported"}
    sources = {"LMER_RELEASE_GITHUB_TOKEN": ".env (host)"}

    drop_env_file_defaults(sources, ["LMER_RELEASE_GITHUB_TOKEN", "OTHER"], env=env)

    assert "LMER_RELEASE_GITHUB_TOKEN" not in env, (
        "a recorded file default must be dropped so a fresh .env load can decide"
    )
    assert sources == {}, "and its source label must go with it"
    assert env["OTHER"] == "exported", (
        "a genuine export was never recorded and keeps its precedence"
    )


def test_drop_env_file_defaults_defaults_to_os_environ():
    with patch.dict(os.environ, {"LMER_RELEASE_GITHUB_TOKEN": "from-file"}, clear=True):
        drop_env_file_defaults(
            {"LMER_RELEASE_GITHUB_TOKEN": ".env (host)"},
            ["LMER_RELEASE_GITHUB_TOKEN"],
        )
        assert "LMER_RELEASE_GITHUB_TOKEN" not in os.environ
