"""The symlink rule the schema states once and two check modules apply
(SCHEMA.md "Inventory": the ``_specs/``/``_plans/`` index links and the
``<session-dir>/<artifact>.md -> masterplan/<artifact>.md`` links).

Wherever this schema puts a symlink it asks the same three questions, in one
order, and takes the FIRST answer:

1. is the target **relative**? an absolute target breaks on every other
   clone of the work repo;
2. does it stay **inside the plexus**? an escaping target addresses
   something this tree does not own;
3. does it **resolve**?

At most one fault per link, and the order is part of the rule: an escaping
target is never also reported dangling, because "it points outside the tree"
is the whole answer and the resolution question is meaningless past it.

Where it lives, and why
=======================
Beside :mod:`lmer_schema.checks.findings` and :mod:`lmer_schema.checks.walk`,
as a SUPPORT module of the checks package rather than inside one of its two
callers. Two check modules need this rule: ``index_links`` for the dated
index links (§3.2, D11) and ``record`` for the session-dir artifact links
(SCHEMA.md "Session directory"). A check module never imports a check module
— the runner is the only file that names them, and its tests pin that — so
leaving the rule in ``index_links`` would have made ``record`` break the
invariant, and copying it into ``record`` would be the !256 discussion-5
failure one level down: two readings of one rule, drifting, with only the
tested copy watched.

The line that keeps this a support module is the same one ``walk`` draws: it
registers no code, emits no finding and never touches a
:class:`~lmer_schema.checks.findings.Report`. It answers a question about one
path; what the answer MEANS — which code, at which severity, with which
message — stays with the check module that owns the surface. That is why the
fault kinds here are plain descriptive names and not check codes: the index
mechanic and the session-dir row happen to share three codes today (D14
registers them once), and this module is deliberately not the place that
decides so.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

__all__ = [
    "DANGLING",
    "ESCAPES",
    "KINDS",
    "NOT_RELATIVE",
    "LinkFault",
    "link_fault",
]

#: The three faults, named for the condition rather than for any check code.
NOT_RELATIVE = "not-relative"
ESCAPES = "escapes"
DANGLING = "dangling"

#: In the order the questions are asked — which is the order a link is
#: judged in, and the reason only the first one is ever reported.
KINDS: Tuple[str, ...] = (NOT_RELATIVE, ESCAPES, DANGLING)


@dataclass(frozen=True)
class LinkFault:
    """What is wrong with one symlink: ``kind`` (one of :data:`KINDS`) and
    ``target`` exactly as ``readlink`` returned it — the caller's finding
    detail, unnormalised, because the spelling IS what is wrong."""

    kind: str
    target: str


def link_fault(root, link) -> Optional[LinkFault]:
    """The one thing wrong with *link* under the plexus at *root*, or
    ``None`` when there is nothing wrong with it.

    ``None`` also when *link* is not a symlink at all: a regular file (or a
    directory) at a name this schema may link is free-form content, and
    content is not this rule's business. Callers may therefore ask about a
    well-known name without first checking what is there.
    """
    link = Path(link)
    if not link.is_symlink():
        return None
    root = Path(root)
    target = os.readlink(link)
    if os.path.isabs(target):
        return LinkFault(NOT_RELATIVE, target)
    if _escapes(root, link, target):
        return LinkFault(ESCAPES, target)
    if not link.exists():
        return LinkFault(DANGLING, target)
    return None


def _escapes(root: Path, link: Path, target: str) -> bool:
    """Whether *target*, read from *link*, addresses something outside *root*.

    Lexically first — these links are written in relative paths and a leading
    ``..`` hop past the root is the common breakage — then through
    ``realpath``, which catches an escape by way of a symlinked intermediate
    directory. A dangling target still answers, since ``realpath`` resolves
    what exists and appends the rest.
    """
    lexical = os.path.normpath(os.path.join(_rel(root, link.parent), target))
    if lexical == ".." or lexical.startswith(".." + os.sep):
        return True
    real_root = os.path.realpath(root)
    real_target = os.path.realpath(os.path.join(link.parent, target))
    return os.path.commonpath([real_root, real_target]) != real_root


def _rel(root: Path, path: Path) -> str:
    """*path* relative to *root*, in the platform's own spelling (this is
    joined with a target, never reported)."""
    return os.path.relpath(os.fspath(path), os.fspath(root))
