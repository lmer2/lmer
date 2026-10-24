#!/bin/sh

git fetch origin --prune
git pull
uv tool install -e lmer --from .
lmer build --update-harness all
lmer platform setup-ui
