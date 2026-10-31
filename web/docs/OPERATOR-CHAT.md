# Operator channel dock and operator chat

## Dock retention

The **operator channel** dock above every run shows what needs you now. In
**Platform settings → Operator channel dock**, set **Operator channel dock: keep
last N notes** to choose how many notes it keeps:

- empty (the default): every note is shown;
- a positive whole number N: the newest N notes are shown;
- **0**: no notes, so the dock shows open questions only.

Open questions are always shown, whatever the setting. Answered questions count as
notes and leave the dock with the other notes once they are past the cutoff. A
question the session closed counts as a note too. One setting applies to the dock on
every run; it is not set per run.

Saving applies to the docks on this page immediately. Other browsers pick it up when
they next open a run or return to the page. **clear from here** still works on top
of the setting: it hides what the dock currently shows for this page visit, and
older notes past the cutoff stay hidden.

Nothing is deleted. The dock setting hides entries only in the dock: the operator
chat tab, the bus journal and the transcript are unchanged.

## Operator chat tab

The lmer tab's **operator chat** pane is the run's full channel history: every
note and question, with your replies, oldest first. No setting prunes it.

The tab counts every entry in that history. Its dot indicates entries received
since the tab was last viewed, including a new answer to an old question. Viewing
the tab clears the dot; a hidden browser page does not count as viewing. Read state
is kept per session for this page's lifetime. A reload starts with unseen history
unread again.

## API

`POST /api/settings` accepts `dock_keep_last_notes` as a whole number of 0 or
more; `null` (or an empty string) removes the stored value, which shows every note.
Anything else is refused with a 400 that names the field. The value persists in
`config.json`. `GET /api/settings` and `GET /api/assistant/config` both report
`dock.keep_last_notes` with `value`, `stored` and `source`. The setting needs no
session restart.

`chat_keep_last`, which pruned the operator chat tab, is retired. It is not
migrated, because its `0` meant "show everything" while the dock's `0` means "no
notes". An old `config.json` that still holds it loads normally and the value is
ignored. A write that names it is refused.
