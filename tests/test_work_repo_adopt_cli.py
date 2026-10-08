"""`work adopt` CLI wiring — the verb registered in `work_repo.cli`.

What these tests pin (issue #367; run-identity spec §10, §1.6):

- `work adopt --apply` is an operator-shell verb: it is exempt from the
  session-uid gate, so a bare shell on a schema-2 tree is not refused with
  "Session write refused" (it still declares its write scope, pinned in
  ``tests/test_work_write_journal.py``);
- #367's integration acceptance: a session launched afterwards against the
  adopted repo URL resolves the flat `<short-name>/` directory through the
  D20 registry resolver, not the host bucket it used to sit in.
"""

import subprocess
import sys
from types import SimpleNamespace

import pytest

from lmer_cli import gate_lock
from tests import schema2_fixtures as s2
from tests.conftest import strip_lmer_env
from work_repo import cli, git_ops, utils

#: An unregistered host bucket inside the schema-2 fixture, and its new name.
BUCKET = f"{s2.HOST}/unnamed/widget"
NAME = "widget"


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
def schema2_work(tmp_path, monkeypatch):
    """A schema-2 tree with an unregistered, project-shaped host bucket,
    under a real git checkout and pointed at by the CLI env."""
    tree = s2.build_schema2_plexus(tmp_path / "work")
    bucket = tree.root / BUCKET
    (bucket / "_runs").mkdir(parents=True)
    (bucket / "_info").mkdir(parents=True)
    (bucket / "_info" / "important.md").write_text("# unnamed\n", encoding="utf-8")
    _init_checkout(tree.root)
    s2.make_env(monkeypatch, tree.root)
    monkeypatch.setenv(gate_lock.LOCK_DIR_ENV, str(tmp_path / "gate-inflight"))
    return tree.root


@pytest.fixture
def commits(monkeypatch):
    """Stand in for the commit → rebase → push helper: success, no git write."""
    calls = []

    def record(paths, message=None, allow_during_gate=False):
        calls.append(SimpleNamespace(paths=list(paths), message=message))
        return 0

    monkeypatch.setattr(git_ops, "commit_work_path", record)
    return calls


def _run_cli(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["work", *argv])
    return cli.main()


def test_apply_from_a_bare_shell_is_not_refused_by_the_uid_gate(
    schema2_work, commits, monkeypatch, capsys
):
    """`adopt` writes records it does not own, from an operator shell with no
    launch uid. The uid gate exempts it (spec §10); before that exemption a
    schema-2 apply was refused with "Session write refused"."""
    monkeypatch.delenv("LMER_SESSION_ID", raising=False)
    rc = _run_cli(monkeypatch, ["adopt", BUCKET, NAME, "--apply"])
    captured = capsys.readouterr()
    assert "Session write refused" not in captured.err
    assert rc == 0, captured.out + captured.err
    assert commits, "the apply did not reach the commit helper"


def test_a_session_afterwards_resolves_the_flat_dir_through_the_registry(
    schema2_work, commits, monkeypatch
):
    """#367's integration acceptance: the D20 resolver finds `<name>/` by the
    adopted repo URL, so a later launch writes into the flat, registered dir."""
    monkeypatch.delenv("LMER_SESSION_ID", raising=False)
    rc = _run_cli(monkeypatch, ["adopt", BUCKET, NAME, "--apply"])
    assert rc == 0
    assert (schema2_work / NAME).is_dir()
    assert not (schema2_work / BUCKET).exists()

    monkeypatch.setenv("LMER_REPO_URL", f"https://{BUCKET}")
    layout = utils.work_repo_layout()
    assert layout.is_schema
    assert layout.base == schema2_work / NAME
    assert layout.project == NAME
