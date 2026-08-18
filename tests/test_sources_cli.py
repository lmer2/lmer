"""Tests for the doctor-facing CLI of lmer_cli.container.sources.

Frozen seam (module docstring "Doctor CLI"): subcommands
validate|normalize|derive|doctor, exactly one JSON document on stdout with
human text only on stderr, exit codes EXIT_OK / EXIT_REFUSAL / EXIT_USAGE,
and full redaction of every URL in the output except the clone_url field
behind --emit-clone-urls (the single documented opt-in). Tests drive
main(argv) directly.
"""

import json

import pytest

from lmer_cli.container.sources import (
    EXIT_OK,
    EXIT_REFUSAL,
    EXIT_USAGE,
    SUPPORTED_SOURCES_SCHEMAS,
    SUPPORTED_TASKDEF_SCHEMAS,
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
