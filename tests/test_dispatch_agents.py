"""Tests for lmer_cli.container.dispatch_agents (per-lane model dispatch).

Covers the spec §4.1 parsing contract, the fence-confined frontmatter
render, and the layout application over a linked agents dir — including
the set-then-unset staleness transition and work-overlay precedence.
"""
import os
from pathlib import Path

import pytest

from lmer_cli.container.dispatch_agents import (
    LANE_AGENTS,
    LaneConfig,
    apply_dispatch,
    main,
    parse_dispatch_value,
    render_agent_md,
)


# ---------------------------------------------------------------------------
# §4.1 parsing contract
# ---------------------------------------------------------------------------

def test_parse_model_only():
    assert parse_dispatch_value("haiku") == LaneConfig(model="haiku")


def test_parse_model_and_effort():
    assert parse_dispatch_value("fable:high") == LaneConfig(
        model="fable", effort="high"
    )


def test_parse_all_effort_levels():
    for effort in ("low", "medium", "high", "xhigh", "max"):
        assert parse_dispatch_value(f"sonnet:{effort}") == LaneConfig(
            model="sonnet", effort=effort
        )


def test_parse_unset_none():
    assert parse_dispatch_value(None) is None


def test_parse_empty_is_unset():
    assert parse_dispatch_value("") is None


def test_parse_whitespace_only_is_unset():
    assert parse_dispatch_value("   ") is None


def test_parse_trims_whitespace():
    assert parse_dispatch_value("  sonnet:low  ") == LaneConfig(
        model="sonnet", effort="low"
    )


def test_parse_effort_case_folded():
    assert parse_dispatch_value("sonnet:HIGH") == LaneConfig(
        model="sonnet", effort="high"
    )


def test_parse_invalid_effort_suffix_warns_model_only():
    config = parse_dispatch_value("sonnet:hgih")
    assert config.model == "sonnet:hgih"
    assert config.effort is None
    assert "hgih" in config.warning


def test_parse_colon_bearing_model_id_survives():
    # Bedrock-style id: the :0 suffix is not an effort token, so the whole
    # value is the model (rule 3) — with a warning naming the suffix.
    config = parse_dispatch_value("anthropic.claude-sonnet-5-v1:0")
    assert config.model == "anthropic.claude-sonnet-5-v1:0"
    assert config.effort is None
    assert config.warning is not None


def test_parse_splits_on_last_colon_only():
    config = parse_dispatch_value("region:model:xhigh")
    assert config == LaneConfig(model="region:model", effort="xhigh")


def test_parse_bare_colon_effort_no_model():
    # ":high" has a valid effort but no model — rejected with a diagnostic
    # naming the actual problem (the empty model, not the suffix).
    config = parse_dispatch_value(":high")
    assert config.model == ""
    assert config.effort is None
    assert "empty model" in config.warning


def test_parse_newline_value_rejected():
    # A newline would smuggle extra frontmatter keys into the agent def.
    config = parse_dispatch_value("sonnet\ntools: Bash, Edit, Write")
    assert config.model == ""
    assert "newline" in config.warning


def test_parse_full_model_id():
    assert parse_dispatch_value("claude-opus-4-8") == LaneConfig(
        model="claude-opus-4-8"
    )


# ---------------------------------------------------------------------------
# Frontmatter render
# ---------------------------------------------------------------------------

AGENT_MD = """---
name: explorer
description: recon
tools: Read, Grep
model: sonnet
---

# explorer

Use model: judgment in the body — this line must never change.
"""


def test_render_replaces_model():
    out = render_agent_md(AGENT_MD, "haiku", None)
    front = out.split("---")[1]
    assert "model: haiku" in front
    assert "model: sonnet" not in front


def test_render_adds_model_when_absent():
    src = "---\nname: coder\n---\nbody\n"
    out = render_agent_md(src, "sonnet", None)
    assert "model: sonnet" in out.split("---")[1]


def test_render_adds_effort():
    out = render_agent_md(AGENT_MD, "fable", "xhigh")
    front = out.split("---")[1]
    assert "model: fable" in front
    assert "effort: xhigh" in front


def test_render_replaces_existing_effort():
    src = "---\nname: coder\neffort: low\n---\nbody\n"
    out = render_agent_md(src, "sonnet", "max")
    front = out.split("---")[1]
    assert "effort: max" in front
    assert "effort: low" not in front


def test_render_keeps_existing_effort_when_none_configured():
    src = "---\nname: coder\neffort: low\n---\nbody\n"
    out = render_agent_md(src, "sonnet", None)
    assert "effort: low" in out.split("---")[1]


def test_render_body_verbatim():
    out = render_agent_md(AGENT_MD, "haiku", "low")
    assert "Use model: judgment in the body — this line must never change." in out
    # The body's "model:" string is untouched: only the fence line changed.
    assert out.split("---", 2)[2] == AGENT_MD.split("---", 2)[2]


def test_render_no_fence_gains_one():
    out = render_agent_md("just a body\n", "haiku", "low")
    assert out.startswith("---\nmodel: haiku\neffort: low\n---\n")
    assert out.endswith("just a body\n")


def test_render_unclosed_fence_treated_as_body():
    src = "---\nname: broken\nno closing fence\n"
    out = render_agent_md(src, "haiku", None)
    # The malformed input is preserved verbatim after a fresh fence.
    assert out.endswith(src)
    assert "model: haiku" in out.split("---")[1]


# ---------------------------------------------------------------------------
# Layout application
# ---------------------------------------------------------------------------

@pytest.fixture
def layout(tmp_path):
    """A linked agents dir with global + work sources, symlinks in place."""
    global_src = tmp_path / "global"
    work_src = tmp_path / "work"
    agents = tmp_path / "agents"
    for d in (global_src, work_src, agents):
        d.mkdir()
    for stem in LANE_AGENTS.values():
        source = global_src / f"{stem}.md"
        source.write_text(f"---\nname: {stem}\n---\n# {stem}\n")
        (agents / f"{stem}.md").symlink_to(source)
    return global_src, work_src, agents


def test_apply_unset_lanes_keep_symlinks(layout):
    global_src, work_src, agents = layout
    messages = apply_dispatch(agents, global_src, work_src, env={})
    assert messages == []
    for stem in LANE_AGENTS.values():
        assert (agents / f"{stem}.md").is_symlink()


def test_apply_configured_lane_materializes_file(layout):
    global_src, work_src, agents = layout
    env = {"LMER_DISPATCH_REVIEW": "fable:high"}
    messages = apply_dispatch(agents, global_src, work_src, env=env)
    target = agents / "adversarial-reviewer.md"
    assert not target.is_symlink()
    text = target.read_text()
    assert "model: fable" in text
    assert "effort: high" in text
    assert any("REVIEW" in m and m.startswith("✅") for m in messages)
    # Other lanes untouched.
    assert (agents / "explorer.md").is_symlink()


def test_apply_set_then_unset_restores_symlink(layout):
    """The staleness transition: a materialized file from a previously
    configured lane must be replaced by the bare symlink once unset."""
    global_src, work_src, agents = layout
    apply_dispatch(agents, global_src, work_src, env={"LMER_DISPATCH_CODE": "sonnet"})
    target = agents / "coder.md"
    assert not target.is_symlink()
    apply_dispatch(agents, global_src, work_src, env={})
    assert target.is_symlink()
    assert os.readlink(target) == str(global_src / "coder.md")


def test_apply_work_overlay_wins_as_render_source(layout):
    global_src, work_src, agents = layout
    overlay = work_src / "explorer.md"
    overlay.write_text("---\nname: explorer\n---\n# overlay body\n")
    # The link pass linked the overlay (work wins); mirror that.
    target = agents / "explorer.md"
    target.unlink()
    target.symlink_to(overlay)
    apply_dispatch(
        agents, global_src, work_src, env={"LMER_DISPATCH_EXPLORE": "sonnet:low"}
    )
    text = target.read_text()
    assert "# overlay body" in text
    assert "model: sonnet" in text


def test_apply_unset_relinks_to_overlay_source(layout):
    """Unset-lane relink must also honor overlay precedence."""
    global_src, work_src, agents = layout
    overlay = work_src / "designer.md"
    overlay.write_text("---\nname: designer\n---\noverlay\n")
    apply_dispatch(agents, global_src, work_src, env={})
    assert os.readlink(agents / "designer.md") == str(overlay)


def test_apply_missing_agent_file_warns(layout):
    global_src, work_src, agents = layout
    (global_src / "designer.md").unlink()
    (agents / "designer.md").unlink()
    messages = apply_dispatch(
        agents, global_src, work_src, env={"LMER_DISPATCH_DESIGN": "fable"}
    )
    assert any("LMER_DISPATCH_DESIGN" in m and m.startswith("⚠️") for m in messages)
    assert not (agents / "designer.md").exists()


def test_apply_invalid_effort_warns_but_dispatches(layout):
    global_src, work_src, agents = layout
    messages = apply_dispatch(
        agents, global_src, work_src, env={"LMER_DISPATCH_MECHANICAL": "haiku:turbo"}
    )
    target = agents / "mechanical.md"
    assert not target.is_symlink()
    assert "model: haiku:turbo" in target.read_text()
    assert any(m.startswith("⚠️") for m in messages)
    assert any(m.startswith("✅") for m in messages)


def test_apply_newline_value_skips_render(layout):
    """A rejected value warns and renders nothing — no injected keys."""
    global_src, work_src, agents = layout
    env = {"LMER_DISPATCH_DESIGN": "sonnet\ntools: Bash, Edit, Write"}
    messages = apply_dispatch(agents, global_src, work_src, env=env)
    target = agents / "designer.md"
    assert target.is_symlink()  # untouched
    assert "tools: Bash, Edit, Write" not in target.read_text()
    assert any("newline" in m for m in messages)


def test_apply_rejected_value_reverts_previous_render(layout):
    """set-then-broken transition: a render from a previously-valid value
    must not stay active once the value is edited into something rejected —
    the lane reverts to the bare symlink (warned, so not silent)."""
    global_src, work_src, agents = layout
    apply_dispatch(
        agents, global_src, work_src, env={"LMER_DISPATCH_DESIGN": "fable:xhigh"}
    )
    target = agents / "designer.md"
    assert not target.is_symlink()
    messages = apply_dispatch(
        agents, global_src, work_src, env={"LMER_DISPATCH_DESIGN": ":high"}
    )
    assert target.is_symlink()
    assert os.readlink(target) == str(global_src / "designer.md")
    assert any("empty model" in m for m in messages)


def test_apply_bad_encoding_does_not_starve_other_lanes(layout):
    """Fail-soft is per-lane: one broken source must not stop the pass."""
    global_src, work_src, agents = layout
    (global_src / "adversarial-reviewer.md").write_bytes(b"\xff\xfe broken")
    env = {
        "LMER_DISPATCH_REVIEW": "fable:high",
        "LMER_DISPATCH_CODE": "sonnet",
    }
    messages = apply_dispatch(agents, global_src, work_src, env=env)
    assert any(m.startswith("⚠️") and "REVIEW" in m for m in messages)
    # CODE still rendered despite REVIEW's broken source.
    assert "model: sonnet" in (agents / "coder.md").read_text()


def test_render_block_scalar_continuation_untouched():
    """An indented line starting with 'model:' is a folded-scalar
    continuation, not a key — it must never be rewritten."""
    src = (
        "---\nname: x\ndescription: >\n  model: judgement text\n---\nbody\n"
    )
    out = render_agent_md(src, "haiku", None)
    assert "  model: judgement text" in out
    front = out.split("---")[1]
    assert "\nmodel: haiku" in front


def test_apply_empty_value_is_unset(layout):
    global_src, work_src, agents = layout
    apply_dispatch(agents, global_src, work_src, env={"LMER_DISPATCH_REVIEW": ""})
    assert (agents / "adversarial-reviewer.md").is_symlink()


def test_apply_idempotent(layout):
    global_src, work_src, agents = layout
    env = {"LMER_DISPATCH_REVIEW": "fable:high"}
    apply_dispatch(agents, global_src, work_src, env=env)
    first = (agents / "adversarial-reviewer.md").read_text()
    apply_dispatch(agents, global_src, work_src, env=env)
    assert (agents / "adversarial-reviewer.md").read_text() == first


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def test_main_missing_agents_dir_exits_zero(tmp_path):
    assert main([str(tmp_path / "nope")]) == 0


def test_main_applies_env(layout, monkeypatch, capsys):
    global_src, work_src, agents = layout
    monkeypatch.setenv("LMER_DISPATCH_EXPLORE", "haiku")
    for lane in LANE_AGENTS:
        if lane != "EXPLORE":
            monkeypatch.delenv(f"LMER_DISPATCH_{lane}", raising=False)
    rc = main(
        [str(agents), "--global-src", str(global_src), "--work-src", str(work_src)]
    )
    assert rc == 0
    assert "model: haiku" in (agents / "explorer.md").read_text()
    assert "EXPLORE" in capsys.readouterr().out
