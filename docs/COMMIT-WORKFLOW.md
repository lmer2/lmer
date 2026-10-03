# Routine commits within authorized work

When the user has authorized development, routine commits within that scope
proceed after the required checks through `gate-check` and `gate-commit`.
Show the results. A failed required check blocks the commit. There is no
additional blanket stop asking the user to say “commit”. An explicit command
such as `/gate-commit` still authorizes the commit it requests.

An explicit restriction such as “do not commit yet” remains binding. Commit
authorization does not authorize starting new work, expanding scope, pushing,
rewriting history, merging, deploying, destructive operations, or changing
another worker's lifecycle. Push allowlists and scoped history-rewrite
authorization still apply.

## Instruction distribution

Freshly provisioned checkouts receive the updated defaults through the existing
release workflow. Provisioning behavior stays unchanged: existing instruction
files and project/user overrides keep their priority. Active worker contexts
are not rewritten by this change.

External taskdef instructions need the corresponding instruction edits through
the existing coordinated distribution workflow. Distribute both builtin
fragments, `service-checkout.jinja2` and `commit-workflow.jinja2`, before applying
the companion instruction patch. Followup includes the lightweight checkout and
commit guidance without repeating branch setup.
