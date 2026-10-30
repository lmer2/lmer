"""Tests for lmer_cli.presets (startup preset config + selector parsing)."""

import json
import logging
import os
import time
from pathlib import Path

import pytest

from lmer_cli.presets import (
    PRESETS_FILE_ENV,
    Preset,
    clear_presets_cache,
    load_presets,
    parse_preset_token,
)


@pytest.fixture(autouse=True)
def _fresh_presets_cache():
    """Each test sees a cold cache, so its load warnings and INFO line fire."""
    clear_presets_cache()
    yield
    clear_presets_cache()


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


class TestServiceGroupField:
    """``service_group`` — a preset that attaches to a whole stack (#312)."""

    def test_loads_and_maps_to_the_flag(self, tmp_path):
        path = tmp_path / "presets.json"
        path.write_text(json.dumps({
            "fullstack": {"checkout": "/srv/stack", "service_group": "stack"},
        }))
        preset = load_presets(str(path))["fullstack"]
        assert preset.service_group == "stack"
        assert preset.cli_tokens() == [
            "--checkout", "/srv/stack", "--service-group", "stack",
        ]

    def test_starting_member_composes_with_the_group(self, tmp_path):
        path = tmp_path / "presets.json"
        path.write_text(json.dumps({
            "fullstack": {
                "checkout": "/srv/stack",
                "service": "web",
                "service_group": "stack",
            },
        }))
        assert load_presets(str(path))["fullstack"].cli_tokens() == [
            "--checkout", "/srv/stack", "--service", "web",
            "--service-group", "stack",
        ]

    def test_group_without_checkout_is_skipped(self, tmp_path):
        """Mirrors the CLI rule, so the preset cannot produce a refused launch."""
        path = tmp_path / "presets.json"
        path.write_text(json.dumps({
            "broken": {"service_group": "stack"},
            "fine": {"checkout": "/srv/x"},
        }))
        presets = load_presets(str(path))
        assert "broken" not in presets
        assert "fine" in presets  # one bad entry never disables the others

    def test_non_string_group_is_skipped(self, tmp_path):
        path = tmp_path / "presets.json"
        path.write_text(json.dumps({
            "broken": {"checkout": "/srv/x", "service_group": ["stack"]},
        }))
        assert load_presets(str(path)) == {}


def _loaded_lines(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.INFO and r.getMessage().startswith("presets_loaded ")
    ]


def _age(path: str, seconds: float = 60.0) -> None:
    """Backdate the file past the racy window, as an operator's edit long ago."""
    then = time.time_ns() - int(seconds * 1e9)
    os.utime(path, ns=(then, then))


class TestLoadPresetsCache:
    """Issue #600: the daemon reads the file per request and announced it each time."""

    def test_unchanged_file_is_announced_once(self, tmp_path, caplog):
        path = _write(tmp_path, {"a": {"checkout": "/a"}, "b": {"checkout": "/b"}})
        with caplog.at_level(logging.DEBUG, logger="lmer_cli.presets"):
            for _ in range(3):
                assert set(load_presets(path)) == {"a", "b"}
        assert len(_loaded_lines(caplog)) == 1
        assert "names=a,b" in _loaded_lines(caplog)[0]

    def test_unchanged_old_file_is_not_reread(self, tmp_path, monkeypatch):
        path = _write(tmp_path, {"a": {"checkout": "/a"}})
        _age(path)
        load_presets(path)

        def _no_read(self):
            raise AssertionError("an unchanged presets file was read again")

        monkeypatch.setattr(Path, "read_bytes", _no_read)
        monkeypatch.setattr(Path, "read_text", _no_read)
        assert set(load_presets(path)) == {"a"}

    def test_rewritten_file_is_announced_again(self, tmp_path, caplog):
        path = _write(tmp_path, {"a": {"checkout": "/a"}})
        _age(path)
        with caplog.at_level(logging.INFO, logger="lmer_cli.presets"):
            load_presets(path)
            _write(tmp_path, {"other": {"checkout": "/o"}})
            assert set(load_presets(path)) == {"other"}
            load_presets(path)
        lines = _loaded_lines(caplog)
        assert len(lines) == 2
        assert "names=a" in lines[0]
        assert "names=other" in lines[1]

    def test_same_size_rewrite_within_one_clock_tick_is_seen(self, tmp_path):
        """A rewrite can keep mtime, size and inode when it lands in the same tick."""
        path = _write(tmp_path, {"two": {"checkout": "/srv/w", "service": "web-two"}})
        stamp = os.stat(path).st_mtime_ns
        assert load_presets(path)["two"].service == "web-two"
        _write(tmp_path, {"two": {"checkout": "/srv/w", "service": "web-one"}})
        os.utime(path, ns=(stamp, stamp))
        assert load_presets(path)["two"].service == "web-one"

    def test_callers_get_independent_copies(self, tmp_path):
        path = _write(tmp_path, {"svc": {"env": {"K": "v"}, "args": ["--ports", "2"]}})
        first = load_presets(path)
        first["svc"].env["K"] = "mutated"
        first["svc"].args.append("--evil")
        first["svc"].checkout = "/elsewhere"
        del first["svc"]

        second = load_presets(path)
        assert second["svc"] == Preset(
            name="svc", env={"K": "v"}, args=["--ports", "2"]
        )
        assert second["svc"] is not load_presets(path)["svc"]

    def test_vanished_file_is_announced_again_when_restored(self, tmp_path, caplog):
        path = _write(tmp_path, {"a": {"checkout": "/a"}})
        with caplog.at_level(logging.INFO, logger="lmer_cli.presets"):
            load_presets(path)
            os.unlink(path)
            assert load_presets(path) == {}
            _write(tmp_path, {"a": {"checkout": "/a"}})
            assert set(load_presets(path)) == {"a"}
        assert len(_loaded_lines(caplog)) == 2


def _errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]


class TestLoadPresetsInvalidFileCache:
    """An invalid file was re-reported at ERROR on every request (#600 review)."""

    def test_invalid_file_is_reported_once_until_fixed(self, tmp_path, caplog):
        path = tmp_path / "presets.json"
        path.write_text("{not valid json", encoding="utf-8")
        with caplog.at_level(logging.DEBUG, logger="lmer_cli.presets"):
            for _ in range(3):
                assert load_presets(str(path)) == {}
            assert len(_errors(caplog)) == 1
            assert "presets_file_unreadable" in _errors(caplog)[0]

            _write(tmp_path, {"fixed": {"checkout": "/f"}})
            assert set(load_presets(str(path))) == {"fixed"}
        assert len(_errors(caplog)) == 1
        assert any("names=fixed" in line for line in _loaded_lines(caplog))

    def test_an_old_invalid_file_is_not_reread(self, tmp_path, monkeypatch):
        path = _write(tmp_path, ["not", "an", "object"])
        _age(path)
        assert load_presets(path) == {}

        def _no_read(self):
            raise AssertionError("an unchanged invalid presets file was read again")

        monkeypatch.setattr(Path, "read_bytes", _no_read)
        monkeypatch.setattr(Path, "read_text", _no_read)
        assert load_presets(path) == {}

    def test_a_different_invalid_file_is_reported_again(self, tmp_path, caplog):
        path = tmp_path / "presets.json"
        path.write_text("{not valid json", encoding="utf-8")
        with caplog.at_level(logging.ERROR, logger="lmer_cli.presets"):
            load_presets(str(path))
            _write(tmp_path, ["still", "not", "an", "object"])
            assert load_presets(str(path)) == {}
            load_presets(str(path))
        errors = _errors(caplog)
        assert len(errors) == 2
        assert "presets_file_not_object" in errors[1]
