"""Test start hook functionality with Jinja2 rendering."""
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
import pytest

from hooks.start import (
    main,
    read_and_display_instructions,
    check_task_context,
    _is_github_host,
    _redact_url_credentials,
    _target_provider_flags,
)
from tests.conftest import strip_lmer_env

FIXTURES = Path(__file__).parent / "fixtures" / "schema_sources"
REPO_TASKDEF = Path(__file__).parent.parent / "taskdef"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    """Strip LMER_* env vars so each test starts from a clean slate.

    Tests here reach read_and_display_instructions(), whose
    run_state_session_start() shells out to the real `work session-start`.
    Without this isolation the subprocess inherits the running session's
    env and seeds/claims runs in the operational work repo (issue #93);
    with no LMER_REPO_HOST/LMER_REPO_PROJECT it no-ops and writes nothing.
    """
    strip_lmer_env(monkeypatch)


class TestStartHook:
    """Test start hook Jinja2 rendering and work mode functionality."""

    def test_jinja2_rendering_with_lmer_env_vars(self, tmp_path, monkeypatch):
        """Test that Jinja2 templates render with LMER_* environment variables."""
        # Setup test environment
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("LMER_REPO_URL", "https://example.com/repo")
        monkeypatch.setenv("LMER_TASK_TARGET", "target123")
        monkeypatch.setenv("OTHER_VAR", "should_not_appear")  # Non-LMER var

        # Create test instructions file
        instructions_file = tmp_path / "instructions.txt"
        instructions_file.write_text(
            "Repository: {{ LMER_REPO_URL }}\n"
            "Target: {{ LMER_TASK_TARGET }}\n"
        )

        # Mock Path.home() for timestamp file
        with patch('hooks.start.Path.home', return_value=tmp_path):
            # Capture print output
            f = io.StringIO()
            with redirect_stdout(f):
                read_and_display_instructions(instructions_file, "finish")

            output = f.getvalue()
            assert "Repository: https://example.com/repo" in output
            assert "Target: target123" in output
            assert "should_not_appear" not in output  # Non-LMER vars shouldn't appear

    def test_work_mode_defaults_to_finish(self, tmp_path, monkeypatch):
        """Test that work_mode defaults to 'finish'."""
        monkeypatch.setenv("HOME", str(tmp_path))

        instructions_file = tmp_path / "instructions.txt"
        instructions_file.write_text("Work mode: {{ work_mode }}")

        with patch('hooks.start.Path.home', return_value=tmp_path):
            f = io.StringIO()
            with redirect_stdout(f):
                read_and_display_instructions(instructions_file, "finish")

            output = f.getvalue()
            assert "Work mode: finish" in output

    def test_work_mode_phasic(self, tmp_path, monkeypatch):
        """Test that work_mode can be set to 'phasic'."""
        monkeypatch.setenv("HOME", str(tmp_path))

        instructions_file = tmp_path / "instructions.txt"
        instructions_file.write_text("Work mode: {{ work_mode }}")

        with patch('hooks.start.Path.home', return_value=tmp_path):
            f = io.StringIO()
            with redirect_stdout(f):
                read_and_display_instructions(instructions_file, "phasic")

            output = f.getvalue()
            assert "Work mode: phasic" in output

    def test_computed_values_available(self, tmp_path, monkeypatch):
        """Test that computed values (taskdef_name, instructions_file) are available."""
        monkeypatch.setenv("HOME", str(tmp_path))

        taskdef_dir = tmp_path / "taskdef" / "test_task"
        taskdef_dir.mkdir(parents=True)
        instructions_file = taskdef_dir / "instructions.txt"
        instructions_file.write_text(
            "Taskdef: {{ taskdef_name }}\n"
            "File: {{ instructions_file }}"
        )

        with patch('hooks.start.Path.home', return_value=tmp_path):
            f = io.StringIO()
            with redirect_stdout(f):
                read_and_display_instructions(instructions_file, "finish")

            output = f.getvalue()
            assert "Taskdef: test_task" in output
            assert "File:" in output
            assert str(instructions_file) in output

    def test_jinja2_conditionals_with_work_mode(self, tmp_path, monkeypatch):
        """Test Jinja2 conditionals work with work_mode."""
        monkeypatch.setenv("HOME", str(tmp_path))

        instructions_file = tmp_path / "instructions.txt"
        instructions_file.write_text(
            "{% if work_mode == 'phasic' %}\n"
            "This is phasic mode\n"
            "{% else %}\n"
            "This is finish mode\n"
            "{% endif %}"
        )

        with patch('hooks.start.Path.home', return_value=tmp_path):
            # Test finish mode
            f = io.StringIO()
            with redirect_stdout(f):
                read_and_display_instructions(instructions_file, "finish")
            output = f.getvalue()
            assert "This is finish mode" in output
            assert "This is phasic mode" not in output

            # Test phasic mode
            f = io.StringIO()
            with redirect_stdout(f):
                read_and_display_instructions(instructions_file, "phasic")
            output = f.getvalue()
            assert "This is phasic mode" in output
            assert "This is finish mode" not in output

    def test_only_lmer_env_vars_included(self, tmp_path, monkeypatch):
        """Test that only LMER_* environment variables are included in context."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("LMER_REPO_URL", "https://example.com/repo")
        monkeypatch.setenv("GITLAB_HOST", "gitlab.com")  # Should not be included
        monkeypatch.setenv("PATH", "/usr/bin")  # Should not be included

        instructions_file = tmp_path / "instructions.txt"
        instructions_file.write_text(
            "{% if LMER_REPO_URL %}Repo: {{ LMER_REPO_URL }}{% endif %}\n"
            "{% if GITLAB_HOST %}GitLab: {{ GITLAB_HOST }}{% endif %}\n"
            "{% if PATH %}Path: {{ PATH }}{% endif %}"
        )

        with patch('hooks.start.Path.home', return_value=tmp_path):
            f = io.StringIO()
            with redirect_stdout(f):
                read_and_display_instructions(instructions_file, "finish")

            output = f.getvalue()
            assert "Repo: https://example.com/repo" in output
            assert "GitLab:" not in output  # GITLAB_HOST should not be available
            assert "Path:" not in output  # PATH should not be available

    def test_main_with_work_mode_parameter(self, tmp_path, monkeypatch):
        """Test main() function parses work_mode parameter correctly."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("LMER_TASK", "test_task")

        instructions_file = tmp_path / "taskdef" / "test_task" / "instructions.txt"
        instructions_file.parent.mkdir(parents=True)
        instructions_file.write_text("Work mode: {{ work_mode }}")

        # Mock find_taskdef_instructions to return our test file
        with patch('hooks.start.find_taskdef_instructions', return_value=instructions_file):
            with patch('hooks.start.Path.home', return_value=tmp_path):
                # Test with phasic mode
                with patch('sys.argv', ['start.py', 'phasic']):
                    f = io.StringIO()
                    with redirect_stdout(f):
                        main()
                    output = f.getvalue()
                    assert "work mode: phasic" in output.lower()
                    assert "Work mode: phasic" in output

    def test_main_defaults_to_finish(self, tmp_path, monkeypatch):
        """Test main() defaults to 'finish' when no parameter provided."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("LMER_TASK", "test_task")

        instructions_file = tmp_path / "taskdef" / "test_task" / "instructions.txt"
        instructions_file.parent.mkdir(parents=True)
        instructions_file.write_text("Work mode: {{ work_mode }}")

        with patch('hooks.start.find_taskdef_instructions', return_value=instructions_file):
            with patch('hooks.start.Path.home', return_value=tmp_path):
                # Test with no arguments
                with patch('sys.argv', ['start.py']):
                    f = io.StringIO()
                    with redirect_stdout(f):
                        main()
                    output = f.getvalue()
                    assert "work mode: finish" in output.lower()
                    assert "Work mode: finish" in output

    def test_main_invalid_work_mode_warning(self, tmp_path, monkeypatch):
        """Test main() warns and defaults when invalid work_mode provided."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("LMER_TASK", "test_task")

        instructions_file = tmp_path / "taskdef" / "test_task" / "instructions.txt"
        instructions_file.parent.mkdir(parents=True)
        instructions_file.write_text("Work mode: {{ work_mode }}")

        with patch('hooks.start.find_taskdef_instructions', return_value=instructions_file):
            with patch('hooks.start.Path.home', return_value=tmp_path):
                # Test with invalid mode
                with patch('sys.argv', ['start.py', 'invalid_mode']):
                    f = io.StringIO()
                    with redirect_stdout(f):
                        main()
                    output = f.getvalue()
                    assert "WARNING" in output or "warning" in output.lower()
                    assert "invalid_mode" in output
                    assert "Work mode: finish" in output  # Should default to finish


class TestTargetProviderFlags:
    """Test is_github / is_gitlab provider detection for the task target."""

    # Env vars that feed host detection; cleared before each scenario so a
    # stray value from the surrounding shell cannot leak into the result.
    _HOST_VARS = ("LMER_REPO_HOST", "LMER_TASK_TARGET", "LMER_REPO_URL")

    def _clear_host_env(self, monkeypatch):
        for var in self._HOST_VARS:
            monkeypatch.delenv(var, raising=False)

    @pytest.mark.parametrize(
        "host,expected",
        [
            ("github.com", True),
            ("GitHub.com", True),
            ("api.github.com", True),
            ("acme.ghe.com", True),
            ("gitlab.example.com", False),
            ("gitlab.com", False),
            ("", False),
            (None, False),
        ],
    )
    def test_is_github_host(self, host, expected):
        assert _is_github_host(host) is expected

    def test_repo_host_github(self, monkeypatch):
        self._clear_host_env(monkeypatch)
        monkeypatch.setenv("LMER_REPO_HOST", "github.com")
        assert _target_provider_flags() == (True, False)

    def test_repo_host_gitlab(self, monkeypatch):
        self._clear_host_env(monkeypatch)
        monkeypatch.setenv("LMER_REPO_HOST", "gitlab.example.com")
        assert _target_provider_flags() == (False, True)

    def test_falls_back_to_task_target_url(self, monkeypatch):
        self._clear_host_env(monkeypatch)
        monkeypatch.setenv(
            "LMER_TASK_TARGET",
            "https://github.com/owner/repo/pull/3",
        )
        assert _target_provider_flags() == (True, False)

    def test_falls_back_to_repo_url(self, monkeypatch):
        self._clear_host_env(monkeypatch)
        monkeypatch.setenv(
            "LMER_REPO_URL",
            "https://oauth2:tok@gitlab.example.com/group/project.git",
        )
        assert _target_provider_flags() == (False, True)

    def test_no_host_both_false(self, monkeypatch):
        self._clear_host_env(monkeypatch)
        assert _target_provider_flags() == (False, False)

    def test_is_github_host_body_matches_canonical(self):
        """Guard against drift between hooks/start.py and lmer_cli.tokens.

        Both implementations must use the same predicate; a future edit to
        either side should fail this test if the bodies diverge.
        """
        import ast
        import inspect
        import textwrap
        from lmer_cli.tokens import _is_github_host as canonical
        from hooks import start as start_hook

        def _body_lines(fn):
            src = textwrap.dedent(inspect.getsource(fn))
            tree = ast.parse(src)
            func = tree.body[0]
            non_doc = [
                node
                for node in func.body
                if not (
                    isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                )
            ]
            return [ast.unparse(node) for node in non_doc]

        assert _body_lines(canonical) == _body_lines(start_hook._is_github_host)

    def test_flags_available_in_template(self, tmp_path, monkeypatch):
        """is_github / is_gitlab are usable in instruction templates."""
        monkeypatch.setenv("HOME", str(tmp_path))
        self._clear_host_env(monkeypatch)
        monkeypatch.setenv("LMER_REPO_HOST", "github.com")

        instructions_file = tmp_path / "instructions.txt"
        instructions_file.write_text(
            "{% if is_github %}use github-review{% endif %}"
            "{% if is_gitlab %}use gitlab-review{% endif %}"
        )

        with patch('hooks.start.Path.home', return_value=tmp_path):
            f = io.StringIO()
            with redirect_stdout(f):
                read_and_display_instructions(instructions_file, "finish")
            output = f.getvalue()
            assert "use github-review" in output
            assert "use gitlab-review" not in output


class TestRedactUrlCredentials:
    """The /start task-context banner must not leak clone-URL credentials."""

    def test_strips_oauth2_token(self):
        url = "https://oauth2:glpat-FAKEtoken1234567890abcd@git.example.com/org/repo.git"
        result = _redact_url_credentials(url)
        assert "glpat-" not in result
        assert "oauth2" not in result
        assert result == "https://git.example.com/org/repo.git"

    def test_strips_userinfo_with_port(self):
        url = "https://user:secretpass@git.example.com:8443/org/repo.git"
        result = _redact_url_credentials(url)
        assert "secretpass" not in result
        assert result == "https://git.example.com:8443/org/repo.git"

    def test_preserves_plain_url(self):
        url = "https://git.example.com/org/repo.git"
        assert _redact_url_credentials(url) == url

    def test_leaves_ssh_url_untouched(self):
        # scp-style SSH URLs have no scheme:// and carry no inline credentials.
        url = "git@git.example.com:org/repo.git"
        assert _redact_url_credentials(url) == url

    def test_handles_empty_and_none(self):
        assert _redact_url_credentials("") == ""
        assert _redact_url_credentials(None) is None

    def test_fails_closed_on_unparseable_url(self):
        # An out-of-range port makes urlparse(...).port raise; the helper must
        # still strip the credential (fail closed), never return it verbatim.
        url = "https://oauth2:glpat-FAKEtoken1234567890abcd@git.example.com:99999/repo.git"
        result = _redact_url_credentials(url)
        assert "glpat-" not in result
        assert "oauth2" not in result

    def test_banner_redacts_token(self, monkeypatch, capsys):
        """check_task_context must print the host but never the token."""
        monkeypatch.setenv(
            "LMER_REPO_URL",
            "https://oauth2:glpat-FAKEtoken1234567890abcd@git.example.com/org/repo.git",
        )
        for var in ("LMER_TASK_TARGET", "LMER_TASK", "LMER_TASKDEF"):
            monkeypatch.delenv(var, raising=False)
        check_task_context()
        out = capsys.readouterr().out
        assert "glpat-" not in out
        assert "oauth2" not in out
        assert "git.example.com" in out


class TestRunStateSessionStart:
    """run_state_session_start() — fail-soft subprocess wrapper."""

    def test_returns_brief_on_success(self, monkeypatch):
        from hooks import start as start_hook
        monkeypatch.setattr(start_hook.shutil, "which", lambda _: "/usr/bin/work")

        class FakeResult:
            returncode = 0
            stdout = "Run: develop-issue-123 (status: in-progress)\n"

        monkeypatch.setattr(
            start_hook.subprocess, "run", lambda *a, **k: FakeResult()
        )
        assert "develop-issue-123" in start_hook.run_state_session_start()

    def test_empty_when_work_missing(self, monkeypatch):
        from hooks import start as start_hook
        monkeypatch.setattr(start_hook.shutil, "which", lambda _: None)
        assert start_hook.run_state_session_start() == ""

    def test_empty_on_nonzero_exit(self, monkeypatch):
        from hooks import start as start_hook
        monkeypatch.setattr(start_hook.shutil, "which", lambda _: "/usr/bin/work")

        class FakeResult:
            returncode = 1
            stdout = ""

        monkeypatch.setattr(start_hook.subprocess, "run", lambda *a, **k: FakeResult())
        assert start_hook.run_state_session_start() == ""

    def test_empty_on_exception(self, monkeypatch):
        from hooks import start as start_hook
        monkeypatch.setattr(start_hook.shutil, "which", lambda _: "/usr/bin/work")

        def boom(*a, **k):
            raise OSError("no exec")

        monkeypatch.setattr(start_hook.subprocess, "run", boom)
        assert start_hook.run_state_session_start() == ""


class TestIncludeResolutionDefense:
    """Issue #80, observed live: LMER_TASKDEF_ROOT carried a host path that
    doesn't exist in the container, the is_dir() guard dropped it, and
    `{% include 'service-mode.jinja2' %}` hard-failed for a taskdef served
    from an external mount with an alternate work repo."""

    def test_shared_fragment_resolves_despite_bogus_taskdef_root(
        self, monkeypatch, tmp_path
    ):
        from hooks.start import render_taskdef_template

        # Taskdef served from an "external mount" containing no fragments.
        external = tmp_path / "taskdefs" / "0" / "chat"
        external.mkdir(parents=True)
        (external / "instructions.txt").write_text(
            "before\n{% include 'service-mode.jinja2' %}\nafter\n"
        )
        # The exact failure conditions from the live session:
        monkeypatch.setenv("LMER_TASKDEF_ROOT", "/home/nobody/Agents/global/taskdef")
        monkeypatch.delenv("LMER_TASKDEF_PATHS", raising=False)
        monkeypatch.delenv("LMER_WORK_REPO_PATH", raising=False)
        # builtin_taskdef_root() falls back to cwd/taskdef on hosts without
        # the container mounts — run from the repo root, where the real
        # built-in fragments live.
        monkeypatch.chdir(Path(__file__).parent.parent)

        out = render_taskdef_template(
            external / "instructions.txt", {"work_mode": "finish"}
        )
        assert "before" in out and "after" in out  # no TemplateNotFound

    def test_builtin_root_appended_to_search_dirs(self, monkeypatch, tmp_path):
        from hooks.start import builtin_taskdef_root, taskdef_search_dirs

        monkeypatch.chdir(tmp_path)
        assert taskdef_search_dirs()[-1] == builtin_taskdef_root()


@pytest.fixture
def _repo_builtin_root(monkeypatch):
    """Pin builtin_taskdef_root() to this checkout's taskdef/ so the tests
    exercise the base template under development, not a container mount."""
    monkeypatch.setattr(
        "hooks.start.builtin_taskdef_root", lambda: REPO_TASKDEF
    )
    monkeypatch.setenv("LMER_TASKDEF_ROOT", str(REPO_TASKDEF))


def _render(source_root, name="demo", extra=None):
    from hooks.start import render_taskdef_template

    context = {"work_mode": "finish", "run_state_brief": ""}
    context.update(extra or {})
    return render_taskdef_template(
        source_root / name / "instructions.txt", context
    )


class TestTaskdefSchemaVersioning:
    """taskdef.yaml manifests: supported-schema gate + source banner."""

    def test_schema2_body_extends_builtin_base(
        self, monkeypatch, _repo_builtin_root, capsys
    ):
        monkeypatch.setenv("LMER_TASKDEF_PATHS", str(FIXTURES / "schema2"))
        out = _render(FIXTURES / "schema2")
        assert out.startswith("# Demo Task")
        assert "## Phase 1: Demo work" in out
        # Inherited spine from base-task.jinja2:
        assert "## Phase -1: Branch Setup" in out
        assert "DO NOT be used outside tests" in out
        banner = capsys.readouterr().out
        assert (
            f"taskdef source: {FIXTURES / 'schema2'} (schema 2)" in banner
        )
        assert (
            f"taskdef source (base-task.jinja2): {REPO_TASKDEF} (schema 1)"
            in banner
        )

    def test_absent_manifest_is_schema1_legacy(
        self, monkeypatch, _repo_builtin_root, capsys
    ):
        monkeypatch.setenv("LMER_TASKDEF_PATHS", str(FIXTURES / "schema1"))
        out = _render(FIXTURES / "schema1")
        assert out.startswith("# Legacy Demo Task")
        assert "Work mode: finish" in out
        banner = capsys.readouterr().out
        assert (
            f"taskdef source: {FIXTURES / 'schema1'} (schema 1)" in banner
        )

    def test_unsupported_schema_fails_naming_source_and_supported_set(
        self, monkeypatch, _repo_builtin_root
    ):
        from hooks.start import TaskdefRenderError

        monkeypatch.setenv(
            "LMER_TASKDEF_PATHS", str(FIXTURES / "schema-unsupported")
        )
        with pytest.raises(TaskdefRenderError) as exc:
            _render(FIXTURES / "schema-unsupported")
        message = str(exc.value)
        assert str(FIXTURES / "schema-unsupported") in message
        assert "schema 99" in message
        assert "1, 2" in message

    def test_malformed_manifest_fails_loudly(
        self, monkeypatch, _repo_builtin_root, tmp_path
    ):
        from hooks.start import TaskdefRenderError

        (tmp_path / "taskdef.yaml").write_text("schema: not-an-int\n")
        taskdef = tmp_path / "demo"
        taskdef.mkdir()
        (taskdef / "instructions.txt").write_text("# X\n")
        monkeypatch.setenv("LMER_TASKDEF_PATHS", str(tmp_path))
        with pytest.raises(TaskdefRenderError) as exc:
            _render(tmp_path)
        assert "schema" in str(exc.value)

    def test_boolean_schema_fails_loudly(
        self, monkeypatch, _repo_builtin_root, tmp_path
    ):
        """`schema: true` is a YAML bool; bool-is-int must not let it render
        as schema 1 (the silent downgrade read_taskdef_schema promises can't
        happen)."""
        from hooks.start import TaskdefRenderError

        (tmp_path / "taskdef.yaml").write_text("schema: true\n")
        taskdef = tmp_path / "demo"
        taskdef.mkdir()
        (taskdef / "instructions.txt").write_text("# X\n")
        monkeypatch.setenv("LMER_TASKDEF_PATHS", str(tmp_path))
        with pytest.raises(TaskdefRenderError) as exc:
            _render(tmp_path)
        assert "integer `schema:`" in str(exc.value)

    def test_unsupported_schema_in_parent_tier_fails(
        self, monkeypatch, _repo_builtin_root, tmp_path
    ):
        """A tier that shadows base-task.jinja2 under an unsupported schema
        must fail the render, not just surface the schema in the banner —
        every consulted source root is gated, parents included."""
        import shutil

        from hooks.start import TaskdefRenderError

        shadow = tmp_path / "shadow-tier"
        shadow.mkdir()
        shutil.copy(REPO_TASKDEF / "base-task.jinja2", shadow)
        (shadow / "taskdef.yaml").write_text("schema: 99\n")
        monkeypatch.setenv(
            "LMER_TASKDEF_PATHS", f"{FIXTURES / 'schema2'}:{shadow}"
        )
        with pytest.raises(TaskdefRenderError) as exc:
            _render(FIXTURES / "schema2")
        message = str(exc.value)
        assert str(shadow) in message
        assert "schema 99" in message

    def test_manifest_in_unused_tier_is_never_consulted(
        self, monkeypatch, _repo_builtin_root, tmp_path
    ):
        """A stale/broken manifest in a tier the file did not resolve from
        must not affect the session (spec: only the resolved root's manifest
        is checked)."""
        stale = tmp_path / "stale-tier"
        stale.mkdir()
        (stale / "taskdef.yaml").write_text("schema: 99\n")
        monkeypatch.setenv(
            "LMER_TASKDEF_PATHS",
            f"{FIXTURES / 'schema2'}:{stale}",
        )
        out = _render(FIXTURES / "schema2")
        assert out.startswith("# Demo Task")

    def test_taskdef_dir_fastpath_reads_parent_manifest(
        self, monkeypatch, _repo_builtin_root, tmp_path
    ):
        """LMER_TASKDEF_DIR fast-path: the source root is the taskdef dir's
        parent, and its manifest governs (covers the CLI's pre-resolved
        path)."""
        from hooks.start import check_taskdef_schema

        (tmp_path / "taskdef.yaml").write_text("schema: 2\n")
        taskdef = tmp_path / "demo"
        taskdef.mkdir()
        (taskdef / "instructions.txt").write_text("# X\n")
        monkeypatch.delenv("LMER_TASKDEF_PATHS", raising=False)
        monkeypatch.setenv("LMER_TASKDEF_DIR", str(taskdef))
        root, schema = check_taskdef_schema(taskdef / "instructions.txt")
        assert root == tmp_path
        assert schema == 2


class TestBlockLint:
    """Render-time block lint over the template AST."""

    def test_unknown_toplevel_override_fails(
        self, monkeypatch, _repo_builtin_root
    ):
        from hooks.start import TaskdefRenderError

        monkeypatch.setenv("LMER_TASKDEF_PATHS", str(FIXTURES / "lint-bad"))
        with pytest.raises(TaskdefRenderError) as exc:
            _render(FIXTURES / "lint-bad")
        message = str(exc.value)
        assert "task_phasez" in message
        assert "base-task.jinja2" in message

    def test_new_block_nested_in_override_is_legal(
        self, monkeypatch, _repo_builtin_root, tmp_path
    ):
        (tmp_path / "taskdef.yaml").write_text("schema: 2\n")
        taskdef = tmp_path / "demo"
        taskdef.mkdir()
        (taskdef / "instructions.txt").write_text(
            "{% extends 'base-task.jinja2' %}\n"
            "{% block intro %}# N "
            "{% block brand_new_nested %}nested{% endblock %}"
            "{% endblock %}\n"
            "{% block task_phases %}## P{% endblock %}\n"
        )
        monkeypatch.setenv("LMER_TASKDEF_PATHS", str(tmp_path))
        out = _render(tmp_path)
        assert "nested" in out

    def test_template_without_extends_is_exempt(
        self, monkeypatch, _repo_builtin_root
    ):
        monkeypatch.setenv("LMER_TASKDEF_PATHS", str(FIXTURES / "schema1"))
        out = _render(FIXTURES / "schema1")
        assert out.startswith("# Legacy Demo Task")

    def test_read_and_display_fails_soft_with_message(
        self, monkeypatch, _repo_builtin_root, tmp_path
    ):
        """/start integration: a lint violation makes
        read_and_display_instructions return False and print the error."""
        monkeypatch.setenv("LMER_TASKDEF_PATHS", str(FIXTURES / "lint-bad"))
        with patch("hooks.start.Path.home", return_value=tmp_path):
            f = io.StringIO()
            with redirect_stdout(f):
                ok = read_and_display_instructions(
                    FIXTURES / "lint-bad" / "demo" / "instructions.txt",
                    "finish",
                )
        assert ok is False
        output = f.getvalue()
        assert "❌ ERROR" in output
        assert "task_phasez" in output
