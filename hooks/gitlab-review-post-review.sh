#!/bin/bash
# Post review to GitLab merge request using review file
# Requires: GITLAB_PROJECT, GITLAB_MR_ID, GITLAB_REVIEW_FILE

if [ -z "$GITLAB_PROJECT" ]; then
  echo "Error: GITLAB_PROJECT environment variable is not set" >&2
  exit 1
fi

if [ -z "$GITLAB_MR_ID" ]; then
  echo "Error: GITLAB_MR_ID environment variable is not set" >&2
  exit 1
fi

if [ -z "$GITLAB_REVIEW_FILE" ]; then
  echo "Error: GITLAB_REVIEW_FILE environment variable is not set" >&2
  exit 1
fi

gitlab-review "$GITLAB_PROJECT" "$GITLAB_MR_ID" --review-file "$GITLAB_REVIEW_FILE"
