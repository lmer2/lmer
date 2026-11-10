# spawn-harness child usage fixtures (#647 Q6)

claude and pi children keep text mode and their transcript (sections below).
codex children are stateless, so their usage is read from their own JSON
output. That is only safe where the JSON output determines the bytes the
child's text mode prints. Each fixture pairs a JSON-mode stdout with the
text-mode stdout it must reproduce. The streams are synthetic, with invented
counts, but follow the real shape. The expected text sits in `cases.json`,
because the repo's end-of-file hooks would rewrite a raw text file's trailing
newlines.

Every finding below comes from reading the installed CLIs: help output,
JavaScript source and machine code. No prompt was run.

## codex (codex-cli 0.166.0): captured

`codex exec --json --output-last-message <file>`.

- **stdout shape.** `exec --json` prints thread events, one per line:
  `thread.started`, `turn.started`, `item.started`, `item.completed` (whose
  `item.type` is `agent_message`, `reasoning`, `command_execution`, ...) and
  `turn.completed`, which carries
  `usage{input_tokens, cached_input_tokens, cache_write_input_tokens, output_tokens, reasoning_output_tokens}`.
  The serde names appear together in the binary's `.rodata`. No event carries
  the model, so the row uses the requested model, else `unknown`. A
  `turn_context` / `session_configured` model and rollout-style `token_count`
  lines are also read.
- **Text mode.** The human-output finaliser (vtable slot `0x8a536f0`) prints
  the final message to stdout only when stdout is not a TTY, which is always
  the case under spawn-harness. It prints with the format template
  `0xf6f405e`, which decodes to `{}` followed by the 1-byte literal `\n`. That
  is `println!("{}", message)`: one `\n` is always added, even after a message
  that already ends in one. With no final message nothing is printed.
- **`--output-last-message`.** The writer at `0x8a0a230` is reached from both
  finalisers, human (`0x8a536f0`) and JSONL (`0x8a53400`), under the same
  message-and-path gate. It writes the message verbatim. With no message it
  writes empty content and eprints `Warning: no last agent message; wrote empty content to …`.
- **Rule.** text = file + `\n` when the file is non-empty, else empty. A file
  that is absent and a stdout that is not JSON events means the raw stdout is
  passed through. An absent file with JSON events means the last
  `agent_message` is the answer.
- **Failures.** JSON mode reports them only as stdout events, `error` and
  `turn.failed` (`turn-failed.jsonl`, shaped like a live codex-cli 0.166.0
  run with no credentials), and writes no last-message file. Text mode prints
  each as `ERROR: <message>` on stderr, so the capture writes that line to
  stderr and into the failure footer's tail.
- **Residual gap.** A final message of exactly `""` prints `\n` in text mode
  but leaves an empty file, which is indistinguishable from "no message". We
  return empty. Both are flagged as degenerate output either way.
- **Usage.** The last cumulative snapshot. One exec runs one turn, so this is
  the same as a per-turn sum. Buckets are mapped by `lmer_usage.codex._counters`.
  `requests` counts usage events, a lower bound on API responses.

Cases (`codex/cases.json`): `answer` (a final message with no trailing
newline), `trailing-newline` (a message ending in `\n` gets a second one) and
`no-usage` (no `turn.completed`, giving the same text and a `not_collected`
row).

## claude (Claude Code 2.1.318): transcript kept

The child keeps text mode, so its stdout is unchanged by construction. Only
`--no-session-persistence` is replaced, by `--session-id <uuid>`; the uuid is
the child session id formatted as a UUID (`lmer_usage.children.transcript_id`).

JSON mode was not used. Its single `result` cannot reproduce text mode exactly:
- the printer (class `jw`) prints every result of a multi-result run, behind
  the remote gate `tengu_jazzy_puddle`;
- it prefixes "may be incomplete" results with earlier assistant text.

There is no projects-only redirect:
- the projects dir is `join(Ue(), "projects")`, and `Ue()` is
  `$CLAUDE_CONFIG_DIR` or `~/.claude`;
- `CLAUDE_CODE_PROJECT_DIR_NAME` is honoured only with `CLAUDE_CONFIG_DIR` set,
  and only names the leaf;
- `CLAUDE_CONFIG_DIR` also moves `.claude.json`, settings, hooks, the
  credential store and `projects/-workspace/memory`.

So the transcript is written under the parent's projects dir. Its child
directory, `.lmer-children/<child>/`, is created before launch, so the parent's
view recognises the file by its id while the child runs. After exit,
spawn-harness moves `<project>/<uuid>.jsonl` and `<project>/<uuid>/` into the
child directory.

`claude/` is `usage/claude/session-a`'s `main.jsonl` and its `agent-abc123`
sub-agent, with every message id, request id and uuid suffixed `-child`. A
child transcript read as the parent's would then change the parent's counts
instead of deduplicating into them. A sub-agent row is `<agent>/<sub-agent>`.

## pi (pi 1.5.0): transcript kept

The child keeps text mode. `--no-session` is replaced by
`--session-dir <session_dir>/.lmer-children/<child>/`:
- `createSessionManager` passes it to `SessionManager.create(cwd, sessionDir)`
  (`dist/main.js`);
- the session file is written there with `appendFileSync`
  (`dist/core/session-manager.js`), so it is complete when pi exits.

JSON mode (`--mode json`) was not used. Text mode prints the last message of
`session.state.messages`, a projection that retries and compaction rewrite.
JSON mode also never sets the exit code of 1 that text mode returns on an
`error`, `aborted` or `length` stop.
