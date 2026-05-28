#!/bin/bash
# Wrapper script for github-review-docs.py hook
# src is mounted from host, venv is container-native
export PYTHONPATH="/Agents/global/src:$PYTHONPATH"
"${LMER_PYTHON:-python3}" /Agents/global/hooks/github-review-docs.py "$@"
