#!/bin/bash
# Wrapper script for gitlab-review-docs.py hook
# src is mounted from host, venv is container-native
export PYTHONPATH="/Agents/global/src:$PYTHONPATH"
"${LMER_PYTHON:-python3}" /Agents/global/hooks/gitlab-review-docs.py "$@"
