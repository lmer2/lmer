"""Workers are told the tag's new spelling and that older sessions may show the old one (#581)."""
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RENDERER = REPO / "libexec" / "render-prompt-fragment.py"


def _render(template, env):
    result = subprocess.run([sys.executable, str(RENDERER), str(REPO / template)],
                            env={**os.environ, **env}, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_ask_fragment_names_the_tag_and_the_legacy_form():
    env = {"LMER_ASK_DIR": "/x", "LMER_PLATFORM_ASSISTANT_NAME": ""}
    out = _render("prompts/orchestrator-ask.md.jinja2", env)
    assert "[Ratchet] …" in out
    assert "[uber lmer]" in out


def test_ask_fragment_follows_the_configured_name():
    out = _render("prompts/orchestrator-ask.md.jinja2", {"LMER_ASK_DIR": "/x", "LMER_PLATFORM_ASSISTANT_NAME": "Sprocket"})
    assert "[Sprocket] …" in out


def test_orchestrate_taskdef_names_the_tag():
    text = (REPO / "taskdef" / "orchestrate" / "instructions.txt").read_text(encoding="utf-8")
    assert "[{{ LMER_PLATFORM_ASSISTANT_NAME | default('Ratchet', true) }}]" in text
    assert "[uber lmer]" in text  # the legacy note
