"""lmer-ask console script — a compatibility shim over ``lmer-signal`` (issue #407).

For one release ``lmer-ask ask|note|wait|close|list`` keeps working exactly as
documented, forwarding to the same verbs of ``lmer-signal`` with the same
stdout, the same exit codes (0, 1, ``TIMEOUT_EXIT_CODE`` 2,
``NO_CHANNEL_EXIT_CODE`` 3) and one deprecation line on **stderr**, so a
script capturing stdout sees no difference. The old ``--dir`` flag, which
named the ask-channel directory, is accepted and ignored: the channel is the
supervisor's bus now, and the directory is a projection of it the supervisor
maintains for older readers.

Removed the release after; the prompt fragments already say ``lmer-signal``.
"""
from __future__ import annotations

import sys
from typing import List, Optional

from . import signal_cli
from .args import (  # noqa: F401 — the names other modules imported from here
    DEFAULT_INTERVAL, DEFAULT_TIMEOUT, HARNESS_COMMAND_TIMEOUT_FLOOR,
    NO_CHANNEL_EXIT_CODE, TIMEOUT_EXIT_CODE, add_text_source as _add_text_source,
    resolve_text as _resolve_text,
)

PROG = "lmer-ask"

DEPRECATION = (
    "lmer-ask: deprecated — use `lmer-signal <verb>`; this shim forwards to it "
    "and goes away next release"
)

#: The verbs the shim forwards. ``lmer-ask`` never had ``post`` or ``milestone``.
VERBS = ("ask", "note", "wait", "close", "list")


def create_parser():
    """The forwarded command's parser, for ``--help`` and for tests that pin the
    verbs."""
    return signal_cli.create_parser()


def translate_argv(argv: List[str]) -> List[str]:
    """``lmer-ask [--dir PATH] <verb> …`` → ``<verb> …`` for ``lmer-signal``."""
    out: List[str] = []
    skip = False
    for index, token in enumerate(argv):
        if skip:
            skip = False
            continue
        if token == "--dir":
            skip = True
            continue
        if token.startswith("--dir="):
            continue
        out.extend(argv[index:])
        break
    return out


def main(argv: Optional[List[str]] = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    forwarded = translate_argv(raw)
    if not forwarded:
        print(f"usage: {PROG} {{{','.join(VERBS)}}} …", file=sys.stderr)
        return 1
    if forwarded[0] not in VERBS and forwarded[0] not in ("-h", "--help"):
        print(f"{PROG}: unknown verb {forwarded[0]!r} (one of {', '.join(VERBS)})", file=sys.stderr)
        return 1
    print(DEPRECATION, file=sys.stderr)
    return signal_cli.main(forwarded)


if __name__ == "__main__":
    sys.exit(main())
