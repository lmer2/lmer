"""Tests for the doctor-facing CLI of lmer_cli.container.sources.

Frozen seam (module docstring "Doctor CLI"): subcommands
validate|normalize|derive|doctor, exactly one JSON document on stdout with
human text only on stderr, exit codes EXIT_OK / EXIT_REFUSAL / EXIT_USAGE,
and full redaction of every URL in the output except the clone_url field
behind --emit-clone-urls (the single documented opt-in). Tests drive
main(argv) directly.
"""

import json
import sys

import pytest

from lmer_cli.container import sources
from lmer_cli.container.sources import (
    EXIT_OK,
    EXIT_REFUSAL,
    EXIT_USAGE,
    MANIFEST_FILENAME,
    SUPPORTED_SOURCES_SCHEMAS,
    SUPPORTED_TASKDEF_SCHEMAS,
    TIER_LEGACY,
    TIER_MANIFEST,
    _build_parser,
    _default_doctor_path,
    main,
)

TOKEN = "glpat-sekrit123456"
WORK_URL = f"https://oauth2:{TOKEN}@git.example.com/20c/worklog.git"
WORK_URL_ANON = "https://git.example.com/20c/worklog.git"

GOOD = """\
schema: 1
sources:
  taskdef:
    repo: https://git.example.com/agents/taskdefs.git
    ref: main
  napkin:
    repo: https://git.example.com/20c/napkin.git
"""

# The manifest tier (issue #370): a `_plexus.yaml` whose `sources:` declares
# taskdef at a different URL than GOOD, so a test can tell which tier won.
MANIFEST_TASKDEF_REPO = "https://git.example.com/agents/taskdefs-v2.git"
MANIFEST_GOOD = f"""\
schema: 1
name: example
sources:
  taskdef:
    repo: {MANIFEST_TASKDEF_REPO}
    ref: main
"""
MANIFEST_NO_SOURCES = "schema: 1\nname: example\n"


@pytest.fixture(autouse=True)
def _no_ambient_repo_url(monkeypatch):
    """Hermetic runs: the CLI's $LMER_REPO_URL fallback must not fire."""
    monkeypatch.delenv("LMER_REPO_URL", raising=False)
    monkeypatch.delenv("LMER_WORK_REPO", raising=False)
    monkeypatch.delenv("LMER_WORK_REPO_CREDENTIAL_FILE", raising=False)


def _write(tmp_path, text):
    path = tmp_path / "sources.yaml"
    path.write_text(text)
    return path


def _run(capsys, argv):
    """Drive main(argv); return (exit_code, parsed_json_or_None, out, err).

    Enforces the output contract on every invocation: stdout is either
    empty (usage error) or exactly one parseable JSON document.
    """
    code = main(argv)
    out, err = capsys.readouterr()
    doc = None
    if out.strip():
        doc = json.loads(out)  # raises if stdout is not one JSON document
        assert isinstance(doc, dict)
    return code, doc, out, err


class TestNormalize:
    def test_normalizes_and_redacts_tokened_url(self, capsys):
        code, doc, out, err = _run(
            capsys,
            ["normalize", f"https://oauth2:{TOKEN}@Git.Example.com:443/a/b.git/"],
        )
        assert code == EXIT_OK
        assert doc["ok"] is True
        assert doc["normalized"] == "git.example.com/a/b"
        assert doc["host"] == "git.example.com"
        assert doc["had_embedded_credential"] is True
        assert doc["errors"] == []
        assert TOKEN not in out
        assert TOKEN not in err

    def test_scp_form_folds_to_same_comparison_form(self, capsys):
        code, doc, _, _ = _run(capsys, ["normalize", "git@git.example.com:a/b.git"])
        assert code == EXIT_OK
        assert doc["normalized"] == "git.example.com/a/b"
        assert doc["had_embedded_credential"] is False

    def test_normalized_field_is_scrubbed_for_an_unparsed_credential(self, capsys):
        # Iteration-6 review finding: `normalized` was the one URL field in
        # the document that trusted the parser instead of the redactor, so a
        # credential the boundary rule did not recognize printed verbatim.
        # The redactor is a regex and independent of the parse, so it holds
        # whichever reading of an ambiguous URL is the true one.
        code, doc, out, err = _run(
            capsys, ["normalize", f"user:se/{TOKEN}@git.example.com/a/b.git"]
        )
        assert code == EXIT_OK
        assert TOKEN not in out
        assert TOKEN not in err
        assert TOKEN not in doc["normalized"]
        # The verdict fields report one of the two readings, so the
        # ambiguity is named on stderr rather than left implied.
        assert "cannot be read unambiguously" in err

    def test_missing_url_is_usage_error_with_empty_stdout(self, capsys):
        code, doc, out, err = _run(capsys, ["normalize"])
        assert code == EXIT_USAGE
        assert doc is None
        assert out == ""
        assert "usage" in err


class TestDerive:
    def test_https_userinfo_derivation_redacted_by_default(self, capsys):
        code, doc, out, err = _run(
            capsys,
            [
                "derive",
                "https://git.example.com/agents/taskdefs.git",
                "--work-repo-url",
                WORK_URL,
            ],
        )
        assert code == EXIT_OK
        assert doc["ok"] is True
        assert doc["mode"] == "https-userinfo"
        assert doc["anonymous"] is False
        assert doc["clone_url_redacted"] is True
        assert doc["clone_url"] == "https://git.example.com/agents/taskdefs.git"
        assert TOKEN not in out
        assert TOKEN not in err

    def test_emit_clone_urls_exposes_only_the_clone_url_field(self, capsys):
        code, doc, _, err = _run(
            capsys,
            [
                "derive",
                "https://git.example.com/agents/taskdefs.git",
                "--work-repo-url",
                WORK_URL,
                "--emit-clone-urls",
            ],
        )
        assert code == EXIT_OK
        assert doc["clone_url"] == (
            f"https://oauth2:{TOKEN}@git.example.com/agents/taskdefs.git"
        )
        assert doc["clone_url_redacted"] is False
        # The opt-in unredacts exactly one field: every other value and the
        # stderr channel stay clean.
        assert TOKEN not in doc["work_repo_url"]
        assert TOKEN not in doc["declared"]
        assert TOKEN not in err

    def test_anonymous_marker_surfaced(self, capsys):
        code, doc, _, _ = _run(
            capsys,
            [
                "derive",
                "https://git.example.com/agents/taskdefs.git",
                "--work-repo-url",
                WORK_URL_ANON,
            ],
        )
        assert code == EXIT_OK
        assert doc["mode"] == "anonymous"
        assert doc["anonymous"] is True

    def test_declared_url_with_embedded_credential_is_refused(self, capsys):
        code, doc, out, err = _run(
            capsys,
            [
                "derive",
                f"https://oauth2:{TOKEN}@git.example.com/a/b.git",
                "--work-repo-url",
                WORK_URL_ANON,
            ],
        )
        assert code == EXIT_REFUSAL
        assert doc["ok"] is False
        assert doc["errors"]
        assert TOKEN not in out
        assert TOKEN not in err

    def test_ambiguous_declared_url_is_refused_like_load_sources(self, capsys):
        # Deriving would carry the work-repo credential onto whichever of the
        # two readings this code guessed; the doctor surface mirrors
        # load_sources and refuses instead (iteration-6 review finding).
        code, doc, out, err = _run(
            capsys,
            [
                "derive",
                f"user:se/{TOKEN}@git.example.com/a/b.git",
                "--work-repo-url",
                WORK_URL,
            ],
        )
        assert code == EXIT_REFUSAL
        assert doc["ok"] is False
        assert "cannot be read unambiguously" in doc["errors"][0]
        assert TOKEN not in out
        assert TOKEN not in err

    def test_no_work_repo_url_anywhere_is_usage_error(self, capsys):
        code, doc, out, err = _run(
            capsys, ["derive", "https://git.example.com/a/b.git"]
        )
        assert code == EXIT_USAGE
        assert doc is None
        assert out == ""
        assert "--work-repo-url" in err

    def test_env_fallback_supplies_work_repo_url(self, capsys, monkeypatch):
        monkeypatch.setenv("LMER_REPO_URL", WORK_URL)
        code, doc, out, _ = _run(
            capsys, ["derive", "https://git.example.com/a/b.git"]
        )
        assert code == EXIT_OK
        assert doc["mode"] == "https-userinfo"
        assert TOKEN not in out


class TestValidate:
    def test_good_file_emits_declared_sources(self, capsys, tmp_path):
        path = _write(tmp_path, GOOD)
        code, doc, out, err = _run(
            capsys, ["validate", str(path), "--work-repo-url", WORK_URL]
        )
        assert code == EXIT_OK
        assert doc["ok"] is True
        assert doc["present"] is True
        assert doc["schema"] == 1
        assert doc["sources"]["taskdef"] == {
            "repo": "https://git.example.com/agents/taskdefs.git",
            "ref": "main",
        }
        assert doc["sources"]["napkin"] == {
            "repo": "https://git.example.com/20c/napkin.git"
        }
        assert doc["warnings"] == [] and doc["errors"] == []
        assert TOKEN not in out
        assert TOKEN not in err

    def test_warnings_surface_in_json_and_stderr(self, capsys, tmp_path):
        path = _write(tmp_path, "schema: 1\nextra: 1\n")
        code, doc, _, err = _run(capsys, ["validate", str(path)])
        assert code == EXIT_OK
        assert len(doc["warnings"]) == 1
        assert "extra" in doc["warnings"][0]
        assert "warning:" in err

    def test_cross_host_refusal(self, capsys, tmp_path):
        path = _write(
            tmp_path,
            "schema: 1\nsources:\n  taskdef:\n"
            "    repo: https://other.example.net/agents/taskdefs.git\n",
        )
        code, doc, out, err = _run(
            capsys, ["validate", str(path), "--work-repo-url", WORK_URL]
        )
        assert code == EXIT_REFUSAL
        assert doc["ok"] is False
        assert doc["sources"] is None
        assert any("trust rule" in e for e in doc["errors"])
        assert TOKEN not in out
        assert TOKEN not in err

    def test_embedded_credential_in_file_never_echoed(self, capsys, tmp_path):
        path = _write(
            tmp_path,
            "schema: 1\nsources:\n  taskdef:\n"
            f"    repo: https://oauth2:{TOKEN}@git.example.com/a/b.git\n",
        )
        code, doc, out, err = _run(capsys, ["validate", str(path)])
        assert code == EXIT_REFUSAL
        assert doc["ok"] is False
        assert TOKEN not in out
        assert TOKEN not in err

    def test_absent_file_is_ok_and_not_present(self, capsys, tmp_path):
        code, doc, _, err = _run(
            capsys, ["validate", str(tmp_path / "sources.yaml")]
        )
        assert code == EXIT_OK
        assert doc["ok"] is True
        assert doc["present"] is False
        assert doc["sources"] is None
        assert err == ""


class TestDoctor:
    def test_aggregated_document(self, capsys, tmp_path):
        path = _write(tmp_path, GOOD)
        code, doc, out, err = _run(
            capsys,
            ["doctor", "--json", "--path", str(path), "--work-repo-url", WORK_URL],
        )
        assert code == EXIT_OK
        assert doc["ok"] is True
        assert doc["present"] is True
        assert doc["schema"] == 1
        assert doc["supported_sources_schemas"] == list(SUPPORTED_SOURCES_SCHEMAS)
        assert doc["supported_taskdef_schemas"] == list(SUPPORTED_TASKDEF_SCHEMAS)
        taskdef = doc["sources"]["taskdef"]
        assert taskdef["repo"] == "https://git.example.com/agents/taskdefs.git"
        assert taskdef["ref"] == "main"
        assert taskdef["mode"] == "https-userinfo"
        assert taskdef["anonymous"] is False
        assert taskdef["clone_url_redacted"] is True
        assert doc["work_repo_url"] == "https://git.example.com/20c/worklog.git"
        assert TOKEN not in out
        assert TOKEN not in err

    def test_emit_clone_urls_unredacts_clone_urls_only(self, capsys, tmp_path):
        path = _write(tmp_path, GOOD)
        code, doc, out, err = _run(
            capsys,
            [
                "doctor",
                "--json",
                "--path",
                str(path),
                "--work-repo-url",
                WORK_URL,
                "--emit-clone-urls",
            ],
        )
        assert code == EXIT_OK
        for name in ("taskdef", "napkin"):
            assert TOKEN in doc["sources"][name]["clone_url"]
            assert doc["sources"][name]["clone_url_redacted"] is False
            assert TOKEN not in doc["sources"][name]["repo"]
        assert TOKEN not in doc["work_repo_url"]
        assert TOKEN not in err

    def test_env_fallback_for_work_repo_url(self, capsys, tmp_path, monkeypatch):
        monkeypatch.setenv("LMER_REPO_URL", WORK_URL)
        path = _write(tmp_path, GOOD)
        code, doc, out, _ = _run(capsys, ["doctor", "--json", "--path", str(path)])
        assert code == EXIT_OK
        assert doc["sources"]["taskdef"]["mode"] == "https-userinfo"
        assert TOKEN not in out

    def test_clean_env_uses_session_credential_file_without_rebuilding_userinfo(
        self, capsys, tmp_path, monkeypatch
    ):
        credential_file = tmp_path / "work-credential"
        credential_file.write_text(WORK_URL + "\n")
        monkeypatch.setenv(
            "LMER_REPO_URL", "https://target.example.net/org/project.git"
        )
        monkeypatch.setenv("LMER_WORK_REPO", WORK_URL_ANON)
        monkeypatch.setenv(
            "LMER_WORK_REPO_CREDENTIAL_FILE", str(credential_file)
        )
        path = _write(tmp_path, GOOD)

        code, doc, out, err = _run(
            capsys,
            ["doctor", "--json", "--emit-clone-urls", "--path", str(path)],
        )

        assert code == EXIT_OK
        taskdef = doc["sources"]["taskdef"]
        assert taskdef["mode"] == "https-credential-file"
        assert taskdef["anonymous"] is False
        assert taskdef["clone_url"] == (
            "https://git.example.com/agents/taskdefs.git"
        )
        assert doc["work_repo_url"] == WORK_URL_ANON
        assert TOKEN not in out
        assert TOKEN not in err

    def test_explicit_doctor_url_does_not_borrow_an_unpaired_session_file(
        self, capsys, tmp_path, monkeypatch
    ):
        credential_file = tmp_path / "work-credential"
        credential_file.write_text(WORK_URL + "\n")
        monkeypatch.setenv("LMER_WORK_REPO", WORK_URL_ANON)
        monkeypatch.setenv(
            "LMER_WORK_REPO_CREDENTIAL_FILE", str(credential_file)
        )
        path = _write(tmp_path, GOOD)

        code, doc, _, _ = _run(
            capsys,
            [
                "doctor", "--json", "--path", str(path),
                "--work-repo-url", WORK_URL_ANON,
            ],
        )

        assert code == EXIT_OK
        assert doc["sources"]["taskdef"]["mode"] == "anonymous"
        assert doc["sources"]["taskdef"]["anonymous"] is True

    def test_no_work_repo_url_skips_derivation_with_warning(
        self, capsys, tmp_path
    ):
        path = _write(tmp_path, GOOD)
        code, doc, _, err = _run(capsys, ["doctor", "--path", str(path)])
        assert code == EXIT_OK
        assert doc["work_repo_url"] is None
        assert "clone_url" not in doc["sources"]["taskdef"]
        assert any("derivation skipped" in w for w in doc["warnings"])
        assert "warning:" in err

    def test_absent_file_is_healthy_legacy(self, capsys, tmp_path):
        code, doc, _, err = _run(
            capsys, ["doctor", "--path", str(tmp_path / "sources.yaml")]
        )
        assert code == EXIT_OK
        assert doc["ok"] is True
        assert doc["present"] is False
        assert doc["sources"] is None
        assert doc["supported_taskdef_schemas"] == list(SUPPORTED_TASKDEF_SCHEMAS)
        assert err == ""

    def test_bad_file_is_refusal_with_errors_in_document(self, capsys, tmp_path):
        path = _write(tmp_path, "schema: 99\n")
        code, doc, _, err = _run(capsys, ["doctor", "--json", "--path", str(path)])
        assert code == EXIT_REFUSAL
        assert doc["ok"] is False
        assert doc["present"] is True
        assert doc["errors"]
        # supported-schema lists stay present even on refusal so the sh
        # consumer can always read them from the one document.
        assert doc["supported_sources_schemas"] == list(SUPPORTED_SOURCES_SCHEMAS)
        assert doc["supported_taskdef_schemas"] == list(SUPPORTED_TASKDEF_SCHEMAS)
        assert "error:" in err

    def test_unknown_flag_is_usage_error(self, capsys):
        code, doc, out, err = _run(capsys, ["doctor", "--nope"])
        assert code == EXIT_USAGE
        assert doc is None
        assert out == ""
        assert "usage" in err


def _write_manifest(tmp_path, text):
    path = tmp_path / MANIFEST_FILENAME
    path.write_text(text)
    return path


class TestDoctorTiers:
    """`doctor` resolves BOTH declaration tiers (issue #370) through
    load_sources, reading the manifest beside --path (decision record in the
    module docstring, "Doctor CLI").

    Additive to the frozen seam: the document gains top-level `tiers`
    (source name -> manifest | legacy, parallel to `sources`), `manifest_path`
    and `manifest_present`. Per-source entries, the exit codes and the
    redaction rules are the ones the legacy-only cases above pin.
    """

    def test_manifest_only_tree_reports_the_manifest_tier(self, capsys, tmp_path):
        manifest = _write_manifest(tmp_path, MANIFEST_GOOD)
        absent = tmp_path / "sources.yaml"
        code, doc, out, err = _run(
            capsys,
            ["doctor", "--json", "--path", str(absent), "--work-repo-url", WORK_URL],
        )
        assert code == EXIT_OK
        assert doc["ok"] is True
        assert doc["present"] is True
        # The manifest's own `schema:` belongs to lmer_schema.manifest;
        # `schema` stays the legacy file's int, None without one.
        assert doc["schema"] is None
        assert doc["manifest_present"] is True
        assert doc["manifest_path"] == str(manifest)
        assert doc["path"] == str(absent)
        assert doc["tiers"] == {"taskdef": TIER_MANIFEST}
        taskdef = doc["sources"]["taskdef"]
        assert taskdef["repo"] == MANIFEST_TASKDEF_REPO
        assert taskdef["ref"] == "main"
        assert taskdef["mode"] == "https-userinfo"
        assert taskdef["anonymous"] is False
        assert taskdef["clone_url_redacted"] is True
        assert doc["warnings"] == [] and doc["errors"] == []
        assert TOKEN not in out
        assert TOKEN not in err

    def test_both_tiers_manifest_wins_per_key_with_the_stale_warning(
        self, capsys, tmp_path
    ):
        manifest = _write_manifest(tmp_path, MANIFEST_GOOD)
        path = _write(tmp_path, GOOD)
        code, doc, _, err = _run(
            capsys,
            ["doctor", "--json", "--path", str(path), "--work-repo-url", WORK_URL],
        )
        assert code == EXIT_OK
        assert doc["ok"] is True
        assert doc["schema"] == 1
        assert doc["manifest_present"] is True
        assert doc["tiers"] == {"taskdef": TIER_MANIFEST, "napkin": TIER_LEGACY}
        # Whole entry per key: the manifest's taskdef, the legacy napkin.
        assert doc["sources"]["taskdef"]["repo"] == MANIFEST_TASKDEF_REPO
        assert doc["sources"]["napkin"]["repo"] == "https://git.example.com/20c/napkin.git"
        (stale,) = doc["warnings"]
        assert "stale" in stale
        assert str(path) in stale and str(manifest) in stale
        assert "warning:" in err

    def test_legacy_only_tree_reports_legacy_for_every_key(self, capsys, tmp_path):
        path = _write(tmp_path, GOOD)
        code, doc, _, _ = _run(capsys, ["doctor", "--json", "--path", str(path)])
        assert code == EXIT_OK
        assert doc["manifest_present"] is False
        # The sibling path is reported even when nothing is there, so the
        # consumer can name where a manifest would have been read from.
        assert doc["manifest_path"] == str(tmp_path / MANIFEST_FILENAME)
        assert doc["tiers"] == {"taskdef": TIER_LEGACY, "napkin": TIER_LEGACY}
        assert doc["schema"] == 1

    def test_nothing_declared_has_no_tiers(self, capsys, tmp_path):
        code, doc, _, err = _run(
            capsys, ["doctor", "--json", "--path", str(tmp_path / "sources.yaml")]
        )
        assert code == EXIT_OK
        assert doc["present"] is False
        assert doc["manifest_present"] is False
        assert doc["sources"] is None
        assert doc["tiers"] is None
        assert err == ""

    def test_manifest_without_sources_section_is_not_a_declaration(
        self, capsys, tmp_path
    ):
        _write_manifest(tmp_path, MANIFEST_NO_SOURCES)
        code, doc, _, err = _run(
            capsys, ["doctor", "--json", "--path", str(tmp_path / "sources.yaml")]
        )
        assert code == EXIT_OK
        assert doc["ok"] is True
        assert doc["manifest_present"] is True
        # Present manifest, no sources: tier — silent legacy mode, exactly as
        # load_sources reports it.
        assert doc["present"] is False
        assert doc["sources"] is None
        assert doc["tiers"] is None
        assert doc["warnings"] == [] and doc["errors"] == []
        assert err == ""

    def test_broken_manifest_is_a_refusal_naming_the_manifest(self, capsys, tmp_path):
        manifest = _write_manifest(tmp_path, "projects: [broken\n")
        code, doc, _, err = _run(
            capsys, ["doctor", "--json", "--path", str(tmp_path / "sources.yaml")]
        )
        assert code == EXIT_REFUSAL
        assert doc["ok"] is False
        assert doc["present"] is True
        assert doc["manifest_present"] is True
        assert doc["manifest_path"] == str(manifest)
        (error,) = doc["errors"]
        assert str(manifest) in error
        assert "unparseable" in error
        assert doc["sources"] is None
        assert doc["tiers"] is None
        # supported-schema lists stay present even on refusal (as for a bad
        # sources.yaml) so the sh consumer can always read them.
        assert doc["supported_sources_schemas"] == list(SUPPORTED_SOURCES_SCHEMAS)
        assert doc["supported_taskdef_schemas"] == list(SUPPORTED_TASKDEF_SCHEMAS)
        assert "error:" in err

    def test_broken_manifest_never_demotes_to_the_legacy_tier(self, capsys, tmp_path):
        # Spec D20: a present manifest this helper cannot read is a refusal,
        # never a fall-back to the legacy file sitting beside it.
        _write_manifest(tmp_path, "projects: [broken\n")
        path = _write(tmp_path, GOOD)
        code, doc, _, _ = _run(capsys, ["doctor", "--json", "--path", str(path)])
        assert code == EXIT_REFUSAL
        assert doc["sources"] is None
        assert doc["tiers"] is None
        assert doc["schema"] is None

    def test_manifest_tier_refusal_is_scrubbed(self, capsys, tmp_path):
        manifest = _write_manifest(
            tmp_path,
            "schema: 1\nname: example\nsources:\n  taskdef:\n"
            f"    repo: https://oauth2:{TOKEN}@git.example.com/a/b.git\n",
        )
        code, doc, out, err = _run(
            capsys, ["doctor", "--json", "--path", str(tmp_path / "sources.yaml")]
        )
        assert code == EXIT_REFUSAL
        assert doc["ok"] is False
        assert str(manifest) in doc["errors"][0]
        assert TOKEN not in out
        assert TOKEN not in err

    def test_manifest_tier_trust_rule_names_the_env_override(self, capsys, tmp_path):
        manifest = _write_manifest(
            tmp_path,
            "schema: 1\nname: example\nsources:\n  napkin:\n"
            "    repo: https://evil.example.org/20c/napkin.git\n",
        )
        code, doc, _, _ = _run(
            capsys,
            [
                "doctor", "--json", "--path", str(tmp_path / "sources.yaml"),
                "--work-repo-url", WORK_URL,
            ],
        )
        assert code == EXIT_REFUSAL
        (error,) = doc["errors"]
        assert "LMER_NAPKIN_REPO" in error
        assert str(manifest) in error

    def test_unimportable_core_is_a_warning_naming_it(
        self, capsys, tmp_path, monkeypatch
    ):
        manifest = _write_manifest(tmp_path, MANIFEST_GOOD)
        path = _write(tmp_path, GOOD)
        # None in sys.modules makes the function-local import raise
        # ImportError — an lmer built without the lmer-schema core. Both the
        # package and the submodule are blocked: an already-imported
        # submodule would otherwise be served straight from the cache.
        monkeypatch.setitem(sys.modules, "lmer_schema", None)
        monkeypatch.setitem(sys.modules, "lmer_schema.manifest", None)
        code, doc, _, err = _run(capsys, ["doctor", "--json", "--path", str(path)])
        assert code == EXIT_OK
        assert doc["ok"] is True
        assert doc["manifest_present"] is True
        (warning,) = [w for w in doc["warnings"] if str(manifest) in w]
        assert "lmer_schema" in warning
        assert "not read" in warning
        assert "PyYAML" not in warning
        assert "warning:" in err
        # The legacy tier is still read; the manifest tier is unread, not absent.
        assert doc["tiers"] == {"taskdef": TIER_LEGACY, "napkin": TIER_LEGACY}
        assert doc["sources"]["taskdef"]["repo"] == "https://git.example.com/agents/taskdefs.git"

    def test_pyyaml_missing_beneath_a_manifest_is_its_own_diagnosis(
        self, capsys, tmp_path, monkeypatch
    ):
        manifest = _write_manifest(tmp_path, MANIFEST_GOOD)
        monkeypatch.setattr(sources, "yaml", None)
        code, doc, _, _ = _run(
            capsys, ["doctor", "--json", "--path", str(tmp_path / "sources.yaml")]
        )
        # No legacy file, so nothing refuses: the manifest tier is simply
        # reported as unread, blaming the interpreter and not the manifest.
        assert code == EXIT_OK
        assert doc["ok"] is True
        assert doc["manifest_present"] is True
        (warning,) = doc["warnings"]
        assert "PyYAML" in warning
        assert str(manifest) in warning
        assert "lmer_schema" not in warning
        assert "unparseable" not in warning
        assert doc["present"] is False
        assert doc["tiers"] is None

    def test_manifest_is_read_beside_path_not_from_the_env_root(
        self, capsys, tmp_path, monkeypatch
    ):
        """The decision record: `<dir of --path>/_plexus.yaml`, nothing else."""
        other = tmp_path / "other"
        other.mkdir()
        _write_manifest(other, MANIFEST_GOOD)
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(other))
        here = tmp_path / "here"
        here.mkdir()
        path = _write(here, GOOD)
        code, doc, _, _ = _run(capsys, ["doctor", "--json", "--path", str(path)])
        assert code == EXIT_OK
        assert doc["manifest_present"] is False
        assert doc["manifest_path"] == str(here / MANIFEST_FILENAME)
        assert doc["tiers"] == {"taskdef": TIER_LEGACY, "napkin": TIER_LEGACY}
        # The canonical no---path invocation reads the work repo's manifest.
        code, doc, _, _ = _run(capsys, ["doctor", "--json"])
        assert code == EXIT_OK
        assert doc["path"] == str(other / "sources.yaml")
        assert doc["manifest_present"] is True
        assert doc["tiers"] == {"taskdef": TIER_MANIFEST}

    def test_emit_clone_urls_unredacts_manifest_tier_clone_urls_only(
        self, capsys, tmp_path
    ):
        _write_manifest(tmp_path, MANIFEST_GOOD)
        code, doc, _, err = _run(
            capsys,
            [
                "doctor", "--json", "--path", str(tmp_path / "sources.yaml"),
                "--work-repo-url", WORK_URL, "--emit-clone-urls",
            ],
        )
        assert code == EXIT_OK
        taskdef = doc["sources"]["taskdef"]
        assert TOKEN in taskdef["clone_url"]
        assert taskdef["clone_url_redacted"] is False
        assert TOKEN not in taskdef["repo"]
        assert TOKEN not in doc["work_repo_url"]
        assert TOKEN not in err


class TestDoctorDefaultPath:
    """`doctor` without --path reads the WORK repo, never /workspace.

    The documented canonical invocation carries no --path, so the default
    decides what every real session's diagnostic looks at: /workspace is the
    target-repo checkout, while sources.yaml lives in the work repo at
    $LMER_WORK_REPO_PATH (default /work).

    The env-unset cases assert the resolved default only — running doctor
    against the real /work would not be hermetic.
    """

    def test_default_is_work_repo_not_workspace_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("LMER_WORK_REPO_PATH", raising=False)
        assert _default_doctor_path() == "/work/sources.yaml"
        assert _build_parser().parse_args(["doctor"]).path == "/work/sources.yaml"

    def test_empty_env_value_falls_back_to_work(self, monkeypatch):
        monkeypatch.setenv("LMER_WORK_REPO_PATH", "")
        assert _default_doctor_path() == "/work/sources.yaml"

    def test_default_tracks_work_repo_path_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path / "work"))
        expected = str(tmp_path / "work" / "sources.yaml")
        assert _default_doctor_path() == expected
        # Read at parser-build time, so a later change is picked up.
        assert _build_parser().parse_args(["doctor"]).path == expected

    def test_no_path_flag_finds_the_declaration_in_the_work_repo(
        self, capsys, monkeypatch, tmp_path
    ):
        """End-to-end: the canonical no---path invocation sees a real file."""
        work = tmp_path / "work"
        work.mkdir()
        (work / "sources.yaml").write_text(GOOD)
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))
        code, doc, _, _ = _run(capsys, ["doctor", "--json"])
        assert code == EXIT_OK
        assert doc["path"] == str(work / "sources.yaml")
        assert doc["present"] is True
        assert set(doc["sources"]) == {"taskdef", "napkin"}


class TestNoSecretInDefaultOutputAnySubcommand:
    """Given a tokened work-repo URL, default output never carries the token."""

    def test_every_subcommand(self, capsys, tmp_path):
        path = _write(tmp_path, GOOD)
        invocations = [
            ["normalize", WORK_URL],
            ["derive", "https://git.example.com/a/b.git", "--work-repo-url", WORK_URL],
            ["validate", str(path), "--work-repo-url", WORK_URL],
            ["doctor", "--json", "--path", str(path), "--work-repo-url", WORK_URL],
        ]
        for argv in invocations:
            code, _, out, err = _run(capsys, argv)
            assert code == EXIT_OK, argv
            assert TOKEN not in out, argv
            assert TOKEN not in err, argv


class TestUsageSurface:
    def test_no_subcommand_is_usage_error(self, capsys):
        code, doc, out, err = _run(capsys, [])
        assert code == EXIT_USAGE
        assert doc is None
        assert out == ""
        assert "usage" in err

    def test_unknown_subcommand_is_usage_error(self, capsys):
        code, _, out, err = _run(capsys, ["frobnicate"])
        assert code == EXIT_USAGE
        assert out == ""
        assert "usage" in err
