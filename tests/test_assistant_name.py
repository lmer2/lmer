"""The assistant's display name (issue #581): one value, every surface, no wire change."""
import re
from pathlib import Path

import pytest

from lmer_cli import comms

REPO = Path(__file__).resolve().parents[1]


class TestDisplayName:
    def test_default_is_ratchet(self):
        assert comms.DEFAULT_ASSISTANT_NAME == "Ratchet"
        assert comms.assistant_display_name({}) == "Ratchet"

    def test_env_overrides(self):
        assert comms.assistant_display_name({"LMER_PLATFORM_ASSISTANT_NAME": "Sprocket"}) == "Sprocket"

    def test_blank_env_falls_back_to_default(self):
        assert comms.assistant_display_name({"LMER_PLATFORM_ASSISTANT_NAME": "   "}) == "Ratchet"

    def test_the_tag_uses_the_name(self, monkeypatch):
        monkeypatch.setenv("LMER_PLATFORM_ASSISTANT_NAME", "Sprocket")
        assert comms.Source(comms.CHANNEL_UBER).label == "Sprocket"
        monkeypatch.delenv("LMER_PLATFORM_ASSISTANT_NAME")
        assert comms.Source(comms.CHANNEL_UBER).label == "Ratchet"

    def test_aliases_carry_the_legacy_label(self):
        assert comms.assistant_label_aliases({}) == ("Ratchet", "uber lmer")
        assert comms.assistant_label_aliases({"LMER_PLATFORM_ASSISTANT_NAME": "Sprocket"}) == (
            "Sprocket", "Ratchet", "uber lmer")

    def test_aliases_use_a_resolved_name_over_the_environment(self):
        # The host has config.json's name in hand even when its own env does not
        # (review of !403): the resolved name is the first alias either way.
        assert comms.assistant_label_aliases({}, name="Sprocket") == (
            "Sprocket", "Ratchet", "uber lmer")
        assert comms.assistant_label_aliases({"LMER_PLATFORM_ASSISTANT_NAME": "Cog"}, name="Sprocket") == (
            "Sprocket", "Ratchet", "uber lmer")

    def test_relay_label_uses_the_resolved_name(self):
        message = comms.Message(id="s/1", seq=1, session="s", at="2026-01-01T00:00:00Z",
                                source=comms.Source(comms.CHANNEL_UBER), kind="message",
                                text="the review is posted")
        text, tagged = comms.relay_payload(message, assistant_name="Sprocket")
        assert tagged and text == "[Sprocket] the review is posted"
        assert comms.relay_payload(message, assistant_name=None)[0] == "[Ratchet] the review is posted"


class TestWireIdentifiersUntouched:
    """Grep guards: the rename is human-facing only."""

    def test_channel_value_is_stable(self):
        from lmer_platform import comms as platform_comms
        assert comms.CHANNEL_UBER == "uber-" + "lmer"
        assert platform_comms.CHANNEL_UBER == "uber-" + "lmer"
        assert comms.CHANNEL_UBER in comms.COMMAND_CHANNELS

    @pytest.mark.parametrize("path, needle", [
        ("src/lmer_platform/daemon.py", '== "uber-' + 'lmer"'),
        ("src/lmer_platform/project_events.py", '== "uber-' + 'lmer"'),
        ("src/lmer_platform/api.py", 'return "uber-' + 'lmer"'),
        ("web/src/outbound.js", "'uber-" + "lmer'"),
    ])
    def test_comparisons_still_name_the_channel(self, path, needle):
        assert needle in (REPO / path).read_text(encoding="utf-8"), path

    def test_container_passthrough_declares_the_name(self):
        source = (REPO / "src/lmer_cli/cli.py").read_text(encoding="utf-8")
        pattern = re.compile(
            r"""["']LMER_PLATFORM_ASSISTANT_NAME["']\s*:\s*os\.environ\.get\(\s*["']LMER_PLATFORM_ASSISTANT_NAME["']\s*\)"""
        )
        assert pattern.search(source), "LMER_PLATFORM_ASSISTANT_NAME missing from cli.py's container env dict"

    def test_documented_in_the_env_var_reference(self):
        docs = (REPO / "docs/LMER-CLI.md").read_text(encoding="utf-8")
        assert "**`LMER_PLATFORM_ASSISTANT_NAME`**" in docs


class TestPlatformConfig:
    def test_default_is_ratchet(self, monkeypatch):
        from lmer_platform import config as platform_config
        monkeypatch.delenv("LMER_PLATFORM_ASSISTANT_NAME", raising=False)
        cfg = platform_config.load({"work_repo_url": "https://git.example.com/w.git"})
        assert cfg.assistant_name == "Ratchet"

    def test_env_wins_over_default(self, monkeypatch):
        from lmer_platform import config as platform_config
        monkeypatch.setenv("LMER_PLATFORM_ASSISTANT_NAME", "Sprocket")
        cfg = platform_config.load({"work_repo_url": "https://git.example.com/w.git"})
        assert cfg.assistant_name == "Sprocket"

    def test_blank_assistant_name_is_default(self, monkeypatch):
        from lmer_platform import config as platform_config
        monkeypatch.setenv("LMER_PLATFORM_ASSISTANT_NAME", "   ")
        cfg = platform_config.load({"work_repo_url": "https://git.example.com/w.git"})
        assert cfg.assistant_name == "Ratchet"

    def test_over_long_name_is_default(self):
        from lmer_platform import config as platform_config
        cfg = platform_config.load({"work_repo_url": "https://git.example.com/w.git", "assistant_name": "x" * 41})
        assert cfg.assistant_name == "Ratchet"


class TestPropagation:
    def test_spawn_exports_the_name(self):
        source = (REPO / "src/lmer_platform/spawn.py").read_text(encoding="utf-8")
        assert '"LMER_PLATFORM_ASSISTANT_NAME"' in source

    def test_assistant_env_file_carries_the_name(self):
        source = (REPO / "src/lmer_platform/assistant.py").read_text(encoding="utf-8")
        assert 'values["LMER_PLATFORM_ASSISTANT_NAME"]' in source
