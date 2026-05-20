---
description: Run gate checks and commit if all pass
argument-hint: [commit message]
---

IMPORTANT: This command is explicit approval to commit.
- If a message is provided in $ARGUMENTS, use it
- If no message provided, generate an appropriate commit message based on staged changes
- Follow all git rules: use gate commands, show output, etc.
- This IS the user's approval to commit - proceed with the commit

Run gate checks and commit:

!git diff --cached --stat
!git diff --cached
!gate-commit -m "${ARGUMENTS:-$(generate appropriate commit message based on the changes)}"
