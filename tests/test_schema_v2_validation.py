"""Validation covers the small schema-2 envelope and leaves metadata open."""

import json

from lmer_schema.checks.runner import validate
from lmer_schema.manifest import parse_manifest
from tests.test_schema_v2_sessions import session_tree, seed_launch  # noqa: F401
from work_repo import run_state


def test_opaque_session_with_custom_metadata_is_valid(session_tree):
    _, directory, state = seed_launch()
    state["session"]["future"] = {"any": [True, "value", 3]}
    run_state.write_state(directory, state)
    report = validate(session_tree)
    assert report.errors == ()
    assert not [f for f in report.findings if f.code.startswith("record.")]


def test_session_uid_must_match_directory(session_tree):
    _, directory, state = seed_launch()
    import yaml

    state["session"]["uid"] = "0" * 32
    (directory / "state.yaml").write_text(yaml.safe_dump(state))
    report = validate(session_tree)
    assert "record.session-metadata" in [f.code for f in report.findings]


def test_invalid_retirement_is_reported_without_rewriting(session_tree):
    path = session_tree / "_memory_retirements.json"
    content = json.dumps({"version": 99, "retired": {}})
    path.write_text(content)
    report = validate(session_tree)
    assert "agents.memory-retirement" in [f.code for f in report.findings]
    assert path.read_text() == content


def test_manifest_model_preserves_unknown_fields_for_writers():
    manifest = parse_manifest("schema: 2\nname: example\nfuture:\n  enabled: true\n")
    assert manifest.document["future"] == {"enabled": True}


def test_invalid_shared_release_claim_refuses_validation(session_tree):
    seed_launch()
    path = session_tree / "widgets" / "_claims" / "release.yaml"
    path.parent.mkdir()
    path.write_text("schema: 1\nholder: wrong\n")
    report = validate(session_tree)
    assert [f.code for f in report.errors] == ["record.release-claim"]


def test_memory_cli_journals_and_commits_the_complete_transition(session_tree, monkeypatch):
    from work_repo import cli

    source = session_tree / "widgets" / "_agents" / "memory" / "fact.md"
    source.parent.mkdir(parents=True)
    source.write_text("An old fact.\n")
    (source.parent / "MEMORY.md").write_text("- [Fact](fact.md)\n")
    key = source.relative_to(session_tree).as_posix()
    journal = []
    commits = []
    monkeypatch.setattr(cli.write_journal, "record_write_intent",
                        lambda command, paths: journal.append((source.exists(), paths)))
    monkeypatch.setattr(cli, "commit_work_path",
                        lambda paths, message: commits.append(paths) or 0)
    assert cli.cmd_memory("forget", None, cli.create_parser(), key) == 0
    assert journal[0][0] is True  # attribution exists before the mutation
    assert len(commits) == 1 and set(commits[0]) == set(journal[0][1])
    assert "_memory_retirements.json" in commits[0]
    assert not source.exists()
    assert cli.cmd_memory("revive", None, cli.create_parser(), key) == 0
    assert source.read_text() == "An old fact.\n"
