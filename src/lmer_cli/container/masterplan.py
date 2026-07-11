"""Masterplan session provisioning helper.

Bridges libexec/claude-runner.sh (shell) and the run-state kernel (Python):
decides whether a container session should run the masterplan workflow and,
if so, computes the bundle root that masterplan honors via ``MASTERPLAN_RUNS_DIR``.

The shell does the actual ``claude plugin`` calls (idempotent, non-fatal) once
this module reports that the mode is active and the bundle root is known — the
gating (``get_bool_env``) and run-dir computation (``work_repo.run_state``)
live here so they follow the project's env-var conventions rather than being
re-implemented in bash.

Unlike ``clone_and_exec`` (which runs standalone and must not import the
package), this helper is invoked as ``python3 -m lmer_cli.container.masterplan``
after the editable install is on ``sys.path``, so importing ``lmer_cli`` and
``work_repo`` is fine.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from lmer_cli.util import get_bool_env


def masterplan_enabled() -> bool:
    """True when the session should run the masterplan workflow.

    Active when the task is the dedicated ``masterplan`` taskdef, or the
    ``LMER_MASTERPLAN`` toggle is truthy (``get_bool_env``: ``1``/``true``/``yes``,
    case-insensitive). Parsing goes through ``get_bool_env`` so ``=0``/``=false``
    is an honest off-switch rather than a surprising "any non-empty value is on".
    """
    if os.environ.get("LMER_TASK") == "masterplan":
        return True
    return get_bool_env("LMER_MASTERPLAN")


def masterplan_runs_dir() -> Optional[Path]:
    """Bundle root for masterplan: ``<current-run-dir>/masterplan``.

    The run dir is computed by ``work_repo.run_state.run_dir()`` (the sole
    authority on where a run's directory lives). Returns ``None`` when the run
    dir cannot be resolved (host/project unset), so the caller can skip
    provisioning without failing the session.
    """
    # Deferred deliberately: keep the import inside this function so that
    # ``masterplan_enabled()`` (a pure ``get_bool_env`` check) and the module as
    # a whole stay importable/testable without ``work_repo`` — and its
    # dependencies — being resolvable on ``sys.path``. Only the run-dir
    # computation actually needs it, and only when the session is a masterplan
    # session, so we pay the import cost lazily at that point.
    from work_repo import run_state

    rdir = run_state.run_dir()
    if rdir is None:
        return None
    return rdir / "masterplan"


def main(argv: Optional[list[str]] = None) -> int:
    """Print ``MASTERPLAN_RUNS_DIR`` when the session is a masterplan session.

    Contract with the shell caller (distinct exit codes so the shell can tell
    "not a masterplan session" apart from "wanted one but couldn't locate it"):
      * exit 0 + one line (the bundle root) → provision the plugin and export
        ``MASTERPLAN_RUNS_DIR``;
      * exit 1 + no output → not a masterplan session; skip silently;
      * exit 2 + no output → masterplan explicitly enabled but the run dir is
        indeterminate — either unresolvable (e.g. LMER_REPO_HOST/LMER_REPO_PROJECT
        unset, as in a chat-style session) or an unexpected error while resolving
        it (e.g. work_repo import failure) → skip, but the caller should warn
        since the user asked for masterplan and got nothing.

    An enabled-but-broken session must not fall through to exit 1: that is the
    silent "plain session" path, so any failure once masterplan is enabled maps
    to exit 2 so the warning still fires.
    """
    if not masterplan_enabled():
        return 1
    try:
        rdir = masterplan_runs_dir()
    except Exception:
        return 2
    if rdir is None:
        return 2
    print(rdir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
