# codex usage fixtures

Synthetic rollouts that follow codex-cli's `token_count` shape (0.166.0). The
counts are invented, and no conversation text is included.

- `rollout-main.jsonl`: a repeated snapshot, an empty `info`, a model switch,
  and a cumulative total that resets lower.
- `rollout-fork.jsonl`: a rollout whose `session_meta` names the thread it
  forked from, so its first snapshot carries the parent's total.
- `rollout-copied.jsonl`: the main thread's history copied into another file of
  the same session.

**Assumption, unconfirmed.** `cache_write_input_tokens` is a subset of
`input_tokens`, in the same way `cached_input_tokens` is. Neither codex's
generated app-server schema nor the earlier capture
(`tests/fixtures/codex-usage/`) describes the field, and every captured sample
was 0. The adapter floors uncached input at 0, so a wrong guess under-counts
`input` and never double-counts it. Replace this note once a non-zero sample is
captured.
