# Startup Presets

A **startup preset** is a named, operator-defined startup configuration for an
`lmer` session — a local checkout to mount, a running service container to
target ([service mode](./SERVICE-MODE.md)), extra environment variables, and
extra `lmer` CLI flags, bundled under a single name. Whoever starts a session
selects a preset *by name*; the operator controls what each name maps to.

Presets live in core lmer (`src/lmer_cli/presets.py`) so they are not tied to
any single spawner. Current consumers:

| Consumer | How a preset is selected | Details |
|---|---|---|
| `lmer-slack-listener` (spawns a session per mention/DM) | `$preset:<name>` token in the triggering Slack message, or `LMER_CHAT_PRESET` / `LMER_PRESET` in the listener's own environment as a listener-wide default (a token replaces it) | [Slack-selected presets](#slack-selected-presets) |
| The `lmer` CLI (direct invocations) | `--preset <name>` flag, `LMER_<TASK>_PRESET` (one taskdef) or `LMER_PRESET` (all) env var | [CLI-selected presets](#cli-selected-presets) |
| Agent fan-out (`spawn-harness` children, issue #130) | `--agents <name,...>` flag or `LMER_AGENTS` env var at launch | [Fan-out agents](#fan-out-agents---agents--lmer_agents) |

The file format, validation rules, and trust model below are shared by every
consumer. How a preset *combines* with the rest of the invocation is each
consumer's contract — see [Merge semantics per consumer](#merge-semantics-per-consumer).

## Quick start

Point `LMER_PRESETS_FILE` at a JSON file on the host:

```json
{
  "my_service": {
    "checkout": "/srv/my-service",
    "service": "mysvc",
    "env": { "LMER_LLM_NAME": "opus" },
    "args": ["--ports", "2"]
  }
}
```

Then select the preset by name:

```
# From Slack — token in the message that starts a session
@lmer-bot $preset:my_service can you check why the worker queue is backed up?
```

```bash
# From the CLI — flag or env var (the flag wins)
lmer develop https://gitlab.example.com/group/project/-/issues/12 --preset my_service
LMER_PRESET=my_service lmer develop https://gitlab.example.com/group/project/-/issues/12

# ...or scoped to one taskdef: LMER_<TASK>_PRESET applies to that task only
LMER_DEVELOP_PRESET=my_service lmer develop https://gitlab.example.com/group/project/-/issues/12

# See what's available
lmer --list-presets
```

Either way the session starts with `--checkout /srv/my-service --service mysvc
--ports 2` applied and `LMER_LLM_NAME=opus` in its environment (subject to the
per-consumer precedence rules below).

## The presets file

`LMER_PRESETS_FILE` names a JSON file **on the host**: a single object mapping
preset names to entries. The variable is read host-side only (by the Slack
listener and the `lmer` CLI) and never needs to reach inside a container — the
preset's *effects* (flags, forwarded env vars) do instead. Unset (the default)
disables the feature.

### Fields

Each preset's fields are all optional:

- **`checkout`** — host path to a local source checkout, passed as
  `--checkout` (mounted as `/workspace`). Required whenever `service` is set.
- **`service`** — a running container / Compose service to target, passed as
  `--service` ([service mode](./SERVICE-MODE.md); the agent can then run
  commands in that container via `target-exec`).
- **`env`** — extra environment variables. How they merge with the
  invocation's environment is per-consumer — see
  [Merge semantics per consumer](#merge-semantics-per-consumer). Use for
  "other startup variables" such as `LMER_LLM_NAME` or
  `LMER_REASONING_EFFORT`.
- **`args`** — extra `lmer` CLI tokens (e.g. `["--ports", "2"]`). How they
  combine with the rest of the command line is likewise per-consumer.

### Preset names

Names must use the selector charset `[A-Za-z0-9_-]` — the same pattern the
`$preset:<name>` token can express. A name outside that charset (e.g.
`prod.api` or `my service`) is logged and skipped at load time, since no
selector could ever pick it.

### Loading and validation

Loading is forgiving by design — a configuration problem never crashes a
consumer:

- A missing, unreadable, or malformed file yields no presets (logged).
- An individual invalid entry is logged with the offending preset name and
  skipped, so it cannot disable the others. Rejection reasons: not a JSON
  object, `checkout`/`service` not a string, `service` without `checkout`
  (mirrors the `--service` requires `--checkout` CLI rule), `env` not a
  string→string map, `args` not a list of strings, or a non-selectable name.
- Unknown keys in an entry produce a warning but keep the preset — a typo /
  forward-compatibility signal rather than an error.
- Selecting a name that did not load gets the consumer's normal
  unknown-preset rejection: a thread reply listing the available presets on
  Slack; exit 2 listing them on the CLI.

## Trust model

Presets are deliberately **not** a raw passthrough of `--service`/`--checkout`
values. The presets file lives on the host and is writable only by whoever
runs the spawner; a user only ever *selects* an operator-defined name, never
supplies a path or flag. Access to use presets is therefore the same as access
to reach the spawner — for the Slack listener, channel membership /
`LMER_SLACK_DM_ALLOWED_USERS`; for the CLI, shell access to the host — and the
feature adds no separate gate or allowlist.

## Slack-selected presets

The listener scans the message that *starts* a session (mention or DM) for a
`$preset:<name>` token, anywhere in the text:

```
@lmer-bot $preset:my_service can you check why the worker queue is backed up?
```

It then spawns, e.g., `lmer chat <permalink> --checkout /srv/my-service
--service mysvc --ports 2` with `LMER_LLM_NAME=opus` in its environment, and
the connecting ack names the applied preset.

- **Unknown name** → the listener rejects it with a thread reply listing the
  available presets and does not spawn.
- **Already-connected thread** → the token is moot (the live session handles
  the new message), so a `$preset:` token only takes effect on the message
  that starts a session.

Listener setup: [Spawning sessions automatically (`lmer-slack-listener`)](./LMER-CLI.md#spawning-sessions-automatically-lmer-slack-listener).

### The listener-wide default (`LMER_CHAT_PRESET`)

The token is not the only selector. Every session the listener spawns is an
`lmer chat` invocation that inherits the listener's environment, so
`LMER_CHAT_PRESET` — the [taskdef-scoped
selector](#per-taskdef-presets-lmer_task_preset) for the `chat` taskdef — in
the listener's own environment applies a preset to **every** session it
starts, with no token and nothing in the Slack message. `LMER_PRESET` does the
same when `LMER_CHAT_PRESET` is unset.

That is a supported way to give a listener deployment a house default (a
checkout to mount, a model to use) without asking every user to type a token:

```bash
# in the listener's environment / deployment .env
LMER_CHAT_PRESET=house_default
```

**A `$preset:` token displaces the default entirely.** It is a replacement,
not an overlay: when a token selects a preset, the listener blanks every
preset selector in the spawned session's environment, so the default is
never loaded. None of its values survive, and none of its keys are inherited
where the token's preset leaves them unset. (Before issue #181 the two stacked
under two different rules — the token's `env` won conflicts while the default
silently filled every gap — which left two presets in play and nothing saying
so.)

**The ack always names the preset in effect**, so which one applied is never
something you have to reconstruct from a log:

```
Connecting a session to this thread using the listener default preset
`house_default` (from `LMER_CHAT_PRESET`)... ⏳

Connecting a session to this thread using preset `chosen` (replacing the
listener default `house_default` from `LMER_CHAT_PRESET`)... ⏳
```

`lmer_session_spawned` records the same pair — `preset=` for the
token-selected one, plus `default_preset=` or `displaced_default=` for the
default it replaces, resolved the way the child itself resolves it
(environment first, then the same `.env` file tiers the spawned `lmer`
reads, including a forwarded `--env-file`).

One residual: a selector whose value uses `${VAR}` interpolation may display
differently from what the session resolves. The display expands a file's
`${VAR}` against the listener's live environment, while the child expands it
against an environment that the *earlier* `.env` tiers have already seeded, so
a reference to a key introduced by an upper tier resolves for the child and not
for the display. The failure direction is one-way: the display can lose a name
this way (falling back to a lower-priority selector, or to no preset), never
invent one. Avoid `${VAR}` in preset selectors if you want the ack to be exact.

Two more things worth knowing:

- **`LMER_CHAT_PRESET` outranks an exported `LMER_PRESET`.** This is the
  [specificity rule](#per-taskdef-presets-lmer_task_preset) doing its job, not
  a bug: taskdef-scoped selection beats the generic one regardless of where
  each value came from, exactly as `LMER_REVIEW_PRESET` beats `LMER_PRESET`
  for `lmer review`. If you want the generic variable to drive the listener,
  leave `LMER_CHAT_PRESET` unset.
- **An undefined default is an operator misconfiguration, and it is fatal per
  session.** Unlike an unknown *token* (rejected before spawning), an unknown
  default is only discovered by the spawned CLI, which exits 2. The listener
  therefore warns in the thread — naming the variable and listing the
  available presets — rather than leaving a session that dies seconds after
  connecting with the reason buried in the listener's log. "Defined" and
  "available" are judged against `LMER_PRESETS_FILE` as the *spawned CLI*
  resolves it, through the same `.env` tiers as the selector: a presets file
  that lives only in a forwarded `--env-file` counts, and the names listed are
  the ones that session could actually have used.

## CLI-selected presets

A direct `lmer` invocation applies a preset with `--preset <name>`, or via the
`LMER_PRESET` environment variable (the flag wins, matching
`--harness`/`LMER_HARNESS`). `LMER_PRESET` is also honored from `.env` files
(cwd, `~/.lmer/.env`, `--env-file`), so a project directory can pin a default
preset:

```bash
echo "LMER_PRESET=my_service" >> .env
```

A preset can also be pinned for **one taskdef** with `LMER_<TASK>_PRESET` —
see [Per-taskdef presets](#per-taskdef-presets-lmer_task_preset) below.

On the CLI the preset supplies **defaults; the explicit invocation always
wins** (see the merge table below). The combined argument set is re-validated
normally, and `--show-env` attributes preset-applied variables to
`preset (<name>)`.

Guard rails (these apply to every CLI selector — `--preset`,
`LMER_<TASK>_PRESET` and `LMER_PRESET` alike):

- An unknown name fails fast (exit 2) listing the available presets and
  naming the selector that chose it (`--preset`, `LMER_<TASK>_PRESET`, or
  `LMER_PRESET`) — a typo'd taskdef-scoped variable never silently falls back
  to `LMER_PRESET`, and a `--verbose` run names the selector in the
  `🎛️  Preset:` line as well.
- A **blank** selector counts as unset and falls through to the next one:
  `LMER_REVIEW_PRESET= lmer review …` drops back to `LMER_PRESET`, and
  `LMER_PRESET= lmer develop …` runs with no preset. Values are stripped, so
  `--preset " demo "` and `LMER_PRESET=" demo "` behave identically.
- Preset `args` must be known lmer flags — a bare positional, a literal `--`,
  or an unrecognized token fails fast (exit 2) rather than silently rebinding
  your command line.
- A preset-supplied `--env-file` never loads: it is ignored (with a warning
  when it isn't already overridden by your own `--env-file`) — pass it on the
  command line instead.

CLI quick reference: [Startup presets in docs/LMER-CLI.md](./LMER-CLI.md#startup-presets---preset--lmer_preset).

### Per-taskdef presets (`LMER_<TASK>_PRESET`)

A preset can be pinned for **one taskdef** instead of all of them (issue
#140). The variable name derives from the taskdef id — uppercased, with every
non-alphanumeric character folded to an underscore:

| Invocation | Taskdef-scoped variable |
| --- | --- |
| `lmer review <mr-url>` | `LMER_REVIEW_PRESET` |
| `lmer develop <issue-url>` | `LMER_DEVELOP_PRESET` |
| `lmer code-review <mr-url>` | `LMER_CODE_REVIEW_PRESET` |

The derivation is mechanical, so work-repo taskdefs and ones from
`LMER_TASKDEF_PATHS` get a scoped variable too — nothing has to be registered.
Two consequences of that, both invisible when they bite:

- It is **many-to-one**. `code-review`, `code_review` and `code.review` all
  fold to `LMER_CODE_REVIEW_PRESET`, so two separator-variant taskdefs on the
  search path would share one selector. No current taskdef set does.
- An id with no ASCII alphanumerics (e.g. a non-Latin name) folds to nothing
  and therefore has **no** scoped selector; use `LMER_PRESET` or `--preset`
  for it.

Selection order, most specific first:

1. `--preset <name>` — the flag always wins.
2. `LMER_<TASK>_PRESET` — applies to that taskdef only.
3. `LMER_PRESET` — the default for every other taskdef.

So a global default and per-task overrides can coexist:

```bash
# ~/.lmer/.env
LMER_PRESET=default_config        # every task
LMER_REVIEW_PRESET=sol_review     # except review, which uses this
```

`lmer develop <url>` gets `default_config`, `lmer review <url>` gets
`sol_review`, and `lmer review <url> --preset other` gets `other`. A
`--no-task` invocation has no taskdef id, so only `--preset`/`LMER_PRESET`
apply to it.

**The order is by specificity, not by source tier.** A scoped variable beats
`LMER_PRESET` no matter where each value came from — including a
`LMER_REVIEW_PRESET` in `~/.lmer/.env` beating a `LMER_PRESET` you exported on
the command line. That is deliberate (a per-task default that an unrelated
export could silently disable would be useless), and it is the one place where
lmer resolves file-versus-export in the file's favor, so that specific
combination prints a warning naming both sides:

```
⚠️  LMER_REVIEW_PRESET (from .env (lmer state dir)) overrides the exported
    LMER_PRESET=safe for this task — taskdef-scoped selection wins regardless
    of where each value came from. Use --preset to choose per invocation.
```

`--preset` is the per-invocation override: it wins over both variables from
any source. The warning is deliberately narrow — it stays quiet when both
selectors come from the same tier (the global-default-plus-override `.env`
above would otherwise warn on every run) and when the scoped variable is
itself exported.

## Fan-out agents (`--agents` / `LMER_AGENTS`)

`lmer <task> <target> --agents=sol-review,opus-review` (or `LMER_AGENTS=…`;
the flag wins) names the presets the session's agent may fan a task out to
with the in-container `spawn-harness` tool — e.g. a review taskdef running
the same review through several harness/model configurations and
consolidating the results (issue #130).

For this consumer a preset is an **agent configuration, not a session
launch**: children run as non-interactive harness subprocesses inside the
orchestrating session's container. What a selected preset contributes:

- **`env`** — the child's overlay (typically `LMER_HARNESS`,
  `LMER_LLM_NAME`, `LMER_REASONING_EFFORT`).
- **`args` `--harness <name>`** — folded into the overlay as
  `LMER_HARNESS` (winning over a preset-env value, mirroring the CLI
  consumer's flag-beats-env rule), so a dual-use preset configures its
  harness once and works with both `--preset` and `--agents`.
- **`args` `--prompt <text>`** — carried as the agent's prompt *preamble*:
  `spawn-harness` prepends it to the prompt the orchestrator supplies, or
  uses it alone when none is given — a canned persona (e.g. "second review
  pass from scratch") can live in the preset.
- Everything else (`checkout`, `service`, remaining args) is ignored with
  a warning.

A name that matches no preset falls back to the **model route**: when the
name is a model whose family implies a harness (the same
`MODEL_HARNESS_HINTS` matching as `LMER_LLM_NAME` autoselection — `fable`
→ claude, `gpt-5.6-sol` → codex), it resolves to a synthesized model-only
agent, so `--agents=fable,sol-review` works without defining a `fable`
preset. The fallback is announced at launch; note a typo'd preset name
containing a model word resolves this way and only fails when the harness
rejects the model. Preset names are case-sensitive — a case-variant of a
defined preset (`--agents=Fable` with a `fable` preset) is rejected with a
did-you-mean rather than silently taking the model route.

The trust model is preserved by resolving at launch: names are validated
against `LMER_PRESETS_FILE` on the host — a name that is neither a preset
nor a routable model fails fast (exit 2) listing the available presets, a
duplicate warns and keeps the first occurrence — and only the resolved
config crosses into the container, as `LMER_SPAWN_AGENTS` (names) plus
`LMER_SPAWN_AGENTS_CONFIG` (JSON `{name: {"env": {...}, "prompt": "..."?}}`).
The container-side names are scoped away from the `LMER_AGENTS` input on
purpose (issue #283): container env is ambient, so under the input name a
nested `lmer` invocation inside the session inherited the outer selection
and tried to resolve it against a presets file that never crossed the
boundary. The presets file never enters the container, so the agent can
only spawn what was named at launch. Inside the session:

```bash
spawn-harness --list
spawn-harness sol-review --prompt-file prompt.md \
    --env LMER_REVIEW_ON_MR=0 --output agents/sol-review.md
```

Credentials follow the children (issue #131): because each agent's implied
harness is known host-side before the container starts, the launcher mounts
the credential files of **every implied child harness** alongside the
session harness's (e.g. `~/.codex/auth.json` for a codex-routed child of a
claude session — same skip-missing-files rule as the session mounts). An
implied child harness with no mountable credential file on the host warns
at launch naming the agent, the harness, and the missing path; it never
errors, since a keys-via-env harness (pi) can authenticate without the
mount — and conversely a mounted file is no promise of working auth. The
launch-time computation only sees launch-configured routing: a child
rerouted at spawn time (`spawn-harness … --env LMER_HARNESS=…`) may select
a harness whose credentials were never mounted — `spawn-harness` prints
the same may-fail-to-authenticate warning in-container when that happens.

One `spawn-harness` invocation runs one child and blocks until it exits
(mirroring the child's exit code); the orchestrating agent parallelizes
with its own background-shell tooling. While a child runs, heartbeat lines
on stderr distinguish a healthy long run from a hung one, and a failed
child's `--output` file carries a failure footer with the stderr tail
instead of being silently empty. A child that exits 0 having produced
nothing at all — empty, whitespace-only or stub-length — is warned about on
stderr and marked with a distinct `[spawn-harness] child produced NO USABLE
OUTPUT` footer, so a silently dropped agent is visible at fan-out time
instead of quietly shrinking the consolidation to N-1. Whether prose is a
*complete* answer is not judged (the exit code stays the child's own —
see [Non-interactive exec mode in docs/HARNESSES.md](./HARNESSES.md#non-interactive-exec-mode-spawn-harness)).
Children run permission-free (the
lmer container is the security boundary), stateless (no run dirs, no
work-repo writes), and cannot fan out further — both fan-out pairs
(`LMER_SPAWN_AGENTS` / `LMER_SPAWN_AGENTS_CONFIG` and the host-input
`LMER_AGENTS` / `LMER_AGENTS_CONFIG`) are stripped from the child
environment, so there are no grandchildren. Every child also gets `LMER_NONINTERACTIVE=1` and a
one-paragraph notice at the head of its prompt — "report a gate-worthy
problem, never end the turn asking for approval" — because an unanswerable
question in a child is a dropped result, not a pause. The notice is in-band
rather than a context file so it reaches every harness alike. Per-harness
invocation details:
[Non-interactive exec mode in docs/HARNESSES.md](./HARNESSES.md#non-interactive-exec-mode-spawn-harness).

## Merge semantics per consumer

How a preset combines with the rest of the invocation is each consumer's
contract:

| | Slack listener | Direct CLI |
|---|---|---|
| **`args`** (and `checkout`/`service` flags) | Appended verbatim to the fixed `lmer chat <permalink>` command, where extra tokens are meaningful | Applied as if typed *before* your own flags, so an explicit flag overrides the preset's value (repeatable flags like `--mount-file` accumulate from both); flags-only, validated up front |
| **`env`** | Merged over the listener's inherited environment — **the preset wins** on conflict | Defaults only — an **exported environment variable wins** over the preset, while the preset wins over `.env`-file values; applies both host-side and in the container environment, where applied preset entries are forwarded even when the variable has no hardcoded passthrough |
| **Unknown name** | Thread reply listing the available presets; no session spawns (a token) / a warning in the ack, and the spawned CLI exits 2 (the env-selected default) | Exit 2 listing the available presets |
| **Two presets selected at once** | Cannot happen: a `$preset:` token [displaces](#the-listener-wide-default-lmer_chat_preset) the env-selected default whole, so exactly one preset is ever in play | Cannot happen: the selectors are a precedence chain (`--preset` > `LMER_<TASK>_PRESET` > `LMER_PRESET`), and the most specific one wins outright |

The env asymmetry is deliberate: a Slack user has no way to express
per-invocation intent beyond the message text, so the operator's preset is
authoritative; a CLI user can type flags and export variables, so their
explicit invocation always wins.

The fan-out consumer ([`--agents`](#fan-out-agents---agents--lmer_agents))
contributes its `env` overlay plus `--harness`/`--prompt` folded from
`args` (other launch-shaping fields are ignored with a warning — see the
fan-out section above), and has its own child-side merge: the child
inherits the orchestrating session's environment, the preset's `env`
overlays it, and explicit `spawn-harness --env KEY=VAL` pairs win over
both.

## Adding a preset field

For contributors wiring a new field into the preset system:

1. Add the field and its validation in `src/lmer_cli/presets.py`
   (`Preset`, `_build_preset`).
2. If it maps to a CLI flag, wire it in `Preset.cli_tokens()` — the single
   home of the field→flag mapping, shared by every spawner.
3. Consumer-specific semantics (env merge, argument guards) live in each
   consumer: `src/slack_chat/sessions.py` (Slack spawn) and
   `src/lmer_cli/cli.py` (`_resolve_and_apply_preset`).
4. Document the field here and update the `LMER_PRESETS_FILE` entry in
   [docs/LMER-CLI.md](./LMER-CLI.md#environment-variables).

## Troubleshooting

- `lmer --list-presets` shows what actually loaded — name plus a summary of
  each preset's fields, with env shown as **key names only** (preset env may
  carry credentials).
- The loader logs every problem with the offending path/name:
  `presets_file_not_found`, `presets_file_unreadable`,
  `presets_file_not_object`, `preset_invalid name=… reason=…`, and
  `preset_unknown_keys` for kept-but-suspicious entries.
- `lmer --show-env` attributes preset-applied variables to `preset (<name>)`,
  so you can see exactly which values a CLI preset contributed.
