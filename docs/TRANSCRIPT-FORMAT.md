# The lmer transcript format (version 1)

A small, versioned JSONL dialect that any harness can be converted **into**, so
that its sessions render as a conversation in the orchestrator's chat view
(`GET /api/sessions/{id}/messages`). It is a public contract: this document is
everything a drop-in author needs, and nothing here requires reading platform
source.

The format serialises exactly the normalised message shape the platform already
produces — role, kind, text, timestamp, tool activity — which is what the chat
view consumes and what the built-in claude/codex/pi adapters normalise *to*.

## When you need it

[HARNESSES.md § Transcript visibility](./HARNESSES.md#transcript-visibility-orchestrator-chat-view)
describes the two tiers:

- **Adapter tier** — claude, codex and pi. Their native session JSONL is read
  in-tree, and that set is closed (the governance rule in HARNESSES.md). A
  drop-in that *wraps* one of these CLIs must **not** ship a converter: its
  native files already render, and a second, converted copy of the same
  conversation would render twice.
- **Canonical tier** — everything else. A drop-in declares `session_dir` in its
  manifest and ships an **in-container converter**: its own code, running where
  the harness already runs, tailing whatever the harness writes and appending
  records in the format below to a `.jsonl` file inside that same directory.

Declaring `session_dir` without a converter is still worth doing — the native
files mount out onto the host and are scrubbed and inspectable — but the chat
view will answer with the explicit "this build cannot read it" note, because no
record in those files belongs to a vocabulary the reader knows. Adding the
converter is what turns those files into a readable conversation. The terminal
log remains the complete record either way.

The daemon never executes drop-in code: it only ever *reads* these files. That
is the whole reason the conversion happens in the container.

## Where the file goes

Anywhere below the harness's declared `session_dir`, as long as it ends in
`.jsonl`. Discovery is recursive, so a subdirectory is fine, and nothing else
needs to be declared or mounted — the platform already mounts that directory
out, scrubs it at session end, and (for user harnesses) stages it behind the
entrypoint's symlink.

Two conventions:

- **A dedicated output directory** (the opencode example's posture): declare a
  directory the harness itself never writes to — `session_dir` means "where
  readable transcripts appear", not "where the harness happens to write" — and
  put one file per harness session in it, named after the harness's own session
  id: `<session_dir>/<session-id>.jsonl`.
- **A reserved `_lmer/` subdirectory** when `session_dir` *is* the harness's own
  storage: `<session_dir>/_lmer/<session-id>.jsonl`. This keeps converter output
  out of the paths the harness scans for its own sessions. A harness that
  tolerates no foreign files at all wants the dedicated directory instead.

One canonical file per harness session is strongly recommended. A session's
files are read in sorted path order and *concatenated* — never merged by
timestamp — so a second file's turns all land after the first file's, whenever
they were written.

The file's stem is what the API reports as the transcript's `id` in the
`sessions` list of a messages page.

## Record types

One JSON object per line, UTF-8, append-only, LF-terminated. Three record types,
all `lmer.`-prefixed so they can never collide with a harness's own vocabulary
(existing dialects claim bare words like `message`, `session`, `user`).

Anything else on the line — an unknown field, an unknown `lmer.*` type, a record
of the harness's own — is ignored, per record, never fatally.

### `lmer.meta` — file header

```json
{"type": "lmer.meta", "format": 1, "harness": "opencode",
 "generator": "opencode-converter/0.1", "native_format": "opencode-export/1"}
```

| field | required | meaning |
|---|---|---|
| `format` | yes | Canonical-format version, an integer. `1` today, and the only value a reader will read records for: a file declaring a **higher** number has its `lmer.*` records skipped from that line on, because a later version may change what a field already here *means*. Absent, or anything that is not an integer (a string, a float, `true`, `null`), reads as `1`. See [Versioning](#versioning). |
| `harness` | yes | The drop-in's harness name, same grammar as a harness directory name: `^[a-z][a-z0-9_-]{0,63}$`, matched against the *whole* string (a trailing newline is not tolerated). This is what labels the transcript's source in the API and UI — the file's own answer to "which harness wrote this". |
| `generator` | no | Free-form provenance for debugging (converter name and version). Not rendered. |
| `native_format` | no | Free-form name of the format converted *from*. Not rendered. |

Recommended as the **first line of every file**. It is not a message and emits
nothing. A canonical file with no `lmer.meta` — or one whose `harness` fails the
grammar above — still reads; its source is just labelled `lmer` rather than by
harness name. The first usable declaration in the file wins.

### `lmer.message` — one conversation turn

```json
{"type": "lmer.message", "role": "assistant", "kind": "said",
 "text": "Rebased onto main and pushed.", "at": "2026-08-14T19:39:06.752Z",
 "tools": [{"id": "call_1", "name": "Bash", "detail": "git status",
            "status": "pending"}]}
```

| field | required | values and semantics |
|---|---|---|
| `role` | yes | `user` \| `assistant` \| `system` \| `monitor` — the roles the view can title. Any other value: the record is skipped. |
| `kind` | no, default `said` | `said` \| `injected` \| `notice`. `injected` is machinery talking to the model (a hook's output, an expanded slash command, injected context); `notice` is the harness talking to the operator. Both sit behind the view's internal toggle. JSON `null` reads as the default, `said` (this field and `tools[].status` are the two where it does). Any other value: the record is skipped. |
| `text` | yes, may be `""` when `tools` is non-empty | Plain text, and a string — anything else skips the record. It passes through the host's credential scrub and its 8000-character cap like every other string. A record with neither text nor tools renders as nothing and is dropped — **including a refusal**, so a turn carrying only `api_refusal` and no prose is lost with its `api_*` fields; a native refusal event usually has no text of its own, so synthesise one (`"Provider refusal: billing_error"`) rather than emitting an empty turn. |
| `at` | no | ISO-8601 UTC timestamp string (`2026-08-14T19:39:06.752Z`). Shown as the turn's age in the chat header. A value that will not parse as a timestamp is **dropped** — the turn reads as having no time, exactly as if the field were absent — so this field carries a timestamp or nothing. It never decides order — see the constraints below. |
| `tools` | no | List of the tool calls this turn made: `{id?, name, detail?, status?, error?}`. `name` is required (and bounded at 160 characters); the rest are optional. `detail` is a one-line hint at what the tool acted on (a command, a path); `status` is `ok` \| `failed` \| `pending`, defaulting to `pending` (JSON `null` reads as that default too); `error` is one line of failure text. Give a call an `id` only if a later `lmer.tool_update` will resolve it. An entry with no usable `name` or an unrecognised `status` is dropped on its own — the turn around it survives. |
| `via` | no | Only `monitor` is honored: a watch or monitor event delivered into the session, which is nobody in the conversation speaking. `ask` is reserved for the platform's own ask-channel merge and cannot be claimed by a file; any other value is ignored. |
| `api_refusal` | no, default `false` | Literally `true` when this turn is the harness recording that the *provider* refused. Anything else, truthy or not, reads as no refusal. Write a refusal as `role: "assistant"`, `kind: "said"` and non-empty text: that is what the claude adapter produces for its own refusal records, so it is what the operator already expects to see, and `notice` would hide the turn behind the view's internal toggle. |
| `api_error` | no | The provider's error class as a short string (`billing_error`, `server_error`), kept to 64 characters. |
| `api_error_status` | no | The HTTP status as an integer (a boolean is not one). |

The three `api_*` fields are what buy a drop-in harness the platform's precise
stall detection instead of the silence backstop; see the mapping table below.

**The converter decides `kind`, and nothing downstream second-guesses it.** The
built-in adapters carry per-harness heuristics for telling an injected turn from
a typed one, because those dialects give both the same role. Canonical records
get none of that: what you write is what the view draws. So a turn the harness
injected into the model's context must be marked `injected` by the converter,
from the native record's own provenance — otherwise it renders as something the
operator typed, which is the one mistake this view keeps having to close.

### `lmer.tool_update` — resolve a pending tool call

```json
{"type": "lmer.tool_update", "id": "call_1", "status": "ok"}
{"type": "lmer.tool_update", "id": "call_2", "status": "failed", "error": "exit 1: no such file"}
```

Folds the outcome onto the tool call previously emitted with that `id` **in the
same file**: `status` — `ok` or `failed`, and nothing else is accepted — plus,
optionally, one line of `error` text. It emits no message of its own. An `id`
this file never emitted a call for silently no-ops.

This is what keeps the file strictly append-only — a message is never rewritten
when its tool finishes — while still giving the live display its "· running" →
outcome transition.

A converter that only ever converts *completed* turns can skip updates entirely
and emit the final `status` inline on the call. That is the simpler design; take
it unless you are tailing a live session.

## Constraints the reader enforces

These are normative because they are the reader's actual behavior, not style
advice.

- **Append-only. Never rewritten, never reordered.** A message's sequence number
  is its position in file order, and clients page and poll with those numbers.
  Rewriting or reordering a line renumbers history under every cursor in flight,
  which makes a live reader skip or repeat turns. File order is conversation
  order; timestamps never re-sort it.
- **Every line stands alone.** Stall detection reads the last 256 KiB of the
  newest file from a mid-file seek and drops the first (probably half) line in
  that window, so each record must be independently parseable — no record that
  depends on having read the file from the top. A consequence to accept rather
  than work around: an `lmer.tool_update` whose call fell outside that window
  silently no-ops there.
- **Sizing.** Keep every line under 1 MiB: a longer one is read in fragments and
  dropped, never parsed (a torn write or a binary file can present as one
  enormous line, and parsing it would cost more than the whole transcript). At
  most 5000 messages are read from one file — past that the page reports itself
  capped. At most 64 transcript files are read for one run.
- **Discovery.** Only `**/*.jsonl` below the declared `session_dir`, found
  recursively. A `.json` or `.log` file mounts out to the host but is never read
  back. A symlink that resolves outside the directory it was found in is refused
  rather than followed: that directory is container-writable, so a link there is
  something the observed session could have planted.
- **Scrubbing, and the file's lifetime.** Credential shapes are masked on every
  read on the way to the browser, and the file itself is rewritten in place with
  the same masking when the session ends, then left mode 0600 in a 0700
  directory. The rewrite replaces the file atomically, so **do not hold the file
  open expecting to append past session end** — writes after that land in the
  replaced inode and are lost. (The masking is a reduction, not a guarantee: it
  catches credential *shapes*. Do not treat a scrubbed transcript as safe to
  publish.)
- **Ordering across files** within one session is by sorted path. One canonical
  file per harness session avoids every surprise here.

## What the chat view does with each field

| canonical | what the operator sees |
|---|---|
| `role: "user"` | Titled **you**, on the operator's ground, rendered **verbatim** (not through the markdown renderer). A `said` user turn is also what settles a message the operator sent from the composer out of its "sending" state. |
| `role: "assistant"` | Titled with the agent's name (`lmer` in a run's chat), rendered as markdown. |
| `role: "system"` | Titled **the session**, on the action ground. |
| `role: "monitor"` | Titled **the watch fired**, drawn as an event line rather than as prose — for a turn nobody in the conversation said. Pair it with `via: "monitor"`. |
| `kind: "said"` | Shown by default. |
| `kind: "injected"` / `"notice"` | Captioned **· internal**, drawn on the action ground, and hidden until the operator flips the view's internal toggle. |
| `via: "monitor"` | Records that a watch delivered the turn rather than a party saying it; the `monitor` role is what titles it. It also crosses to API clients, which is the point — a turn assembled by machinery is never passed off as something somebody said. |
| `text` | Capped at 8000 characters, and what survives is the **end** of the message (an agent's turn ends with its conclusion). A trimmed turn is captioned "… earlier part of this message trimmed — the terminal has all of it". |
| `at` | The relative age beside the speaker ("4m ago"), with the raw timestamp on hover — a turn with no `at` reads "never", so emit one. Also the instant used to interleave the session's ask-channel questions and answers; a timestamp older than the turn above it is clamped rather than allowed to reorder anything. |
| `tools[].name` | The bold half of a one-line tool chip. |
| `tools[].detail` | Appended as ` · <detail>`. Reduced to its first non-empty line and capped at 160 characters, keeping the **head** (a command or a path is identified by its beginning). |
| `tools[].status: "pending"` | The chip reads ` · running`. |
| `tools[].status: "failed"` | The chip turns red and takes an alert icon. |
| `tools[].error` | Appended as ` — <error>`, same one-line 160-character treatment as `detail`. |
| `api_refusal: true` on an `assistant` turn | When the run has gone quiet, the fleet view names the stall `api_error` — the provider refusing, on the harness's own report rather than on a reading of the prose — instead of falling back to the silence backstop. `api_error` and `api_error_status` become the cause detail. |
| newest turn is `role: "user"` or `"monitor"` | A quiet run reads as `unanswered`: something was said to the session and nothing answered it. |

Nothing else crosses. Token accounting, request ids, model reasoning, whole tool
payloads: the chat view is the readable summary and the terminal log is the
faithful one, so leave them out of the canonical file rather than hoping they
are dropped.

## Writing a converter

The converter is entirely a `runner.sh` concern. There is no manifest key for
it, and nothing host-side knows it exists — the reader recognises canonical
records by their `type`, per record.

**Start it backgrounded, before `harness_exec`:**

```bash
python3 "$HARNESS_DIR/converter.py" >>"$HOME/.lmer-session/converter.log" 2>&1 &
harness_exec mycli "$@"
```

`harness_exec` execs into the supervisor, so the converter is orphaned to the
container's init (`--init` is there for exactly this) and dies with the
container.

`~/.lmer-session` is the per-session directory the orchestrator bind-mounts for
a spawned session — the right home for a converter log, since it lands on the
host beside the session's own. It does **not** exist in a plain `lmer` run
(nothing mounts it there), and a redirect into a missing directory would keep
the converter from starting at all, so guard it if your harness is used both
ways:

```bash
LOG_DIR="$HOME/.lmer-session"; [ -d "$LOG_DIR" ] || LOG_DIR="$HOME"
python3 "$HARNESS_DIR/converter.py" >>"$LOG_DIR/converter.log" 2>&1 &
```

**Append and flush record by record.** Nothing signals the converter at session
end and nothing waits for it: there is no shutdown hook and no flush window.
Whatever is not on disk when the container exits is gone. Tailing and flushing
per record bounds that loss to the record in flight at teardown, which the
terminal log covers.

**Write through the *declared* `session_dir` path.** For a user harness that
path is a symlink the container entrypoint creates into the staged mount;
resolving it is transparent. Tolerate its absence — the linker is deliberately
fail-soft — by failing quietly. Same for every other error you can hit: a lost
converter must never cost the session. Log to a file and carry on.

**Be defensive about the native format.** Skip records you do not recognise,
tolerate a torn last line, re-stat for rotation, retry on the next poll rather
than exiting. The harness's format is not your contract; a converter crash costs
the chat view and nothing else.

**Alternative, for a guaranteed final pass.** Instead of `harness_exec`'s
`exec`, run the supervisor as a *child* and convert once more after it returns —
still inside the window the container waits on:

```bash
lmer-supervisor -- mycli "$@"
python3 "$HARNESS_DIR/converter.py" --once
```

The caveat is real: the runner's shell then sits in the signal path between the
container and the harness, and a shell that mishandles a signal costs the
session's shutdown. Not calling `harness_exec` also means not getting what it
does around the supervisor (see `libexec/harness-common.sh`). The backgrounded
tailer stays the default.

## Worked example

[`examples/harnesses/opencode/`](../examples/harnesses/opencode/) is a complete,
copy-installable drop-in — `harness.json`, `runner.sh`, `converter.py` —
exercised in CI against captured fixtures. It demonstrates the decoupled case:
opencode persists sessions in a SQLite database, so nothing native ever lands in
its `session_dir` and the declared directory is purely the converter's output
home.

[USER-HARNESS-OPENCODE.md](./USER-HARNESS-OPENCODE.md) walks the same drop-in
end to end, from the manifest to the host-side login.

## Versioning

Version 1 readers ignore unknown fields and unknown `lmer.*` record types, so
the format evolves **additively**: new optional fields and new record types can
appear without breaking an older host, which simply skips what it does not know.
That tolerance is *within* a version — a file growing a new record type still
declares `format: 1`.

A change that could not be ignored safely — one that redefined a field already
in this document — would take a new `format` number, and the reader refuses one:
a file whose `lmer.meta` declares a `format` higher than 1 has its `lmer.*`
records skipped from that record on, and its session answers the "on disk but
nothing to show" note instead of a conversation. The file is still labelled by
that meta's `harness`, so the empty page names the drop-in that wrote it. A
`format` that is absent or is not an integer reads as 1.

That refusal is the point of writing the number: converters update on their
author's schedule, inside the container, while the reader is whatever the host
happens to be running, so a format 2 file *will* meet a format 1 host. Skipping
its records is the safe half of that meeting; reading them as version 1 would
render a redefined field as though it still meant what it means here.

One limit worth knowing: stall detection reads the tail of the newest file
(see [the constraints](#constraints-the-reader-enforces)), and a `format` line
at the top of the file is outside that window — so the gate is best-effort
there, as tool correlation already is.

Until a version 2 exists, treat everything in this document as the one and only
version: emit `format: 1` and rely on the additive path.
