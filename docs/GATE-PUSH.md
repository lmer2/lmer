# Publishing through gate-push

`gate-push` authorizes the remote destination, runs the normal gate checks,
then pushes. Without a target option it keeps the current-branch push and
upstream tracking behavior. `--tag NAME`, `--ref REFSPEC`, and
`--remote NAME` retain their existing meanings.

## A rebased branch

First obtain explicit human operator permission for the scoped history rewrite.
Only then set the approval variable for that command and supply the exact
destination branch and old remote commit you intend to replace:

```sh
LMER_FORCE_PUSH_EXPLICITLY_APPROVED_BY_USER=1 gate-push --force-with-lease refs/heads/feature/example OLD_OID --remote origin
```

Replace `OLD_OID` with the full, lowercase commit object ID (40 hexadecimal
characters for SHA-1 repositories, 64 for SHA-256). It must be nonzero. There
is no shorthand lease that reads or refreshes a remote-tracking ref.

Before rebasing, inspect the configured push endpoint and branch without
printing URL credentials:

```sh
gate-push --inspect-lease refs/heads/feature/example --remote origin
```

This read-only helper captures Git output internally and prints only the
transport, hostname and optional port, plus the branch's commit ID and ref.
It omits URL userinfo, path, query and fragment; endpoints without a renderable
host get a fixed placeholder. Git failures report the operation and exit status
without captured stdout/stderr or exception details, which may contain even
bare credential values. This is structural omission, not universal secret
detection: hostnames and branch names remain visible. The helper neither pushes
nor runs the gates and does not grant authorization. Check the displayed host
against your intended named remote and retain the reported ID as `OLD_OID`.
Do not print `git remote get-url` output or interpolate a
credential-bearing URL into a command transcript. Use the configured remote
name for both inspection and the later push. Inspection refuses missing or
symbolic branches and multiple push URLs; it never refreshes a later lease.

Inspection also disables Git's inherited trace and verbose HTTP diagnostics
before its first Git call, throughout configuration capture and the private
transport. It removes `GIT_TRACE*` and `GIT_CURL_VERBOSE` from the child
environment and explicitly disables all three Trace2 targets, overriding
system/global configuration and included files. Merely unsetting environment
targets would allow those configured trace destinations to remain active.
This prevents Git from writing inspection URLs to its diagnostic files,
directories, descriptors or sockets. It does not edit the caller's environment
or configuration, and push diagnostics keep their normal behavior. External
credential helpers, hooks, proxies and SSH programs remain trusted; their own
independent logging is outside this inspection guarantee.

After rebasing and committing, run the leased push from a clean checkout.
The source is always the checked-out branch's committed HEAD. A differently
named destination is supported when that destination is authorized.

The gate passes Git both `--force-with-lease=DEST:OLD_OID` and the explicit
`GATED_OID:DEST` refspec. Git checks the expectation with the actual update;
the remote inspection is not a substitute for this lease. A remote change
after you selected the expectation makes the push fail, preserving that
writer's work. Even a fetch during checks cannot refresh the expectation.
There is no automatic retry or unconditional-force fallback. Inspect and
reconcile the remote changes before choosing a new expectation.

## Authorization and supported targets

Under the [Git rules](../rules/git.md), an agent must first obtain explicit
human operator permission for the specific history rewrite. Existing explicit
approval for that same scoped action need not be requested again. Generic task
or development authorization and repository/ref allowlisting alone are
insufficient. The normal repository and destination-ref allowlists still apply.

`LMER_FORCE_PUSH_EXPLICITLY_APPROVED_BY_USER` is a workflow checkpoint, not
independent authentication of human consent. Only after that permission may
the agent set it to exactly `1` in the approved command's environment. Unset,
empty, whitespace, alternate spellings/numbers (including `true`, `yes`, `01`
and `1.0`), or any other value refuses before normal gates, source pinning,
transport preflight or force-operation network activity. The refusal says:

> Force push blocked. Obtain explicit operator permission first; only then set LMER_FORCE_PUSH_EXPLICITLY_APPROVED_BY_USER=1 for this command.

Do not export it for the session, add it to defaults, presets, taskdefs or
persistent `.env` files, or automatically enable it after refusal. It belongs
only to the approved command. Exact `1` permits entry into the existing checks;
it does not bypass any check or authorize unrelated history changes. Ordinary
pushes and read-only `--inspect-lease` do not require it. An approved leased
update uses `gate-push` without a direct `git push` exception.

Leased mode supports exactly one fully qualified `refs/heads/...` destination
on exactly one push URL. It refuses tags, deletions, wildcard or compound
refspecs, short names, arbitrary source expressions, detached HEAD, mirror
remotes, and advertised symbolic destination branches. `--tag` and `--ref`
cannot accompany a lease. Repeated lease/remote options and abbreviated long
options are refused. There is no `--force` or `-f` option. Missing, abbreviated,
zero, or symbolic expected IDs are refused.

## What remains fixed during checks

A prepared Git reference verification transaction holds HEAD and source-branch
locks until the push and its receipt finish. This prevents cooperating Git
writers from moving the checked source. A lost lock, changed source, or dirty
index/worktree refuses the push. Checks that edit files require committing
those edits and running the gate again. The actual push uses the literal
commit ID, never a source ref that Git could resolve to a later commit.

The gate captures the push URL it authorized. Before delivery it builds a
private Git directory with a snapshot of the effective configuration, including
conditional includes resolved in the source checkout. Include directives and
URL rewrite rules are removed from that snapshot: the authorized URL is already
resolved, and neither `insteadOf` nor `pushInsteadOf` may redirect it again.
The actual push receives this snapshot in its environment; changes to local,
worktree, global, system, included, or command configuration after capture
cannot change its destination. A second URL precheck alone would not provide
this guarantee.

The private view shares the source's objects and refs, copies its locked HEAD,
and preserves its worktree and hooks path. Credential helpers, URL-scoped HTTP
settings, named-remote proxies, SSH settings and their original configuration
precedence are retained. Helpers, hook programs, SSH configuration and transport
services remain trusted programs; their own files are not snapshotted. Explicit
service selectors are a separate restriction: the selected named remote must
not configure `receivepack`, `uploadpack`, `vcs` or `serverOption`, even with an
empty or default value. These can select different services or pass opaque
service options outside the checked endpoint. The gate refuses them before
advertisement or push, including values from includes and command configuration.
Settings on unrelated remotes do not affect this restriction. The
private config files live in a private temporary directory and are removed
after receipt emission. No repository or host configuration is edited.

This requires Git's files reference backend and its empty remote-URL list reset
semantics; unsupported pinning refuses the operation. The gate verifies exactly
one captured URL. Mirror mode, automatic tag following and submodule pushes are
disabled in the actual command. Explicit source/destination arguments replace
configured push refspecs. Raw URL targets retain anchored allowlist checks and
use the authorized URL literally, without named-remote settings or rewrites.
Supported transports are file/local, scp-style SSH, `ssh`, `git+ssh`, `ssh+git`,
`git`, `http` and `https`, still subject to the normal URL allowlist (which
refuses bare filesystem push targets). Explicit `<helper>::...` URLs and other
protocol helpers are unsupported. `GIT_OVERRIDE_VIRTUAL_HOST`, which overrides
the native Git daemon's host selection, and `GIT_NAMESPACE`, which selects a
different ref namespace in a local transport service, must be unset. Inspection
uses the same transport restrictions. These restrictions do not change ordinary
pushes.

The remote controls its ref namespace. Symbolic-branch inspection refuses
advertised aliases; the lease itself protects the expected remote object ID
under Git's ref semantics, not concurrent server-administrator changes to
symbolic-ref layout. External credential helpers, hooks, proxies, SSH programs
and server administration remain trusted, as for ordinary Git pushes; the gate
does not defend against malicious implementations of those programs.

## Results

A failed gate never pushes. Exactly one gate receipt records the checks and
the final command status. Missing or invalid approval records `outcome: fail`
with nonzero `exit_code`, without claiming passed checks or test coverage.
A push rejection after green checks is recorded as
`outcome: pass` with a nonzero `exit_code`; this is not a successful delivery.
Leased receipts also name the pinned source in `commit_sha`. The existing
gate-in-flight marker covers checks, push and receipt as before.

Git contracts: [explicit leases](https://git-scm.com/docs/git-push),
[reference transactions](https://git-scm.com/docs/git-update-ref), and
[configuration scopes and environment](https://git-scm.com/docs/git-config#SCOPES).
