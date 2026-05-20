"""Command-line interface for GitLab code reviewer."""

import sys
import json
import argparse
from typing import List, Tuple, Optional

from .client import (
    GitLabClient,
    CodeReviewer,
    InlineComment,
    GitLabError,
    read_file_content,
    DEFAULT_PAGE_SIZE,
    DEFAULT_MAX_PAGES,
)


def create_parser() -> argparse.ArgumentParser:
    """Create CLI argument parser."""
    parser = argparse.ArgumentParser(
        description='GitLab Code Review Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # List all open merge requests
  gitlab-review myorg/myproject --list

  # Close a merge request
  gitlab-review myorg/myproject 123 --close

  # List available issue templates
  gitlab-review group/project --list-templates

  # Create a new issue with template
  gitlab-review group/project --create-issue --title "Deploy Release YYYYMM" --template deploy

  # Create a new issue with description from file
  gitlab-review group/project --create-issue --title "Bug Report" --description-file bug_details.md

  # Edit an existing issue title
  gitlab-review group/project 42 --edit-issue --title "Updated Issue Title"

  # Edit an existing issue description from file
  gitlab-review group/project 42 --edit-issue --description-file updated_description.md

  # Edit both title and description
  gitlab-review group/project 42 --edit-issue --title "New Title" --description-file content.md

  # Edit MR title
  gitlab-review myorg/myproject 123 --edit-mr --title "Updated MR Title"

  # Edit MR description from file
  gitlab-review myorg/myproject 123 --edit-mr --description-file mr_description.md

  # Edit both MR title and description
  gitlab-review myorg/myproject 123 --edit-mr --title "New MR Title" --description-file content.md

  # Retarget MR to a different branch
  gitlab-review myorg/myproject 123 --edit-mr --target-branch develop

  # Create a new merge request
  gitlab-review myorg/myproject --create-mr --title "Feature: Add new functionality" --source-branch feature-branch --target-branch main

  # Create MR with description from file and additional options
  gitlab-review myorg/myproject --create-mr --title "Bug fix" --source-branch bugfix --target-branch develop --description-file mr_description.md --draft --remove-source-branch

  # Add inline comment
  gitlab-review myorg/myproject 123 --comment src/file.py 45 "Fix this issue"

  # Add multiple comments and summary
  gitlab-review myorg/myproject 123 \\
    --comment src/file1.py 10 "Issue 1" \\
    --comment src/file2.py 20 "Issue 2" \\
    --summary "Overall the code looks good with minor issues"

  # Post review from JSON file
  gitlab-review myorg/myproject 123 --review-file /tmp/review.json

  # Get MR information
  gitlab-review myorg/myproject 123 --info

  # Get unresolved comments/discussions
  gitlab-review myorg/myproject 123 --comments

  # Get all comments/discussions (resolved and unresolved)
  gitlab-review myorg/myproject 123 --all-comments

  # Get issue information
  gitlab-review myorg/myproject 456 --issue-info

  # Get issue information and save description to file
  gitlab-review myorg/myproject 456 --issue-info --output-file issue_body.txt

  # Get issue comments/discussions (excludes system notes)
  gitlab-review myorg/myproject 456 --issue-comments

  # Get all issue comments/discussions (includes system notes)
  gitlab-review myorg/myproject 456 --all-issue-comments

  # Post a comment to an issue from a file
  gitlab-review myorg/myproject 456 --issue-comment-file comment.md

  # Reply to an existing issue discussion thread
  gitlab-review myorg/myproject 456 --issue-comment-file reply.md --reply-issue-thread abc123def

  # Use custom GitLab instance
  GITLAB_HOST=gitlab.example.com gitlab-review myorg/myproject --list
        """
    )

    parser.add_argument('project', help='Project path (e.g., group/project)')
    parser.add_argument('id', type=int, nargs='?', help='MR ID or Issue ID (optional for --list, --create-issue, --create-mr, and --list-templates; required for --edit-issue, --edit-mr, --close, --info, and --issue-info)')

    parser.add_argument('--host', help='GitLab host (default: GITLAB_HOST env var)')
    parser.add_argument('--token', help='GitLab token (default: GITLAB_TOKEN env var)')

    parser.add_argument('--comment', action='append', nargs=3,
                       metavar=('FILE', 'LINE', 'TEXT'),
                       help='Add inline comment: --comment file.py 123 "Comment text"')

    parser.add_argument('--summary', help='Add general review summary comment')

    parser.add_argument('--review-file', help='Path to JSON file containing review data. Format: {"inline_comments": [{"file_path": "file.py", "line_number": 123, "comment": "text"}], "summary": "optional summary"}')

    parser.add_argument('--info', action='store_true',
                       help='Get merge request information (approvals, branches, participants)')

    parser.add_argument('--comments', action='store_true',
                       help='Get merge request comments/discussions (unresolved by default)')

    parser.add_argument('--all-comments', action='store_true',
                       help='Get all merge request comments/discussions (resolved and unresolved)')

    parser.add_argument('--resolve-thread', help='Resolve a discussion thread by ID')

    parser.add_argument('--issue-info', action='store_true',
                       help='Get issue information (title, description, labels, assignees)')

    parser.add_argument('--issue-comments', action='store_true',
                       help='Get issue comments/discussions (excludes system notes)')

    parser.add_argument('--all-issue-comments', action='store_true',
                       help='Get all issue comments/discussions (includes system notes)')

    parser.add_argument('--issue-comment-file',
                       help='Post a comment to an issue from a file')

    parser.add_argument('--reply-issue-thread',
                       help='Reply to an issue discussion thread by ID (use with --issue-comment-file)')

    parser.add_argument('--output-file', help='Write issue description to file (use with --issue-info)')

    parser.add_argument('--list', action='store_true',
                       help='List all open merge requests in the project')

    parser.add_argument('--close', action='store_true',
                       help='Close the merge request')

    parser.add_argument('--create-issue', action='store_true',
                       help='Create a new issue in the project')

    parser.add_argument('--edit-issue', action='store_true',
                       help='Edit an existing issue in the project')

    parser.add_argument('--edit-mr', action='store_true',
                       help='Edit an existing merge request in the project')

    parser.add_argument('--create-mr', action='store_true',
                       help='Create a new merge request in the project')

    parser.add_argument('--list-templates', action='store_true',
                       help='List available issue templates in the project')

    parser.add_argument('--title', help='Title (required with --create-issue and --create-mr, optional with --edit-issue and --edit-mr)')

    parser.add_argument('--description-file', help='Path to file containing description (optional with --create-issue, --edit-issue, --edit-mr, and --create-mr)')

    parser.add_argument('--source-branch', help='Source branch name (required with --create-mr)')

    parser.add_argument('--target-branch', help='Target branch name (required with --create-mr, optional with --edit-mr, defaults to main for --create-mr)')

    parser.add_argument('--assignee-id', type=int, help='User ID to assign MR to (optional with --create-mr)')

    parser.add_argument('--reviewer-ids', nargs='*', type=int, help='User IDs to set as reviewers (optional with --create-mr)')

    parser.add_argument('--labels', nargs='*', help='Label names to apply (optional with --create-mr)')

    parser.add_argument('--milestone-id', type=int, help='Milestone ID to associate with (optional with --create-mr)')

    parser.add_argument('--remove-source-branch', action='store_true', help='Remove source branch when MR is merged (optional with --create-mr)')

    parser.add_argument('--draft', action='store_true', help='Create as draft MR (optional with --create-mr)')

    parser.add_argument('--template', help='Issue template name to use (optional with --create-issue)')

    parser.add_argument('--json', action='store_true',
                       help='Output results in JSON format')

    parser.add_argument('--quiet', '-q', action='store_true',
                       help='Suppress non-error output')

    parser.add_argument('--page-size', type=int, default=DEFAULT_PAGE_SIZE,
                       help=f'Items per page when calling paginated GitLab list endpoints '
                            f'(default: {DEFAULT_PAGE_SIZE}, GitLab max: 100)')

    parser.add_argument('--max-pages', type=int, default=DEFAULT_MAX_PAGES,
                       help=f'Maximum number of pages to fetch from any single list endpoint '
                            f'(default: {DEFAULT_MAX_PAGES}); a stderr warning is printed if '
                            f'GitLab indicates more results exist beyond this cap')

    return parser


def parse_inline_comments(comment_args: List[List[str]]) -> List[InlineComment]:
    """Parse inline comment arguments into InlineComment objects."""
    comments = []

    for file_path, line_str, comment_text in comment_args:
        try:
            line_number = int(line_str)
        except ValueError:
            raise ValueError(f"Invalid line number: {line_str}")

        comments.append(InlineComment(
            file_path=file_path,
            line_number=line_number,
            comment=comment_text
        ))

    return comments


def load_review_from_file(file_path: str) -> Tuple[List[InlineComment], Optional[str]]:
    """Load review data from JSON file.

    Args:
        file_path: Path to JSON file containing review data

    Returns:
        Tuple of (inline_comments list, summary string)

    Raises:
        ValueError: If file format is invalid
        FileNotFoundError: If file doesn't exist
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        raise FileNotFoundError(f"Review file not found: {file_path}")
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in review file: {e}")

    inline_comments = []
    summary = None

    # Parse inline comments
    if 'inline_comments' in data:
        if not isinstance(data['inline_comments'], list):
            raise ValueError("'inline_comments' must be a list")

        for i, comment_data in enumerate(data['inline_comments']):
            if not isinstance(comment_data, dict):
                raise ValueError(f"inline_comments[{i}] must be an object")

            required_fields = ['file_path', 'line_number', 'comment']
            for field in required_fields:
                if field not in comment_data:
                    raise ValueError(f"inline_comments[{i}] missing required field: {field}")

            try:
                line_number = int(comment_data['line_number'])
            except (ValueError, TypeError):
                raise ValueError(f"inline_comments[{i}].line_number must be an integer")

            inline_comments.append(InlineComment(
                file_path=str(comment_data['file_path']),
                line_number=line_number,
                comment=str(comment_data['comment'])
            ))

    # Parse summary
    if 'summary' in data:
        summary = str(data['summary']) if data['summary'] is not None else None

    return inline_comments, summary


def format_results(results: dict, json_output: bool = False) -> str:
    """Format review results for output."""
    if json_output:
        return json.dumps(results, indent=2)

    lines = []
    lines.append(f"Review completed for MR {results['mr_id']} in {results['project']}")
    lines.append(f"Base SHA: {results['base_sha']}")
    lines.append(f"Head SHA: {results['head_sha']}")
    lines.append("")

    # Inline comments
    if results['inline_comments']:
        lines.append("Inline comments:")
        for comment in results['inline_comments']:
            status = "✅" if comment['status'] == 'success' else "❌"
            lines.append(f"  {status} {comment['file']}:{comment['line']}")

    # Summary comment
    if results['summary_comment']:
        status = "✅" if results['summary_comment']['status'] == 'success' else "❌"
        lines.append(f"Summary comment: {status}")

    # Errors
    if results['errors']:
        lines.append("")
        lines.append("Errors:")
        for error in results['errors']:
            if error['type'] == 'inline_comment':
                lines.append(f"  ❌ {error['file']}:{error['line']} - {error['error']}")
            else:
                lines.append(f"  ❌ {error['type']} - {error['error']}")

    return "\n".join(lines)


def format_mr_list(mrs: List[dict], json_output: bool = False) -> str:
    """Format MR list for output."""
    if json_output:
        return json.dumps(mrs, indent=2)

    if not mrs:
        return "No open merge requests found."

    lines = []
    lines.append(f"Found {len(mrs)} open merge request(s):")
    lines.append("")

    for mr in mrs:
        title = mr['title']
        if len(title) > 60:
            title = title[:57] + "..."

        status_indicators = []
        if mr.get('draft'):
            status_indicators.append("DRAFT")
        elif mr.get('work_in_progress'):
            status_indicators.append("WIP")
        if mr.get('has_conflicts'):
            status_indicators.append("CONFLICTS")

        status_text = f" [{', '.join(status_indicators)}]" if status_indicators else ""

        lines.append(f"#{mr['iid']:>3} | {mr['source_branch']:>20} → {mr['target_branch']:<20} | {title}{status_text}")
        lines.append(f"     | {mr['author']['name']} | {mr['created_at'][:10]}")
        lines.append("")

    return "\n".join(lines)


def format_close_result(mr_data: dict, json_output: bool = False) -> str:
    """Format MR close result for output."""
    if json_output:
        return json.dumps(mr_data, indent=2)

    return f"✅ Successfully closed MR #{mr_data['iid']}: {mr_data['title']}"


def format_resolve_thread_result(discussion_data: dict, discussion_id: str, json_output: bool = False) -> str:
    """Format resolve thread result for output."""
    if json_output:
        return json.dumps(discussion_data, indent=2)

    return f"✅ Successfully resolved discussion thread {discussion_id}"


def format_template_list(templates: List[dict], json_output: bool = False) -> str:
    """Format template list for output."""
    if json_output:
        return json.dumps(templates, indent=2)

    if not templates:
        return "No issue templates found."

    lines = []
    lines.append(f"Found {len(templates)} issue template(s):")
    lines.append("")

    for template in templates:
        # Handle different possible response structures
        name = template.get('name', template.get('key', 'Unknown'))
        lines.append(f"📝 {name}")

        # Show first line of content as preview if available
        content = template.get('content') or template.get('body') or template.get('description')
        if content:
            first_line = str(content).split('\n')[0].strip()
            if first_line:
                preview = first_line[:60] + "..." if len(first_line) > 60 else first_line
                lines.append(f"   {preview}")
        lines.append("")

    return "\n".join(lines)


def format_mr_update_result(mr_data: dict, json_output: bool = False) -> str:
    """Format MR update result for output."""
    if json_output:
        return json.dumps(mr_data, indent=2)

    return f"✅ Successfully updated MR #{mr_data['iid']}: {mr_data['title']}\nURL: {mr_data['web_url']}"


def format_issue_update_result(issue_data: dict, json_output: bool = False) -> str:
    """Format issue update result for output."""
    if json_output:
        return json.dumps(issue_data, indent=2)

    return f"✅ Successfully updated issue #{issue_data['iid']}: {issue_data['title']}\nURL: {issue_data['web_url']}"


def format_issue_result(issue_data: dict, json_output: bool = False) -> str:
    """Format issue creation result for output."""
    if json_output:
        return json.dumps(issue_data, indent=2)

    return f"✅ Successfully created issue #{issue_data['iid']}: {issue_data['title']}\nURL: {issue_data['web_url']}"


def format_mr_creation_result(mr_data: dict, json_output: bool = False) -> str:
    """Format MR creation result for output."""
    if json_output:
        return json.dumps(mr_data, indent=2)

    status_indicators = []
    if mr_data.get('draft'):
        status_indicators.append("DRAFT")
    elif mr_data.get('work_in_progress'):
        status_indicators.append("WIP")

    status_text = f" [{', '.join(status_indicators)}]" if status_indicators else ""

    lines = []
    lines.append(f"✅ Successfully created MR #{mr_data['iid']}: {mr_data['title']}{status_text}")
    lines.append(f"Source: {mr_data['source_branch']} → Target: {mr_data['target_branch']}")
    lines.append(f"URL: {mr_data['web_url']}")

    return "\n".join(lines)


def format_mr_info(mr_info: dict, json_output: bool = False) -> str:
    """Format MR info for output."""
    if json_output:
        return json.dumps(mr_info, indent=2)

    lines = []
    lines.append(f"=== Merge Request #{mr_info['iid']} ===")
    lines.append(f"Title: {mr_info['title']}")
    lines.append(f"State: {mr_info['state']}")
    lines.append(f"Author: {mr_info['author']['name']} (@{mr_info['author']['username']})")
    lines.append(f"Created: {mr_info['created_at']}")
    lines.append(f"Updated: {mr_info['updated_at']}")

    if mr_info.get('merged_at'):
        lines.append(f"Merged: {mr_info['merged_at']}")

    lines.append("")
    lines.append("=== Branches ===")
    lines.append(f"Source: {mr_info['source_branch']}")
    lines.append(f"Target: {mr_info['target_branch']}")
    lines.append(f"Merge Status: {mr_info['merge_status']}")

    if mr_info.get('draft'):
        lines.append("Status: Draft")
    elif mr_info.get('work_in_progress'):
        lines.append("Status: Work in Progress")

    lines.append("")
    lines.append("=== Approvals ===")
    approvals = mr_info.get('approvals', {})
    lines.append(f"Approved: {'Yes' if approvals.get('approved') else 'No'}")
    lines.append(f"Approvals Required: {approvals.get('approvals_required', 0)}")
    lines.append(f"Approvals Left: {approvals.get('approvals_left', 0)}")

    if approvals.get('approved_by'):
        lines.append("Approved By:")
        for approver in approvals['approved_by']:
            user = approver.get('user', approver)  # Handle both formats
            lines.append(f"  - {user['name']} (@{user['username']})")

    if mr_info.get('reviewers'):
        lines.append("")
        lines.append("=== Reviewers ===")
        for reviewer in mr_info['reviewers']:
            lines.append(f"  - {reviewer['name']} (@{reviewer['username']})")

    if mr_info.get('assignees'):
        lines.append("")
        lines.append("=== Assignees ===")
        for assignee in mr_info['assignees']:
            lines.append(f"  - {assignee['name']} (@{assignee['username']})")

    if mr_info.get('participants'):
        lines.append("")
        lines.append("=== Participants ===")
        for participant in mr_info['participants']:
            lines.append(f"  - {participant['name']} (@{participant['username']})")

    lines.append("")
    lines.append("=== Statistics ===")
    lines.append(f"Changes: {mr_info.get('changes_count', 'N/A')}")
    lines.append(f"Comments: {mr_info.get('user_notes_count', 'N/A')}")
    lines.append(f"Upvotes: {mr_info.get('upvotes', 0)}")
    lines.append(f"Downvotes: {mr_info.get('downvotes', 0)}")
    lines.append(f"Has Conflicts: {'Yes' if mr_info.get('has_conflicts') else 'No'}")
    lines.append(f"Blocking Discussions Resolved: {'Yes' if mr_info.get('blocking_discussions_resolved', True) else 'No'}")

    lines.append("")
    lines.append(f"Web URL: {mr_info['web_url']}")

    return "\n".join(lines)


def format_discussions(discussions: List[dict], json_output: bool = False) -> str:
    """Format MR discussions/comments for output."""
    if json_output:
        return json.dumps(discussions, indent=2)

    if not discussions:
        return "No discussions found."

    lines = []
    lines.append(f"Found {len(discussions)} discussion(s):")
    lines.append("")

    for i, discussion in enumerate(discussions):
        # Use discussion['id'] if available, otherwise fallback to generated index (though less reliable for API calls)
        discussion_id = discussion.get('id', 'Unknown')

        lines.append(f"=== Discussion {i+1} (ID: {discussion_id}) ===")

        # Check if this is resolved
        resolved_status = "✅ RESOLVED" if discussion.get('resolved', False) else "⏳ UNRESOLVED"
        lines.append(f"Status: {resolved_status}")

        # Get discussion notes
        notes = discussion.get('notes', [])
        if not notes:
            lines.append("No notes in this discussion.")
            lines.append("")
            continue

        for j, note in enumerate(notes):
            author = note.get('author', {})
            author_name = author.get('name', 'Unknown')
            author_username = author.get('username', '')
            created_at = note.get('created_at', '')
            body = note.get('body', '')

            # Check if this is a position-based comment (inline)
            position = note.get('position')
            if position:
                file_path = position.get('new_path', position.get('old_path', 'Unknown file'))
                line_number = position.get('new_line', position.get('old_line', 'Unknown line'))
                lines.append(f"📍 Inline comment on {file_path}:{line_number}")
            else:
                lines.append(f"💬 General comment")

            lines.append(f"👤 {author_name} (@{author_username}) - {created_at[:16]}")
            lines.append("")

            # Add comment body with indentation
            if body:
                for line in body.split('\n'):
                    lines.append(f"   {line}")
            lines.append("")

            if j < len(notes) - 1:  # Add separator between notes in same discussion
                lines.append("   " + "-" * 40)
                lines.append("")

        lines.append("=" * 60)
        lines.append("")

    return "\n".join(lines)


def format_issue_discussions(discussions: List[dict], json_output: bool = False) -> str:
    """Format issue discussions/comments for output."""
    if json_output:
        return json.dumps(discussions, indent=2)

    if not discussions:
        return "No discussions found."

    lines = []
    lines.append(f"Found {len(discussions)} discussion(s):")
    lines.append("")

    for i, discussion in enumerate(discussions):
        discussion_id = discussion.get('id', 'Unknown')
        lines.append(f"=== Discussion {i+1} (ID: {discussion_id}) ===")

        notes = discussion.get('notes', [])
        if not notes:
            lines.append("No notes in this discussion.")
            lines.append("")
            continue

        for j, note in enumerate(notes):
            if note.get('system', False):
                lines.append(f"🔧 System: {note.get('body', '')}")
                lines.append("")
                continue

            author = note.get('author', {})
            author_name = author.get('name', 'Unknown')
            author_username = author.get('username', '')
            created_at = note.get('created_at', '')

            lines.append(f"💬 Comment")
            lines.append(f"👤 {author_name} (@{author_username}) - {created_at[:16]}")
            lines.append("")

            body = note.get('body', '')
            if body:
                for line in body.split('\n'):
                    lines.append(f"   {line}")
            lines.append("")

            if j < len(notes) - 1:
                lines.append("   " + "-" * 40)
                lines.append("")

        lines.append("=" * 60)
        lines.append("")

    return "\n".join(lines)


def format_issue_comment_result(note_data: dict, json_output: bool = False) -> str:
    """Format issue comment creation result for output."""
    if json_output:
        return json.dumps(note_data, indent=2)

    lines = []
    lines.append("✅ Comment posted successfully")
    lines.append(f"Note ID: {note_data.get('id', 'Unknown')}")
    author = note_data.get('author', {})
    lines.append(f"Author: {author.get('name', 'Unknown')} (@{author.get('username', '')})")
    lines.append(f"Created: {note_data.get('created_at', '')}")
    return "\n".join(lines)


def format_issue_info(issue_info: dict, json_output: bool = False) -> str:
    """Format issue info for output."""
    if json_output:
        return json.dumps(issue_info, indent=2)

    lines = []
    lines.append(f"=== Issue #{issue_info['iid']} ===")
    lines.append(f"Title: {issue_info['title']}")
    lines.append(f"State: {issue_info['state']}")
    lines.append(f"Author: {issue_info['author']['name']} (@{issue_info['author']['username']})")
    lines.append(f"Created: {issue_info['created_at']}")
    lines.append(f"Updated: {issue_info['updated_at']}")

    if issue_info.get('closed_at'):
        lines.append(f"Closed: {issue_info['closed_at']}")

    if issue_info.get('due_date'):
        lines.append(f"Due Date: {issue_info['due_date']}")

    lines.append("")
    lines.append("=== Description ===")
    description = issue_info.get('description', '')
    if description:
        lines.append(description)
    else:
        lines.append("No description provided.")

    if issue_info.get('labels'):
        lines.append("")
        lines.append("=== Labels ===")
        for label in issue_info['labels']:
            lines.append(f"  - {label}")

    if issue_info.get('assignees'):
        lines.append("")
        lines.append("=== Assignees ===")
        for assignee in issue_info['assignees']:
            lines.append(f"  - {assignee['name']} (@{assignee['username']})")

    if issue_info.get('milestone'):
        lines.append("")
        lines.append("=== Milestone ===")
        milestone = issue_info['milestone']
        lines.append(f"  - {milestone['title']} ({milestone['state']})")
        if milestone.get('due_date'):
            lines.append(f"    Due: {milestone['due_date']}")

    lines.append("")
    lines.append("=== Statistics ===")
    lines.append(f"Comments: {issue_info.get('user_notes_count', 0)}")
    lines.append(f"Upvotes: {issue_info.get('upvotes', 0)}")
    lines.append(f"Downvotes: {issue_info.get('downvotes', 0)}")
    lines.append(f"Merge Requests Count: {issue_info.get('merge_requests_count', 0)}")

    if issue_info.get('time_stats'):
        time_stats = issue_info['time_stats']
        lines.append("")
        lines.append("=== Time Tracking ===")
        if time_stats.get('time_estimate'):
            lines.append(f"Estimated Time: {time_stats['time_estimate']}s")
        if time_stats.get('total_time_spent'):
            lines.append(f"Time Spent: {time_stats['total_time_spent']}s")

    lines.append("")
    lines.append(f"Web URL: {issue_info['web_url']}")

    return "\n".join(lines)


def main() -> int:
    """Main CLI entry point."""
    parser = create_parser()
    args = parser.parse_args()

    try:
        # Create reviewer
        client = GitLabClient(host=args.host, token=args.token)
        reviewer = CodeReviewer(client)

        # Handle list request
        if args.list:
            mrs = reviewer.list_open_mrs(
                args.project, page_size=args.page_size, max_pages=args.max_pages
            )
            if not args.quiet:
                output = format_mr_list(mrs, args.json)
                print(output)
            return 0

        # Handle list templates request
        if args.list_templates:
            try:
                templates = reviewer.list_issue_templates(
                    args.project, page_size=args.page_size, max_pages=args.max_pages
                )
                if not args.quiet:
                    output = format_template_list(templates, args.json)
                    print(output)
                return 0
            except GitLabError as e:
                print(f"GitLab error listing templates: {e}", file=sys.stderr)
                return 1
            except Exception as e:
                print(f"Unexpected error listing templates: {e}", file=sys.stderr)
                return 1

        # Handle create issue request
        if args.create_issue:
            if not args.title:
                print("Error: --title is required with --create-issue", file=sys.stderr)
                return 1
            try:
                issue_data = reviewer.create_issue(args.project, args.title, None, args.template, args.description_file)
                if not args.quiet:
                    output = format_issue_result(issue_data, args.json)
                    print(output)
                return 0
            except GitLabError as e:
                print(f"GitLab error creating issue: {e}", file=sys.stderr)
                return 1
            except Exception as e:
                print(f"Unexpected error creating issue: {e}", file=sys.stderr)
                return 1

        # Handle edit issue request
        if args.edit_issue:
            if not args.id:
                print("Error: Issue ID is required with --edit-issue", file=sys.stderr)
                return 1
            if not args.title and not args.description_file:
                print("Error: At least one of --title or --description-file is required with --edit-issue", file=sys.stderr)
                return 1
            try:
                issue_data = reviewer.update_issue(args.project, args.id, args.title, None, args.description_file)
                if not args.quiet:
                    output = format_issue_update_result(issue_data, args.json)
                    print(output)
                return 0
            except GitLabError as e:
                print(f"GitLab error updating issue: {e}", file=sys.stderr)
                return 1
            except Exception as e:
                print(f"Unexpected error updating issue: {e}", file=sys.stderr)
                return 1

        # Handle edit MR request
        if args.edit_mr:
            if not args.id:
                print("Error: MR ID is required with --edit-mr", file=sys.stderr)
                return 1
            if not args.title and not args.description_file and not args.target_branch:
                print("Error: At least one of --title, --description-file, or --target-branch is required with --edit-mr", file=sys.stderr)
                return 1
            try:
                mr_data = reviewer.update_mr(args.project, args.id, args.title, None, args.description_file, args.target_branch)
                if not args.quiet:
                    output = format_mr_update_result(mr_data, args.json)
                    print(output)
                return 0
            except GitLabError as e:
                print(f"GitLab error updating MR: {e}", file=sys.stderr)
                return 1
            except Exception as e:
                print(f"Unexpected error updating MR: {e}", file=sys.stderr)
                return 1

        # Handle create MR request
        if args.create_mr:
            if not args.title:
                print("Error: --title is required with --create-mr", file=sys.stderr)
                return 1
            if not args.source_branch:
                print("Error: --source-branch is required with --create-mr", file=sys.stderr)
                return 1

            target_branch = args.target_branch or 'main'

            try:
                mr_data = reviewer.create_mr(
                    project=args.project,
                    title=args.title,
                    source_branch=args.source_branch,
                    target_branch=target_branch,
                    description_file=args.description_file,
                    assignee_id=args.assignee_id,
                    reviewer_ids=args.reviewer_ids,
                    labels=args.labels,
                    milestone_id=args.milestone_id,
                    remove_source_branch=args.remove_source_branch,
                    draft=args.draft
                )
                if not args.quiet:
                    output = format_mr_creation_result(mr_data, args.json)
                    print(output)
                return 0
            except GitLabError as e:
                print(f"GitLab error creating MR: {e}", file=sys.stderr)
                return 1
            except Exception as e:
                print(f"Unexpected error creating MR: {e}", file=sys.stderr)
                return 1

        # All operations below require ID
        if not args.id:
            print("Error: MR ID is required", file=sys.stderr)
            return 1

        # Handle close request
        if args.close:
            mr_data = reviewer.close_mr(args.project, args.id)
            if not args.quiet:
                output = format_close_result(mr_data, args.json)
                print(output)
            return 0

        # Handle resolve thread request
        if args.resolve_thread:
            discussion_data = reviewer.resolve_thread(args.project, args.id, args.resolve_thread)
            if not args.quiet:
                output = format_resolve_thread_result(discussion_data, args.resolve_thread, args.json)
                print(output)
            return 0

        # Handle info request
        if args.info:
            mr_info = reviewer.get_mr_info(
                args.project, args.id, page_size=args.page_size, max_pages=args.max_pages
            )
            if not args.quiet:
                output = format_mr_info(mr_info, args.json)
                print(output)
            return 0

        # Handle comments request
        if args.comments or args.all_comments:
            unresolved_only = not args.all_comments  # Default to unresolved only unless --all-comments is specified
            discussions = reviewer.get_mr_discussions(
                args.project, args.id, unresolved_only=unresolved_only,
                page_size=args.page_size, max_pages=args.max_pages,
            )
            if not args.quiet:
                output = format_discussions(discussions, args.json)
                print(output)
            return 0

        # Handle issue info request
        if args.issue_info:
            issue_info = reviewer.get_issue(args.project, args.id)

            # Write description to file if requested
            if args.output_file:
                try:
                    description = issue_info.get('description', '')
                    with open(args.output_file, 'w', encoding='utf-8') as f:
                        f.write(description)
                    if not args.quiet:
                        print(f"Issue description written to: {args.output_file}")
                except Exception as e:
                    print(f"Error writing to file {args.output_file}: {e}", file=sys.stderr)
                    return 1

            if not args.quiet:
                output = format_issue_info(issue_info, args.json)
                print(output)
            return 0

        # Handle issue comments request
        if args.issue_comments or args.all_issue_comments:
            include_system = args.all_issue_comments
            discussions = reviewer.get_issue_discussions(
                args.project, args.id, system=include_system,
                page_size=args.page_size, max_pages=args.max_pages,
            )
            if not args.quiet:
                output = format_issue_discussions(discussions, args.json)
                print(output)
            return 0

        # Handle reply-issue-thread without issue-comment-file
        if args.reply_issue_thread and not args.issue_comment_file:
            print("Error: --reply-issue-thread requires --issue-comment-file", file=sys.stderr)
            return 1

        # Handle posting a comment to an issue
        if args.issue_comment_file:
            try:
                body = read_file_content(args.issue_comment_file)
            except GitLabError as e:
                print(f"Error: {e}", file=sys.stderr)
                return 1

            if args.reply_issue_thread:
                # Reply to an existing discussion thread
                note_data = reviewer.reply_to_issue_discussion(
                    args.project, args.id, args.reply_issue_thread, body
                )
            else:
                # Post a new top-level comment
                note_data = reviewer.add_issue_comment(args.project, args.id, body)

            if not args.quiet:
                output = format_issue_comment_result(note_data, args.json)
                print(output)
            return 0

        # Parse inline comments and summary
        # Priority: --review-file > individual --comment/--summary args
        inline_comments = []
        summary = None

        if args.review_file:
            # Load from JSON file
            try:
                inline_comments, summary = load_review_from_file(args.review_file)
            except (FileNotFoundError, ValueError) as e:
                print(f"Error loading review file: {e}", file=sys.stderr)
                return 1
        else:
            # Use individual arguments (old way)
            if args.comment:
                inline_comments = parse_inline_comments(args.comment)
            if args.summary:
                summary = args.summary

        # Perform review
        results = reviewer.review(
            project=args.project,
            mr_id=args.id,
            inline_comments=inline_comments if inline_comments else None,
            summary=summary
        )

        # Output results
        if not args.quiet:
            output = format_results(results, args.json)
            print(output)

        # Return error code if there were errors
        return 1 if results['errors'] else 0

    except GitLabError as e:
        print(f"GitLab error: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"Invalid input: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAborted by user", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
