"""Per-lane model+effort dispatch for Claude subagent definitions.

Applies ``LMER_DISPATCH_<LANE>=<model>[:<effort>]`` configuration to the
agent definitions laid out under ``~/.claude/agents/`` by
``claude_link_agent_files`` (libexec/claude-agent-files.sh). The link pass
symlinks every agent def verbatim; this module is the post-link render
pass for the five dispatch lanes:

* a **configured** lane replaces its agent's symlink with a real file whose
  YAML frontmatter carries the configured ``model:`` (and ``effort:`` when
  one parsed) — the rewrite is confined to the leading ``---`` fence, the
  body is copied verbatim;
* an **unset** lane is forced back to the bare symlink, so a stale
  materialized copy from a previously-configured run can never outlive its
  configuration (the set-then-unset staleness transition); a **rejected**
  value (newline, empty model) warns and reverts the same way.

Work-repo overlay precedence is preserved by rendering from the same
winning source the link pass chose (work source wins on name collision).

Invoked as ``python3 -m lmer_cli.container.dispatch_agents <agents-dir>
--global-src <dir> --work-src <dir>`` from claude-agent-files.sh after the
editable install is on ``sys.path`` (the ``lmer_cli.container.masterplan``
precedent). Fail-soft throughout: a broken lane value or missing agent file
warns and continues — provisioning must never kill the session.
"""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Lane name (the <LANE> in LMER_DISPATCH_<LANE>) -> agent definition stem.
LANE_AGENTS = {
    "REVIEW": "adversarial-reviewer",
    "DESIGN": "designer",
    "CODE": "coder",
    "MECHANICAL": "mechanical",
    "EXPLORE": "explorer",
}
ENV_PREFIX = "LMER_DISPATCH_"
# The subagent-frontmatter effort vocabulary (docs: sub-agents `effort:`).
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class LaneConfig:
    """A parsed LMER_DISPATCH_<LANE> value.

    ``warning`` carries the human-readable note for a colon suffix that was
    not a valid effort token (the value still dispatches, model-only).
    """

    model: str
    effort: Optional[str] = None
    warning: Optional[str] = None


def parse_dispatch_value(raw: Optional[str]) -> Optional[LaneConfig]:
    """Parse one lane value per the spec §4.1 contract.

    Returns None for unset: a missing, empty, or whitespace-only value.
    Splits on the LAST colon, and only when the suffix is a valid effort
    token (case-insensitive) and a non-empty model remains — otherwise the
    entire value is the model, so colon-bearing model ids (Bedrock-style
    ``…-v1:0``) pass through intact, with a warning naming the rejected
    suffix. The model is never validated beyond being non-empty: claude
    itself rejects unknown models (the LMER_LLM_NAME philosophy).
    """
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    if "\n" in value or "\r" in value:
        # A newline would be written verbatim into the frontmatter fence,
        # smuggling arbitrary extra keys (e.g. tools:) into the agent def —
        # and values layer from cwd/.env, which may not be operator-owned.
        # Reject the whole value.
        return LaneConfig(
            model="", warning="value contains a newline — ignored"
        )
    if ":" in value:
        model, _, suffix = value.rpartition(":")
        model = model.strip()
        effort = suffix.strip().lower()
        if effort in EFFORT_LEVELS:
            if model:
                return LaneConfig(model=model, effort=effort)
            # ":high" — the effort half is fine; the model is missing.
            return LaneConfig(
                model="",
                warning=f"empty model before ':{effort}' — ignored",
            )
        return LaneConfig(
            model=value,
            warning=(
                f"suffix {suffix.strip()!r} is not a valid effort "
                f"({'|'.join(EFFORT_LEVELS)}) — using the whole value as the model"
            ),
        )
    return LaneConfig(model=value)


def render_agent_md(text: str, model: str, effort: Optional[str]) -> str:
    """Set/replace ``model:`` (and ``effort:``) in the leading fence only.

    The rewrite is confined to the first ``--- … ---`` block: a body that
    happens to contain the string ``model:`` (operator-controlled work-repo
    overlay input) is never touched. A file without a leading fence gains a
    minimal one, body verbatim. An existing ``effort:`` key is left alone
    when no effort was configured (nothing to inject).
    """
    lines = text.split("\n")
    if lines and lines[0].strip() == "---":
        try:
            close = next(
                i for i in range(1, len(lines)) if lines[i].strip() == "---"
            )
        except StopIteration:
            close = None
        if close is not None:
            front = lines[1:close]
            front = _set_key(front, "model", model)
            if effort is not None:
                front = _set_key(front, "effort", effort)
            return "\n".join(lines[:1] + front + lines[close:])
    fence = ["---", f"model: {model}"]
    if effort is not None:
        fence.append(f"effort: {effort}")
    fence.append("---")
    return "\n".join(fence) + "\n" + text


def _set_key(front: list[str], key: str, value: str) -> list[str]:
    """Replace ``key:`` in frontmatter lines, or append it."""
    out = []
    replaced = False
    for line in front:
        # Top-level (unindented) keys only: an indented line is a block-
        # scalar continuation (e.g. a folded description) and must never be
        # rewritten even if its text happens to start with "model:".
        if not replaced and line.startswith(f"{key}:"):
            out.append(f"{key}: {value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}: {value}")
    return out


def _ensure_default_link(target: Path, source: Optional[Path]) -> None:
    """Force ``target`` back to the bare symlink (the unset-lane state)."""
    if source is None:
        return
    if target.is_symlink() and os.readlink(target) == str(source):
        return
    if target.is_symlink() or target.exists():
        target.unlink()
    target.symlink_to(source)


def _winning_source(
    name: str, global_src: Optional[Path], work_src: Optional[Path]
) -> Optional[Path]:
    """The source file the link pass would have linked (work wins)."""
    for src in (work_src, global_src):
        if src is not None:
            candidate = src / name
            if candidate.is_file():
                return candidate
    return None


def apply_dispatch(
    agents_dir: Path,
    global_src: Optional[Path] = None,
    work_src: Optional[Path] = None,
    env: Optional[dict] = None,
) -> list[str]:
    """Apply every lane's configuration to a linked agents dir.

    Returns the messages to surface (the caller prints them). Never raises
    for a per-lane problem — each message is prefixed ✅ / ⚠️ in the
    claude-runner.sh output style.
    """
    environ = os.environ if env is None else env
    messages: list[str] = []
    for lane, agent in LANE_AGENTS.items():
        var = ENV_PREFIX + lane
        name = f"{agent}.md"
        target = agents_dir / name
        source = _winning_source(name, global_src, work_src)
        try:
            config = parse_dispatch_value(environ.get(var))
            if config is None:
                # Unset lane: today's behavior — the bare symlink. Force the
                # link back if a previously-materialized real file (or a
                # link to the wrong source) is squatting on the name, so
                # stale dispatch config never outlives its env var.
                _ensure_default_link(target, source)
                continue
            if config.warning:
                messages.append(f"⚠️  {var}: {config.warning}")
            if not config.model:
                # Rejected value (newline, empty model) — warned above; the
                # lane reverts to the unset default so a render from a
                # previously-valid value can't stay active under a value the
                # operator has since broken. The warning keeps the revert
                # from being silent.
                _ensure_default_link(target, source)
                continue
            if source is None:
                messages.append(
                    f"⚠️  {var} set but no {name} in any agent source — skipped"
                )
                continue
            rendered = render_agent_md(
                source.read_text(), config.model, config.effort
            )
            if target.is_symlink() or target.exists():
                target.unlink()
            target.write_text(rendered)
            suffix = f" effort={config.effort}" if config.effort else ""
            messages.append(
                f"✅ Dispatch lane {lane} → {agent}: model={config.model}{suffix}"
            )
        except Exception as exc:  # noqa: BLE001 — one broken lane (bad
            # encoding, odd fs state) must never starve the remaining lanes;
            # the whole pass is fail-soft by contract.
            messages.append(f"⚠️  {var}: could not apply ({exc})")
    return messages


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entrypoint for claude-agent-files.sh (always exits 0: fail-soft)."""
    parser = argparse.ArgumentParser(
        description="Apply LMER_DISPATCH_<LANE> config to linked agent defs"
    )
    parser.add_argument("agents_dir", help="the ~/.claude/agents directory")
    parser.add_argument("--global-src", default="", help="global agents source dir")
    parser.add_argument("--work-src", default="", help="work-repo agents source dir")
    args = parser.parse_args(argv)

    agents_dir = Path(args.agents_dir)
    if not agents_dir.is_dir():
        # Nothing linked (no agent sources at all) — nothing to render. But
        # if lanes ARE configured, say so instead of silently ignoring them.
        configured = [
            ENV_PREFIX + lane
            for lane in LANE_AGENTS
            if (os.environ.get(ENV_PREFIX + lane) or "").strip()
        ]
        if configured:
            print(
                f"⚠️  {', '.join(sorted(configured))} set but no agents dir "
                f"at {agents_dir} — dispatch config not applied"
            )
        return 0
    global_src = Path(args.global_src) if args.global_src else None
    work_src = Path(args.work_src) if args.work_src else None
    for message in apply_dispatch(agents_dir, global_src, work_src):
        print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
