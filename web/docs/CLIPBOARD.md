# Copying messages and code

Use the copy icon at the upper right of a message to copy its source text,
including Markdown formatting. Operator-channel questions and answers have
separate controls. A code block has its own copy icon; it copies the code without
the fence or language tag. The control briefly shows “Copied” after the browser
accepts the copy. A refused or unavailable clipboard shows “Copy failed”.

Copying uses the browser clipboard API. It normally requires HTTPS or localhost
and permission from the browser. A message trimmed by the messages API copies the
text that reached the pane; it cannot recover the omitted part.
