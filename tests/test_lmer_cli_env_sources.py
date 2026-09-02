"""Test environment variable display and redaction."""
import io
import re
import subprocess
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

from lmer_cli import cli, clone_cache
from lmer_cli.cli import (
    RELEASE_GITHUB_TOKEN_ENV,
    RELEASE_SIGNING_KEY_ENV,
    RELEASE_TASK_ID,
    _display_env_config_cli,
    _display_sources_config_cli,
    _redact_env_value,
    _release_credential_env,
    _resolve_napkin_path,
)
from lmer_cli.mounts import CONTAINER_RELEASE_SIGNING_KEY_PATH
from lmer_cli.runtime import _is_selinux_enforcing

from tests.conftest import strip_lmer_env

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
    key) reach release-taskdef sessions ONLY (spec §5)."""

    @pytest.fixture()
    def fake_home(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        return home

    @pytest.fixture(autouse=True)
    def _clean_release_env(self, monkeypatch):
        for key in (RELEASE_GITHUB_TOKEN_ENV, RELEASE_SIGNING_KEY_ENV):
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


class TestCliEnvDictDeclaresReleaseCredentials:
    """Source-level guard (env-var convention §4, the
    test_cli_env_dict_declares_reasoning_effort pattern): main()'s env dict
    spreads the gate's entries so the production credentials are always
    seeded (blocking the .env merge and preset seeding leak paths)."""

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


WORK_REPO = "https://git.example.com/org/work.git"

VALID_SOURCES_YAML = """\
schema: 1
sources:
  taskdef:
    repo: https://git.example.com/org/taskdefs.git
    ref: v2
  napkin:
    repo: https://git.example.com/org/napkin.git
"""

CROSS_HOST_SOURCES_YAML = """\
schema: 1
sources:
  taskdef:
    repo: https://elsewhere.example.net/org/taskdefs.git
"""

CREDENTIALED_SOURCES_YAML = """\
schema: 1
sources:
  taskdef:
    repo: https://oauth2:glpat-FAKEdeclared99@git.example.com/org/taskdefs.git
"""


def _render_sources_block(monkeypatch, cached, env=None,
                          reason=clone_cache.CACHE_NO_MIRROR):
    """Render the block with a stubbed clone cache and a clean LMER_* env.

    `cached` is what the clone-cache reader pretends the work repo's cached
    sources.yaml contains (None = miss, reported with `reason`); `env` is the
    complete LMER_* environment for the render (everything else is stripped
    first).
    """
    strip_lmer_env(monkeypatch)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        cli,
        "read_cached_repo_file_status",
        lambda url, path="sources.yaml": (
            (cached, clone_cache.CACHE_HIT) if cached is not None else (None, reason)
        ),
    )
    f = io.StringIO()
    with redirect_stdout(f):
        _display_sources_config_cli()
    return f.getvalue()


def _sources_row(output, label):
    """The (value, origin) cells of the rendered table row named *label*."""
    for line in output.splitlines():
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) == 3 and parts[0] == label:
            return parts[1], parts[2]
    raise AssertionError(f"no `{label}` row in output:\n{output}")


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class TestDisplaySourcesConfigCli:
    """Canonical-sources block for --show-env (plan task 11, issue #105)."""

    def _render(self, monkeypatch, cached, env=None,
                reason=clone_cache.CACHE_NO_MIRROR):
        """Render the block with a stubbed clone cache and a clean LMER_* env.

        `cached` is what the clone-cache reader pretends the work repo's
        cached sources.yaml contains (None = miss, reported with `reason`);
        `env` is the LMER_* environment for the render.
        """
        return _render_sources_block(monkeypatch, cached, env, reason=reason)

    def test_declared_from_cache_labeled_possibly_stale(self, monkeypatch):
        output = self._render(
            monkeypatch, VALID_SOURCES_YAML, env={"LMER_WORK_REPO": WORK_REPO}
        )
        assert "Canonical Sources" in output
        assert "https://git.example.com/org/taskdefs.git" in output
        assert "https://git.example.com/org/napkin.git" in output
        assert "v2" in output
        # Every declared row is labeled as coming from a possibly-stale cache.
        assert output.count("declared (cached, possibly stale)") == 3

    def test_env_override_detected_when_urls_differ(self, monkeypatch):
        output = self._render(
            monkeypatch,
            VALID_SOURCES_YAML,
            env={
                "LMER_WORK_REPO": WORK_REPO,
                "LMER_TASKDEF_REPO": "https://git.example.com/other/taskdefs.git",
            },
        )
        assert "env-override" in output
        assert "https://git.example.com/other/taskdefs.git" in output

    def test_normalizer_prevents_false_env_override(self, monkeypatch):
        """An env var spelling the SAME repo differently (scp form, no .git)
        is a silent match by the core normalizer, not an env-override."""
        output = self._render(
            monkeypatch,
            VALID_SOURCES_YAML,
            env={
                "LMER_WORK_REPO": WORK_REPO,
                "LMER_TASKDEF_REPO": "git@git.example.com:org/taskdefs",
            },
        )
        assert "env-override" not in output
        assert "env-match" in output

    def test_load_warnings_are_rendered(self, monkeypatch):
        """Iteration-4 review finding: --show-env dropped load_sources'
        recoverable-findings channel, so a declaration with a typo'd key
        rendered as a clean `declared:` line on the one surface an operator
        uses to understand it — while bin/doctor and container startup both
        warn. The temp copy's path must not leak into the message either.
        """
        output = self._render(
            monkeypatch,
            "schema: 1\n"
            "profile: default\n"
            "sources:\n"
            "  taskdef:\n"
            "    repo: https://git.example.com/org/taskdefs.git\n"
            "    branch: main\n"
            "  masterplan_mirror:\n"
            "    repo: https://git.example.com/org/mirror.git\n",
            env={"LMER_WORK_REPO": WORK_REPO},
        )
        assert "unknown top-level key `profile`" in output
        assert "unknown key `branch`" in output
        assert "`sources.masterplan_mirror` is reserved" in output
        # Named by the real filename, never the throwaway temp path.
        assert "sources.yaml:" in output
        assert "/tmp" not in output
        # The valid part of the declaration still renders.
        assert "https://git.example.com/org/taskdefs.git" in output

    def test_no_warnings_no_warning_lines(self, monkeypatch):
        output = self._render(
            monkeypatch, VALID_SOURCES_YAML, env={"LMER_WORK_REPO": WORK_REPO}
        )
        assert "⚠️" not in output

    def test_env_match_across_a_custom_ssh_port(self, monkeypatch):
        """Host/container parity for the sixth iteration-5 finding: the env
        value that is the DERIVED form of the declaration is env-match on
        both surfaces, not env-override.
        """
        output = self._render(
            monkeypatch,
            VALID_SOURCES_YAML,
            env={
                "LMER_WORK_REPO": "ssh://git@git.example.com:2222/org/work.git",
                "LMER_TASKDEF_REPO": "ssh://git@git.example.com:2222/org/taskdefs.git",
            },
        )
        assert "env-match" in output
        assert "env-override" not in output

    def test_env_only_when_nothing_declared(self, monkeypatch):
        output = self._render(
            monkeypatch,
            None,  # cold cache: no declared side at all
            env={
                "LMER_WORK_REPO": WORK_REPO,
                "LMER_NAPKIN_REPO": "https://git.example.com/org/napkin.git",
            },
        )
        assert "declared: unknown (work repo not cached)" in output
        assert "env-only" in output
        assert "env-override" not in output

    def test_warm_mirror_without_sources_yaml_is_not_a_cache_miss(self, monkeypatch):
        """A warm mirror whose repo declares nothing is the expected state
        until the cutover — it must not be reported as an uncached work repo,
        which sends the operator debugging clone-cache state that is fine."""
        output = self._render(
            monkeypatch,
            None,
            env={"LMER_WORK_REPO": WORK_REPO},
            reason=clone_cache.CACHE_FILE_ABSENT,
        )
        assert "declared: none (no sources.yaml in the work repo)" in output
        assert "not cached" not in output
        assert output.count("unset-fallback") == 3

    def test_unreadable_cache_is_reported_as_such(self, monkeypatch):
        """A reader that blew up is neither a declaration nor a cold cache."""
        output = self._render(
            monkeypatch,
            None,
            env={"LMER_WORK_REPO": WORK_REPO},
            reason=clone_cache.CACHE_ERROR,
        )
        assert "declared: unknown (work-repo cache unreadable)" in output
        assert output.count("unset-fallback") == 3

    def test_unset_fallback_row_is_explicit(self, monkeypatch):
        """Decision (b): unset sources print a loud row, never omitted."""
        output = self._render(
            monkeypatch, None, env={"LMER_WORK_REPO": WORK_REPO}
        )
        assert "taskdef repo" in output
        assert "taskdef ref" in output
        assert "napkin repo" in output
        assert output.count("unset-fallback") == 3

    def test_invalid_cached_sources_yaml_surfaced_not_raised(self, monkeypatch):
        """Decision (c): a bad cached sources.yaml renders as invalid; the
        renderer never raises and rows fall back to env-only/unset."""
        output = self._render(
            monkeypatch,
            "schema: 99\nsources: {}\n",
            env={"LMER_WORK_REPO": WORK_REPO},
        )
        assert "declared: invalid (" in output
        # The throwaway temp path is stripped so the message names the file.
        assert "sources.yaml declares schema 99" in output
        assert "unset-fallback" in output

    def test_no_credential_in_any_rendered_row(self, monkeypatch):
        """Tokened env URLs (https userinfo and scp-form) never reach stdout."""
        output = self._render(
            monkeypatch,
            VALID_SOURCES_YAML,
            env={
                "LMER_WORK_REPO": "https://oauth2:glpat-FAKEwork1234@git.example.com/org/work.git",
                "LMER_TASKDEF_REPO": "https://oauth2:glpat-FAKEtask1234@git.example.com/other/taskdefs.git",
                "LMER_NAPKIN_REPO": "oauth2:glpat-FAKEnapkin12@git.example.com:other2/napkin",
            },
        )
        assert "glpat-" not in output
        assert "oauth2" not in output


class TestSourcesOriginMatrix:
    """Remaining cells of the per-field origin matrix (plan task 12): every
    field reaches every origin, not just the Wave-2 samples (declared-only
    for all three, env-match/env-override on taskdef repo, env-only on
    napkin repo, unset-fallback for all three)."""

    def test_taskdef_ref_env_matching_declaration_is_not_an_override(self, monkeypatch):
        output = _render_sources_block(
            monkeypatch,
            VALID_SOURCES_YAML,
            env={"LMER_WORK_REPO": WORK_REPO, "LMER_TASKDEF_REF": "v2"},
        )
        assert _sources_row(output, "taskdef ref") == (
            "v2",
            "env-match (declaration cached, possibly stale)",
        )
        assert "env-override" not in output

    def test_taskdef_ref_env_override_shows_env_value(self, monkeypatch):
        output = _render_sources_block(
            monkeypatch,
            VALID_SOURCES_YAML,
            env={"LMER_WORK_REPO": WORK_REPO, "LMER_TASKDEF_REF": "v9"},
        )
        assert _sources_row(output, "taskdef ref") == ("v9", "env-override")

    def test_taskdef_ref_comparison_is_exact_not_normalized(self, monkeypatch):
        """Refs are opaque strings: only repo fields go through the URL
        normalizer, so a ref differing only in case IS an override."""
        output = _render_sources_block(
            monkeypatch,
            VALID_SOURCES_YAML,
            env={"LMER_WORK_REPO": WORK_REPO, "LMER_TASKDEF_REF": "V2"},
        )
        assert _sources_row(output, "taskdef ref") == ("V2", "env-override")

    def test_napkin_repo_env_matching_via_normalizer(self, monkeypatch):
        output = _render_sources_block(
            monkeypatch,
            VALID_SOURCES_YAML,
            env={
                "LMER_WORK_REPO": WORK_REPO,
                "LMER_NAPKIN_REPO": "git@git.example.com:org/napkin",
            },
        )
        # The declared spelling renders, labeled as a silent match.
        assert _sources_row(output, "napkin repo") == (
            "https://git.example.com/org/napkin.git",
            "env-match (declaration cached, possibly stale)",
        )

    def test_napkin_repo_env_override_shows_env_value(self, monkeypatch):
        output = _render_sources_block(
            monkeypatch,
            VALID_SOURCES_YAML,
            env={
                "LMER_WORK_REPO": WORK_REPO,
                "LMER_NAPKIN_REPO": "https://git.example.com/other/napkin.git",
            },
        )
        assert _sources_row(output, "napkin repo") == (
            "https://git.example.com/other/napkin.git",
            "env-override",
        )

    def test_taskdef_repo_env_only_without_declaration(self, monkeypatch):
        output = _render_sources_block(
            monkeypatch,
            None,  # cache miss
            env={
                "LMER_WORK_REPO": WORK_REPO,
                "LMER_TASKDEF_REPO": "https://git.example.com/org/taskdefs.git",
            },
        )
        assert _sources_row(output, "taskdef repo") == (
            "https://git.example.com/org/taskdefs.git",
            "env-only",
        )

    def test_taskdef_ref_env_only_without_declaration(self, monkeypatch):
        output = _render_sources_block(
            monkeypatch,
            None,
            env={"LMER_WORK_REPO": WORK_REPO, "LMER_TASKDEF_REF": "v9"},
        )
        assert _sources_row(output, "taskdef ref") == ("v9", "env-only")
        # The untouched rows stay loud unset-fallback rows (decision (b)).
        assert _sources_row(output, "taskdef repo") == ("(unset)", "unset-fallback")
        assert _sources_row(output, "napkin repo") == ("(unset)", "unset-fallback")

    def test_one_override_leaves_other_rows_declared(self, monkeypatch):
        """Rows are independent: overriding napkin never bleeds an
        env-override label onto the taskdef rows."""
        output = _render_sources_block(
            monkeypatch,
            VALID_SOURCES_YAML,
            env={
                "LMER_WORK_REPO": WORK_REPO,
                "LMER_NAPKIN_REPO": "https://git.example.com/other/napkin.git",
            },
        )
        assert _sources_row(output, "taskdef repo") == (
            "https://git.example.com/org/taskdefs.git",
            "declared (cached, possibly stale)",
        )
        assert _sources_row(output, "taskdef ref") == (
            "v2",
            "declared (cached, possibly stale)",
        )

    def test_whitespace_only_env_value_is_unset(self, monkeypatch):
        output = _render_sources_block(
            monkeypatch,
            None,
            env={"LMER_WORK_REPO": WORK_REPO, "LMER_TASKDEF_REF": "   "},
        )
        assert _sources_row(output, "taskdef ref") == ("(unset)", "unset-fallback")

    def test_work_repo_unset_renders_without_touching_cache(self, monkeypatch):
        """No LMER_WORK_REPO: the block still renders (never raises) and the
        cache reader is never consulted — there is no URL to map."""
        strip_lmer_env(monkeypatch)

        def _boom(url, path="sources.yaml"):
            raise AssertionError("cache read without LMER_WORK_REPO")

        monkeypatch.setattr(cli, "read_cached_repo_file_status", _boom)
        f = io.StringIO()
        with redirect_stdout(f):
            _display_sources_config_cli()
        output = f.getvalue()
        assert "declared: unknown (LMER_WORK_REPO not set)" in output
        assert output.count("unset-fallback") == 3


class TestSourcesBlockInvalidCachedYaml:
    """More decision-(c) shapes beyond Wave 2's unsupported-schema case: any
    untrustable cached sources.yaml becomes a non-fatal `declared: invalid`
    notice and the rows fall back — never a raise, never a traceback."""

    def test_unparseable_yaml_renders_notice(self, monkeypatch):
        output = _render_sources_block(
            monkeypatch, "sources: [unclosed\n", env={"LMER_WORK_REPO": WORK_REPO}
        )
        assert "declared: invalid (" in output
        # The throwaway temp path is stripped so the notice names the file.
        assert "unparseable sources.yaml" in output
        assert output.count("unset-fallback") == 3

    def test_non_mapping_yaml_renders_notice(self, monkeypatch):
        output = _render_sources_block(
            monkeypatch, "- taskdef\n- napkin\n", env={"LMER_WORK_REPO": WORK_REPO}
        )
        assert "declared: invalid (" in output
        assert "must be a YAML mapping" in output
        assert output.count("unset-fallback") == 3

    def test_trust_rule_violation_renders_notice(self, monkeypatch):
        """A cached file declaring a cross-host repo fails load_sources'
        schema-1 trust rule; the block reports it and keeps rendering."""
        output = _render_sources_block(
            monkeypatch, CROSS_HOST_SOURCES_YAML, env={"LMER_WORK_REPO": WORK_REPO}
        )
        assert "declared: invalid (" in output
        assert "schema-1 trust rule" in output
        assert output.count("unset-fallback") == 3

    def test_invalid_declaration_still_honors_env_rows(self, monkeypatch):
        """An invalid cache never suppresses env-side configuration."""
        output = _render_sources_block(
            monkeypatch,
            CROSS_HOST_SOURCES_YAML,
            env={
                "LMER_WORK_REPO": WORK_REPO,
                "LMER_TASKDEF_REPO": "https://git.example.com/org/taskdefs.git",
            },
        )
        assert _sources_row(output, "taskdef repo") == (
            "https://git.example.com/org/taskdefs.git",
            "env-only",
        )

    def test_credentialed_declared_url_never_reaches_stdout(self, monkeypatch):
        """Declared-side scrubbing: load_sources refuses a credentialed
        repo URL, and the rendered refusal itself is scrubbed."""
        output = _render_sources_block(
            monkeypatch, CREDENTIALED_SOURCES_YAML, env={"LMER_WORK_REPO": WORK_REPO}
        )
        assert "declared: invalid (" in output
        assert "embeds a credential" in output
        assert "glpat-" not in output
        assert "oauth2" not in output


class TestSourcesBlockRealCloneCache:
    """Declared side end-to-end against a REAL bare mirror under a temp
    clone-cache root — no read_cached_repo_file stub: URL→mirror mapping,
    `git show`, parse, validate, render."""

    def _mirror_work_repo(self, tmp_path, sources_yaml):
        """A bare mirror for WORK_REPO with *sources_yaml* at HEAD, built at
        the real mapping's location under a fresh cache root.

        `sources_yaml=None` builds a perfectly good mirror of a work repo
        that simply declares nothing — the state every work repo is in until
        the cutover lands.
        """
        seed = tmp_path / "seed"
        seed.mkdir()
        _git("init", "-b", "main", cwd=seed)
        _git("config", "user.email", "test@example.com", cwd=seed)
        _git("config", "user.name", "Test User", cwd=seed)
        name = "sources.yaml" if sources_yaml is not None else "README.md"
        (seed / name).write_text(sources_yaml if sources_yaml is not None else "work\n")
        _git("add", name, cwd=seed)
        _git("commit", "-m", "declare sources", cwd=seed)
        cache_root = tmp_path / "clone-cache"
        mirror = cache_root / "git.example.com" / "org" / "work.git"
        mirror.parent.mkdir(parents=True)
        _git("clone", "--bare", "--quiet", str(seed), str(mirror), cwd=tmp_path)
        return cache_root

    def _render(self, monkeypatch, cache_root, work_repo=WORK_REPO):
        strip_lmer_env(monkeypatch)
        monkeypatch.setenv("LMER_CLONE_CACHE_DIR", str(cache_root))
        monkeypatch.setenv("LMER_WORK_REPO", work_repo)
        f = io.StringIO()
        with redirect_stdout(f):
            _display_sources_config_cli()
        return f.getvalue()

    def test_real_mirror_rows_labeled_cached_possibly_stale(self, monkeypatch, tmp_path):
        cache_root = self._mirror_work_repo(tmp_path, VALID_SOURCES_YAML)
        output = self._render(monkeypatch, cache_root)
        assert (
            "declared: sources.yaml from work-repo cache (cached, possibly stale)"
            in output
        )
        assert _sources_row(output, "taskdef repo") == (
            "https://git.example.com/org/taskdefs.git",
            "declared (cached, possibly stale)",
        )
        assert _sources_row(output, "taskdef ref") == (
            "v2",
            "declared (cached, possibly stale)",
        )
        assert _sources_row(output, "napkin repo") == (
            "https://git.example.com/org/napkin.git",
            "declared (cached, possibly stale)",
        )

    def test_tokenized_work_repo_url_finds_same_mirror_and_scrubs(
        self, monkeypatch, tmp_path
    ):
        """The URL→mirror mapping scrubs userinfo, so a credentialed
        LMER_WORK_REPO still hits the mirror — and never reaches stdout."""
        cache_root = self._mirror_work_repo(tmp_path, VALID_SOURCES_YAML)
        output = self._render(
            monkeypatch,
            cache_root,
            work_repo="https://oauth2:glpat-FAKEwork5678@git.example.com/org/work.git",
        )
        assert output.count("declared (cached, possibly stale)") == 3
        assert "glpat-" not in output
        assert "oauth2" not in output

    def test_no_mirror_renders_work_repo_not_cached(self, monkeypatch, tmp_path):
        # Real reader against an existing-but-empty cache root: a cold cache
        # is a normal miss, rendered as such.
        cache_root = tmp_path / "clone-cache"
        cache_root.mkdir()
        output = self._render(monkeypatch, cache_root)
        assert "declared: unknown (work repo not cached)" in output
        assert output.count("unset-fallback") == 3

    def test_warm_mirror_without_declaration_renders_none(self, monkeypatch, tmp_path):
        """Real mirror, real `git show` miss: a warm cache for a repo that
        declares nothing is told apart from a cold cache by the reader, not
        guessed at by the renderer."""
        cache_root = self._mirror_work_repo(tmp_path, None)
        output = self._render(monkeypatch, cache_root)
        assert "declared: none (no sources.yaml in the work repo)" in output
        assert "not cached" not in output
        assert output.count("unset-fallback") == 3


class TestShowEnvSourcesBlockSmoke:
    """The sources block is part of real `lmer --show-env` output — at the
    normal call site and at the UnknownHarnessError fail-fast branch (the
    sibling-renderer placement, decision (a))."""

    def _isolate(self, monkeypatch, tmp_path):
        strip_lmer_env(monkeypatch)
        monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.lmer/.env leakage
        monkeypatch.chdir(tmp_path)  # no stray cwd .env in the early load
        monkeypatch.setattr(
            cli,
            "read_cached_repo_file_status",
            lambda url, path="sources.yaml": (None, clone_cache.CACHE_NO_MIRROR),
        )

    def test_show_env_output_contains_sources_block(self, monkeypatch, tmp_path, capsys):
        self._isolate(monkeypatch, tmp_path)

        rc = cli.main(["--show-env"])
        out = capsys.readouterr().out

        assert rc == 0
        assert "Canonical Sources" in out
        # One origin row per source, loud even with nothing configured.
        assert "taskdef repo" in out
        assert "taskdef ref" in out
        assert "napkin repo" in out
        assert out.count("unset-fallback") == 3
        assert "declared: unknown (LMER_WORK_REPO not set)" in out

    def test_block_renders_even_when_env_table_early_returns(
        self, monkeypatch, tmp_path, capsys
    ):
        """A typo'd LMER_HARNESS takes the fail-fast branch; the block must
        still render there (and the exit code stays the harness error's 2)."""
        self._isolate(monkeypatch, tmp_path)
        monkeypatch.setenv("LMER_HARNESS", "not-a-harness")

        rc = cli.main(["--show-env"])
        out = capsys.readouterr().out

        assert rc == 2
        assert "Unknown harness 'not-a-harness'" in out
        assert "Canonical Sources" in out
        assert "unset-fallback" in out


class TestShowEnvBackwardCompat:
    """Pre-#105 invariant: with no cached work repo and neither
    LMER_TASKDEF_REPO nor LMER_NAPKIN_REPO set, --show-env keeps its exit
    code and renders the env table byte-identically — the sources block
    only appends after it."""

    def test_exit_code_and_env_table_unchanged(self, monkeypatch, tmp_path, capsys):
        strip_lmer_env(monkeypatch)
        monkeypatch.setenv("HOME", str(tmp_path))  # default cache root: absent
        monkeypatch.chdir(tmp_path)  # no stray cwd .env
        monkeypatch.setenv("LMER_TASK", "review")

        rc = cli.main(["--show-env"])
        out = capsys.readouterr().out

        # The pre-existing table, rendered directly with the same inputs
        # main() computes (LMER_TASK is the only host LMER_* var).
        f = io.StringIO()
        with redirect_stdout(f):
            _display_env_config_cli(host_lmer_vars={"LMER_TASK"}, env_file_sources={})
        expected_table = f.getvalue()

        assert rc == 0
        assert expected_table in out  # byte-identical env table
        # The sources block strictly follows the table, never replaces it.
        assert out.index(expected_table) < out.index("Canonical Sources")
