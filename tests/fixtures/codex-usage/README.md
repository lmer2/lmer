# Codex 0.154.0 representative telemetry

Shapes captured on 2026-09-14 from the installed Codex app server's
`account/rateLimits/read` and the active primary thread's rollout `token_count`
records. All counts, timestamps, bucket labels, and ordinals here are synthetic;
account identifiers, credentials, plan/credit details, and conversation text
have been omitted. The installed schema was generated with
`codex app-server generate-json-schema --experimental --out <scratch-directory>`.

The main bucket deliberately has only a 7-day primary window. Another bucket
has a genuine 0% 5-hour window and a 7-day secondary window. Primary/secondary
are not duration labels, and absence is not zero. Cached/reasoning breakdowns
are included in input/output totals, not additional independent token counts.
