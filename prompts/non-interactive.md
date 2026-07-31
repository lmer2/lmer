## Non-interactive session — nobody is attached to answer

`LMER_NONINTERACTIVE` is set for this session: no human is watching it. This
is a cron run, a CI launch, a scheduler, or another headless start, and any
question you ask is delivered to nobody.

**No gate may end your turn with a question.** An unanswered prompt is not a
pause, it is a dropped result — the caller gets your near-empty output and
your work vanishes with no error surfaced.

- Approval already granted before the session started — the launch prompt,
  the taskdef instructions, `/gate-commit` — is still approval. Act on it.
  A headless run whose purpose is to produce committed work still commits,
  and still runs the gate's checks first.
- For an approval you would have to obtain *now*: do not ask for it, and do
  not perform the gated action either. Stop that line of work and state in
  your final output what you would have asked, why you stopped, and what you
  completed before stopping.
- At the CONTEXT SWITCH GATE, state the switch in your output and proceed
  rather than stopping for confirmation.

The full rules are the NON-INTERACTIVE SESSIONS section of `AGENTS.md`; this
notice restates them so they apply even where that file is not in context.
