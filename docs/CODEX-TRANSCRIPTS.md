# Codex wrapper rows

The chat view collapses empty Codex `exec`/`functions.exec` wrapper-only rows into
an expandable details group. It keeps prose, meaningful results, failures and
unrelated tools visible. A mixed turn remains a normal conversation turn.

The native transcript adapter marks only the known freeform wrapper shape. Its
input and output remain inspectable as escaped text, scrubbed by the existing
credential scrubber before being bounded to the normal transcript text limit.
The standard successful wrapper envelope alone is not a result. Other output is
kept as a bounded tool detail with raw details available on expansion. Explicit
wrapper failures remain errors. Arbitrary JavaScript is never evaluated or
misrepresented as a shell command.

Other harness tools and unknown Codex variants keep their existing rendering.
