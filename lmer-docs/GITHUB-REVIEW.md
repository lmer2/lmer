# GitHub Review Tool

Use `github-review` to interact with GitHub pull requests and issues.

This is a **thin wrapper around `gh`**. It only fills the gap `gh` does not
have a clean native equivalent for — atomic inline-review submission — and
normalizes read-side output so consumers can read either GitLab or GitHub
output without branching on field names.

For create-PR, create-issue, post-comment, edit, close, etc. use `gh`
directly. The official CLI's UX is fine and there's no value in re-wrapping
it.

## Quick Reference

The command is available as: `github-review <project> [id] [options]`

`<project>` is `owner/repo` (e.g. `myorg/myrepo`).

**When working with a task target URL**, extract the hostname and use `--host`:
- URL: `https://github.com/myorg/myrepo/pull/123`
- Command: `github-review myorg/myrepo 123 --info` (default host is github.com)
- For GHE: `github-review myorg/myrepo 123 --host github.example.com --info`

## Common Operations

### View PR Information

```bash
github-review owner/repo 123 --info --json
```

Output shape mirrors `gitlab-review --info --json`: `iid`, `title`, `state`,
`author`, `source_branch`, `target_branch`, `approvals`, `web_url`, etc.

**Cross-provider field mapping** (so consumers can compare against GitLab
values without branching on host):

| Normalized field      | GitHub source field   | Value translation                      |
| --------------------- | --------------------- | -------------------------------------- |
| `merge_status`        | `pullRequest.mergeable` | `MERGEABLE`→`can_be_merged`, `CONFLICTING`→`cannot_be_merged`, `UNKNOWN`→`checking`, null/`""`→`unchecked` |
| `has_conflicts`       | `pullRequest.mergeable` | true when GitHub reports `CONFLICTING` |
| `user_notes_count`    | issue comments + review-thread comments | sum (matches GitLab's "all notes" semantics) |
| `blocking_discussions_resolved` | (no GitHub equivalent) | always `true` — GitHub uses required-review-decision instead |
| `upvotes` / `downvotes` | (no GitHub equivalent) | always `0` |

The `merge_status` translation is intentional: `gitlab-review` consumers
that string-compare against `can_be_merged` / `cannot_be_merged` will work
unchanged against GitHub PRs.

### List Open PRs

```bash
github-review owner/repo --list
github-review owner/repo --list --page-size 50 --max-pages 4 --json
```

`--page-size` × `--max-pages` defines the effective cap (`gh pr list` does
not expose a stable cursor, so the wrapper does a single capped fetch). A
stderr warning is printed if the cap is reached and more results may
exist, matching `gitlab-review --list`.

### Get PR Discussions

```bash
# Unresolved review threads + issue comments (default)
github-review owner/repo 123 --comments

# All discussions including resolved review threads
github-review owner/repo 123 --all-comments

# Tune pagination on busy PRs
github-review owner/repo 123 --comments --page-size 100 --max-pages 10
```

GitHub PR discussions come from two streams which `github-review` merges:
1. **Issue-level comments** on the PR — `gh pr view --json comments`
2. **Inline review threads** — fetched via paginated GraphQL so we can
   include the `isResolved` flag (which `gh pr view` does not expose).
   Both the thread list and each thread's reply list are walked with
   cursor pagination; a stderr warning is printed if `--max-pages` is
   reached with more results available.

### Resolve a Review Thread

```bash
github-review owner/repo 123 --resolve-thread <thread_id>
```

The thread id is the **GraphQL node id** (e.g. `PRRT_kwDOAB...`) returned in
`--comments` output. It is not the numeric REST comment id.

### Post a Review (Atomic Inline Comments + Summary)

This is the workflow `gh` does not have a clean equivalent for. The wrapper
accepts the **same JSON shape as `gitlab-review --review-file`**:

```bash
github-review owner/repo 123 --review-file /tmp/review.json
```

**Review JSON format:**

```json
{
  "inline_comments": [
    {
      "file_path": "src/file1.py",
      "line_number": 10,
      "comment": "Consider using a more descriptive variable name here"
    },
    {
      "file_path": "src/file2.py",
      "line_number": 45,
      "comment": "This function could benefit from error handling"
    }
  ],
  "summary": "Overall the code looks good with minor issues. Please address the inline comments before merging."
}
```

**GitHub-specific extensions** (all optional):

- Each inline comment may carry `"side": "LEFT" | "RIGHT"`. Default `RIGHT`
  (post-change). Use `LEFT` to comment on the pre-change blob.
- Top-level `"event"` overrides the review action. Valid values:
  - `"COMMENT"` (default) — leave a review with comments, no approval state
  - `"APPROVE"` — approve the PR
  - `"REQUEST_CHANGES"` — block merge until addressed

**GitHub quirks to be aware of:**

- `line` must be a line on the PR's **diff hunk**. Commenting on an
  unchanged line will be rejected with HTTP 422. To comment on a file as a
  whole (without a line anchor) use `gh api` directly with
  `subject_type: "file"` — this is outside the wrapper's scope.
- Submitting `APPROVE` with empty `summary` works; submitting
  `REQUEST_CHANGES` without a summary may be rejected.

## Issue Management

### Get Issue Information

```bash
# View issue details
github-review owner/repo 456 --issue-info

# Save issue description to file
github-review owner/repo 456 --issue-info --output-file issue_body.txt
```

### Get Issue Comments

```bash
github-review owner/repo 456 --issue-comments
```

(GitHub issue comments have no system-note layer, so `--all-issue-comments`
is accepted as an alias for `--issue-comments`.)

## Operations Intentionally Not Exposed

Use `gh` directly for:

| Operation             | `gh` command                                |
| --------------------- | ------------------------------------------- |
| Create PR             | `gh pr create`                              |
| Edit PR               | `gh pr edit`                                |
| Close PR              | `gh pr close`                               |
| Comment on PR/issue   | `gh pr comment` / `gh issue comment`        |
| Create issue          | `gh issue create`                           |
| Edit issue            | `gh issue edit`                             |
| List issues           | `gh issue list`                             |
| Reply to thread       | `gh api repos/.../pulls/comments/.../replies` |

## Configuration

### Host

Default host is `github.com`. For GitHub Enterprise Server / Cloud:

```bash
github-review myorg/myproject 123 --host github.example.com --info
```

### Authentication

The tool looks for a token in this order:
1. `--token` command-line argument
2. `GH_TOKEN_<sanitized_host>` env var (e.g. `GH_TOKEN_github_example_com`)
   — useful when one process talks to multiple GitHub hosts
3. `GH_TOKEN` (gh's preferred env var)
4. `GITHUB_TOKEN` (Actions-style fallback)

The resolved token is exported as `GH_TOKEN` to each `gh` subprocess call,
so multi-host setups do **not** require `gh auth login`.

## Output Formats

- Default: human-readable text similar to `gitlab-review`'s default output.
- `--json` — machine-readable JSON shaped close to `gitlab-review --json`.
- `--quiet` / `-q` — suppress stdout (errors still go to stderr).

## See also

- `lmer-docs/GITLAB-REVIEW.md` — the GitLab counterpart
- `gh --help` — the underlying CLI
- [GitHub Reviews REST API](https://docs.github.com/en/rest/pulls/reviews) — what `--review-file` ultimately POSTs to
