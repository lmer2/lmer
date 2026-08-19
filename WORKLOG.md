# Worklog

## 2026-08-19 — Issue #314 bounded ready slice

- Made GitLab and GitHub post-review wrappers report their own successful milestones.
- Followed explicit `reslugged_from` history in platform run resolution, answer, and resume.
- Removed the manual supervision restart test's registry/state persistence race.
- Added a content-checked gate-cache handoff from pre-commit staged state to the clean post-commit tree, with structured cache verdicts in receipts.
- Kept live-probe and product-decision items deferred on #314 for a later slice.

## 2026-08-19 — MR !230 review iteration 1

- Made post-review milestone delivery best-effort so a posted review always
  returns the reviewer command's successful status.
- Recorded a passing test result before the optional commit-handoff index probe,
  and limited that mutating Git probe to cache-enabled `gate-commit` runs.
- Indexed live reslug successors once per mirror revision and reused one shared
  `reslugged_from` predicate in platform and work-repo consumers.
- Kept the pre-existing tracked-key/adoption split deferred to #263 because it
  needs a product/data-model decision rather than an implicit alias change.

## 2026-08-19 — issue #313: Codex harness improvements

- Reproduced the ambient `LMER_HARNESS=codex` supervisor-default failure and
  established the untouched baseline with `env -u LMER_HARNESS gate-check`.
- Added an image-managed Codex Stop hook that turns answered `lmer-ask`
  questions into native continuation turns without exposing answer text.
- Added Codex bracketed-paste submission framing and control-plane `/followup`
  translation while preserving original-payload delivery receipts.
- Isolated the supervisor test module from ambient lmer session state and added
  hook, image, PTY, FastAPI, and harness-regression coverage.
- Review iteration 1 replaced the removed `/prompts:followup` mechanism with a
  plain-text hook instruction, made the ask guard select any oldest unread
  answer, pinned Codex hooks on, removed duplicate unreleased changelog edits,
  and hardened bracketed paste against an embedded terminator.
- Review iteration 2 rebased onto `prep-release` while retaining both worklog
  sections, corrected the last two live Codex slash-command claims, and pinned
  the wind-down verb through Codex's framed supervisor submit path.
