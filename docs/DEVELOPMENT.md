# Development Conventions

Design decisions and code conventions for working on lmer itself (`src/`,
`libexec/`, `bin/`). This is contributor-facing documentation; for operating
lmer, start at [INDEX.md](./INDEX.md). Add new decisions as dated sections so
the rationale survives the people who made them.

## Structured records: stdlib dataclasses; pydantic only at the FastAPI wire boundary

**Decision (2026-07-15):** internal structured records use stdlib
`@dataclass` (frozen where the record is registry- or config-like). Pydantic
models are used only where FastAPI requires them for request/response
validation (`_InputBody` / `_OutputResponse` in `src/lmer_cli/supervisor.py`)
— do not introduce pydantic for internal data structures.

Rationale:

- **Pydantic is not a declared dependency.** It arrives transitively via
  `fastapi`. Using it for internal records would silently promote a
  transitive dependency into a load-bearing one without buying anything.
- **There is nothing to validate.** Pydantic earns its keep parsing untrusted
  external data into typed objects. lmer's internal records (harness
  registry, gate results, review findings, dispatch lanes, slack sessions)
  are constructed from literals or already-validated values. Structural
  guarantees live in tests — e.g.
  `tests/test_harness.py::TestRegistryShape` — which also pin properties no
  type system expresses (naming conventions, uniqueness across entries,
  referenced files existing and being executable on disk).
- **Some modules are deliberately stdlib-only** so container-side code can
  import them cheaply in minimal contexts and no import cycles are possible.
  `src/lmer_cli/harness.py` is the canonical example.

Existing dataclass record sites: `lmer_cli/harness.py`, `lmer_cli/gates.py`,
`lmer_cli/container/dispatch_agents.py`, `gitlab_reviewer/client.py`,
`work_repo/findings.py`, `slack_chat/sessions.py`.

**How to apply:** new internal record types follow the dataclass pattern of
those modules. Reach for a pydantic model only when data crosses a validated
wire boundary — a FastAPI endpoint, or a future surface that parses untrusted
payloads.

## Changelog entries: changelog.d fragments, never the unreleased block

**Decision (2026-07-18):** unreleased changelog entries are written as
per-branch YAML fragments under `changelog.d/` (format and naming in
[`changelog.d/README.md`](../changelog.d/README.md)), not appended to
`CHANGELOG.yaml`'s `unreleased:` lists. `ctl changelog release` rolls
fragments into the version section at release time (requires ctl with
fragment support — the `feature/changelog-d` branch of
[20c/ctl](https://github.com/20c/ctl); not yet in a released ctl version).

Rationale:

- **`unreleased:` was the single most conflict-prone spot in the repo.**
  Every concurrent branch appended to the same lines, so every merge
  re-conflicted the rest of the MR queue. Fragments are one file per
  branch — the conflict class is structurally gone.
- **The YAML-canonical model is preserved.** Fragments use the same section
  vocabulary (`added`/`fixed`/`changed`/`deprecated`/`removed`/`security`)
  and roll into `CHANGELOG.yaml`, which remains the source of truth;
  text renderers like towncrier/scriv were rejected upstream for breaking
  that model.

**How to apply:** one fragment per branch, `changelog.d/YYYYMMDD-<branch-slug>.yaml`,
mapping section → list of entry strings. Entry-quality rules unchanged
(user-facing perspective; skip internal refactors/test-only/CI changes).
The gate-check Changelog check accepts a staged fragment as the changelog
update; release commits that rewrite `CHANGELOG.yaml` itself still pass.
