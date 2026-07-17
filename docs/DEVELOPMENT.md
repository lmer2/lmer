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
