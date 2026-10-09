"""`work knowledge index` (plan task 41, #372)."""
import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.conftest import strip_lmer_env
from work_repo import knowledge

GOLDENS = Path(__file__).resolve().parent / "fixtures" / "knowledge-index"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    strip_lmer_env(monkeypatch)


def _git(root, *args, env=None):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=str(root), check=True, capture_output=True, env=env,
    )


def _build(tmp_path):
    root = tmp_path / "plexus"
    kd = root / "_knowledge"

    def w(rel, text):
        p = kd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    w("acme/overview.md", "# Acme overview\n\nWelcome.\n")
    w("acme/no-heading.md", "Just prose, no heading.\n")
    w("acme/guides/setup.md", "---\ndescription: How to set up\n---\n# Setup guide\n")
    w("acme/INDEX.md", "# Hand-written acme index\n\n- [Overview](overview.md)\n")
    w("beta/README.md", "# Beta README\n")
    w("beta/notes/deep/nested.md", "# Nested note\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    env = {**os.environ, "GIT_AUTHOR_DATE": "2026-01-02T00:00:00Z",
           "GIT_COMMITTER_DATE": "2026-01-02T00:00:00Z"}
    _git(root, "commit", "-qm", "init", env=env)
    return root


def test_generated_indexes_match_the_goldens(tmp_path, monkeypatch, capsys):
    root = _build(tmp_path)
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(root))
    assert knowledge.run_index() == 0
    out = capsys.readouterr().out
    assert "hand-written; skipped" in out
    for org, golden in (("acme", "acme.INDEX.md"), ("beta", "beta.INDEX.md")):
        produced = (root / "_knowledge" / org / "INDEX.md").read_text()
        assert produced == (GOLDENS / golden).read_text()


def test_a_missing_index_in_an_org_with_pages_is_out_of_date(tmp_path, monkeypatch):
    root = _build(tmp_path)
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(root))
    assert knowledge.run_index(check=True) == 1  # nothing generated yet
    assert not (root / "_knowledge" / "beta" / "INDEX.md").exists()
    assert knowledge.run_index() == 0
    assert knowledge.run_index(check=True) == 0


def test_check_returns_one_after_a_page_is_added(tmp_path, monkeypatch):
    root = _build(tmp_path)
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(root))
    assert knowledge.run_index() == 0
    (root / "_knowledge" / "beta" / "new.md").write_text("# New page\n")
    assert knowledge.run_index(check=True) == 1
    assert knowledge.run_index() == 0
    assert knowledge.run_index(check=True) == 0


def test_a_hand_written_index_is_never_overwritten(tmp_path, monkeypatch):
    root = _build(tmp_path)
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(root))
    hand = root / "_knowledge" / "acme" / "INDEX.md"
    before = hand.read_bytes()
    knowledge.run_index()
    assert hand.read_bytes() == before


def test_no_new_validator_codes(tmp_path, monkeypatch):
    from lmer_schema import cli as schema_cli

    root = _build(tmp_path)
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(root))
    before = _codes(root)
    knowledge.run_index()
    after = _codes(root)
    assert after == before


def _codes(root):
    proc = subprocess.run(
        ["python", "-m", "lmer_schema.cli", "validate", str(root), "--json"],
        capture_output=True, text=True, check=False,
    )
    try:
        doc = json.loads(proc.stdout)
    except ValueError:
        return set()
    return {f.get("code") for f in doc.get("findings", [])}
