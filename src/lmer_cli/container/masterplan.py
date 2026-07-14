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

from lmer_cli.util import TRUTHY_VALUES, get_bool_env


# Per-task manifest, resolved beside the taskdef's instructions.txt through
# the same tier precedence as every other taskdef file.
TASK_MANIFEST = "task.yaml"


def taskdef_declares_masterplan() -> bool:
    """True when the active taskdef ships a ``task.yaml`` declaring masterplan.

    A taskdef whose instructions require the masterplan plugin (e.g. a
    work-repo ``spec`` taskdef) declares that need in a per-task manifest
    beside its ``instructions.txt``::

        # taskdef/<name>/task.yaml
        masterplan: true

    The manifest resolves through the same tier precedence as the taskdef's
    other files (work-repo project, work-repo global, LMER_TASKDEF_PATHS,
    built-in) — the highest-precedence tier shipping a ``task.yaml`` wins, so
    a work-repo override can flip the flag either way. An unreadable,
    malformed, or non-mapping manifest counts as "not declared": provisioning
    is logged-never-fatal and must not flip masterplan on (or the session
    over) because of a bad YAML file.
    """
    task = os.environ.get("LMER_TASK")
    if not task:
        return False
    # Deferred like the work_repo import below: keep masterplan_enabled()'s
    # pure-env paths importable/testable without the search (or yaml) loaded.
    from lmer_cli.container.taskdefs import find_taskdef_file

    manifest = find_taskdef_file(TASK_MANIFEST, task)
    if manifest is None:
        return False
    try:
        import yaml

        data = yaml.safe_load(manifest.read_text())
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    value = data.get("masterplan")
    if value is None:
        return False
    # Same truthy set as get_bool_env, so `masterplan: "1"`/"yes"/"true" (and
    # the YAML booleans, via str()) all read the same as the env toggle would.
    return str(value).strip().lower() in TRUTHY_VALUES


def masterplan_enabled() -> bool:
    """True when the session should run the masterplan workflow.

    Active when the task is the dedicated ``masterplan`` taskdef, the
    ``LMER_MASTERPLAN`` toggle is truthy (``get_bool_env``: ``1``/``true``/``yes``,
    case-insensitive), or the active taskdef declares ``masterplan: true`` in
    its ``task.yaml`` (see ``taskdef_declares_masterplan``). Parsing goes
    through ``get_bool_env`` so ``=0``/``=false`` is an honest off-switch
    rather than a surprising "any non-empty value is on" — for the toggle
    itself: an explicit falsy toggle does not veto the taskdef-name or
    taskdef-declared signals, the same way it never vetoed
    ``LMER_TASK=masterplan``. A taskdef whose instructions require masterplan
    stays provisioned.
    """
    if os.environ.get("LMER_TASK") == "masterplan":
        return True
    if get_bool_env("LMER_MASTERPLAN"):
        return True
    try:
        return taskdef_declares_masterplan()
    except Exception:
        # Same logged-never-fatal posture as the shell caller: a broken
        # declaration lookup degrades to "plain session", never a crash.
        return False


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
