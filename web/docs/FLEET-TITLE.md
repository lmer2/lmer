# Fleet state in the browser title

The title uses the same fleet response as the listing:

| State | Title |
| --- | --- |
| The fleet read failed or has not succeeded yet | ⚠ offline — lmer |
| The attention list or attention total is nonzero | ⏳ waiting — lmer |
| No attention and the live total is nonzero | ▶ working — lmer |
| No attention and no live sessions | · idle — lmer |

Waiting takes priority over working. The next successful fleet poll clears the
offline state. Unrelated action errors do not mark the daemon offline. Identical
poll results do not rewrite the title.
