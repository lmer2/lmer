"""Test environment variable display and redaction."""
import io
import re
from contextlib import redirect_stdout
from pathlib import Path

from lmer_cli.cli import _redact_env_value, _display_env_config_cli, _resolve_napkin_path

CLI_PY = Path(__file__).parent.parent / "src" / "lmer_cli" / "cli.py"


class TestResolveNapkinPath:
    """_resolve_napkin_path picks /napkin (separate) or a work-repo subdir."""

    def test_separate_repo_uses_slash_napkin(self):
        assert _resolve_napkin_path("https://oauth2:t@h/org/napkin.git", "/work") == "/napkin"

    def test_subdir_mode_uses_work_repo_subdir(self):
        assert _resolve_napkin_path("", "/work") == "/work/napkin"

    def test_subdir_mode_honors_custom_work_path(self):
        assert _resolve_napkin_path("", "/custom") == "/custom/napkin"


class TestCliEnvDictDeclaresNapkinTaskdef:
    """Source-level guard: the inline env dict in main() declares the new
    container-bound vars and seeds the raw token vars to None so the .env
    merge cannot forward them (env-var convention §4)."""

    def test_napkin_repo_forwarded(self):
        source = CLI_PY.read_text()
        assert re.search(r"""["']LMER_NAPKIN_REPO["']\s*:\s*napkin_repo_url""", source)

    def test_napkin_path_forwarded(self):
        source = CLI_PY.read_text()
        assert re.search(r"""["']LMER_NAPKIN_PATH["']\s*:\s*napkin_path""", source)

    def test_taskdef_repo_forwarded(self):
        source = CLI_PY.read_text()
        assert re.search(r"""["']LMER_TASKDEF_REPO["']\s*:\s*taskdef_repo_url""", source)

    def test_taskdef_ref_forwarded(self):
        source = CLI_PY.read_text()
        assert re.search(
            r"""["']LMER_TASKDEF_REF["']\s*:\s*os\.environ\.get\(\s*["']LMER_TASKDEF_REF["']\s*\)""",
            source,
        )

    def test_napkin_token_seeded_none(self):
        source = CLI_PY.read_text()
        assert re.search(r"""["']LMER_NAPKIN_TOKEN["']\s*:\s*None""", source)

    def test_taskdef_token_seeded_none(self):
        source = CLI_PY.read_text()
        assert re.search(r"""["']LMER_TASKDEF_TOKEN["']\s*:\s*None""", source)

    def test_token_vars_not_read_into_env_dict(self):
        """Raw token vars must not be passed through via os.environ.get in cli.py
        (they are consumed only inside tokens.py's
        _inject_gitlab_token_if_available via its dedicated_env arg)."""
        source = CLI_PY.read_text()
        assert not re.search(r"""os\.environ\.get\(\s*["']LMER_NAPKIN_TOKEN["']""", source)
        assert not re.search(r"""os\.environ\.get\(\s*["']LMER_TASKDEF_TOKEN["']""", source)


class TestRedactEnvValue:
    """Test CLI-side value redaction."""

    def test_redacts_token(self):
        assert _redact_env_value("GITLAB_TOKEN", "glpat-abc123") == "glpa***"

    def test_redacts_short_value(self):
        assert _redact_env_value("GITLAB_TOKEN", "ab") == "***"

    def test_preserves_normal_value(self):
        assert _redact_env_value("LMER_TASK", "review") == "review"

    def test_strips_url_credentials(self):
        url = "https://oauth2:token123@git.example.com/repo.git"
        result = _redact_env_value("LMER_REPO_URL", url)
        assert "token123" not in result
        # Exact match (not a substring check) so the host is preserved verbatim
        # and the credential is fully stripped.
        assert result == "https://git.example.com/repo.git"

    def test_preserves_plain_url(self):
        url = "https://git.example.com/repo.git"
        assert _redact_env_value("LMER_REPO_URL", url) == url

    def test_redacts_work_repo_url_token(self):
        # Regression for the cli.py work-repo URL log line: LMER_WORK_REPO does
        # not match the TOKEN/KEY/SECRET name regex, so it takes the URL branch
        # and the embedded oauth2:<token>@ credential is stripped, not logged.
        url = "https://oauth2:glpat-FAKEtoken1234567890abcd@git.example.com/org/repo.git"
        result = _redact_env_value("LMER_WORK_REPO", url)
        assert "glpat-" not in result
        assert result == "https://git.example.com/org/repo.git"

    def test_fails_closed_on_unparseable_url(self):
        # An out-of-range port makes urlparse(...).port raise; the redactor must
        # still strip the credential (fail closed) rather than return it verbatim.
        url = "https://oauth2:glpat-FAKEtoken1234567890abcd@git.example.com:99999/repo.git"
        result = _redact_env_value("LMER_REPO_URL", url)
        assert "glpat-" not in result
        assert "oauth2" not in result

    def test_redacts_key_vars(self):
        assert _redact_env_value("API_KEY", "sk-1234567890") == "sk-1***"

    def test_redacts_password_vars(self):
        assert _redact_env_value("DB_PASSWORD", "hunter2") == "hunt***"

    def test_redacts_secret_vars(self):
        assert _redact_env_value("MY_SECRET", "supersecret") == "supe***"

    def test_redacts_credentials_vars(self):
        assert _redact_env_value("LMER_CREDENTIALS", "mycreds123") == "mycr***"


class TestDisplayEnvConfigCli:
    """Test CLI-side env config display."""

    def test_shows_env_vars_with_sources(self, monkeypatch):
        monkeypatch.setenv("LMER_TASK", "review")
        monkeypatch.setenv("LMER_TRUST_MISE", "1")

        f = io.StringIO()
        with redirect_stdout(f):
            _display_env_config_cli(
                host_lmer_vars={"LMER_TASK"},
                env_file_sources={"LMER_TRUST_MISE": ".env (lmer state dir)"},
            )

        output = f.getvalue()
        assert "LMER_TASK" in output
        assert "environment" in output
        assert "LMER_TRUST_MISE" in output
        assert ".env (lmer state dir)" in output

    def test_table_has_fenced_delimiters(self, monkeypatch):
        monkeypatch.setenv("LMER_TASK", "review")

        f = io.StringIO()
        with redirect_stdout(f):
            _display_env_config_cli(set(), {})

        output = f.getvalue()
        assert output.startswith("---\n")
        assert output.rstrip().endswith("---")

    def test_redacts_sensitive_vars_in_table(self, monkeypatch):
        monkeypatch.setenv("LMER_SECRET_KEY", "super-secret-value-123")
        monkeypatch.setenv("LMER_TASK", "review")

        f = io.StringIO()
        with redirect_stdout(f):
            _display_env_config_cli(set(), {})

        output = f.getvalue()
        assert "LMER_SECRET_KEY" in output
        assert "super-secret-value-123" not in output
        assert "supe***" in output

    def test_no_output_when_no_lmer_vars(self, monkeypatch):
        for key in list(k for k in __import__('os').environ if k.startswith("LMER_")):
            monkeypatch.delenv(key, raising=False)

        f = io.StringIO()
        with redirect_stdout(f):
            _display_env_config_cli(set(), {})

        output = f.getvalue()
        assert "---" not in output

    def test_lmer_quick_gate_commit_listed_in_show_env(self, monkeypatch):
        """LMER_QUICK_GATE_COMMIT is picked up by --show-env via the LMER_ prefix scan."""
        monkeypatch.setenv("LMER_QUICK_GATE_COMMIT", "1")

        f = io.StringIO()
        with redirect_stdout(f):
            _display_env_config_cli(host_lmer_vars={"LMER_QUICK_GATE_COMMIT"}, env_file_sources={})

        output = f.getvalue()
        assert "LMER_QUICK_GATE_COMMIT" in output
        assert "1" in output

    def test_env_file_source_overrides_host(self, monkeypatch):
        """If a var was in host env but also from .env, source should show .env."""
        monkeypatch.setenv("LMER_TRUST_MISE", "1")

        f = io.StringIO()
        with redirect_stdout(f):
            _display_env_config_cli(
                host_lmer_vars={"LMER_TRUST_MISE"},
                env_file_sources={"LMER_TRUST_MISE": ".env (working directory)"},
            )

        output = f.getvalue()
        assert ".env (working directory)" in output
