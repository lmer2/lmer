---
description: Post review to GitLab merge request using review file
allowed-tools: Bash(bash /Agents/global/hooks/gitlab-review-post-review.sh:*)
---

Post a review to a GitLab merge request using a JSON review file.

Requires environment variables:
- GITLAB_PROJECT: Project path (e.g., group/project)
- GITLAB_MR_ID: Merge request ID
- GITLAB_REVIEW_FILE: Path to JSON file containing review data

!bash /Agents/global/hooks/gitlab-review-post-review.sh
