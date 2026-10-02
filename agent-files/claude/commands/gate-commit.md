---
description: Run gate checks and commit if all pass
argument-hint: [commit message]
---

IMPORTANT: This command is explicit approval to commit. Routine commits within
already-authorized work may also proceed through the required gates without a
separate confirmation. Respect explicit user holds and task-specific restrictions;
failing checks block. Commit authorization does not grant push, history rewrite,
merge, deploy, destructive-operation or worker-lifecycle authorization.
- If a message is provided in $ARGUMENTS, use it
- If no message provided, generate an appropriate commit message based on staged changes
- Follow all git rules: use gate commands, show output, etc.
- This IS the user's approval to commit - proceed with the commit

Run gate checks and commit:

!git diff --cached --stat
!git diff --cached
!gate-commit -m "${ARGUMENTS:-$(generate appropriate commit message based on the changes)}"
