"""`work adopt` + `work migrate` CLI acceptance (#367/#368, spec §10).

What these tests pin end to end, through the real `work` CLI on a git
checkout of the legacy fixture:

- `work adopt` bootstraps the manifest and moves the host bucket to the flat
  short name; `work migrate <project> --apply` then moves the bare areas and
  lands ONE commit, after which the validator reports no ERRORs;
- a session launched afterwards resumes a migrated run by session id
  (`work sessions` lists it, `work resume --session <id>` briefs it);
- `work migrate --apply` is an operator-shell verb: a bare shell on a
  schema-2 tree is not refused by the session-uid gate.
"""

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from lmer_cli import gate_lock
from lmer_schema.checks import CHECKS, Form, Report, Severity
from lmer_schema.checks.runner import validate
from tests import plexus_fixtures as pf
from tests.conftest import strip_lmer_env
from work_repo import cli, git_ops

BUCKET = pf.PRIMARY_REPO
NAME = "lmer"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout


def _init_checkout(root):
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "tests@example.test")
    _git(root, "config", "user.name", "tests")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "fixture")


@pytest.fixture
def legacy_work(tmp_path, monkeypatch):
    """The legacy fixture, committed, with its second session signed out so
    the §1.6 preflight has nothing to refuse."""
    tree = pf.build_legacy_plexus(tmp_path / "work")
    events = tree.session_dirs[0] / "events.jsonl"
    events.write_text(
        events.read_text(encoding="utf-8")
        + json.dumps({
            "ts": "2026-08-24T11:50:00Z",
            "session": pf.SESSION_IDS[1],
            "type": "session_end",
        }) + "\n",
        encoding="utf-8",
    )
    _init_checkout(tree.root)
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tree.root))
    monkeypatch.setenv(gate_lock.LOCK_DIR_ENV, str(tmp_path / "gate-inflight"))
    return tree.root


@pytest.fixture
def commits(monkeypatch):
    """Stand in for commit → rebase → push: record the call and perform the
    local commit (no network), so a chained adopt then migrate sees a clean
    tree exactly as the real helper would leave it."""
    calls = []

    def record(paths, message=None, allow_during_gate=False):
        calls.append(SimpleNamespace(paths=list(paths), message=message))
        root = os.environ["LMER_WORK_REPO_PATH"]
        subprocess.run(
            ["git", "-C", root, "add", "--", *paths],
            check=False, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", root, "commit", "-q", "-m", message or "commit"],
            check=False, capture_output=True,
        )
        return 0

    monkeypatch.setattr(git_ops, "commit_work_path", record)
    return calls


def _run_cli(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["work", *argv])
    return cli.main()


def _error_findings(root):
    report = Report(Form.SCHEMA)
    from lmer_schema.checks import record as record_checks

    record_checks.check_record(root, report)
    return [f for f in report.findings if f.severity is Severity.ERROR]


def test_adopt_then_migrate_lands_one_commit_and_validates(
    legacy_work, commits, monkeypatch, capsys
):
    monkeypatch.delenv("LMER_SESSION_ID", raising=False)
    assert _run_cli(monkeypatch, ["adopt", BUCKET, NAME, "--apply"]) == 0
    assert len(commits) == 1
    capsys.readouterr()
    assert (legacy_work / NAME / "_runs").is_dir() or (legacy_work / NAME / "runs").is_dir()

    assert _run_cli(monkeypatch, ["migrate", NAME, "--apply"]) == 0
    assert len(commits) == 2, "the migration must land its own ONE commit"
    capsys.readouterr()
    assert (legacy_work / NAME / "_runs" / pf.SLUG).is_dir()
    assert _error_findings(legacy_work) == []


def test_a_session_afterwards_resumes_a_migrated_run(
    legacy_work, commits, monkeypatch, capsys
):
    monkeypatch.delenv("LMER_SESSION_ID", raising=False)
    assert _run_cli(monkeypatch, ["adopt", BUCKET, NAME, "--apply"]) == 0
    assert _run_cli(monkeypatch, ["migrate", NAME, "--apply"]) == 0
    capsys.readouterr()

    monkeypatch.setenv("LMER_REPO_URL", f"https://{BUCKET}")
    monkeypatch.setenv("LMER_REPO_HOST", pf.BUCKET_HOST)
    monkeypatch.setenv("LMER_REPO_PROJECT", pf.PRIMARY_REPO.split("/", 1)[1])
    rc = _run_cli(monkeypatch, ["sessions", "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    document = json.loads(captured.out.strip().splitlines()[-1])
    session_ids = {row["id"] for row in document["sessions"]}
    assert pf.SESSION_IDS[0] in session_ids

    rc = _run_cli(monkeypatch, ["resume", "--session", pf.SESSION_IDS[0]])
    assert rc == 0, capsys.readouterr().out + capsys.readouterr().err


def test_apply_from_a_bare_shell_is_not_refused_by_the_uid_gate(
    legacy_work, commits, monkeypatch, capsys
):
    monkeypatch.delenv("LMER_SESSION_ID", raising=False)
    assert _run_cli(monkeypatch, ["adopt", BUCKET, NAME, "--apply"]) == 0
    capsys.readouterr()
    rc = _run_cli(monkeypatch, ["migrate", NAME, "--apply"])
    captured = capsys.readouterr()
    assert "Session write refused" not in captured.err
    assert rc == 0, captured.out + captured.err
