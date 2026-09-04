# The session status document (schema 1)

A small JSON file a session publishes about itself so the platform can show
what the harness's own status line shows: how much of the context window is
gone, how much of the account's usage windows are spent, and what reasoning
effort the session is running at (issue #248).

It is a contract between two independently-installed halves — a harness writing
inside a container, the daemon reading on the host — so it is versioned here and
owned by the platform rather than by any harness.

## Why a document and not a query

Nothing on the host can ask a running harness what its status line says. The
numbers exist only in the process that renders that line, inside a container
the daemon cannot reach into. So the session writes and the platform reads,
across a directory the platform bind-mounts per session — the same transport
the ask channel, the chat upload store and the session log already use.

Reading the screen is not an alternative. The terminal is a redrawing TUI, and
deriving numbers from it would mean parsing a screen whose layout is nobody's
contract.

## Where it goes

`$LMER_STATUS_DIR/status.json`.

`LMER_STATUS_DIR` names a container-side directory the platform created and
mounted read-write before the container started. **Do not create it.** An
absent or empty variable, or a directory that is not there, means this session
has no reader — a session started straight from the `lmer` CLI, or one whose
platform predates this feature — and the correct behaviour is to publish
nothing at all.

Write it atomically (a temporary file beside it, then `os.replace` or `mv`):
the reader is another process and there is no lock, so a half-written file is
read as *no status*. Mode `0600`. Rewrite it as often as the numbers change —
claude publishes on every status render.

## The document

```json
{
  "schema": 1,
  "harness": "claude",
  "updated": "2026-09-03T14:31:07Z",
  "context": {"percent": 9},
  "limits": [
    {"key": "five_hour", "label": "5h", "percent": 24, "resets_at": 1738425600},
    {"key": "seven_day", "label": "7d", "percent": 61}
  ],
  "effort": {"level": "high"}
}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `schema` | yes | `1`. A document declaring a later version is read as nothing rather than guessed at, because the two sides ship separately. |
| `harness` | yes | The harness this is about, lowercase (`claude`, or a drop-in's own name). |
| `updated` | yes | When these numbers were read, UTC ISO-8601. `YYYY-MM-DDTHH:MM:SSZ` is the canonical spelling; a general ISO-8601 stamp (what `datetime.now(timezone.utc).isoformat()` gives you) is accepted and normalised to it, and a stamp with no zone is read as UTC. **A document whose `updated` cannot be parsed is not served at all** — the platform ages the reading against this field and captions an old one, and a reading it cannot date would render exactly like one taken three seconds ago. |
| `context.percent` | no | Percentage of the context window used, `0`–`100`. |
| `limits` | no | Usage windows, in the order they should be read. At most 8 are served. |
| `limits[].key` | yes, per entry | Stable identifier for the window, e.g. `five_hour`. Duplicates are dropped, first occurrence winning. |
| `limits[].label` | no | What to call it on screen, e.g. `5h`. The **harness** supplies this: only it knows what its windows are called, and a platform that named them itself would show nothing for the next harness that reports usage. Falls back to `key`. |
| `limits[].percent` | yes, per entry | Percentage of that window consumed. Entries without one are dropped. |
| `limits[].resets_at` | no | When the window resets, Unix epoch **seconds**. The platform converts it to ISO-8601 on the wire. |
| `effort.level` | no | Reasoning effort, e.g. `high`. Free-form: the vocabulary is the harness's. |

`schema`, `harness` and `updated` are required; every block below them is
optional, and absence means *not reported* — never zero. A harness that can report none of them should publish no file:
no file is no strip, which is the correct rendering of "this harness does not
say".

## What the platform does with it

`GET /api/state` carries it per run (`run.statusline`) for the run's live
session, and `GET /api/assistant` carries it for uber lmer's own session. The
UI renders a strip of meters in the run view, one dimmed line of text in the run
list row, and the same strip in the chat pane.

Reads are best-effort in both directions. A document that is absent,
**not a regular file**, oversized (>16 KiB), unparseable, wrongly-shaped or
undateable is *no status*, silently: a strip is worth less than the view it sits
in. Values are clamped and bounded on the way out, because the file is written
inside a container and lands in an HTTP response.

Write a **regular file**, and nothing else. The platform opens the path
`O_NOFOLLOW | O_NONBLOCK` and refuses anything that is not a regular file: a
symlink or a FIFO at that name would otherwise let one session's directory
redirect or stall the read that serves the whole fleet.

Only a **live** session's document is served. The document describes a session
that is rendering; once nothing is rendering, the last reading is a number with
no subject.

## How the UI renders it

One component draws all three surfaces (`web/src/components/StatusStrip.vue`)
in two densities. The fleet row gets one dimmed run of text in its own facts
line: a row is *scanned*, and a chip group or a bar per number would out-shout
the attention signal the whole list exists for. The run drawer and the chat
pane get meters, because those are read rather than scanned.

The meters are monochrome on purpose. Colour in this UI is urgency (the ramp in
`web/src/main.js`) plus one accent for "go and see this", and no threshold here
has been decided to be urgent — the scope of issue #248 is showing the numbers,
so a red bar at 90% would be the component inventing that policy.

A reading never presents itself as "now". A session publishes only while it is
rendering, so the strip captions its own age once the reading is more than a
minute old and says nothing while it is fresh. That caption is computed from
the daemon's `age_seconds`, not the browser's clock: gating on the server's
number and labelling with the viewer's produced a caption that could contradict
its own gate ("as of 0s ago" on a slow clock). A reading whose age is missing
says `age unknown` rather than nothing, since no caption is exactly what a
three-second-old reading looks like. A window's `resets_at` is a tooltip rather
than a fourth segment — it answers a question asked *after* the percentage, and
inline it doubles the width of the segment an operator scans for a number.

Absence renders nothing at all: no strip, no empty frame, no `0%` meter. Every
dormant run, every session on an older image and every harness but claude is in
that case.

## Publishing it from a drop-in harness

A drop-in (see [HARNESSES.md](./HARNESSES.md)) publishes this the same way it
converts its transcripts: its own code, running in the container, writing the
file above. Nothing needs declaring in the manifest and nothing host-side
changes — the directory is mounted on every spawn, whatever harness the session
resolves to, and the variable is in the container's environment.

The built-in claude provider is `hooks/statusline.py`, which Claude Code already
invokes on every status render; `codex` and `pi` publish nothing today and so
report nothing rather than failing.
