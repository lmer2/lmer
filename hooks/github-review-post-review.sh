#!/bin/bash
# Post review to GitHub pull request using review file
# Requires: GITHUB_PROJECT, GITHUB_PR_ID, GITHUB_REVIEW_FILE
# Optional: GITHUB_HOST (defaults to github.com)

if [ -z "$GITHUB_PROJECT" ]; then
  echo "Error: GITHUB_PROJECT environment variable is not set" >&2
  exit 1
fi

if [ -z "$GITHUB_PR_ID" ]; then
  echo "Error: GITHUB_PR_ID environment variable is not set" >&2
  exit 1
fi

if [ -z "$GITHUB_REVIEW_FILE" ]; then
  echo "Error: GITHUB_REVIEW_FILE environment variable is not set" >&2
  exit 1
fi

HOST_ARG=()
if [ -n "$GITHUB_HOST" ]; then
  HOST_ARG=(--host "$GITHUB_HOST")
fi

github-review "$GITHUB_PROJECT" "$GITHUB_PR_ID" "${HOST_ARG[@]}" --review-file "$GITHUB_REVIEW_FILE"
REVIEW_STATUS=$?
if [ "$REVIEW_STATUS" -ne 0 ]; then
  exit "$REVIEW_STATUS"
fi

lmer-signal "Posted GitHub review for ${GITHUB_PROJECT}#${GITHUB_PR_ID}" || \
  echo "Warning: review posted but milestone was not signalled" >&2
exit "$REVIEW_STATUS"
