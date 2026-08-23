# Matrix chat — reaching the fleet from a phone

`lmer-matrix-bridge` puts one lmer platform daemon into a Matrix room. A run
that needs a human announces itself there, in a thread of its own; an
allowlisted person replies in that thread and the run continues.

That is the whole feature in slice 1: **outbound announcements and answers to
questions**. Driving a session from the room — sending input, spawning, winding
down — is slice 2 and is deliberately not here; the vocabulary for it exists in
the allowlist and is refused with its own name if you try to grant it.

---

## What the room shows

One room per orchestrator (one platform daemon), one **thread per run**.

A thread opens when a run *enters* a state that wants a human, and its root
message carries four things and nothing else:

```
Matrix bridge — stopped on a question — replying here starts a session to continue the run
Which target branch should the MR use?
http://your-platform-host:8765
```

- what the run is (its title, else its name)
- why it wants you, in plain words
- the question itself, as the platform already renders it
- a link back for everything else

Later messages about that run are replies **in the same thread**: at most one
reminder every `remind_seconds` while it is still waiting, and one line when it
resolves. A tick where nothing changed produces nothing at all — a room that
narrates the fleet every fifteen seconds is a room you mute, and a muted room
answers no questions.

The two question states read differently on purpose:

| the row says | the room says | answering it |
|---|---|---|
| `live_question` | "a running session is waiting on an answer — replying here answers it and the session continues" | writes into a channel the container is already polling |
| `question` | "stopped on a question — replying here starts a session to continue the run" | **starts a container** |

Merging those two would put you one tap from starting a container when you
meant to reply to a session that is already running.

Replying is just a reply in the thread. Nobody pastes a run id: the thread
relation is what identifies the run.

---

## Setting it up

### 1. The bridge is host-side

It runs where `~/.lmer/platform/` is — the same host as the daemon — and reads
that daemon's secret locally. Nothing is containerised, and the platform
credential never travels. Install lmer with the extra that carries the Matrix
libraries:

```bash
uv tool install 'lmer[matrix]' --from git+https://github.com/lmer2/lmer
```

The `matrix` extra is optional because one of its dependencies (`python-olm`)
ships wheels for one CPython version and Linux only; without the extra, every
other part of lmer installs as before and only `lmer-matrix-bridge` is missing.

### 2. Configure it

The bridge reads a `matrix` section from the platform's `config.json`
(`~/.lmer/platform/config.json`):

```json
{
  "matrix": {
    "name": "orchestrator-a",
    "homeserver": "https://matrix.example.org",
    "server_name": "example.org",
    "url": "https://bridge.example.org",
    "bind_address": "127.0.0.1",
    "bind_port": 29331,
    "poll_seconds": 15,
    "remind_seconds": 1800,
    "allow": {
      "@alice:example.org": ["read", "answer-live", "answer-stopped"],
      "@bob:example.org": ["read", "answer-live"],
      "@lmer-orchestrator-b:example.org": ["read", "answer-live"]
    }
  }
}
```

| key | what it is |
|---|---|
| `name` | this bridge's identity. Becomes the MXID `@lmer-<name>:<server_name>` and the ghost-user namespace `@lmer-<name>_.*`. Two daemons on one homeserver are two names, two registrations, two rooms. |
| `homeserver` | the URL the bridge talks to |
| `server_name` | the domain in MXIDs; defaults to the homeserver URL's host, which is right unless you use well-known delegation |
| `url` | where the *homeserver* pushes transactions to the bridge. Goes into the registration verbatim. |
| `bind_address`, `bind_port` | where the bridge listens. Loopback by default: the homeserver reaches it through whatever `url` names — a reverse proxy, a tunnel. |
| `room_id` | left out on a first start. The bridge creates the room, invites the allowlist, and **writes the id back here**. |
| `control_url` | the URL a **person in the room** opens to reach the control UI. The bridge cannot derive it — what it knows is a bind pair — so unset means the messages carry no link rather than a `127.0.0.1` one nobody can open. |
| `authenticated_media` | your assertion that `matrix_enable_authenticated_media` is on at the homeserver. Set it in the same change that sets the flag. Defaults to false, and while it is false the bridge attaches nothing. |
| `poll_seconds` | how often the fleet view is read (default 15) |
| `remind_seconds` | how rarely a still-waiting run is repeated (default 1800) |
| `allow` | who may do what — below |

Anything invalid is a refusal that names the key. The bridge does not start on a
guess about its allowlist.

### 3. The three secrets are environment-only

```bash
export LMER_MATRIX_AS_TOKEN=…        # appservice token, from the registration
export LMER_MATRIX_HS_TOKEN=…        # homeserver token, from the registration
export LMER_MATRIX_RECOVERY_KEY=…    # unlocks the crypto-store backup
```

They never appear in `config.json` — that file is state the daemon serves back
through its own API, and it lands in screenshots. A token-shaped key found in
the `matrix` section is itself a refusal: finding one means a secret has already
been disclosed, and the bridge will not start until it is removed and rotated.

### 4. Register it with the homeserver

```bash
lmer-matrix-bridge register > lmer-matrix-bridge.yaml
```

That prints the appservice registration derived from the config above, with
**placeholders where the tokens go**, because its natural home is a
configuration-management repository. To fill them in for a vault:

```bash
lmer-matrix-bridge register --with-secrets | your-vault-command
```

Three MSCs are involved, and they are opted into in **two different places**:

```yaml
# homeserver.yaml — both keys read in synapse/config/experimental.py
experimental_features:
  msc2409_to_device_messages_enabled: true   # to-device events (the room keys)
  msc3202_transaction_extensions: true       # OTK counts / device lists per transaction

# homeserver.yaml — synapse/config/repository.py. DEFAULT TRUE since Synapse
# 1.120: check it is not turned off rather than assuming it needs turning on.
# `matrix_enable_authenticated_media` is the matrix-docker-ansible-deploy role
# variable that sets this key; Synapse's own key has no prefix.
enable_authenticated_media: true
```

There is no `msc3202_device_masquerading` setting — grep Synapse 1.158.0 for it
and you get nothing. Device masquerading needs no homeserver switch; the
transaction extensions above plus the registration's `org.matrix.msc3202` are
the whole opt-in. (Every key on this page was checked against that version's
source rather than from memory, after a review found one that Synapse has never
had.)

```yaml
# the registration file, which `register` emits with these already in it
receive_ephemeral: true       # MSC2409, service side
org.matrix.msc3202: true      # MSC3202, service side
io.element.msc4190: true      # MSC4190 — registration only, see below
```

Each side is inert without the other: the homeserver flags do nothing unless
the registration opts in, and the opt-ins do nothing unless the homeserver
flags are on. The failure mode is **silence, not an error** — the bridge simply
never receives a transaction.

> **MSC4190 has no homeserver flag at all.** It is enabled per appservice, by
> `io.element.msc4190` in the registration (Synapse 1.158.0,
> `config/appservice.py`). Synapse reads `experimental_features` with `.get()`,
> so an invented `msc4190_enabled` there is silently ignored — a deployment that
> looks configured and does nothing. The only homeserver-wide alternative is
> `config.mas.enabled`, which forces it on for every appservice and belongs to
> the MAS / MSC3861 work that is a later slice.
>
> Synapse has implemented MSC4190 since 1.121, so on any current homeserver the
> older MSC2409+MSC3202 per-ghost-`/login` path is not needed and the bridge
> does not implement it.

`enable_authenticated_media` is different in kind: it is what makes uploaded
media require a credential to fetch. The bridge does not read it (it cannot —
see *Attachments*); you assert it with `matrix.authenticated_media`, and until
you do the bridge attaches nothing.

### 4a. Where the homeserver reaches the bridge

`url` and `bind_address` are two halves of one fact, and getting them wrong
fails **silently**: the homeserver POSTs transactions into a closed port and the
room simply stays empty. `lmer-matrix-bridge check` reports the pair as a
`reachability` line; the two working shapes are:

| your homeserver | set |
|---|---|
| in a container/pod, or on another host | `bind_address` to an address it can route to (e.g. the host's LAN IP) and `url` to `http://<that address>:<port>` |
| on this host, behind a reverse proxy you already run | `bind_address: 127.0.0.1` and `url` to the proxied address |

The default `bind_address` is loopback deliberately — a bridge holding the
fleet's answer routes should not listen on every interface by accident — so a
containerised homeserver needs this changed. Note that a homeserver in a pod
resolves `127.0.0.1` as *its own* loopback, not the host's.

### 5. Run it under something that restarts it

`lmer-matrix-bridge run` is a foreground process; nothing supervises it, and a
bridge that died with your shell session is a room that goes quiet without
saying so. A systemd **user** unit is the shape that matches where it runs
(beside the platform daemon, as your own user):

```ini
# ~/.config/systemd/user/lmer-matrix-bridge.service
[Unit]
Description=lmer Matrix bridge
# No After=network-online.target: that target does not exist in the systemd
# *user* manager, so ordering against it is inert — a line that reads as a
# guarantee and is not one. Restart=always is what covers a start before the
# network is up.

[Service]
Type=exec
ExecStart=%h/.local/bin/lmer-matrix-bridge run
EnvironmentFile=%h/.lmer/matrix-bridge.env
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
mkdir -p ~/.lmer ~/.config/systemd/user
install -m 0600 /dev/null ~/.lmer/matrix-bridge.env
# then put the three secrets in it, one KEY=value per line:
#   LMER_MATRIX_AS_TOKEN=…
#   LMER_MATRIX_HS_TOKEN=…
#   LMER_MATRIX_RECOVERY_KEY=…
systemctl --user daemon-reload
systemctl --user enable --now lmer-matrix-bridge
loginctl enable-linger "$USER"     # so it survives logout
```

The secrets belong in that file (mode `0600`), not in a shell profile: an
`EnvironmentFile` is read by systemd and never appears in `ps`, in your shell
history, or in a transcript. It sits in `~/.lmer/` beside the platform state the
bridge already reads, rather than in a directory this project otherwise never
uses. `loginctl enable-linger` is what keeps a user unit running when nobody is
logged in.

### 6. Check, then run

```bash
lmer-matrix-bridge check      # reports every precondition, changes nothing
lmer-matrix-bridge run
```

`check` is the verb to reach for when the room has gone quiet. It reports the
config, the three secrets, the crypto store, the room, the size of the
allowlist and your media assertion locally; and over the network, **whether the
homeserver accepts this appservice at all** (the most likely cause of a quiet
room — a registration that was never installed, or an `as_token` that does not
match), whether the configured room is encrypted, and whether the daemon
answers. It creates nothing, mints no device and writes no config, because a
diagnostic that changes what it measures is not one. `check --local` skips the
three questions that need the network; without the Matrix secrets it still
answers the daemon one, which needs no Matrix identity.

A first start with no `room_id` is fine — that is a **note**, not a failure, and
the bridge creates the room.

`url` depends on your bind, in three cases:

- **Loopback** (the default): unset is a **note**. The registration falls back to
  `http://127.0.0.1:<port>`, which is honest — only this host can reach it.
- **A routable address** (`10.0.0.5`): unset is **fine**. The registration falls
  back to `http://10.0.0.5:<port>`, which is an address the homeserver can dial.
- **A wildcard** (`0.0.0.0`): unset is a **failure** and `check` exits 1, because
  the registration would carry `http://0.0.0.0:<port>` — a bind directive rather
  than an address anything can dial. Set `matrix.url` to the address the
  homeserver reaches you on.

### What the first run does to your config

With `room_id` unset, the first start **creates the room** — encrypted at
creation — invites every MXID in `allow`, and then **writes the room id back
into `config.json`** through the platform's own config writer. Two consequences
worth knowing before you deploy:

- `config.json` is modified by the bridge. If you template that file from
  configuration management, either template `room_id` yourself after the first
  run or leave the key out of your template so the write survives.
- Nobody is invited later. Adding an MXID to `allow` after the room exists
  grants the capability but does not invite them — invite them in a client, or
  they can join if the room allows it.

### Rotating or re-running `register`

`register` **generates nothing**. It reads `config.json` and, with
`--with-secrets`, the three environment variables; the tokens are whatever you
put in them. So re-running it is idempotent and safe: the same config plus the
same environment produces byte-identical output, and nothing rotates behind
your back.

That also means the tokens are yours to mint (any CSPRNG — `openssl rand -hex
32`) and yours to keep in step: the registration the homeserver reads and the
bridge's environment must carry the same pair, and changing one means changing
both and restarting both sides.

---

## The allowlist

`allow` maps a Matrix id to what that id may do:

| capability | what it permits |
|---|---|
| `read` | being in the room (the bridge invites everyone listed when it creates the room) |
| `answer-live` | answering a session that is running and waiting |
| `answer-stopped` | answering a run that exited — **this starts a container** |
| `input`, `spawn`, `stop` | slice 2. Parsed and **refused** today, so nobody grants a permission nothing enforces. |

Three rules, each of which is a thing this deliberately does not do:

- **Explicit MXIDs only.** Never a server domain, never a wildcard, never room
  membership, never a power level. `@*:example.org` and `@alice:*` are refused
  rather than treated as literals, because an operator who writes a glob
  believes they granted a group.
- **Humans and bots are the same kind of entry.** Another orchestrator's bridge
  is an MXID with capabilities — `@lmer-orchestrator-b:example.org` above. Matrix
  is an agent bus, and a separate rule for bots would be a second authorization
  system.
- **`answer-stopped` is its own capability**, because it spawns. Someone trusted
  to reply to a running session is not automatically trusted to start one.

> **This is deliberately unlike `LMER_PUSH_ALLOW_LIST`**, which does match whole
> domains. A homeserver federates, and if yours has open registration then a
> domain match is not a trust unit at all. Do not harmonise the two.

An unlisted or under-privileged sender is **ignored in the room** and named in
the log. That is not politeness: a refusal posted into a federated room tells a
stranger that they found something.

The bridge holds this authorization because nothing else can. Both platform
credentials open every route — the daemon says so about itself — so an MXID that
gets past this list has the whole fleet.

---

## Encryption, and the failure mode worth understanding

The room is end-to-end encrypted from creation, and a **configured** room is
checked on the way in: the bridge refuses to run in one that is not encrypted
rather than posting your questions and answers in the clear.

The bridge keeps its crypto store at `~/.lmer/platform/matrix/crypto/store.db`
and an encrypted export of it in its own account data on the homeserver.
`LMER_MATRIX_RECOVERY_KEY` is the passphrase for that export: the first start
generates the key from it and uploads the salt (no secret) as account data, and
every later start re-derives the same key from the same passphrase — which is
what lets a host wiped down to its environment restore. **Every** successful
start refreshes the export (after folding in the store's write-ahead log, so it
is never short the rows written moments before), so the backup keeps up with
the keys the bridge accumulates.

What happens on a restart:

| what is on disk | what the bridge does |
|---|---|
| a readable store | opens it — the ordinary restart |
| nothing (a wiped host), and a backup exists | restores from the backup with the recovery key |
| nothing, and no backup | mints a new device — the first start, and the only time this is right |
| a store it cannot read, and a backup exists | restores |
| a store it cannot read, and no backup | **refuses to start**, naming the variable and the path |
| the export is empty | keeps the existing backup rather than replacing it — an empty export means the store is wrong, not that the backup should go |
| it cannot tell whether a backup exists | **refuses to start** — "cannot tell" must not become "mint a new device and overwrite the export" |

That last row is the one to know. The bridge will not quietly mint a fresh
device to get past a broken store, because a bridge that did would run, look
healthy, encrypt to keys nobody in the room holds, and leave you with a room
that has simply gone quiet. Losing both the store *and* the recovery key means
old history is unreadable to the bridge — new messages are unaffected, and
slice 1 only ever needs new messages.

---

## Attachments

The bridge may attach a file to a thread, and refuses unless **all three** hold:

1. the room is encrypted — enforced, and a configured room that is not
   encrypted stops the bridge at startup rather than downgrading anything;
2. `matrix.authenticated_media` is `true`;
3. the bytes pass the same secret scrub as text.

The second one is **your assertion, not a measurement**, and the bridge says so
rather than pretending otherwise. There is no client-visible signal for that
homeserver setting: the authenticated media endpoints are served by every
Synapse from spec v1.11 regardless of it, so "the endpoint answers" would be a
version fact standing in for a configuration fact — and it would say yes on
exactly the homeserver the guard exists to refuse. It defaults to false, so an
operator who never sets it gets no attachments rather than a leak.

Media uploaded while authenticated media is off stays anonymously fetchable by
its `mxc` id forever, and a room's history does not expire — which is why the
encryption precondition is enforced independently: what reaches the homeserver
is ciphertext whatever the flag says.

A refused attachment is dropped, the message it accompanied is still sent with a
one-line note saying so, and the precondition is logged. A **binary** carrying a
secret is refused outright rather than redacted, because a regex substitution
inside a compressed stream produces a corrupt file, not a safe one.

Long *text* is truncated with the link rather than attached: a link is one tap,
an attachment is a download.

---

## What the log will tell you

Everything the bridge declines to do, it logs and names:
`matrix_inbound_ignored` (not a thread, unknown thread, no open question),
`matrix_inbound_denied` (allowlist), `matrix_upload_refused` (which
precondition), `matrix_platform_unreachable` (once per outage, not per tick),
`matrix_crypto_store` (which plan it took on start).

If the room is quiet and the log is empty, the homeserver is not pushing
transactions — check the four experimental flags above.
