"""lmer orchestrator platform — host-side daemon that owns the lmer fleet.

The platform is a long-lived host process (``lmer platform``) that holds the
queue, the concurrency caps, the service-mode slots and the spawn path for every
lmer instance on a host, and serves the control UI. See the approved design spec
(work repo, ``runs/develop-issue-141--lmer-orchestrator/spec.md``) for the
decisions this package implements.

Two invariants run through every module here:

- **The work repo stays authoritative for run state.** This package reads
  ``state.yaml`` / ``events.jsonl`` / ``ledger.yaml`` and never writes them; the
  ``work`` CLI remains their only writer (spec D3).
- **The daemon is the single writer of platform state**, and that state is
  reconstructible rather than precious — which is what licenses plain files over
  a database (spec D2, §5.3).
"""

__all__ = ["SCHEMA_VERSION"]

#: Schema version stamped into every platform state file this package writes.
SCHEMA_VERSION = 1
