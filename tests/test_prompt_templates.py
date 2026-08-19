"""Tests for the Claude-command to pi prompt-template converter."""

from pathlib import Path

from lmer_cli.container.prompt_templates import (
    convert_command_text,
    main,
    render_prompt_templates,
)

COMMAND = """---
description: Load follow-up instructions for the active task
allowed-tools: Bash(work:*), Bash(git add:*), Edit(/tmp/**)
---

Load and display the active task's follow-up instructions:

!bash /Agents/global/hooks/followup.sh
"""


class TestConvertCommandText:
    def test_description_kept_allowed_tools_dropped(self):
        out = convert_command_text(COMMAND)
        assert "description: Load follow-up instructions for the active task" in out
        assert "allowed-tools" not in out
        assert "Bash(work:*)" not in out

    def test_argument_hint_kept(self):
        out = convert_command_text(
            "---\ndescription: d\nargument-hint: \"<mode>\"\n---\nbody $1\n"
        )
        assert 'argument-hint: "<mode>"' in out

    def test_bang_line_rewritten_to_run_instruction(self):
        out = convert_command_text(COMMAND)
        assert "!bash /Agents/global/hooks/followup.sh" not in out
        assert (
            "Run `bash /Agents/global/hooks/followup.sh` now and follow the "
            "instructions in its output." in out
        )

    def test_prose_body_passes_through(self):
        out = convert_command_text(COMMAND)
        assert "Load and display the active task's follow-up instructions:" in out

    def test_no_frontmatter_body_only(self):
        out = convert_command_text("Just a prompt body.\n")
        assert not out.startswith("---")
        assert "Just a prompt body." in out

    def test_arguments_appended_when_body_never_references_them(self):
        # Claude Code appends the invocation arguments when a command has no
        # placeholder; pi drops them — the trailing $ARGUMENTS preserves
        # `/start phasic` semantics (and expands to nothing without args).
        # $ARGUMENTS is the renderer's canonical spelling (pi accepts $@ too).
        out = convert_command_text(COMMAND)
        assert out.rstrip().endswith("$ARGUMENTS")

    def test_arguments_not_appended_when_placeholder_present(self):
        assert convert_command_text("use $ARGUMENTS here\n").rstrip().endswith(
            "use $ARGUMENTS here"
        )
        assert convert_command_text("first is $1\n").rstrip().endswith("first is $1")
        assert convert_command_text("all: $@\n").rstrip().endswith("all: $@")

    def test_unclosed_frontmatter_treated_as_body(self):
        # No closing fence → not frontmatter; the text passes through
        # verbatim as body (plus the appended $ARGUMENTS) instead of being
        # filtered.
        out = convert_command_text("---\ndescription: dangling\nbody\n")
        assert out.startswith("---\ndescription: dangling\nbody")
        assert out.rstrip().endswith("$ARGUMENTS")


class TestRenderPromptTemplates:
    def _commands(self, root: Path, name_to_text: dict) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        for name, text in name_to_text.items():
            (root / name).write_text(text)
        return root

    def test_renders_all_md_files(self, tmp_path):
        src = self._commands(tmp_path / "cmds", {"start.md": COMMAND, "test.md": COMMAND})
        target = tmp_path / "prompts"
        assert render_prompt_templates(target, [src]) == 2
        assert (target / "start.md").is_file()
        assert (target / "test.md").is_file()

    def test_later_source_wins_on_name_collision(self, tmp_path):
        global_src = self._commands(
            tmp_path / "global", {"start.md": "---\ndescription: global\n---\ng\n"}
        )
        work_src = self._commands(
            tmp_path / "work", {"start.md": "---\ndescription: work\n---\nw\n"}
        )
        target = tmp_path / "prompts"
        assert render_prompt_templates(target, [global_src, work_src]) == 1
        assert "description: work" in (target / "start.md").read_text()

    def test_missing_source_dir_is_skipped(self, tmp_path):
        src = self._commands(tmp_path / "cmds", {"start.md": COMMAND})
        count = render_prompt_templates(
            tmp_path / "prompts", [tmp_path / "missing", src]
        )
        assert count == 1

    def test_discovery_is_non_recursive(self, tmp_path):
        # Both harnesses load only top-level *.md from their prompts dir.
        src = self._commands(tmp_path / "cmds", {"start.md": COMMAND})
        (src / "sub").mkdir()
        (src / "sub" / "nested.md").write_text(COMMAND)
        target = tmp_path / "prompts"
        assert render_prompt_templates(target, [src]) == 1
        assert not (target / "nested.md").exists()


class TestMain:
    def test_main_renders_and_reports(self, tmp_path, capsys):
        src = tmp_path / "cmds"
        src.mkdir()
        (src / "followup.md").write_text(COMMAND)
        target = tmp_path / "prompts"
        assert main([str(target), str(src)]) == 0
        assert (target / "followup.md").is_file()
        assert "Rendered 1 slash-command prompt template(s)" in capsys.readouterr().out

    def test_main_quiet_when_nothing_to_render(self, tmp_path, capsys):
        assert main([str(tmp_path / "prompts"), str(tmp_path / "missing")]) == 0
        assert "Rendered" not in capsys.readouterr().out
