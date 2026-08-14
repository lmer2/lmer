"""Tests for the gitlab_reviewer package.

Everything is mocked at the GitLab client layer — no network. The goal is
to catch regressions in:

  - The --resolve-thread LMER_TASK guard (Thread Resolution Policy,
    rules/git.md): non-review sessions must be refused before any API call
  - The --reply-thread/--resolve-thread pair: URL and payload of the reply
    call, the --comment-file requirement, reply-then-resolve ordering,
    truncated discussion IDs, and non-resolvable (general) threads
  - The --info thread-provenance block (counts, resolver breakdown,
    resolved-before-head heuristic, page-cap truncation marker)
  - Fail-soft behavior: --info must never fail on provenance trouble
  - Token resolution: the client sends whatever it resolves as a PRIVATE-TOKEN
    header to whatever `--host` it was handed, so the generic GITLAB_TOKEN must
    stay scoped to its issuing host (issue #161)
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from gitlab_reviewer import cli as gl_cli
from gitlab_reviewer.client import GitLabClient, GitLabError
from lmer_cli import tokens
from tests.conftest import strip_lmer_env


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


HEAD_SHA = "abc123def456"
HEAD_COMMITTED_DATE = "2026-07-01T12:00:00+00:00"

# Discussion IDs are full 40-character SHA1 hex digests
THREAD_ID = "0123456789abcdef0123456789abcdef01234567"


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


def make_general_thread(discussion_id: str) -> dict:
    """A summary/general thread: its notes are not resolvable.

    GitLab only lets diff (inline) threads be resolved; general threads come
    back with resolvable false and the resolve call is refused.
    """
    thread = make_thread(discussion_id)
    thread["individual_note"] = True
    thread["notes"][0]["resolvable"] = False
    return thread


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
        self.discussion = None  # single-thread lookup override
        # Call recording
        self.resolve_calls = []
        self.reply_calls = []
        self.discussion_lookups = []
        self.discussion_calls = []
        self.commit_calls = []
        self.write_order = []

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

    def get_merge_request_discussion(self, project, mr_id, discussion_id):
        self.discussion_lookups.append(discussion_id)
        if self.discussion is not None:
            return self.discussion
        return make_thread(discussion_id)

    def reply_to_merge_request_discussion(self, project, mr_id, discussion_id, body):
        self.reply_calls.append((discussion_id, body))
        self.write_order.append("reply")
        return {"id": 4242, "body": body,
                "author": {"name": "Alice", "username": "alice"},
                "created_at": "2026-07-03T10:00:00+00:00"}

    def resolve_merge_request_discussion(self, project, mr_id, discussion_id):
        self.resolve_calls.append(discussion_id)
        self.write_order.append("resolve")
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
                                     "--resolve-thread", THREAD_ID])

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
                                     "--resolve-thread", THREAD_ID])

    assert rc == 0
    assert fake.resolve_calls == [THREAD_ID]
    out = capsys.readouterr().out
    assert f"Successfully resolved discussion thread {THREAD_ID}" in out


def test_resolve_thread_allowed_in_review_session(monkeypatch):
    monkeypatch.setenv("LMER_TASK", "review")
    fake = FakeGitLabClient()

    rc = run_cli(monkeypatch, fake, ["group/project", "7",
                                     "--resolve-thread", THREAD_ID])

    assert rc == 0
    assert fake.resolve_calls == [THREAD_ID]


# ---------------------------------------------------------------------------
# --reply-thread (and composing it with --resolve-thread)
# ---------------------------------------------------------------------------


@pytest.fixture
def review_session(monkeypatch):
    """Resolving is reviewer-only — opt in where the resolve path is exercised."""
    monkeypatch.setenv("LMER_TASK", "review")


def write_comment_file(tmp_path, body: str = "Fixed in abc1234.") -> str:
    """Write a reply body to a file and return its path."""
    path = tmp_path / "reply.md"
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_reply_to_mr_discussion_posts_note_to_thread():
    """Client layer: URL shape and payload of the reply call."""
    client = GitLabClient(host="gitlab.example.com", token="fake-token")

    with patch.object(GitLabClient, "_request", return_value={"id": 1}) as request:
        client.reply_to_merge_request_discussion(
            "group/project", 7, THREAD_ID, "Fixed in abc1234."
        )

    request.assert_called_once_with(
        "POST",
        f"projects/group%2Fproject/merge_requests/7/discussions/{THREAD_ID}/notes",
        json={"body": "Fixed in abc1234."},
    )


def test_reply_thread_sends_comment_file_body(monkeypatch, capsys, tmp_path):
    fake = FakeGitLabClient()
    comment_file = write_comment_file(tmp_path, "Fixed in abc1234.")

    rc = run_cli(monkeypatch, fake, ["group/project", "7",
                                     "--reply-thread", THREAD_ID,
                                     "--comment-file", comment_file])

    assert rc == 0
    assert fake.reply_calls == [(THREAD_ID, "Fixed in abc1234.")]
    # A reply alone must not touch the resolve path
    assert fake.resolve_calls == []
    out = capsys.readouterr().out
    assert f"Successfully replied to discussion thread {THREAD_ID}" in out


def test_reply_thread_requires_comment_file(monkeypatch, capsys):
    fake = FakeGitLabClient()

    rc = run_cli(monkeypatch, fake, ["group/project", "7",
                                     "--reply-thread", THREAD_ID])

    assert rc == 1
    assert fake.reply_calls == []
    assert "--reply-thread requires --comment-file" in capsys.readouterr().err


def test_comment_file_requires_reply_thread(monkeypatch, capsys, tmp_path):
    """A comment file with no thread to attach it to must not be ignored."""
    fake = FakeGitLabClient()

    rc = run_cli(monkeypatch, fake, ["group/project", "7",
                                     "--comment-file", write_comment_file(tmp_path)])

    assert rc == 1
    assert fake.reply_calls == []
    assert "--comment-file requires --reply-thread" in capsys.readouterr().err


def test_reply_thread_reports_missing_comment_file(monkeypatch, capsys, tmp_path):
    fake = FakeGitLabClient()

    rc = run_cli(monkeypatch, fake, ["group/project", "7",
                                     "--reply-thread", THREAD_ID,
                                     "--comment-file", str(tmp_path / "nope.md")])

    assert rc == 1
    assert fake.reply_calls == []
    assert "File not found" in capsys.readouterr().err


def test_reply_then_resolve_in_one_invocation(monkeypatch, capsys, tmp_path, review_session):
    fake = FakeGitLabClient()

    rc = run_cli(monkeypatch, fake, ["group/project", "7",
                                     "--reply-thread", THREAD_ID,
                                     "--comment-file", write_comment_file(tmp_path),
                                     "--resolve-thread", THREAD_ID])

    assert rc == 0
    # The reply must land before the thread is closed
    assert fake.write_order == ["reply", "resolve"]
    assert fake.resolve_calls == [THREAD_ID]
    out = capsys.readouterr().out
    assert f"Successfully replied to discussion thread {THREAD_ID}" in out
    assert f"Successfully resolved discussion thread {THREAD_ID}" in out


def test_reply_and_resolve_json_is_one_document(monkeypatch, capsys, tmp_path, review_session):
    fake = FakeGitLabClient()

    rc = run_cli(monkeypatch, fake, ["group/project", "7",
                                     "--reply-thread", THREAD_ID,
                                     "--comment-file", write_comment_file(tmp_path),
                                     "--resolve-thread", THREAD_ID,
                                     "--json"])

    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["reply"]["body"] == "Fixed in abc1234."
    assert data["resolve"] == {"id": THREAD_ID, "resolved": True}


def test_resolve_thread_json_keeps_raw_payload(monkeypatch, capsys, review_session):
    """Resolving alone keeps the pre-existing single-payload JSON shape."""
    fake = FakeGitLabClient()

    rc = run_cli(monkeypatch, fake, ["group/project", "7",
                                     "--resolve-thread", THREAD_ID, "--json"])

    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"id": THREAD_ID, "resolved": True}


@pytest.mark.parametrize("flag", ["--reply-thread", "--resolve-thread"])
def test_truncated_discussion_id_is_rejected(monkeypatch, capsys, tmp_path, flag):
    """A short ID would 404 — indistinguishable from a token-scope failure."""
    fake = FakeGitLabClient()
    argv = ["group/project", "7", flag, THREAD_ID[:8]]
    if flag == "--reply-thread":
        argv += ["--comment-file", write_comment_file(tmp_path)]

    rc = run_cli(monkeypatch, fake, argv)

    assert rc == 1
    # Rejected before any API call
    assert fake.reply_calls == []
    assert fake.resolve_calls == []
    assert fake.discussion_lookups == []
    err = capsys.readouterr().err
    assert f"{flag} got an invalid discussion ID" in err
    assert "40-character SHA1" in err
    assert "8 character(s)" in err


def test_non_hex_discussion_id_is_rejected(monkeypatch, capsys, review_session):
    fake = FakeGitLabClient()

    rc = run_cli(monkeypatch, fake, ["group/project", "7",
                                     "--resolve-thread", "z" * 40])

    assert rc == 1
    assert fake.resolve_calls == []
    assert "invalid discussion ID" in capsys.readouterr().err


def test_resolve_refuses_general_thread(monkeypatch, capsys, review_session):
    fake = FakeGitLabClient()
    fake.discussion = make_general_thread(THREAD_ID)

    rc = run_cli(monkeypatch, fake, ["group/project", "7",
                                     "--resolve-thread", THREAD_ID])

    assert rc == 1
    assert fake.discussion_lookups == [THREAD_ID]
    assert fake.resolve_calls == []
    err = capsys.readouterr().err
    assert (f"thread {THREAD_ID} is not resolvable "
            "(general threads cannot be resolved)") in err


def test_general_thread_refusal_posts_no_reply(monkeypatch, capsys, tmp_path, review_session):
    """Composing on a general thread must not half-apply: nothing is written."""
    fake = FakeGitLabClient()
    fake.discussion = make_general_thread(THREAD_ID)

    rc = run_cli(monkeypatch, fake, ["group/project", "7",
                                     "--reply-thread", THREAD_ID,
                                     "--comment-file", write_comment_file(tmp_path),
                                     "--resolve-thread", THREAD_ID])

    assert rc == 1
    assert fake.write_order == []
    assert "general threads cannot be resolved" in capsys.readouterr().err


def test_reply_works_on_a_general_thread(monkeypatch, tmp_path):
    """Reply is not restricted to diff threads — only resolve is."""
    fake = FakeGitLabClient()
    fake.discussion = make_general_thread(THREAD_ID)

    rc = run_cli(monkeypatch, fake, ["group/project", "7",
                                     "--reply-thread", THREAD_ID,
                                     "--comment-file", write_comment_file(tmp_path)])

    assert rc == 0
    assert fake.reply_calls == [(THREAD_ID, "Fixed in abc1234.")]
    # No resolvability lookup is needed when only replying
    assert fake.discussion_lookups == []


def test_resolve_refused_in_non_review_session_posts_no_reply(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("LMER_TASK", "develop")
    fake = FakeGitLabClient()

    rc = run_cli(monkeypatch, fake, ["group/project", "7",
                                     "--reply-thread", THREAD_ID,
                                     "--comment-file", write_comment_file(tmp_path),
                                     "--resolve-thread", THREAD_ID])

    assert rc == 1
    assert fake.write_order == []
    assert "Thread Resolution Policy" in capsys.readouterr().err


@pytest.mark.parametrize("replying", [True, False])
@pytest.mark.parametrize("refusal", ["policy", "not-resolvable", "bad-id"])
def test_refused_resolve_says_the_reply_went_nowhere(monkeypatch, capsys, tmp_path,
                                                     refusal, replying):
    """Every resolve refusal aborts the reply half too — say so.

    The refusal texts only speak about resolving, so a composed call reads
    as "the reply landed, only the resolve was declined". The notice must
    fire on all three refusal paths, and only when a reply was in flight.
    """
    monkeypatch.setenv("LMER_TASK", "develop" if refusal == "policy" else "review")
    fake = FakeGitLabClient()
    if refusal == "not-resolvable":
        fake.discussion = make_general_thread(THREAD_ID)
    resolve_id = THREAD_ID[:8] if refusal == "bad-id" else THREAD_ID

    argv = ["group/project", "7", "--resolve-thread", resolve_id]
    if replying:
        argv += ["--reply-thread", THREAD_ID,
                 "--comment-file", write_comment_file(tmp_path)]

    rc = run_cli(monkeypatch, fake, argv)

    assert rc == 1
    assert fake.write_order == []
    err = capsys.readouterr().err
    assert ("Your reply was NOT posted" in err) is replying
    assert ("--reply-thread alone to post it" in err) is replying


def test_bad_reply_id_does_not_claim_a_reply_was_dropped(monkeypatch, capsys, tmp_path,
                                                         review_session):
    """The reply's own ID being bad is self-explanatory — no extra notice."""
    fake = FakeGitLabClient()

    rc = run_cli(monkeypatch, fake, ["group/project", "7",
                                     "--reply-thread", THREAD_ID[:8],
                                     "--comment-file", write_comment_file(tmp_path),
                                     "--resolve-thread", THREAD_ID])

    assert rc == 1
    assert fake.write_order == []
    err = capsys.readouterr().err
    assert "--reply-thread got an invalid discussion ID" in err
    assert "Your reply was NOT posted" not in err


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


# ---------------------------------------------------------------------------
# Token resolution (issue #161)
# ---------------------------------------------------------------------------


#: Provider vars the shared lookup consults; cleared so a developer's real
#: shell cannot satisfy (or defeat) any assertion below.
_TOKEN_ENV = (
    "GITLAB_TOKEN",
    "GITLAB_TOKEN_git_example_com",
    "GITLAB_TOKEN_gitlab_other_com",
    "GITLAB_HOST",
    "GH_TOKEN",
    "GITHUB_TOKEN",
)

_STUB = "glpat-notarealcredential"


@pytest.fixture
def token_env(monkeypatch):
    """No run context, no provider vars, no carried-over refusal notices."""
    strip_lmer_env(monkeypatch)
    for name in _TOKEN_ENV:
        monkeypatch.delenv(name, raising=False)
    tokens._warned.clear()
    yield
    tokens._warned.clear()


def test_host_specific_token_applies_to_its_own_host(token_env, monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN_git_example_com", _STUB)
    assert GitLabClient._resolve_token("git.example.com") == _STUB


def test_host_specific_token_does_not_leak_to_another_host(token_env, monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN_git_example_com", _STUB)
    assert GitLabClient._resolve_token("gitlab.other.com") is None


def test_generic_token_applies_to_the_work_repo_host(token_env, monkeypatch):
    """The single-host setup: one PAT, one host, unchanged behavior."""
    monkeypatch.setenv("GITLAB_TOKEN", _STUB)
    monkeypatch.setenv("LMER_WORK_REPO", "https://git.example.com/agents/work.git")
    assert GitLabClient._resolve_token("git.example.com") == _STUB
    client = GitLabClient(host="git.example.com")
    assert client.session.headers["PRIVATE-TOKEN"] == _STUB


def test_generic_token_refused_for_another_host(token_env, monkeypatch, capsys):
    monkeypatch.setenv("GITLAB_TOKEN", _STUB)
    monkeypatch.setenv("LMER_WORK_REPO", "https://git.example.com/agents/work.git")
    assert GitLabClient._resolve_token("gitlab.other.com") is None
    err = capsys.readouterr().err
    assert "GITLAB_TOKEN not used for gitlab.other.com" in err
    assert _STUB not in err


def test_generic_token_refused_when_the_issuing_host_is_unknown(token_env, monkeypatch):
    """Default --host is gitlab.com; with no LMER_WORK_REPO nothing vouches for it."""
    monkeypatch.setenv("GITLAB_TOKEN", _STUB)
    assert GitLabClient._resolve_token("gitlab.com") is None


def test_refused_generic_token_raises_and_names_the_per_host_variable(
    token_env, monkeypatch, capsys
):
    monkeypatch.setenv("GITLAB_TOKEN", _STUB)
    monkeypatch.setenv("LMER_WORK_REPO", "https://git.example.com/agents/work.git")
    with pytest.raises(GitLabError) as exc_info:
        GitLabClient(host="gitlab.other.com")
    assert "GITLAB_TOKEN_gitlab_other_com" in str(exc_info.value)
    assert _STUB not in str(exc_info.value)
    assert _STUB not in capsys.readouterr().err


def test_explicit_token_argument_bypasses_the_lookup(token_env):
    """`--token` is the operator's own call about who gets the credential."""
    client = GitLabClient("gitlab.other.com", _STUB)
    assert client.session.headers["PRIVATE-TOKEN"] == _STUB
