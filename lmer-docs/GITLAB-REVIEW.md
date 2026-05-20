# GitLab Review Tool

Use `gitlab-review` to interact with GitLab merge requests and issues.

## Quick Reference

The command is available as: `gitlab-review <project> [id] [options]`

**When working with a task target URL**, extract the hostname and use `--host`:
- URL: `https://gitlab.example.com/group/project/-/merge_requests/123`
- Command: `gitlab-review group/project 123 --host gitlab.example.com --info`

## Common Operations

### View MR Information
```bash
gitlab-review myorg/myproject 123 --info --json
```
Shows description, approvals, branches, and participants.

### Get Comments/Discussions
```bash
# Unresolved comments only (default)
gitlab-review myorg/myproject 123 --comments

# All comments (resolved and unresolved)
gitlab-review myorg/myproject 123 --all-comments
```

### Resolve Comments
```bash
# Resolve a discussion thread
gitlab-review myorg/myproject 123 --resolve-thread <discussion_id>
```

### Post Review Comments

Post reviews to merge requests using a JSON file containing all review data:

```bash
gitlab-review myorg/myproject 123 --review-file /tmp/review.json
```

**Review JSON Format:**

The review file should contain inline comments and an optional summary:

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

**Example workflow:**

1. Create a review JSON file:
   ```bash
   cat > /tmp/review.json << 'EOF'
   {
     "inline_comments": [
       {
         "file_path": "src/main.py",
         "line_number": 42,
         "comment": "Fix potential null pointer exception"
       }
     ],
     "summary": "Good work! Just one minor issue to address."
   }
   EOF
   ```

2. Post the review:
   ```bash
   gitlab-review myorg/myproject 123 --review-file /tmp/review.json
   ```

### List Open MRs
```bash
gitlab-review myorg/myproject --list
```

### Close an MR
```bash
gitlab-review myorg/myproject 123 --close
```

## Issue Management

### Get Issue Information
```bash
# View issue details
gitlab-review myorg/myproject 456 --issue-info

# Save issue description to file
gitlab-review myorg/myproject 456 --issue-info --output-file issue_body.txt
```

### Create Issue
```bash
# With description from file
gitlab-review myorg/myproject --create-issue \
  --title "Bug Report" \
  --description-file bug_details.md

# With template
gitlab-review group/project --create-issue \
  --title "Deploy Release YYYYMM" \
  --template deploy
```

### List Issue Templates
```bash
gitlab-review group/project --list-templates
```

### Get Issue Comments/Discussions
```bash
# User comments only (excludes system notes like label changes)
gitlab-review myorg/myproject 456 --issue-comments

# All comments including system notes
gitlab-review myorg/myproject 456 --all-issue-comments
```

### Post Comment to Issue
```bash
# Post a comment from a file
gitlab-review myorg/myproject 456 --issue-comment-file comment.md

# Reply to an existing discussion thread
gitlab-review myorg/myproject 456 --issue-comment-file reply.md --reply-issue-thread <discussion_id>
```

### Edit Issue
```bash
# Edit title
gitlab-review group/project 42 --edit-issue --title "Updated Title"

# Edit description from file
gitlab-review group/project 42 --edit-issue --description-file content.md

# Edit both
gitlab-review group/project 42 --edit-issue \
  --title "New Title" \
  --description-file content.md
```

## Merge Request Management

### Create MR
```bash
# Basic MR
gitlab-review myorg/myproject --create-mr \
  --title "Feature: Add new functionality" \
  --source-branch feature-branch \
  --target-branch main

# With description and options
gitlab-review myorg/myproject --create-mr \
  --title "Bug fix" \
  --source-branch bugfix \
  --target-branch develop \
  --description-file mr_description.md \
  --draft \
  --remove-source-branch
```

### Edit MR
```bash
# Edit title
gitlab-review myorg/myproject 123 --edit-mr --title "Updated MR Title"

# Edit description from file
gitlab-review myorg/myproject 123 --edit-mr --description-file mr_description.md

# Retarget to different branch
gitlab-review myorg/myproject 123 --edit-mr --target-branch develop

# Edit both title and description
gitlab-review myorg/myproject 123 --edit-mr \
  --title "New MR Title" \
  --description-file content.md
```

## Configuration

### GitLab Host

**Important**: When working with a task target URL (e.g., `https://gitlab.example.com/group/project/-/merge_requests/123`), extract the hostname from the URL and use the `--host` flag.

The host is determined in this priority order:
1. `--host` command-line argument (recommended when task target URL is provided)
2. `GITLAB_HOST` environment variable

**Example**: For task target URL `https://gitlab.example.com/group/project/-/merge_requests/123`:
- Extract host: `gitlab.example.com`
- Use: `gitlab-review group/project 123 --host gitlab.example.com --info`

```bash
# Using --host flag (recommended)
gitlab-review myorg/myproject 123 --host gitlab.example.com --info

# Using environment variable
GITLAB_HOST=gitlab.example.com gitlab-review myorg/myproject --list
```

### Authentication
The tool looks for authentication tokens in this order:
1. `--token` command line argument
2. Host-specific variables: `GITLAB_TOKEN_{sanitized_host}` (hostname with dots/hyphens replaced by underscores)
3. `GITLAB_TOKEN` environment variable (generic fallback)

Example: for `gitlab.example.com`, the tool checks `GITLAB_TOKEN_gitlab_example_com`, then falls back to `GITLAB_TOKEN`.

### Pagination

List endpoints (`--comments`, `--all-comments`, `--issue-comments`, `--all-issue-comments`, `--list`, `--list-templates`, and the participants list in `--info`) automatically follow GitLab's pagination across all pages. Two flags let callers tune this:

- `--page-size` — items per API page (default: `20`, matching GitLab's own default)
- `--max-pages` — safety cap on the number of pages fetched (default: `25`, giving a ~500-item default limit)

```bash
# Fetch with a larger page size to reduce round trips on busy MRs
gitlab-review myorg/myproject 123 --all-comments --page-size 100

# Raise the safety cap when an MR has many hundreds of discussions
gitlab-review myorg/myproject 123 --all-comments --max-pages 100
```

If the page cap is reached and GitLab indicates more results exist, a warning is printed to stderr so JSON consumers on stdout are unaffected but silent truncation is visible.

## Output Formats

### JSON Output
Add `--json` flag to get machine-readable output:
```bash
gitlab-review myorg/myproject 123 --info --json
```

### Quiet Mode
Suppress non-error output with `-q` or `--quiet`:
```bash
gitlab-review myorg/myproject 123 --close --quiet
```

## Full Command Reference

Run `gitlab-review --help` for complete documentation of all options.
