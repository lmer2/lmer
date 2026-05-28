---
description: Post review to GitHub pull request using review file
allowed-tools: Bash(bash /Agents/global/hooks/github-review-post-review.sh:*)
---

Post a review to a GitHub pull request using a JSON review file.

Requires environment variables:
- GITHUB_PROJECT: Project path (e.g., owner/repo)
- GITHUB_PR_ID: Pull request number
- GITHUB_REVIEW_FILE: Path to JSON file containing review data
- GITHUB_HOST: (optional) GitHub host, defaults to github.com

!bash /Agents/global/hooks/github-review-post-review.sh
