"""The split memory index's area names and tombstone shape (#435, #601).

A split memory scope keeps its entry lines in ``areas/<slug>.md``.
``work_repo.memory`` (which writes them) and
``lmer_schema.checks.agents`` (which validates them) both decide what an
area index is and which line is a ``forget`` tombstone, so they share this
one definition. The validator cannot import ``work_repo`` at module scope
(the import direction is ``work_repo -> lmer_schema``), and both may import
``lmer_cli``, so the definition lives here.
"""

from __future__ import annotations

import posixpath
import re

#: A split scope's area indexes live at ``<scope>/areas/<slug>.md``.
AREAS_DIR = "areas"

#: The area persist files an entry into when its memory names no usable one.
UNSORTED_AREA = "unsorted"

#: An area slug, matched against the WHOLE string (``fullmatch``): ``$`` would
#: also match before a trailing newline and let ``"dev\n"`` through.
AREA_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]*")

#: An ``_archive/`` component in an index link names the archive mirror.
ARCHIVE_MARKERS = ("_archive", "archive")

#: A markdown link on an index line: ``[text](target)``.
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")

#: A link that is not a relative path: an absolute path, a URL, an anchor.
_NOT_RELATIVE_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//|/|#)", re.IGNORECASE)


def is_area_slug(value: object) -> bool:
    """Whether *value* is a usable area slug (``[a-z0-9][a-z0-9-]*``)."""
    return isinstance(value, str) and AREA_SLUG_RE.fullmatch(value) is not None


def is_forget_tombstone(line: str) -> bool:
    """Whether *line* is an entry ``forget`` re-pointed at the archive mirror.

    ``forget`` rewrites the entry's own link, which leads its line, so the
    line's FIRST relative ``.md`` link names the ``_archive/`` mirror. Every
    writer keeps such a line verbatim (``revive`` finds it by its exact text)
    and the validator reads no entry on it (#601). A line that leads with
    anything else is no tombstone because a later link names the archive.
    """
    for target in _LINK_RE.findall(line):
        if not target.lower().endswith(".md") or _NOT_RELATIVE_RE.match(target):
            continue
        return any(part in ARCHIVE_MARKERS for part in posixpath.normpath(target).split("/"))
    return False
