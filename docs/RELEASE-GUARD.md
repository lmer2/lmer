# Release Guard — Leak Scan and Pre-Push Guard

`lmer-release-guard` is what the `release` taskdef runs before anything
reaches a project's public GitHub mirror. It has four verbs: `check-config`
validates the project's configuration, `scan` and `scan-text` find
candidate leaks for the run to triage, and `prepush` is a pass/fail guard
run immediately before every push to the mirror. Where each one runs in a
release is described in [RELEASE-FLOW.md](./RELEASE-FLOW.md) §2.

The tool ships in the public lmer repository, so it carries **generic**
detectors only. Everything project-private comes from the project tier.

## Configuration: `release.leak_scan`

The block sits under the `release:` mapping of the project's release
`task.yaml`, the same file that holds `github_target` and the other release
parameters (resolved first-match across the taskdef tiers; see
[RELEASE-ADOPTION.md](./RELEASE-ADOPTION.md) §1). Every verb takes that
file as `--config`.

```yaml
release:
  github_target: https://github.com/<owner>/<repo>
  # ... the other release parameters ...
  leak_scan:
    public_mirror: true
    exceptions: leak-scan-exceptions.md
    ancestry_denylist:
      - <full 40-hex SHA>
    patterns:
      - name: build-host
        regex: '(?i)\bbuild\.corp\.invalid\b'
    guard_strings:
      - 'project-codename'
```

| Key | Required | Meaning |
| --- | --- | --- |
| `public_mirror` | yes | Exactly `true` or `false`. `false` means the mirror is not public: the release run skips the leak scan and the pre-push guard and logs that it did. The GitHub CI candidate still runs. |
| `exceptions` | no | A file name in the project's work-repo `info/` directory listing approved occurrences. The release run reads it (through `work read-project-info`) when it triages hits; the tool does not. |
| `ancestry_denylist` | no | Full 40-hex SHAs that must never be published. `prepush` fails if any of them is an ancestor of the pushed object. |
| `patterns` | no | Extra detectors, each with exactly a `name` (lowercase letters, digits, hyphens; not a generic category name) and a `regex`. Use them for people's handles, internal hostnames and project codenames. Their hits are triaged like any other. |
| `guard_strings` | no | Regexes, matched case-insensitively, that fail `prepush` outright when they appear anywhere in what a push adds (diffs, messages, identities). |

Validation is strict: an unknown or duplicated key, a non-boolean
`public_mirror`, a list key holding anything but a list (`null` included),
a short SHA, a bad regex or a duplicate pattern name is an error naming it,
so a typo cannot silently drop a check. A manifest with no `leak_scan`
block at all is also an error (`REQUIRE_LEAK_SCAN_BLOCK` in
`src/lmer_cli/release_guard.py`): an absent block must not quietly turn
into a scan without the project's private patterns.

`lmer-release-guard check-config --config <task.yaml>` validates the block
and prints a summary that names patterns but never shows their regexes. The
release taskdef runs it in Phase 0.5; a non-zero exit is a hard stop.

## `scan`

```
lmer-release-guard scan --config <task.yaml> --base <previous tag> \
    --candidate <sha> [--published <rev>] --out <result.json> [--repo <path>]
```

It scans everything a push of `--candidate` publishes:

- **Cumulative:** every added line of `git diff <base> <candidate>`. Every
  diff the tool parses is run with fixed prefixes, rename detection and no
  textconv or external diff driver, whatever the user's git config or a
  committed `.gitattributes` says. Only a newline ends a line (a carriage
  return, form feed or Unicode separator does not). The line counts are
  checked against `git diff --numstat`: a diff the tool could not fully
  read is an error, never a quiet partial scan.
- **Binary files:** a file git counts no lines for is checked by content.
  Text that a `.gitattributes` entry marks `binary` keeps its line scan.
  A real binary file (a NUL byte, or not UTF-8) has its bytes searched for
  held values, token prefixes and shapes, project patterns and guard
  strings, and is listed under `binary` in the result. Its lines are not
  counted against `--numstat`, which reports none for it.
- **Per commit**, over `<published>..<candidate>` (`--published` defaults to
  `--base`; the release run passes the mirror's `main`):
  - each single-parent commit's own added **and removed** lines, because a
    value a later commit deletes is still in published history;
  - each merge's tree compared with `git merge-tree --write-tree` of its
    parents, scanning only the merge's own resolution when they differ;
  - root commits whole, and octopus merges against their first parent.
- **Messages and identities:** every commit message line and every
  author/committer name and email in the same range.
- **Live secrets:** the values this session holds (secret-named environment
  variables, the password half of `~/.git-credentials*` lines, and the
  repository's gitignored `LOCAL/` env files) anywhere in the above. A
  secret-named variable that holds a locator, not a credential, is not a
  held value: `LMER_GITLAB_TOKEN_HOST` and `LMER_RELEASE_SIGNING_KEY` (the
  key's container path) are the ones lmer sets.

The generic detectors cover the repository's own credential detectors
(token prefixes and shapes, URL userinfo), secret assignments in code,
`.env`, YAML and JSON, auth headers, PEM private key headers, AWS, OpenSSH, Slack, Google, JWT,
Stripe, npm, PyPI and Matrix credential shapes, email addresses, phone
numbers, generic internal domains (`.internal`, `.corp`, `.lan`,
`.intranet`, `home.arpa`), RFC 1918 addresses and `/home/<user>` or
`/Users/<user>` paths. The JSON also carries an inventory of hostname-shaped
tokens, to help spot an internal host no pattern names. Spotting one means
reading the hostnames whole, so the inventory is the one part of the output
that is not masked. A host that contains a held value or matches a project pattern or
guard string is left out, since its hit already reports it masked. The
inventory is read, never copied into `leak-scan.md`.

**Masking.** No hit's value is ever printed or written whole: a hit shows
its first three and last two characters and its length, and a live-secret
hit shows only which held value it was and its length.

**Exit codes.** `2` means a live secret or a guard string is in the range,
which is a finding by definition. `0` means the scan ran; it does not mean the range is clean.
Most hits are test fixtures, placeholder hosts and identities that are
already public, and deciding that is a judgement, not a pattern. The
release run triages every hit against the exceptions file, the history the
mirror already carries, and operator decisions recorded earlier, and writes
the result to `leak-scan.md`. `1` is an error (bad config, git failure).

## `scan-text`

```
lmer-release-guard scan-text <file> --config <task.yaml> --out <result.json>
```

The same detectors over a text file, for things that are about to become
public but are not commits, such as a code-scanning dismissal comment.
Exit codes are as for `scan`.

## `prepush`

```
lmer-release-guard prepush --config <task.yaml> --version <X.Y.Z> \
    --published <mirror main> <object> <dest-ref>
```

Run on a fresh fetch of the mirror, immediately before the push it guards.
Each check prints `PASS` or `FAIL`; the verdict line is `GUARD PASS` (exit 0)
or `GUARD FAIL` (exit 1).

- **Ref:** the destination must be `refs/heads/main`, `refs/tags/v<X.Y.Z>`
  or `refs/heads/release-candidate/v<X.Y.Z>`. Nothing else is a release
  push.
- **Ancestry:** no `ancestry_denylist` commit may be an ancestor of
  `<object>`. A denylisted commit that is absent from a full clone cannot be
  an ancestor. In a shallow clone ancestry cannot be proven, so the check
  fails.
- **Guard strings:** none may match the patch text of
  `<published>..<object>`: every commit's diff (`--text`, no textconv, and
  each merge's own changes as a remerge diff), messages and identities,
  plus the tag object itself when `<object>` is an annotated tag. The
  patch includes context and removed lines as well as added ones, which
  errs towards failing. `scan` reports guard strings too, as findings.
- **Live secrets:** none of the session's held values may appear in that
  same text.

The guard is deliberately narrower than `scan`. It does not triage; it
refuses the specific things that must never be published, on every push,
including pushes made long after the scan ran.
