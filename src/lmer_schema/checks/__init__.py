"""``lmer_schema.checks`` — the validator's check modules and their shared
model (spec §7). Internal to the validator: NOT re-exported from the
``lmer_schema`` package root.

This package root exports the finding model and tier engine of
:mod:`lmer_schema.checks.findings` — the one surface every check module
(manifest, registry, tree, agents, record, index) and the runner/CLI import.
Check modules live as siblings here and register their codes against
:data:`CHECKS` at import time; this ``__init__`` imports none of them, so
importing the model never runs a check.
"""

from lmer_schema.checks.findings import (
    CHECKS,
    CODE_RE,
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_UNUSABLE,
    NAMESPACES,
    SEVERITIES,
    CheckCode,
    CheckRegistry,
    Finding,
    Form,
    Report,
    Severity,
    Tier,
    register,
)

__all__ = [
    "EXIT_CLEAN",
    "EXIT_ERRORS",
    "EXIT_UNUSABLE",
    "Severity",
    "SEVERITIES",
    "Form",
    "Tier",
    "Finding",
    "CODE_RE",
    "NAMESPACES",
    "CheckCode",
    "CheckRegistry",
    "CHECKS",
    "register",
    "Report",
]
