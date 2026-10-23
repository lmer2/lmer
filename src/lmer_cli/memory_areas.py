"""The split memory index's area names (issue #435).

A split memory scope keeps its entry lines in ``areas/<slug>.md``.
``work_repo.memory`` (which writes them) and
``lmer_schema.checks.agents`` (which validates them) both decide what an
area index is, so they share this one definition. The validator cannot
import ``work_repo`` at module scope (the import direction is
``work_repo -> lmer_schema``), and both may import ``lmer_cli``, so the
definition lives here.
"""

from __future__ import annotations

import re

#: A split scope's area indexes live at ``<scope>/areas/<slug>.md``.
AREAS_DIR = "areas"

#: The area persist files an entry into when its memory names no usable one.
UNSORTED_AREA = "unsorted"

#: An area slug, matched against the WHOLE string (``fullmatch``): ``$`` would
#: also match before a trailing newline and let ``"dev\n"`` through.
AREA_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]*")


def is_area_slug(value: object) -> bool:
    """Whether *value* is a usable area slug (``[a-z0-9][a-z0-9-]*``)."""
    return isinstance(value, str) and AREA_SLUG_RE.fullmatch(value) is not None
