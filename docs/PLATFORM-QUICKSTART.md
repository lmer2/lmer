# Platform quickstart

Get the lmer platform — the web control plane for a fleet of lmer sessions,
with its supervising assistant ("uber lmer") — running on a host in a few
minutes. The full reference for a container deploy is
[PLATFORM-CONTAINER.md](./PLATFORM-CONTAINER.md); this page is the short path.

## Install and first run

From a checkout of this repository:

```bash
git pull
uv tool install --force --from . lmer   # a copied install is not updated by git pull
lmer platform setup-ui                  # once per update, from the checkout
lmer platform run
```

`setup-ui` builds the web UI and installs it into the platform's state
directory (`~/.lmer/platform/`), so `lmer platform run` afterwards works from
any directory. Re-run it after every update — `npm run build` alone does not
refresh what the daemon serves.

By default the daemon binds loopback (`127.0.0.1:8600`). Open the printed URL
in a browser; it prompts for credentials — any username, and the shared secret
as the password:

```bash
lmer platform secret     # prints it
```

## Reaching it from another machine

Two options.

**Bind an interface directly** (plain HTTP, trusted networks only):

```bash
lmer platform run --bind 0.0.0.0 --port 8180
```

The ports in these examples (`8180` here and below, `7180` behind the proxy)
are arbitrary choices, not defaults — they are deliberately distinct from
`8600` so it is obvious which side of the proxy each number belongs to.

**Front it with a reverse proxy** (TLS, the recommended shape). Bind the
daemon to loopback and put nginx in front. The one thing that always gets
missed: the terminal runs over a WebSocket, and nginx forwards upgrades only
when told to — and its default `proxy_read_timeout` of 60s (or lower) will cut
a quiet terminal socket on a rhythm.

```nginx
# http{} context (top of a conf.d file, above the server block):
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 8180 ssl;
    server_name example.host;
    ssl_certificate     /etc/letsencrypt/live/example.host/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/example.host/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    location / {
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;

        # WebSocket upgrade (the terminal) — all three lines required
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;

        # terminal sockets are long-lived and mostly quiet
        proxy_read_timeout 1h;
        proxy_send_timeout 1h;

        proxy_pass http://127.0.0.1:7180/;
    }
}
```

Then start the daemon on the proxied port, and tell it the address its
*containers* should use — sessions it spawns (the assistant in particular)
reach the API from inside containers, where the host's loopback does not
exist:

```bash
LMER_PLATFORM_CONTAINER_URL=https://example.host:8180 \
    lmer platform run --bind 127.0.0.1 --port 7180
```

Without the variable, a wildcard bind lets the daemon derive a container
address on its own (podman's host alias, or docker's bridge gateway); behind a
loopback bind + proxy it cannot, so the variable is required in this shape.

**Gotcha:** sessions capture the container URL at *spawn time*. If you change
it — moving behind a proxy, changing ports — a surviving assistant from the
previous daemon keeps calling the old address forever. Restart it from its
drawer (the restart button), or:

```bash
curl -X POST -H "Authorization: Bearer $(lmer platform secret)" \
    http://127.0.0.1:7180/api/assistant/rotate
```

Standing orders and the handover note survive a restart; only the
conversation window is lost.

## Using it

- **Fleet view** — every run this platform tracks, attention first. The row
  with the orange border and the robot is uber lmer itself; `chat` on that
  row (or the robot in the app bar) opens its drawer.
- **Spawn a run** — the `run` button. Taskdef + target, optionally a preset,
  a fan-out roster (`--agents`), a title.
- **Answer a question** — a run stopped on a question shows a reply box.
  Answering a *stopped* run starts a fresh session with the answer attached;
  replying to a *live* one is delivered into the waiting session.
- **Wind down vs exit** — wind down asks the agent to wrap up and end itself;
  exit ends it now. Both under the run's detail view.
- **uber lmer** — the supervising assistant. It hears attention changes and
  session milestones (`lmer-signal`), can spawn/answer/wind-down runs on
  request, and keeps operator-taught standing orders across restarts. Teach it
  rules in plain chat ("spawn reviewers with preset X"); restart it from the
  drawer when needed.
- **uber lmer settings** — how its session is *run* (model, harness, preset,
  agents fan-out), per platform instance: the settings entry in the drawer's
  overflow menu, or `GET`/`POST /api/assistant/config`. Each value shows which
  layer decided it (an `LMER_PLATFORM_ASSISTANT_*` export beats what the
  dialog persists to `config.json`, and the dialog says so). Changes apply to
  the **next** incarnation — the running one keeps its context window until
  you restart it, and the dialog offers the restart. Standing orders are the
  chat's to edit and are deliberately not in this dialog.
- **Adopt / forget** — runs started outside the platform can be adopted into
  the fleet view (`+ run` → adopt, or `POST /api/runs/adopt`); ended runs can
  be forgotten from their card (undo window; nothing on disk is touched).

## Where things live

| what | where |
|---|---|
| platform state, logs, UI | `~/.lmer/platform/` |
| shared secret | `lmer platform secret` |
| per-session host log | `~/.lmer/platform/logs/<session-id>.log` |
| events ledger | `~/.lmer/platform/events.jsonl` |
| REST surface | `GET /api` (plain-text route index) |
