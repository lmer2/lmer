# Work repository publication and recovery

`work memory persist` and `work commit` use the same publication checks.
Separate napkin repositories use the same Git safety and push helpers. The
release-claim compare-and-swap path remains separate.

Before staging, the command refuses detached HEAD, unmerged index entries,
and an interrupted rebase, merge, cherry-pick or revert. Memory persistence
also checks before copying into the work repository. An operation found in
progress is left intact for manual recovery.

Before committing, Git checks newly staged lines for conflict markers across
the whole index, including files staged before the command. This inspects
staged content: resolving a file only in the working tree is insufficient.
The check does not impose a whitespace policy. When documenting marker
examples, quote or indent their lines instead of placing raw markers at the
start of a line.

Git preserves the index when staging fails. If subsequent status inspection or
marker validation fails, the command restores the previous index, including
partial staging. Working files remain available
for correction. An unsafe blob staged before the command remains staged and
must be corrected or unstaged explicitly. Restoration failures are reported.

The command commits locally, then pulls with rebase and pushes. A failed
pull stops publication immediately. If that pull started a rebase which
remains interrupted, the command aborts it, retaining the original local
commits. An abort failure is reported too. Pushes have at most three
attempts, with a fresh pull and rebase before each attempt.

Rebase uses autostash to preserve unrelated tracked edits, such as an uncommitted
run log. Git can return success even when restoring those edits fails. The
command compares `refs/stash` before and after each successful pull; a new or
changed stash stops publication and reports its commit ID for recovery. An
unchanged pre-existing stash does not block publication. A concurrent stash
write also causes this conservative refusal. Autostash can restore previously
staged edits as unstaged edits on a clean retry. Inspect both `git status` and
`git stash list` before resolving that situation.

A successful push exit status is not enough to report success. The command
fetches the configured upstream branch explicitly and checks that the commit
captured after rebasing is its ancestor. A later remote commit is acceptable;
a missing branch, fetch error, missing upstream, or failed ancestry check is
a failure. This confirms reachability at the fetch; it cannot prevent a later
remote rewrite.

Pull, push, and verification use the same explicit upstream remote and branch;
push-default and push-remote settings do not redirect work publication.

A retry with no new file changes still publishes existing local commits.
This includes a retry after deleting the last memory file. A clean tree with
no pending commits verifies remote ancestry without creating another commit.
An active gate still defers publication and records a deferral rather than
claiming the work was pushed.

Even an unchanged durability write fetches the remote branch before returning
success. Missing upstream configuration or unavailable network access therefore
fails a clean retry too, including a configured separate napkin repository.
These commands are online durability checks, not offline no-ops.

## Recovering a failure

Inspect the repository first:

```bash
git -C /work status
git -C /work log --oneline --decorate -5
```

For an operation already in progress on entry, resolve and continue it, or
abort it after preserving any manual conflict resolutions. For detached HEAD,
preserve any local commits and check out the intended branch. The command
does not choose a branch or discard commits on the operator's behalf.

For a conflict raised by this command and successfully aborted, the local
commit is retained on its original branch. Rebase it manually, resolve the
reported files, stage those specific paths, and continue the rebase. Then
retry `work commit` or `work memory persist` to publish and verify it.

An autostash restoration failure is different from an aborted rebase: the
rebase has finished, and the saved edits remain in the reported stash. Inspect
`git -C /work stash show -p <reported-commit>` and `git -C /work status` first.
If application conflicted, Git leaves conflict markers in working files and
unmerged index entries. These block later `work` publication, including state,
goal, artifact, ledger, and session-end writes, until resolved. Preserve the
stash while resolving those files and stage only the resolved paths. Do not
apply the stash again over the unresolved index.

If application failed without conflicts (for example, a concurrent index
writer held Git's lock), the working tree may contain only the rebased files;
the uncommitted edits are still in the stash. Once the other Git operation has
finished and the index is ready, use `git -C /work stash apply <reported-commit>`
and resolve any conflicts. Verify the recovered edits before removing the
stash entry. Then retry publication. The command does not reset conflicted
files or discard the saved stash automatically.

For a transport failure, repair access and retry the command. No new edit or
empty commit is needed to retry an existing local commit.

When recovering memory conflicts, inspect both `MEMORY.md` and the memory
files, including deletions and content changes. These Git guards preserve
the existing restore-manifest merge behavior; they do not implement a new
memory merge or automatic conflict-resolution policy.

Deploying these checks requires rebuilding and rolling the session container
image. Existing containers retain their baked `work` implementation. Host-side
users must update their installed package as well.
