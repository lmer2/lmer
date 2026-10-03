#!/bin/sh

git fetch --all --prune
git pull
uv tool install -e lmer --from .
lmer build --update-harness all
