# Container Provisioning Fixes — Implementation Plan

Executes `spec.md`. Two independent fixes (Wave 0) + one integration smoke test
(Wave 1). All paths relative to the lmer repo root (`/workspace`).

## Global Constraints

Every task implicitly includes these.

- **Do NOT run `git add`/`git commit`** — the orchestrator commits per wave.
- **Verify with a WORKING interpreter.** The project `.venv` is a broken host
  bind-mount in self-dev (that is Bug 2). Run tests with **`python -m pytest`**
  (bare `python` resolves to `/Agents/global/.venv`, Python 3.12 + pytest) —
  NOT `.venv/bin/python`.
- **Stay strictly within your declared files.**
- **No AI attribution** in any file or message.
- **TDD:** write/adjust the test, watch it fail for the right reason, implement,
  watch it pass.

---

### Task 1: Pre-create `/napkin` + `/taskdef` in the image (Containerfile)

**Files:** `Containerfile`, `tests/test_clone_and_exec_aux_repos.py`

**Why:** `ensure_clone()` does `Path("/napkin").mkdir()`; `/` is `0555`
root-owned, so the clone fails and `~/napkin` dangles. Bind-mount targets
survive because they are pre-created at `Containerfile:109`. `/napkin` and
`/taskdef` were never added there.

**Step 1 — Containerfile.** In the `RUN` at line 109, add `/napkin /taskdef` to
both the `mkdir -p` list and the final `chown developer:developer` list:

```dockerfile
RUN mkdir -p /Agents/global /workspace /work /napkin /taskdef && \
    echo "CONTAINER_ENV=true" > /etc/container-environment && \
    echo "CONTAINER_TYPE=lmer" >> /etc/container-environment && \
    echo "RESOURCE_LIMITS=cpu:1,memory:2G,procs:512" >> /etc/container-environment && \
    chown -R developer:developer /Agents && \
    chown developer:developer /workspace /work /napkin /taskdef
```

**Step 2 — regression test.** In `tests/test_clone_and_exec_aux_repos.py` add a
test asserting `ensure_clone` populates a **pre-existing empty** directory
(the post-fix runtime condition). Use a real local source repo so no network is
needed:

- `git init` a source dir with one commit; pre-create an empty target dir
  (mimicking the image-provided mountpoint); call
  `clone_and_exec.ensure_clone(target, str(source), None, None)`; assert
  `(target / ".git").exists()` and the committed file is present.
- Also assert a second call is a no-op (already has `.git`).

**Verify:** `python -m pytest tests/test_clone_and_exec_aux_repos.py -q`
(Containerfile is not unit-testable here; the smoke test in Task 3 exercises it.)

---

### Task 2: Harden gate test-interpreter selection (gates.py)

**Files:** `src/lmer_cli/gates.py`, `tests/test_gates.py`

**Why:** `check_tests()` uses `<project>/.venv/bin/python` if it merely exists.
In self-dev that python is a host bind-mount resolving to system 3.9 with no
pytest → false "Tests failed". Mirror the existing `_venv_script_launchable`
guard (`gates.py:382`).

**Step 1 — helper.** Add a staticmethod near `_venv_script_launchable`:

```python
@staticmethod
def _interpreter_can_import(python_cmd: str, module: str = "pytest") -> bool:
    """True iff ``python_cmd -c 'import <module>'`` succeeds.

    A bind-mounted host venv can leave a ``.venv/bin/python`` that resolves to
    a system interpreter without the venv's site-packages (self-dev), so a mere
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

**Step 2 — selection.** Replace the `venv_python.exists()` block
(`gates.py:304-309`) with: use the project venv python **iff** it exists AND
`_interpreter_can_import` passes; else use the first of `("python3", "python")`
that passes the probe; else fall back to `"python"` (preserve current default so
the run still surfaces a clear pytest error). Keep the `PYTHONPATH` src-prepend
unchanged. Add a one-line comment pointing at the self-dev host-venv cause.

**Step 3 — tests.** In `tests/test_gates.py` (`TestGateSystem`) add units. The
existing `check_tests` tests `@patch('subprocess.run')` globally; your probe
also calls `subprocess.run`, so use `side_effect` to distinguish the probe
(`["...","-c","import pytest"]`) from the pytest run. Cover:
- project venv python that CAN import pytest → it is used.
- project venv python that CANNOT import → falls back to a PATH interpreter that
  can (assert the pytest invocation ran with the fallback, not the venv python).
- confirm existing `test_check_tests_pass` / `test_check_tests_fail` still pass
  (adjust their mocks to a `side_effect` if the added probe call breaks them).

**Verify:** `python -m pytest tests/test_gates.py -q`

---

### Task 3 (Wave 1, after 1+2): container smoke test

**Files:** `tests/test_container_provisioning_smoke.py` (new)

**Why:** the prior bundle's mocked-path units missed both real breakages. Add a
`requires_container` smoke test using the `tests/_lmer_runtime.py` harness and
the `tests/test_lmer_integration.py` subprocess pattern (`lmer --no-task --exec
'<cmd>'`).

**Assertions (keep narrow):**
- With `LMER_NAPKIN_REPO` set, `~/napkin` resolves to a real checkout — e.g.
  `lmer ... --exec 'test -e ~/napkin/.git && echo NAPKIN_OK'` prints `NAPKIN_OK`
  (not a dangling link). Skip/xfail cleanly if no napkin repo URL is available
  in the test env rather than hard-failing on missing creds.
- The gate test-interpreter resolves to one that can import pytest inside the
  container: `lmer ... --exec 'python -c "import pytest"'` exits 0. (This
  exercises Fix 2's fallback target in the real container filesystem.)
- Gate the whole module with `@requires_container` (and `requires_lmer_venv`
  where it shells `lmer`) so it skips without a runtime socket.

**Verify:** `python -m pytest tests/test_container_provisioning_smoke.py -q`
(expected: skipped when no container runtime; green where one exists).
