"""Render-matrix tests for the taskdef tiers (docs/TASKDEFS.md).

Renders `chat` (builtin) plus schema-1 and schema-2 fixture taskdefs across
work modes (finish/phasic) × providers (gitlab/github) × the three supported
source configurations:

  - work-repo-only: taskdefs served from ``{work_repo}/taskdef/``, extending
    the builtin base across tiers
  - dedicated-repo: taskdefs served via ``LMER_TASKDEF_PATHS`` (the same
    mechanism the CLI uses for the ``/taskdef`` clone of LMER_TASKDEF_REPO)
  - builtin-only: no work repo, no external paths — ``chat`` renders with
    zero configuration

Also covers cross-tier shadowing (a work-repo tier overriding
``base-task.jinja2`` or a shared partial, with the banner naming the winning
source) and the render-time block lint's failure mode.

External-source mode: when ``LMER_RENDER_SOURCE`` is set, every taskdef
directory found under it is rendered — honoring its root ``taskdef.yaml`` —
against the *current checkout's* builtin base. This exact contract is reused
by the ``agents/taskdefs`` population checks and that repo's CI.
"""
import os
import shutil
from pathlib import Path

import pytest

from hooks.start import TaskdefRenderError, render_taskdef_template
from tests.conftest import strip_lmer_env

REPO_TASKDEF = Path(__file__).parent.parent / "taskdef"
FIXTURES = Path(__file__).parent / "fixtures" / "taskdefs"

# Read before the autouse env-strip fixture runs: external-source mode is an
# invocation-level switch, not per-test state.
RENDER_SOURCE = os.environ.get("LMER_RENDER_SOURCE")

WORK_MODES = ("finish", "phasic")
# provider name -> LMER_REPO_HOST value that selects it
PROVIDERS = {"gitlab": "git.example.com", "github": "github.com"}


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    """Strip LMER_* env vars so each matrix cell builds its own config."""
    strip_lmer_env(monkeypatch)


@pytest.fixture(autouse=True)
def _repo_builtin_root(monkeypatch):
    """Pin the builtin tier to this checkout's taskdef/ — the matrix must
    exercise the base template under development, not a container mount."""
    monkeypatch.setattr(
        "hooks.start.builtin_taskdef_root", lambda: REPO_TASKDEF
    )
    monkeypatch.setenv("LMER_TASKDEF_ROOT", str(REPO_TASKDEF))


def _render(template_file, work_mode):
    return render_taskdef_template(
        template_file,
        {
            "work_mode": work_mode,
            "run_state_brief": "",
            "instructions_file": str(template_file),
            "followup_file": str(template_file),
        },
    )


def _assert_fully_rendered(out):
    assert out.strip()
    assert "{%" not in out and "{{" not in out


def _assert_mode(out, work_mode, has_phasic_include=True):
    if not has_phasic_include:
        return
    if work_mode == "phasic":
        assert "## Phasic workflow" in out
    else:
        assert "## Phasic workflow" not in out


def _install_fixture(kind, dest_root):
    """Copy the schema-1 or schema-2 fixture taskdef into a source root."""
    src_root = FIXTURES / kind
    dest_root.mkdir(parents=True, exist_ok=True)
    for entry in src_root.iterdir():
        target = dest_root / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, dirs_exist_ok=True)
        else:
            shutil.copy(entry, target)
    taskdef_name = "s2demo" if kind == "schema2" else "s1demo"
    return dest_root / taskdef_name / "instructions.txt"


def _configure_source(config, kind, tmp_path, monkeypatch):
    """Build one source configuration; returns (instructions_file, root)."""
    if config == "workrepo":
        work = tmp_path / "work"
        root = work / "taskdef"
        instructions = _install_fixture(kind, root)
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))
        return instructions, root
    if config == "dedicated":
        root = tmp_path / "taskdef-clone"
        instructions = _install_fixture(kind, root)
        monkeypatch.setenv("LMER_TASKDEF_PATHS", str(root))
        return instructions, root
    raise AssertionError(f"unknown config {config}")


class TestRenderMatrix:
    """chat + schema-1/schema-2 fixtures × modes × providers × sources."""

    @pytest.mark.parametrize("provider", sorted(PROVIDERS))
    @pytest.mark.parametrize("work_mode", WORK_MODES)
    @pytest.mark.parametrize("config", ("workrepo", "dedicated"))
    @pytest.mark.parametrize("kind", ("schema1", "schema2"))
    def test_fixture_taskdefs_render(
        self, kind, config, work_mode, provider, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("LMER_REPO_HOST", PROVIDERS[provider])
        monkeypatch.setenv("LMER_TASK_TARGET", "matrix-target")
        instructions, root = _configure_source(
            config, kind, tmp_path, monkeypatch
        )

        out = _render(instructions, work_mode)

        _assert_fully_rendered(out)
        _assert_mode(out, work_mode)
        if provider == "github":
            assert "github-review" in out
            assert "gitlab-review" not in out
        else:
            assert "gitlab-review" in out
            assert "github-review" not in out
        schema = 2 if kind == "schema2" else 1
        banner = capsys.readouterr().out
        assert f"taskdef source: {root} (schema {schema})" in banner
        if kind == "schema2":
            # The body's own content plus the inherited spine:
            assert out.startswith("# Schema-2 Matrix Demo")
            assert "## Phase 1: Matrix demo work" in out
            assert "## Phase -1: Branch Setup" in out
            assert "DO NOT leave the render matrix" in out
            assert "DO NOT create worklogs, progress notes, or task journals" in out
            if work_mode == "phasic":
                assert "Checking the work repository run log" in out
                assert "recording progress with `work log`" in out
            # The nested do_not_branch_rule block is part of the block
            # interface (docs/TASKDEFS.md) — the fixture overrides it, so
            # removing or renaming it base-side fails here, not only in
            # the external-source matrix.
            assert "the matrix continues on an existing one" in out
            assert "always create a new branch first" not in out
            assert (
                f"taskdef source (base-task.jinja2): {REPO_TASKDEF} "
                "(schema 1)" in banner
            )
            if provider == "github":
                assert "gh pr create" in out
            else:
                assert "--create-mr" in out

    @pytest.mark.parametrize("provider", sorted(PROVIDERS))
    @pytest.mark.parametrize("work_mode", WORK_MODES)
    def test_builtin_only_chat_renders_with_zero_config(
        self, work_mode, provider, monkeypatch, capsys
    ):
        """chat must work out of the box: no work repo, no external paths."""
        monkeypatch.setenv("LMER_REPO_HOST", PROVIDERS[provider])
        out = _render(REPO_TASKDEF / "chat" / "instructions.txt", work_mode)
        _assert_fully_rendered(out)
        assert "no fixed script" in out
        banner = capsys.readouterr().out
        assert f"taskdef source: {REPO_TASKDEF} (schema 1)" in banner

    def test_repository_guidance_sends_progress_to_the_work_repository(self):
        guidance = (REPO_TASKDEF.parent / "AGENTS.md").read_text()

        assert "WORKLOG.md" not in guidance
        assert "work repository run log" in guidance
        assert "`work artifact`" in guidance

    @pytest.mark.parametrize("work_mode", WORK_MODES)
    def test_schema2_followup_renders(self, work_mode, tmp_path, monkeypatch):
        """followup.txt resolves and renders through the same tiers."""
        monkeypatch.setenv("LMER_REPO_HOST", PROVIDERS["gitlab"])
        monkeypatch.setenv("LMER_TASK_TARGET", "matrix-target")
        instructions, _ = _configure_source(
            "dedicated", "schema2", tmp_path, monkeypatch
        )
        followup = instructions.parent / "followup.txt"
        out = _render(followup, work_mode)
        _assert_fully_rendered(out)
        assert "matrix follow-up" in out
        assert "matrix-target" in out


class TestTierShadowing:
    """Cross-tier template shadowing is a supported override feature; the
    banner keeps it observable by naming the winning source."""

    def test_workrepo_tier_shadows_base_template(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("LMER_REPO_HOST", PROVIDERS["gitlab"])
        work = tmp_path / "work"
        root = work / "taskdef"
        instructions = _install_fixture("schema2", root)
        # The work-repo tier ships its own base — e.g. a customer patching
        # the shared spine. It must win over the builtin copy.
        (root / "base-task.jinja2").write_text(
            "{% block intro %}{% endblock %}\n"
            "WORKREPO-BASE-MARKER\n"
            "{% block task_phases %}{% endblock %}\n"
            "{% block do_not_branch_rule %}{% endblock %}\n"
            "{% block do_not_extra %}{% endblock %}\n"
        )
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))

        out = _render(instructions, "finish")

        assert "WORKREPO-BASE-MARKER" in out
        assert "## Phase -1: Branch Setup" not in out  # builtin base lost
        banner = capsys.readouterr().out
        assert f"taskdef source (base-task.jinja2): {root} " in banner

    def test_workrepo_tier_shadows_shared_partial(
        self, tmp_path, monkeypatch
    ):
        """A body in the dedicated tier picks up a work-repo override of a
        shared partial — one patched block, everything else builtin."""
        monkeypatch.setenv("LMER_REPO_HOST", PROVIDERS["gitlab"])
        instructions, _ = _configure_source(
            "dedicated", "schema2", tmp_path, monkeypatch
        )
        work = tmp_path / "work"
        (work / "taskdef").mkdir(parents=True)
        (work / "taskdef" / "phasic.jinja2").write_text(
            "WORKREPO-PHASIC-MARKER\n"
        )
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))

        out = _render(instructions, "phasic")

        assert "WORKREPO-PHASIC-MARKER" in out
        assert "## Phasic workflow" not in out  # builtin partial shadowed
        assert "## Phase -1: Branch Setup" in out  # base still builtin


class TestBlockLintInMatrix:
    """The render-time block lint fails the matrix, not just unit tests."""

    def test_bad_override_block_fails_render(self, tmp_path, monkeypatch):
        root = tmp_path / "taskdef-clone"
        root.mkdir()
        (root / "taskdef.yaml").write_text("schema: 2\n")
        bad = root / "bad"
        bad.mkdir()
        (bad / "instructions.txt").write_text(
            "{% extends 'base-task.jinja2' %}\n"
            "{% block intro %}# Bad{% endblock %}\n"
            "{% block task_phasez %}dropped silently{% endblock %}\n"
        )
        monkeypatch.setenv("LMER_TASKDEF_PATHS", str(root))
        with pytest.raises(TaskdefRenderError) as exc:
            _render(bad / "instructions.txt", "finish")
        assert "task_phasez" in str(exc.value)


@pytest.mark.skipif(
    not RENDER_SOURCE,
    reason="external-source mode: set LMER_RENDER_SOURCE to a taskdef root",
)
class TestExternalSourceMatrix:
    """The LMER_RENDER_SOURCE contract: render every taskdef dir found under
    the given root — honoring its root taskdef.yaml — against the current
    checkout's builtin base, across the full mode × provider matrix.
    Reused by the agents/taskdefs population checks and that repo's CI."""

    def test_external_source_renders_every_taskdef(self, monkeypatch):
        root = Path(RENDER_SOURCE)
        assert root.is_dir(), f"LMER_RENDER_SOURCE not a directory: {root}"
        taskdef_dirs = sorted(
            d
            for d in root.iterdir()
            if d.is_dir() and (d / "instructions.txt").exists()
        )
        assert taskdef_dirs, f"no taskdef dirs under {root}"
        monkeypatch.setenv("LMER_TASKDEF_PATHS", str(root))
        monkeypatch.setenv("LMER_TASK_TARGET", "external-target")
        rendered = 0
        for taskdef_dir in taskdef_dirs:
            files = [taskdef_dir / "instructions.txt"]
            if (taskdef_dir / "followup.txt").exists():
                files.append(taskdef_dir / "followup.txt")
            for template_file in files:
                for work_mode in WORK_MODES:
                    for host in PROVIDERS.values():
                        monkeypatch.setenv("LMER_REPO_HOST", host)
                        out = _render(template_file, work_mode)
                        _assert_fully_rendered(out)
                        rendered += 1
        print(
            f"external-source matrix: {len(taskdef_dirs)} taskdef(s), "
            f"{rendered} render(s) from {root}"
        )
