"""Render lmer's Claude slash-command files as harness prompt templates.

Claude Code slash commands live in ``agent-files/claude/commands/*.md``
(YAML frontmatter + a markdown body where a leading-``!`` line executes a
shell command and injects its output). codex and pi have a native
equivalent — markdown *prompt templates* discovered from a per-user
directory and invoked as slash commands:

* pi: ``~/.pi/agent/prompts/*.md`` → ``/name``
* codex: ``~/.codex/prompts/*.md`` → ``/prompts:name`` (deprecated upstream
  in favor of skills, but functional)

Both read the same frontmatter fields lmer's command files already carry
(``description``, ``argument-hint``) and the same ``$1``/``$ARGUMENTS``
placeholders, so the conversion is mechanical:

* frontmatter: keep ``description`` and ``argument-hint``; drop everything
  claude-specific (``allowed-tools`` — these harnesses have no claude
  permission model to feed);
* body: rewrite each leading-``!`` execution line into an instruction to
  run that command and follow its output — the template expands to plain
  prompt text, and codex/pi then run the command as an ordinary tool call;
* a body that never references its arguments gets a trailing
  ``$ARGUMENTS`` line, preserving Claude Code's append-the-arguments
  behavior (``/start phasic`` must not silently drop ``phasic``; with no
  arguments it expands to nothing). ``$ARGUMENTS`` is the portable
  spelling — pi also accepts ``$@`` as a synonym, codex does not document
  it.

Sources layer like ``claude_link_agent_files``: the global tree renders
first, then the work repo over it, so a work-repo command of the same name
wins.

Invoked as ``python3 -m lmer_cli.container.prompt_templates <target-dir>
<source-dir> [<source-dir> ...]`` from ``harness_render_prompt_templates``
(libexec/harness-common.sh). Fail-soft throughout: an unreadable command
file warns and is skipped — provisioning must never kill the session.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Frontmatter keys shared by claude commands and codex/pi prompt templates.
KEPT_FRONTMATTER_KEYS = ("description", "argument-hint")

# Argument placeholders codex/pi expand; a body containing none of these
# gets the trailing $ARGUMENTS appended. $@ stays in the detection set (pi
# accepts it as a synonym, so a pi-authored work-repo command may use it)
# but is never emitted — codex does not document it.
_ARG_PLACEHOLDER = re.compile(r"\$(ARGUMENTS\b|@|[1-9])")


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Split ``text`` into (frontmatter, body); frontmatter is "" if absent."""
    if not text.startswith("---\n"):
        return "", text
    end = text.find("\n---\n", 4)
    if end == -1:
        return "", text
    return text[4:end], text[end + len("\n---\n"):]


def _kept_frontmatter_lines(frontmatter: str) -> list[str]:
    """Return the frontmatter lines whose key survives the conversion.

    The command files use single-line ``key: value`` entries only (the
    long ``allowed-tools`` value is one line), so line-wise filtering is
    sufficient — no YAML parser needed.
    """
    kept = []
    for line in frontmatter.splitlines():
        key = line.split(":", 1)[0].strip().lower()
        if key in KEPT_FRONTMATTER_KEYS and ":" in line:
            kept.append(line)
    return kept


def _convert_body(body: str) -> str:
    """Rewrite Claude Code ``!command`` execution lines into instructions."""
    lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("!") and len(stripped) > 1:
            cmd = stripped[1:].strip()
            lines.append(f"Run `{cmd}` now and follow the instructions in its output.")
        else:
            lines.append(line)
    converted = "\n".join(lines).strip("\n")
    if not _ARG_PLACEHOLDER.search(converted):
        converted += "\n\n$ARGUMENTS"
    return converted + "\n"


def convert_command_text(text: str) -> str:
    """Convert one claude command file's text into a prompt template."""
    frontmatter, body = _split_frontmatter(text)
    kept = _kept_frontmatter_lines(frontmatter)
    out = ""
    if kept:
        out = "---\n" + "\n".join(kept) + "\n---\n\n"
    return out + _convert_body(body)


def render_prompt_templates(target_dir: Path, source_dirs: list[Path]) -> int:
    """Render every command in ``source_dirs`` into ``target_dir``.

    Later sources overwrite earlier ones on name collision (pass the
    global tree first, the work repo last). Returns the number of
    templates written; discovery is non-recursive to match how both
    harnesses load the directory.
    """
    written: set[str] = set()
    target_dir.mkdir(parents=True, exist_ok=True)
    for source in source_dirs:
        if not source.is_dir():
            continue
        for command_file in sorted(source.glob("*.md")):
            try:
                template = convert_command_text(command_file.read_text())
                (target_dir / command_file.name).write_text(template)
                written.add(command_file.name)
            except OSError as exc:
                print(
                    f"⚠️  Skipping prompt template {command_file}: {exc}",
                    file=sys.stderr,
                )
    return len(written)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render claude command files as harness prompt templates"
    )
    parser.add_argument("target_dir", type=Path)
    parser.add_argument("source_dirs", type=Path, nargs="+")
    ns = parser.parse_args(argv)
    count = render_prompt_templates(ns.target_dir, ns.source_dirs)
    if count:
        print(f"✅ Rendered {count} slash-command prompt template(s) into {ns.target_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
