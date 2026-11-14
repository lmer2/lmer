# Human time tracking

The platform daemon records the time people spend working through lmer, per
person and per project, and writes every finished entry to `timesheet.d/` in
the work repo. A timer starts on its own when a person writes in a project's
thread and stops after they go quiet. It can also be started and stopped by
hand, from the control UI or by asking Ratchet.

lmer knows no tracker. Each project picks a **tracking handler**. The built-in
`timesheet` handler writes the entry to the work repo and nothing more. Any
other handler is a YAML definition in the work repo that names the extra fields
an entry needs. Copying entries into a tracker is the job of a sync process run
by the work repo's owner, outside lmer. That process reports back through a
**ledger** the platform reads.

Contents:

- [Turning it on](#turning-it-on)
- [People](#people)
- [Settings](#settings)
- [Projects](#projects)
- [Handlers](#handlers)
- [Entries](#entries)
- [The sync ledger](#the-sync-ledger)
- [Automatic and manual time](#automatic-and-manual-time)
- [The timesheet writer](#the-timesheet-writer)
- [Who a call acts for](#who-a-call-acts-for)
- [API](#api)
- [`lmer-ctl time`](#lmer-ctl-time)
- [Asking Ratchet](#asking-ratchet)
- [The control UI](#the-control-ui)
- [Platform settings](#platform-settings)
- [Troubleshooting](#troubleshooting)
- [Spec decisions referenced in the code](#spec-decisions-referenced-in-the-code)

## Turning it on

1. Run a platform with a work repo (`work_repo_url` in
   `~/.lmer/platform/config.json`, or `LMER_WORK_REPO`).
2. List the people whose time this platform tracks in **Platform settings →
   Time tracking** ([Settings](#settings)); it takes effect at once. The first
   person is listed there. After that, anyone can ask Ratchet to set them up
   ([Asking Ratchet](#asking-ratchet)).
3. Give the writer a credential that can push to the work repo's default
   branch, if the mirror's credential can only read
   ([The timesheet writer](#the-timesheet-writer)).
4. Deploy the control UI that matches the daemon. The header indicator, the
   time-tracking bar and the entries page are part of the UI bundle, so an
   upgraded daemon still serves the old UI until the bundle is rebuilt:
   - on a bare host, run `lmer platform setup-ui` from the new lmer checkout;
   - with the platform container, pull or build a new platform image
     (`Dockerfile.platform` builds the bundle into the image);
   - or point `LMER_PLATFORM_UI_DIST` at a freshly built `dist/`.

   The daemon reads the bundle on each request, so a browser reload picks up
   the new one. The new `/api/time/*` routes need the upgraded daemon running.

Tracking runs once at least one person is listed, the `time_tracking` switch
is on (the default) and the configuration is valid. With nobody listed
(`people: []`, the default) or the switch off, tracking is **off**:
no tracker, writer or activity consumer starts, no `timesheet.sqlite3` is
created, the time routes answer `503 time_tracking_off` with an empty
`problems` list, and the control UI shows no time controls. A problem in the
registry or in a time setting in `config.json` also turns tracking off, with
a warning in the daemon log (`platform_people_invalid`,
`platform_time_*_invalid`). The daemon still boots and serves everything else;
the time routes answer `503 time_tracking_off`, naming every problem.

## People

The registry is part of the platform's `config.json`. It has no environment
override:

```json
{
  "people": [
    {"key": "alice", "name": "Alice", "slack": ["U0123ABCD"], "matrix": ["@alice:example.org"]},
    {"key": "bob", "name": "Bob", "slack": ["U0456EFGH"]}
  ],
  "default_person": "alice"
}
```

- **`key`** is the person key that every entry carries and that names the
  person's directory under `timesheet.d/`. It is a slug
  (`[a-z0-9][a-z0-9._-]*`, at most 64 characters), unique, and not `runs`,
  `info`, `memory` or `specs`. Those name work-repo areas.
- **`name`** is for display and defaults to the key.
- **`slack`** lists the person's Slack user ids. **`matrix`** lists their MXIDs
  (`@user:server`). An identity may be listed under one person only.
- **`default_person`** is the person the operator credential acts for: the
  control UI, the web chat, and `lmer-ctl` run with the shared secret. It must
  name a listed person. With exactly one person listed it is optional: that
  person is the default. With several listed and none named, those surfaces
  cannot start or stop time (`409 time_no_default_person`), and web-chat
  messages are not activity.

An edit through the settings page, the API or `lmer-ctl` applies to the running
daemon at once ([Settings](#settings)). A hand edit to `config.json` is read
when the daemon starts, so restart it after one.

### How a message maps to a person

Only a listed identity drives that person's timer. Another human in a shared
thread never starts someone else's clock.

| record | maps by |
|---|---|
| Slack `message` / `answer` | `source.author_id`, the Slack user id. The Slack connector sets it on human posts only ([COMMS.md](./COMMS.md)). |
| Matrix `message` / `answer` | `source.author_id`, the sender's MXID. Records from a Matrix connector process started before it kept `author_id` map by `source.author` when that is an MXID. A record posted through the daemon's API (`meta.credential` set) never does. |
| `platform` record the daemon stamped `meta.credential: operator` | With author `operator` (the web chat, the web UI, or another operator-credential post that names no author): `default_person`. With an MXID author (the Matrix bridge's answers name their sender): the person that MXID is listed under, else nobody (listed as unmapped `matrix:<mxid>`). Any other author, a `platform` record without that stamp (a connector credential's, the daemon's own), or one carrying a `ref` or `meta.notification_class` maps to nobody. |
| everything else | nobody: agent, supervisor, assistant (`uber-lmer`), peer and terminal records. |

A human the registry does not list is remembered (the last 100 identities) and
shown as `unmapped` in `GET /api/time/status`, as `slack:<id>` or
`matrix:<mxid>`. Add them to `people` to track them.

## Settings

**Platform settings → Time tracking** in the control UI edits the registry and
the platform-wide time settings, so nothing needs a hand edit to `config.json`:

- **Track time**, the `time_tracking` switch. Off stops tracking and keeps
  the registry and the settings. Running timers end when it is switched off:
  an automatic one at its last activity (`trigger.stop: idle`), a manual one
  at that moment (`manual`). Removing a person ends their running timer the
  same way. Their entries are written when tracking next runs. A hand edit of
  `config.json` and a restart does not end them.
- **People**: add, edit and remove people, their Slack user ids and Matrix ids,
  and the default person. Ids that wrote recently but are not listed are shown
  under **Not listed yet** (status's `unmapped`): **Add to…** puts one on a
  person in one click, moving it from anyone who had it, or starts a new person
  with it.
- **Defaults**: the settings in [Platform settings](#platform-settings) below.
  Each field says when an environment variable is exported and wins over it.
  Below them, every project's idle grace, its own or the default it inherits.

A write is patched into `config.json` (only the keys it names; the rest stay
as they are) and applied to the running daemon: a changed registry, default
person or writer identity is swapped into the running tracker, and turning
tracking on or off starts or stops it. Every reply says what happened in
`applied`: `started`, `stopped`, `updated`, `off` (saved, and tracking stays
off), or `restart` (saved, and no tracking in this process to apply it to).
A write that would leave the registry unusable (a key that is not a slug, an
id under two people, a `default_person` nobody has) is refused, naming the
problem, and nothing is written. The writer's push token is never set here
([The timesheet writer](#the-timesheet-writer)).

Who may change what:

- **The operator credential** (the control UI, `lmer-ctl` with the shared
  secret) changes everything above, and any project's time settings.
- **An assistant credential** (Ratchet) lists or renames only the person it is
  talking to (`POST /api/time/me`), and only while it is handling a delivered
  conversation request: the request's sender is the one person known to have
  asked. Outside one, listing or renaming answers `403 time_no_request` and the
  operator does it. Inside a request, a `record` from anyone but the requester
  is refused. Their Slack or Matrix id is never taken from the request body. A key another person holds
  is refused, so an asker cannot attach themselves to someone else, and with
  nobody listed it lists no one: the first person is the one the operator
  credential acts for. It sets only its own project's time settings. Everything
  else answers `403 time_settings_operator_only`.
- **A connector credential** changes nothing.

Listing a second person when nobody was named `default_person` stores the
first as `default_person`, so the operator credential keeps acting for them.

Every credential can read the settings (`GET /api/time/settings`), as with the
other settings screens; an assistant's reply carries only its own project and
none of the people or unmapped ids, and a count of registry problems rather
than the problems, which quote ids. `503 time_tracking_off` does the same. The settings routes work while tracking is
off.

## Projects

A project is a platform project. Every project is tracked by default. Its
time settings live in the project's settings, edited in **Project settings →
Time tracking** or with `PATCH /api/projects/{id}`:

| setting | meaning |
|---|---|
| `time_project_key` | The stable key entries are filed under: the `<project>` in an entry's file name and its `project:` field. 1–64 characters of `[a-z0-9][a-z0-9._-]*`, with no `--` (it separates the parts of an entry's file name). It must not be any other project's key (`409 project_key_taken`). |
| `time_handler` | The tracking handler. Empty means the built-in `timesheet`. |
| `time_clients` | Client labels, at most 50, each 1–32 printable characters, unique ignoring case. The first label is the default for automatic entries. |
| `time_idle_grace_minutes` | This project's idle grace, a whole number of minutes, at least 1. Empty means the platform default. |

**The project key is stored the first time it is needed**, for example at the
first start or the first status read that lists the project. The key is a slug
of the project name; a name that is already taken gets `-2`, `-3` and so on. It
is never derived again, so **renaming a project keeps its key**. A settings
write that leaves the key out keeps the stored one. Changing the key by hand
files later entries under the new key. Earlier files are not moved.

**One identity with the work repo.** When the work repo has a project registry
(`_lmer.yaml`, [SCHEMA.md](./SCHEMA.md)), the settings form suggests its short
names as the key. The ones this project's repositories resolve to are listed
first, marked "matches this project's repository". A name another project
already uses as its key is listed but cannot be picked. A name that cannot be
a key (one with `--`) is left out. Suggestions never set a key on their own:
a new project's key is still the slug of its name, and a stored key changes
only when the operator picks another and saves. A platform project is not tied
to one repository and may share one with another project, so lmer cannot infer
which registry name a project is. Without a registry, or with one the daemon
cannot read, nothing is suggested.

The handler is not checked against the work repo when it is set, because the
mirror may lag behind. A project whose handler is not defined, or whose
definition is broken, tracks no time until it is: a manual start is refused, an
automatic start prompts, and the status and settings form show a `handler`
warning. It never falls back to `timesheet`. A project with no handler set uses
`timesheet`.

Typing a client label that the project does not list yet, in the bar, in
`lmer-ctl time start --client`, or in a `PATCH /api/time/current`, adds it to
the project. The settings form re-reads the project before it saves, so a label
added while the form was open is kept.

## Handlers

A handler other than `timesheet` is one YAML file in the work repo, at
`timesheet.d/.handlers/<name>.yaml`:

```yaml
# timesheet.d/.handlers/tracker.yaml
name: tracker
label: Example tracker
fields:
  - key: task_id
    label: Tracker task
    required: true
    pattern: "[a-z0-9]{6,12}"
    hint: The id at the end of the task's URL
    link: https://tracker.example.com/t/{value}
sync: true
```

| key | meaning |
|---|---|
| `name` | The handler name, a slug. It defaults to the file name and must match it. |
| `label` | Display name. Defaults to `name`. |
| `fields` | The extra fields an entry needs. Each field has: |
| `fields[].key` | the key under the entry's `fields:` (`[A-Za-z_][A-Za-z0-9_]*`, unique); |
| `fields[].label` | what the bar and the prompts call it (defaults to the key); |
| `fields[].required` | `true` when time cannot be tracked without a value (default `false`); |
| `fields[].pattern` | optional regular expression that the whole value must match (Python `re.fullmatch`; the bar checks it in the browser too, so keep to syntax both understand); |
| `fields[].hint` | optional help text shown under the field; |
| `fields[].link` | optional `http(s)` URL template: `{value}` is replaced by the URL-encoded value, and the entries page links the value there. |
| `fields[].per_issue` | `true` when the value belongs to an issue, not to the project (a tracker task per issue): it is never borrowed from the project's last value. See [Field values per issue](#automatic-and-manual-time). Default `false`. |
| `sync` | `true` when a sync process writes a ledger for this handler. Then the entries page and the header show sync status. Default `false`. |

lmer enforces the fields without knowing what they mean. A broken definition
(a field with no key, YAML that uses an alias, a duplicate key, a pattern that is not a regex, a link
that is not `http(s)`, a `name` that differs from the file) is skipped and
reported in `handler_problems` and on the entries page. Unknown keys are
reported without failing the definition. `timesheet.yaml` is refused: the
built-in handler cannot be overridden.

The platform reads definitions from its read-only work-repo mirror, so a new
handler appears once the mirror has pulled it.

### The issue map

A handler can have an issue map: `timesheet.d/.handlers/<name>/issues.yaml`,
mapping an issue URL to field values. Whoever runs the handler's sync process
(or a person) keeps it; lmer only reads it, from the mirror. It sits in a
directory because the loader reads every `.handlers/*.yaml` as a handler.

```yaml
# timesheet.d/.handlers/clickup/issues.yaml
https://git.example.com/acme/widgets/-/issues/12:
  task_id: 86c1x2y3z
```

Keys are normalised like every issue URL lmer stores (see [Entries](#entries)).
A map that is not YAML or not a mapping is listed in `handler_problems` and
gives no values; the handler still works. A key that is not an http(s) URL, a
key that repeats another after normalising, an unknown field or a value that
fails its field's pattern is skipped with a problem, and the rest still count.

### Work-repo example: a ClickUp handler

lmer ships no tracker-specific code. The example below is what a work repo
that syncs to ClickUp would hold. It is the example from the spec and is
**not part of lmer**. The `pattern` is illustrative; use the id format of your
tracker.

```yaml
# timesheet.d/.handlers/clickup.yaml (in the work repo, not in lmer)
name: clickup
label: ClickUp
fields:
  - key: task_id
    label: ClickUp task
    required: true
    pattern: "[0-9a-z]+"
    link: https://app.clickup.com/t/{value}
sync: true
```

The sync process that goes with it, and its tracker credentials and person ↔
tracker-user mapping, live in that work repo owner's own environment, never in
lmer.

## Entries

Each finished entry is one YAML file:

```
timesheet.d/<person>/<YYYY>/<MM>/<start>--<project-key>--<id>.yaml
```

The month is the UTC month of `start`, and `<start>` is `YYYYMMDDTHHMMSSZ`.
Files are append-only and never conflict, like `changelog.d/`. An entry lmer
wrote:

```yaml
# timesheet.d/alice/2026/10/20261027T090200Z--webshop--01M5Y9GMT03S9PR5SPQQ63EP8H.yaml
id: 01M5Y9GMT03S9PR5SPQQ63EP8H
person: alice
project: webshop
handler: tracker
fields:
  task_id: abc123
client: acme
title: Checkout bug triage
start: '2026-10-27T09:02:00Z'
end: '2026-10-27T09:40:00Z'
duration: 2280
trigger:
  start: activity
  stop: switch
source: lmer
sessions:
- run: git.example.com/acme/webshop/981ec036134f4a8580927ddca71658a9
  uid: 981ec036134f4a8580927ddca71658a9
- run: git.example.com/acme/webshop/develop-issue-12
issues:
- https://git.example.com/acme/webshop/-/issues/12
```

| field | meaning |
|---|---|
| `id` | Unique. lmer writes a ULID: 26 Crockford base32 characters, with the millisecond start time first, so ids sort by start time. |
| `person` | The person key. |
| `project` | The project key. |
| `handler` | The handler name. `timesheet` when absent. |
| `fields` | Handler field values, as strings. |
| `client` | The client label, or empty. |
| `title` | The entry title. |
| `start`, `end` | ISO 8601 timestamps with an offset. lmer writes UTC. |
| `duration` | Seconds. Derived from `start` and `end` when absent. |
| `trigger` | `{start, stop}`, each one of `manual`, `activity`, `idle`, `switch`, `hand` or `edit` (an edit after time had accrued, which splits the entry). |
| `source` | `lmer`, or `hand` for a hand-written entry (the default when absent). |
| `sessions` | Optional. The runs the entry covered, in the order they were added, each once. An item has `run`, the platform run key `host/project/<uid or slug>` (the key an `lmer://run/` reference carries), and `uid`, the 32-hex execution uid of a schema-2 run ([SCHEMA-V2.md](./SCHEMA-V2.md)); it needs at least one of the two. lmer writes the key only when the list is not empty. |
| `issues` | Optional. The issue and merge-request URLs the entry's run was about, the first being the one its field values follow. lmer writes the key only when the list is not empty. |

lmer writes every key above except `sessions` and `issues`, which it leaves
out when empty. A parser that does not know them, or the `edit` trigger (an
older `lmer_timesheet`), reports a soft problem and still returns the entry, so
a sync process keeps working. A `sessions` or `issues` item that cannot be
used is a problem on the entry's row and is dropped; the entry itself is kept.

Issue URLs are normalised before they are stored, compared or looked up: the
scheme and host are lower-cased, the query, the fragment and trailing slashes
are dropped, and GitLab's `/-/work_items/<n>` is written `/-/issues/<n>`. A
merge-request URL stays one, and a URL with credentials is refused.

The schema is one module, `lmer_timesheet` (`src/lmer_timesheet/`). It holds the
entry, the handler definition, the ledger line, the path layout, the parser
and the field validation. It needs only the standard library and PyYAML, so a
sync process can import it instead of copying this contract.

### Hand-written entries

Hand-written entries are valid. They need `id`, `person`, `project`, `start`,
and either `end` or `duration`, plus the required fields of their handler:

```yaml
# timesheet.d/bob/2026/10/20261027T120000Z--docs-site--hand-1.yaml
id: hand-1
person: bob
project: docs-site
start: 2026-10-27T14:00:00+02:00
duration: 900
```

Any unique id works. Put the file under the person and the UTC month of
`start`, and quote handler field values that YAML would otherwise read as
numbers. YAML anchors and aliases (`&name`, `*name`) are not accepted in entry
or handler files; write each value out. The entries page shows every `.yaml` file under the person's
directory and flags problems on its row: a missing required key or handler
field, an id used by two files, a file in the wrong directory, a `duration`
that disagrees with `start` and `end`, or an undefined handler. A file too
broken to parse is still listed, so it can be found and fixed.

To fix any entry, edit its file. The platform never rewrites an entry.

## The sync ledger

A handler's sync process appends one JSON line per entry it handled to
`timesheet.d/<person>/.sync/<handler>.jsonl`:

```json
{"id": "01M5Y9GMT03S9PR5SPQQ63EP8H", "hash": "sha256:1e827c1ebe015099ae819d81b3f1cbb9918a971aad5bd04a4f16c93d87836d80", "status": "synced", "ref": "9876", "link": "https://tracker.example.com/entries/9876", "at": "2026-10-27T10:00:00Z"}
{"id": "01M5YC4R2B8ZK6Q1H7V3N9TXWD", "hash": "sha256:…", "status": "held", "reason": "no tracker identity for bob", "at": "2026-10-27T10:00:00Z"}
```

| key | meaning |
|---|---|
| `id` | The entry's id. Required. |
| `hash` | `sha256:<hex>` of the entry file's **raw bytes** as committed, at the time of sync (`lmer_timesheet.entry_hash`). Compute it over the bytes you read, not over a re-serialised entry. Required. |
| `status` | `synced` or `held`. Required. |
| `ref`, `link` | Optional. The external entry's id and its `http(s)` URL; a line with any other `link` is skipped and reported. |
| `reason` | Optional. Why the entry is `held`. |
| `at` | When the line was written, ISO 8601 with an offset. Required. |

For each written entry under a `sync: true` handler, the platform shows:

- **`synced`**, with the link, when the last line for the id is `synced` and its
  hash matches the file;
- **`held`**, with the reason, when the last line is `held` and its hash
  matches. Held entries also show as a header warning, one per handler and
  reason;
- **`pending`**, when there is no line for the id yet ("no ledger line yet")
  or the last line's hash no longer matches the file ("changed since it was
  synced"). An edited entry is therefore pending again. The stale line's link
  is kept, so the external entry can still be opened.

The last line for an id wins. A line that does not parse is skipped and
reported on the entries page with its line number. A sync process should mark
a broken entry `held`, not drop it. lmer never writes a ledger, and the writer
never touches `.sync/`.

## Automatic and manual time

A person has at most one running timer.

**What counts as activity.** A `message` or `answer` on `slack`, `matrix` or
`platform` that maps to a person ([People](#people)) counts, including messages
to Ratchet. Its time is the record's own `at`. Agent, supervisor, assistant
and peer posts do not count, and neither do terminal output, an open session
or clicks in the web UI.

**Which project.** One message can be recorded by several sessions. Records
that share a channel message reference (`source.ref`, or else the record id)
are one activity, decided when its first copy arrives; later copies are
ignored. Its project comes from the connector bindings, never from which copies
have arrived: the owner of the message's thread, else the home project of every
session bound to that thread. If that is more than one project, or no session
is bound to the thread, nothing starts or switches. Slack and Matrix refs both
name their thread (a Matrix ref is `<thread tag>:<event id>`). The connector
tags the room by the id it resolved, so a binding kept under a `#alias` (when
the alias could not be resolved as the binding was made) is compared by the
room id the alias resolves to; a resolution is kept until the daemon
restarts. A thread is its root event, so the alias is looked up only when no
`!id` binding matched the message, or when its root is the matched thread's.
When the lookup fails for now (no answer, a rate limit or a server error), the
binding could be the message's thread: the message is ambiguous if that
binding's root is the matched thread's, and starts nothing if nothing matched.
A binding on another thread never blocks it. An alias the directory says names
no room, or one with no Matrix account to ask as, matches nothing. A failed
lookup is not repeated for the server's `retry_after`, at most 30 seconds, and
while the daemon stops no lookup is made. A Matrix
connector process started before the tag existed posts the bare event id,
which names no thread: such a message counts only when the session that
recorded it has a single Matrix thread, and is ambiguous otherwise. The daemon
starts connectors with its own Python, so bare refs end when the upgraded
daemon restarts and respawns them. A web-chat message reaches one session and
counts for that session's home.

**Which runs.** The sessions credited with an activity are those the decision
weighed whose home is the credited project: the bound sessions homed there,
or the web-chat message's own session. Each one's run goes onto the timer when
the activity starts or refreshes it, and the entry lists them as `sessions`.
A switch puts the new runs on the new timer only, and a split (a run switch or
an edit) starts the new part with its own run only. Activity on a running
manual timer's own project, from its start on, adds its runs to that timer,
which stays manual, though a run switch splits it (D27, below); other noted
activity adds none.
Ratchet has no run (it is a project's assistant, not a run), so a manual start
through it adds none; neither does the operator credential (the control UI, web chat, `lmer-ctl` with
the shared secret). A session the platform knows no run for adds nothing. A
timer keeps at most 50 runs, the earliest ones.

**Which run an entry is for.** An automatic entry belongs to one run, its
*identity*. Only a worker session with a run the platform tracks names one; an
assistant session never does. When an activity names one run, that is its run.
When it names several, the running entry's own run wins if it is among them;
otherwise the activity counts as run-less. A run that moved from its slug key
to its uid key is the same run. An entry with a run takes its **issues** (its
target when that is an issue or merge-request URL, then the issues set with
`lmer-ctl runs meta set --issue`) and its **title**: the run's title in this
orchestrator's metadata, else its slug. The title follows the run, re-read on
every activity for it and when the entry ends, until a person sets one. A
run-less entry takes the person's last title for the project, else none; a
run's name is never a fallback title. Inside the running project:

| running entry | activity | what happens |
|---|---|---|
| run R | run R | refresh |
| run R | run S, entry younger than `time_min_entry_seconds` | take over in place: run, issues, title and field values become S's, except what a person set; if S has no value for a required field, it is a split instead |
| run R | run S | split: the entry ends `switch`, a new one starts `activity` for S |
| no run | run R | adopt R in place: its run, issues and title, and its values for empty fields; if that would change a value the entry has, or R has no value for a required field, it is a split instead |
| any | no run (Ratchet) | refresh only: the person's messages to Ratchet extend the running entry |

Activity older than the entry's last activity never splits, takes over or
adopts; it only adds its runs. Run-less activity in another project is still a
project switch. While automatic tracking is off or paused, an automatic
entry's run never changes, but its title still follows the run.

A **manual** timer follows runs inside its project by the same table (D27): a
run switch splits it and the new part is manual too, started by `activity`. It
never ends by itself: when the new run has no value for a required field, the
new part runs without it ("unassigned") and the person is prompted. Activity
in other projects is only noted, and automatic tracking off or paused does not
stop a manual timer following runs.

**Automatic.**

- Activity with nothing running starts an automatic timer on the project.
- Activity on the running project refreshes it.
- Activity on another project switches: the running entry ends with
  `trigger.stop: switch` and a new timer starts.
- Activity older than the running timer's last activity, or than the person's
  last stop, is only noted unless it is on the running project. Sessions
  deliver independently, so a message can be decided after a newer one; time
  never runs backwards. The last stop is kept across restarts and is not
  pruned with the recent values.
- An automatic timer stops once its last activity is a full **idle grace**
  old: the project's `time_idle_grace_minutes`, else the platform's (default
  15). The entry ends at the **last activity**, not when the grace ran out, with
  `trigger.stop: idle`. An idle sweep runs every `time_sweep_seconds` and once
  at daemon start-up, so a timer left open across a restart is closed at its
  last activity.

**Manual overrides automatic.**

- While a manual timer runs, it is never idle and activity elsewhere does not
  switch it. That activity is only noted.
- A manual start ends whatever is running (`trigger.stop: switch`) and starts
  a manual timer. The one exception is a manual timer already running on that
  project, which takes the new values as an edit does (below).
- A manual stop means "done tracking by hand", not "done with work", so by
  default automatic tracking carries on: the next message starts an automatic
  timer. With `time_pause_hours` above 0 (the default is 0), a manual stop
  opens a **pause window** of that many hours, even when nothing was running.
  Inside it, activity is only noted. The pause ends when the person starts a
  timer by hand, or when the window runs out.
- **Automatic tracking off.** Each person can turn automatic tracking off, from
  the bar, with `lmer-ctl time auto off`, or by asking Ratchet. It stays off,
  across daemon restarts, until they turn it back on. While it is off, activity
  is only noted for automatic tracking: nothing starts, switches or prompts
  for it, and a running automatic timer gets no more time: it ends at its last
  activity, whether its grace runs out, a manual start or stop ends it, or
  automatic tracking is turned back on first. A running manual timer still
  follows runs in its project (D27). Manual start, stop and edits work as ever, and a manual start or
  stop leaves the setting as it is. `GET /api/time/status` reports it as
  `automatic: {on, off_since}`.

**Required handler fields.**

- A **manual start** is refused until every required field has a value that
  matches its pattern. The refusal names the keys in `missing`. A manual start
  never fills a field on its own; the bar offers the recent values.
- An **automatic start** fills each field as [Field values per
  issue](#automatic-and-manual-time) says. When a required one stays empty, the
  start is **suppressed**: no time is recorded, and the header prompts
  "*<project> #13 needs a <field label> to track time*" (the issue, else the
  run's title, else just the project). If another timer was running, it still
  ends there (`switch`), because the person moved on. Prompts are per issue or
  run: each clears when its key gets a value, when a start for it succeeds, or
  after `time_title_max_age_days`.
- A project whose **handler is not defined** is treated like a missing field:
  a manual start is refused, and an automatic start is suppressed with the
  prompt "*<project> needs a usable definition of handler '<name>' to track
  time*".
- A value edited while the timer runs lands in the entry when it stops.

**Field values per issue.** A field's value follows the entry's **work
item** (`work_key` in the API): the piece of work the time belongs to, its first
issue, else its run. Whether the work is billed is nothing lmer knows about.
In order, an automatic start takes:

1. the person's last value for that key (a run key matches the run's earlier
   keys too);
2. the handler's [issue map](#the-issue-map), for an issue;
3. the person's last value for the project, unless the field is `per_issue`.

Values a person set on an entry (typed, answered or edited) are remembered
for its work item only, never for the other listed issues. A value from the
issue map (2) or the project (3) is never remembered for the key, so a
corrected map applies to the next entry; a remembered value (1) the entry
carried is kept fresh while in use, unless a newer value has replaced it. A
run-less automatic entry starts without its required `per_issue` values
("unassigned", flagged in the header and on the entries page); adopting a run
fills them. A timer that takes a key clears that
key's prompt, under the run's earlier keys too, and every prompt of its run,
whichever work item the run had then. **Answering a prompt** (Start from the
prompt in the bar, or `lmer-ctl time start --prompt ID --field KEY=VALUE`)
first ends an idle timer; a prompt that this satisfies is no longer open and
the answer is refused. A running timer the prompt is for (its key, or its
run for a prompt under a run key) is filled when unassigned; one that already
has its values refuses the answer and drops the prompt (edit the timer
instead). A prompt under another issue of the same run is not the timer's.
Otherwise the answer remembers the value for the prompt's key, then: with a
manual timer running anywhere it only remembers, and the key's next start uses
the value; with automatic tracking off or paused it starts a manual timer,
which needs every required field; otherwise it ends any automatic timer
(`switch`) and starts an automatic one for the prompt's run, `trigger.start:
manual`. A prompt of an archived project is refused.

**Edits split.** Changing a non-empty title, client or field value once
`time_min_entry_seconds` has accrued ends the entry (`trigger.stop: edit`) and
goes on in a new one (`trigger.start: edit`) with the new values and the same
mode, project, run and issues. An automatic entry splits at its last activity
(the new part grows with the next message); a manual one at the edit. Filling
an empty value, or any change before the minimum, edits in place. A value
equal to the `base` the caller's form loaded is not an edit, so a title the
run renamed meanwhile is not taken for one. A value a person sets is theirs:
a take-over keeps it, and a title they set no longer follows the run.

**Client and title.** When no client is given, an entry takes the project's
first client label. When no title is given, it takes its run's title, else the
person's most recent title for the project. Recent titles, client
labels and field values are kept per person and project, most recently used
first. Each is dropped after `time_title_max_age_days` (default 30) unused.
Despite its name, that setting governs every remembered value, as well as
kept entries and prompts.

**Short entries.** An entry shorter than `time_min_entry_seconds` (default 60)
is not written. It is **kept locally** and shown on the entries page with the
reason, until `time_title_max_age_days` has passed. `0` writes every entry.

Start changes only the daemon's own state. Only a finished entry is written to
the work repo. The tracker's state is one SQLite file,
`~/.lmer/platform/timesheet.sqlite3` (mode 0600): timers, pause windows, each
person's automatic on/off, recent values per project and per work item, kept
entries (with their runs and issues), prompts per work item and the writer's
outbox.

## The timesheet writer

The writer is the only platform code that writes the work repo.

- **Outbox.** A stopped entry is committed to the outbox in the same
  transaction that ends its timer. A row leaves the outbox only once the push
  that carries it has landed. Rows are delivered oldest first, and the first
  row that cannot be delivered stops the drain, so a later entry never lands
  before an earlier one. An outage delays entries but never loses them.
  Failed drains retry with backoff (5 seconds, doubling, at most 5 minutes);
  a new entry wakes the writer at once.
- **Own clone.** The writer uses its own clone,
  `~/.lmer/platform/timesheet-clone` (mode 0700), never the read-only mirror.
  The clone holds no work of its own. Every drain starts with `reset --hard`
  onto the upstream default branch, and the clone is re-made if the work-repo
  URL changes. The outbox, not the clone, is what is undelivered.
- **One commit per entry.** Message `timesheet: <person> <project> <start>`,
  pushed to the default branch of the work repo (the clone's `origin/HEAD`),
  with fetch, rebase and retry (3 attempts per row). Hooks and signing are off
  in the clone, so the operator's git setup can neither block nor alter a
  timesheet commit. An entry whose file is already upstream counts as
  delivered, so a replay after a crash is harmless and never overwrites a
  correction made by hand.
- **Change-set guard.** After writing a file, the writer reads the clone's
  actual change set (`git status`, staged, unstaged and untracked), not the
  path it meant to write. If any path lies outside `timesheet.d/`, it refuses,
  resets and cleans the clone, and keeps the row queued. Before every push it
  checks the outgoing commits the same way. A `timesheet.d/` that is a symlink,
  or a target path that resolves outside it, is refused as well.
- **Identity.** Commits are made as `lmer timesheet <timesheet@lmer.invalid>`,
  set with `git config` in the clone. It never depends on a global git
  identity. The person is already in the entry and its path. Set
  `time_writer_name` / `time_writer_email` (or `LMER_PLATFORM_TIME_WRITER_NAME`
  / `LMER_PLATFORM_TIME_WRITER_EMAIL`) when the forge's push rules reject that
  author. The daemon reads both when it starts.
- **Credential.** By default the writer uses the mirror's credential
  (`LMER_WORK_REPO_TOKEN`, else the deprecated `GITLAB_TOKEN_worklog`, else the
  host's usual token lookup). The mirror only needs read access. The writer
  needs **push access to the work repo's default branch**, so when the mirror's
  token can only read, set `LMER_PLATFORM_TIME_WRITER_TOKEN` in the daemon's
  environment. It is environment-only and is never written to `config.json`.
  The credential is passed on each fetch and push and never stored in the
  clone's `.git/config`. The first clone runs with it, and the next command
  rewrites `origin` to the plain URL; if the daemon dies in between, the next
  drain finds the credential there and drops it. Every git message is scrubbed
  of credentials before it is logged or stored.

A push refusal, such as a protected branch or a read-only token, keeps the
entries queued. The head row's error shows in `GET /api/time/status`
(`writer_refusal`, plus a `writer` warning in the header) and on that entry's
row of the entries page.

## Who a call acts for

The person is never taken from the request body. A name passed in the body is
ignored. Each credential acts as follows:

- **The operator credential** (the control UI, the web chat, `lmer-ctl` with
  the shared secret) acts for `default_person`.
- **An assistant credential** (Ratchet) acts for the person behind the record
  that carried the request. The daemon looks, in order, at:
  1. `record`, when given: a bus record id that must be a human `message` or
     `answer` on the assistant's own session;
  2. the conversation request the credential is handling (a Slack or Matrix
     mention), while it is delivered to this assistant session and not yet
     replied to, whose sender maps through the registry. A replied or
     still-queued request is skipped;
  3. the latest human `message` or `answer` on the assistant's own session.

  An author the registry does not map is refused. The daemon does not fall
  back to an earlier one, because the latest human is who asked. An assistant
  can track time only on its own project. A start there still switches a timer
  running on another project, because a person has one running timer. A stop
  or edit never reaches another project's timer, and a response never
  describes it: its timer and finished entry are null, and a refusal because
  of it reads "a timer is running on another project", without naming it.
- **A connector credential** acts for nobody.

This is identification, not authorization.

## API

Every route is under the platform's usual authentication. Refusals answer
`{"detail": {"code", "message", ...}}`.

| route | does |
|---|---|
| `GET /api/time/status?project=&record=` | The acting person's running timer (`running`: project, mode `manual`/`auto`, start, last activity, client, title, fields, `sessions`, `identity` (the entry's run, or null), `issues`, `work_key`, `title_source` (`run`, `person`, `recent` or null), `sources` (`person` for each value a person set, `key` for a field value their memory for the work item supplied), `unassigned` (required fields it runs without), `elapsed_seconds`, `idle_at`), `running_elsewhere`, `warnings` (`no_client`, `missing_field`, `handler`, `held`, `writer`), `prompts` (each `{id, workspace_id, project_key, project_name, work_key, label, run, issues, missing: [{key, label, per_issue}], at, message}`), the `pause` window, the `outbox`, `automatic` (`{on, off_since}`), `writer_refusal`, `unmapped` identities, `acting` (who, and `via`: `default_person`, `record`, `request` or `session`), the defined `handlers` and `handler_problems`, and per-project options under `projects`: handler definition, client labels, idle grace, recent titles, clients and field values, and `key_suggestions` (`[{key, matched, taken}]`, the registry short names offered for the project key; always empty for an assistant). The operator sees every active project. An assistant sees its own: `running` only when the timer is on its project (otherwise `running` is null and `running_elsewhere` is true), prompts and warnings for its project only, and no `writer_refusal` or `outbox.last_error`. |
| `POST /api/time/start` `{project, client?, title?, fields?, record?, prompt?}` | Start by hand. `project` is the workspace id. `prompt` (an id from `status`) answers that prompt instead, and `project` may then be left out. `422 time_refused` with `missing` while a required field has no valid value. |
| `POST /api/time/stop` `{project?, record?}` | Stop by hand, opening the pause window when `time_pause_hours` gives it a length. With `project`, only when the running timer is on it. |
| `PATCH /api/time/current` `{client?, title?, fields?, record?, base?}` | Edit the running timer. An omitted value is left as it is; `""` clears it. A change after time has accrued splits the entry (outcome `split`). `base` holds the values the caller's form loaded: a value equal to it is not an edit. |
| `PUT /api/time/auto` `{on, record?}` | Turn the acting person's automatic tracking on (`true`) or off (`false`); off stays until turned on. Turning it on ends a running automatic timer at its last activity when tracking was off, and the reply then carries that `entry`, `seq` and `kept` too. The reply carries `automatic` and the running timer; for an assistant on another project, the timer and the finished entry are null. `400 time_invalid` when `on` is not a boolean. |
| `GET /api/time/settings` | `tracking` (`{switch, runs, problems, live}`), `people` as stored, `default_person` (`{stored, effective}`), `defaults` (per setting: `value`, `source` (`env`, `config.json` or `default`), `stored`, `env`, `kind` (`int` or `text`), `default`, and `minimum` for an `int`), `projects` (`[{workspace_id, name, idle_grace_minutes, effective_idle_grace_minutes}]`) and `unmapped`. |
| `POST /api/time/people` `{key, name?, slack?, matrix?}` | Operator. Add a person. `409 time_person_exists` for a listed key. |
| `PATCH /api/time/people/{key}` `{name?, slack?, matrix?, add?}` | Operator. A list given replaces the stored one; `add` (`["slack:U…", "matrix:@…"]`) appends ids, moving each from whoever had it. The key cannot change. |
| `DELETE /api/time/people/{key}` | Operator. Remove a person; a `default_person` naming them is cleared. |
| `PATCH /api/time/defaults` `{tracking?, default_person?, idle_grace_minutes?, min_entry_seconds?, pause_hours?, title_max_age_days?, sweep_seconds?, writer_name?, writer_email?}` | Operator. `null` or `""` clears a key so the default shows through. |
| `POST /api/time/me` `{key?, name?, record?}` | Assistant. List the person who asked under a new `key`, or rename them; the reply adds `person`, `created` and `identity`. `409 time_identity_listed` when they are already listed under another key. |
| `PATCH /api/time/project` `{project?, handler?, clients?, idle_grace_minutes?, project_key?, record?}` | A project's time settings: the operator's for any project, an assistant's for its own. `""` clears one; the project key cannot be cleared. |
| `POST /api/time/handlers/check` `{name, text}` | Parse a handler definition with the daemon's own parser: `{ok, handler, problems}`. `name` is the file name without `.yaml`. |
| `GET /api/time/entries?from=&to=&project=&client=&handler=&status=&record=` | The acting person's entries, newest first. `from`/`to` are a UTC day (`YYYY-MM-DD`, `to` inclusive) or an ISO 8601 timestamp; `project` is a project key or workspace id; `status` is a stage (`running`, `in_outbox`, `kept_locally`, `written`) or a sync status (`synced`, `held`, `pending`). Each row has its `stage`, `sync` (`status`, `detail`, `link`, `ref`, `reason`, `at`, `hash`), `path`, `origin` (`tracker`, `outbox`, `mirror`, `clone`), `raw` text, `problems`, `sessions` (the linked runs, `[{run, uid}]`), `issues`, `identity`, `title_source` and `unassigned`. |

Written entries are read from the mirror and from the writer's clone, so an
entry pushed since the mirror last pulled is still listed. The clone is read
only while it has seen upstream commits the mirror's `HEAD` lacks; once the
mirror has pulled past it, the mirror alone is listed, so an entry file
deleted or moved in the work repo does not come back from the clone. Ledgers
and handler definitions are read from the mirror.

| code | status | when |
|---|---|---|
| `time_tracking_off` | 503 | tracking is not running; `problems` lists why (empty when nobody is listed in `people`) |
| `time_no_default_person` | 409 | the operator credential acts for nobody |
| `time_record_not_operator` | 400 | `record` passed with the operator credential |
| `time_caller_refused` | 403 | a connector credential |
| `time_no_session` | 403 | an assistant credential with no session |
| `time_record_foreign` | 403 | `record` is not on the caller's session |
| `time_record_not_human` | 422 | the record is not a human's message or answer |
| `time_identity_unmapped` | 403 | the author or request sender is not a listed person |
| `time_no_record` | 422 | no human message on the session says who asked |
| `time_no_project` / `time_other_project` | 403 | an assistant with no project, or naming another project |
| `time_project_required` | 400 | a start with no `project` and no `prompt` |
| `time_prompt_unknown` | 404 | `prompt` is not one of the acting person's open prompts |
| `time_prompt_project` | 400 | `prompt` and `project` name different projects |
| `time_invalid` / `time_invalid_filter` | 400 | a malformed value or filter |
| `time_refused` | 422 | the tracker would not do it; the body carries `reasons`, `missing` and the tracker's result |
| `time_settings_operator_only` | 403 | a people or defaults write with a credential other than the operator's |
| `time_settings_assistant_only` | 403 | `time/me` with a credential other than an assistant's |
| `time_settings_invalid` | 400 | a settings write the registry or a setting's rule refuses; `problems` lists each |
| `time_settings_unreadable` | 409 | a people, `default_person` or `time/me` write while `people` in config.json is not a list; fix the file by hand |
| `time_settings_web_chat` | 422 | `time/me` for a web-chat message, which acts for `default_person` |
| `time_registry_empty` | 409 | `time/me` with nobody listed: the operator lists the first person |
| `time_record_not_asker` | 403 | `time/me` naming a record that is not from the requester |
| `time_no_request` | 403 | `time/me` listing or renaming outside a delivered conversation request |
| `time_person_exists` / `time_person_unknown` / `time_identity_listed` | 409 / 404 / 409 | a key already listed, no such person, an asker listed under another key |

Inside a conversation request, a refusal is recorded as a refusal before any
effect, so the same call may be retried.

## `lmer-ctl time`

None of the verbs takes a person. The daemon resolves who asked
([Who a call acts for](#who-a-call-acts-for)).

```
lmer-ctl time status  [--project P] [--record ID]
lmer-ctl time start   --project P | --prompt ID [--client C] [--title T] [--field KEY=VALUE]... [--record ID]
lmer-ctl time stop    [--project P] [--record ID]
lmer-ctl time set     [--title T] [--client C] [--field KEY=VALUE]... [--record ID]
lmer-ctl time auto    on|off [--record ID]
lmer-ctl time entries [--from DATE] [--to DATE] [--project P] [--client C]
                      [--handler H] [--status S] [--record ID]
lmer-ctl time settings
lmer-ctl time person add KEY [--name N] [--slack ID]... [--matrix MXID]...
lmer-ctl time person set KEY [--name N] [--slack ID]... [--matrix MXID]...
                      [--add slack:ID|matrix:MXID]... [--clear-slack] [--clear-matrix]
lmer-ctl time person remove KEY
lmer-ctl time defaults set [--tracking on|off] [--default-person KEY]
                      [--idle-grace-minutes N] [--min-entry-seconds N] [--pause-hours N]
                      [--title-max-age-days N] [--sweep-seconds N]
                      [--writer-name T] [--writer-email T]
lmer-ctl time me      [--key K] [--name N] [--record ID]
lmer-ctl time project set [--project P] [--handler H] [--client C]... [--clear-clients]
                      [--idle-grace-minutes N] [--key K]
lmer-ctl time handler check FILE
```

`person` and `defaults` are the operator's ([Settings](#settings)); `person`
names a registry entry to edit, never the person a call acts for. `me` takes
no id: the daemon reads it from the request. An empty value clears a default
or a project setting. `handler check` sends the file with its stem as the
handler name.

`--project` is the workspace id (`--project` on `entries` also takes a project
key). `--field` is repeatable and splits at the first `=`. `set` edits the
running timer (`PATCH /api/time/current`): a flag left out leaves that value
as it is, an empty value clears it, and `set` with nothing to change is
refused before any request. `start --prompt` answers a prompt from `status`.
`auto` is `PUT /api/time/auto`. `--record` names a bus record id (from
`lmer-ctl comms`) on the caller's own session.

A run's issues beyond its target are set with
`lmer-ctl runs meta set <run key> --issue URL [--issue URL]...` (replacing the
list) and cleared with `--clear-issues`.

## Asking Ratchet

Ratchet's `orchestrate` instructions tell it what to do when a person asks in
plain words to start, stop or check their time ("start my time", "I'm done for
today"). It calls `lmer-ctl time start --project "$LMER_WORKSPACE_ID"`, `stop`
or `status` while it handles the request, and tells the person what came back.
"I'm on #604 now" retitles the running timer with `lmer-ctl time set --title`.
"Stop tracking me automatically" and "track me automatically again" are
`lmer-ctl time auto off` and `lmer-ctl time auto on`. It never passes a name.
A retitle once time has accrued splits the entry ([Edits
split](#automatic-and-manual-time)). The person's messages to Ratchet extend
the running entry and never retitle it. When a start is refused with `missing`, Ratchet
checks `lmer-ctl time status` for a recent value of that field, but never for a
`per_issue` field, whose value belongs to one issue. It retries with a recent
value and says which one it used, or asks the person in the thread and retries
with `--field`. A prompt in `status` is answered with
`lmer-ctl time start --prompt ID --field KEY=VALUE`. A run about more issues
than its target gets `lmer-ctl runs meta set <run key> --issue URL`. Ratchet
never invents a value.

"Set up time tracking for me" walks the person through
[Settings](#settings) from the chat, asking only for what is missing: `time
me` lists them (their id comes from the request), `time project set` sets the
project's handler and clients, and a handler definition the tracker needs is
written by Ratchet in its own work-repo clone, checked with `time handler
check`, and pushed; it counts once the platform's mirror has pulled it. What
only the operator may change, Ratchet points to Platform settings → Time
tracking. No token goes through Ratchet or the bus: the writer's push token is
`LMER_PLATFORM_TIME_WRITER_TOKEN` in `~/.lmer/.env` on the daemon's host
(restart the daemon after), and a tracker's API token belongs to its sync
process, outside lmer.

## The control UI

- **Header.** The clock is the only time control in the header. While a timer
  runs it shows the mode's timer icon (plain for manual, with arrows for
  automatic) on a fill derived from the header's project colour: the same hue,
  made lighter or darker until it stands out from the header at 3:1, with a
  black or white icon, whichever reads better. Clicking it then opens and closes
  the bar's editor. While no timer runs it shows and hides the bar, and a badge
  on it counts the warnings and prompts the hidden bar holds. When several
  people are listed and no `default_person` is set, the header shows "time: no
  default person" instead; with tracking off it shows nothing.
- **Time-tracking bar**, full width under the header. The layout reserves its
  height, so the drawers and the view start below it and it covers nothing. It
  is **always shown while a timer runs**; otherwise the clock shows or hides
  it. It has two states:
  - **Slim**, one line: the timer's mode icon, project, client (a chip), title,
    elapsed time and mode, or with no timer the project a Start would track.
    Then the warning chips (no client, a missing handler field, entries held by
    sync, entries not written, a suppressed automatic start), then Start or
    Stop, and **Edit**. A held chip opens the entries page filtered to held
    entries; a prompt chip opens the editor on that project.
  - **Edit** adds the controls: project, client and title (comboboxes with the
    project's labels and recent titles), one input per handler field (label,
    `*` when required, hint, recent values, pattern check), then Start, Update
    (when the running timer's values were edited), Stop, **View entries** and
    the **Automatic** switch, which turns the person's automatic tracking on or
    off. Start stays disabled until every required field is valid. Missing
    fields, a missing client, a handler warning, automatic tracking being off,
    and the pause window are shown here. **Done** returns to slim.
- **Sidebar.** The project that is tracking carries a timer icon: plain for
  manual, with arrows for automatic.
- **Platform settings → Time tracking.** The switch, the people and the
  platform-wide defaults ([Settings](#settings)).
- **Project settings → Time tracking.** Project key, tracking handler, client
  labels and idle grace. The project key offers the work repo's registry
  short names ([Projects](#projects)). The section is hidden while tracking is
  off.
- **Time entries**, a dialog over the current view, so the run being read keeps
  its place; it closes with its close button, Escape or a click outside, and
  focus goes back to what opened it. It is
  read-only, for debugging. It has filters for date
  range, project, client, handler and status. Entries are grouped by UTC day,
  with totals per day and per project. Each row shows start, end, duration,
  project, client (or a warning), title, handler fields (linked through the
  handler's `link`), trigger icons, the stage, and the sync status (synced
  with its link, held with its reason, or pending). Only `http(s)` links are
  linked; any other link shows as plain text. An entry that covered runs shows
  a link icon with their count. Expanding a row shows the file path, id,
  where it was read from, the linked runs, the ledger reference and hash, and
  the raw text. A linked run opens in the fleet view, the way a run reference
  in chat does, and closes the dialog; a run the fleet view no longer tracks
  shows a notice instead.

The UI reads `GET /api/time/status` on the fleet view's own poll.

## Platform settings

Each setting resolves in this order: the environment variable, then the
`config.json` key, then the default. The tracker and the idle sweep re-read the
numbers as they run. The registry, the writer's identity and the switch are
read when the daemon starts and whenever [Settings](#settings) writes them; a
hand edit to `config.json` needs a restart. None of them reaches a session
container. The same list, with each variable, is in
[LMER-CLI.md](./LMER-CLI.md).

| `config.json` key | environment | default | meaning |
|---|---|---|---|
| `time_tracking` | (none) | `true` | The switch: `false` stops tracking and keeps the registry. |
| `time_idle_grace_minutes` | `LMER_PLATFORM_TIME_IDLE_GRACE_MINUTES` | `15` | Idle grace for projects without their own; at least 1. |
| `time_min_entry_seconds` | `LMER_PLATFORM_TIME_MIN_ENTRY_SECONDS` | `60` | Shorter entries are kept locally; `0` writes every entry. |
| `time_pause_hours` | `LMER_PLATFORM_TIME_PAUSE_HOURS` | `0` | Pause after a manual stop, in hours; `0` is none. |
| `time_title_max_age_days` | `LMER_PLATFORM_TIME_TITLE_MAX_AGE_DAYS` | `30` | How long recent values, kept entries and prompts are kept; at least 1. |
| `time_sweep_seconds` | `LMER_PLATFORM_TIME_SWEEP_SECONDS` | `30` | Idle sweep interval; at least 1. |
| `time_writer_name` | `LMER_PLATFORM_TIME_WRITER_NAME` | `lmer timesheet` | The writer's commit author name. |
| `time_writer_email` | `LMER_PLATFORM_TIME_WRITER_EMAIL` | `timesheet@lmer.invalid` | The writer's commit author email. |
| (none) | `LMER_PLATFORM_TIME_WRITER_TOKEN` | unset | The writer's push token; unset uses the mirror's credential. |

An unusable environment value warns and falls through to `config.json`. An
unusable `config.json` value warns and turns tracking off until it is fixed,
rather than recording time on a number nobody chose. A writer name or email
must be non-empty text of at most 200 characters, with no control characters
or angle brackets. The email must contain `@` and no spaces.

## Troubleshooting

- **Nothing starts automatically.** Check `unmapped` in `GET /api/time/status`
  (the person is not listed); `prompts` (a required field has no remembered
  value); `pause` (a manual stop within `time_pause_hours`); and whether the
  session that recorded the message belongs to a project (the daemon log has
  `platform_time_activity_skipped` with the reason, including ambiguous ones).
- **Entries are not appearing in the work repo.** `outbox.depth` and
  `writer_refusal` in the status say what the head row is waiting on. A push
  refused for permissions needs `LMER_PLATFORM_TIME_WRITER_TOKEN` or a writer
  identity the forge accepts. A row whose file already exists upstream counts
  as delivered: the path carries the entry's unique id, so the file is that
  entry, perhaps corrected by hand, and the file is kept as it is.
- **A held entry.** The ledger's `reason` is the sync process's. Fix the cause
  on the sync side (often a missing tracker identity for the person); the next
  `synced` line clears it.
- **`time_tracking_off`.** The `problems` list names each bad registry entry or
  setting in `config.json`; fix them in [Settings](#settings) (or by hand, then
  restart the daemon). An empty list means nobody is listed in `people`, or the
  message says the `time_tracking` switch is off: add a person, or turn it on.

## Spec decisions referenced in the code

Code comments cite the design's decision numbers. Each one is described in
this page:

| ref | where |
|---|---|
| D1 | idle grace, stop stamped at last activity: [Automatic and manual time](#automatic-and-manual-time) |
| D2 | the person registry: [People](#people) |
| D3 | header, bar, sidebar, Ratchet: [The control UI](#the-control-ui), [Asking Ratchet](#asking-ratchet) |
| D4 | manual overrides automatic; the pause window, none by default; a person's automatic tracking off: [Automatic and manual time](#automatic-and-manual-time) |
| D5 | what counts as activity: [Automatic and manual time](#automatic-and-manual-time) |
| D6 | a project is a platform project: [Projects](#projects) |
| D7 | tracking runs in the daemon; restart closes idle timers: [Automatic and manual time](#automatic-and-manual-time) |
| D8 | start changes live state only; stop writes the entry: [Automatic and manual time](#automatic-and-manual-time) |
| D9 | who a call acts for: [Who a call acts for](#who-a-call-acts-for) |
| D10 | one message seen by several sessions: [Automatic and manual time](#automatic-and-manual-time) |
| D11 | short entries kept locally: [Automatic and manual time](#automatic-and-manual-time) |
| D12 | client labels and titles: [Projects](#projects), [Automatic and manual time](#automatic-and-manual-time) |
| D13 | `timesheet.d/` is the log of record: [Entries](#entries) |
| D14 | the dedicated writer and outbox: [The timesheet writer](#the-timesheet-writer) |
| D15 | layout, entry format, project key: [Entries](#entries), [Projects](#projects) |
| D17 | a missing tracker identity shows as `held`: [The sync ledger](#the-sync-ledger) |
| D18 | the entries page: [The control UI](#the-control-ui), [API](#api) |
| D19 | handlers and required fields: [Handlers](#handlers), [Automatic and manual time](#automatic-and-manual-time) |
| D20 | the sync ledger: [The sync ledger](#the-sync-ledger) |
| D21 | which run an activity is for: [Automatic and manual time](#automatic-and-manual-time) |
| D22 | an entry's run, issues, title and the run table: [Automatic and manual time](#automatic-and-manual-time) |
| D23 | The person's messages to Ratchet extend the running entry: [Automatic and manual time](#automatic-and-manual-time) |
| D24 | edits after time accrued split: [Automatic and manual time](#automatic-and-manual-time), [API](#api) |
| D25 | field values per work item, the issue map, prompts and answers: [Handlers](#handlers), [Automatic and manual time](#automatic-and-manual-time) |
| D26 | an entry's `issues`: [Entries](#entries) |
| D27 | manual timers follow runs: [Automatic and manual time](#automatic-and-manual-time) |
| D28 | `sessions` per split part; Ratchet adds no run: [Automatic and manual time](#automatic-and-manual-time) |
