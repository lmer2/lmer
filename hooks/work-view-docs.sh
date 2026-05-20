#!/bin/bash
# Wrapper script for work-view-docs.py hook
# src is mounted from host, venv is container-native
export PYTHONPATH="/Agents/global/src:$PYTHONPATH"
"${LMER_PYTHON:-python3}" /Agents/global/hooks/work-view-docs.py "$@"
