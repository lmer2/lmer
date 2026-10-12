# Ratchet's faces

chadd's final Ratchet icons (2026-10-12, from the Google Drive art folder).
All ten are 128 x 128 px, 8-bit RGBA PNGs and are used **exactly as delivered** —
do not resize or recompress them here. The UI downscales them in CSS.

Files are named for the fleet state, so replacing one is overwriting a file;
`web/src/ratchet.js` maps each mood to its file in one table (`MOODS`,
`faceFile`). `loading` shares `ratchet_talking.png`: before the first poll there
is nothing to be alarmed about, so Ratchet is just "talking".

| file | state |
|---|---|
| ratchet_question.png | a question is waiting for the operator |
| ratchet_oops.png | a run crashed or got stuck |
| ratchet_panic.png | something genuinely serious |
| ratchet_strained.png | the fleet is near its capacity limit |
| ratchet_on_it.png | work is running normally |
| ratchet_skeptical.png | a run is waiting on a review |
| ratchet_idle.png | everything is quiet |
| ratchet_yes.png | something just finished or merged |
| ratchet_talking.png | Ratchet is replying in chat (also the pre-poll state) |
| ratchet_off.png | Ratchet is switched off |

Two rules from chadd, kept in the code:

- Every face is paired with a plain-text status label — a face alone is
  ambiguous, so the mood reason is always available beside it (tooltip and
  accessible name).
- `panic` is only for something genuinely serious, never a routine retry or a
  single crash. The rule: `panic` fires only on an attention item whose reason
  is `critical_error`. Ordinary crashes and stalls stay `oops`, however many
  there are.
