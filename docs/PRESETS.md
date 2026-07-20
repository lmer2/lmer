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
| `lmer-slack-listener` (spawns a session per mention/DM) | `$preset:<name>` token in the triggering Slack message | [Slack-selected presets](#slack-selected-presets) |
| The `lmer` CLI (direct invocations) | `--preset <name>` flag or `LMER_PRESET` env var | [CLI-selected presets](#cli-selected-presets) |

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

## CLI-selected presets

A direct `lmer` invocation applies a preset with `--preset <name>`, or via the
`LMER_PRESET` environment variable (the flag wins, matching
`--harness`/`LMER_HARNESS`). `LMER_PRESET` is also honored from `.env` files
(cwd, `~/.lmer/.env`, `--env-file`), so a project directory can pin a default
preset:

```bash
echo "LMER_PRESET=my_service" >> .env
```

On the CLI the preset supplies **defaults; the explicit invocation always
wins** (see the merge table below). The combined argument set is re-validated
normally, and `--show-env` attributes preset-applied variables to
`preset (<name>)`.

Guard rails:

- An unknown name fails fast (exit 2) listing the available presets.
- Preset `args` must be known lmer flags — a bare positional, a literal `--`,
  or an unrecognized token fails fast (exit 2) rather than silently rebinding
  your command line.
- A preset-supplied `--env-file` never loads: it is ignored (with a warning
  when it isn't already overridden by your own `--env-file`) — pass it on the
  command line instead.

CLI quick reference: [Startup presets in docs/LMER-CLI.md](./LMER-CLI.md#startup-presets---preset--lmer_preset).

## Merge semantics per consumer

How a preset combines with the rest of the invocation is each consumer's
contract:

| | Slack listener | Direct CLI |
|---|---|---|
| **`args`** (and `checkout`/`service` flags) | Appended verbatim to the fixed `lmer chat <permalink>` command, where extra tokens are meaningful | Applied as if typed *before* your own flags, so an explicit flag overrides the preset's value (repeatable flags like `--mount-file` accumulate from both); flags-only, validated up front |
| **`env`** | Merged over the listener's inherited environment — **the preset wins** on conflict | Defaults only — an **exported environment variable wins** over the preset, while the preset wins over `.env`-file values; applies both host-side and in the container environment, where applied preset entries are forwarded even when the variable has no hardcoded passthrough |
| **Unknown name** | Thread reply listing the available presets; no session spawns | Exit 2 listing the available presets |

The env asymmetry is deliberate: a Slack user has no way to express
per-invocation intent beyond the message text, so the operator's preset is
authoritative; a CLI user can type flags and export variables, so their
explicit invocation always wins.

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
