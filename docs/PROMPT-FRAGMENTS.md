# Prompt Fragments

LMER can inject small markdown sections into Claude's system prompt at
container start-up. These sections are called **prompt fragments**. They let
you feed session-specific context (e.g. the human user's identity) to the
model without hard-coding text in shell and without modifying the target
project's `AGENTS.md`.

`human-identity` is the only *templated* fragment today, but the pieces
(`render-prompt-fragment.py`, the template search, the append step) are
deliberately generic and intended to be reused for future fragments. Two
more fragments ship as plain markdown (`prompts/agent-memory.md`,
`prompts/non-interactive.md`): they carry no session values, so they skip
the renderer and are concatenated directly. Everything below about *when*
a fragment is injected applies to them unchanged — each is gated on its own
env var (`LMER_PERSIST_AGENT_MEMORY`, `LMER_NONINTERACTIVE`).

## How it works

Three components cooperate:

1. **Templates** live under `prompts/`. Each template is a Jinja2 file
   (`*.md.jinja2`) whose rendered output is a self-contained markdown
   section (a heading plus a few lines of prose is typical).

2. **The renderer** (`libexec/render-prompt-fragment.py`) is a small Jinja2
   CLI. Given a template path, it renders the template to stdout using a
   context built from every `LMER_*` environment variable currently set —
   minus any keys or values flagged as sensitive (see below).

3. **`libexec/claude-runner.sh`** decides which fragments to inject. For
   each fragment, it:
   - Gates on whatever signal makes the fragment relevant (typically an
     env var being non-empty).
   - Locates the template by searching a list of candidate paths.
   - Locates the renderer by searching a list of candidate paths.
   - Creates (or reuses) a combined prompt file under `/tmp` that starts
     with the workspace or user `AGENTS.md`, appends the rendered fragment,
     and passes the result to `claude` via `--append-system-prompt-file`.

   Non-claude harnesses reuse the same templates and renderer through
   `harness_render_global_context` in `libexec/harness-common.sh`, which
   writes the rendered fragments (plus `~/.lmer/AGENTS.md`) to the harness's
   *global* context file (e.g. `~/.codex/AGENTS.md`) instead of a CLI flag —
   those harnesses read the workspace `AGENTS.md` natively. See
   [HARNESSES.md](./HARNESSES.md).

The upshot is that the final system prompt Claude sees is:

```
<workspace AGENTS.md or ~/.lmer/AGENTS.md>

<fragment 1>

<fragment 2>
…
```

## Template search precedence

`claude-runner.sh` searches these paths in order and uses the first match:

1. `$(dirname "$0")/../prompts/<name>.md.jinja2` — sibling of the running
   script, so an in-tree checkout (e.g. a CI build directory or
   `/workspace/` in a dev container) picks up the repo copy.
2. `/workspace/prompts/<name>.md.jinja2` — self-development mode where the
   repo is mounted at `/workspace`.
3. `$LMER_HOME/prompts/<name>.md.jinja2` — user install.
4. `/Agents/global/prompts/<name>.md.jinja2` — operational global install.

The same precedence list is used for the renderer.

## Context exposed to templates

The renderer exposes every `LMER_*` environment variable to the Jinja2
context — but not unconditionally. An entry is excluded if **either** of
the following holds:

- **Sensitive key name.** Name matches
  `TOKEN | KEY | SECRET | PASSWORD | CREDENTIALS` (case-insensitive).
- **URL with embedded credentials.** Value is a URL whose netloc contains a
  username or password — e.g. `LMER_REPO_URL` when it carries an oauth
  token: `https://oauth2:glpat-…@host/…`.

This means a template can safely reference any `LMER_*` name: if the value
is sensitive, the variable will be undefined in the context and standard
Jinja2 default handling applies (rendering `{{ LMER_FOO_TOKEN }}` with no
default will fail loudly rather than leak). Non-LMER env vars are never
exposed.

> **Auto-escape is off.** The renderer produces markdown, not HTML, so
> values containing `<`, `&`, etc. pass through verbatim. That is the right
> default for system-prompt content; be aware of it if you ever reuse this
> for a different output format.

## Anatomy of a fragment

Using the shipped `human-identity` fragment as a reference:

**`prompts/human-identity.md.jinja2`** — the content:

```jinja
## Human user identity

You are in an interactive session with: {{ LMER_HUMAN_IDENTITY }}

When you encounter this name, email address, or related handles in pull
requests, merge requests, issues, comments, commit history, or other
repository artifacts, attribute them to the human user you are
collaborating with.
```

**`libexec/claude-runner.sh`** — the gate-and-append block:

```bash
if [ -n "$(printf '%s' "$LMER_HUMAN_IDENTITY" | tr -d '[:space:]')" ]; then
    IDENTITY_TEMPLATE=""
    for candidate in \
        "$(dirname "$0")/../prompts/human-identity.md.jinja2" \
        "/workspace/prompts/human-identity.md.jinja2" \
        "$LMER_HOME/prompts/human-identity.md.jinja2" \
        "/Agents/global/prompts/human-identity.md.jinja2"; do
        if [ -f "$candidate" ]; then
            IDENTITY_TEMPLATE="$candidate"
            break
        fi
    done
    # …same for $RENDERER…

    if [ -n "$IDENTITY_TEMPLATE" ] && [ -n "$RENDERER" ]; then
        # create or reuse $AGENTS_COMBINED, then:
        python3 "$RENDERER" "$IDENTITY_TEMPLATE" >> "$AGENTS_COMBINED"
        AGENTS_PROMPT_ARGS="--append-system-prompt-file $AGENTS_COMBINED"
    fi
fi
```

The `tr -d '[:space:]'` test rejects whitespace-only values. The host-side
resolver (`resolve_human_identity()` in `src/lmer_cli/util.py`) already
strips whitespace, but the shell-side check is defence-in-depth for
container environments populated from other sources (e.g. manual
`docker run -e`, a `.env` file).

## Adding a new fragment

To ship another fragment — say, one that pins the active task target so
Claude doesn't have to re-derive it from env vars:

1. **Pick a gate.** Decide which env var (or other signal) determines that
   the fragment is relevant. Convention: one env var per fragment, named
   `LMER_<PURPOSE>`.

2. **Write the template** at `prompts/<name>.md.jinja2`. Start with a `##`
   heading so the section is distinguishable inside the combined prompt.
   Reference whichever `LMER_*` variables the template needs; the renderer
   will have them in scope (unless they are sensitive — see above).

3. **Forward the env var** into the container. For `LMER_*` variables the
   host CLI forwards, add an entry to the env dict built in
   `src/lmer_cli/cli.py` — pattern follows the existing `LMER_HUMAN_IDENTITY`
   and `LMER_REASONING_EFFORT` entries. If the var is purely container-side
   (set from `.env` or by another hook), you can skip this step.

4. **Add a block to `libexec/claude-runner.sh`** modelled on the
   `human-identity` block above:
   - Gate on the relevant signal (non-empty after whitespace trim if it
     comes from an env var).
   - Search for the template and the renderer via the four-path list.
   - Create `$AGENTS_COMBINED` if it doesn't already exist (the first
     fragment seeds it from `$WORKSPACE_AGENTS` or `$USER_AGENTS`).
   - Append a blank-line separator then pipe the renderer into
     `$AGENTS_COMBINED`.
   - Set `$AGENTS_PROMPT_ARGS` to
     `--append-system-prompt-file $AGENTS_COMBINED`.

5. **Tests.** Mirror `tests/test_human_identity.py`:
   - Packaged-template sanity (file exists, references the expected var,
     renders without error when the var is set).
   - End-to-end claude-runner behaviour (unset → no injection; whitespace
     → no injection; set → section appears in the prompt file).
   - Shell-injection safety (a value containing `$(…)` and backticks must
     not be evaluated).
   - If the fragment depends on a new CLI resolver helper, unit-test it
     directly with mocked subprocess calls where applicable.

6. **Document the env var** in `docs/LMER-CLI.md` under *LMER-Specific
   Environment Variables*, and point at the template filename so users
   know where to edit the wording.

## Why a template, not a shell heredoc

The wording of prompt fragments is load-bearing — it goes straight into
the model's context and shapes behaviour. Keeping it in a Jinja2 file
rather than `printf` / heredoc inside `claude-runner.sh` means:

- Edits don't touch shell code, so there is less risk of breaking the
  boot path while tweaking phrasing.
- Templates can be reviewed without reading around `"`-escaping.
- Values are interpolated by Jinja2, not by the shell, so shell
  metacharacters in the env value can't be evaluated (covered by
  `TestClaudeRunnerHumanIdentity::test_value_with_shell_metacharacters_is_safe`).

## Files

| Path | Purpose |
| --- | --- |
| `prompts/*.md.jinja2` | Fragment templates. |
| `libexec/render-prompt-fragment.py` | Jinja2 CLI renderer. |
| `libexec/claude-runner.sh` | Orchestrates which fragments to inject per session. |
| `src/lmer_cli/util.py` | Host-side resolver helpers (e.g. `resolve_human_identity`). |
| `src/lmer_cli/cli.py` | Forwards relevant `LMER_*` vars host → container. |
| `tests/test_human_identity.py` | Reference test suite for a fragment. |
