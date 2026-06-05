"""Tests for the --prompt CLI flag (issue #50).

`--prompt` lets an automated `lmer` run hand claude an immediate follow-up
instruction that the in-container supervisor injects right after the auto-/start.
The host CLI forwards it to the container as the LMER_START_PROMPT env var.
"""
from __future__ import annotations

from lmer_cli.cli import parse_args


class TestPromptArg:
    def test_prompt_defaults_to_none(self):
        ns, _rest = parse_args(["chat", "https://example.com/x/y/-/issues/1"])
        assert ns.prompt is None

    def test_prompt_value_parsed(self):
        ns, _rest = parse_args(
            ["chat", "https://example.com/x/y/-/issues/1", "--prompt", "research X first"]
        )
        assert ns.prompt == "research X first"

    def test_prompt_equals_form(self):
        ns, _rest = parse_args(
            ["chat", "https://example.com/x/y/-/issues/1", "--prompt=research X first"]
        )
        assert ns.prompt == "research X first"

    def test_prompt_does_not_consume_targets(self):
        # --prompt must not swallow the positional target.
        ns, _rest = parse_args(
            ["chat", "https://example.com/x/y/-/issues/1", "--prompt", "do the thing"]
        )
        assert ns.task == "chat"
        assert ns.target == ["https://example.com/x/y/-/issues/1"]
        assert ns.prompt == "do the thing"
