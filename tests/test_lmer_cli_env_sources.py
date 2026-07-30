"""Test environment variable display and redaction."""
import io
import re
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

from lmer_cli.cli import (
    REHEARSAL_CREDENTIAL_ENVS,
    RELEASE_GITHUB_TOKEN_ENV,
    RELEASE_SIGNING_KEY_ENV,
    RELEASE_TASK_ID,
    _display_env_config_cli,
    _redact_env_value,
    _release_credential_env,
    _resolve_napkin_path,
)
from lmer_cli.mounts import CONTAINER_RELEASE_SIGNING_KEY_PATH
from lmer_cli.runtime import _is_selinux_enforcing

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


class TestReleaseCredentialEnv:
    """The release credential scoping gate (_release_credential_env):
    production credentials (fine-grained GitHub PAT + release SSH signing
    key) reach release-taskdef sessions ONLY; the rig-scoped rehearsal
    credentials reach every session, env-borne only (spec §5, F5/F6)."""

    @pytest.fixture()
    def fake_home(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        return home

    @pytest.fixture(autouse=True)
    def _clean_release_env(self, monkeypatch):
        for key in (RELEASE_GITHUB_TOKEN_ENV, RELEASE_SIGNING_KEY_ENV, *REHEARSAL_CREDENTIAL_ENVS):
            monkeypatch.delenv(key, raising=False)

    # --- positive path: release-taskdef session ---

    def test_release_session_forwards_pat(self, monkeypatch):
        monkeypatch.setenv(RELEASE_GITHUB_TOKEN_ENV, "github_pat_fixture")
        entries, mounts, fatal = _release_credential_env("docker", RELEASE_TASK_ID)
        assert fatal is None
        assert entries[RELEASE_GITHUB_TOKEN_ENV] == "github_pat_fixture"
        assert mounts == []

    def test_release_session_mounts_signing_key(self, monkeypatch, fake_home):
        key = fake_home / ".ssh" / "lmer_release_key"
        key.parent.mkdir()
        key.write_text("PRIVATE KEY")
        monkeypatch.setenv(RELEASE_SIGNING_KEY_ENV, str(key))
        with patch("pathlib.Path.home", return_value=fake_home):
            with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
                _is_selinux_enforcing.cache_clear()
                entries, mounts, fatal = _release_credential_env(
                    "docker", RELEASE_TASK_ID
                )
        assert fatal is None
        assert mounts == ["-v", f"{key}:{CONTAINER_RELEASE_SIGNING_KEY_PATH}:ro"]
        # The container env carries the REMAPPED path — never the host path.
        assert entries[RELEASE_SIGNING_KEY_ENV] == CONTAINER_RELEASE_SIGNING_KEY_PATH

    def test_release_session_rejected_key_is_fatal(self, monkeypatch, fake_home):
        monkeypatch.setenv(RELEASE_SIGNING_KEY_ENV, str(fake_home / "missing_key"))
        with patch("pathlib.Path.home", return_value=fake_home):
            entries, mounts, fatal = _release_credential_env("docker", RELEASE_TASK_ID)
        assert fatal is not None and "does not exist" in fatal
        assert mounts == []
        assert entries[RELEASE_SIGNING_KEY_ENV] is None

    def test_release_session_unconfigured_credentials_not_fatal(self):
        """Leg-1 release work (bump, MR) needs neither credential — absence
        is announced elsewhere, never fatal."""
        entries, mounts, fatal = _release_credential_env("docker", RELEASE_TASK_ID)
        assert fatal is None
        assert mounts == []
        assert entries[RELEASE_GITHUB_TOKEN_ENV] is None
        assert entries[RELEASE_SIGNING_KEY_ENV] is None

    # --- negative path: any other session (the wave-2 module extends these) ---

    @pytest.mark.parametrize("task_id", [None, "masterplan", "review", "chat"])
    def test_non_release_session_seeds_production_none(
        self, monkeypatch, fake_home, task_id
    ):
        """Even with BOTH production credentials configured on the host, a
        non-release session gets None seeds and no mount."""
        key = fake_home / "release_key"
        key.write_text("PRIVATE KEY")
        monkeypatch.setenv(RELEASE_GITHUB_TOKEN_ENV, "github_pat_fixture")
        monkeypatch.setenv(RELEASE_SIGNING_KEY_ENV, str(key))
        with patch("pathlib.Path.home", return_value=fake_home):
            entries, mounts, fatal = _release_credential_env("docker", task_id)
        assert fatal is None
        assert mounts == []
        assert entries[RELEASE_GITHUB_TOKEN_ENV] is None
        assert entries[RELEASE_SIGNING_KEY_ENV] is None

    @pytest.mark.parametrize("task_id", [None, "masterplan", "review", RELEASE_TASK_ID])
    def test_production_keys_always_seeded(self, task_id):
        """Both production keys are ALWAYS present in the entries dict —
        this is the leak blocker: the .env merge and the preset seeding
        loop in main() both skip keys already in the env dict
        (`key not in env` is False even for a None value)."""
        entries, _, _ = _release_credential_env("docker", task_id)
        assert RELEASE_GITHUB_TOKEN_ENV in entries
        assert RELEASE_SIGNING_KEY_ENV in entries

    # --- rig-scoped rehearsal credentials (R3/F5/F6) ---

    def test_non_release_session_receives_rig_vars(self, monkeypatch):
        """POSITIVE: a non-release (masterplan/rehearsal) session DOES
        receive the rig-scoped rehearsal variables."""
        monkeypatch.setenv("LMER_REHEARSAL_GITHUB_TOKEN", "github_pat_rig")
        monkeypatch.setenv("LMER_REHEARSAL_TESTPYPI_TOKEN", "pypi-rig-token")
        monkeypatch.setenv("LMER_REHEARSAL_SIGNING_KEY", "/home/u/.ssh/rig_key")
        entries, mounts, fatal = _release_credential_env("docker", "masterplan")
        assert fatal is None
        assert entries["LMER_REHEARSAL_GITHUB_TOKEN"] == "github_pat_rig"
        assert entries["LMER_REHEARSAL_TESTPYPI_TOKEN"] == "pypi-rig-token"
        assert entries["LMER_REHEARSAL_SIGNING_KEY"] == "/home/u/.ssh/rig_key"
        # F5/F6: rig provisioning is env-vars ONLY — the rehearsal signing
        # key is never delivered through the production key mount builder.
        assert mounts == []

    def test_release_session_also_receives_rig_vars(self, monkeypatch):
        monkeypatch.setenv("LMER_REHEARSAL_GITHUB_TOKEN", "github_pat_rig")
        entries, _, _ = _release_credential_env("docker", RELEASE_TASK_ID)
        assert entries["LMER_REHEARSAL_GITHUB_TOKEN"] == "github_pat_rig"


class TestCliEnvDictDeclaresReleaseCredentials:
    """Source-level guard (env-var convention §4, the
    test_cli_env_dict_declares_reasoning_effort pattern): main()'s env dict
    spreads the gate's entries so the production credentials are always
    seeded (blocking the .env merge and preset seeding leak paths) and the
    rehearsal variables are declared passthrough."""

    def test_release_entries_spread_into_env_dict(self):
        source = CLI_PY.read_text()
        assert re.search(r"\*\*release_env_entries", source), (
            "main()'s container env dict must spread release_env_entries — "
            "without the None seeds a PAT in ~/.lmer/.env or a preset would "
            "be forwarded into every session"
        )

    def test_production_names_frozen(self):
        """Later release-flow tasks cite these exact names."""
        assert RELEASE_GITHUB_TOKEN_ENV == "LMER_RELEASE_GITHUB_TOKEN"
        assert RELEASE_SIGNING_KEY_ENV == "LMER_RELEASE_SIGNING_KEY"

    def test_rehearsal_names_frozen(self):
        """Names frozen in Ctl/rehearsal/README.md (credential isolation)."""
        assert REHEARSAL_CREDENTIAL_ENVS == (
            "LMER_REHEARSAL_GITHUB_TOKEN",
            "LMER_REHEARSAL_TESTPYPI_TOKEN",
            "LMER_REHEARSAL_SIGNING_KEY",
        )

    def test_release_task_id_frozen(self):
        assert RELEASE_TASK_ID == "release"

    def test_production_vars_not_declared_as_dict_literals(self):
        """The production credential keys must reach the env dict ONLY via
        the gate's entries — never as literal passthrough entries that
        would bypass the task-id scoping."""
        source = CLI_PY.read_text()
        assert not re.search(r"""["']LMER_RELEASE_GITHUB_TOKEN["']\s*:""", source)
        assert not re.search(r"""["']LMER_RELEASE_SIGNING_KEY["']\s*:""", source)

    def test_gate_fatal_aborts_launch(self):
        """The gate's fatal reason must abort main() before launch (a
        release session that cannot sign must not start)."""
        source = CLI_PY.read_text()
        assert re.search(r"if release_fatal:\s*\n\s*error\(.*\)\s*\n\s*return 1", source)


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
