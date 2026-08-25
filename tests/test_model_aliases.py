"""Tests for operator-defined model aliases (issue #309).

The load-bearing property is not the substitution but its *timing*: expansion
before harness resolution is what makes `--model sol` select codex. Coverage is
per reader, because each gets the name from a different place — the session's own
model, an `--agents` child's preset, a `LMER_DISPATCH_<LANE>` value, and a
`spawn-harness --env` child.
"""

import json
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from lmer_cli.harness import LLM_NAME_ENV, resolve_harness_selection
from lmer_cli.model_aliases import (
    MODEL_ALIASES_ENV,
    expand_dispatch_value,
    expand_model_alias,
    load_model_aliases,
    parse_model_aliases,
)

REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture(autouse=True)
def _forget_parsed_tables():
    """The parse cache holds warnings too, so asserting one is order-dependent."""
    from lmer_cli import model_aliases

    model_aliases._CACHE.clear()
    yield
    model_aliases._CACHE.clear()


class TestParse:
    def test_pairs_and_surrounding_whitespace(self):
        aliases, warnings = parse_model_aliases(
            " sol = gpt-5.6-sol , fast=claude-haiku-4-5 "
        )
        assert aliases == {"sol": "gpt-5.6-sol", "fast": "claude-haiku-4-5"}
        assert warnings == []

    @pytest.mark.parametrize("raw", [None, "", "   ", ",,", " , "])
    def test_nothing_is_an_empty_table(self, raw):
        assert parse_model_aliases(raw) == ({}, [])

    def test_a_model_id_may_contain_colons_and_slashes(self):
        """Only the first `=` splits and only commas separate."""
        aliases, warnings = parse_model_aliases(
            "bedrock=anthropic.claude-v1:0,vendor=openai/gpt-5.6-sol"
        )
        assert aliases == {
            "bedrock": "anthropic.claude-v1:0",
            "vendor": "openai/gpt-5.6-sol",
        }
        assert warnings == []

    @pytest.mark.parametrize(
        "raw, bad",
        [
            ("sol", "sol"),
            ("sol=", "sol="),
            ("=gpt-5.6-sol", "usable alias name"),
            ("my alias=gpt-5.6-sol", "my alias"),
            ("-lead=gpt-5.6-sol", "-lead"),
        ],
    )
    def test_a_malformed_entry_warns_and_is_skipped(self, raw, bad):
        aliases, warnings = parse_model_aliases(f"good=real-model,{raw}")
        # The rest of the table survives — one typo must not disable the others.
        assert aliases == {"good": "real-model"}
        assert len(warnings) == 1
        assert bad in warnings[0]

    def test_the_last_duplicate_wins(self):
        aliases, warnings = parse_model_aliases("sol=first,sol=second")
        assert aliases == {"sol": "second"}
        assert warnings == []


class TestLoad:
    def test_reads_the_environment(self):
        aliases, warnings = load_model_aliases({MODEL_ALIASES_ENV: "sol=gpt-5.6-sol"})
        assert aliases == {"sol": "gpt-5.6-sol"}
        assert warnings == []

    def test_warnings_are_reported_once_per_value(self):
        """Several readers per launch; one complaint per typo."""
        environ = {MODEL_ALIASES_ENV: "this-value-is-only-used-here"}
        first = load_model_aliases(environ)[1]
        second = load_model_aliases(environ)[1]
        assert len(first) == 1
        assert second == []


class TestExpand:
    def test_a_known_alias_expands(self):
        assert expand_model_alias("sol", {"sol": "gpt-5.6-sol"}) == "gpt-5.6-sol"

    def test_an_unknown_name_passes_through(self):
        """"Not an alias" and "not a model" are indistinguishable here."""
        assert expand_model_alias("opus", {"sol": "gpt-5.6-sol"}) == "opus"

    @pytest.mark.parametrize("blank", ["", None])
    def test_blank_stays_blank(self, blank):
        assert expand_model_alias(blank, {"sol": "gpt-5.6-sol"}) == blank

    def test_expansion_is_a_single_pass(self):
        """`a=b,b=c` gives b, never c — a chain would need cycle detection."""
        aliases = {"a": "b", "b": "c"}
        assert expand_model_alias("a", aliases) == "b"


class TestExpandDispatchValue:
    def test_the_whole_value_as_an_alias(self):
        assert expand_dispatch_value("sol", {"sol": "gpt-5.6-sol"}) == "gpt-5.6-sol"

    def test_the_model_half_before_an_effort(self):
        assert (
            expand_dispatch_value("sol:high", {"sol": "gpt-5.6-sol"})
            == "gpt-5.6-sol:high"
        )

    @pytest.mark.parametrize(
        "value",
        [
            # The head IS an alias here, which is what the old unconditional
            # rpartition rewrote: docs/LMER-CLI.md promises a Bedrock-style id
            # passes through intact, and `:0` is not an effort token.
            "sol:0",
            # Any other non-effort suffix, for the same reason.
            "sol:bogus",
            "sol:HIGHER",
        ],
    )
    def test_a_non_effort_suffix_leaves_an_aliased_head_alone(self, value):
        assert expand_dispatch_value(value, {"sol": "gpt-5.6-sol"}) == value

    @pytest.mark.parametrize(
        "value, expected",
        [
            # The effort half's own case is the operator's, not ours: the lane
            # parser lowercases it downstream, but nothing here rewrites it.
            ("sol:HIGH", "gpt-5.6-sol:HIGH"),
            ("sol:xhigh", "gpt-5.6-sol:xhigh"),
        ],
    )
    def test_a_valid_effort_suffix_is_carried_over_verbatim(self, value, expected):
        assert expand_dispatch_value(value, {"sol": "gpt-5.6-sol"}) == expected

    def test_the_split_agrees_with_the_authoritative_lane_parser(self):
        """Whatever the lane parser calls the model is all the expansion may replace."""
        from lmer_cli.container.dispatch_agents import parse_dispatch_value

        aliases = {"sol": "gpt-5.6-sol", "claude-v1": "anthropic.claude-instant"}
        for value in [
            "sol", "sol:high", "sol:0", "sol:bogus",
            "claude-v1:0", "claude-v1:high", "fable:high", "plain-model",
        ]:
            expanded = expand_dispatch_value(value, aliases)
            lane = parse_dispatch_value(value)
            expected_model = aliases.get(lane.model, lane.model)
            assert parse_dispatch_value(expanded).model == expected_model, value

    def test_a_colon_bearing_model_id_is_not_touched(self):
        """The lane format's rule lives in dispatch_agents; nothing here may
        contradict it."""
        assert (
            expand_dispatch_value("anthropic.claude-v1:0", {"sol": "gpt-5.6-sol"})
            == "anthropic.claude-v1:0"
        )

    @pytest.mark.parametrize("value", [None, "", "   ", "fable:high"])
    def test_nothing_to_expand_is_returned_unchanged(self, value):
        assert expand_dispatch_value(value, {"sol": "gpt-5.6-sol"}) == value


class TestSessionModel:
    """The session's own model — flag, export, or a preset's env."""

    @pytest.fixture(autouse=True)
    def _clean_model_env(self, monkeypatch):
        # setenv-then-delenv so monkeypatch records a restore for the value
        # _apply_model_selection writes into os.environ by design.
        monkeypatch.setenv(LLM_NAME_ENV, "")
        monkeypatch.delenv(LLM_NAME_ENV)
        monkeypatch.setenv(MODEL_ALIASES_ENV, "sol=gpt-5.6-sol")

    def test_the_flag_is_expanded(self):
        from lmer_cli.cli import _apply_model_selection

        assert _apply_model_selection("sol", {}) == "gpt-5.6-sol"
        assert os.environ[LLM_NAME_ENV] == "gpt-5.6-sol"

    def test_an_inherited_value_is_expanded_too(self, monkeypatch):
        """A preset's env and a .env file both arrive as an exported value, so
        the alias has to work without the flag."""
        from lmer_cli.cli import _apply_model_selection

        monkeypatch.setenv(LLM_NAME_ENV, "sol")
        assert _apply_model_selection(None, {}) == "gpt-5.6-sol"
        assert os.environ[LLM_NAME_ENV] == "gpt-5.6-sol"

    def test_an_unaliased_model_is_untouched(self):
        from lmer_cli.cli import _apply_model_selection

        assert _apply_model_selection("opus", {}) == "opus"
        assert os.environ[LLM_NAME_ENV] == "opus"

    def test_the_show_env_table_names_the_alias(self):
        """That table exists to say where a value came from."""
        from lmer_cli.cli import _apply_model_selection

        sources: dict = {}
        _apply_model_selection("sol", sources)
        assert "alias" in sources[LLM_NAME_ENV]
        assert "sol" in sources[LLM_NAME_ENV]

    def test_the_alias_selects_the_harness_its_real_id_implies(self, monkeypatch):
        """The point of the feature: an unexpanded `sol` implies no harness, so the
        session would fall back to claude."""
        from lmer_cli.cli import _apply_model_selection

        monkeypatch.delenv("LMER_HARNESS", raising=False)
        _apply_model_selection("sol", {})
        assert resolve_harness_selection(None) == ("codex", "model")

    def test_a_malformed_table_warns_and_leaves_the_rest_working(
        self, monkeypatch, capsys
    ):
        from lmer_cli.cli import _apply_model_selection

        monkeypatch.setenv(MODEL_ALIASES_ENV, "oops,sol=gpt-5.6-sol")
        assert _apply_model_selection("sol", {}) == "gpt-5.6-sol"
        assert "oops" in capsys.readouterr().out


class TestDispatchLanes:
    """Lane values, expanded in the environment rather than in the env dict.

    Expanding while building the dict replaced cli.py's five literal passthrough
    entries and silently defeated ``tests/test_dispatch_env.py``.
    """

    def test_expansion_lands_in_the_environment(self, monkeypatch):
        from lmer_cli.cli import _apply_dispatch_aliases

        monkeypatch.setenv(MODEL_ALIASES_ENV, "sol=gpt-5.6-sol")
        monkeypatch.setenv("LMER_DISPATCH_REVIEW", "sol:high")
        monkeypatch.setenv("LMER_DISPATCH_CODE", "sonnet")
        _apply_dispatch_aliases({})
        assert os.environ["LMER_DISPATCH_REVIEW"] == "gpt-5.6-sol:high"
        assert os.environ["LMER_DISPATCH_CODE"] == "sonnet"

    def test_every_lane_is_covered(self):
        """Derived from the renderer's map, so a sixth lane is covered on arrival."""
        from lmer_cli.cli import DISPATCH_LANE_VARS
        from lmer_cli.container.dispatch_agents import ENV_PREFIX, LANE_AGENTS

        assert set(DISPATCH_LANE_VARS) == {ENV_PREFIX + lane for lane in LANE_AGENTS}

    def test_the_show_env_table_names_the_alias(self, monkeypatch):
        from lmer_cli.cli import _apply_dispatch_aliases

        monkeypatch.setenv(MODEL_ALIASES_ENV, "sol=gpt-5.6-sol")
        monkeypatch.setenv("LMER_DISPATCH_REVIEW", "sol")
        sources: dict = {}
        _apply_dispatch_aliases(sources)
        assert "alias" in sources["LMER_DISPATCH_REVIEW"]


class TestContainerLaunch:
    """What the container is handed: expanded lane values, and the table itself."""

    def _run(self, argv, env_in, captured_env):
        from tests.test_lmer_cli_slack_target import _BASE_ENV, _make_main_mocks

        env = {**_BASE_ENV, **env_in}
        with patch.dict(os.environ, env, clear=True):
            with _make_main_mocks(captured_env=captured_env):
                from lmer_cli.cli import main

                return main(argv)

    def test_dispatch_lanes_are_expanded_before_forwarding(self, tmp_path):
        """The in-container lane parser must never need the alias table."""
        from tests.test_lmer_cli_slack_target import REPO_URL

        captured: dict = {}
        code = self._run(
            ["--no-task", "--exec", "true", REPO_URL],
            {
                "HOME": str(tmp_path),
                MODEL_ALIASES_ENV: "sol=gpt-5.6-sol",
                "LMER_DISPATCH_REVIEW": "sol:high",
                "LMER_DISPATCH_CODE": "sonnet",
            },
            captured,
        )
        assert code == 0
        assert captured["LMER_DISPATCH_REVIEW"] == "gpt-5.6-sol:high"
        assert captured["LMER_DISPATCH_CODE"] == "sonnet"

    def test_the_table_and_the_expanded_model_both_cross(self, tmp_path):
        from tests.test_lmer_cli_slack_target import REPO_URL

        captured: dict = {}
        code = self._run(
            ["--no-task", "--exec", "true", "--model", "sol", REPO_URL],
            {"HOME": str(tmp_path), MODEL_ALIASES_ENV: "sol=gpt-5.6-sol"},
            captured,
        )
        assert code == 0
        # The container's own model is always the resolved real id.
        assert captured["LMER_LLM_NAME"] == "gpt-5.6-sol"
        # And the table travels, so a nested lmer resolves the same names.
        assert captured[MODEL_ALIASES_ENV] == "sol=gpt-5.6-sol"

    def test_cli_env_dict_declares_the_table(self):
        """Guard the passthrough (env-var convention step 4)."""
        source = (REPO_ROOT / "src" / "lmer_cli" / "cli.py").read_text()
        pattern = re.compile(
            r"MODEL_ALIASES_ENV\s*:\s*os\.environ\.get\(\s*MODEL_ALIASES_ENV\s*\)"
        )
        assert pattern.search(source), (
            f"{MODEL_ALIASES_ENV} entry missing from cli.py's container env dict"
        )


class TestAgentsFanOut:
    """An --agents child's preset model (issue #130's config, expanded once)."""

    def test_the_resolved_config_carries_the_real_id(self, tmp_path, monkeypatch):
        from lmer_cli.cli import _resolve_agents_cli
        from lmer_cli.presets import PRESETS_FILE_ENV

        presets = tmp_path / "presets.json"
        presets.write_text(
            json.dumps({"sol-review": {"env": {LLM_NAME_ENV: "sol"}}})
        )
        monkeypatch.setenv(PRESETS_FILE_ENV, str(presets))
        monkeypatch.setenv(MODEL_ALIASES_ENV, "sol=gpt-5.6-sol")

        import argparse

        resolved = _resolve_agents_cli(argparse.Namespace(agents="sol-review"))
        assert resolved["sol-review"]["env"][LLM_NAME_ENV] == "gpt-5.6-sol"


class TestSpawnHarnessChild:
    """A child model named at spawn time, inside the container.

    The container expands only what the host never saw — a `--env` name. Anything
    else was resolved host-side, and a second pass on a chained table gives the
    child a different model from its session.
    """

    def test_env_flag_alias_expands_and_picks_the_harness(self, tmp_path):
        """The stub is `codex`: if the alias had not expanded before harness
        selection, the child would have run claude and never touched it."""
        from tests.test_spawn_harness import _run_main

        code, argv_file, env_file = _run_main(
            tmp_path,
            [
                "harness-only",
                "--prompt",
                "p",
                "--env",
                f"{LLM_NAME_ENV}=sol",
            ],
            config={"harness-only": {"env": {}}},
            extra_env={MODEL_ALIASES_ENV: "sol=gpt-5.6-sol"},
            stub="codex",
        )
        assert code == 0, code
        argv = argv_file.read_text().splitlines()
        assert "gpt-5.6-sol" in argv, argv
        assert "sol" not in argv
        # The child process sees the resolved id, not the alias.
        assert f"{LLM_NAME_ENV}=gpt-5.6-sol" in env_file.read_text().splitlines()

    def test_a_host_resolved_model_is_not_expanded_again(self, tmp_path):
        """`big=opus,opus=claude-haiku-4-5`, session already resolved to `opus`:
        the child must run `opus` too."""
        from tests.test_spawn_harness import _run_main

        code, argv_file, env_file = _run_main(
            tmp_path,
            ["harness-only", "--prompt", "p"],
            config={"harness-only": {"env": {}}},
            extra_env={
                MODEL_ALIASES_ENV: "big=opus,opus=claude-haiku-4-5",
                LLM_NAME_ENV: "opus",
            },
            stub="claude",
        )
        assert code == 0, code
        argv = argv_file.read_text().splitlines()
        assert "opus" in argv, argv
        assert "claude-haiku-4-5" not in argv
        assert f"{LLM_NAME_ENV}=opus" in env_file.read_text().splitlines()

    def test_a_preset_model_reaches_the_child_as_the_host_resolved_it(self, tmp_path):
        """A preset's model is expanded before the config crosses the boundary, so
        an alias sitting in one is something the host never wrote."""
        from tests.test_spawn_harness import _run_main

        code, argv_file, _ = _run_main(
            tmp_path,
            ["sol-review", "--prompt", "p"],
            config={"sol-review": {"env": {LLM_NAME_ENV: "gpt-5.6-sol"}}},
            extra_env={MODEL_ALIASES_ENV: "gpt-5.6-sol=would-be-a-second-pass"},
            stub="codex",
        )
        assert code == 0, code
        argv = argv_file.read_text().splitlines()
        assert "gpt-5.6-sol" in argv, argv
        assert "would-be-a-second-pass" not in argv

    def test_listing_shows_what_the_host_resolved(self, tmp_path, capsys):
        """`--list` reports the config as it arrived; nothing re-derives it."""
        from lmer_cli.container import spawn_harness

        spawn_harness._list_agents({"sol-review": {"env": {LLM_NAME_ENV: "gpt-5.6-sol"}}})
        out = capsys.readouterr().out
        assert "model=gpt-5.6-sol" in out
        assert "harness=codex" in out
