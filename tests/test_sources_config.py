"""Tests for schema-1 parse/validation of sources.yaml (load_sources).

Covers the spec's validation surface: good file; bad/missing/bool schema
version (the YAML-bool trap: isinstance(True, int) is True); embedded
credential rejection (oauth2 tokens) vs the allowed bare git@ SSH forms;
the same-host trust rule; ref valid under taskdef only; unknown-key
forward-compat warnings; and absent file = silent legacy with zero output.

Plus the manifest tier (issue #370): `_plexus.yaml sources:` handed in as the
raw dict lmer_schema.manifest already parsed (`manifest_sources=`), outranking
the legacy file per key with the same trust rules at both tiers, validated
through the ONE per-entry rule set (`validate_source_entry`); the `tiers` map
the result carries; and the origin labels (`declared_origin_label`).
"""

import pytest

from lmer_cli.container import sources
from lmer_cli.container.sources import (
    SOURCES_FILENAME,
    TIER_LEGACY,
    TIER_MANIFEST,
    SourcesConfigError,
    declared_origin_label,
    load_sources,
    validate_source_entry,
)

WORK_URL = "https://git.example.com/20c/worklog.git"

# The manifest tier is handed over as the raw `Manifest.sources` dict; the path
# is only ever a label in messages (load_sources never opens the file).
MANIFEST_PATH = "/work/_plexus.yaml"

MANIFEST_SOURCES = {
    "taskdef": {"repo": "https://git.example.com/agents/taskdefs.git", "ref": "v2"},
    "napkin": {"repo": "https://git.example.com/20c/napkin.git"},
}

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


class TestManifestTier:
    """#370 points 1-2: `_plexus.yaml sources:` outranks `sources.yaml` per key."""

    def _load(self, tmp_path, manifest, **kw):
        kw.setdefault("work_repo_url", WORK_URL)
        kw.setdefault("manifest_path", MANIFEST_PATH)
        return load_sources(tmp_path / "sources.yaml", manifest_sources=manifest, **kw)

    def test_a_manifest_only_declaration_resolves(self, tmp_path):
        cfg, warnings = self._load(tmp_path, MANIFEST_SOURCES)
        assert warnings == []
        assert cfg["sources"] == {
            "taskdef": {"repo": "https://git.example.com/agents/taskdefs.git", "ref": "v2"},
            "napkin": {"repo": "https://git.example.com/20c/napkin.git"},
        }
        assert cfg["tiers"] == {"taskdef": TIER_MANIFEST, "napkin": TIER_MANIFEST}
        # No legacy file means no legacy `schema:` int to report; the
        # manifest's own `schema:` belongs to lmer_schema.manifest and
        # `sources:` carries no second version field.
        assert cfg["schema"] is None

    def test_manifest_beats_legacy_per_key_with_one_stale_warning(self, tmp_path):
        legacy = _write(tmp_path, GOOD)  # taskdef (ref main) + napkin
        manifest = {"taskdef": {"repo": "https://git.example.com/agents/taskdefs-v2.git"}}
        cfg, warnings = self._load(tmp_path, manifest)
        # Whole entry: the manifest's taskdef declares no ref, so the legacy
        # file's `ref: main` does not leak through underneath it.
        assert cfg["sources"]["taskdef"] == {
            "repo": "https://git.example.com/agents/taskdefs-v2.git"
        }
        # A key only the legacy file declares keeps the legacy entry.
        assert cfg["sources"]["napkin"] == {"repo": "https://git.example.com/20c/napkin.git"}
        assert cfg["tiers"] == {"taskdef": TIER_MANIFEST, "napkin": TIER_LEGACY}
        assert cfg["schema"] == 1
        # Exactly ONE warning, naming the legacy file as stale for that key.
        assert len(warnings) == 1, warnings
        (warning,) = warnings
        assert str(legacy) in warning
        assert "`sources.taskdef`" in warning
        assert "stale" in warning
        assert MANIFEST_PATH in warning
        assert "napkin" not in warning

    def test_legacy_only_is_unchanged_under_an_empty_manifest_tier(self, tmp_path):
        legacy = _write(tmp_path, GOOD)
        plain, plain_warnings = load_sources(legacy, work_repo_url=WORK_URL)
        cfg, warnings = self._load(tmp_path, {})
        assert plain_warnings == warnings == []
        assert cfg["sources"] == plain["sources"]
        assert cfg["schema"] == plain["schema"] == 1
        assert cfg["tiers"] == {"taskdef": TIER_LEGACY, "napkin": TIER_LEGACY}

    def test_untiered_call_returns_todays_value(self, tmp_path):
        # The tier rides in a parallel `tiers` map that exists only when the
        # manifest tier was consulted: without `manifest_sources` the result
        # is byte-for-byte the pre-#370 shape (the pins above and in
        # tests/test_sources_cli.py rely on it).
        cfg, _ = load_sources(_write(tmp_path, GOOD), work_repo_url=WORK_URL)
        assert set(cfg) == {"schema", "sources"}

    def test_neither_tier_present_yields_nothing(self, tmp_path, capsys):
        cfg, warnings = self._load(tmp_path, {})
        assert cfg is None
        assert warnings == []
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_same_host_violation_in_manifest_raises_naming_env_var(self, tmp_path):
        manifest = {"napkin": {"repo": "https://evil.example.org/20c/napkin.git"}}
        with pytest.raises(SourcesConfigError) as excinfo:
            self._load(tmp_path, manifest)
        msg = str(excinfo.value)
        assert "evil.example.org" in msg
        assert "git.example.com" in msg
        assert "LMER_NAPKIN_REPO" in msg
        # The message names the manifest, not the (absent) legacy file.
        assert msg.startswith(f"{MANIFEST_PATH}: ")
        assert SOURCES_FILENAME not in msg

    def test_credential_in_manifest_refuses_and_scrubs(self, tmp_path):
        # Fake token value (glpat- shape, not a real secret).
        manifest = {
            "taskdef": {"repo": "https://oauth2:glpat-FAKEtoken123@git.example.com/a/t.git"}
        }
        with pytest.raises(SourcesConfigError) as excinfo:
            self._load(tmp_path, manifest)
        msg = str(excinfo.value)
        assert "glpat-FAKEtoken123" not in msg
        assert "oauth2:" not in msg
        assert "Strip the credential" in msg
        assert msg.startswith(f"{MANIFEST_PATH}: ")

    def test_ambiguous_url_in_manifest_refuses(self, tmp_path):
        manifest = {"taskdef": {"repo": "user:se/cret@git.example.com/agents/taskdefs.git"}}
        with pytest.raises(SourcesConfigError) as excinfo:
            self._load(tmp_path, manifest)
        msg = str(excinfo.value)
        assert "se/cret" not in msg
        assert "cannot be read unambiguously" in msg
        assert msg.startswith(f"{MANIFEST_PATH}: ")

    def test_invalid_outranked_legacy_file_still_refuses(self, tmp_path):
        # The manifest declares every key the legacy file does, so the legacy
        # file is fully outranked — and still refuses start: YAML-bool trap.
        _write(
            tmp_path,
            "schema: true\nsources:\n  taskdef:\n"
            "    repo: https://git.example.com/agents/taskdefs.git\n",
        )
        with pytest.raises(SourcesConfigError, match=r"integer `schema:`"):
            self._load(tmp_path, MANIFEST_SOURCES)

    def test_outranked_legacy_entry_is_still_validated_in_full(self, tmp_path):
        # Both tiers are validated before the merge: a cross-host legacy
        # entry the manifest would override is still a refusal.
        _write(
            tmp_path,
            "schema: 1\nsources:\n  taskdef:\n"
            "    repo: https://evil.example.org/agents/taskdefs.git\n",
        )
        with pytest.raises(SourcesConfigError, match="evil.example.org"):
            self._load(tmp_path, MANIFEST_SOURCES)

    def test_unsupported_outranked_legacy_schema_still_refuses(self, tmp_path):
        _write(tmp_path, "schema: 99\nsources: {}\n")
        with pytest.raises(SourcesConfigError, match=r"schema 99.*supports: 1"):
            self._load(tmp_path, MANIFEST_SOURCES)

    def test_manifest_napkin_ref_refuses(self, tmp_path):
        # What the lmer_schema.manifest / checks comments predicted: with the
        # loader landed, a manifest `sources.napkin.ref` refuses start.
        manifest = {"napkin": {"repo": "https://git.example.com/20c/napkin.git", "ref": "main"}}
        with pytest.raises(SourcesConfigError, match=r"`sources.taskdef` only") as excinfo:
            self._load(tmp_path, manifest)
        assert str(excinfo.value).startswith(f"{MANIFEST_PATH}: ")

    def test_knowledge_and_plans_warn_and_are_absent(self, tmp_path):
        manifest = dict(
            MANIFEST_SOURCES,
            knowledge={"repo": "https://git.example.com/20c/knowledge.git"},
            plans={"repo": "https://git.example.com/20c/plans.git"},
        )
        cfg, warnings = self._load(tmp_path, manifest)
        assert set(cfg["sources"]) == {"taskdef", "napkin"}
        assert set(cfg["tiers"]) == {"taskdef", "napkin"}
        assert len(warnings) == 2, warnings
        for name in ("knowledge", "plans"):
            assert any(
                f"`sources.{name}`" in w and "not mounted" in w and MANIFEST_PATH in w
                for w in warnings
            ), (name, warnings)

    def test_unmounted_names_mirror_the_manifest_vocabulary(self):
        # Drift guard: sources.py never imports lmer_schema (standalone
        # contract), so the names are a module-local mirror.
        from lmer_schema.manifest import RESERVED_SOURCE_NAMES

        assert sources.UNMOUNTED_SOURCE_NAMES == RESERVED_SOURCE_NAMES

    def test_manifest_filename_mirrors_lmer_schema(self):
        from lmer_schema.manifest import MANIFEST_NAME

        assert sources.MANIFEST_FILENAME == MANIFEST_NAME

    def test_manifest_path_defaults_to_the_bare_filename(self, tmp_path):
        manifest = {"napkin": {"repo": "https://evil.example.org/20c/napkin.git"}}
        with pytest.raises(SourcesConfigError, match=r"^_plexus\.yaml: "):
            load_sources(
                tmp_path / "sources.yaml", work_repo_url=WORK_URL, manifest_sources=manifest
            )

    def test_unknown_manifest_source_name_warns_not_errors(self, tmp_path):
        manifest = {"mystery": {"repo": "https://git.example.com/x/y.git"}}
        cfg, warnings = self._load(tmp_path, manifest)
        assert cfg["sources"] == {}
        assert cfg["tiers"] == {}
        assert any("`mystery`" in w and "sources:" in w and MANIFEST_PATH in w for w in warnings)

    def test_unknown_key_inside_manifest_entry_warns(self, tmp_path):
        manifest = {
            "taskdef": {"repo": "https://git.example.com/agents/taskdefs.git", "depth": 1}
        }
        cfg, warnings = self._load(tmp_path, manifest)
        assert cfg["sources"]["taskdef"] == {"repo": "https://git.example.com/agents/taskdefs.git"}
        assert any("`depth`" in w and MANIFEST_PATH in w for w in warnings)

    def test_non_mapping_manifest_entry_refused(self, tmp_path):
        with pytest.raises(SourcesConfigError, match=r"`sources.taskdef` must be a mapping"):
            self._load(tmp_path, {"taskdef": "just-a-string"})

    def test_missing_manifest_repo_refused(self, tmp_path):
        with pytest.raises(SourcesConfigError, match=r"non-empty string `repo:`"):
            self._load(tmp_path, {"taskdef": {"ref": "main"}})

    def test_no_work_repo_url_skips_host_check_at_the_manifest_tier_too(self, tmp_path):
        manifest = {"taskdef": {"repo": "https://other.example.org/agents/taskdefs.git"}}
        cfg, _ = self._load(tmp_path, manifest, work_repo_url=None)
        assert cfg["tiers"] == {"taskdef": TIER_MANIFEST}


class TestValidateSourceEntry:
    """The ONE per-entry rule set, shared by both tiers (and by the migrate
    fold, which validates legacy entries through it before folding them)."""

    ORIGIN = "/work/sources.yaml"

    def test_returns_parsed_entry_and_unknown_key_warnings(self):
        parsed, warnings = validate_source_entry(
            "taskdef",
            {"repo": " https://git.example.com/a/t.git ", "ref": " main ", "depth": 1},
            work_repo_url=WORK_URL,
            origin=self.ORIGIN,
        )
        assert parsed == {"repo": "https://git.example.com/a/t.git", "ref": "main"}
        assert warnings == [
            f"{self.ORIGIN}: unknown key `depth` under `sources.taskdef` ignored (forward-compat)"
        ]

    def test_ref_only_when_declared(self):
        parsed, warnings = validate_source_entry(
            "napkin",
            {"repo": "https://git.example.com/20c/napkin.git"},
            work_repo_url=WORK_URL,
            origin=self.ORIGIN,
        )
        assert parsed == {"repo": "https://git.example.com/20c/napkin.git"}
        assert warnings == []

    @pytest.mark.parametrize(
        "key, entry, pattern",
        [
            ("taskdef", "just-a-string", r"`sources.taskdef` must be a mapping"),
            ("taskdef", {"ref": "main"}, r"non-empty string `repo:`"),
            ("taskdef", {"repo": "   "}, r"non-empty string `repo:`"),
            (
                "napkin",
                {"repo": "https://user:hunter2@git.example.com/20c/napkin.git"},
                r"embeds a credential",
            ),
            (
                "taskdef",
                {"repo": "user:se/cret@git.example.com/agents/taskdefs.git"},
                r"cannot be read unambiguously",
            ),
            (
                "taskdef",
                {"repo": "https://evil.example.org/agents/taskdefs.git"},
                r"LMER_TASKDEF_REPO",
            ),
            (
                "napkin",
                {"repo": "https://git.example.com/20c/napkin.git", "ref": "main"},
                r"`sources.taskdef` only",
            ),
            (
                "taskdef",
                {"repo": "https://git.example.com/agents/taskdefs.git", "ref": 1.0},
                r"non-empty string",
            ),
        ],
    )
    def test_refusals_name_the_origin(self, key, entry, pattern):
        with pytest.raises(SourcesConfigError, match=pattern) as excinfo:
            validate_source_entry(key, entry, work_repo_url=WORK_URL, origin=self.ORIGIN)
        msg = str(excinfo.value)
        assert msg.startswith(f"{self.ORIGIN}: ")
        assert "hunter2" not in msg
        assert "se/cret" not in msg

    def test_no_work_repo_url_skips_the_host_check(self):
        parsed, _ = validate_source_entry(
            "taskdef",
            {"repo": "https://other.example.org/agents/taskdefs.git"},
            origin=self.ORIGIN,
        )
        assert parsed["repo"] == "https://other.example.org/agents/taskdefs.git"

    def test_both_tiers_route_through_it(self, tmp_path, monkeypatch):
        # No trust rule exists twice: every entry of either tier reaches the
        # one function, each with its own file as the origin.
        calls = []
        real = sources.validate_source_entry

        def spy(key, entry, **kw):
            calls.append((key, str(kw["origin"])))
            return real(key, entry, **kw)

        monkeypatch.setattr(sources, "validate_source_entry", spy)
        legacy = _write(tmp_path, GOOD)
        load_sources(
            legacy,
            work_repo_url=WORK_URL,
            manifest_sources=MANIFEST_SOURCES,
            manifest_path=MANIFEST_PATH,
        )
        assert sorted(calls) == sorted(
            [
                ("taskdef", MANIFEST_PATH),
                ("napkin", MANIFEST_PATH),
                ("taskdef", str(legacy)),
                ("napkin", str(legacy)),
            ]
        )


class TestDeclaredOriginLabel:
    """The origin vocabulary, defined once for the container banner and
    --show-env."""

    def test_tier_vocabulary(self):
        assert sources.SOURCE_TIERS == (TIER_MANIFEST, TIER_LEGACY)
        assert sources.TIER_LABELS == {TIER_MANIFEST: "manifest", TIER_LEGACY: SOURCES_FILENAME}

    def test_labels(self):
        assert declared_origin_label(TIER_MANIFEST) == "declared (manifest)"
        assert declared_origin_label(TIER_LEGACY) == "declared (sources.yaml)"

    def test_qualifiers_ride_inside_the_parentheses(self):
        # --show-env's cache caveat.
        assert (
            declared_origin_label(TIER_MANIFEST, "cached, possibly stale")
            == "declared (manifest, cached, possibly stale)"
        )

    def test_unknown_tier_rejected(self):
        with pytest.raises(ValueError, match="env"):
            declared_origin_label("env")
