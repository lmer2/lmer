"""Work-repo git failures must not echo the remote's credential (issue #124 A3).

`run_git_command` returns stdout+stderr combined and the failure/retry paths
print it verbatim. The work-repo origin is a tokenized clone URL
(``https://oauth2:<credential>@host/agents/work.git``), so a transport or auth
error that quotes the remote would put a live credential on stderr and into the
agent transcript. Only the printed form is scrubbed — the returned detail that
`claim_push_once` classifies on is unchanged, which these pin from both sides.
"""

import pytest

from work_repo import git_ops
from work_repo.git_ops import (
    CLAIM_PUSH_ERROR,
    CLAIM_PUSH_LOST_RACE,
    _scrub_credentials,
    claim_push_once,
)
from tests.conftest import strip_lmer_env

# A credential built by concatenation so no literal secret-shaped assignment
# exists in the suite; matches the glpat- prefix a real leak would carry.
SAMPLE = "glpat-" + "A" * 20
CRED_URL = f"https://oauth2:{SAMPLE}@git.example.com/agents/work.git"
FATAL = f"fatal: unable to access '{CRED_URL}/': The requested URL returned error: 403"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


def _fixed_result(rc, output):
    """Stand in for run_git_command: every git call answers the same way."""
    def run_git_command(cmd, cwd, check=True):
        return rc, output
    return run_git_command


class TestScrubCredentials:
    def test_it_is_the_shared_definition(self):
        """One regex, one place: git_ops imports the scrub every other sink
        uses rather than carrying a copy that can drift from it."""
        from lmer_cli.container.clone_and_exec import (
            _scrub_credentials as shared_scrub,
        )

        assert git_ops._scrub_credentials is shared_scrub

    def test_strips_oauth2_userinfo(self):
        assert _scrub_credentials(CRED_URL) == "https://git.example.com/agents/work.git"

    def test_strips_credential_from_git_fatal_line(self):
        scrubbed = _scrub_credentials(FATAL)
        assert SAMPLE not in scrubbed
        assert "oauth2" not in scrubbed
        # The diagnosable part — host, path, status — survives.
        assert "git.example.com/agents/work.git" in scrubbed
        assert "403" in scrubbed

    def test_strips_basic_userinfo(self):
        text = "fatal: could not read from https://user:hunter2@git.example.com/x.git"
        scrubbed = _scrub_credentials(text)
        assert "hunter2" not in scrubbed
        assert "git.example.com" in scrubbed

    def test_preserves_text_without_credentials(self):
        text = "fatal: repository 'https://git.example.com/x.git' not found"
        assert _scrub_credentials(text) == text

    def test_leaves_an_at_sign_outside_a_url_alone(self):
        # A commit trailer or author line is not a credential.
        text = "Author: someone@git.example.com"
        assert _scrub_credentials(text) == text

    def test_handles_empty(self):
        assert _scrub_credentials("") == ""


class TestPushWithRebaseRetries:
    """The retry path prints on every attempt — each line is a leak site."""

    def test_no_credential_in_any_failure_line(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr(git_ops, "run_git_command", _fixed_result(1, FATAL))
        rc = git_ops._push_with_rebase_retries(tmp_path, "work repo")
        err = capsys.readouterr().err
        assert rc == 1
        assert SAMPLE not in err
        assert "oauth2:" not in err
        # Every printed line is still a usable diagnostic.
        assert "git pull --rebase warning" in err
        assert "git push rejected" in err
        assert f"git push failed after {git_ops.PUSH_RETRIES} attempts" in err
        assert err.count("git.example.com/agents/work.git") >= git_ops.PUSH_RETRIES

    def test_successful_push_prints_nothing(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr(git_ops, "run_git_command", _fixed_result(0, ""))
        assert git_ops._push_with_rebase_retries(tmp_path, "work repo") == 0
        assert capsys.readouterr().err == ""


class TestClaimPushOnce:
    """Scrubbing is at the print only: the returned detail is git's raw output,
    because the caller re-evaluates the claim on it."""

    def test_fetch_failure_line_is_scrubbed_but_detail_is_raw(
        self, monkeypatch, capsys, tmp_path
    ):
        monkeypatch.setattr(git_ops, "run_git_command", _fixed_result(1, FATAL))
        outcome, detail = claim_push_once(tmp_path, "work repo")
        err = capsys.readouterr().err
        assert outcome == CLAIM_PUSH_ERROR
        assert "git fetch failed" in err
        assert SAMPLE not in err
        assert detail == FATAL

    def test_push_failure_line_is_scrubbed(self, monkeypatch, capsys, tmp_path):
        def run_git_command(cmd, cwd, check=True):
            return (0, "") if cmd[0] == "fetch" else (1, FATAL)

        monkeypatch.setattr(git_ops, "run_git_command", run_git_command)
        outcome, detail = claim_push_once(tmp_path, "work repo")
        err = capsys.readouterr().err
        assert outcome == CLAIM_PUSH_ERROR
        assert "git push failed" in err
        assert SAMPLE not in err
        assert detail == FATAL

    def test_lost_race_classification_is_unaffected(self, monkeypatch, capsys, tmp_path):
        rejected = "! [rejected] main -> main (non-fast-forward)"

        def run_git_command(cmd, cwd, check=True):
            return (0, "") if cmd[0] == "fetch" else (1, rejected)

        monkeypatch.setattr(git_ops, "run_git_command", run_git_command)
        outcome, detail = claim_push_once(tmp_path, "work repo")
        assert outcome == CLAIM_PUSH_LOST_RACE
        assert detail == rejected
        assert capsys.readouterr().err == ""


class TestCommitPaths:
    """The staging/commit failures print git output too (work repo and napkin)."""

    def test_work_repo_add_failure_is_scrubbed(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
        (tmp_path / "notes.md").write_text("x\n")
        monkeypatch.setattr(git_ops, "run_git_command", _fixed_result(1, FATAL))
        rc = git_ops.commit_work_path("notes.md", "msg", allow_during_gate=True)
        err = capsys.readouterr().err
        assert rc == 1
        assert "git add failed (work repo)" in err
        assert SAMPLE not in err

    def test_napkin_add_failure_is_scrubbed(self, monkeypatch, capsys, tmp_path):
        napkin = tmp_path / "napkin"
        (napkin / ".git").mkdir(parents=True)
        monkeypatch.setenv("LMER_NAPKIN_PATH", str(napkin))
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path / "work"))
        monkeypatch.setattr(git_ops, "run_git_command", _fixed_result(1, FATAL))
        rc = git_ops.push_napkin_if_separate("msg")
        err = capsys.readouterr().err
        assert rc == 1
        assert "git add failed (napkin)" in err
        assert SAMPLE not in err
