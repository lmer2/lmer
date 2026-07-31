"""Tests for schema-1 parse/validation of sources.yaml (load_sources).

Covers the spec's validation surface: good file; bad/missing/bool schema
version (the YAML-bool trap: isinstance(True, int) is True); embedded
credential rejection (oauth2 tokens) vs the allowed bare git@ SSH forms;
the same-host trust rule; ref valid under taskdef only; unknown-key
forward-compat warnings; and absent file = silent legacy with zero output.
"""

import pytest

from lmer_cli.container.sources import SourcesConfigError, load_sources

WORK_URL = "https://git.example.com/20c/worklog.git"

GOOD = """\
schema: 1
sources:
  taskdef:
    repo: https://git.example.com/agents/taskdefs.git
    ref: main
  napkin:
    repo: https://git.example.com/20c/napkin.git
"""


def _write(tmp_path, text):
    path = tmp_path / "sources.yaml"
    path.write_text(text)
    return path


class TestGoodFile:
    def test_full_declaration_parses(self, tmp_path):
        cfg, warnings = load_sources(_write(tmp_path, GOOD), work_repo_url=WORK_URL)
        assert warnings == []
        assert cfg["schema"] == 1
        assert cfg["sources"]["taskdef"] == {
            "repo": "https://git.example.com/agents/taskdefs.git",
            "ref": "main",
        }
        assert cfg["sources"]["napkin"] == {
            "repo": "https://git.example.com/20c/napkin.git"
        }

    def test_ref_is_optional_under_taskdef(self, tmp_path):
        path = _write(
            tmp_path,
            "schema: 1\nsources:\n  taskdef:\n"
            "    repo: https://git.example.com/agents/taskdefs.git\n",
        )
        cfg, warnings = load_sources(path, work_repo_url=WORK_URL)
        assert warnings == []
        assert cfg["sources"]["taskdef"] == {
            "repo": "https://git.example.com/agents/taskdefs.git"
        }

    def test_source_key_absent_is_silent_legacy_for_that_source(self, tmp_path):
        path = _write(
            tmp_path,
            "schema: 1\nsources:\n  napkin:\n"
            "    repo: https://git.example.com/20c/napkin.git\n",
        )
        cfg, warnings = load_sources(path, work_repo_url=WORK_URL)
        assert warnings == []
        assert "taskdef" not in cfg["sources"]

    def test_no_sources_key_is_valid_empty_declaration(self, tmp_path):
        cfg, warnings = load_sources(_write(tmp_path, "schema: 1\n"))
        assert warnings == []
        assert cfg == {"schema": 1, "sources": {}}


class TestAbsentFile:
    def test_absent_file_is_silent_legacy(self, tmp_path, capsys):
        cfg, warnings = load_sources(tmp_path / "sources.yaml")
        assert cfg is None
        assert warnings == []
        # Zero output: legacy sessions must be byte-identical to today.
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""


class TestSchemaVersion:
    def test_unsupported_schema_fails_loud(self, tmp_path):
        path = _write(tmp_path, "schema: 99\nsources: {}\n")
        with pytest.raises(SourcesConfigError, match=r"schema 99.*supports: 1"):
            load_sources(path)

    def test_missing_schema_fails_loud(self, tmp_path):
        path = _write(tmp_path, "sources: {}\n")
        with pytest.raises(SourcesConfigError, match=r"integer `schema:`"):
            load_sources(path)

    def test_string_schema_fails_loud(self, tmp_path):
        path = _write(tmp_path, 'schema: "1"\n')
        with pytest.raises(SourcesConfigError, match=r"integer `schema:`"):
            load_sources(path)

    def test_bool_schema_rejected_despite_isinstance_int(self, tmp_path):
        # YAML-bool trap: `schema: true` parses as a bool, and
        # isinstance(True, int) is True — it must not sail through as 1.
        path = _write(tmp_path, "schema: true\n")
        with pytest.raises(SourcesConfigError, match=r"integer `schema:`"):
            load_sources(path)

    def test_non_mapping_document_fails_loud(self, tmp_path):
        path = _write(tmp_path, "- just\n- a\n- list\n")
        with pytest.raises(SourcesConfigError, match="mapping"):
            load_sources(path)

    def test_unparseable_yaml_fails_loud_not_silent(self, tmp_path):
        path = _write(tmp_path, "schema: 1\nsources: [unclosed\n")
        with pytest.raises(SourcesConfigError, match="unparseable"):
            load_sources(path)


class TestCredentialRejection:
    def test_oauth2_token_rejected_and_scrubbed(self, tmp_path):
        # Fake token value (glpat- shape, not a real secret).
        path = _write(
            tmp_path,
            "schema: 1\nsources:\n  taskdef:\n"
            "    repo: https://oauth2:glpat-FAKEtoken123@git.example.com/a/t.git\n",
        )
        with pytest.raises(SourcesConfigError) as excinfo:
            load_sources(path, work_repo_url=WORK_URL)
        msg = str(excinfo.value)
        # The refusal never echoes the secret and says how to fix it.
        assert "glpat-FAKEtoken123" not in msg
        assert "oauth2:" not in msg
        assert "Strip the credential" in msg

    def test_user_password_rejected(self, tmp_path):
        path = _write(
            tmp_path,
            "schema: 1\nsources:\n  napkin:\n"
            "    repo: https://user:hunter2@git.example.com/20c/napkin.git\n",
        )
        with pytest.raises(SourcesConfigError) as excinfo:
            load_sources(path, work_repo_url=WORK_URL)
        assert "hunter2" not in str(excinfo.value)

    def test_secret_containing_a_slash_is_rejected_and_scrubbed(self, tmp_path):
        # Iteration-4 review reproduction: the "/" inside the secret ended the
        # authority split before the "@" was found, so the userinfo was never
        # seen — the refusal did not fire and `validate`/`normalize` printed
        # the secret verbatim in their JSON.
        path = _write(
            tmp_path,
            "schema: 1\nsources:\n  taskdef:\n"
            "    repo: https://user:se/cret@git.example.com/a/t.git\n",
        )
        with pytest.raises(SourcesConfigError) as excinfo:
            load_sources(path, work_repo_url=WORK_URL)
        msg = str(excinfo.value)
        assert "se/cret" not in msg
        assert "Strip the credential" in msg

    def test_scp_form_secret_containing_a_slash_is_rejected(self, tmp_path):
        path = _write(
            tmp_path,
            "schema: 1\nsources:\n  napkin:\n"
            "    repo: user:se/cret@git.example.com:20c/napkin.git\n",
        )
        with pytest.raises(SourcesConfigError) as excinfo:
            load_sources(path, work_repo_url=WORK_URL)
        assert "se/cret" not in str(excinfo.value)

    def test_ambiguous_scheme_less_shape_is_refused_naming_both_readings(
        self, tmp_path
    ):
        # Iteration-6 review finding: `user:se/cret@host/org/x.git` has no
        # scp colon after the "@", so the scp discriminator suppressed the
        # userinfo extension and the credential went undetected — the
        # operator got the trust rule's "host '' does not match" instead of
        # "strip the credential". The shape is genuinely two-way ambiguous
        # (see _url_shape_is_ambiguous), so it is refused as unreadable with
        # both readings spelled out rather than guessed at.
        path = _write(
            tmp_path,
            "schema: 1\nsources:\n  taskdef:\n"
            "    repo: user:se/cret@git.example.com/agents/taskdefs.git\n",
        )
        with pytest.raises(SourcesConfigError) as excinfo:
            load_sources(path, work_repo_url=WORK_URL)
        msg = str(excinfo.value)
        assert "se/cret" not in msg
        assert "cannot be read unambiguously" in msg
        # Both halves of the ambiguity, so neither operator is misdirected.
        assert "never commit tokens" in msg
        assert "scheme form" in msg
        # Not the trust rule's message.
        assert "does not match the work repo host" not in msg

    def test_ambiguous_shape_refused_without_a_work_repo_url(self, tmp_path):
        # The trust rule is what used to catch this shape, and it only runs
        # when a work-repo URL is known. An unreadable URL is untrustworthy
        # either way, so the refusal does not depend on it.
        path = _write(
            tmp_path,
            "schema: 1\nsources:\n  napkin:\n"
            "    repo: user:se/cret@git.example.com/20c/napkin.git\n",
        )
        with pytest.raises(SourcesConfigError, match="cannot be read unambiguously"):
            load_sources(path, work_repo_url=None)

    def test_ambiguous_at_in_path_reading_is_refused_the_same_way(self, tmp_path):
        # The other reading of the same shape (iteration-5's URL): a userless
        # scp URL whose path contains "@". It is equally unreadable, and its
        # refusal must not accuse the operator of committing a credential.
        path = _write(
            tmp_path,
            "schema: 1\nsources:\n  taskdef:\n"
            "    repo: git.example.com:agents/re@po.git\n",
        )
        with pytest.raises(SourcesConfigError) as excinfo:
            load_sources(path, work_repo_url=WORK_URL)
        msg = str(excinfo.value)
        assert "cannot be read unambiguously" in msg
        assert "embeds a credential" not in msg

    def test_bare_scp_form_git_at_is_allowed(self, tmp_path):
        # git@ there is protocol userinfo, not a credential.
        path = _write(
            tmp_path,
            "schema: 1\nsources:\n  taskdef:\n"
            "    repo: git@git.example.com:agents/taskdefs.git\n",
        )
        cfg, warnings = load_sources(path, work_repo_url=WORK_URL)
        assert cfg["sources"]["taskdef"]["repo"] == "git@git.example.com:agents/taskdefs.git"
        assert warnings == []

    def test_bare_ssh_scheme_git_at_is_allowed(self, tmp_path):
        path = _write(
            tmp_path,
            "schema: 1\nsources:\n  taskdef:\n"
            "    repo: ssh://git@git.example.com/agents/taskdefs.git\n",
        )
        cfg, warnings = load_sources(path, work_repo_url=WORK_URL)
        assert cfg["sources"]["taskdef"]["repo"].startswith("ssh://git@")
        assert warnings == []


class TestTrustRule:
    def test_cross_host_taskdef_rejected_naming_env_override(self, tmp_path):
        path = _write(
            tmp_path,
            "schema: 1\nsources:\n  taskdef:\n"
            "    repo: https://evil.example.org/agents/taskdefs.git\n",
        )
        with pytest.raises(SourcesConfigError) as excinfo:
            load_sources(path, work_repo_url=WORK_URL)
        msg = str(excinfo.value)
        assert "evil.example.org" in msg
        assert "git.example.com" in msg
        assert "LMER_TASKDEF_REPO" in msg

    def test_cross_host_napkin_names_its_own_override(self, tmp_path):
        path = _write(
            tmp_path,
            "schema: 1\nsources:\n  napkin:\n"
            "    repo: https://evil.example.org/20c/napkin.git\n",
        )
        with pytest.raises(SourcesConfigError, match="LMER_NAPKIN_REPO"):
            load_sources(path, work_repo_url=WORK_URL)

    def test_same_host_across_url_forms_accepted(self, tmp_path):
        # scp-form work repo, https declared: hosts normalize equal.
        path = _write(
            tmp_path,
            "schema: 1\nsources:\n  taskdef:\n"
            "    repo: https://Git.Example.COM:443/agents/taskdefs.git\n",
        )
        cfg, warnings = load_sources(
            path, work_repo_url="git@git.example.com:20c/worklog.git"
        )
        assert "taskdef" in cfg["sources"]
        assert warnings == []

    def test_ssh_work_repo_on_custom_port_accepts_https_declaration(self, tmp_path):
        # Iteration-4 review reproduction: SSH-on-2222 + HTTPS-on-443 is an
        # ordinary self-hosted layout, and derive_clone_url handles it. Before
        # the fix the port rode along in the comparison key and the refusal
        # message read as self-contradictory ("git.example.com does not match
        # git.example.com").
        path = _write(
            tmp_path,
            "schema: 1\nsources:\n  taskdef:\n"
            "    repo: https://git.example.com/agents/taskdefs.git\n",
        )
        cfg, warnings = load_sources(
            path, work_repo_url="ssh://git@git.example.com:2222/20c/worklog.git"
        )
        assert "taskdef" in cfg["sources"]
        assert warnings == []

    def test_custom_port_does_not_make_a_cross_host_declaration_legal(self, tmp_path):
        # The port dropping out of the key must not weaken the rule itself.
        path = _write(
            tmp_path,
            "schema: 1\nsources:\n  taskdef:\n"
            "    repo: https://evil.example.org:2222/agents/taskdefs.git\n",
        )
        with pytest.raises(SourcesConfigError, match="evil.example.org"):
            load_sources(path, work_repo_url="ssh://git@git.example.com:2222/20c/w.git")

    def test_no_work_repo_url_skips_host_check(self, tmp_path):
        # Callers with no work-repo context (work_repo_url=None) get parse
        # and credential validation only.
        path = _write(
            tmp_path,
            "schema: 1\nsources:\n  taskdef:\n"
            "    repo: https://other.example.org/agents/taskdefs.git\n",
        )
        cfg, _ = load_sources(path)
        assert "taskdef" in cfg["sources"]


class TestRefPlacement:
    def test_ref_under_napkin_rejected(self, tmp_path):
        path = _write(
            tmp_path,
            "schema: 1\nsources:\n  napkin:\n"
            "    repo: https://git.example.com/20c/napkin.git\n"
            "    ref: main\n",
        )
        with pytest.raises(SourcesConfigError, match=r"`sources.taskdef` only"):
            load_sources(path, work_repo_url=WORK_URL)

    def test_non_string_ref_rejected(self, tmp_path):
        path = _write(
            tmp_path,
            "schema: 1\nsources:\n  taskdef:\n"
            "    repo: https://git.example.com/agents/taskdefs.git\n"
            "    ref: 1.0\n",
        )
        with pytest.raises(SourcesConfigError, match="non-empty string"):
            load_sources(path, work_repo_url=WORK_URL)


class TestMalformedEntries:
    def test_non_mapping_sources_rejected(self, tmp_path):
        path = _write(tmp_path, "schema: 1\nsources: nope\n")
        with pytest.raises(SourcesConfigError, match=r"`sources:` must be a mapping"):
            load_sources(path)

    def test_non_mapping_entry_rejected(self, tmp_path):
        path = _write(tmp_path, "schema: 1\nsources:\n  taskdef: just-a-string\n")
        with pytest.raises(SourcesConfigError, match=r"`sources.taskdef` must be a mapping"):
            load_sources(path)

    def test_missing_repo_rejected(self, tmp_path):
        path = _write(tmp_path, "schema: 1\nsources:\n  taskdef:\n    ref: main\n")
        with pytest.raises(SourcesConfigError, match=r"non-empty string `repo:`"):
            load_sources(path)


class TestForwardCompatWarnings:
    def test_unknown_top_level_key_warns_not_errors(self, tmp_path):
        path = _write(tmp_path, GOOD + "profiles: {}\n")
        cfg, warnings = load_sources(path, work_repo_url=WORK_URL)
        assert cfg is not None
        assert any("`profiles`" in w for w in warnings)

    def test_unknown_source_key_warns_not_errors(self, tmp_path):
        path = _write(
            tmp_path,
            "schema: 1\nsources:\n  mystery:\n"
            "    repo: https://git.example.com/x/y.git\n",
        )
        cfg, warnings = load_sources(path, work_repo_url=WORK_URL)
        assert cfg["sources"] == {}
        assert any("`mystery`" in w and "sources:" in w for w in warnings)

    def test_masterplan_mirror_reserved_warning(self, tmp_path):
        path = _write(
            tmp_path,
            "schema: 1\nsources:\n  masterplan_mirror:\n"
            "    repo: https://git.example.com/x/y.git\n",
        )
        cfg, warnings = load_sources(path, work_repo_url=WORK_URL)
        assert "masterplan_mirror" not in cfg["sources"]
        assert any("reserved" in w for w in warnings)

    def test_unknown_key_inside_entry_warns(self, tmp_path):
        path = _write(
            tmp_path,
            "schema: 1\nsources:\n  taskdef:\n"
            "    repo: https://git.example.com/agents/taskdefs.git\n"
            "    depth: 1\n",
        )
        cfg, warnings = load_sources(path, work_repo_url=WORK_URL)
        assert cfg["sources"]["taskdef"] == {
            "repo": "https://git.example.com/agents/taskdefs.git"
        }
        assert any("`depth`" in w for w in warnings)
