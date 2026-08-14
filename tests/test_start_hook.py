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
    configured_taskdef_root,
    detect_shadowed_taskdefs,
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
        from lmer_cli.tokens import _is_github_host as canonical
        from hooks import start as start_hook
        from tests.conftest import ast_body_lines

        assert ast_body_lines(canonical) == ast_body_lines(start_hook._is_github_host)

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


class TestSupportedTaskdefSchemasMirror:
    """Guard against drift between hooks/start.py and the sources module.

    lmer_cli.container.sources surfaces SUPPORTED_TASKDEF_SCHEMAS in its
    `doctor` document as a module-local mirror of this file's constant:
    importing hooks/start.py from there is banned (start.py hard-imports
    yaml and jinja2 at module scope, which would break the sources module's
    import-cleanly-without-PyYAML contract). Same drift-guard idea as
    test_is_github_host_body_matches_canonical above — an edit to either
    constant must fail here until the other side is updated.
    """

    def test_sources_module_mirror_matches_canonical(self):
        from hooks.start import SUPPORTED_TASKDEF_SCHEMAS as canonical
        from lmer_cli.container.sources import (
            SUPPORTED_TASKDEF_SCHEMAS as mirrored,
        )

        assert mirrored == canonical


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


class TestTaskdefContextFilter:
    """Issue #285: the taskdef render context must not carry credentials.

    The fragment renderer (libexec/render-prompt-fragment.py) has filtered
    its context since it shipped; these tests hold the taskdef renderer to
    the same standard — name-matched keys dropped, credentialed URL values
    redacted in place so the tokenless URL still renders.
    """

    FAKE_CRED = "glpat-" + "FAKEtaskdef1234567890abcd"

    def _render(self, tmp_path, monkeypatch, template_text):
        monkeypatch.setenv("HOME", str(tmp_path))
        instructions_file = tmp_path / "instructions.txt"
        instructions_file.write_text(template_text)
        with patch('hooks.start.Path.home', return_value=tmp_path):
            f = io.StringIO()
            with redirect_stdout(f):
                assert read_and_display_instructions(instructions_file, "finish")
            return f.getvalue()

    def test_sensitive_named_var_dropped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LMER_FASTAPI_TOKEN", self.FAKE_CRED)
        output = self._render(
            tmp_path, monkeypatch,
            "[{{ LMER_FASTAPI_TOKEN | default('DROPPED') }}]\n",
        )
        assert "[DROPPED]" in output
        assert self.FAKE_CRED not in output

    def test_credentialed_url_renders_tokenless(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "LMER_REPO_URL",
            f"https://oauth2:{self.FAKE_CRED}@gitlab.example.com/org/repo.git",
        )
        output = self._render(tmp_path, monkeypatch, "URL: {{ LMER_REPO_URL }}\n")
        assert "URL: https://gitlab.example.com/org/repo.git" in output
        assert self.FAKE_CRED not in output
        assert "oauth2" not in output

    def test_credentialed_url_stays_truthy_in_conditionals(
        self, tmp_path, monkeypatch
    ):
        """Redact-in-place (not drop): {% if LMER_REPO_URL %} keeps firing."""
        monkeypatch.setenv(
            "LMER_REPO_URL",
            f"https://oauth2:{self.FAKE_CRED}@gitlab.example.com/org/repo.git",
        )
        output = self._render(
            tmp_path, monkeypatch,
            "{% if LMER_REPO_URL %}HAS_URL{% else %}NO_URL{% endif %}\n",
        )
        assert "HAS_URL" in output

    def test_value_shape_catches_unenumerated_vars(self, tmp_path, monkeypatch):
        """Any LMER_* URL value is caught, not a name allowlist (#285 comment:
        LMER_NAPKIN_REPO would slip past a name-by-name fix)."""
        monkeypatch.setenv(
            "LMER_NAPKIN_REPO",
            f"https://oauth2:{self.FAKE_CRED}@gitlab.example.com/org/napkin.git",
        )
        output = self._render(
            tmp_path, monkeypatch, "Napkin: {{ LMER_NAPKIN_REPO }}\n"
        )
        assert "Napkin: https://gitlab.example.com/org/napkin.git" in output
        assert self.FAKE_CRED not in output

    def test_plain_url_passes_through_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LMER_REPO_URL", "https://gitlab.example.com/org/repo.git")
        output = self._render(tmp_path, monkeypatch, "URL: {{ LMER_REPO_URL }}\n")
        assert "URL: https://gitlab.example.com/org/repo.git" in output

    def test_structured_value_secret_caught_by_backstop(
        self, tmp_path, monkeypatch
    ):
        """A token inside a structured value (the LMER_SPAWN_AGENTS_CONFIG
        shape) matches neither the name rule nor the URL-userinfo shape;
        the redact_secrets backstop must stamp it (MR !220 review)."""
        overlay = (
            '{"reviewer": {"env": {"GITLAB_TOKEN": "%s"}}}' % self.FAKE_CRED
        )
        monkeypatch.setenv("LMER_SPAWN_AGENTS_CONFIG", overlay)
        output = self._render(
            tmp_path, monkeypatch, "Overlay: {{ LMER_SPAWN_AGENTS_CONFIG }}\n"
        )
        assert self.FAKE_CRED not in output
        assert "***REDACTED***" in output
        # The rest of the structured value survives readable.
        assert '"reviewer"' in output

    def test_secret_named_hostname_var_does_not_poison_urls(
        self, tmp_path, monkeypatch
    ):
        """!220 iteration-2 blocker pin: LMER_GITLAB_TOKEN_HOST is
        secret-NAMED but holds a hostname; the backstop must not sweep that
        value out of every URL containing it. The var itself is dropped
        (name rule), the URLs render intact and tokenless."""
        monkeypatch.setenv("LMER_GITLAB_TOKEN_HOST", "gitlab.example.com")
        monkeypatch.setenv(
            "LMER_REPO_URL",
            f"https://oauth2:{self.FAKE_CRED}@gitlab.example.com/org/repo.git",
        )
        monkeypatch.setenv(
            "LMER_TASK_TARGET",
            "https://gitlab.example.com/org/repo/-/merge_requests/1",
        )
        output = self._render(
            tmp_path, monkeypatch,
            "URL: {{ LMER_REPO_URL }}\nTarget: {{ LMER_TASK_TARGET }}\n",
        )
        assert "URL: https://gitlab.example.com/org/repo.git" in output
        assert (
            "Target: https://gitlab.example.com/org/repo/-/merge_requests/1"
            in output
        )
        assert "***REDACTED***" not in output
        assert self.FAKE_CRED not in output

    def test_dropped_key_without_default_renders_empty(
        self, tmp_path, monkeypatch
    ):
        """Contract pin: a dropped key with no `| default` renders as an
        empty string (default Jinja Undefined), silently — not an error."""
        monkeypatch.setenv("LMER_FASTAPI_TOKEN", self.FAKE_CRED)
        output = self._render(
            tmp_path, monkeypatch, "[{{ LMER_FASTAPI_TOKEN }}]\n"
        )
        assert "[]" in output
        assert self.FAKE_CRED not in output


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
        # builtin_taskdef_root() probes fixed ambient paths (a developer's
        # /home/developer/.lmer — e.g. the #112 clone cache — satisfies the
        # first probe and yields a taskdef-less root) before falling back to
        # cwd/taskdef. Pin it to this checkout's taskdef/, where the real
        # built-in fragments live (same isolation as _repo_builtin_root; the
        # ambient search-chain behavior itself is issue #80's scope). The
        # defense under test is unchanged: include resolution must reach the
        # builtin root instead of trusting the bogus LMER_TASKDEF_ROOT.
        monkeypatch.setattr(
            "hooks.start.builtin_taskdef_root", lambda: REPO_TASKDEF
        )

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
        # This fixture does not override do_not_branch_rule, so it pins the
        # default path: emptying the block body base-side would silently drop
        # the branch-discipline HARD RULE from every inheriting taskdef.
        assert "always create a new branch first" in out
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


def _mk_taskdef(root, name):
    """Create <root>/<name>/instructions.txt (the marker of a taskdef)."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "instructions.txt").write_text("# x\n")
    return d


class TestShadowDetection:
    """detect_shadowed_taskdefs(): configured-taskdef-repo shadow warnings.

    The configured repo tier is identified as the LAST LMER_TASKDEF_PATHS
    entry, gated on LMER_TASKDEF_REPO being non-empty (cli.py appends the
    container clone path as the final PATHS entry when a repo is set).
    Detection is read-only and must stay silent for legacy sessions.
    """

    REPO_URL = "https://git.example.com/org/taskdefs.git"

    def _configure(self, monkeypatch, tmp_path, before=()):
        """Set up a configured taskdef repo root, optionally preceded by
        external PATHS entries, and return the root."""
        root = tmp_path / "taskdef-repo"
        root.mkdir(exist_ok=True)
        entries = [*before, root]
        monkeypatch.setenv("LMER_TASKDEF_REPO", self.REPO_URL)
        monkeypatch.setenv(
            "LMER_TASKDEF_PATHS", ":".join(str(p) for p in entries)
        )
        return root

    def _work_repo(self, monkeypatch, tmp_path, project=False):
        """Create a work repo with global (and optionally project) taskdef
        tiers; return (project_dir_or_None, global_dir)."""
        work = tmp_path / "work"
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))
        global_dir = work / "taskdef"
        global_dir.mkdir(parents=True)
        project_dir = None
        if project:
            monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
            monkeypatch.setenv("LMER_REPO_PROJECT", "group/proj")
            project_dir = work / "git.example.com" / "group/proj" / "taskdef"
            project_dir.mkdir(parents=True)
        return project_dir, global_dir

    # ── Configured-root identification ──────────────────────────────

    def test_unconfigured_returns_none_and_empty(self, monkeypatch, tmp_path):
        """Legacy sessions (no LMER_TASKDEF_REPO) get no detection at all,
        even when PATHS entries and work-repo tiers are present."""
        ext = tmp_path / "ext"
        _mk_taskdef(ext, "develop")
        monkeypatch.setenv("LMER_TASKDEF_PATHS", str(ext))
        self._work_repo(monkeypatch, tmp_path)
        assert configured_taskdef_root() is None
        assert detect_shadowed_taskdefs() == []

    def test_repo_configured_but_no_paths_is_silent(self, monkeypatch):
        monkeypatch.setenv("LMER_TASKDEF_REPO", self.REPO_URL)
        monkeypatch.delenv("LMER_TASKDEF_PATHS", raising=False)
        assert configured_taskdef_root() is None
        assert detect_shadowed_taskdefs() == []

    def test_root_is_last_paths_entry(self, monkeypatch, tmp_path):
        ext = tmp_path / "ext"
        ext.mkdir()
        root = self._configure(monkeypatch, tmp_path, before=[ext])
        assert configured_taskdef_root() == root

    # ── Each tier shadowing a name individually ─────────────────────

    def test_project_tier_shadow(self, monkeypatch, tmp_path):
        root = self._configure(monkeypatch, tmp_path)
        project_dir, _ = self._work_repo(monkeypatch, tmp_path, project=True)
        _mk_taskdef(root, "develop")
        _mk_taskdef(project_dir, "develop")
        assert detect_shadowed_taskdefs() == [
            ("develop", project_dir, "work-repo project tier")
        ]

    def test_global_tier_shadow(self, monkeypatch, tmp_path):
        root = self._configure(monkeypatch, tmp_path)
        _, global_dir = self._work_repo(monkeypatch, tmp_path)
        _mk_taskdef(root, "develop")
        _mk_taskdef(global_dir, "develop")
        assert detect_shadowed_taskdefs() == [
            ("develop", global_dir, "work-repo global tier")
        ]

    def test_earlier_paths_entry_shadow_label_includes_path(
        self, monkeypatch, tmp_path
    ):
        ext = tmp_path / "ext"
        root = self._configure(monkeypatch, tmp_path, before=[ext])
        _mk_taskdef(root, "develop")
        _mk_taskdef(ext, "develop")
        assert detect_shadowed_taskdefs() == [
            ("develop", ext, f"LMER_TASKDEF_PATHS entry {ext}")
        ]

    # ── Aggregation and precedence ───────────────────────────────────

    def test_multiple_shadowed_names_reported(self, monkeypatch, tmp_path):
        ext = tmp_path / "ext"
        root = self._configure(monkeypatch, tmp_path, before=[ext])
        _, global_dir = self._work_repo(monkeypatch, tmp_path)
        for name in ("develop", "review", "chat"):
            _mk_taskdef(root, name)
        _mk_taskdef(ext, "develop")
        _mk_taskdef(global_dir, "review")
        assert detect_shadowed_taskdefs() == [
            ("develop", ext, f"LMER_TASKDEF_PATHS entry {ext}"),
            ("review", global_dir, "work-repo global tier"),
        ]

    def test_name_only_in_configured_root_not_reported(
        self, monkeypatch, tmp_path
    ):
        root = self._configure(monkeypatch, tmp_path)
        _, global_dir = self._work_repo(monkeypatch, tmp_path)
        _mk_taskdef(root, "develop")
        _mk_taskdef(root, "unshadowed")
        _mk_taskdef(global_dir, "develop")
        result = detect_shadowed_taskdefs()
        assert [name for name, _, _ in result] == ["develop"]

    def test_project_tier_wins_over_global_and_paths_entry(
        self, monkeypatch, tmp_path
    ):
        """When several tiers shadow the same name, only the highest-
        precedence winner is reported (first match in search order)."""
        ext = tmp_path / "ext"
        root = self._configure(monkeypatch, tmp_path, before=[ext])
        project_dir, global_dir = self._work_repo(
            monkeypatch, tmp_path, project=True
        )
        _mk_taskdef(root, "develop")
        _mk_taskdef(ext, "develop")
        _mk_taskdef(global_dir, "develop")
        _mk_taskdef(project_dir, "develop")
        assert detect_shadowed_taskdefs() == [
            ("develop", project_dir, "work-repo project tier")
        ]

    def test_paths_entry_after_root_is_not_a_shadow(
        self, monkeypatch, tmp_path
    ):
        """Only entries PRECEDING the configured root count. With today's
        cli.py propagation the repo tier is always the last PATHS entry, so
        the root is passed explicitly here to pin the precede-only contract
        for any future identification change (e.g. sources.yaml naming the
        root directly)."""
        before = tmp_path / "before"
        after = tmp_path / "after"
        root = tmp_path / "taskdef-repo"
        root.mkdir()
        monkeypatch.setenv(
            "LMER_TASKDEF_PATHS", f"{before}:{root}:{after}"
        )
        _mk_taskdef(root, "develop")
        _mk_taskdef(root, "review")
        _mk_taskdef(after, "develop")   # after the root: never a shadow
        _mk_taskdef(before, "review")   # before the root: reported
        result = detect_shadowed_taskdefs(configured_root=root)
        assert result == [
            ("review", before, f"LMER_TASKDEF_PATHS entry {before}")
        ]
        assert all(d != after for _, d, _ in result)


class TestShadowWarningEmission:
    """/start end-to-end: shadow warnings in the session output.

    Contract (plan task 14): one ``⚠️  TASKDEF SHADOW: taskdef '<name>' in
    the configured taskdef repo is shadowed by <tier label>`` line per
    shadowed name, emitted after the 📍 Location line and before the
    ``taskdef source:`` banner — between the task-context block and the
    rendered instructions, where it cannot be missed. Advisory only:
    rendering is unaffected and legacy sessions (no LMER_TASKDEF_REPO)
    print zero new output.
    """

    REPO_URL = "https://git.example.com/org/taskdefs.git"

    def _run_start(self, tmp_path):
        """Run the full /start flow (main()) and return captured stdout."""
        with patch("hooks.start.Path.home", return_value=tmp_path), \
                patch("hooks.start.run_state_session_start", return_value=""), \
                patch("sys.argv", ["start.py"]):
            f = io.StringIO()
            with redirect_stdout(f):
                main()
        return f.getvalue()

    def _configure_repo(self, monkeypatch, tmp_path, before=()):
        """Configured taskdef repo carrying 'develop', active as LMER_TASK."""
        root = tmp_path / "taskdef-repo"
        root.mkdir(exist_ok=True)
        entries = [*before, root]
        monkeypatch.setenv("LMER_TASKDEF_REPO", self.REPO_URL)
        monkeypatch.setenv(
            "LMER_TASKDEF_PATHS", ":".join(str(p) for p in entries)
        )
        monkeypatch.setenv("LMER_TASK", "develop")
        _mk_taskdef(root, "develop")
        return root

    @staticmethod
    def _warning_lines(output):
        return [l for l in output.splitlines() if "TASKDEF SHADOW" in l]

    def test_project_tier_shadow_warns_with_placement(
        self, monkeypatch, tmp_path
    ):
        self._configure_repo(monkeypatch, tmp_path)
        work = tmp_path / "work"
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))
        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
        monkeypatch.setenv("LMER_REPO_PROJECT", "group/proj")
        project_dir = work / "git.example.com" / "group/proj" / "taskdef"
        _mk_taskdef(project_dir, "develop")

        out = self._run_start(tmp_path)
        assert self._warning_lines(out) == [
            "⚠️  TASKDEF SHADOW: taskdef 'develop' in the configured "
            "taskdef repo is shadowed by work-repo project tier"
        ]
        # Placement: after the Location line, before the source banner.
        assert (
            out.index("📍 Location:")
            < out.index("TASKDEF SHADOW")
            < out.index("taskdef source:")
        )
        # Advisory only: the shadowing copy still renders, /start succeeds.
        assert "✅ Task instructions loaded" in out

    def test_global_tier_shadow_warns(self, monkeypatch, tmp_path):
        self._configure_repo(monkeypatch, tmp_path)
        work = tmp_path / "work"
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))
        _mk_taskdef(work / "taskdef", "develop")

        out = self._run_start(tmp_path)
        assert self._warning_lines(out) == [
            "⚠️  TASKDEF SHADOW: taskdef 'develop' in the configured "
            "taskdef repo is shadowed by work-repo global tier"
        ]
        assert "✅ Task instructions loaded" in out

    def test_earlier_paths_entry_shadow_warns(self, monkeypatch, tmp_path):
        ext = tmp_path / "ext"
        self._configure_repo(monkeypatch, tmp_path, before=[ext])
        _mk_taskdef(ext, "develop")

        out = self._run_start(tmp_path)
        assert self._warning_lines(out) == [
            "⚠️  TASKDEF SHADOW: taskdef 'develop' in the configured "
            f"taskdef repo is shadowed by LMER_TASKDEF_PATHS entry {ext}"
        ]
        assert "✅ Task instructions loaded" in out

    def test_one_line_per_shadowed_name(self, monkeypatch, tmp_path):
        ext = tmp_path / "ext"
        root = self._configure_repo(monkeypatch, tmp_path, before=[ext])
        _mk_taskdef(root, "review")
        work = tmp_path / "work"
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))
        _mk_taskdef(ext, "develop")
        _mk_taskdef(work / "taskdef", "review")

        out = self._run_start(tmp_path)
        assert self._warning_lines(out) == [
            "⚠️  TASKDEF SHADOW: taskdef 'develop' in the configured "
            f"taskdef repo is shadowed by LMER_TASKDEF_PATHS entry {ext}",
            "⚠️  TASKDEF SHADOW: taskdef 'review' in the configured "
            "taskdef repo is shadowed by work-repo global tier",
        ]

    def test_configured_repo_without_shadows_prints_no_warnings(
        self, monkeypatch, tmp_path
    ):
        root = self._configure_repo(monkeypatch, tmp_path)
        _mk_taskdef(root, "review")
        work = tmp_path / "work"
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))
        _mk_taskdef(work / "taskdef", "unrelated")

        out = self._run_start(tmp_path)
        assert self._warning_lines(out) == []
        assert "✅ Task instructions loaded" in out

    def test_legacy_mode_prints_zero_new_output(self, monkeypatch, tmp_path):
        """Backward compat: with LMER_TASKDEF_REPO unset the /start output
        is byte-identical to a run with the warning path disabled — the
        feature adds ZERO output for legacy sessions, even with PATHS
        entries and a work-repo tier carrying overlapping names."""
        ext = tmp_path / "ext"
        _mk_taskdef(ext, "develop")
        monkeypatch.delenv("LMER_TASKDEF_REPO", raising=False)
        monkeypatch.setenv("LMER_TASKDEF_PATHS", str(ext))
        monkeypatch.setenv("LMER_TASK", "develop")
        work = tmp_path / "work"
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))
        _mk_taskdef(work / "taskdef", "develop")

        out = self._run_start(tmp_path)
        assert "TASKDEF SHADOW" not in out
        with patch(
            "hooks.start.taskdef_shadow_warning_lines", return_value=[]
        ):
            baseline = self._run_start(tmp_path)
        assert out == baseline


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
