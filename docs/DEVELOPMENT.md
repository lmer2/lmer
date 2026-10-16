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

## Manifest rewrites: ruamel.yaml round trip for `_lmer.yaml`, PyYAML everywhere else

**Decision (2026-10-07):** tool rewrites of `_lmer.yaml` — the registry
writer (#366) and its consumers `work adopt` (#367) and `work migrate`
(#368) — go through ruamel.yaml's round-trip mode, declared as a core
dependency (`ruamel.yaml>=0.19.1,<0.20`) that ONLY the manifest writer
imports. The read path — `lmer_schema.manifest`, `lmer_cli.container.sources`
and `lmer_cli.container.clone_and_exec` — stays PyYAML-only, and every other
YAML file lmer writes keeps PyYAML `safe_dump(sort_keys=False)`. The spike
that measured this (issue #365) is
`tests/test_lmer_schema_manifest_roundtrip.py` against
`tests/fixtures/plexus_manifest/commented.yaml`, on ruamel.yaml 0.19.1 and
PyYAML 6.0.3; the fixture and the test are the contract the writer inherits.

The issue's four questions, in order:

1. **Comments, blank lines, quoting, indentation.** With the configuration
   below, an unchanged document round-trips byte for byte, and each write
   the writer needs — insert an entry (`repo`/`remotes`/`description`; first,
   between two entries, appended), remove an entry (first, middle, last),
   canonicalize one `repo:` — produces exactly the intended lines and
   nothing else. The fixture covers a file header, a full-line comment above
   a top-level key, an end-of-line comment on a scalar, a comment between
   `projects:` entries, comments inside a `remotes:` list (full-line and
   trailing an item), inside `sources:` and inside `housekeeping:`, blank
   lines between sections, `'single'` and `"double"` quotes, flow mappings
   (`{repo: ...}`, `{}`, `[]`) and a long `description:`. Where it is not
   free:
   - ruamel hangs everything that FOLLOWS a scalar — its end-of-line
     comment, the blank lines after it and the full-line comments above the
     next key — on that scalar's comment token as one string. A naive
     `mapping[name] = entry` therefore lands the new entry BELOW the comment
     that annotates its neighbour (or below the blank line before
     `sources:`), and a naive `del mapping[name]` takes the next entry's
     comment, or the blank line before the next section, with it. The
     writer must relocate that remainder: on insert, everything after the
     previous leaf's end-of-line comment moves to the new entry's last
     leaf; on remove, the contiguous full-line comments directly above the
     removed key are its own and go with it, and whatever followed its last
     leaf moves back to the previous leaf (to the section key's slot when
     the first entry goes). `_insert_entry`/`_remove_entry` in the test are
     that rule measured — about forty lines, mutating ruamel's tokens in
     place because one token object can sit in two slots.
   - `null` spelling is normalized: `~` and an empty value are written back
     as `null`, SCHEMA.md's spelling (ruamel has no preserved-null type).
     The value is unchanged; this is the one line that can differ from the
     author's bytes without an edit.
   - A rewritten scalar keeps its end-of-line comment on the line at the
     column it had: ruamel pads a shorter value out to that column, and a
     value that runs past it pushes the comment to one space after.
   - Indentation is the writer's, not sniffed: `indent(mapping=2,
     sequence=4, offset=2)` is SCHEMA.md's style (list items indented under
     their key). A manifest hand-written with `- item` flush with its key is
     re-indented wholesale on its first rewrite — a one-time, lossless,
     visible diff — and keeps the tool's style from then on.

   All of it is tolerable: no comment is lost, none moves relative to the
   key it annotates. Not measured and left to #366: an entry written with no
   keys at all (`name:` / `name: {}`, a D19 repoless project named alone)
   has no leaf to carry a following comment, so the relocation needs a
   fallback to the entry's own key slot.

   Configuration: `YAML(typ="rt")`, `preserve_quotes = True` (without it
   ruamel drops quotes it deems unnecessary), the `indent` above,
   `width = 4096` (never fold a long `description:`), and a `None`
   representer that writes `null`, registered on a `RoundTripRepresenter`
   subclass so the shared class is untouched.

2. **The `schema:` int.** Round-trip mode loads a plain `1`/`2` as a Python
   `int` and writes it back unchanged — a rewrite never bumps it to
   `CURRENT_SCHEMA`; that is a migration's job — and PyYAML reads the
   result back as an int. The write sequence is `parse_manifest(text)`
   first, ruamel second: the PyYAML read path is the gate, so a NEWER int
   is refused with the §8 "upgrade lmer" `ManifestError` and `schema: true`
   by the bool trap before ruamel opens the document, and the file on disk
   is untouched byte for byte
   (`test_newer_or_bool_schema_is_refused_before_any_write`).

3. **Dependency cost.** A core dependency; not an optional extra, and not
   PyYAML with documented loss.
   - Two YAML libraries in one package is the price, and it is small:
     ruamel.yaml 0.19 is pure Python — it dropped `ruamel.yaml.clib` as a
     dependency (the C accelerator is now its own extra, and round-trip
     mode never uses it) — a 118 KB wheel, 1.3 MB installed, no dependencies of its
     own, so it carries none of the wheel-availability risk that made
     `matrix` an extra.
   - An extra would make `work adopt`/`work migrate` fail at first use for
     anyone who installed `lmer` without it. The operator-shell verbs are
     what the package install is for; an ImportError at the moment of
     adoption is a worse first impression than the stripped comments the
     issue was filed to prevent.
   - PyYAML with documented loss is what the spike was meant to rule in or
     out, and it is out: the manifest is the one file operators annotate,
     and ruamel keeps every position measured.
   - The read side costs nothing. The writer is its own `lmer_schema`
     module and the only importer; `clone_and_exec` runs standalone with
     its guarded PyYAML import and never writes the manifest. The container
     venv is `uv sync`ed from the lock, so ruamel is present there too, but
     nothing in the container imports it: the test imports all three read
     modules in a fresh interpreter and asserts no `ruamel*` module loaded,
     and checks their source for the name.

4. **Version pinning.** `ruamel.yaml>=0.19.1,<0.20` in `pyproject.toml`;
   `uv.lock` holds the exact version (0.19.1, 2026-01-02) for our own
   installs and the container image, under the usual one-week
   `exclude-newer`. The floor is the first release without the C-library
   dependency. The ceiling is deliberate and otherwise unusual in this
   file: ruamel.yaml is 0.x and each minor so far changed something
   load-bearing (0.18 removed the module-level `load`/`dump` API; 0.19
   swapped the C dependency), while patch releases within a minor have
   been safe. Raising the ceiling is a one-line bump plus
   `uv lock --upgrade-package ruamel.yaml` and a green
   `tests/test_lmer_schema_manifest_roundtrip.py` — the round-trip test is
   the real upgrade gate, so a minor that changes comment attachment fails
   in CI, not in an operator's manifest.

**How to apply:** the registry writer (#366) is one module under
`lmer_schema/` that imports ruamel at module scope and is imported by
nothing on the read path; it calls `parse_manifest` before `YAML(typ="rt")`,
uses the configuration above, implements the relocation rule (port the
test's helpers; do not import them), and writes `null` for `None`. `work
adopt` and `work migrate` stay thin consumers of that API with the
no-YAML-dump guard test spec §13.B asks for. Every other YAML file lmer
writes (run records, indexes, changelog fragments) keeps PyYAML; do not
reach for ruamel because it is now installed. Add a comment position to the
fixture before relying on it, and extend the round-trip test with the write
that needs it.
