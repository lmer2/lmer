"""Tests for the github_reviewer package.

The wrapper is a thin layer over the ``gh`` CLI. We do **not** call gh in
these tests — every external interaction is mocked. The goal is to catch
regressions in:

  - JSON-shape normalization (gh's response → gitlab-review-style dict)
  - The --review-file translation contract (gitlab-style JSON in → GitHub
    REST POST payload out)
  - Token resolution priority (GH_TOKEN_<host> over GH_TOKEN over
    GITHUB_TOKEN)
"""

from __future__ import annotations

import json
import sys
from unittest.mock import patch

import pytest

from github_reviewer import cli as gh_cli


# ---------------------------------------------------------------------------
# Token resolution
# ---------------------------------------------------------------------------


def test_resolve_token_explicit_wins(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "from-env")
    assert gh_cli._resolve_token("github.com", explicit="from-arg") == "from-arg"


def test_resolve_token_host_specific_wins_over_gh_token(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "generic")
    monkeypatch.setenv("GH_TOKEN_github_example_com", "for-ghe")
    assert gh_cli._resolve_token("github.example.com") == "for-ghe"


def test_resolve_token_falls_back_to_gh_token(monkeypatch):
    monkeypatch.delenv("GH_TOKEN_github_com", raising=False)
    monkeypatch.setenv("GH_TOKEN", "fallback")
    monkeypatch.setenv("GITHUB_TOKEN", "lower-fallback")
    assert gh_cli._resolve_token("github.com") == "fallback"


def test_resolve_token_falls_back_to_github_token(monkeypatch):
    monkeypatch.delenv("GH_TOKEN_github_com", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "actions-style")
    assert gh_cli._resolve_token("github.com") == "actions-style"


def test_resolve_token_returns_none_when_nothing_set(monkeypatch):
    for var in ("GH_TOKEN", "GITHUB_TOKEN", "GH_TOKEN_github_com"):
        monkeypatch.delenv(var, raising=False)
    assert gh_cli._resolve_token("github.com") is None


def test_sanitize_hostname():
    assert gh_cli._sanitize_hostname("github.com") == "github_com"
    assert gh_cli._sanitize_hostname("Git.Example-Org.Com") == "git_example_org_com"


# ---------------------------------------------------------------------------
# PR info normalization
# ---------------------------------------------------------------------------


SAMPLE_GH_PR = {
    "number": 42,
    "title": "Add feature X",
    "state": "OPEN",
    "author": {"login": "alice", "name": "Alice A."},
    "createdAt": "2026-05-01T10:00:00Z",
    "updatedAt": "2026-05-02T11:00:00Z",
    "mergedAt": None,
    "headRefName": "feature/x",
    "baseRefName": "main",
    "mergeable": "MERGEABLE",
    "isDraft": False,
    "body": "PR body text.",
    "reviewDecision": "APPROVED",
    "reviews": [
        {"state": "APPROVED", "author": {"login": "bob", "name": "Bob B."}},
        {"state": "COMMENTED", "author": {"login": "carol", "name": ""}},
    ],
    "reviewRequests": [{"login": "dave", "name": "Dave D."}],
    "assignees": [{"login": "alice", "name": "Alice A."}],
    "comments": [{"author": {"login": "carol"}, "body": "lgtm", "createdAt": "2026-05-02T09:00:00Z"}],
    "changedFiles": 3,
    "url": "https://github.com/owner/repo/pull/42",
    "participants": [
        {"login": "alice", "name": "Alice A."},
        {"login": "bob", "name": "Bob B."},
    ],
}


def test_norm_pr_info_basic_shape():
    norm = gh_cli._norm_pr_info(SAMPLE_GH_PR)
    assert norm["iid"] == 42
    assert norm["title"] == "Add feature X"
    assert norm["state"] == "open"
    assert norm["author"] == {"name": "Alice A.", "username": "alice"}
    assert norm["source_branch"] == "feature/x"
    assert norm["target_branch"] == "main"
    assert norm["draft"] is False
    assert norm["has_conflicts"] is False
    assert norm["web_url"].endswith("/pull/42")
    # merge_status uses GitLab vocabulary (parity claim of normalization)
    assert norm["merge_status"] == "can_be_merged"


def test_norm_pr_info_merge_status_translates_to_gitlab_vocab():
    cases = {
        "MERGEABLE": "can_be_merged",
        "CONFLICTING": "cannot_be_merged",
        "UNKNOWN": "checking",
        "": "unchecked",
        None: "unchecked",
    }
    for gh_value, gitlab_value in cases.items():
        pr = {**SAMPLE_GH_PR, "mergeable": gh_value}
        assert gh_cli._norm_pr_info(pr)["merge_status"] == gitlab_value, (
            f"GitHub mergeable={gh_value!r} should map to {gitlab_value!r}"
        )


def test_norm_pr_info_user_notes_count_sums_review_comments():
    # Without _review_comment_count, falls back to issue-comments only
    pr = {**SAMPLE_GH_PR}  # has 1 issue comment
    assert gh_cli._norm_pr_info(pr)["user_notes_count"] == 1
    # With _review_comment_count injected (as cmd_pr_info does), they sum
    pr_with_review = {**SAMPLE_GH_PR, "_review_comment_count": 7}
    assert gh_cli._norm_pr_info(pr_with_review)["user_notes_count"] == 8


def test_norm_pr_info_approval_decision():
    norm = gh_cli._norm_pr_info(SAMPLE_GH_PR)
    assert norm["approvals"]["approved"] is True
    approvers = norm["approvals"]["approved_by"]
    # Only the APPROVED review should appear, not the COMMENTED one
    assert len(approvers) == 1
    assert approvers[0]["user"]["username"] == "bob"


def test_norm_pr_info_conflict_detection():
    pr = {**SAMPLE_GH_PR, "mergeable": "CONFLICTING"}
    norm = gh_cli._norm_pr_info(pr)
    assert norm["has_conflicts"] is True
    assert norm["merge_status"] == "cannot_be_merged"


def test_gh_mergeable_to_gitlab_unknown_values_fall_back_to_unchecked(capsys):
    # Defensive: any unexpected new GitHub value should not crash; map to unchecked.
    assert gh_cli._gh_mergeable_to_gitlab("SOMETHING_NEW") == "unchecked"
    # ...and warn to stderr so a new GitHub enum value doesn't silently masquerade.
    err = capsys.readouterr().err
    assert "unrecognized GitHub mergeable value" in err
    assert "SOMETHING_NEW" in err


def test_gh_mergeable_to_gitlab_known_values_do_not_warn(capsys):
    # The four known values should not warn — only true unknowns should.
    for v in ("MERGEABLE", "CONFLICTING", "UNKNOWN", "", None):
        gh_cli._gh_mergeable_to_gitlab(v)
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# Count-only review-thread stats walker (used by cmd_pr_info)
# ---------------------------------------------------------------------------


def test_fetch_review_thread_stats_sums_totalcount_without_walking_bodies():
    """The cheap-path walker sums comments.totalCount per thread,
    paginating thread pages only (not per-thread reply pages).
    """
    page1 = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                        "nodes": [
                            {"isResolved": True, "resolvedBy": {"login": "bob"}, "comments": {"totalCount": 3}},
                            {"isResolved": False, "resolvedBy": None, "comments": {"totalCount": 1}},
                        ],
                    }
                }
            }
        }
    }
    page2 = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [{"isResolved": False, "resolvedBy": None, "comments": {"totalCount": 5}}],
                    }
                }
            }
        }
    }

    calls = []

    def fake_gh_json(args, *, host, token):
        calls.append(args)
        return [page1, page2][len(calls) - 1]

    with patch.object(gh_cli, "_gh_json", side_effect=fake_gh_json):
        stats = gh_cli._fetch_review_thread_stats(
            "o/r", 1, host="github.com", token=None, page_size=10, max_pages=10
        )

    assert stats["comment_count"] == 3 + 1 + 5
    assert len(stats["threads"]) == 3
    assert stats["partial"] is False
    # Cursor was passed on the second call
    assert any("cursor=c1" in a for a in calls[1])
    query_text = next(a for a in calls[0] if "query=" in a)
    # Query must use first:0 on the per-thread comments to avoid fetching bodies
    assert "comments(first: 0)" in query_text
    # Provenance needs the resolver login...
    assert "resolvedBy { login }" in query_text
    # ...but resolvedAt does not exist in GitHub's schema — never request it.
    assert "resolvedAt" not in query_text


def test_fetch_review_thread_stats_warns_and_marks_partial_at_page_cap(capsys):
    response = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": True, "endCursor": "x"},
                        "nodes": [{"isResolved": False, "resolvedBy": None, "comments": {"totalCount": 2}}],
                    }
                }
            }
        }
    }
    with patch.object(gh_cli, "_gh_json", return_value=response):
        stats = gh_cli._fetch_review_thread_stats(
            "o/r", 1, host="github.com", token=None, page_size=1, max_pages=2
        )

    # 2 pages * 1 thread/page * 2 comments/thread
    assert stats["comment_count"] == 4
    assert stats["partial"] is True
    err = capsys.readouterr().err
    assert "user_notes_count may undercount" in err


def test_norm_user_falls_back_to_login_when_name_blank():
    user = {"login": "x", "name": ""}
    assert gh_cli._norm_user(user) == {"name": "x", "username": "x"}


def test_norm_user_handles_none():
    assert gh_cli._norm_user(None) == {"name": "", "username": ""}


# ---------------------------------------------------------------------------
# Issue info normalization
# ---------------------------------------------------------------------------


SAMPLE_GH_ISSUE = {
    "number": 7,
    "title": "Bug: thing breaks",
    "state": "OPEN",
    "author": {"login": "alice", "name": "Alice"},
    "createdAt": "2026-05-01T10:00:00Z",
    "updatedAt": "2026-05-01T10:00:00Z",
    "closedAt": None,
    "body": "Steps to reproduce: ...",
    "labels": [{"name": "bug"}, {"name": "needs-triage"}],
    "assignees": [{"login": "alice", "name": "Alice"}],
    "milestone": {"title": "v1.0", "state": "open", "dueOn": "2026-06-01"},
    "comments": [{}],
    "url": "https://github.com/owner/repo/issues/7",
}


def test_norm_issue_info():
    norm = gh_cli._norm_issue_info(SAMPLE_GH_ISSUE)
    assert norm["iid"] == 7
    assert norm["title"] == "Bug: thing breaks"
    assert norm["state"] == "open"
    assert norm["labels"] == ["bug", "needs-triage"]
    assert norm["milestone"] == {"title": "v1.0", "state": "open", "due_date": "2026-06-01"}
    assert norm["user_notes_count"] == 1


# ---------------------------------------------------------------------------
# Review-file translation
# ---------------------------------------------------------------------------


def test_load_review_from_file_minimal(tmp_path):
    f = tmp_path / "review.json"
    f.write_text(json.dumps({
        "inline_comments": [
            {"file_path": "src/x.py", "line_number": 10, "comment": "nit"}
        ],
        "summary": "lgtm overall",
    }))
    inline, summary, event = gh_cli.load_review_from_file(str(f))
    assert inline == [{"file_path": "src/x.py", "line_number": 10, "comment": "nit"}]
    assert summary == "lgtm overall"
    assert event == "COMMENT"


def test_load_review_from_file_accepts_event_and_side(tmp_path):
    f = tmp_path / "review.json"
    f.write_text(json.dumps({
        "inline_comments": [
            {"file_path": "x.py", "line_number": 1, "comment": "c", "side": "left"}
        ],
        "summary": "must change",
        "event": "request_changes",
    }))
    inline, summary, event = gh_cli.load_review_from_file(str(f))
    assert inline[0]["side"] == "LEFT"
    assert event == "REQUEST_CHANGES"


def test_load_review_from_file_rejects_bad_event(tmp_path):
    f = tmp_path / "review.json"
    f.write_text(json.dumps({"inline_comments": [], "event": "MERGE"}))
    with pytest.raises(gh_cli.GitHubError, match="event"):
        gh_cli.load_review_from_file(str(f))


def test_load_review_from_file_rejects_bad_side(tmp_path):
    f = tmp_path / "review.json"
    f.write_text(json.dumps({
        "inline_comments": [
            {"file_path": "x", "line_number": 1, "comment": "c", "side": "middle"}
        ],
    }))
    with pytest.raises(gh_cli.GitHubError, match="side"):
        gh_cli.load_review_from_file(str(f))


def test_load_review_from_file_rejects_missing_field(tmp_path):
    f = tmp_path / "review.json"
    f.write_text(json.dumps({
        "inline_comments": [{"file_path": "x", "comment": "c"}],  # missing line_number
    }))
    with pytest.raises(gh_cli.GitHubError, match="line_number"):
        gh_cli.load_review_from_file(str(f))


def test_load_review_from_file_rejects_non_integer_line(tmp_path):
    f = tmp_path / "review.json"
    f.write_text(json.dumps({
        "inline_comments": [{"file_path": "x", "line_number": "abc", "comment": "c"}],
    }))
    with pytest.raises(gh_cli.GitHubError, match="integer"):
        gh_cli.load_review_from_file(str(f))


# ---------------------------------------------------------------------------
# cmd_review_file: payload built correctly and POSTed via gh api
# ---------------------------------------------------------------------------


def test_cmd_review_file_builds_correct_payload(tmp_path):
    f = tmp_path / "review.json"
    f.write_text(json.dumps({
        "inline_comments": [
            {"file_path": "a.py", "line_number": 5, "comment": "one"},
            {"file_path": "b.py", "line_number": 12, "comment": "two", "side": "LEFT"},
        ],
        "summary": "two comments",
    }))

    captured = {}

    def fake_gh(args, *, host, token, input_data=None):
        captured["args"] = args
        captured["host"] = host
        captured["input"] = input_data
        return json.dumps({"id": 999, "state": "COMMENTED", "html_url": "u"})

    with patch.object(gh_cli, "_gh", side_effect=fake_gh):
        result = gh_cli.cmd_review_file(
            "owner/repo", 42, str(f), host="github.com", token="t"
        )

    assert result == {"id": 999, "state": "COMMENTED", "html_url": "u"}
    # Verify the gh api call shape
    assert captured["args"][:5] == ["api", "-X", "POST", "repos/owner/repo/pulls/42/reviews", "--input"]
    payload = json.loads(captured["input"].decode())
    assert payload["body"] == "two comments"
    assert payload["event"] == "COMMENT"
    assert payload["comments"] == [
        {"path": "a.py", "line": 5, "side": "RIGHT", "body": "one"},
        {"path": "b.py", "line": 12, "side": "LEFT", "body": "two"},
    ]


def test_cmd_review_file_summary_only_no_inline(tmp_path):
    f = tmp_path / "review.json"
    f.write_text(json.dumps({"summary": "just a thought", "event": "APPROVE"}))

    captured = {}

    def fake_gh(args, *, host, token, input_data=None):
        captured["input"] = input_data
        return json.dumps({"id": 1, "state": "APPROVED", "html_url": "u"})

    with patch.object(gh_cli, "_gh", side_effect=fake_gh):
        gh_cli.cmd_review_file("o/r", 1, str(f), host="github.com", token=None)

    payload = json.loads(captured["input"].decode())
    assert payload["event"] == "APPROVE"
    assert payload["body"] == "just a thought"
    assert "comments" not in payload  # no inline → key omitted


# ---------------------------------------------------------------------------
# _gh subprocess wiring: token/host env propagation
# ---------------------------------------------------------------------------


def test_gh_subprocess_sets_gh_token_and_gh_host(monkeypatch):
    captured = {}

    class FakeResult:
        returncode = 0
        stdout = b'{}'
        stderr = b''

    def fake_run(args, env, input, capture_output, check):
        captured["env"] = env
        captured["args"] = args
        return FakeResult()

    monkeypatch.setattr(gh_cli.subprocess, "run", fake_run)

    gh_cli._gh(["pr", "view", "1"], host="github.example.com", token="abc")

    assert captured["args"] == ["gh", "pr", "view", "1"]
    assert captured["env"]["GH_TOKEN"] == "abc"
    assert captured["env"]["GH_HOST"] == "github.example.com"


def test_gh_subprocess_raises_on_nonzero(monkeypatch):
    class FakeResult:
        returncode = 1
        stdout = b''
        stderr = b'permission denied'

    monkeypatch.setattr(
        gh_cli.subprocess,
        "run",
        lambda *a, **k: FakeResult(),
    )

    with pytest.raises(gh_cli.GitHubError, match="permission denied"):
        gh_cli._gh(["pr", "view", "1"], host="github.com", token=None)


# ---------------------------------------------------------------------------
# Discussion normalization (PR review thread + issue comment)
# ---------------------------------------------------------------------------


def test_norm_review_thread_preserves_resolved_and_position():
    thread = {
        "id": "PRRT_xyz",
        "isResolved": True,
        "comments": {
            "nodes": [
                {
                    "author": {"login": "alice"},
                    "body": "fix this",
                    "createdAt": "2026-05-01T10:00:00Z",
                    "path": "src/x.py",
                    "line": 42,
                    "originalLine": 40,
                },
            ]
        },
    }
    norm = gh_cli._norm_review_thread(thread)
    assert norm["id"] == "PRRT_xyz"
    assert norm["resolved"] is True
    note = norm["notes"][0]
    assert note["body"] == "fix this"
    assert note["position"]["new_path"] == "src/x.py"
    assert note["position"]["new_line"] == 42


def test_norm_pr_list_entry_includes_draft_flag():
    pr = {
        "number": 5,
        "title": "WIP",
        "state": "OPEN",
        "author": {"login": "alice", "name": "Alice"},
        "createdAt": "2026-05-01T10:00:00Z",
        "headRefName": "feat",
        "baseRefName": "main",
        "isDraft": True,
        "mergeable": "MERGEABLE",
        "url": "https://github.com/o/r/pull/5",
    }
    norm = gh_cli._norm_pr_list_entry(pr)
    assert norm["draft"] is True
    assert norm["work_in_progress"] is True
    assert norm["has_conflicts"] is False


# ---------------------------------------------------------------------------
# Formatters smoke (they should not crash on normalized output)
# ---------------------------------------------------------------------------


def test_format_pr_info_text_runs():
    norm = gh_cli._norm_pr_info(SAMPLE_GH_PR)
    out = gh_cli.format_pr_info(norm, json_output=False)
    assert "Pull Request #42" in out
    assert "Add feature X" in out


def test_format_pr_info_json_is_valid():
    norm = gh_cli._norm_pr_info(SAMPLE_GH_PR)
    out = gh_cli.format_pr_info(norm, json_output=True)
    assert json.loads(out)["iid"] == 42


def test_format_issue_info_text_runs():
    norm = gh_cli._norm_issue_info(SAMPLE_GH_ISSUE)
    out = gh_cli.format_issue_info(norm, json_output=False)
    assert "Issue #7" in out


def test_format_pr_list_empty():
    assert "No open pull requests" in gh_cli.format_pr_list([], json_output=False)


def test_format_discussions_empty():
    assert "No discussions" in gh_cli.format_discussions([], json_output=False, kind="pr")


# ---------------------------------------------------------------------------
# _split_project
# ---------------------------------------------------------------------------


def test_split_project_valid():
    assert gh_cli._split_project("owner/repo") == ("owner", "repo")


def test_split_project_rejects_no_slash():
    with pytest.raises(gh_cli.GitHubError, match="owner/repo"):
        gh_cli._split_project("just-a-name")


# ---------------------------------------------------------------------------
# Paginated GraphQL review-thread fetcher
# ---------------------------------------------------------------------------


def _make_thread_page(thread_count: int, has_next: bool, end_cursor: str, *, thread_id_prefix="T"):
    """Build a fake GraphQL response page for reviewThreads."""
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
                        "nodes": [
                            {
                                "id": f"{thread_id_prefix}{i}",
                                "isResolved": False,
                                "comments": {
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                    "nodes": [{"author": {"login": "alice"}, "body": "c", "createdAt": "t", "path": "f", "line": 1, "originalLine": 1}],
                                },
                            }
                            for i in range(thread_count)
                        ],
                    }
                }
            }
        }
    }


def test_fetch_review_threads_walks_pagination():
    """Two pages → both pages' threads appear in the result."""
    responses = [
        _make_thread_page(2, has_next=True, end_cursor="cur1", thread_id_prefix="A"),
        _make_thread_page(1, has_next=False, end_cursor=None, thread_id_prefix="B"),
    ]
    calls = []

    def fake_gh_json(args, *, host, token):
        calls.append(args)
        return responses.pop(0)

    with patch.object(gh_cli, "_gh_json", side_effect=fake_gh_json):
        threads = gh_cli._fetch_all_review_threads(
            "o/r", 1, host="github.com", token=None, page_size=2, max_pages=10
        )

    assert [t["id"] for t in threads] == ["A0", "A1", "B0"]
    # Second call must include the cursor from the first response
    assert "-f" in calls[1] and any("cursor=cur1" in a for a in calls[1])


def test_fetch_review_threads_warns_and_stops_at_max_pages(capsys):
    """When max_pages cap is hit with more results available, stderr warns."""
    # All pages claim hasNextPage=True; max_pages=2 → fetch 2 pages then warn.
    response = _make_thread_page(1, has_next=True, end_cursor="c", thread_id_prefix="P")

    with patch.object(gh_cli, "_gh_json", return_value=response):
        threads = gh_cli._fetch_all_review_threads(
            "o/r", 1, host="github.com", token=None, page_size=1, max_pages=2
        )

    assert len(threads) == 2  # one per page * 2 pages
    err = capsys.readouterr().err
    assert "page cap (2) reached" in err


def test_fetch_review_threads_walks_per_thread_reply_pagination():
    """If a thread has hasNextPage=True on its comments, walk those too."""
    first_page = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "id": "T1",
                                "isResolved": False,
                                "comments": {
                                    "pageInfo": {"hasNextPage": True, "endCursor": "r1"},
                                    "nodes": [{"body": "reply 0"}],
                                },
                            }
                        ],
                    }
                }
            }
        }
    }
    reply_page = {
        "data": {
            "node": {
                "comments": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [{"body": "reply 1"}, {"body": "reply 2"}],
                }
            }
        }
    }

    responses = [first_page, reply_page]
    with patch.object(gh_cli, "_gh_json", side_effect=lambda *a, **k: responses.pop(0)):
        threads = gh_cli._fetch_all_review_threads(
            "o/r", 1, host="github.com", token=None, page_size=10, max_pages=10
        )

    # All 3 replies should be flattened into the thread
    bodies = [c["body"] for c in threads[0]["comments"]["nodes"]]
    assert bodies == ["reply 0", "reply 1", "reply 2"]


def test_cmd_pr_list_warns_when_cap_hit(capsys):
    """If gh returns exactly the effective limit, warn about possible truncation."""
    fake_prs = [
        {
            "number": i,
            "title": "t",
            "state": "OPEN",
            "author": {"login": "u"},
            "createdAt": "",
            "headRefName": "h",
            "baseRefName": "main",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "url": "u",
        }
        for i in range(6)
    ]

    captured = {}

    def fake_gh_json(args, *, host, token):
        captured["args"] = args
        return fake_prs

    with patch.object(gh_cli, "_gh_json", side_effect=fake_gh_json):
        prs = gh_cli.cmd_pr_list(
            "o/r", host="github.com", token=None, page_size=2, max_pages=3
        )

    assert len(prs) == 6
    # effective_limit = page_size * max_pages = 6, passed to `gh pr list --limit`
    limit_idx = captured["args"].index("--limit") + 1
    assert captured["args"][limit_idx] == "6"
    err = capsys.readouterr().err
    assert "returned 6 results" in err


def test_cmd_pr_list_no_warn_below_cap(capsys):
    fake_prs = [
        {
            "number": 1,
            "title": "t",
            "state": "OPEN",
            "author": {"login": "u"},
            "createdAt": "",
            "headRefName": "h",
            "baseRefName": "main",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "url": "u",
        }
    ]
    with patch.object(gh_cli, "_gh_json", return_value=fake_prs):
        gh_cli.cmd_pr_list(
            "o/r", host="github.com", token=None, page_size=20, max_pages=25
        )
    assert capsys.readouterr().err == ""


def test_cli_parser_accepts_page_size_and_max_pages():
    """The new gitlab-parity flags must be in the argparse surface."""
    parser = gh_cli.create_parser()
    args = parser.parse_args(["o/r", "1", "--comments", "--page-size", "50", "--max-pages", "5"])
    assert args.page_size == 50
    assert args.max_pages == 5


# ---------------------------------------------------------------------------
# --resolve-thread resolution-policy guard (Thread Resolution Policy,
# rules/git.md): only review sessions may resolve; LMER_TASK names the
# session type.
# ---------------------------------------------------------------------------


RESOLVE_MUTATION_RESPONSE = {
    "data": {"resolveReviewThread": {"thread": {"id": "PRRT_1", "isResolved": True}}}
}


def _run_resolve_thread(monkeypatch):
    """Invoke main() as `github-review o/r 1 --resolve-thread PRRT_1` with
    the GraphQL layer mocked; returns (exit_code, gh_json_mock)."""
    monkeypatch.setattr(
        sys, "argv", ["github-review", "o/r", "1", "--resolve-thread", "PRRT_1"]
    )
    with patch.object(
        gh_cli, "_gh_json", return_value=RESOLVE_MUTATION_RESPONSE
    ) as mock_gh_json:
        rc = gh_cli.main()
    return rc, mock_gh_json


def test_resolve_thread_refused_in_non_review_session(monkeypatch, capsys):
    monkeypatch.setenv("LMER_TASK", "develop")
    rc, mock_gh_json = _run_resolve_thread(monkeypatch)

    assert rc == 1
    mock_gh_json.assert_not_called()  # refused before any API call
    err = capsys.readouterr().err
    # Names the session type and the policy...
    assert "'develop' session" in err
    assert "Thread Resolution Policy" in err
    assert "rules/git.md" in err
    # ...states the author workflow (reply with fix + SHA, leave open)...
    assert "commit SHA" in err
    assert "leaves the thread open" in err
    assert "only the" in err and "reviewer resolves" in err
    # ...and the workarounds (human web UI / host shell without LMER_TASK).
    assert "web UI" in err
    assert "LMER_TASK is unset" in err


def test_resolve_thread_allowed_when_lmer_task_unset(monkeypatch, capsys):
    monkeypatch.delenv("LMER_TASK", raising=False)
    rc, mock_gh_json = _run_resolve_thread(monkeypatch)

    assert rc == 0
    mock_gh_json.assert_called_once()
    assert "Resolved thread PRRT_1" in capsys.readouterr().out


def test_resolve_thread_allowed_in_review_session(monkeypatch, capsys):
    monkeypatch.setenv("LMER_TASK", "review")
    rc, mock_gh_json = _run_resolve_thread(monkeypatch)

    assert rc == 0
    mock_gh_json.assert_called_once()
    assert "Resolved thread PRRT_1" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# --info thread provenance (total/unresolved/resolved + resolver breakdown;
# derived blocking_discussions_resolved)
# ---------------------------------------------------------------------------


def _thread_node(resolved, resolver=None, comments=1):
    return {
        "isResolved": resolved,
        "resolvedBy": {"login": resolver} if resolver else None,
        "comments": {"totalCount": comments},
    }


def _fake_info_gh_json(thread_nodes, *, has_next=False, fail_graphql=False):
    """Fake _gh_json for cmd_pr_info: `gh pr view` returns the sample PR,
    the GraphQL thread walk returns one page of ``thread_nodes``."""

    def fake(args, *, host, token):
        if args[0] == "pr":
            return dict(SAMPLE_GH_PR)
        if fail_graphql:
            raise gh_cli.GitHubError("graphql exploded")
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": has_next, "endCursor": "c"},
                            "nodes": thread_nodes,
                        }
                    }
                }
            }
        }

    return fake


def test_cmd_pr_info_thread_provenance_mixed_resolvers():
    nodes = [
        _thread_node(True, "bob"),
        _thread_node(True, "bob"),
        _thread_node(True, "alice"),
        _thread_node(False),
    ]
    with patch.object(gh_cli, "_gh_json", side_effect=_fake_info_gh_json(nodes)):
        info = gh_cli.cmd_pr_info("o/r", 42, host="github.com", token=None)

    assert info["thread_provenance"] == {
        "total": 4,
        "unresolved": 1,
        "resolved": 3,
        "resolvers": {"bob": 2, "alice": 1},
        "partial": False,
    }
    # An unresolved thread → blocking discussions are NOT resolved
    assert info["blocking_discussions_resolved"] is False

    out = gh_cli.format_pr_info(info, json_output=False)
    assert "=== Threads ===" in out
    assert "Total: 4 (3 resolved, 1 unresolved)" in out
    assert "  - @alice: 1" in out
    assert "  - @bob: 2" in out
    assert "counts partial" not in out


def test_cmd_pr_info_thread_provenance_truncation_marker(capsys):
    # Every page claims hasNextPage=True → max_pages cap hits → partial
    nodes = [_thread_node(True, "bob")]
    with patch.object(
        gh_cli, "_gh_json", side_effect=_fake_info_gh_json(nodes, has_next=True)
    ):
        info = gh_cli.cmd_pr_info(
            "o/r", 42, host="github.com", token=None, page_size=1, max_pages=2
        )

    assert info["thread_provenance"]["partial"] is True
    out = gh_cli.format_pr_info(info, json_output=False)
    assert "counts partial — page cap" in out


def test_cmd_pr_info_partial_walk_never_claims_all_resolved():
    """Fail-closed on truncation: a page-cap-truncated walk whose fetched
    threads are all resolved must NOT report blocking_discussions_resolved
    True — unfetched pages may hold unresolved threads."""
    nodes = [_thread_node(True, "bob")]
    with patch.object(
        gh_cli, "_gh_json", side_effect=_fake_info_gh_json(nodes, has_next=True)
    ):
        info = gh_cli.cmd_pr_info(
            "o/r", 42, host="github.com", token=None, page_size=1, max_pages=2
        )

    assert info["thread_provenance"]["partial"] is True
    assert info["thread_provenance"]["unresolved"] == 0
    assert info["blocking_discussions_resolved"] is False


def test_cmd_pr_info_blocking_discussions_resolved_when_all_resolved():
    nodes = [_thread_node(True, "bob"), _thread_node(True, "alice")]
    with patch.object(gh_cli, "_gh_json", side_effect=_fake_info_gh_json(nodes)):
        info = gh_cli.cmd_pr_info("o/r", 42, host="github.com", token=None)

    assert info["blocking_discussions_resolved"] is True
    assert info["thread_provenance"]["unresolved"] == 0


def test_cmd_pr_info_json_thread_provenance_shape():
    nodes = [_thread_node(True, "bob"), _thread_node(False)]
    with patch.object(gh_cli, "_gh_json", side_effect=_fake_info_gh_json(nodes)):
        info = gh_cli.cmd_pr_info("o/r", 42, host="github.com", token=None)

    prov = json.loads(gh_cli.format_pr_info(info, json_output=True))["thread_provenance"]
    # Same key shape as gitlab-review's thread_provenance block
    assert set(prov.keys()) == {"total", "unresolved", "resolved", "resolvers", "partial"}
    assert prov["total"] == 2
    assert prov["unresolved"] == 1
    assert prov["resolved"] == 1
    assert prov["resolvers"] == {"bob": 1}
    assert prov["partial"] is False


def test_cmd_pr_info_provenance_failure_is_fail_soft(capsys):
    """--info must never fail on provenance trouble: a GraphQL error
    degrades to issue-comment counts and no Threads block."""
    with patch.object(
        gh_cli, "_gh_json", side_effect=_fake_info_gh_json([], fail_graphql=True)
    ):
        info = gh_cli.cmd_pr_info("o/r", 42, host="github.com", token=None)

    # Fail-soft: no provenance, blocking flag stays True, count falls back
    # to issue-level comments only (SAMPLE_GH_PR has 1).
    assert info["thread_provenance"] is None
    assert info["blocking_discussions_resolved"] is True
    assert info["user_notes_count"] == 1

    out = gh_cli.format_pr_info(info, json_output=False)
    assert "=== Threads ===" not in out
    err = capsys.readouterr().err
    assert "thread provenance omitted" in err
