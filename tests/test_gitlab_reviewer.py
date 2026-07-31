"""Tests for the gitlab_reviewer package.

Everything is mocked at the GitLab client layer — no network. The goal is
to catch regressions in:

  - The --resolve-thread LMER_TASK guard (Thread Resolution Policy,
    rules/git.md): non-review sessions must be refused before any API call
  - The --info thread-provenance block (counts, resolver breakdown,
    resolved-before-head heuristic, page-cap truncation marker)
  - Fail-soft behavior: --info must never fail on provenance trouble
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from gitlab_reviewer import cli as gl_cli
from gitlab_reviewer.client import GitLabError


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


HEAD_SHA = "abc123def456"
HEAD_COMMITTED_DATE = "2026-07-01T12:00:00+00:00"


def make_mr_info(sha: str = HEAD_SHA) -> dict:
    """Minimal MR payload covering every key format_mr_info dereferences."""
    return {
        "iid": 7,
        "title": "Add feature",
        "state": "opened",
        "author": {"name": "Alice", "username": "alice"},
        "created_at": "2026-06-01T10:00:00+00:00",
        "updated_at": "2026-07-01T10:00:00+00:00",
        "source_branch": "feature/x",
        "target_branch": "main",
        "merge_status": "can_be_merged",
        "sha": sha,
        "web_url": "https://gitlab.example.com/group/project/-/merge_requests/7",
    }


def make_thread(discussion_id: str, resolved: bool = False,
                resolved_by: str | None = None,
                resolved_at: str | None = None) -> dict:
    """A user discussion thread with one non-system note."""
    note = {
        "author": {"name": "Alice", "username": "alice"},
        "body": f"note in {discussion_id}",
        "created_at": "2026-06-15T10:00:00+00:00",
        "system": False,
        "resolvable": True,
    }
    if resolved_by is not None:
        note["resolved_by"] = {"username": resolved_by}
    if resolved_at is not None:
        note["resolved_at"] = resolved_at
    return {"id": discussion_id, "individual_note": False,
            "resolved": resolved, "notes": [note]}


class FakeGitLabClient:
    """Stands in for GitLabClient: records calls, serves canned data."""

    def __init__(self, host=None, token=None):
        self.host = host or "gitlab.example.com"
        self.token = token or "fake-token"
        self.last_page_capped = False
        # Test knobs
        self.discussions = []
        self.cap_discussions = False
        self.discussions_error = None
        self.commit = {"id": HEAD_SHA, "committed_date": HEAD_COMMITTED_DATE}
        self.commit_error = None
        # Call recording
        self.resolve_calls = []
        self.discussion_calls = []
        self.commit_calls = []

    def get_merge_request_info(self, project, mr_id):
        return make_mr_info()

    def get_merge_request_approvals(self, project, mr_id):
        return {"approved": True, "approvals_required": 1,
                "approvals_left": 0, "approved_by": []}

    def get_merge_request_participants(self, project, mr_id,
                                       page_size=20, max_pages=25):
        return []

    def get_merge_request_discussions(self, project, mr_id, resolved=False,
                                      page_size=20, max_pages=25):
        self.discussion_calls.append({"resolved": resolved})
        if self.discussions_error is not None:
            raise self.discussions_error
        self.last_page_capped = self.cap_discussions
        discussions = self.discussions
        if resolved is not None:
            discussions = [d for d in discussions
                           if d.get("resolved", False) == resolved]
        return discussions

    def get_commit(self, project, sha):
        self.commit_calls.append(sha)
        if self.commit_error is not None:
            raise self.commit_error
        return self.commit

    def resolve_merge_request_discussion(self, project, mr_id, discussion_id):
        self.resolve_calls.append(discussion_id)
        return {"id": discussion_id, "resolved": True}


def run_cli(monkeypatch, fake_client, argv):
    """Run gl_cli.main() with argv and the client layer replaced."""
    monkeypatch.setattr("sys.argv", ["gitlab-review"] + argv)
    with patch.object(gl_cli, "GitLabClient", return_value=fake_client):
        return gl_cli.main()


# ---------------------------------------------------------------------------
# --resolve-thread LMER_TASK guard
# ---------------------------------------------------------------------------


def test_resolve_thread_refused_in_non_review_session(monkeypatch, capsys):
    monkeypatch.setenv("LMER_TASK", "develop")
    fake = FakeGitLabClient()

    rc = run_cli(monkeypatch, fake, ["group/project", "7",
                                     "--resolve-thread", "disc1"])

    assert rc == 1
    # The resolve API must NOT have been called
    assert fake.resolve_calls == []
    err = capsys.readouterr().err
    # Names the offending session type and the policy
    assert "LMER_TASK=develop" in err
    assert "Thread Resolution Policy" in err
    assert "rules/git.md" in err
    # Explains the fix-author protocol and the human workaround
    assert "leaves the thread open" in err
    assert "web UI" in err
    assert "LMER_TASK is unset" in err


def test_resolve_thread_allowed_when_lmer_task_unset(monkeypatch, capsys):
    monkeypatch.delenv("LMER_TASK", raising=False)
    fake = FakeGitLabClient()

    rc = run_cli(monkeypatch, fake, ["group/project", "7",
                                     "--resolve-thread", "disc1"])

    assert rc == 0
    assert fake.resolve_calls == ["disc1"]
    out = capsys.readouterr().out
    assert "Successfully resolved discussion thread disc1" in out


def test_resolve_thread_allowed_in_review_session(monkeypatch):
    monkeypatch.setenv("LMER_TASK", "review")
    fake = FakeGitLabClient()

    rc = run_cli(monkeypatch, fake, ["group/project", "7",
                                     "--resolve-thread", "disc1"])

    assert rc == 0
    assert fake.resolve_calls == ["disc1"]


# ---------------------------------------------------------------------------
# --info thread provenance
# ---------------------------------------------------------------------------


def _resolved_after_head(discussion_id, resolved_by):
    """A thread resolved after the head commit — no heuristic warning."""
    return make_thread(discussion_id, resolved=True, resolved_by=resolved_by,
                       resolved_at="2026-07-02T12:00:00+00:00")


def test_info_provenance_counts_and_resolver_breakdown(monkeypatch, capsys):
    fake = FakeGitLabClient()
    fake.discussions = [
        _resolved_after_head("d1", "lmer-bob"),
        _resolved_after_head("d2", "lmer-bob"),
        _resolved_after_head("d3", "alice"),
        make_thread("d4", resolved=False),
        # System-only and individual notes are not threads — must be skipped
        {"id": "sys1", "individual_note": False, "resolved": False,
         "notes": [{"system": True, "body": "changed milestone"}]},
        {"id": "ind1", "individual_note": True, "resolved": False,
         "notes": [{"system": False, "body": "drive-by comment"}]},
    ]

    rc = run_cli(monkeypatch, fake, ["group/project", "7", "--info"])

    assert rc == 0
    # All discussions were requested (resolved=None), not just unresolved
    assert fake.discussion_calls == [{"resolved": None}]
    out = capsys.readouterr().out
    assert "=== Threads ===" in out
    assert "Total: 4" in out
    assert "Unresolved: 1" in out
    assert "Resolved: 3" in out
    assert "lmer-bob: 2" in out
    assert "alice: 1" in out
    # Nothing resolved before head → no heuristic warning
    assert "resolution predates the current code" not in out


def test_info_provenance_flags_thread_resolved_before_head(monkeypatch, capsys):
    fake = FakeGitLabClient()
    fake.discussions = [
        # Resolved a day BEFORE the head commit's committed_date
        make_thread("stale1", resolved=True, resolved_by="lmer-bob",
                    resolved_at="2026-06-30T12:00:00+00:00"),
        _resolved_after_head("fresh1", "alice"),
    ]

    rc = run_cli(monkeypatch, fake, ["group/project", "7", "--info"])

    assert rc == 0
    assert fake.commit_calls == [HEAD_SHA]
    out = capsys.readouterr().out
    assert ("⚠️ Thread stale1 resolved before the latest change — "
            "resolution predates the current code") in out
    assert "Thread fresh1 resolved before" not in out


def test_info_heuristic_silent_when_resolved_at_missing(monkeypatch, capsys):
    fake = FakeGitLabClient()
    fake.discussions = [
        make_thread("d1", resolved=True, resolved_by="lmer-bob",
                    resolved_at=None),
    ]

    rc = run_cli(monkeypatch, fake, ["group/project", "7", "--info"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "=== Threads ===" in out
    assert "Resolved: 1" in out
    assert "resolution predates the current code" not in out


def test_info_heuristic_silent_when_commit_lookup_fails(monkeypatch, capsys):
    fake = FakeGitLabClient()
    fake.discussions = [
        make_thread("d1", resolved=True, resolved_by="lmer-bob",
                    resolved_at="2026-06-30T12:00:00+00:00"),
    ]
    fake.commit_error = GitLabError("404 commit not found")

    rc = run_cli(monkeypatch, fake, ["group/project", "7", "--info", "--json"])

    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    provenance = data["thread_provenance"]
    # Heuristic skipped entirely → key omitted, counts still present
    assert "resolved_before_head" not in provenance
    assert provenance["resolved"] == 1


def test_info_provenance_partial_marker_on_page_cap(monkeypatch, capsys):
    fake = FakeGitLabClient()
    fake.discussions = [make_thread("d1", resolved=False)]
    fake.cap_discussions = True

    rc = run_cli(monkeypatch, fake, ["group/project", "7", "--info"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "counts partial — page cap" in out


def test_info_json_thread_provenance_shape(monkeypatch, capsys):
    fake = FakeGitLabClient()
    fake.discussions = [
        make_thread("stale1", resolved=True, resolved_by="lmer-bob",
                    resolved_at="2026-06-30T12:00:00+00:00"),
        _resolved_after_head("fresh1", "alice"),
        make_thread("open1", resolved=False),
    ]

    rc = run_cli(monkeypatch, fake, ["group/project", "7", "--info", "--json"])

    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["thread_provenance"] == {
        "total": 3,
        "unresolved": 1,
        "resolved": 2,
        "resolvers": {"lmer-bob": 1, "alice": 1},
        "resolved_before_head": ["stale1"],
        "partial": False,
    }


def test_info_survives_provenance_failure(monkeypatch, capsys):
    fake = FakeGitLabClient()
    fake.discussions_error = GitLabError("boom: discussions endpoint down")

    rc = run_cli(monkeypatch, fake, ["group/project", "7", "--info"])

    # --info must never fail on provenance trouble
    assert rc == 0
    out = capsys.readouterr().out
    assert "Merge Request #7" in out
    assert "Add feature" in out
    assert "=== Threads ===" not in out


def test_info_json_provenance_failure_is_null_not_omitted(monkeypatch, capsys):
    """Parity with github-review: key always present, null on failure."""
    fake = FakeGitLabClient()
    fake.discussions_error = GitLabError("boom: discussions endpoint down")

    rc = run_cli(monkeypatch, fake, ["group/project", "7", "--info", "--json"])

    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert "thread_provenance" in data
    assert data["thread_provenance"] is None
