# Gate Fast Paths

How the commit and push gates avoid re-running a test suite that cannot tell them anything new — and, just as important, when they refuse to.

- [Why](#why)
- [The two mechanisms](#the-two-mechanisms)
- [Text-only fast path](#text-only-fast-path)
  - [When it engages](#when-it-engages)
  - [What counts as text](#what-counts-as-text)
  - [Declaring the subset](#declaring-the-subset)
  - [Why "tests that read text", not "safe to skip"](#why-tests-that-read-text-not-safe-to-skip)
  - [The guard scan is a floor, not a proof](#the-guard-scan-is-a-floor-not-a-proof)
- [Test-result cache](#test-result-cache)
  - [What the key covers](#what-the-key-covers)
  - [The environment is checked in the entry](#the-environment-is-checked-in-the-entry)
  - [What is cached, and what is not](#what-is-cached-and-what-is-not)
  - [Entry safety](#entry-safety)
  - [What the key still cannot see](#what-the-key-still-cannot-see)
- [When the full suite always runs](#when-the-full-suite-always-runs)
- [Kill switches](#kill-switches)
- [Reading a gate receipt](#reading-a-gate-receipt)

## Why

The suite is the dominant cost of every gate, and the gates sit on the critical path of every commit, every push, and twice per release. When a gate is slow enough that people route around it, the gate stops being a safety net — the 0.7.0 release paid for seven-plus full runs and two of those were avoided only by waiving the gate by hand.

Both mechanisms below exist to make the fast path the honest default. Neither one publishes untested code: each is a claim that a specific run would tell us nothing we do not already know, and each fails toward running the suite whenever it cannot make that claim.

## The two mechanisms

| | Text-only fast path | Test-result cache |
|---|---|---|
| **Question it answers** | "Can this change only affect a few tests?" | "Has this exact thing already passed?" |
| **Trigger** | every changed path is prose | the same tree, invocation, interpreter, dependencies and environment as a recorded pass |
| **Effect** | runs the declared subset | runs nothing |
| **Opt-in** | per project, via a declaration | automatic |
| **Kill switch** | `LMER_GATE_NO_FASTPATH=1` | `LMER_GATE_NO_CACHE=1` |

They compose: because the exact pytest invocation is part of the cache key, a subset run and a full run are different keys, so a narrowed pass can never satisfy a later full-suite need.

## Text-only fast path

### When it engages

The gate classifies the paths a change touches:

- **commit gate** — the staged set;
- **push gate** — the commit range being pushed, for a push of the current branch to its upstream.

If every path is text *and* the project declares a subset, `check_tests` runs that subset instead of the whole suite:

```
⏭️  Text-only diff — 1 changed path(s), all text:
    docs/CONTAINER.md
    Running the declared text-reading subset (34 path(s)) instead of the full suite:
    tests/test_consistency.py, tests/test_containerfile.py, …
    (declared in .lmer/gate-check.yaml → tests.text_diff_subset)
```

### What counts as text

`*.md`, `*.rst`, `changelog.d/*.yaml`, `changelog.d/*.yml`, `CHANGELOG.yaml`, `LICENSE`.

The list is deliberately narrower than "files that look like prose", because it is **global** — `gates.py` gates every repository lmer is pointed at — while the subset that makes calling something text safe is **per-repository**. Two exclusions worth stating:

- **`.txt` is not prose.** `requirements.txt` is a dependency set; `taskdef/*/instructions.txt` is executable instruction text.
- **`docs/` is a location, not a role.** `docs/conf.py` executes.

Both classify as code and take the full suite.

### Declaring the subset

```yaml
# .lmer/gate-check.yaml, in the repository being gated
tests:
  text_diff_subset:
    - tests/test_security.py
    - tests/test_consistency.py
```

The declaration lives in the gated repository because it names that repository's own test files and should be versioned with them. The work repo's `info/gate-check.yaml` is consulted as a fallback.

**A project that declares nothing gets the full suite, always.** That is the default, and it is what keeps the mechanism sound in repositories nobody has audited.

### Why "tests that read text", not "safe to skip"

The obvious design is to skip the suite outright for a docs-only change. That was measured against this repository and it is false here: `tests/test_security.py` walks every `*.md` and `*.yaml` in the tree and asserts on their **content**, `tests/test_consistency.py` asserts that paths referenced in Markdown exist, and others read `AGENTS.md`, `README.md`, `LICENSE` and rule files by name. A documentation-only change can fail this suite — including the leak class that publication-safety scrubs exist to catch.

So the declaration is not an allowlist of files nobody tests. It is the set of tests that **read** text, and on a text-only change those are exactly the tests that still run.

### The guard scan is a floor, not a proof

`tests/test_gate_text_diff_subset.py` scans the test tree and fails when a module that reads text is missing from the declaration, so adding such a test forces a decision rather than silently voiding the fast path.

It models repo-anchored `pathlib` composition, `joinpath`, and `glob`/`rglob`. It is **blind** to:

- `os.path.join` and string/f-string path building
- `os.walk`, `glob.glob`, `iterdir`
- cwd-relative reads such as `open('README.md')`
- tests that shell out to a repository script which itself reads a doc

A test written with one of those idioms must be added to the declaration by hand. **A green scan does not certify the subset complete** — it certifies that nothing the scanner can see is missing.

## Test-result cache

A passing suite is recorded, and a later gate that composes the same key reuses it:

```
✅ Python Tests — cached result for this exact tree
   Proven green 32s ago by gate-check (8921 passed, 40 skipped in 642.44s (0:10:42))
   Tree b5150824a204…, working tree clean, same test invocation, interpreter and environment.
   LMER_GATE_NO_CACHE=1 forces a re-run.
```

This is what removes the repeat runs a release performs on one unchanged tree — 0.7.0 ran the full suite five times over identical code — and it makes a tag push cheap for free, since a tag moves no code and therefore points at an already-green tree.

### What the key covers

| Component | Source |
|---|---|
| Committed tree | `git rev-parse HEAD^{tree}` |
| Uncommitted state | porcelain status with `--untracked-files=all --ignore-submodules=none` pinned, so no git config can hide a change, plus the blob hash of every modified or added file |
| Test invocation | the exact pytest argv |
| Interpreter | resolved path and version string |
| Dependency surface | the interpreter's `pyvenv.cfg` and the distribution names installed in its site directories, so an image rebuilt with different packages under an unchanged tree misses |

The status probe and the file hashing are anchored to the repository root (`git rev-parse --show-toplevel`), so running a gate from a subdirectory cannot make the digest content-blind.

### Commit-to-push handoff

A commit changes the fingerprint even when it changes no tested content: before
the commit, the key is the old `HEAD` plus staged state; afterwards it is the new
`HEAD` plus a clean tree. After `gate-commit` succeeds, it records the passing
suite under that clean post-commit key as well, but only when the new committed
tree exactly equals the index tree captured with the pass and the working tree is
clean. Only `gate-commit` captures that index identity, and only while caching is
enabled; `gate-check` and `gate-push` never run `git write-tree` for a handoff
they cannot consume. `write-tree` leaves staged content and working-tree files
alone, but it can update the index cache-tree, take the index lock, and write tree
objects. A commit hook edit, partial commit, quick gate, unknown fingerprint, or
leftover change declines the handoff, so `gate-push` runs the suite normally.

The exact pytest argv is retained during the handoff. A text-diff subset can
therefore hand off only to the same subset key; it still cannot satisfy a
full-suite lookup.

### The environment is checked in the entry

The environment the suite is handed has to match too — pytest reads `PYTEST_ADDOPTS`, and a suite's tests can branch on any other variable. It is checked out of the **entry** rather than out of the filename: the entry carries one digest of the environment plus a digest per variable name, so a mismatch is still a miss *and* the gate can say what moved.

```
ℹ️  Cache miss: same tree and invocation, environment differs (PYTEST_ADDOPTS)
```

**Values are never stored or printed** — only digests and names — because the environment carries credentials.

Four variables are excluded as volatile and inert: `_`, `SHLVL`, `OLDPWD`, `__MISE_SESSION`. The last one is the token mise's shell activation mints per interactive shell; keyed on, it made the cache inert, because a gate runs from a fresh login shell every time.

That exclusion list is a **denylist**, not an allowlist of "variables that can reach pytest", because of which way each fails. An allowlist silently excludes every variable nobody thought of, and an excluded variable that does matter yields a false hit. A denylist's missing entry only costs a suite run — one that now announces itself in the miss notice.

### What is cached, and what is not

- **Only passes.** A failing suite is never recorded.
- **Only `check_tests`.** Every cheap check runs every time; so does a project-supplied `gate-check-run-tests.sh`, whose inputs live outside everything the key covers.
- **Nothing, when anything is unknown.** Not a git repository, a git command that fails, a dirty submodule, an unreadable site directory, a cache directory this uid does not own — each means no read and no write.
- The pass is confirmed after the run: if the fingerprint moved while the suite was executing, nothing is recorded. An edit still present when the suite ends prevents the write; an edit made and reverted inside the run is not detected.

### Entry safety

An entry authorizes *skipping* the suite, so it is treated as security-relevant rather than as a scratch file. The directory is created `0700` (tightened if found looser, refused outright if it is a symlink or owned by another uid — the default lives in a world-writable `/tmp`), entries are written `0600` via a temp plus rename, and an entry whose recorded tree, working digest or environment digest disagrees with the fingerprint that found it is ignored. A hand-written file cannot mint a pass.

Entries are one JSON file per key under `LMER_GATE_CACHE_DIR`, expire after 7 days, and are pruned opportunistically on write.

### What the key still cannot see

Stated plainly, because a cache's honesty is exactly its list of blind spots:

- a package whose `.dist-info` name did not change — a rebuilt wheel of the same version, an editable install whose source moved;
- a distribution installed outside the interpreter's own site directories;
- the machine around the run — kernel, libc, the C libraries an extension links against, the clock, the network.

That residue is also why the cache defaults to the container's `/tmp` and is not meant to outlive it: a cached verdict describes a tree *and* the environment that ran it.

## When the full suite always runs

Both mechanisms fail toward running everything. The full suite runs when:

- the project declares no `tests.text_diff_subset` (fast path), or nothing is recorded for the key (cache);
- any changed path is not text;
- the working tree has unstaged or untracked changes, for the commit classifier;
- the diff cannot be resolved — a first push with no remote-tracking ref, a failing git command;
- the push names an explicit `--ref` or `--tag`. The range the classifier can name is `HEAD` against the current branch's upstream, which is not what an explicit refspec pushes;
- a declared subset path no longer exists (the gate warns and falls back);
- either kill switch is set.

The initial clean-state baseline of a development run has one more route, `gate-check --baseline`, which can reuse CI evidence for the exact starting commit instead of running the suite. It never reads or writes this cache; see [GATE-CI-BASELINE.md](./GATE-CI-BASELINE.md).

## Kill switches

| Variable | Effect |
|---|---|
| `LMER_GATE_NO_FASTPATH=1` | Run the full suite even for a text-only diff |
| `LMER_GATE_NO_CACHE=1` | Re-run the suite and record nothing |
| `LMER_GATE_CACHE_DIR` | Where entries live (default `/tmp/lmer-gate-cache`) |

All three are parsed the usual way (`1`/`true`/`yes`, case-insensitive) and are forwarded into the container by the host CLI, since the gates run there.

## Reading a gate receipt

Every gate receipt with a test scope carries `test_scope`, `test_targets`,
`test_cache_verdict`, and `test_cache_reason`, so a reader can tell both what a
green proved and why the cache did or did not answer. Verdicts are `hit`, `miss`,
`disabled`, or `unknown`; reasons name environment variables but never their
values.

| `test_scope` | Meaning |
|---|---|
| `full suite` | the whole suite ran |
| `text-diff subset` | the declared text-reading subset ran |
| `cached full suite` | a recorded full-suite pass was reused |
| `cached text-diff subset` | a recorded subset pass was reused |

The field is **absent** when the run cannot say — a custom test runner, no `tests/` directory, `LMER_QUICK_GATE_COMMIT`, a bypass. Absence must never be read as "full suite".

See also: [RUN-STATE.md](./RUN-STATE.md) for the receipt schema, [LMER-CLI.md](./LMER-CLI.md) for the environment variables in context.

## Optional full pre-commit pass reuse

Pre-commit reuse is a separate, operator-owned opt-in and is off by default. It
may be enabled only from the work repo's `{host}/{project}/info/gate-check.yaml`,
outside the tree being gated, after auditing the project's pinned hooks:

```yaml
precommit:
  reuse_all_files: true
```

Only a successful `pre-commit run --all-files` pass can be reused, for 15
minutes. Its identity covers the exact on-disk content and tracked path set that
`--all-files` checks (so committing already-tracked modifications with unchanged
bytes remains reusable), the complete `.pre-commit-config.yaml` (therefore every
hook revision), the resolved executable bytes and version, exact argv, and every
inherited environment variable (values are hashed, never stored). The key also
covers the effective external attributes files, staged-added path projection,
and merge/rebase markers used by this project's pinned `check-added-large-files`
and `check-merge-conflict` hooks. Any missing or changed input is a miss. The
fingerprint is recomputed after the run, so a hook autofix or concurrent
checked-content change prevents recording the pass. Entries use an owner-only
`/tmp/lmer-precommit-cache` directory and fail soft to running the hooks
normally. Override that directory with `LMER_PRECOMMIT_CACHE_DIR`.


## Parallel test runner overrides

Parallel execution is an operator-owned, work-repo-only opt-in for the **test
runner step**. Lint, secrets, permissions and the other gate checks still run.
Activation is per project: shipping a runner helper does not activate it. The
operator enables the work-repo policy after validating that project's suite
under its allocated CPU and memory limits.
Third-party projects need no source-repo configuration, helper or dependency
change: their work override invokes what their environment already supports.

Put this pair in `{host}/{project}/info/`, or in
`{host}/{project}/{task}/info/` for a task override:

```text
gate-check.yaml
gate-check-run-tests-parallel.sh  (executable)
```

```yaml
tests:
  parallel:
    min_cpus: 4
    min_memory_mib: 4096
```

The parallel script runs with the project root as cwd, with no added arguments,
just like `gate-check-run-tests.sh`. Shared selection is framework-agnostic; the
project owns its command, dependencies, worker count and workload safety. An
executed failure stays a failure: there is no normal-runner retry.

Selection reads task policy before project policy, independently of the existing
generic configuration loader. At each level `gate-check.yaml` wins over `.yml`.
The first explicit `tests.parallel` block must pair with an executable parallel
script **in that same directory**. `tests: {parallel: false}`, invalid policy,
insufficient resources or a missing script stop lower-tier parallel selection.
A valid mapping with no parallel block permits project policy. Malformed root
or `tests` containers stop lookup with a diagnostic. An unreadable policy
file falls back conservatively. Repo-local `.lmer/gate-check.yaml` cannot enable,
disable or override this policy. Other configuration precedence is unchanged.

Normal fallback retains existing task `gate-check-run-tests.sh`, then project
`gate-check-run-tests.sh`, then default pytest precedence. Set
`LMER_GATE_NO_PARALLEL=1` (also `true`/`yes`, forwarded into containers) to force
that normal path. A custom normal command may itself use concurrency; this switch
does not rewrite it.

Eligibility uses finite Linux cgroup v1/v2 CPU quotas and memory limits, taking
visible ancestors into account and bounding CPU allocation by process affinity.
Fractional CPU quotas round down. Unknown limits or affinity, malformed values,
or allocations below either declared minimum select the normal runner. The
memory requirement is an **eligibility floor**, not measured available memory or
a promise of headroom. Cgroup ancestors hidden by the container mount cannot be
inspected; the operator must provide an accurate effective allocation.

### lmer: retain subset, cache and commit handoff

lmer uses the same pair, with one additional explicit contract:

```yaml
tests:
  parallel:
    min_cpus: 4
    min_memory_mib: 4096
    protocol: argv-v1
```

Its executable work script contains:

```sh
#!/bin/sh
exec python3 ci/gate-tests.py "$@"
```

`ci/gate-tests.py` owns pytest/xdist selection. It retains the gate-selected
interpreter and exact targets, checks xdist in that interpreter, and adds at most
four workers, bounded again by quota and affinity. Missing xdist or unknown
resources keeps serial pytest. Its tracked execution wrapper disables override
discovery in nested gates under test and publishes the worker count as
`LMER_TESTS_WAIT_SCALE` (see the connector family below), then execs the already
selected pytest command, including its explicit workers. Dedicated policy tests clear that
inherited switch and exercise selection directly. The cache records the wrapper
and child argv together; no default-pytest receipt is fabricated. pytest-xdist is a development
dependency of lmer only. Install its locked development environment
before enabling the pair; no operational `/Agents/global` edits are needed.

`argv-v1` explicitly opts into command description instead of opaque script
execution. The gate calls the script with `--lmer-command`, JSON stdin
`{"targets": [...], "argv": [...]}`, the normal test environment and a 30-second
timeout. A dedicated lifecycle process owns the descriptor process group and
collects its descendants on completion or failure, with up to four additional
seconds for cleanup.
The script must **only describe**, returning a JSON object containing
its exact executable `argv` and the identical `targets` as acknowledgement. It
must honor those targets without hidden selection or environment changes. Invalid
output, a failed description or an executed suite failure fails the test step.
The gate executes the returned argv directly, without shell interpolation.

Description runs before every cache lookup. The existing fingerprint covers the
actual returned argv, interpreter, dependencies, source tree and execution
environment; the environment additionally carries the work script's SHA-256.
Changing worker choice therefore changes the command key. Existing cache limits
still apply: unknown interpreter/dependency identity means execution without a
cache entry. This contract does not make arbitrary external test environments
cacheable. lmer's helper and test inputs are in the tree being hashed.

The normal text-diff target selector, full/subset receipts, pass recording and
post-commit cache handoff are reused. An unchanged parallel command can hit its
own cache; a subset pass cannot answer for a full suite. Ordinary custom runners,
including parallel overrides without `protocol`, remain opaque and uncached;
their optional mode label does not establish structured coverage.

### Measurement and limits

Issue #454 measured one sequential serial/n2/n4 run each on identical source and
dependency overlay, with a verified quota of `400000 / 100000 = 4` CPUs and
`4294967296` bytes memory. Every run collected 14,695 tests, with 14,646 passing
and 49 skipped; all collection IDs and setup/call/teardown outcomes matched.

| Mode | pytest seconds | Gate seconds | External cleanup seconds | Total seconds |
|---|---:|---:|---:|---:|
| serial | 1761.20 | 1792.38 | 2.04 | 1794.42 |
| n2 | 898.23 | 911.75 | 1.70 | 913.45 |
| n4 | 532.89 | 546.11 | 1.76 | 547.87 |

n4 reduced total time 69.5% against serial, but reached 4096 MiB and increased
cgroup memory allocation-limit failures by 217,614, without an OOM kill. That
is evidence of cap/reclaim pressure. n2 peaked around 3902 MiB without allocation
failures. OS caches were not flushed; initial cgroup memory rose across the
sequential runs, and unrelated host load varied. These single-run observations
justify the small opt-in, not a universal speedup or memory-safety claim.
All benchmark descendants were bounded and reaped; suite/worker shutdown is
included in gate time and external cleanup is reported separately.

Collection, outcome, timing, cgroup and cleanup evidence is retained with the
change review and benchmark report.

### Isolating tests from runner policy

Gate unit tests must use a test-owned work-repo root, including tests that mock
subprocess execution. Reading live task or project policy can add a runner
description subprocess that a serial-runner mock never expected. Policy tests
create their own configuration and executable scripts under that isolated root.

### Tests that wait on real processes

The connector suites and the supervisor teardown tests start real child
processes, real sockets and real threads, so every one of them waits on
wall-clock time. Under four workers, with other gates sharing the host,
whichever bound is tightest trips first — a different test each day through
September 2026. They are held to three rules (issue #480).

**Every bound is a named budget.** `tests/timing_budgets.py` owns them:
`SETTLE` for a child reaching an observable state, `SHUTDOWN` for one reacting
to a signal, `DRAIN` for an in-process connector draining itself, `STOP` for
terminating a child that should already be finished, `CHILD_DRAIN` for what a
child gives its own drain, `TEARDOWN_BOUND` for how fast the supervisor is done
with its child, and so on. No test writes a number. `outlast(budget)` derives an
injected stall from the bound it has to outlive, so the two cannot drift apart. Poll
intervals are the deliberate exception: they stay short and unscaled, because
they decide how quickly a satisfied condition is noticed, not how long a test is
willing to wait. A test that reintroduces a literal fails
`tests/test_timing_budgets.py`.

**One factor scales them together.** `LMER_TESTS_WAIT_SCALE` multiplies every
budget. `ci/gate-tests.py` derives it from the worker count it chose: the
description step adds `-n <workers>`, and the `--run` wrapper that execs that
command publishes the same number as the factor. So the parallel gate runs with
four times the base bounds and a serial run with one. A value already in the
environment is never overwritten.

**Process-spawning modules share one worker.** The five modules that fork
children (`test_connector_process.py`, `test_connector_recovery.py`,
`test_connector_review_boundaries.py`, `test_connector_ingestion.py`,
`test_supervisor_teardown.py`) carry
`pytest.mark.xdist_group(XDIST_GROUP)` — the group name is `process_spawning`.
So does the one test outside them that forks connector children,
`test_lmer_cli_slack_target.py::test_chat_launch_starts_connector_with_digest_only_container`;
the guard finds anything that builds a `ChildHarness` and fails if it is not in
the group, because a hand-kept list is how that one was missed. The marker does
nothing under xdist's default distribution, so the parallel invocation passes
`--dist loadgroup` alongside `-n`. The in-process connector suites stay spread
across the other workers.

#### Running them serially, and reproducing a loaded host

Serially, in the container, from the repository root:

```sh
PYTHONPATH=src python -m pytest tests/test_connector_process.py \
    tests/test_connector_recovery.py tests/test_connector_review_boundaries.py \
    tests/test_connector_ingestion.py tests/test_connector_delivery.py \
    tests/test_connector_lifecycle.py tests/test_connector_ownership.py \
    tests/test_slack_connector.py tests/test_matrix_chat.py \
    tests/test_chat_connector_notices.py tests/test_supervisor_teardown.py \
    "tests/test_lmer_cli_slack_target.py::test_chat_launch_starts_connector_with_digest_only_container" -q
```

That is the run every one of these tests passes in isolation, which is exactly
why a green result there proves nothing about the parallel gate. To reproduce
the conditions that fail, recreate both halves of them:

```sh
# four workers, the group honoured, and the bounds the gate would use
LMER_TESTS_WAIT_SCALE=4 PYTHONPATH=src python -m pytest tests/test_connector_process.py \
    -n 4 --dist loadgroup -q
```

and load the host while it runs — a second full `gate-check`, or
`for i in $(seq "$(nproc)"); do (while :; do :; done) & done` and `kill %1 %2 …`
afterwards. Raising `LMER_TESTS_WAIT_SCALE` alone does not reproduce a flake; it
is the cushion, not the load. Lowering it to `1` under load is the way to make
the family fail on demand and see what the failure now says.

#### What a failure says

A failing wait names each child's pid and exit status, then quotes every log the
harness collected, including a line for a log that really is empty. The fixture
children write a start line, an exit line with the code, and a line for a reload
exec; their logging goes to stderr with timestamps, and they run unbuffered, so
the log is populated even when the child dies before its own `main()`. An empty
child log — the only evidence either 2026-09-16 occurrence left — now means the
child never started, and says so. The recovery CLI is the one child whose output
does not go there: the harness captures it, so it reaches the assertion through
the completed process rather than through the log.

The supervisor teardown tests do the same through their own fixture: a wait that
runs out prints the supervised child's pid, its exit code and everything it
wrote to the supervisor's output buffer.

Fixture teardown joins finalizer threads before restoring registry
monkeypatches, so late persistence cannot reach the operator's real registry.
