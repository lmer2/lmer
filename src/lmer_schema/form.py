"""Form detection — schema form or legacy form, and nothing else (spec D20).

Dual-read ships in this run: ONE lmer reads a migrated plexus and a legacy
tree, so every consumer that routes a path (the work CLI's ``_runs``/``_specs``
/``_agents/memory`` routing, the validator's severity tier, the hooks-adjacent
resolvers) begins by asking which form the tree it is standing in is written
in. D20 answers with one bit of evidence:

    a plexus root holding ``_plexus.yaml`` is SCHEMA form; one without it is
    LEGACY form.

Two answers, spelled as an enum
===============================
:class:`Form` has exactly two members and no "unknown": consumers branch on
``form is Form.SCHEMA``, never on the truthiness of a path or a manifest
object. ``None`` already means something else in this package (a legacy tree's
absent :class:`~lmer_schema.manifest.Manifest`, a resolution MISS), and a
third "undetermined" state would have to be handled by every caller to say
the same thing legacy already says: no manifest, read the bare names.

The enum deliberately mirrors ``lmer_schema.checks.findings.Form`` — same
class name, same two values — rather than importing it. ``findings.Form.coerce``
recognises this vocabulary structurally, so the validator takes detection's
answer directly while the core keeps no dependency on the validator's
internals (and the validator none on the core's), which is what lets either
side be imported alone.

Detection is presence-only
==========================
This module opens nothing and parses nothing. A manifest that is present but
BROKEN — unparseable YAML, a bool ``schema:``, an unreadable file, a dangling
symlink, a directory in its place — is still schema form, and
:func:`~lmer_schema.manifest.load_manifest` is the thing that fails (loudly,
with :class:`~lmer_schema.manifest.ManifestError`). That asymmetry is the
whole point of D20: if a typo demoted a tree to legacy, the same lmer would
quietly start writing at the legacy paths of a migrated plexus and split the
tree in two. Detection is therefore total — it never raises for a tree —
and the presence test here is the SAME test ``load_manifest`` uses for its
one silent outcome (``is_symlink() or exists()``), so the two can never
disagree about which trees are legacy. ``tests/test_lmer_schema_form.py``
pins both the agreement and the "no YAML parser here" guard on the module
source.

Finding the root
================
:func:`detect_form` answers about a root the caller already knows (the work
CLI always does — ``LMER_WORK_REPO_PATH`` and friends). Tooling started
somewhere INSIDE a tree instead asks :func:`find_plexus_root`, which climbs
to the nearest ancestor carrying a manifest, git-style. The walk touches only
the manifest name, needs nothing on disk to exist yet (a session resolves its
dir before creating it), and takes a file path as its directory's.

Only the SCHEMA form is self-describing. A legacy tree has no marker at all —
bare ``runs/``, ``agent-files/`` and a standalone ``sources.yaml`` are the
shapes of any number of directories inside a tree, not evidence of its root —
so the walk finds nothing there and :func:`detect` reports LEGACY at the root
the caller passed, or ``None`` when it passed none. No root is ever invented,
for the same reason D9 invents no project name: a guess that lands work in
the wrong place is worse than an admitted "you tell me".

``stop=``/``root=`` bound the walk to the tree the caller is asking about, so
a plexus ABOVE it (a checkout inside a plexus, a fixture tree inside a
developer's own) can never answer for it. A boundary that is not an ancestor
of the start is a caller bug and raises rather than silently letting the walk
run to the filesystem root.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from lmer_schema.manifest import manifest_path

__all__ = [
    "Detection",
    "Form",
    "detect",
    "detect_form",
    "find_plexus_root",
    "has_manifest",
]


class Form(Enum):
    """The form a plexus tree is written in (D20) — the only two answers.

    ``SCHEMA``: a ``_plexus.yaml`` is present at the root, so the tree uses
    the one form (§2, ``_``-owned area names, flat short-name project dirs).
    ``LEGACY``: no manifest, so the tree uses the bare pre-schema names and
    the ``{host}/{project}`` placement (§9), read in place and validated at
    the lenient tier (D14).

    The values are the strings the JSON report and the fixtures use, and the
    class name and value set are the shared vocabulary
    ``lmer_schema.checks.findings.Form.coerce`` accepts (module docstring).
    """

    SCHEMA = "schema"
    LEGACY = "legacy"


@dataclass(frozen=True)
class Detection:
    """What detection found: the ``form``, the ``root`` it is about, and the
    ``manifest`` path in schema form.

    ``manifest`` says where the manifest IS, never what it says — it is
    populated for a broken manifest exactly as for a good one, and reading it
    is :func:`~lmer_schema.manifest.load_manifest`'s job. ``root`` is ``None``
    only in the legacy case where the caller supplied no root: nothing on disk
    marks a legacy root and detection invents none.
    """

    form: Form
    root: Optional[Path]
    manifest: Optional[Path]


def has_manifest(root) -> bool:
    """Whether a ``_plexus.yaml`` is PRESENT at *root* (D20's one bit).

    ``is_symlink`` first, exactly as ``load_manifest`` tests it: a dangling
    symlink is present — someone put a manifest there — even though
    ``exists`` follows it and says no. Presence only; nothing is opened, so a
    broken or unreadable manifest still counts.
    """
    path = manifest_path(root)
    return path.is_symlink() or path.exists()


def detect_form(root) -> Form:
    """The form of the plexus rooted at *root* (D20).

    Total: never raises for a tree, whatever state its manifest is in. *root*
    need not exist — a path with no manifest at it is legacy form.
    """
    return Form.SCHEMA if has_manifest(root) else Form.LEGACY


def find_plexus_root(start, *, stop=None) -> Optional[Path]:
    """The nearest ancestor of *start* (inclusive) holding a manifest, or
    ``None``.

    The upward search for a SCHEMA-form root, for tooling that knows only
    where it is standing. *start* is taken as a directory; a path to a file
    (``state.yaml``) searches from its directory. Nothing needs to exist.
    The nearest manifest wins: a manifest nested inside another tree is a
    broken tree for the validator to report, not a form question.

    *stop* bounds the walk INCLUSIVELY — the tree the caller is asking about,
    so a plexus above it cannot answer for it. It must be an ancestor of
    *start* (or *start* itself); anything else raises :class:`ValueError`
    rather than letting the walk escape to the filesystem root.

    ``None`` is not "legacy" by itself — it is "no schema-form root found in
    this chain". :func:`detect` is the composed answer.
    """
    current = _search_dir(start)
    chain = (current, *current.parents)
    floor = None
    if stop is not None:
        floor = _abs(stop)
        if floor not in chain:
            raise ValueError(
                f"stop={floor} is not {current} nor one of its parents; the "
                "search boundary must be an ancestor of the starting path."
            )
    for candidate in chain:
        if has_manifest(candidate):
            return candidate
        if candidate == floor:
            return None
    return None


def detect(start, *, root=None) -> Detection:
    """Detect the form of the tree *start* is in (D20).

    Climbs from *start* to the nearest ancestor holding a manifest
    (:func:`find_plexus_root`) and reports :class:`Detection`. *root* is the
    plexus root the caller already knows: it bounds the walk (a manifest
    above it belongs to another tree) and it is the ``root`` reported when no
    manifest is found. Without it, a legacy answer carries ``root=None`` —
    detection never invents a root.
    """
    found = find_plexus_root(start, stop=root)
    if found is not None:
        return Detection(form=Form.SCHEMA, root=found, manifest=manifest_path(found))
    return Detection(
        form=Form.LEGACY,
        root=_abs(root) if root is not None else None,
        manifest=None,
    )


def _abs(path) -> Path:
    """*path* made absolute and lexically normalised, WITHOUT resolving
    symlinks: the walk compares path components, and resolving would rewrite
    a caller's spelling of its own tree (a symlinked work repo) into another
    one it never named."""
    return Path(os.path.abspath(path))


def _search_dir(start) -> Path:
    """Where the upward walk begins: *start* itself, or its parent when it
    names an existing file."""
    start = _abs(start)
    return start.parent if start.is_file() else start
