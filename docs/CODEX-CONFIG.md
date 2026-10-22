# Codex preferences in platform sessions

The platform mounts a durable **directory** at `/home/developer/.codex`.
Codex can atomically replace `config.toml`; the replacement stays in that
directory and survives the next container. Existing credential and per-session
transcript mounts remain nested at `auth.json` and `sessions`.

## Ownership and scope

The durable location belongs to the OS user running the platform on that host:

- Assistant: `~/.lmer/platform/codex-config/assistant/config.toml`, shared by
  successive assistant incarnations, including rotations.
- Worker: `~/.lmer/platform/codex-config/<scope>/config.toml`, where `scope` is
  SHA-256 of the JSON `[host, project, run_slug]` identity. Resume and replacement
  of that run select the same directory. Independent runs select different
  directories, so their concurrent config edits cannot overwrite one another.

The platform daemon serializes launches from their ownership checks through
process startup and registry publication. A concurrent request for an already
claimed worker run receives a conflict instead of starting a second writer.
If registry publication fails after process startup, the daemon stops the
unpublished launch's process group, reaps its child, and releases its PTY and
control token before allowing another launch. A cleanup failure stays reserved
in that daemon: later launch requests retry cleanup and refuse to start a child
until it succeeds. The durable config is retained for retry. Once a registered
owner exits, a later launch can reuse that run's directory.
The assistant also has one live incarnation.
Failed launches give the whole process group five seconds to handle TERM before
KILL, even if the launcher exits first, so its runtime child can forward shutdown.
These homes are not cleaned up with session logs.
An untracked worker with no resolved host/project/run identity receives its
own `untracked-<session-ID hash>` directory. There is no known run to resume
in that case, so a later launch starts a separate scope instead of sharing
writable preferences with another unidentified worker.
They are local platform state, not work-repository artifacts; never commit them.
The directory is offered to every platform launch because a preset can resolve
the harness inside the child launcher.

Direct `lmer` CLI launches and propagation of preferences between independent
runs or hosts are outside this feature. A new run starts with its own defaults.
Other Codex home files also persist within the scope; the separate nested
credential and transcript mounts retain their existing lifecycles.

## Seeding and precedence

For a new scope without `config.toml`, the Codex runner uses the existing
work-repository `agent-files/codex/config.toml` override, falling back to the
installed template. An existing file always wins. Changes made after launch,
including unrelated preferences and `notice.hide_rate_limit_model_nudge`, are
not reseeded on restart.

Optionally set `LMER_CODEX_CONFIG_SEED` in the **platform daemon's environment**
to a host-readable TOML file. The launcher copies it, mode `0600`, into a scope
only when no config exists. Later changes to the seed affect only new scopes.
An unreadable configured seed refuses a codex launch, naming the setting and
the path, rather than pretending that defaults were applied. Only a codex launch
is refused. Who refuses depends on who knows the harness:

- A request that names `codex` as its harness is refused by the daemon before
  anything starts. The session's `--harness` flag outranks everything else.
- Every other launch starts. The daemon logs one warning naming the path and
  hands the reason to the session's `lmer` in `LMER_CODEX_CONFIG_SEED_ERROR`.
  That `lmer` refuses, before any container starts, only if the harness it
  resolves is codex. It resolves the harness after its preset, model aliases,
  `.env` files and the inherited `LMER_HARNESS`/`LMER_LLM_NAME` are applied.
  So an inherited `LMER_HARNESS=codex` with a preset that picks claude runs
  claude without the seed.

The seed is a host-only input and is never forwarded to sessions, and neither
is the reason.
Paths inside the seed are used literally by Codex; use container-valid paths.

The runner still passes the resolved preset/model/effort as command-line
overrides, and the private app server receives those same overrides. They are
never written into saved config. Codex's native CLI override precedence applies.
See [OpenAI configuration precedence](https://learn.chatgpt.com/docs/config-file/config-basic).

## Upgrade and acceptance

Both the platform/launcher and session image must contain this change. The first
new scope starts from the seed or generated defaults. A preference lost with an
old container cannot be recovered automatically. Before replacing an old
container, copy any preferences you want to keep into an operator-controlled
seed; do not publish its contents.

Automated tests execute assistant and worker launch code, inspect the actual
mount arguments, and simulate fresh container homes over that directory. They
exercise seed-once behavior and atomic replacement. They do **not** substitute
for the operator's container-runtime acceptance check:

1. Start the assistant through the platform using the updated launcher/image.
2. In Codex, select **Keep current model (never show again)**. Verify only the
   value of `notice.hide_rate_limit_model_nudge` in the running container's
   `/home/developer/.codex/config.toml`; it must be `true`.
3. Record the container ID and inspect mounts. The directory ending in
   `codex-config/assistant` must mount at `/home/developer/.codex` read-write.
4. Use the platform's **Rotate assistant** action. Confirm a different container
   ID. Before editing or reseeding anything, read the same preference; it must
   still be `true`. Verify the selected launch model/effort still take precedence.
5. Repeat on a worker using the platform's resume/replacement path for the same
   run, then check a separate run does not inherit that worker's changes.

The MR remains subject to this host-side acceptance check before merge.

On the host, resolve its configured runtime and substitute the actual container
IDs before running these read-only checks (they print only the one preference):

```bash
runtime="$(python3 -c 'from lmer_cli.runtime import detect_runtime; print(detect_runtime())')"
prior_container='<assistant container ID before rotation>'
"$runtime" inspect "$prior_container" --format '{{json .Mounts}}'
"$runtime" exec "$prior_container" python3 -c 'import pathlib,tomllib; p=pathlib.Path("/home/developer/.codex/config.toml"); print(tomllib.loads(p.read_text()).get("notice",{}).get("hide_rate_limit_model_nudge"))'
```

After selecting **Rotate assistant** in the platform, substitute the replacement
container ID and run the read before any edit or repair:

```bash
replacement_container='<assistant container ID after rotation>'
test "$prior_container" != "$replacement_container"
"$runtime" inspect "$replacement_container" --format '{{json .Mounts}}'
"$runtime" exec "$replacement_container" python3 -c 'import pathlib,tomllib; p=pathlib.Path("/home/developer/.codex/config.toml"); print(tomllib.loads(p.read_text()).get("notice",{}).get("hide_rate_limit_model_nudge"))'
```

Both preference reads must print `True`, and both inspections must show the
same Codex config directory source. A restart inside the original container
does not satisfy this check.
