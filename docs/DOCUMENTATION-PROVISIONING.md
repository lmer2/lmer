# Documentation Provisioning

When lmer launches a container to work on a target repository, the agent relies on `AGENTS.md` and `rules/*.md` files to guide its behavior. Not every repository ships these files, and maintaining identical copies across many projects creates drift.

Documentation provisioning solves this by automatically providing default documentation files when the target repository is missing them, using a layered override system.

## How It Works

After the target repository is cloned into `/workspace` (and the work repository into `/work`), lmer checks whether each documentation file exists in the workspace. For any file that is missing, it is copied from a fallback source as a real file into the workspace. The agent reads and follows these files exactly as if the project shipped them — this is not a gate-check bypass but actual documentation that guides agent behavior.

If no source provides a given file (the project doesn't have it, the work repo doesn't have it, and lmer doesn't have it), the file simply won't exist and gate-check will still fail with "Critical documentation missing."

### Override Hierarchy

Sources are checked in order from highest to lowest priority:

| Priority | Source | Location | Description |
|----------|--------|----------|-------------|
| 1 (highest) | Project repo | `/workspace/` | The repository's own files always win |
| 2 | Work repo | `/work/{host}/{project}/info/` | Project-specific overrides maintained in the work repo |
| 3 (lowest) | lmer global | `/Agents/global/` | Generic defaults shipped with lmer |

For each file (e.g., `AGENTS.md`, `rules/git.md`):

1. If the project repo already has the file, nothing happens.
2. Otherwise, lmer checks the work repo at `{host}/{project}/info/{file}`.
3. If that doesn't exist either, lmer copies from its own global defaults at `/Agents/global/{file}`.

### Files Provisioned

- `AGENTS.md` -- top-level agent configuration
- `rules/*.md` -- all rule modules discovered in lmer's `rules/` directory (e.g., `git.md`, `testing.md`, `code-quality.md`, `security.md`, `documentation.md`, `ci-cd.md`, `dependencies.md`)

The set of rules files is discovered dynamically from what lmer ships, so adding a new rule module to lmer automatically makes it available as a fallback.

## Git Integration

Provisioned files are development-time artifacts that should not be committed to the target repository. To prevent them from appearing as untracked changes:

- Each provisioned file is added to `.git/info/exclude` (a local gitignore that is not committed).
- A `.lmer-provisioned-docs` marker file is written to the workspace root, listing all provisioned files. This marker is also excluded from git tracking.

This means `git status`, `git add`, and the gate system all ignore these files cleanly.

## Gate-Check Behavior

The gate system's documentation check (`check_documentation`) is aware of provisioning:

- **PASSED**: All required docs exist and are native to the project repo.
- **WARNING**: Required docs exist but some were provisioned by lmer. The warning tells the agent that lmer defaults are in use and suggests adding project-specific files to the repository.
- **FAILED**: Required docs are missing entirely. This happens when provisioning did not run, failed, or no fallback source had the file. The gate blocks the commit.

The WARNING is non-critical and does not block commits. The FAILED status is unchanged from pre-provisioning behavior — provisioning reduces how often it triggers but does not remove the safety net.

## Work Repo Overrides

To provide project-specific documentation without modifying the target repository, place files in the work repo under:

```
{host}/{project}/info/AGENTS.md
{host}/{project}/info/rules/git.md
{host}/{project}/info/rules/testing.md
...
```

For example, to provide custom testing rules for `git.example.com/myorg/myproject`:

```
git.example.com/myorg/myproject/info/rules/testing.md
```

These override lmer's generic defaults but are still overridden by files in the project repo itself.

## Rationale

### Why not require every repo to have AGENTS.md?

Many repositories the agent works on are third-party or upstream projects. Requiring them to ship lmer-specific configuration creates unnecessary friction and maintenance burden. The agent should be able to work productively with sensible defaults on any repository.

### Why a layered system instead of just defaults?

Different projects have different conventions. A Python library has different testing rules than a Go microservice. The work repo layer lets operators maintain per-project overrides centrally without forking or modifying the target repositories.

### Why copy instead of symlink?

Copied files are self-contained within the workspace. Symlinks pointing to `/Agents/global/` would create a runtime dependency and could be confusing when reading the workspace. Copies also allow the agent to see the files in their expected locations without any special path resolution.

### Why .git/info/exclude instead of .gitignore?

`.gitignore` is a tracked file -- modifying it would itself show up as an untracked change and could conflict with the project's existing `.gitignore`. `.git/info/exclude` is local to the clone and is never committed, making it the right place for machine-generated exclusions.

## Implementation

The provisioning logic lives in:

- `src/lmer_cli/container/clone_and_exec.py` -- `provision_documentation()` function, called during container startup after the target repo and work repo are cloned.
- `src/lmer_cli/gates.py` -- `check_documentation()` and `_read_provisioned_docs()` handle the gate-check awareness.
