"""``_plexus.yaml`` — the plexus manifest, parsed into a typed model (spec §4).

The manifest is the one file whose presence makes a tree schema form (D20):
``schema:`` int, instance ``name``, the ``projects`` registry, area
``sources`` (the folded-in ``sources.yaml``, D4) and the ``housekeeping``
policy (§8). This module reads that envelope and nothing more: the registry's
identity rules (primary vs additional remotes, normalization, repoless
entries — D8/D19) are ``lmer_schema.registry``'s, and the sources trust rules
stay with ``lmer_cli.container.sources``. The three sections are therefore
kept RAW — plain ``dict``s exactly as written — so no consumer has to
reconstruct what the file said.

Error taxonomy
==============
Two outcomes, deliberately asymmetric:

- **No manifest** — ``load_manifest`` returns ``None``. That is the
  legacy-form signal form detection consumes; it is the ONLY silent outcome
  and needs nothing but the file being absent.
- **Present but broken** — ``ManifestError``, always. Unreadable file
  (including a dangling symlink or a directory in its place), unparseable
  YAML, a non-mapping document, a missing/non-int/bool ``schema:``, an
  unsupported ``schema:`` int, a missing or non-string ``name:``, a section
  or section entry that is not a mapping, a ``projects:`` key that is not a
  short name, a ``repo:``/``remotes:`` value of the wrong type (next
  section). D20 forbids a broken manifest
  demoting a schema-form plexus to legacy — a typo must fail loud, not route
  work to the legacy paths — which is the same rule ``load_sources`` applies
  to a present ``sources.yaml``. ``ManifestError`` is this package's own
  type rather than a reuse of ``SourcesConfigError``: that class belongs to
  ``lmer_cli.container`` (with its dual-import identity caveat), and the only
  ``lmer_schema`` -> ``lmer_cli`` dependency is the URL-normalization adapter.

Recoverable findings never raise: unknown top-level keys and unknown keys
inside any section are forward-compat WARNINGS (schema plasticity — the
manifest vocabulary may grow under one ``schema:`` int) returned on
``Manifest.warnings`` in document order, and the raw sections still carry
the unknown keys.

Registry keys and values
========================
A ``projects:`` key is a short name and becomes a directory name, so this
loader is THE one charset validation (§1: ``SHORT_NAME_RE``,
``[a-z0-9][a-z0-9_-]*`` — lowercase, no dots, no slashes, no leading ``_`` or
``-``). A key outside it (``../../escape``, ``/tmp/abs``, ``Lmer``) is a
``ManifestError`` naming the key, fail-closed like the unquoted-``123:``
error. Consumers MAY rely on it: every key of ``Manifest.projects`` is a
short name and safe as a single path segment; nothing downstream needs to
re-derive the rule before joining a key onto a path.

The two documented remote-bearing values are TYPE-checked here, for every
document that reaches a consumer through this loader: ``repo:`` is a
non-empty string, ``remotes:`` a list of non-empty strings (``null`` for
either reads as absent). ``lmer_schema.registry`` re-checks the same two
types and the key charset at its own API boundary, because :class:`Manifest`
is a public dataclass a caller can build by hand and ``build_registry`` must
not splat on one; both raise :class:`ManifestError` (``RegistryError`` is a
subclass), so a consumer sees one taxonomy either way, and for a document
read from disk this loader is the layer that fires. Without this ``remotes:
origin`` — a remote NAME — would be iterated as a string and hand the URL
adapter six one-character keys, and ``repo: 12345`` would surface as an
``AttributeError`` past the one documented exception type. Only the types
are checked; the registry's semantics (primary vs additional, ``remotes:``
without ``repo:``, duplicates, D19 repoless entries, credential and
normalization rules — D8/D19) stay with ``lmer_schema.registry``.

``schema:`` handling
====================
Booleans are rejected BEFORE the int check: ``schema: true`` is a YAML bool
and ``isinstance(True, int)`` would otherwise let it sail through as schema 1
(the trap ``hooks/start.py::read_taskdef_schema`` and ``load_sources`` both
guard against). ``SUPPORTED_SCHEMAS`` is the tuple of ints this lmer
understands — exactly ``(1,)`` at v1 — and ``CURRENT_SCHEMA`` is the newest, the
int a tool writes. A NEWER int is refused with an "upgrade lmer" message (§8
version floor: a newer lmer wrote the tree; the shape is not guessable). An
int older than the floor is refused as unsupported; once a schema 2 exists,
schema 1 stays in ``SUPPORTED_SCHEMAS`` and loads normally (spec §4: older
int → tools read and write, validator WARN).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml

#: The manifest filename at the plexus root (§2).
MANIFEST_NAME = "_plexus.yaml"

#: Schema versions this loader understands. A present manifest declaring
#: anything else fails loud (see the module docstring).
SUPPORTED_SCHEMAS = (1,)

#: The newest supported schema — the int a tool writes into a new manifest.
CURRENT_SCHEMA = max(SUPPORTED_SCHEMAS)

#: The §4 / SCHEMA.md top-level vocabulary. ``schema`` and ``name`` are
#: required; the three sections are optional mappings.
TOP_LEVEL_KEYS = ("schema", "name", "projects", "sources", "housekeeping")

#: Documented keys of a ``projects:`` entry (SCHEMA.md registry table). The
#: loader WARNs on unknowns and type-checks ``repo``/``remotes`` (module
#: docstring, "Registry keys and values"); their semantics are the registry's.
PROJECT_ENTRY_KEYS = ("repo", "remotes", "description")

#: The §1 short-name charset (SCHEMA.md "Short names MUST match"). Applied to
#: every ``projects:`` key by this loader — the ONE place it is checked.
SHORT_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")

#: ``sources:`` names this lmer mounts today, mirroring the
#: ``lmer_cli.container.sources`` vocabulary (SCHEMA.md sources table).
SOURCE_NAMES = ("taskdef", "napkin")

#: ``sources:`` names the schema documents as reserved — "mountable the same
#: way when needed" — warned about by name so the operator learns the key is
#: known-but-unsupported, not a typo.
RESERVED_SOURCE_NAMES = ("knowledge", "plans")

#: Documented keys of a ``sources.<name>`` entry, PER NAME — the vocabulary
#: is not uniform. SCHEMA.md's sources table gives ``taskdef`` ``{repo, ref?}``
#: and every other name ``{repo}``, and ``load_sources`` enforces exactly that
#: asymmetry on a standalone ``sources.yaml`` ("`ref:` is valid under
#: `sources.taskdef` only in schema 1 (there is no LMER_NAPKIN_REF)") — a
#: refuse-start error, not a warning. A uniform set here would accept
#: ``sources.napkin.ref`` in a manifest, where nothing would ever act on it:
#: the manifest's ``sources:`` tier is declared-only until its loader lands
#: (issue #370), so the ref takes effect nowhere and no tool says a word.
#: The day that loader arrives, every manifest already carrying one becomes a
#: tree that refuses to start — which is why the key set is per-name NOW,
#: while the warning still costs an operator one edit.
#:
#: Keyed by name rather than special-cased at the call site: which names take
#: ``ref`` is a schema fact, and a second place that knows it is a second
#: place to drift from the table.
SOURCE_ENTRY_KEYS_BY_NAME = {
    "taskdef": ("repo", "ref"),
    "napkin": ("repo",),
    "knowledge": ("repo",),
    "plans": ("repo",),
}

#: The keys of a ``sources:`` name this schema does not document at all. Such
#: a name is already warned about BY NAME and mounts nothing, so its entry is
#: read at the widest documented vocabulary: the finding is the name.
UNKNOWN_SOURCE_ENTRY_KEYS = ("repo", "ref")

#: Documented keys of a ``housekeeping.<area>`` policy. Area names are NOT
#: vetted here: SCHEMA.md leaves the area set to the housekeeping tool.
HOUSEKEEPING_POLICY_KEYS = ("archive_after",)


class ManifestError(Exception):
    """A present ``_plexus.yaml`` cannot be read or interpreted.

    The single exception type of this module. Callers treat any instance as
    "this tree is schema form and unusable" — never as "no manifest, fall back
    to legacy" (D20). ``path`` is the manifest the failure is about.
    """

    def __init__(self, path, message):
        self.path = Path(path)
        super().__init__(f"{self.path}: {message}")


@dataclass(frozen=True)
class Manifest:
    """One parsed ``_plexus.yaml``.

    ``projects``, ``sources`` and ``housekeeping`` are the sections as
    written: short name / source name / area name -> the raw entry mapping
    (``{}`` for an entry written with no keys, e.g. a D19 repoless project
    registered by name alone). An absent or ``null`` section is ``{}``.
    Every ``projects`` key is a §1 short name (checked here, relied on
    everywhere). ``warnings`` are the forward-compat findings, in document
    order.
    """

    path: Path
    schema: int
    name: str
    projects: Dict[str, Dict[str, Any]]
    sources: Dict[str, Dict[str, Any]]
    housekeeping: Dict[str, Dict[str, Any]]
    warnings: Tuple[str, ...]


def source_entry_keys(name) -> Tuple[str, ...]:
    """The documented keys of ``sources.<name>``
    (:data:`SOURCE_ENTRY_KEYS_BY_NAME`, defaulting to
    :data:`UNKNOWN_SOURCE_ENTRY_KEYS` for a name this schema does not
    document). The ONE place the per-name vocabulary is read."""
    return SOURCE_ENTRY_KEYS_BY_NAME.get(name, UNKNOWN_SOURCE_ENTRY_KEYS)


def is_short_name(name) -> bool:
    """Whether *name* is a §1 short name (a string matching ``SHORT_NAME_RE``
    in full)."""
    return isinstance(name, str) and SHORT_NAME_RE.fullmatch(name) is not None


def manifest_path(root) -> Path:
    """Where the manifest lives for the plexus rooted at *root*."""
    return Path(root) / MANIFEST_NAME


def load_manifest(root) -> Optional[Manifest]:
    """Load the manifest of the plexus rooted at *root*.

    Returns ``None`` when no ``_plexus.yaml`` exists there — the legacy-form
    signal (D20). Anything present that cannot be read or interpreted raises
    :class:`ManifestError`; see the module docstring for the full taxonomy.
    """
    path = manifest_path(root)
    # ``is_symlink`` first: a dangling symlink is PRESENT (someone put a
    # manifest there) even though ``exists`` follows it and says no.
    if not (path.is_symlink() or path.exists()):
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(
            path,
            f"unreadable {MANIFEST_NAME}: {exc}. A present manifest is never "
            "silently ignored — fix it or remove it.",
        )
    return parse_manifest(text, path=path)


def parse_manifest(text, path=None) -> Manifest:
    """Parse manifest *text* into a :class:`Manifest`.

    The pure half of :func:`load_manifest`: no disk access. *path* names the
    manifest in errors and warnings (and lands on ``Manifest.path``); it
    defaults to the bare filename when the text did not come from a file.
    """
    path = Path(path) if path is not None else Path(MANIFEST_NAME)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError(path, f"unparseable {MANIFEST_NAME}: {exc}")
    if not isinstance(data, dict):
        raise ManifestError(
            path,
            f"{MANIFEST_NAME} must be a YAML mapping with `schema:` and "
            f"`name:` keys, got {type(data).__name__}",
        )

    schema = _parse_schema(path, data.get("schema"))
    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise ManifestError(
            path,
            "must declare a non-empty string `name:` (the instance name), got "
            f"{name!r}",
        )

    warnings: list = []
    checks = {
        "projects": _check_project_entry,
        "sources": _check_source_entry,
        "housekeeping": _check_housekeeping_entry,
    }
    sections: Dict[str, Dict[str, Dict[str, Any]]] = {name: {} for name in checks}
    # One pass in document order, so warnings (and the first error) come out
    # in the order the operator wrote the file, not section by section.
    for key, raw in data.items():
        if key in checks:
            sections[key] = _parse_section(path, key, raw, warnings, checks[key])
        elif key not in TOP_LEVEL_KEYS:
            warnings.append(
                f"{path}: unknown top-level key `{key}` ignored (forward-compat)"
            )

    return Manifest(
        path=path,
        schema=schema,
        name=name,
        projects=sections["projects"],
        sources=sections["sources"],
        housekeeping=sections["housekeeping"],
        warnings=tuple(warnings),
    )


def _parse_schema(path, schema) -> int:
    supported = ", ".join(str(s) for s in SUPPORTED_SCHEMAS)
    # Booleans are explicitly rejected before the int check: `schema: true`
    # is a YAML bool and isinstance(True, int) would let it sail through
    # (the read_taskdef_schema / load_sources trap).
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise ManifestError(
            path,
            f"must declare an integer `schema:` (supported: {supported}), got "
            f"{schema!r}",
        )
    if schema > CURRENT_SCHEMA:
        raise ManifestError(
            path,
            f"declares schema {schema}, but this lmer supports: {supported}. "
            "A newer lmer wrote this plexus; upgrade lmer to a release that "
            f"understands schema {schema} (§8 version floor).",
        )
    if schema not in SUPPORTED_SCHEMAS:
        raise ManifestError(
            path,
            f"declares schema {schema}, but this lmer supports: {supported}.",
        )
    return schema


def _parse_section(path, section, raw, warnings, check_entry) -> Dict[str, Dict[str, Any]]:
    """One optional top-level section: a mapping of string keys to entry
    mappings. Absent/``null`` → ``{}``; a ``null`` entry → ``{}`` (an entry
    written with no keys). Everything inside an entry (unknown-key warnings,
    the registry's key charset and value types) is *check_entry*'s; the
    entry itself is kept raw."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ManifestError(
            path,
            f"`{section}:` must be a mapping of names to entries, got "
            f"{type(raw).__name__}",
        )
    out: Dict[str, Dict[str, Any]] = {}
    for key, entry in raw.items():
        if not isinstance(key, str):
            raise ManifestError(
                path,
                f"`{section}:` key {key!r} is a YAML {type(key).__name__}, not "
                "a string; quote it (e.g. \"123\") — names are never coerced.",
            )
        if entry is None:
            entry = {}
        if not isinstance(entry, dict):
            raise ManifestError(
                path,
                f"`{section}.{key}` must be a mapping, got {type(entry).__name__}",
            )
        check_entry(path, key, entry, warnings)
        out[key] = entry
    return out


def _warn_unknown_keys(path, prefix, entry, known, warnings):
    for key in entry:
        if key not in known:
            warnings.append(
                f"{path}: unknown key `{prefix}.{key}` ignored (forward-compat)"
            )


def _check_project_entry(path, name, entry, warnings):
    """A registry entry: THE short-name check on the key and the TYPES of
    ``repo``/``remotes`` (module docstring, "Registry keys and values"). The
    values' semantics are the registry's; the entry is left as written."""
    if not is_short_name(name):
        raise ManifestError(
            path,
            f"`projects:` key {name!r} is not a short name: names match "
            f"`{SHORT_NAME_RE.pattern}` (lowercase, no dots — a dot marks a "
            "host bucket — no slashes, no leading `_` or `-`). A registry key "
            "is a directory name and is never sanitized; rename it.",
        )
    where = f"projects.{name}"
    repo = entry.get("repo")
    if repo is not None and not _is_non_empty_str(repo):
        raise ManifestError(
            path,
            f"`{where}.repo:` must be a non-empty remote URL string, got {repo!r}",
        )
    remotes = entry.get("remotes")
    if remotes is not None:
        if not isinstance(remotes, list):
            raise ManifestError(
                path,
                f"`{where}.remotes:` must be a list of remote URL strings "
                f"(one `- url` per line), got {remotes!r}",
            )
        for i, value in enumerate(remotes):
            if not _is_non_empty_str(value):
                raise ManifestError(
                    path,
                    f"`{where}.remotes:[{i}]` must be a non-empty remote URL "
                    f"string, got {value!r}",
                )
    _warn_unknown_keys(path, where, entry, PROJECT_ENTRY_KEYS, warnings)


def _is_non_empty_str(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _check_source_entry(path, name, entry, warnings):
    if name in RESERVED_SOURCE_NAMES:
        warnings.append(
            f"{path}: `sources.{name}` is reserved for a future schema and not "
            "supported yet; ignored"
        )
    elif name not in SOURCE_NAMES:
        warnings.append(
            f"{path}: unknown key `sources.{name}` ignored (forward-compat)"
        )
    _warn_unknown_keys(
        path, f"sources.{name}", entry, source_entry_keys(name), warnings
    )


def _check_housekeeping_entry(path, area, entry, warnings):
    _warn_unknown_keys(
        path, f"housekeeping.{area}", entry, HOUSEKEEPING_POLICY_KEYS, warnings
    )
