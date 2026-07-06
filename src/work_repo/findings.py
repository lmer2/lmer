"""Shared lint-finding shape for the pure kernels (plan_index, goals).

Both lints follow the same discipline — callers pass text/objects in,
ordered findings come out — and their reports render identically. One home
for the finding/report format keeps the two from drifting (MR !116 review).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    """One lint finding. level is 'error' (blocks the gate/verb) or 'warning'."""

    level: str
    rule: str
    message: str


def error(rule: str, message: str) -> Finding:
    return Finding("error", rule, message)


def warning(rule: str, message: str) -> Finding:
    return Finding("warning", rule, message)


def format_findings(findings: list[Finding]) -> list[str]:
    """Render findings as report lines (errors ❌, warnings ⚠️ )."""
    return [
        ("❌ " if f.level == "error" else "⚠️  ") + f.message
        for f in findings
    ]
