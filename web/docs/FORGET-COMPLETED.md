# Forget all completed runs

Use **Forget all completed** in the run listing to stop tracking every completed
run that has no live session. Failed and active runs are excluded. Search does not
limit the selection; the confirmation states this before sending any request.

The browser sends the selected runs one at a time through the existing
`POST /api/runs/forget` route. Runs that arrive after the dialog opens are not
added to that selection. Each run must still be completed and stopped before its
request is sent. Runs with an individual forget already pending are excluded.

Successful requests remove their rows. Failures remain in the listing with a
message so they can be retried. Leaving the listing stops further requests in
that batch. Forgetting cannot be undone from the UI; it preserves run files and
work-repo state.
