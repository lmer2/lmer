# Design: Container Provisioning Fixes (napkin dir + gate pytest interpreter)

**Date:** 2026-07-03
**Status:** Draft — brainstormed, decisions locked, pre-plan

## Problem

Two independent breakages, same root class as the `start.py` taskdef fix
(commit `a7ed81f`): **container code assumes a container-native path/interpreter,
but self-dev (and bind-mounts generally) supply a host one.**

### Bug 1 — napkin (and `/taskdef` clone target) never provisioned

`clone_aux_repos()` (`src/lmer_cli/container/clone_and_exec.py:93`) clones the
napkin repo to `/napkin` via `ensure_clone()`, which first does
`Path("/napkin").mkdir(parents=True, exist_ok=True)` (`clone_and_exec.py:70`).
`/` is mode `0555` root-owned, so the `mkdir` raises `PermissionError`; the
clone is caught and downgraded to a non-fatal warning, leaving `~/napkin`
(symlink → `/napkin`) dangling.

Bind-mount targets escape this because the **container runtime creates
mountpoints as root** — and `Containerfile:109` pre-creates the rest
(`mkdir -p /Agents/global /workspace /work … chown developer:developer
/workspace /work`). `/napkin` and `/taskdef` were simply never added to that
line, so no writable directory exists for the in-container clone.

`/taskdef` (the `LMER_TASKDEF_REPO` clone target) has the identical bug; it is
merely masked today because taskdef also has a bind-mount path
(`LMER_TASKDEF_PATHS` → `/Agents/taskdefs/<n>`). Napkin has no mount fallback,
so it is fully broken.

### Bug 2 — gate `check_tests` selects a dead interpreter

`GateChecker.check_tests()` (`src/lmer_cli/gates.py:289`) picks
`<project>/.venv/bin/python` whenever it merely *exists* (`gates.py:304-309`).
In self-dev, `/workspace/.venv` is a bind-mount of the **host** project venv:

- `/workspace/.venv/bin/pytest` shebang is `#!/home/user/Agents/global/.venv/bin/python`
  — a host path → `bad interpreter: No such file or directory`.
- `/workspace/.venv/bin/python` resolves to the container's **system Python 3.9**,
  whose `sys.path` does not include the venv's pytest → `ModuleNotFound: pytest`.

So `check_tests` runs an interpreter that structurally cannot import pytest and
reports "Tests failed" even though the suite passes. The **pre-commit check
already guards this exact failure mode** via `_venv_script_launchable()`
(`gates.py:382`) — the tests check never got the analogous guard.

Note: bare `python`/`python3` on `PATH` in the container resolves to
`/Agents/global/.venv/bin/python` (Python 3.12, pytest 8.4.1 — the venv built at
`Containerfile:135`), i.e. a working fallback is already on `PATH`.

## Goals

1. `~/napkin` resolves to a real, writable napkin checkout inside the container
   (separate-repo mode), so agents can write and `work commit` can push it.
2. `/taskdef` clone target works for the same reason (parallel fix).
3. `gate-check`/`gate-commit` run the test suite under an interpreter that can
   import pytest, in self-dev and normal runs alike — no false "Tests failed".
4. Both fixes are covered by unit tests **and** a real-container smoke test that
   would have caught these (the prior bundle's mocked-path unit tests did not).

## Non-Goals

- Host bind-mounting napkin (considered; rejected for this bundle in favor of
  the minimal pre-create-dir + keep-clone approach).
- A broader self-dev path-assumption audit (deferred; out of scope).
- Changing napkin's separate-repo-vs-subdir semantics or the `work commit` push.
- Fixing the ephemeral `/napkin` volume for *this already-running* container
  (only new containers / rebuilt images are in scope).

## Design

### Decision summary (locked in brainstorm)

- **Napkin provisioning:** pre-create writable `/napkin` + `/taskdef` in the
  image; keep the existing in-container clone. Fresh clone per container,
  persisted via `work commit` push.
- **Gate interpreter:** probe-and-fall-back, mirroring `_venv_script_launchable`.
- **Verification bar:** unit tests + a `requires_container` smoke test.
- **Scope:** exactly these two fixes.

### Fix 1 — pre-create `/napkin` and `/taskdef` (Containerfile)

Add both dirs to the existing mountpoint/pre-create line and chown to developer:

```dockerfile
# Containerfile:109 (anchor — extend the existing RUN)
RUN mkdir -p /Agents/global /workspace /work /napkin /taskdef && \
    ... && \
    chown -R developer:developer /Agents && \
    chown developer:developer /workspace /work /napkin /taskdef
```

`ensure_clone()` already handles a pre-existing empty dir (`mkdir(exist_ok=True)`
then `git clone` into it) and short-circuits if `.git` is present, so no
entrypoint change is required. Optional hardening: make the clone-failure
`print` in `clone_aux_repos()` include the target path (it already does) — no
change needed.

Rollout note: normal users need `lmer build` (image rebuild) to pick this up;
self-dev picks it up on the next container from a rebuilt image. Both `/napkin`
and `/taskdef` come from the image overlay (`/`), so a rebuilt image carries the
dirs even under self-dev's bind-mount layout.

### Fix 2 — harden `check_tests` interpreter selection (gates.py)

Introduce a small probe mirroring `_venv_script_launchable`:

```python
@staticmethod
def _interpreter_can_import(python_cmd: str, module: str = "pytest") -> bool:
    """True iff `python_cmd -c 'import <module>'` succeeds.

    A bind-mounted host venv can leave a .venv/bin/python that resolves to a
    system interpreter without the venv's site-packages (self-dev), so a mere
    existence check is insufficient — probe an actual import.
    """
    try:
        return subprocess.run(
            [python_cmd, "-c", f"import {module}"],
            capture_output=True, timeout=30,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
```

`check_tests()` selection becomes: prefer `<project>/.venv/bin/python` **iff**
`_interpreter_can_import(...)`; else the first of `python3` / `python` on `PATH`
that can; else keep current behavior and let the run surface a clear error.
Preserve the existing `PYTHONPATH` src-prepend (`gates.py:311-318`).

### Verification

**Unit (mocked):**
- `gates.py`: project venv that can't import pytest → falls back to a PATH
  interpreter that can; project venv that can → used directly; none available →
  clear failure. (Extend `tests/test_gates.py`.)
- `clone_and_exec.py`: unchanged logic; add/confirm a test that `ensure_clone`
  succeeds into a pre-existing empty dir. (`tests/test_clone_and_exec_aux_repos.py`.)

**Real-container smoke (`requires_container`, `tests/_lmer_runtime.py`):**
- Launch a container and assert `~/napkin` is a live git checkout (not a
  dangling symlink) when `LMER_NAPKIN_REPO` is set.
- Assert `gate-check`'s test step runs pytest (not "No module named pytest")
  in the self-dev/bind-mounted-venv layout.
- Gated by `requires_container` so it skips cleanly without a docker/podman
  socket (matches `test_lmer_integration.py`).

## Files

| File | Repo | Responsibility | Task |
|---|---|---|---|
| `Containerfile` | lmer | Pre-create + chown `/napkin`, `/taskdef` | 1 |
| `src/lmer_cli/gates.py` | lmer | `_interpreter_can_import` + `check_tests` fallback | 2 |
| `tests/test_gates.py` | lmer | Unit tests for interpreter fallback | 2 |
| `tests/test_clone_and_exec_aux_repos.py` | lmer | Confirm clone into pre-created dir | 1 |
| `tests/test_container_provisioning_smoke.py` (new) | lmer | `requires_container` smoke: napkin resolves, gate runs pytest | 3 |

## Proposed tasks / waves

- **Wave 0** (independent):
  - Task 1 — Containerfile `/napkin` + `/taskdef` pre-create; clone-into-empty-dir unit test.
  - Task 2 — `gates.py` interpreter probe + fallback; `test_gates.py` units.
- **Wave 1** (depends on 1 & 2 landing):
  - Task 3 — container smoke test exercising both fixes end-to-end.

## Open questions / risks

- Smoke test cost/flakiness: launching a real container in CI. Mitigate via
  `requires_container` skip + keeping the assertions narrow (symlink target
  resolves; gate test-step exit reason). Confirm CI has a runtime socket.
- The `_interpreter_can_import` probe adds a subprocess per gate run (~1 short
  import). Negligible, and only on the tests check.
