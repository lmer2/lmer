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
REVIEW_STATUS=$?
if [ "$REVIEW_STATUS" -ne 0 ]; then
  exit "$REVIEW_STATUS"
fi

lmer-signal "Posted GitLab review for ${GITLAB_PROJECT}!${GITLAB_MR_ID}" || \
  echo "Warning: review posted but milestone was not signalled" >&2
exit "$REVIEW_STATUS"
