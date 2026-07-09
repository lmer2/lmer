"""Tests for work-repo-based taskdef discovery in hooks/start.py.

Covers project-scoped, work-globally-scoped, precedence ordering, and
graceful handling of missing env vars / paths.
"""
from pathlib import Path

import pytest

from hooks.start import (
    find_taskdef_file,
    render_taskdef_template,
    taskdef_search_dirs,
    work_repo_taskdef_dirs,
)
from lmer_cli import cli as lmer_cli
from tests.conftest import strip_lmer_env


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    """Strip LMER_* env vars so each test starts from a clean slate."""
    strip_lmer_env(monkeypatch)


def _make_taskdef(parent: Path, task_name: str, marker: str) -> Path:
    """Create a taskdef directory with an instructions.txt sentinel marker."""
    taskdef = parent / task_name
    taskdef.mkdir(parents=True, exist_ok=True)
    (taskdef / "instructions.txt").write_text(marker)
    return taskdef


class TestWorkRepoTaskdefDirs:
    """Discovery of work-repo taskdef directories."""

    def test_empty_when_work_repo_path_unset(self, monkeypatch):
        assert work_repo_taskdef_dirs() == []

    def test_empty_when_work_repo_path_missing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path / "missing"))
        assert work_repo_taskdef_dirs() == []

    def test_project_scoped_only(self, monkeypatch, tmp_path):
        project_taskdefs = tmp_path / "git.example.com" / "org" / "repo" / "taskdef"
        project_taskdefs.mkdir(parents=True)

        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
        monkeypatch.setenv("LMER_REPO_PROJECT", "org/repo")

        assert work_repo_taskdef_dirs() == [project_taskdefs]

    def test_work_global_only(self, monkeypatch, tmp_path):
        global_taskdefs = tmp_path / "taskdef"
        global_taskdefs.mkdir()

        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
        # host/project unset → no project-scoped entry

        assert work_repo_taskdef_dirs() == [global_taskdefs]

    def test_project_then_global_order(self, monkeypatch, tmp_path):
        project_taskdefs = tmp_path / "git.example.com" / "org" / "repo" / "taskdef"
        project_taskdefs.mkdir(parents=True)
        global_taskdefs = tmp_path / "taskdef"
        global_taskdefs.mkdir()

        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
        monkeypatch.setenv("LMER_REPO_PROJECT", "org/repo")

        assert work_repo_taskdef_dirs() == [project_taskdefs, global_taskdefs]

    def test_skips_nonexistent_project_dir(self, monkeypatch, tmp_path):
        # Only global exists, project does not
        global_taskdefs = tmp_path / "taskdef"
        global_taskdefs.mkdir()

        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
        monkeypatch.setenv("LMER_REPO_PROJECT", "org/repo")

        assert work_repo_taskdef_dirs() == [global_taskdefs]


class TestTaskdefSearchDirsPrecedence:
    """taskdef_search_dirs() must order: project > work-global > LMER_TASKDEF_PATHS > built-in."""

    def test_full_precedence_order(self, monkeypatch, tmp_path):
        work_root = tmp_path / "work"
        project_taskdefs = work_root / "git.example.com" / "org" / "repo" / "taskdef"
        project_taskdefs.mkdir(parents=True)
        global_taskdefs = work_root / "taskdef"
        global_taskdefs.mkdir()
        external = tmp_path / "external"
        external.mkdir()

        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work_root))
        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
        monkeypatch.setenv("LMER_REPO_PROJECT", "org/repo")
        monkeypatch.setenv("LMER_TASKDEF_PATHS", str(external))

        dirs = taskdef_search_dirs()
        # The first three entries must be in this order; the trailing built-in
        # path depends on container layout (/home/developer/.lmer or
        # /Agents/global) so we don't assert its exact value.
        assert dirs[0] == project_taskdefs
        assert dirs[1] == global_taskdefs
        assert dirs[2] == external
        assert dirs[-1].name == "taskdef"  # built-in always last


class TestFindTaskdefFileWorkRepoOverride:
    """find_taskdef_file() honours work-repo taskdefs even when CLI pre-resolved."""

    def test_work_project_overrides_pre_resolved_env(self, monkeypatch, tmp_path):
        # Built-in path (set by the CLI as a fast path)
        builtin_root = tmp_path / "builtin"
        builtin_taskdef = _make_taskdef(builtin_root, "develop", "from-builtin")

        # Work-repo project taskdef with the same name
        work_root = tmp_path / "work"
        project_taskdefs = work_root / "git.example.com" / "org" / "repo" / "taskdef"
        project_taskdef = _make_taskdef(project_taskdefs, "develop", "from-project")

        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work_root))
        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
        monkeypatch.setenv("LMER_REPO_PROJECT", "org/repo")
        monkeypatch.setenv("LMER_TASK", "develop")
        # Simulate cli.py's pre-resolution to the built-in path
        monkeypatch.setenv("LMER_TASKDEF_DIR", str(builtin_taskdef))
        monkeypatch.setenv(
            "LMER_TASK_INSTRUCTIONS", str(builtin_taskdef / "instructions.txt")
        )

        resolved = find_taskdef_file("instructions.txt")
        assert resolved == project_taskdef / "instructions.txt"
        assert resolved.read_text() == "from-project"

    def test_work_global_overrides_external_paths(self, monkeypatch, tmp_path):
        external = tmp_path / "external"
        _make_taskdef(external, "develop", "from-external")

        work_root = tmp_path / "work"
        global_taskdefs = work_root / "taskdef"
        global_taskdef = _make_taskdef(global_taskdefs, "develop", "from-global")

        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work_root))
        monkeypatch.setenv("LMER_TASK", "develop")
        monkeypatch.setenv("LMER_TASKDEF_PATHS", str(external))

        resolved = find_taskdef_file("instructions.txt")
        assert resolved == global_taskdef / "instructions.txt"

    def test_project_overrides_global(self, monkeypatch, tmp_path):
        work_root = tmp_path / "work"
        project_taskdefs = work_root / "git.example.com" / "org" / "repo" / "taskdef"
        project_taskdef = _make_taskdef(project_taskdefs, "develop", "from-project")
        global_taskdefs = work_root / "taskdef"
        _make_taskdef(global_taskdefs, "develop", "from-global")

        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work_root))
        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
        monkeypatch.setenv("LMER_REPO_PROJECT", "org/repo")
        monkeypatch.setenv("LMER_TASK", "develop")

        resolved = find_taskdef_file("instructions.txt")
        assert resolved == project_taskdef / "instructions.txt"

    def test_falls_back_to_env_when_work_repo_lacks_task(
        self, monkeypatch, tmp_path
    ):
        builtin_taskdef = _make_taskdef(tmp_path / "builtin", "develop", "from-builtin")

        # work repo exists but has no matching task
        work_root = tmp_path / "work"
        (work_root / "taskdef").mkdir(parents=True)

        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work_root))
        monkeypatch.setenv("LMER_TASK", "develop")
        monkeypatch.setenv("LMER_TASKDEF_DIR", str(builtin_taskdef))

        resolved = find_taskdef_file("instructions.txt")
        assert resolved == builtin_taskdef / "instructions.txt"


class TestRenderTaskdefTemplateWorkRepoIncludes:
    """render_taskdef_template() must add work-repo dirs to the Jinja2 loader."""

    def test_include_resolves_from_work_repo_project(self, monkeypatch, tmp_path):
        # Built-in taskdef has the entrypoint template; the included partial
        # lives only in the work-repo project taskdefs dir.
        builtin_root = tmp_path / "builtin"
        builtin_taskdef = _make_taskdef(builtin_root, "develop", "unused")
        template_file = builtin_taskdef / "instructions.txt"
        template_file.write_text("BODY\n{% include 'shared/snippet.jinja2' %}\nEND")

        work_root = tmp_path / "work"
        project_taskdefs = work_root / "git.example.com" / "org" / "repo" / "taskdef"
        project_taskdefs.mkdir(parents=True)
        snippet_dir = project_taskdefs / "shared"
        snippet_dir.mkdir()
        (snippet_dir / "snippet.jinja2").write_text("PROJECT-SNIPPET")

        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work_root))
        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
        monkeypatch.setenv("LMER_REPO_PROJECT", "org/repo")

        rendered = render_taskdef_template(template_file)
        assert "BODY" in rendered
        assert "PROJECT-SNIPPET" in rendered
        assert "END" in rendered

    def test_include_resolves_from_work_repo_global(self, monkeypatch, tmp_path):
        builtin_root = tmp_path / "builtin"
        builtin_taskdef = _make_taskdef(builtin_root, "develop", "unused")
        template_file = builtin_taskdef / "instructions.txt"
        template_file.write_text("{% include 'shared/snippet.jinja2' %}")

        work_root = tmp_path / "work"
        global_snippet_dir = work_root / "taskdef" / "shared"
        global_snippet_dir.mkdir(parents=True)
        (global_snippet_dir / "snippet.jinja2").write_text("GLOBAL-SNIPPET")

        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work_root))

        rendered = render_taskdef_template(template_file)
        assert rendered == "GLOBAL-SNIPPET"


class TestCliUnknownTaskRelaxation:
    """CLI must no longer hard-error on an unknown task name; it warns and proceeds.

    The validation block in cli.py used to `return 2` when the task wasn't in
    the host-side known set. With work-repo taskdefs only visible inside the
    container, that check is now advisory.
    """

    def test_unknown_task_warns_instead_of_erroring(self, monkeypatch, tmp_path, capsys):
        """A made-up task name produces a warning and proceeds past the validation block."""
        # main() will eventually fail at the LMER_WORK_REPO check (line ~790)
        # because we deliberately unset it. That failure is several steps PAST
        # the validation block we care about; reaching it confirms validation
        # was relaxed from a hard error to a warning.
        monkeypatch.delenv("LMER_WORK_REPO", raising=False)

        # Provide a non-empty known_tasks so the warning branch can fire at all.
        fake_taskdef_dir = tmp_path / "taskdef"
        (fake_taskdef_dir / "develop").mkdir(parents=True)
        (fake_taskdef_dir / "develop" / "instructions.txt").write_text("x")
        monkeypatch.setattr(lmer_cli, "repo_root_path", lambda: tmp_path)

        rc = lmer_cli.main(["nonexistent-task"])
        combined = capsys.readouterr().out

        # The old behaviour was: error("Unknown task ..."); return 2 — at the
        # validation step. The new behaviour: warning(...) then proceed. We
        # expect a non-zero rc from a downstream failure, but never the old
        # "Unknown task '" error message, and we expect the new warning copy.
        assert "Unknown task '" not in combined
        assert "not found in host-side taskdef directories" in combined
        # Sanity: the run still fails downstream (missing LMER_WORK_REPO) —
        # not a clean exit, but also not the validation-block hard reject.
        assert rc != 0
