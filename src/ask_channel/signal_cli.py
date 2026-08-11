"""lmer-signal console script: one line to the orchestrator, and nothing back.

Why this is not a fourth ``lmer-ask`` verb
-----------------------------------------
``lmer-ask`` is addressed to the *operator* — every one of its verbs ends in a
person reading something or typing something back, and its whole prompt fragment
teaches an agent when a human is worth interrupting. A milestone is the other
direction: "I pushed the MR", "the review is finished", "I am done with this
task" is what the *supervising assistant* needs in order to route the next step,
and waking the operator for it is exactly the noise spec §8.3 keeps out of a
context window (operator request, 2026-07-29: "a dedicated tool that just lets
lmer send a signal to the orchestrator explicitly when it pushes a PR or is done
with a review").

Two commands rather than one with a flag, because the difference is *who reads
it* and that is not a modifier — an agent choosing between "ask my operator" and
"tell the orchestrator" is making the decision this tool exists to make easy, and
a flag on the operator's tool would let it be made by accident.

The surface is one argument
---------------------------
``lmer-signal "<what happened>"``. No options, no waiting, no ids to keep: a
signal is one-way and terminal, so there is nothing to poll for and nothing to
close. The three ways to supply the text are ``lmer-ask``'s own
(:func:`ask_channel.cli._add_text_source`), because the hazard is the same one —
a body on the command line goes through the shell first, and a milestone line
saying what was pushed is exactly the text that carries backticks.

Exit codes
----------
0                        the signal is on the channel
1                        a real error (no text, an unwritable channel, a channel
                         that is full)
NO_CHANNEL_EXIT_CODE (3) this session has no channel — it was not started by the
                         orchestrator, or the mount is missing. Distinct from 1
                         for the reason ``lmer-ask`` gives it: a script can then
                         carry on in its ordinary output instead of retrying.

There is no timeout code, and there cannot be one: nothing is waited for.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from . import protocol
# The three text sources and the "no channel" code, imported rather than
# restated: an agent uses both commands in one session, and a second definition
# of ``-`` means stdin — or of what exit 3 means — is a second thing to forget to
# change. Same trade :mod:`lmer_platform.assistant` makes importing
# ``transcripts._scrub``.
from .cli import NO_CHANNEL_EXIT_CODE, _add_text_source, _resolve_text
from .protocol import AskError, ChannelUnavailable

PROG = "lmer-signal"


def create_parser() -> argparse.ArgumentParser:
    """Create and return the argument parser for lmer-signal."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Tell the lmer orchestrator that a milestone happened — an MR "
            "pushed, a review finished, the current task done. One-way: this "
            "reaches the supervising assistant, not your operator (that is "
            "lmer-ask), and nothing answers it."
        ),
    )
    parser.add_argument(
        "--dir",
        metavar="PATH",
        help=(
            f"Channel directory. Defaults to ${protocol.ASK_DIR_ENV}, which the "
            "orchestrator sets when it starts a session."
        ),
    )
    _add_text_source(parser, "signal")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Parse arguments and file the signal. Returns the exit code.

    The entry id goes to stdout alone, as ``lmer-ask note`` prints its own, so a
    caller can capture it — there is nothing to do with it here, but it is what
    ties a line in the operator's platform history to the record on the channel.
    """
    args = create_parser().parse_args(argv)

    try:
        directory = protocol.resolve_channel_dir(args.dir)
    except ChannelUnavailable as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return NO_CHANNEL_EXIT_CODE

    try:
        entry = protocol.post_signal(directory, _resolve_text(args))
    except AskError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 1
    print(entry.id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
