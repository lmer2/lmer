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
