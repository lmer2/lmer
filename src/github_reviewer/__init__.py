"""GitHub Code Review Library.

Thin wrapper around the ``gh`` CLI. Exists to fill the one workflow that
``gh`` does not have a clean native equivalent for — atomic inline review
submission (N file/line comments + a summary in one call) — and to provide
read-side output that is shaped similarly to ``gitlab-review --json`` so
downstream consumers don't have to branch on host provider.
"""

from .cli import main

__all__ = ["main"]
