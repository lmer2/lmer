"""Command-line interface for GitHub code reviewer.

This is a thin wrapper around the ``gh`` CLI. It exists to provide:

- ``--review-file`` — atomic inline-review submission (N comments + summary
  in one POST), the one workflow ``gh`` has no clean native equivalent for.
- Normalized read-side output (``--info``, ``--comments``, ``--issue-info``,
  ``--issue-comments``, ``--list``) shaped close to ``gitlab-review --json``
  so downstream consumers don't have to branch on host provider.

For everything else (creating PRs/issues, editing, posting top-level
comments, closing, etc.) use ``gh`` directly — its UX is fine and there is
no value in re-wrapping it.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple


class GitHubError(Exception):
    """Raised when ``gh`` returns a non-zero exit or produces invalid output."""


# Defaults mirror gitlab_reviewer's DEFAULT_PAGE_SIZE/DEFAULT_MAX_PAGES so
# parity-tool flags behave the same across providers.
DEFAULT_PAGE_SIZE = 20
DEFAULT_MAX_PAGES = 25


def _sanitize_hostname(host: str) -> str:
    """Convert hostname to env var suffix (lowercase, dots/hyphens -> _)."""
    return re.sub(r"[.\-]", "_", host.lower())


def _resolve_token(host: str, explicit: Optional[str] = None) -> Optional[str]:
    """Look up a GitHub token for ``host`` from the environment.

    Lookup order:
      1. ``--token`` explicit override
      2. ``GH_TOKEN_<sanitized_host>`` — host-specific (parity with the
         ``GITLAB_TOKEN_<host>`` convention; lets one process talk to
         github.com and a GHE host simultaneously)
      3. ``GH_TOKEN`` — gh's own preferred env var
      4. ``GITHUB_TOKEN`` — Actions-style fallback

    Returns ``None`` if nothing matches; the caller decides whether that is
    an error (gh itself will read GH_TOKEN/GITHUB_TOKEN so we only need a
    token here when we want to override per-host).
    """
    if explicit:
        return explicit

    suffix = _sanitize_hostname(host)
    token = os.environ.get(f"GH_TOKEN_{suffix}")
    if token:
        return token

    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")


def _gh(args: List[str], *, host: str, token: Optional[str], input_data: Optional[bytes] = None) -> str:
    """Invoke ``gh`` with the given args; return stdout as text.

    Sets ``GH_TOKEN`` and ``GH_HOST`` per-call so multi-host usage works
    without ``gh auth login`` and without mutating the caller environment.

    Raises GitHubError on non-zero exit with stderr included.
    """
    env = os.environ.copy()
    if token:
        env["GH_TOKEN"] = token
    if host:
        env["GH_HOST"] = host

    try:
        result = subprocess.run(
            ["gh", *args],
            env=env,
            input=input_data,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as e:
        raise GitHubError("gh CLI not found in PATH") from e

    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        raise GitHubError(f"gh {' '.join(args)} failed (exit {result.returncode}): {stderr}")

    return (result.stdout or b"").decode("utf-8", errors="replace")


def _gh_json(args: List[str], *, host: str, token: Optional[str]) -> Any:
    """Run gh and parse stdout as JSON."""
    out = _gh(args, host=host, token=token)
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise GitHubError(f"gh returned invalid JSON: {e}") from e


# ---------------------------------------------------------------------------
# Normalization helpers — produce gitlab-review-shaped dicts so downstream
# consumers (review.json templates, log parsers) can read either provider
# without branching on field names.
# ---------------------------------------------------------------------------


def _norm_user(gh_user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalize a gh user object to {name, username}.

    gh emits ``login`` for the username and (sometimes) ``name`` for the
    display name. GitHub's "name" field can be empty for users who have not
    set it, so fall back to login.
    """
    if not gh_user:
        return {"name": "", "username": ""}
    login = gh_user.get("login", "") or ""
    name = gh_user.get("name") or login
    return {"name": name, "username": login}


def _gh_mergeable_to_gitlab(mergeable: Optional[str]) -> str:
    """Map GitHub's ``mergeable`` to GitLab's ``merge_status`` vocabulary.

    GitHub returns one of ``MERGEABLE`` / ``CONFLICTING`` / ``UNKNOWN`` /
    null. The whole point of normalization is that consumers can compare
    against gitlab values (``can_be_merged`` / ``cannot_be_merged`` /
    ``checking`` / ``unchecked``) without branching on host. A passthrough
    of the GitHub vocabulary would silently mis-classify.

    Unknown future values fall back to ``unchecked`` (defensive: don't
    crash) and emit a stderr warning so a new GitHub enum value doesn't
    silently masquerade as "not yet computed."
    """
    m = (mergeable or "").upper()
    if m == "MERGEABLE":
        return "can_be_merged"
    if m == "CONFLICTING":
        return "cannot_be_merged"
    if m == "UNKNOWN":
        return "checking"
    if m == "":
        return "unchecked"
    print(
        f"⚠️  github-review: unrecognized GitHub mergeable value {mergeable!r}; "
        f"mapping to 'unchecked'. Please file an issue so the mapping can be updated.",
        file=sys.stderr,
    )
    return "unchecked"


def _norm_pr_info(pr: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize ``gh pr view --json`` output to gitlab-review-shaped MR info."""
    reviews = pr.get("reviews") or []
    approvers: List[Dict[str, Any]] = []
    for r in reviews:
        if (r.get("state") or "").upper() == "APPROVED":
            approvers.append({"user": _norm_user(r.get("author"))})

    review_decision = (pr.get("reviewDecision") or "").upper()
    approved = review_decision == "APPROVED"

    # user_notes_count: gitlab counts both general comments and inline
    # review-thread comments. GitHub's `gh pr view --json comments` only
    # returns issue-level comments, so callers can pass an explicit
    # `_review_comment_count` (computed alongside the GraphQL review-thread
    # fetch) to make the count match gitlab semantics. When absent, fall
    # back to issue-level count only.
    issue_comments = len(pr.get("comments") or [])
    review_comments = pr.get("_review_comment_count")
    user_notes_count = (
        issue_comments + review_comments if isinstance(review_comments, int) else issue_comments
    )

    return {
        "iid": pr.get("number"),
        "title": pr.get("title", ""),
        "state": (pr.get("state") or "").lower(),
        "author": _norm_user(pr.get("author")),
        "created_at": pr.get("createdAt", ""),
        "updated_at": pr.get("updatedAt", ""),
        "merged_at": pr.get("mergedAt"),
        "source_branch": pr.get("headRefName", ""),
        "target_branch": pr.get("baseRefName", ""),
        "merge_status": _gh_mergeable_to_gitlab(pr.get("mergeable")),
        "draft": bool(pr.get("isDraft")),
        "work_in_progress": bool(pr.get("isDraft")),
        "has_conflicts": (pr.get("mergeable") or "").upper() == "CONFLICTING",
        "blocking_discussions_resolved": True,  # github has no direct equivalent
        "description": pr.get("body", "") or "",
        "approvals": {
            "approved": approved,
            "approvals_required": 0,
            "approvals_left": 0,
            "approved_by": approvers,
        },
        "reviewers": [_norm_user(u) for u in (pr.get("reviewRequests") or [])],
        "assignees": [_norm_user(u) for u in (pr.get("assignees") or [])],
        "participants": [_norm_user(u) for u in (pr.get("participants") or [])],
        "user_notes_count": user_notes_count,
        "upvotes": 0,
        "downvotes": 0,
        "changes_count": pr.get("changedFiles"),
        "web_url": pr.get("url", ""),
    }


def _norm_issue_info(issue: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize ``gh issue view --json`` output to gitlab-review-shaped issue info."""
    milestone = issue.get("milestone")
    norm_milestone = None
    if milestone:
        norm_milestone = {
            "title": milestone.get("title", ""),
            "state": milestone.get("state", ""),
            "due_date": milestone.get("dueOn"),
        }

    return {
        "iid": issue.get("number"),
        "title": issue.get("title", ""),
        "state": (issue.get("state") or "").lower(),
        "author": _norm_user(issue.get("author")),
        "created_at": issue.get("createdAt", ""),
        "updated_at": issue.get("updatedAt", ""),
        "closed_at": issue.get("closedAt"),
        "due_date": None,
        "description": issue.get("body", "") or "",
        "labels": [lbl.get("name", "") for lbl in (issue.get("labels") or [])],
        "assignees": [_norm_user(u) for u in (issue.get("assignees") or [])],
        "milestone": norm_milestone,
        "user_notes_count": len(issue.get("comments") or []),
        "upvotes": 0,
        "downvotes": 0,
        "merge_requests_count": 0,
        "web_url": issue.get("url", ""),
    }


def _norm_pr_list_entry(pr: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a single ``gh pr list`` entry."""
    return {
        "iid": pr.get("number"),
        "title": pr.get("title", ""),
        "state": (pr.get("state") or "").lower(),
        "source_branch": pr.get("headRefName", ""),
        "target_branch": pr.get("baseRefName", ""),
        "author": _norm_user(pr.get("author")),
        "created_at": pr.get("createdAt", ""),
        "draft": bool(pr.get("isDraft")),
        "work_in_progress": bool(pr.get("isDraft")),
        "has_conflicts": (pr.get("mergeable") or "").upper() == "CONFLICTING",
        "web_url": pr.get("url", ""),
    }


def _norm_pr_comment_to_discussion(comment: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a PR issue-comment to a discussion with one note."""
    note = {
        "author": _norm_user(comment.get("author")),
        "created_at": comment.get("createdAt", ""),
        "body": comment.get("body", ""),
        "system": False,
    }
    # gh's `gh pr view --comments` doesn't include a stable thread id; use
    # the node-id-style "id" field if present.
    discussion_id = str(comment.get("id") or comment.get("url") or "")
    return {
        "id": discussion_id,
        "resolved": False,
        "notes": [note],
    }


def _norm_review_thread(thread: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a GraphQL PR review thread to a gitlab-style discussion."""
    notes = []
    for c in thread.get("comments", {}).get("nodes", []) or []:
        author_login = (c.get("author") or {}).get("login", "")
        notes.append(
            {
                "author": {"name": author_login, "username": author_login},
                "created_at": c.get("createdAt", ""),
                "body": c.get("body", ""),
                "position": {
                    "new_path": c.get("path", ""),
                    "new_line": c.get("line"),
                    "old_path": c.get("path", ""),
                    "old_line": c.get("originalLine"),
                }
                if c.get("path")
                else None,
                "system": False,
            }
        )
    return {
        "id": thread.get("id", ""),
        "resolved": bool(thread.get("isResolved")),
        "notes": notes,
    }


def _norm_issue_comment_to_discussion(comment: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a single issue comment to a discussion."""
    note = {
        "author": _norm_user(comment.get("author")),
        "created_at": comment.get("createdAt", ""),
        "body": comment.get("body", ""),
        "system": False,
    }
    return {
        "id": str(comment.get("id") or comment.get("url") or ""),
        "resolved": False,
        "notes": [note],
    }


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def _count_review_thread_comments(
    project: str,
    pr_id: int,
    host: str,
    token: Optional[str],
    page_size: int,
    max_pages: int,
) -> int:
    """Return the total number of inline review-thread comments on a PR.

    Uses ``comments(first: 0) { totalCount }`` per thread so we never
    transfer comment bodies — only their counts. Thread pages are walked
    with the same cursor pagination as ``_fetch_all_review_threads`` so
    PRs with many threads are not silently undercounted. This is the
    cheap path used by ``cmd_pr_info`` to compute ``user_notes_count``
    without paying for full thread bodies.
    """
    owner, name = _split_project(project)
    query = """
    query($owner: String!, $name: String!, $number: Int!, $page: Int!, $cursor: String) {
      repository(owner: $owner, name: $name) {
        pullRequest(number: $number) {
          reviewThreads(first: $page, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              comments(first: 0) { totalCount }
            }
          }
        }
      }
    }
    """
    total = 0
    cursor: Optional[str] = None
    pages = 0
    while True:
        args = [
            "api",
            "graphql",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={pr_id}",
            "-F",
            f"page={page_size}",
            "-f",
            f"query={query}",
        ]
        if cursor:
            args += ["-f", f"cursor={cursor}"]
        data = _gh_json(args, host=host, token=token)
        rt = (
            data.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
        ) or {}
        for thread in rt.get("nodes") or []:
            total += (thread.get("comments") or {}).get("totalCount", 0) or 0
        pages += 1
        page_info = rt.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        if pages >= max_pages:
            print(
                f"⚠️  github-review: reviewThreads page cap ({max_pages}) reached "
                f"while counting comments; user_notes_count may undercount.",
                file=sys.stderr,
            )
            break
        cursor = page_info.get("endCursor")
    return total


def _fetch_all_review_threads(
    project: str,
    pr_id: int,
    host: str,
    token: Optional[str],
    page_size: int,
    max_pages: int,
) -> List[Dict[str, Any]]:
    """Fetch PR review threads with cursor pagination at both levels.

    Walks ``reviewThreads`` pages until exhausted or ``max_pages`` is hit.
    For any thread whose ``comments.pageInfo.hasNextPage`` is true, walks
    its replies the same way. If either cap is hit while more results
    exist, prints a stderr warning naming the cap (parity with
    ``gitlab-review``'s pagination behavior).
    """
    owner, name = _split_project(project)
    threads_query = """
    query($owner: String!, $name: String!, $number: Int!, $page: Int!, $cursor: String) {
      repository(owner: $owner, name: $name) {
        pullRequest(number: $number) {
          reviewThreads(first: $page, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id
              isResolved
              comments(first: $page) {
                pageInfo { hasNextPage endCursor }
                nodes {
                  author { login }
                  body
                  createdAt
                  path
                  line
                  originalLine
                }
              }
            }
          }
        }
      }
    }
    """

    threads: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    pages = 0
    while True:
        args = [
            "api",
            "graphql",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={pr_id}",
            "-F",
            f"page={page_size}",
            "-f",
            f"query={threads_query}",
        ]
        if cursor:
            args += ["-f", f"cursor={cursor}"]
        data = _gh_json(args, host=host, token=token)
        rt = (
            data.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
        ) or {}
        threads.extend(rt.get("nodes") or [])
        pages += 1
        page_info = rt.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        if pages >= max_pages:
            print(
                f"⚠️  github-review: reviewThreads page cap ({max_pages}) reached "
                f"with more results available; raise --max-pages or --page-size to fetch them all.",
                file=sys.stderr,
            )
            break
        cursor = page_info.get("endCursor")

    # Walk per-thread reply pagination for any thread whose first page was capped.
    reply_query = """
    query($id: ID!, $page: Int!, $cursor: String) {
      node(id: $id) {
        ... on PullRequestReviewThread {
          comments(first: $page, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              author { login }
              body
              createdAt
              path
              line
              originalLine
            }
          }
        }
      }
    }
    """
    for thread in threads:
        comments = thread.get("comments") or {}
        page_info = comments.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            continue
        cursor = page_info.get("endCursor")
        nodes = comments.get("nodes") or []
        reply_pages = 1  # the initial fetch above counts as page 1
        while True:
            args = [
                "api",
                "graphql",
                "-F",
                f"id={thread['id']}",
                "-F",
                f"page={page_size}",
                "-f",
                f"query={reply_query}",
            ]
            if cursor:
                args += ["-f", f"cursor={cursor}"]
            data = _gh_json(args, host=host, token=token)
            page = (
                data.get("data", {}).get("node", {}).get("comments", {})
            ) or {}
            nodes.extend(page.get("nodes") or [])
            reply_pages += 1
            pinfo = page.get("pageInfo") or {}
            if not pinfo.get("hasNextPage"):
                break
            if reply_pages >= max_pages:
                print(
                    f"⚠️  github-review: thread {thread.get('id')} reply page cap "
                    f"({max_pages}) reached with more results available.",
                    file=sys.stderr,
                )
                break
            cursor = pinfo.get("endCursor")
        thread["comments"]["nodes"] = nodes

    return threads


def cmd_pr_info(
    project: str,
    pr_id: int,
    host: str,
    token: Optional[str],
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> Dict[str, Any]:
    fields = (
        "number,title,state,author,createdAt,updatedAt,mergedAt,"
        "headRefName,baseRefName,mergeable,isDraft,body,"
        "reviewDecision,reviews,reviewRequests,assignees,"
        "comments,changedFiles,url"
    )
    pr = _gh_json(
        ["pr", "view", str(pr_id), "--repo", project, "--json", fields],
        host=host,
        token=token,
    )
    # participants is not a direct json field; derive from author + reviewers + assignees + comment authors
    participants: List[Dict[str, Any]] = []
    seen = set()
    for u in [pr.get("author")] + (pr.get("reviewRequests") or []) + (pr.get("assignees") or []):
        if not u:
            continue
        login = u.get("login")
        if login and login not in seen:
            participants.append(u)
            seen.add(login)
    for c in pr.get("comments") or []:
        a = c.get("author") or {}
        login = a.get("login")
        if login and login not in seen:
            participants.append(a)
            seen.add(login)
    pr["participants"] = participants

    # Count inline review-thread comments so user_notes_count matches
    # gitlab's "all notes on the MR" semantics. Uses the count-only path
    # (``comments(first: 0) { totalCount }``) so --info doesn't transfer
    # comment bodies — only counts. Thread pages are still walked under
    # the same --max-pages cap, with a stderr warning if hit.
    pr["_review_comment_count"] = _count_review_thread_comments(
        project, pr_id, host, token, page_size=page_size, max_pages=max_pages
    )

    return _norm_pr_info(pr)


def cmd_pr_list(
    project: str,
    host: str,
    token: Optional[str],
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> List[Dict[str, Any]]:
    """List open PRs.

    GitHub's REST PRs endpoint does not expose stable cursors via ``gh``, so
    pagination here is implemented by raising the ``--limit`` ``gh pr list``
    sees (``page_size * max_pages``) and warning if results reach that cap.
    The flag names mirror ``gitlab-review`` for parity even though the
    underlying mechanism is a single capped fetch rather than per-page
    walks.
    """
    fields = "number,title,state,author,createdAt,headRefName,baseRefName,isDraft,mergeable,url"
    effective_limit = max(1, page_size * max_pages)
    prs = _gh_json(
        [
            "pr",
            "list",
            "--repo",
            project,
            "--state",
            "open",
            "--limit",
            str(effective_limit),
            "--json",
            fields,
        ],
        host=host,
        token=token,
    ) or []
    if len(prs) >= effective_limit:
        print(
            f"⚠️  github-review: --list returned {effective_limit} results "
            f"(page-size {page_size} × max-pages {max_pages}); more may exist. "
            f"Raise --page-size or --max-pages to widen the cap.",
            file=sys.stderr,
        )
    return [_norm_pr_list_entry(pr) for pr in prs]


def cmd_pr_comments(
    project: str,
    pr_id: int,
    host: str,
    token: Optional[str],
    unresolved_only: bool,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> List[Dict[str, Any]]:
    """Fetch PR conversations.

    GitHub distinguishes two streams:
      - Issue-level comments on the PR (``gh pr view --json comments``)
      - Pull-request review threads (inline, with resolved state — only
        reachable via GraphQL, paginated at both the thread and per-thread
        reply level via ``_fetch_all_review_threads``)

    We merge both into a single discussions list. ``unresolved_only=True``
    keeps issue comments (they have no resolved concept) but filters review
    threads to those with ``isResolved=false``.
    """
    discussions: List[Dict[str, Any]] = []

    # Issue-level comments
    pr = _gh_json(
        ["pr", "view", str(pr_id), "--repo", project, "--json", "comments"],
        host=host,
        token=token,
    )
    for c in pr.get("comments") or []:
        discussions.append(_norm_pr_comment_to_discussion(c))

    # Inline review threads via paginated GraphQL
    threads = _fetch_all_review_threads(
        project, pr_id, host, token, page_size=page_size, max_pages=max_pages
    )
    for t in threads:
        if unresolved_only and t.get("isResolved"):
            continue
        discussions.append(_norm_review_thread(t))

    return discussions


def cmd_issue_info(project: str, issue_id: int, host: str, token: Optional[str]) -> Dict[str, Any]:
    fields = (
        "number,title,state,author,createdAt,updatedAt,closedAt,"
        "body,labels,assignees,milestone,comments,url"
    )
    issue = _gh_json(
        ["issue", "view", str(issue_id), "--repo", project, "--json", fields],
        host=host,
        token=token,
    )
    return _norm_issue_info(issue)


def cmd_issue_comments(
    project: str, issue_id: int, host: str, token: Optional[str]
) -> List[Dict[str, Any]]:
    issue = _gh_json(
        ["issue", "view", str(issue_id), "--repo", project, "--json", "comments"],
        host=host,
        token=token,
    )
    return [_norm_issue_comment_to_discussion(c) for c in (issue.get("comments") or [])]


def cmd_review_file(
    project: str,
    pr_id: int,
    review_file: str,
    host: str,
    token: Optional[str],
) -> Dict[str, Any]:
    """Submit an atomic review (N inline comments + summary) via the GitHub
    Reviews REST API.

    Accepts the same JSON contract as ``gitlab-review --review-file``::

        {
          "inline_comments": [
            {"file_path": "src/x.py", "line_number": 12, "comment": "..."}
          ],
          "summary": "..."
        }

    Optional extension (GitHub-specific): each inline comment may carry
    ``"side": "LEFT"|"RIGHT"`` (default RIGHT — post-change). ``"event"``
    at the top level overrides the review event (default ``COMMENT``;
    other valid values are ``APPROVE`` and ``REQUEST_CHANGES``).
    """
    inline_comments, summary, event = load_review_from_file(review_file)

    body_payload: Dict[str, Any] = {
        "body": summary or "",
        "event": event,
    }
    if inline_comments:
        body_payload["comments"] = [
            {
                "path": c["file_path"],
                "line": c["line_number"],
                "side": c.get("side", "RIGHT"),
                "body": c["comment"],
            }
            for c in inline_comments
        ]

    payload_bytes = json.dumps(body_payload).encode("utf-8")

    out = _gh(
        [
            "api",
            "-X",
            "POST",
            f"repos/{project}/pulls/{pr_id}/reviews",
            "--input",
            "-",
        ],
        host=host,
        token=token,
        input_data=payload_bytes,
    )

    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise GitHubError(f"GitHub returned invalid JSON for review: {e}") from e


def load_review_from_file(
    file_path: str,
) -> Tuple[List[Dict[str, Any]], Optional[str], str]:
    """Load a review JSON file. Returns (inline_comments, summary, event).

    Mirrors the contract used by ``gitlab-review --review-file`` with one
    GitHub-specific addition: top-level ``"event"`` (default ``COMMENT``).
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise GitHubError(f"Review file not found: {file_path}")
    except json.JSONDecodeError as e:
        raise GitHubError(f"Invalid JSON in review file: {e}")

    inline = data.get("inline_comments", []) or []
    if not isinstance(inline, list):
        raise GitHubError("'inline_comments' must be a list")

    parsed: List[Dict[str, Any]] = []
    for i, c in enumerate(inline):
        if not isinstance(c, dict):
            raise GitHubError(f"inline_comments[{i}] must be an object")
        for field in ("file_path", "line_number", "comment"):
            if field not in c:
                raise GitHubError(f"inline_comments[{i}] missing required field: {field}")
        try:
            line_number = int(c["line_number"])
        except (ValueError, TypeError):
            raise GitHubError(f"inline_comments[{i}].line_number must be an integer")
        entry = {
            "file_path": str(c["file_path"]),
            "line_number": line_number,
            "comment": str(c["comment"]),
        }
        if "side" in c:
            side = str(c["side"]).upper()
            if side not in ("LEFT", "RIGHT"):
                raise GitHubError(f"inline_comments[{i}].side must be LEFT or RIGHT")
            entry["side"] = side
        parsed.append(entry)

    summary = data.get("summary")
    if summary is not None:
        summary = str(summary)

    event = str(data.get("event", "COMMENT")).upper()
    if event not in ("COMMENT", "APPROVE", "REQUEST_CHANGES"):
        raise GitHubError("'event' must be one of: COMMENT, APPROVE, REQUEST_CHANGES")

    return parsed, summary, event


def cmd_resolve_thread(thread_id: str, host: str, token: Optional[str]) -> Dict[str, Any]:
    """Resolve a PR review thread by node id via GraphQL."""
    query = "mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { id isResolved } } }"
    data = _gh_json(
        [
            "api",
            "graphql",
            "-F",
            f"id={thread_id}",
            "-f",
            f"query={query}",
        ],
        host=host,
        token=token,
    )
    return data.get("data", {}).get("resolveReviewThread", {}).get("thread", {})


def _split_project(project: str) -> Tuple[str, str]:
    parts = project.split("/", 1)
    if len(parts) != 2:
        raise GitHubError(f"Invalid project (expected owner/repo): {project!r}")
    return parts[0], parts[1]


# ---------------------------------------------------------------------------
# Output formatting — text shapes mirror gitlab-review's --no-json output
# so a human (or claude) reading either is comfortable.
# ---------------------------------------------------------------------------


def format_pr_info(info: Dict[str, Any], json_output: bool) -> str:
    if json_output:
        return json.dumps(info, indent=2)
    lines = [
        f"=== Pull Request #{info['iid']} ===",
        f"Title: {info['title']}",
        f"State: {info['state']}",
        f"Author: {info['author']['name']} (@{info['author']['username']})",
        f"Created: {info['created_at']}",
        f"Updated: {info['updated_at']}",
    ]
    if info.get("merged_at"):
        lines.append(f"Merged: {info['merged_at']}")
    lines += [
        "",
        "=== Branches ===",
        f"Source: {info['source_branch']}",
        f"Target: {info['target_branch']}",
        f"Merge Status: {info['merge_status']}",
    ]
    if info.get("draft"):
        lines.append("Status: Draft")
    lines += [
        "",
        "=== Approvals ===",
        f"Approved: {'Yes' if info['approvals']['approved'] else 'No'}",
    ]
    if info["approvals"].get("approved_by"):
        lines.append("Approved By:")
        for approver in info["approvals"]["approved_by"]:
            user = approver.get("user", approver)
            lines.append(f"  - {user['name']} (@{user['username']})")
    if info.get("reviewers"):
        lines += ["", "=== Reviewers ==="]
        for r in info["reviewers"]:
            lines.append(f"  - {r['name']} (@{r['username']})")
    if info.get("assignees"):
        lines += ["", "=== Assignees ==="]
        for a in info["assignees"]:
            lines.append(f"  - {a['name']} (@{a['username']})")
    if info.get("description"):
        lines += ["", "=== Description ===", info["description"]]
    lines += [
        "",
        "=== Statistics ===",
        f"Changes: {info.get('changes_count', 'N/A')}",
        f"Comments: {info.get('user_notes_count', 0)}",
        f"Has Conflicts: {'Yes' if info.get('has_conflicts') else 'No'}",
        "",
        f"Web URL: {info['web_url']}",
    ]
    return "\n".join(lines)


def format_pr_list(prs: List[Dict[str, Any]], json_output: bool) -> str:
    if json_output:
        return json.dumps(prs, indent=2)
    if not prs:
        return "No open pull requests found."
    lines = [f"Found {len(prs)} open pull request(s):", ""]
    for pr in prs:
        title = pr["title"]
        if len(title) > 60:
            title = title[:57] + "..."
        flags = []
        if pr.get("draft"):
            flags.append("DRAFT")
        if pr.get("has_conflicts"):
            flags.append("CONFLICTS")
        status = f" [{', '.join(flags)}]" if flags else ""
        lines.append(
            f"#{pr['iid']:>3} | {pr['source_branch']:>20} → {pr['target_branch']:<20} | {title}{status}"
        )
        lines.append(f"     | {pr['author']['name']} | {pr['created_at'][:10]}")
        lines.append("")
    return "\n".join(lines)


def format_issue_info(info: Dict[str, Any], json_output: bool) -> str:
    if json_output:
        return json.dumps(info, indent=2)
    lines = [
        f"=== Issue #{info['iid']} ===",
        f"Title: {info['title']}",
        f"State: {info['state']}",
        f"Author: {info['author']['name']} (@{info['author']['username']})",
        f"Created: {info['created_at']}",
        f"Updated: {info['updated_at']}",
    ]
    if info.get("closed_at"):
        lines.append(f"Closed: {info['closed_at']}")
    lines += ["", "=== Description ==="]
    lines.append(info.get("description") or "No description provided.")
    if info.get("labels"):
        lines += ["", "=== Labels ==="]
        for lbl in info["labels"]:
            lines.append(f"  - {lbl}")
    if info.get("assignees"):
        lines += ["", "=== Assignees ==="]
        for a in info["assignees"]:
            lines.append(f"  - {a['name']} (@{a['username']})")
    if info.get("milestone"):
        m = info["milestone"]
        lines += ["", "=== Milestone ===", f"  - {m['title']} ({m['state']})"]
        if m.get("due_date"):
            lines.append(f"    Due: {m['due_date']}")
    lines += [
        "",
        "=== Statistics ===",
        f"Comments: {info.get('user_notes_count', 0)}",
        "",
        f"Web URL: {info['web_url']}",
    ]
    return "\n".join(lines)


def format_discussions(discussions: List[Dict[str, Any]], json_output: bool, kind: str) -> str:
    if json_output:
        return json.dumps(discussions, indent=2)
    if not discussions:
        return "No discussions found."
    lines = [f"Found {len(discussions)} discussion(s):", ""]
    for i, d in enumerate(discussions):
        lines.append(f"=== Discussion {i+1} (ID: {d.get('id', 'Unknown')}) ===")
        if kind == "pr":
            status = "✅ RESOLVED" if d.get("resolved") else "⏳ UNRESOLVED"
            lines.append(f"Status: {status}")
        for j, note in enumerate(d.get("notes", [])):
            position = note.get("position")
            if position:
                file_path = position.get("new_path") or position.get("old_path", "")
                line_no = position.get("new_line") or position.get("old_line", "")
                lines.append(f"📍 Inline comment on {file_path}:{line_no}")
            else:
                lines.append("💬 Comment")
            author = note.get("author", {})
            lines.append(
                f"👤 {author.get('name', '')} (@{author.get('username', '')}) - {note.get('created_at', '')[:16]}"
            )
            lines.append("")
            for line in (note.get("body") or "").split("\n"):
                lines.append(f"   {line}")
            lines.append("")
            if j < len(d.get("notes", [])) - 1:
                lines.append("   " + "-" * 40)
                lines.append("")
        lines.append("=" * 60)
        lines.append("")
    return "\n".join(lines)


def format_review_result(result: Dict[str, Any], json_output: bool) -> str:
    if json_output:
        return json.dumps(result, indent=2)
    return f"✅ Review submitted (id={result.get('id')}, state={result.get('state')})\nURL: {result.get('html_url', '')}"


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="GitHub Code Review Tool (thin wrapper around `gh`).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # View PR info
  github-review owner/repo 123 --info

  # List open PRs
  github-review owner/repo --list

  # View issue
  github-review owner/repo 42 --issue-info

  # Submit a review with N inline comments + summary
  github-review owner/repo 123 --review-file /tmp/review.json

  # Resolve a review thread (id is the GraphQL node id)
  github-review owner/repo 123 --resolve-thread <thread_id>

For create-PR, create-issue, comment, edit, etc. use `gh` directly —
this tool intentionally does not duplicate those commands.
        """,
    )
    parser.add_argument("project", help="Project path (owner/repo)")
    parser.add_argument(
        "id", type=int, nargs="?", help="PR ID or Issue ID (required for most operations)"
    )
    parser.add_argument("--host", default="github.com", help="GitHub host (default: github.com)")
    parser.add_argument("--token", help="GitHub token (default: GH_TOKEN env var)")

    parser.add_argument("--info", action="store_true", help="Get pull request information")
    parser.add_argument(
        "--comments",
        action="store_true",
        help="Get PR discussions (unresolved review threads + issue comments)",
    )
    parser.add_argument(
        "--all-comments",
        action="store_true",
        help="Get all PR discussions including resolved review threads",
    )
    parser.add_argument(
        "--resolve-thread",
        help="Resolve a PR review thread by GraphQL node id",
    )

    parser.add_argument("--issue-info", action="store_true", help="Get issue information")
    parser.add_argument("--issue-comments", action="store_true", help="Get issue comments")
    parser.add_argument(
        "--all-issue-comments",
        action="store_true",
        help="Alias for --issue-comments (github issue comments have no system-note layer)",
    )
    parser.add_argument(
        "--output-file", help="Write issue description to file (use with --issue-info)"
    )

    parser.add_argument("--list", action="store_true", help="List open pull requests")

    parser.add_argument(
        "--review-file",
        help='JSON file with {"inline_comments": [...], "summary": "...", "event": "COMMENT"}',
    )

    parser.add_argument("--json", action="store_true", help="Output results in JSON format")
    parser.add_argument(
        "--quiet", "-q", action="store_true", help="Suppress non-error output"
    )

    # Pagination flags — match gitlab-review for parity. For --comments and
    # --info these drive GraphQL cursor pagination; for --list they cap the
    # effective `gh pr list --limit` (gh exposes no stable cursor).
    parser.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        help=f"Items per GraphQL page when walking PR review threads or per-thread replies "
             f"(default: {DEFAULT_PAGE_SIZE}). For --list this multiplies with --max-pages to "
             f"cap how many PRs `gh pr list --limit` requests.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
        help=f"Maximum number of pages to fetch from any single list endpoint "
             f"(default: {DEFAULT_MAX_PAGES}); a stderr warning is printed when the cap is hit.",
    )

    return parser


def main() -> int:
    parser = create_parser()
    args = parser.parse_args()

    host = args.host or "github.com"
    token = _resolve_token(host, args.token)

    try:
        if args.list:
            prs = cmd_pr_list(
                args.project,
                host=host,
                token=token,
                page_size=args.page_size,
                max_pages=args.max_pages,
            )
            if not args.quiet:
                print(format_pr_list(prs, args.json))
            return 0

        if args.id is None:
            print("Error: PR/Issue ID is required for this operation", file=sys.stderr)
            return 1

        if args.info:
            info = cmd_pr_info(
                args.project,
                args.id,
                host=host,
                token=token,
                page_size=args.page_size,
                max_pages=args.max_pages,
            )
            if not args.quiet:
                print(format_pr_info(info, args.json))
            return 0

        if args.comments or args.all_comments:
            unresolved = not args.all_comments
            discussions = cmd_pr_comments(
                args.project,
                args.id,
                host=host,
                token=token,
                unresolved_only=unresolved,
                page_size=args.page_size,
                max_pages=args.max_pages,
            )
            if not args.quiet:
                print(format_discussions(discussions, args.json, kind="pr"))
            return 0

        if args.resolve_thread:
            result = cmd_resolve_thread(args.resolve_thread, host=host, token=token)
            if not args.quiet:
                if args.json:
                    print(json.dumps(result, indent=2))
                else:
                    print(f"✅ Resolved thread {args.resolve_thread}")
            return 0

        if args.issue_info:
            info = cmd_issue_info(args.project, args.id, host=host, token=token)
            if args.output_file:
                try:
                    with open(args.output_file, "w", encoding="utf-8") as f:
                        f.write(info.get("description", "") or "")
                    if not args.quiet:
                        print(f"Issue description written to: {args.output_file}")
                except OSError as e:
                    print(f"Error writing to file {args.output_file}: {e}", file=sys.stderr)
                    return 1
            if not args.quiet:
                print(format_issue_info(info, args.json))
            return 0

        if args.issue_comments or args.all_issue_comments:
            discussions = cmd_issue_comments(args.project, args.id, host=host, token=token)
            if not args.quiet:
                print(format_discussions(discussions, args.json, kind="issue"))
            return 0

        if args.review_file:
            result = cmd_review_file(
                args.project, args.id, args.review_file, host=host, token=token
            )
            if not args.quiet:
                print(format_review_result(result, args.json))
            return 0

        print("Error: no operation specified (try --info, --list, --review-file, ...)", file=sys.stderr)
        return 1

    except GitHubError as e:
        print(f"GitHub error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAborted by user", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
