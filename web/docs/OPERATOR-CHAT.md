# Operator chat display

In **Ratchet settings**, set **Operator chat: keep last** to a positive whole
number to show only the newest N operator-chat entries. Set it to **0** to turn
auto-clear off and show all entries supplied by the channel. An entry includes
its question or note and any attached answer. The default is off.

Saving applies to open panes on this page immediately. Reopening a run or returning
to the browser page reloads the setting. Older entries are hidden only in the pane:
the pending-question dock, bus journal, and transcript are unchanged. Turning the
setting off restores the older entries.

The operator-chat tab counts the entries retained for display. Its dot indicates retained
entries received since the tab was last viewed, including a new answer to an old
question. Viewing the tab clears the dot; a hidden browser page does not count as
viewing. Read state is kept per session for this page's lifetime. A reload starts
with unseen history unread again.

The existing `POST /api/assistant/config` accepts `chat_keep_last` as an integer;
`0` disables retention and `null` removes the stored override. It persists in
`config.json`. Both GET and POST report `chat.keep_last` with `value`, `stored`,
and `source`. This display setting requires no session restart.
