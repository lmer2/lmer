"""Tests for lmer_cli.presets (startup preset config + selector parsing)."""

import json
from pathlib import Path

from lmer_cli.presets import (
    PRESETS_FILE_ENV,
    Preset,
    load_presets,
    parse_preset_token,
)


class TestParsePresetToken:
    def test_none_and_empty(self):
        assert parse_preset_token(None) is None
        assert parse_preset_token("") is None

    def test_no_token(self):
        assert parse_preset_token("hey can you look at this please") is None

    def test_extracts_name(self):
        assert parse_preset_token("$preset:my_service do the thing") == "my_service"

    def test_extracts_with_leading_mention(self):
        assert parse_preset_token("<@U123> $preset:prod-1 please") == "prod-1"

    def test_stops_at_punctuation(self):
        # The char class is [A-Za-z0-9_-]+, so a trailing '.' is not part of it.
        assert parse_preset_token("use $preset:my_service.") == "my_service"

    def test_first_of_multiple_wins(self):
        assert parse_preset_token("$preset:alpha then $preset:beta") == "alpha"

    def test_bare_token_without_name_is_none(self):
        # "$preset:" with no following name does not match.
        assert parse_preset_token("$preset: nothing here") is None

    def test_allows_word_chars_and_dashes(self):
        assert parse_preset_token("$preset:My_Svc-2") == "My_Svc-2"


def _write(tmp_path: Path, data) -> str:
    path = tmp_path / "presets.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


class TestLoadPresetsOff:
    def test_env_unset_returns_empty(self, monkeypatch):
        monkeypatch.delenv(PRESETS_FILE_ENV, raising=False)
        assert load_presets() == {}

    def test_blank_env_returns_empty(self, monkeypatch):
        monkeypatch.setenv(PRESETS_FILE_ENV, "   ")
        assert load_presets() == {}

    def test_explicit_empty_path_returns_empty(self):
        assert load_presets("") == {}


class TestLoadPresetsFileErrors:
    def test_missing_file_returns_empty(self, tmp_path):
        assert load_presets(str(tmp_path / "nope.json")) == {}

    def test_malformed_json_returns_empty(self, tmp_path):
        path = tmp_path / "presets.json"
        path.write_text("{not valid json", encoding="utf-8")
        assert load_presets(str(path)) == {}

    def test_top_level_not_object_returns_empty(self, tmp_path):
        path = _write(tmp_path, ["a", "b"])
        assert load_presets(path) == {}


class TestLoadPresetsValid:
    def test_reads_from_env_var_by_default(self, tmp_path, monkeypatch):
        path = _write(tmp_path, {"svc": {"checkout": "/co"}})
        monkeypatch.setenv(PRESETS_FILE_ENV, path)
        presets = load_presets()
        assert set(presets) == {"svc"}
        assert presets["svc"].checkout == "/co"

    def test_full_preset(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "my_service": {
                    "checkout": "/srv/my-service",
                    "service": "mysvc",
                    "env": {"LMER_LLM_NAME": "opus"},
                    "args": ["--ports", "2"],
                }
            },
        )
        presets = load_presets(path)
        preset = presets["my_service"]
        assert preset == Preset(
            name="my_service",
            checkout="/srv/my-service",
            service="mysvc",
            env={"LMER_LLM_NAME": "opus"},
            args=["--ports", "2"],
        )

    def test_minimal_env_only_preset(self, tmp_path):
        path = _write(tmp_path, {"fast": {"env": {"LMER_REASONING_EFFORT": "low"}}})
        preset = load_presets(path)["fast"]
        assert preset.checkout is None
        assert preset.service is None
        assert preset.env == {"LMER_REASONING_EFFORT": "low"}
        assert preset.args == []

    def test_multiple_presets(self, tmp_path):
        path = _write(
            tmp_path,
            {"a": {"checkout": "/a"}, "b": {"checkout": "/b", "service": "bsvc"}},
        )
        presets = load_presets(path)
        assert set(presets) == {"a", "b"}

    def test_unknown_keys_kept_with_warning(self, tmp_path, caplog):
        path = _write(tmp_path, {"svc": {"checkout": "/co", "bogus": 1}})
        with caplog.at_level("WARNING"):
            presets = load_presets(path)
        assert "svc" in presets
        assert any("preset_unknown_keys" in r.message for r in caplog.records)


class TestLoadPresetsInvalidEntriesSkipped:
    def test_service_without_checkout_skipped(self, tmp_path):
        path = _write(
            tmp_path,
            {"bad": {"service": "svc"}, "good": {"checkout": "/co"}},
        )
        presets = load_presets(path)
        assert set(presets) == {"good"}

    def test_non_object_entry_skipped(self, tmp_path):
        path = _write(tmp_path, {"bad": "just a string", "good": {"checkout": "/co"}})
        assert set(load_presets(path)) == {"good"}

    def test_non_string_checkout_skipped(self, tmp_path):
        path = _write(tmp_path, {"bad": {"checkout": 5}, "good": {"checkout": "/co"}})
        assert set(load_presets(path)) == {"good"}

    def test_non_string_service_skipped(self, tmp_path):
        path = _write(
            tmp_path,
            {"bad": {"checkout": "/co", "service": 1}, "good": {"checkout": "/co"}},
        )
        assert set(load_presets(path)) == {"good"}

    def test_env_not_string_map_skipped(self, tmp_path):
        path = _write(
            tmp_path,
            {"bad": {"env": {"K": 1}}, "good": {"checkout": "/co"}},
        )
        assert set(load_presets(path)) == {"good"}

    def test_env_not_object_skipped(self, tmp_path):
        path = _write(
            tmp_path,
            {"bad": {"env": ["nope"]}, "good": {"checkout": "/co"}},
        )
        assert set(load_presets(path)) == {"good"}

    def test_args_not_list_skipped(self, tmp_path):
        path = _write(
            tmp_path,
            {"bad": {"args": "--ports 2"}, "good": {"checkout": "/co"}},
        )
        assert set(load_presets(path)) == {"good"}

    def test_args_not_all_strings_skipped(self, tmp_path):
        path = _write(
            tmp_path,
            {"bad": {"args": ["--ports", 2]}, "good": {"checkout": "/co"}},
        )
        assert set(load_presets(path)) == {"good"}

    def test_name_with_dot_skipped(self, tmp_path):
        # "prod.api" loads but $preset:prod.api could only ever match "prod",
        # so an unselectable name is skipped rather than silently listed.
        path = _write(
            tmp_path,
            {"prod.api": {"checkout": "/co"}, "good": {"checkout": "/co"}},
        )
        assert set(load_presets(path)) == {"good"}

    def test_name_with_space_skipped(self, tmp_path):
        path = _write(
            tmp_path,
            {"my service": {"checkout": "/co"}, "good": {"checkout": "/co"}},
        )
        assert set(load_presets(path)) == {"good"}
