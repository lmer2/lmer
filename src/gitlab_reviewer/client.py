"""GitLab API client for code reviews."""

import os
import sys
import requests
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

# The token lookup rule, not a second copy of it — see _resolve_token.
# lmer_cli.tokens is stdlib-only and bin/gitlab-review puts src/ on
# PYTHONPATH, so the import costs nothing and always resolves.
from lmer_cli.tokens import _get_gitlab_token


DEFAULT_PAGE_SIZE = 20
DEFAULT_MAX_PAGES = 25


def read_file_content(file_path: str) -> str:
    """Read content from a file.

    Args:
        file_path: Path to the file to read

    Returns:
        File content as string

    Raises:
        GitLabError: If file cannot be read
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except FileNotFoundError:
        raise GitLabError(f"File not found: {file_path}")
    except Exception as e:
        raise GitLabError(f"Error reading file {file_path}: {e}")


@dataclass
class InlineComment:
    """Represents an inline comment on a specific line."""
    file_path: str
    line_number: int
    comment: str


class GitLabError(Exception):
    """Custom exception for GitLab API errors."""
    pass


class GitLabClient:
    """Simple GitLab API client focused on code review operations."""

    def __init__(self, host: str = None, token: str = None):
        """Initialize GitLab client.

        Args:
            host: GitLab host (defaults to GITLAB_HOST env var or gitlab.com)
            token: API token (defaults to host-specific GITLAB_TOKEN_{host}, or
                GITLAB_TOKEN when this host is the one that issued it)
        """
        self.host = host or os.getenv('GITLAB_HOST', 'gitlab.com')
        self.token = token or self._resolve_token(self.host)

        if not self.token:
            import re
            suffix = re.sub(r"[.\-]", "_", self.host.lower())
            raise GitLabError(
                f"GitLab token required. Set GITLAB_TOKEN_{suffix} (always applies "
                "to this host) or pass token parameter; a generic GITLAB_TOKEN is "
                "used only for its issuing host (LMER_GITLAB_TOKEN_HOST)."
            )

        self.base_url = f"https://{self.host}/api/v4"
        # True when the most recent _paginate() call stopped at the page cap
        # while GitLab indicated more results exist (i.e. results truncated).
        self.last_page_capped = False
        self.session = requests.Session()
        self.session.headers.update({
            'PRIVATE-TOKEN': self.token,
            'Content-Type': 'application/json'
        })

    @staticmethod
    def _resolve_token(host: str) -> str | None:
        """Resolve API token for a given host from environment variables.

        Delegates to the shared lookup, which keeps the precedence this used
        to implement inline — host-specific GITLAB_TOKEN_{sanitized_host}
        first, generic GITLAB_TOKEN after — but hands the generic token out
        only for the host that issued it (LMER_GITLAB_TOKEN_HOST, defaulting
        to the LMER_WORK_REPO host). Without that scoping, `--host` alone
        decided who received the PAT in a PRIVATE-TOKEN header (issue #161).
        """
        return _get_gitlab_token(host)

    def _request(self, method: str, endpoint: str, **kwargs) -> Any:
        """Make API request with error handling."""
        url = f"{self.base_url}/{endpoint}"

        try:
            response = self.session.request(method, url, **kwargs)
            response.raise_for_status()

            # Handle different response types
            if not response.content:
                return {}

            # Try JSON first
            try:
                return response.json()
            except ValueError:
                # If not JSON, return text content
                return response.text

        except requests.HTTPError as e:
            raise GitLabError(f"GitLab API error: {e}") from e
        except requests.RequestException as e:
            raise GitLabError(f"Request failed: {e}") from e

    def _paginate(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> List[Any]:
        """GET a list endpoint, following GitLab pagination across pages.

        Concatenates page results into a single flat list. Termination signals,
        in priority order: empty `X-Next-Page` header, fewer items returned than
        `page_size`, or `max_pages` reached. When the page cap is reached and
        `X-Next-Page` indicates more results exist, a warning is printed to
        stderr (silent truncation is the bug this helper is meant to prevent).
        """
        url = f"{self.base_url}/{endpoint}"
        page_params = dict(params or {})
        page_params['per_page'] = page_size

        self.last_page_capped = False
        results: List[Any] = []
        response = None

        for page in range(1, max_pages + 1):
            page_params['page'] = page
            try:
                response = self.session.request('GET', url, params=page_params)
                response.raise_for_status()
            except requests.HTTPError as e:
                raise GitLabError(f"GitLab API error: {e}") from e
            except requests.RequestException as e:
                raise GitLabError(f"Request failed: {e}") from e

            try:
                page_data = response.json() if response.content else []
            except ValueError as e:
                raise GitLabError(
                    f"expected JSON list response from /{endpoint}, "
                    f"got non-JSON body"
                ) from e

            if not isinstance(page_data, list):
                raise GitLabError(
                    f"expected list response from /{endpoint}, "
                    f"got {type(page_data).__name__}"
                )

            results.extend(page_data)

            next_page = response.headers.get('x-next-page', '').strip()
            if not next_page:
                return results
            if len(page_data) < page_size:
                return results

        # Loop exited without natural termination -> safety cap hit
        next_page = response.headers.get('x-next-page', '').strip() if response is not None else ''
        if next_page:
            self.last_page_capped = True
            print(
                f"WARNING: gitlab-review pagination cap reached after "
                f"{max_pages} pages ({len(results)} items) for /{endpoint}. "
                f"More results may exist. Use --max-pages to raise the limit.",
                file=sys.stderr,
            )
        return results

    def get_merge_request(self, project: str, mr_id: int) -> Dict[str, Any]:
        """Get merge request details including diff refs."""
        project_encoded = project.replace('/', '%2F')
        return self._request('GET', f"projects/{project_encoded}/merge_requests/{mr_id}/changes")

    def get_merge_request_info(self, project: str, mr_id: int) -> Dict[str, Any]:
        """Get merge request basic information (title, description, branches, etc.)."""
        project_encoded = project.replace('/', '%2F')
        return self._request('GET', f"projects/{project_encoded}/merge_requests/{mr_id}")

    def get_merge_request_approvals(self, project: str, mr_id: int) -> Dict[str, Any]:
        """Get merge request approval information."""
        project_encoded = project.replace('/', '%2F')
        return self._request('GET', f"projects/{project_encoded}/merge_requests/{mr_id}/approvals")

    def get_merge_request_participants(
        self,
        project: str,
        mr_id: int,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> List[Dict[str, Any]]:
        """Get merge request participants (reviewers, approvers, etc.)."""
        project_encoded = project.replace('/', '%2F')
        return self._paginate(
            f"projects/{project_encoded}/merge_requests/{mr_id}/participants",
            page_size=page_size,
            max_pages=max_pages,
        )

    def get_merge_request_discussions(
        self,
        project: str,
        mr_id: int,
        resolved: bool = False,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> List[Dict[str, Any]]:
        """Get merge request discussions (comments/notes).

        Args:
            project: GitLab project path (e.g., 'group/project')
            mr_id: Merge request ID
            resolved: Filter by resolved status (False=unresolved only, True=resolved only, None=all)
            page_size: Items per API page
            max_pages: Safety cap on pages fetched

        Returns:
            List of discussion dictionaries containing notes/comments
        """
        project_encoded = project.replace('/', '%2F')
        discussions = self._paginate(
            f"projects/{project_encoded}/merge_requests/{mr_id}/discussions",
            page_size=page_size,
            max_pages=max_pages,
        )

        # Filter by resolved status (default to unresolved only)
        if resolved is not None:
            discussions = [d for d in discussions if d.get('resolved', False) == resolved]

        return discussions

    def get_merge_request_discussion(self, project: str, mr_id: int, discussion_id: str) -> Dict[str, Any]:
        """Get a single merge request discussion thread.

        Args:
            project: GitLab project path (e.g., 'group/project')
            mr_id: Merge request ID
            discussion_id: ID of the discussion thread

        Returns:
            Discussion data including its notes
        """
        project_encoded = project.replace('/', '%2F')
        return self._request('GET', f"projects/{project_encoded}/merge_requests/{mr_id}/discussions/{discussion_id}")

    def reply_to_merge_request_discussion(self, project: str, mr_id: int, discussion_id: str, body: str) -> Dict[str, Any]:
        """Reply to an existing discussion thread on a merge request.

        Args:
            project: GitLab project path (e.g., 'group/project')
            mr_id: Merge request ID
            discussion_id: Discussion thread ID to reply to
            body: Reply text

        Returns:
            Created note data
        """
        project_encoded = project.replace('/', '%2F')
        data = {"body": body}
        return self._request('POST', f"projects/{project_encoded}/merge_requests/{mr_id}/discussions/{discussion_id}/notes", json=data)

    def resolve_merge_request_discussion(self, project: str, mr_id: int, discussion_id: str) -> Dict[str, Any]:
        """Resolve a merge request discussion thread.

        Args:
            project: GitLab project path (e.g., 'group/project')
            mr_id: Merge request ID
            discussion_id: ID of the discussion thread to resolve

        Returns:
            Updated discussion data
        """
        project_encoded = project.replace('/', '%2F')
        return self._request('PUT', f"projects/{project_encoded}/merge_requests/{mr_id}/discussions/{discussion_id}",
                           json={'resolved': True})

    def get_commit(self, project: str, sha: str) -> Dict[str, Any]:
        """Get a single repository commit by SHA.

        Args:
            project: GitLab project path (e.g., 'group/project')
            sha: Commit SHA (or ref name)

        Returns:
            Commit data including committed_date
        """
        project_encoded = project.replace('/', '%2F')
        return self._request('GET', f"projects/{project_encoded}/repository/commits/{sha}")

    def list_open_merge_requests(
        self,
        project: str,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> List[Dict[str, Any]]:
        """List all open merge requests in a project.

        Args:
            project: GitLab project path (e.g., 'group/project')
            page_size: Items per API page
            max_pages: Safety cap on pages fetched

        Returns:
            List of merge request dictionaries with basic information
        """
        project_encoded = project.replace('/', '%2F')
        return self._paginate(
            f"projects/{project_encoded}/merge_requests",
            params={'state': 'opened'},
            page_size=page_size,
            max_pages=max_pages,
        )

    def close_merge_request(self, project: str, mr_id: int) -> Dict[str, Any]:
        """Close a merge request.

        Args:
            project: GitLab project path (e.g., 'group/project')
            mr_id: Merge request ID

        Returns:
            Updated merge request data
        """
        project_encoded = project.replace('/', '%2F')
        data = {'state_event': 'close'}
        return self._request('PUT', f"projects/{project_encoded}/merge_requests/{mr_id}", json=data)

    def get_issue_templates(
        self,
        project: str,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> List[Dict[str, Any]]:
        """Get available issue templates in a project.

        Args:
            project: GitLab project path (e.g., 'group/project')
            page_size: Items per API page (used when listing templates)
            max_pages: Safety cap on pages fetched

        Returns:
            List of template dictionaries with name and content
        """
        project_encoded = project.replace('/', '%2F')

        # Try the official templates API first
        try:
            templates_meta = self._paginate(
                f"projects/{project_encoded}/templates/issues",
                page_size=page_size,
                max_pages=max_pages,
            )
            # The templates API returns metadata, need to fetch content separately
            templates = []
            for template_meta in templates_meta:
                template_name = template_meta.get('key') or template_meta.get('name')
                if template_name:
                    try:
                        # Fetch the actual template content
                        content = self._request('GET', f"projects/{project_encoded}/templates/issues/{template_name}")
                        templates.append({
                            'name': template_name,
                            'key': template_name,
                            'content': content.get('content', '') if isinstance(content, dict) else str(content)
                        })
                    except GitLabError:
                        # If can't get content, add template without content
                        templates.append({
                            'name': template_name,
                            'key': template_name,
                            'content': ''
                        })
            return templates
        except GitLabError:
            # Fallback: try to get templates from .gitlab/issue_templates/ directory
            try:
                files = self._paginate(
                    f"projects/{project_encoded}/repository/tree",
                    params={'path': '.gitlab/issue_templates', 'ref': 'main'},
                    page_size=page_size,
                    max_pages=max_pages,
                )
                templates = []

                for file_info in files:
                    if file_info['type'] == 'blob' and file_info['name'].endswith('.md'):
                        # Get file content
                        file_content = self._request('GET',
                                                   f"projects/{project_encoded}/repository/files/{file_info['path'].replace('/', '%2F')}/raw",
                                                   params={'ref': 'main'})

                        template_name = file_info['name'].replace('.md', '')
                        templates.append({
                            'name': template_name,
                            'key': template_name,
                            'content': file_content if isinstance(file_content, str) else ''
                        })

                return templates
            except GitLabError:
                # If both approaches fail, return empty list
                return []

    def create_issue(self, project: str, title: str, description: str = None, template_name: str = None) -> Dict[str, Any]:
        """Create a new issue in a project.

        Args:
            project: GitLab project path (e.g., 'group/project')
            title: Issue title
            description: Issue description (optional, overrides template)
            template_name: Name of issue template to use (optional)

        Returns:
            Created issue data
        """
        project_encoded = project.replace('/', '%2F')
        data = {'title': title}

        # If template is specified, fetch and use it
        if template_name and not description:
            try:
                templates = self.get_issue_templates(project)
                template = next((t for t in templates if
                               t.get('name') == template_name or
                               t.get('key') == template_name), None)

                if template:
                    # Handle different possible content field names
                    content = template.get('content') or template.get('body') or template.get('description')
                    if content:
                        data['description'] = content
                else:
                    raise GitLabError(f"Template '{template_name}' not found")
            except GitLabError:
                # If template fetch fails, continue without template
                pass
        elif description:
            data['description'] = description

        return self._request('POST', f"projects/{project_encoded}/issues", json=data)

    def update_issue(self, project: str, issue_id: int, title: str = None, description: str = None) -> Dict[str, Any]:
        """Update an existing issue.

        Args:
            project: GitLab project path (e.g., 'group/project')
            issue_id: Issue ID
            title: New issue title (optional)
            description: New issue description (optional)

        Returns:
            Updated issue data
        """
        project_encoded = project.replace('/', '%2F')
        data = {}

        if title is not None:
            data['title'] = title
        if description is not None:
            data['description'] = description

        if not data:
            raise GitLabError("At least one of title or description must be provided")

        return self._request('PUT', f"projects/{project_encoded}/issues/{issue_id}", json=data)

    def update_merge_request(self, project: str, mr_id: int, title: str = None, description: str = None, target_branch: str = None) -> Dict[str, Any]:
        """Update an existing merge request.

        Args:
            project: GitLab project path (e.g., 'group/project')
            mr_id: Merge request ID
            title: New MR title (optional)
            description: New MR description (optional)
            target_branch: New target branch (optional)

        Returns:
            Updated merge request data
        """
        project_encoded = project.replace('/', '%2F')
        data = {}

        if title is not None:
            data['title'] = title
        if description is not None:
            data['description'] = description
        if target_branch is not None:
            data['target_branch'] = target_branch

        if not data:
            raise GitLabError("At least one of title, description, or target_branch must be provided")

        return self._request('PUT', f"projects/{project_encoded}/merge_requests/{mr_id}", json=data)

    def get_issue(self, project: str, issue_id: int) -> Dict[str, Any]:
        """Get issue details including title, description, and metadata.

        Args:
            project: GitLab project path (e.g., 'group/project')
            issue_id: Issue ID

        Returns:
            Issue data including title, description, labels, assignees, etc.
        """
        project_encoded = project.replace('/', '%2F')
        return self._request('GET', f"projects/{project_encoded}/issues/{issue_id}")

    def create_merge_request(self, project: str, title: str, source_branch: str, target_branch: str,
                            description: str = None, assignee_id: int = None, reviewer_ids: List[int] = None,
                            labels: List[str] = None, milestone_id: int = None,
                            remove_source_branch: bool = False, draft: bool = False) -> Dict[str, Any]:
        """Create a new merge request in a project.

        Args:
            project: GitLab project path (e.g., 'group/project')
            title: MR title
            source_branch: Source branch name
            target_branch: Target branch name (defaults to 'main' or 'master')
            description: MR description (optional)
            assignee_id: User ID to assign the MR to (optional)
            reviewer_ids: List of user IDs to set as reviewers (optional)
            labels: List of label names to apply (optional)
            milestone_id: Milestone ID to associate with (optional)
            remove_source_branch: Whether to remove source branch when MR is merged (optional)
            draft: Whether to create as draft MR (optional)

        Returns:
            Created merge request data
        """
        project_encoded = project.replace('/', '%2F')
        data = {
            'title': title,
            'source_branch': source_branch,
            'target_branch': target_branch
        }

        if description is not None:
            data['description'] = description
        if assignee_id is not None:
            data['assignee_id'] = assignee_id
        if reviewer_ids:
            data['reviewer_ids'] = reviewer_ids
        if labels:
            data['labels'] = ','.join(labels)
        if milestone_id is not None:
            data['milestone_id'] = milestone_id
        if remove_source_branch:
            data['remove_source_branch'] = True
        if draft:
            data['title'] = f"Draft: {title}" if not title.startswith('Draft:') else title

        return self._request('POST', f"projects/{project_encoded}/merge_requests", json=data)

    def add_inline_comment(self, project: str, mr_id: int, comment: InlineComment,
                          base_sha: str, head_sha: str, start_sha: str = None) -> Dict[str, Any]:
        """Add inline comment to specific line in MR diff."""
        project_encoded = project.replace('/', '%2F')

        data = {
            "body": comment.comment,
            "position": {
                "position_type": "text",
                "new_path": comment.file_path,
                "new_line": comment.line_number,
                "base_sha": base_sha,
                "start_sha": start_sha or base_sha,
                "head_sha": head_sha
            }
        }

        return self._request('POST', f"projects/{project_encoded}/merge_requests/{mr_id}/discussions", json=data)

    def add_general_comment(self, project: str, mr_id: int, comment: str) -> Dict[str, Any]:
        """Add general comment to MR (not tied to specific line)."""
        project_encoded = project.replace('/', '%2F')
        data = {"body": comment}
        return self._request('POST', f"projects/{project_encoded}/merge_requests/{mr_id}/notes", json=data)

    def get_issue_discussions(
        self,
        project: str,
        issue_id: int,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> List[Dict[str, Any]]:
        """Get issue discussions (comments/notes).

        Args:
            project: GitLab project path (e.g., 'group/project')
            issue_id: Issue ID
            page_size: Items per API page
            max_pages: Safety cap on pages fetched

        Returns:
            List of discussion dictionaries containing notes/comments
        """
        project_encoded = project.replace('/', '%2F')
        return self._paginate(
            f"projects/{project_encoded}/issues/{issue_id}/discussions",
            page_size=page_size,
            max_pages=max_pages,
        )

    def add_issue_comment(self, project: str, issue_id: int, body: str) -> Dict[str, Any]:
        """Add a comment (note) to an issue.

        Args:
            project: GitLab project path (e.g., 'group/project')
            issue_id: Issue ID
            body: Comment text

        Returns:
            Created note data
        """
        project_encoded = project.replace('/', '%2F')
        data = {"body": body}
        return self._request('POST', f"projects/{project_encoded}/issues/{issue_id}/notes", json=data)

    def reply_to_issue_discussion(self, project: str, issue_id: int, discussion_id: str, body: str) -> Dict[str, Any]:
        """Reply to an existing discussion thread on an issue.

        Args:
            project: GitLab project path (e.g., 'group/project')
            issue_id: Issue ID
            discussion_id: Discussion thread ID to reply to
            body: Reply text

        Returns:
            Created note data
        """
        project_encoded = project.replace('/', '%2F')
        data = {"body": body}
        return self._request('POST', f"projects/{project_encoded}/issues/{issue_id}/discussions/{discussion_id}/notes", json=data)


class CodeReviewer:
    """High-level code review interface."""

    def __init__(self, client: GitLabClient = None):
        """Initialize with GitLab client."""
        self.client = client or GitLabClient()

    def review(self, project: str, mr_id: int,
               inline_comments: List[InlineComment] = None,
               summary: str = None) -> Dict[str, Any]:
        """Perform complete code review.

        Args:
            project: GitLab project path (e.g., 'group/project')
            mr_id: Merge request ID
            inline_comments: List of inline comments to add
            summary: General review summary comment

        Returns:
            Dictionary with review results
        """
        # Get MR data for SHA values
        mr_data = self.client.get_merge_request(project, mr_id)
        diff_refs = mr_data['diff_refs']

        results = {
            'mr_id': mr_id,
            'project': project,
            'base_sha': diff_refs['base_sha'],
            'head_sha': diff_refs['head_sha'],
            'inline_comments': [],
            'summary_comment': None,
            'errors': []
        }

        # Add inline comments
        if inline_comments:
            for comment in inline_comments:
                try:
                    result = self.client.add_inline_comment(
                        project=project,
                        mr_id=mr_id,
                        comment=comment,
                        base_sha=diff_refs['base_sha'],
                        head_sha=diff_refs['head_sha'],
                        start_sha=diff_refs['start_sha']
                    )
                    results['inline_comments'].append({
                        'file': comment.file_path,
                        'line': comment.line_number,
                        'status': 'success',
                        'id': result.get('id')
                    })
                except GitLabError as e:
                    results['errors'].append({
                        'type': 'inline_comment',
                        'file': comment.file_path,
                        'line': comment.line_number,
                        'error': str(e)
                    })

        # Add summary comment
        if summary:
            try:
                result = self.client.add_general_comment(project, mr_id, summary)
                results['summary_comment'] = {
                    'status': 'success',
                    'id': result.get('id')
                }
            except GitLabError as e:
                results['errors'].append({
                    'type': 'summary_comment',
                    'error': str(e)
                })

        return results

    def get_mr_info(
        self,
        project: str,
        mr_id: int,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> Dict[str, Any]:
        """Get comprehensive merge request information.

        Args:
            project: GitLab project path (e.g., 'group/project')
            mr_id: Merge request ID
            page_size: Items per API page (used for participants list)
            max_pages: Safety cap on pages fetched

        Returns:
            Dictionary with MR info including approvals, branches, and participants
        """
        try:
            # Get basic MR info
            mr_info = self.client.get_merge_request_info(project, mr_id)

            # Get approval information
            try:
                mr_info['approvals'] = self.client.get_merge_request_approvals(project, mr_id)
            except GitLabError:
                # Some GitLab instances may not have approval feature enabled
                mr_info['approvals'] = {"approved": False, "approvals_left": 0, "approved_by": []}

            # Get participants
            try:
                mr_info['participants'] = self.client.get_merge_request_participants(
                    project, mr_id, page_size=page_size, max_pages=max_pages
                )
            except GitLabError:
                mr_info['participants'] = []

            return mr_info
        except GitLabError as e:
            raise GitLabError(f"Failed to get MR info: {e}") from e

    def list_open_mrs(
        self,
        project: str,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> List[Dict[str, Any]]:
        """List all open merge requests in a project.

        Args:
            project: GitLab project path (e.g., 'group/project')
            page_size: Items per API page
            max_pages: Safety cap on pages fetched

        Returns:
            List of merge request dictionaries with key information
        """
        try:
            return self.client.list_open_merge_requests(
                project, page_size=page_size, max_pages=max_pages
            )
        except GitLabError as e:
            raise GitLabError(f"Failed to list open MRs: {e}") from e

    def close_mr(self, project: str, mr_id: int) -> Dict[str, Any]:
        """Close a merge request.

        Args:
            project: GitLab project path (e.g., 'group/project')
            mr_id: Merge request ID

        Returns:
            Updated merge request data
        """
        try:
            return self.client.close_merge_request(project, mr_id)
        except GitLabError as e:
            raise GitLabError(f"Failed to close MR {mr_id}: {e}") from e

    def create_issue(self, project: str, title: str, description: str = None, template_name: str = None, description_file: str = None) -> Dict[str, Any]:
        """Create a new issue in a project.

        Args:
            project: GitLab project path (e.g., 'group/project')
            title: Issue title
            description: Issue description (optional, overrides template)
            template_name: Name of issue template to use (optional)
            description_file: Path to file containing description (optional, overrides description and template)

        Returns:
            Created issue data
        """
        try:
            # If description_file is provided, read content from file (highest priority)
            if description_file:
                description = read_file_content(description_file)

            return self.client.create_issue(project, title, description, template_name)
        except GitLabError as e:
            raise GitLabError(f"Failed to create issue: {e}") from e

    def update_issue(self, project: str, issue_id: int, title: str = None, description: str = None, description_file: str = None) -> Dict[str, Any]:
        """Update an existing issue.

        Args:
            project: GitLab project path (e.g., 'group/project')
            issue_id: Issue ID
            title: New issue title (optional)
            description: New issue description (optional)
            description_file: Path to file containing description (optional, overrides description)

        Returns:
            Updated issue data
        """
        try:
            # If description_file is provided, read content from file
            if description_file:
                description = read_file_content(description_file)

            return self.client.update_issue(project, issue_id, title, description)
        except GitLabError as e:
            raise GitLabError(f"Failed to update issue {issue_id}: {e}") from e

    def update_mr(self, project: str, mr_id: int, title: str = None, description: str = None, description_file: str = None, target_branch: str = None) -> Dict[str, Any]:
        """Update an existing merge request.

        Args:
            project: GitLab project path (e.g., 'group/project')
            mr_id: Merge request ID
            title: New MR title (optional)
            description: New MR description (optional)
            description_file: Path to file containing description (optional, overrides description)
            target_branch: New target branch (optional)

        Returns:
            Updated merge request data
        """
        try:
            # If description_file is provided, read content from file
            if description_file:
                description = read_file_content(description_file)

            return self.client.update_merge_request(project, mr_id, title, description, target_branch)
        except GitLabError as e:
            raise GitLabError(f"Failed to update MR {mr_id}: {e}") from e

    def list_issue_templates(
        self,
        project: str,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> List[Dict[str, Any]]:
        """List available issue templates in a project.

        Args:
            project: GitLab project path (e.g., 'group/project')
            page_size: Items per API page
            max_pages: Safety cap on pages fetched

        Returns:
            List of template dictionaries
        """
        try:
            return self.client.get_issue_templates(
                project, page_size=page_size, max_pages=max_pages
            )
        except GitLabError as e:
            raise GitLabError(f"Failed to list issue templates: {e}") from e

    def get_issue(self, project: str, issue_id: int) -> Dict[str, Any]:
        """Get issue details including title, description, and metadata.

        Args:
            project: GitLab project path (e.g., 'group/project')
            issue_id: Issue ID

        Returns:
            Issue data including title, description, labels, assignees, etc.
        """
        try:
            return self.client.get_issue(project, issue_id)
        except GitLabError as e:
            raise GitLabError(f"Failed to get issue {issue_id}: {e}") from e

    def get_issue_discussions(
        self,
        project: str,
        issue_id: int,
        system: bool = False,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> List[Dict[str, Any]]:
        """Get issue discussions/comments.

        Args:
            project: GitLab project path (e.g., 'group/project')
            issue_id: Issue ID
            system: If True, include system notes (label changes, assignments, etc.)
            page_size: Items per API page
            max_pages: Safety cap on pages fetched

        Returns:
            List of discussion dictionaries with notes/comments
        """
        try:
            discussions = self.client.get_issue_discussions(
                project, issue_id, page_size=page_size, max_pages=max_pages
            )
            if not system:
                discussions = [
                    d for d in discussions
                    if any(not note.get('system', False) for note in d.get('notes', []))
                ]
            return discussions
        except GitLabError as e:
            raise GitLabError(f"Failed to get issue discussions: {e}") from e

    def add_issue_comment(self, project: str, issue_id: int, body: str) -> Dict[str, Any]:
        """Add a comment to an issue.

        Args:
            project: GitLab project path (e.g., 'group/project')
            issue_id: Issue ID
            body: Comment text

        Returns:
            Created note data
        """
        try:
            return self.client.add_issue_comment(project, issue_id, body)
        except GitLabError as e:
            raise GitLabError(f"Failed to add comment to issue {issue_id}: {e}") from e

    def reply_to_issue_discussion(self, project: str, issue_id: int, discussion_id: str, body: str) -> Dict[str, Any]:
        """Reply to an existing discussion thread on an issue.

        Args:
            project: GitLab project path (e.g., 'group/project')
            issue_id: Issue ID
            discussion_id: Discussion thread ID
            body: Reply text

        Returns:
            Created note data
        """
        try:
            return self.client.reply_to_issue_discussion(project, issue_id, discussion_id, body)
        except GitLabError as e:
            raise GitLabError(f"Failed to reply to discussion {discussion_id}: {e}") from e

    def create_mr(self, project: str, title: str, source_branch: str, target_branch: str,
                  description: str = None, description_file: str = None, assignee_id: int = None,
                  reviewer_ids: List[int] = None, labels: List[str] = None, milestone_id: int = None,
                  remove_source_branch: bool = False, draft: bool = False) -> Dict[str, Any]:
        """Create a new merge request in a project.

        Args:
            project: GitLab project path (e.g., 'group/project')
            title: MR title
            source_branch: Source branch name
            target_branch: Target branch name
            description: MR description (optional, overridden by description_file)
            description_file: Path to file containing description (optional, overrides description)
            assignee_id: User ID to assign the MR to (optional)
            reviewer_ids: List of user IDs to set as reviewers (optional)
            labels: List of label names to apply (optional)
            milestone_id: Milestone ID to associate with (optional)
            remove_source_branch: Whether to remove source branch when MR is merged (optional)
            draft: Whether to create as draft MR (optional)

        Returns:
            Created merge request data
        """
        try:
            # If description_file is provided, read content from file
            if description_file:
                description = read_file_content(description_file)

            return self.client.create_merge_request(
                project=project,
                title=title,
                source_branch=source_branch,
                target_branch=target_branch,
                description=description,
                assignee_id=assignee_id,
                reviewer_ids=reviewer_ids,
                labels=labels,
                milestone_id=milestone_id,
                remove_source_branch=remove_source_branch,
                draft=draft
            )
        except GitLabError as e:
            raise GitLabError(f"Failed to create MR: {e}") from e

    def get_mr_discussions(
        self,
        project: str,
        mr_id: int,
        unresolved_only: bool = True,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> List[Dict[str, Any]]:
        """Get merge request discussions/comments.

        Args:
            project: GitLab project path (e.g., 'group/project')
            mr_id: Merge request ID
            unresolved_only: If True, return only unresolved discussions (default)
            page_size: Items per API page
            max_pages: Safety cap on pages fetched

        Returns:
            List of discussion dictionaries with notes/comments
        """
        try:
            resolved_filter = False if unresolved_only else None
            return self.client.get_merge_request_discussions(
                project, mr_id, resolved=resolved_filter,
                page_size=page_size, max_pages=max_pages,
            )
        except GitLabError as e:
            raise GitLabError(f"Failed to get MR discussions: {e}") from e

    def get_mr_discussion(self, project: str, mr_id: int, discussion_id: str) -> Dict[str, Any]:
        """Get a single merge request discussion thread.

        Args:
            project: GitLab project path (e.g., 'group/project')
            mr_id: Merge request ID
            discussion_id: Discussion ID to fetch

        Returns:
            Discussion data including its notes
        """
        try:
            return self.client.get_merge_request_discussion(project, mr_id, discussion_id)
        except GitLabError as e:
            raise GitLabError(f"Failed to get discussion {discussion_id}: {e}") from e

    def reply_to_thread(self, project: str, mr_id: int, discussion_id: str, body: str) -> Dict[str, Any]:
        """Reply to an existing discussion thread on a merge request.

        Args:
            project: GitLab project path (e.g., 'group/project')
            mr_id: Merge request ID
            discussion_id: Discussion ID to reply to
            body: Reply text

        Returns:
            Created note data
        """
        try:
            return self.client.reply_to_merge_request_discussion(project, mr_id, discussion_id, body)
        except GitLabError as e:
            raise GitLabError(f"Failed to reply to discussion {discussion_id}: {e}") from e

    def resolve_thread(self, project: str, mr_id: int, discussion_id: str) -> Dict[str, Any]:
        """Resolve a discussion thread.

        Args:
            project: GitLab project path (e.g., 'group/project')
            mr_id: Merge request ID
            discussion_id: Discussion ID to resolve

        Returns:
            Updated discussion data
        """
        try:
            return self.client.resolve_merge_request_discussion(project, mr_id, discussion_id)
        except GitLabError as e:
            raise GitLabError(f"Failed to resolve discussion {discussion_id}: {e}") from e
