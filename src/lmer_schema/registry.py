"""The projects registry and project-directory resolution (spec D8/D9/D19, §4).

Two halves in one module, because they share one invariant — the registry's
UNION of normalized remote keys — and one vocabulary (short name, host bucket,
project-shaped dir, §1):

1. **The registry** (:func:`load_registry`, :func:`build_registry`,
   :class:`Registry`, :class:`Project`) is the typed reading of the manifest's
   raw ``projects:`` block. Each entry's ``repo:`` is the PRIMARY remote (the
   one lmer clones and pushes to by default), ``remotes:`` lists ADDITIONAL
   remotes for the same project, and both are optional (D19). The union of
   every entry's keys maps one repo to AT MOST one project.
2. **Resolution** (:func:`resolve_project_dir`, :class:`Resolution`) answers
   "where is this repo's project dir": normalize the lookup URL, match it
   against the union, and on a miss fall back to the ``{host}/{project}``
   HOST BUCKET on disk (D9). Registry first, bucket second, never a derived
   name.

Identity and the union
======================
Every URL comparison goes through ``lmer_schema.urls`` (D21): ``lookup_key``
applies the root schema's comparison contract and ``normalize`` supplies the
canonical spelling. This module dissects no URL of its own —
``tests/test_lmer_schema_registry.py`` guards that on the module source.

An entry contributes ``entry_keys(repo, remotes)`` to the union, primary
first. **A repoless entry contributes NO keys**: it lives in
``Registry.projects`` under its short name, is addressed by name
(``registry[name]``, ``Project.dir(root)``) and never matches a remote
lookup — §4: "it is addressed by name and never matches a remote-lookup
resolution, so resolution is unchanged". Zero, one and many remotes are the
same rule at three counts.

What the registry refuses (all :class:`RegistryError`):

- a short name outside ``[a-z0-9][a-z0-9_-]*`` — a dotted name would be
  read as a host bucket (§1), a ``_`` name is the schema's (D6), a capital
  is the case-insensitive-fs trap;
- ``remotes:`` without ``repo:`` (nothing to clone), ``remotes:`` that is
  not a list of non-empty strings, ``repo:``/``description:`` of the wrong
  type, a value that normalizes to nothing;
- an embedded credential in any value (the no-secrets rule; detected by
  ``url_has_embedded_credential``, never echoed — every value printed goes
  through ``scrub_credentials``);
- ``repo:`` repeated inside its own ``remotes:`` under ANY spelling, and two
  ``remotes:`` that fold to one key — §4 registry corners: "the same
  normalized remote in two entries, or twice within one entry, is an error";
- one key claimed by two entries — the error names BOTH projects.

What it does NOT refuse: ``remotes: []`` beside ``repo:``, a non-canonical
spelling, a repoless entry with no ``description:``. Those are the
validator's WARN nudges (``lmer_schema.checks``); a loader that failed on
them would turn a nudge into an outage.

``RegistryError`` subclasses ``ManifestError`` rather than being a fresh
type: the registry IS a section of the manifest, so a broken registry is a
broken manifest — present, unusable, never demoted to legacy form (D20) —
and every caller that already catches ``ManifestError`` for "schema form but
unusable" gets registry failures for free while the subclass keeps them
distinguishable. It carries the manifest path exactly like its parent.

Resolution
==========
``resolve_project_dir(root, url)`` returns a :class:`Resolution` whose
``how`` is one of three named values:

- ``REGISTRY_HIT`` — ``path`` is ``<root>/<short-name>``, ``project`` the
  short name. The dir need not exist yet (a session creates it on first
  write); resolution answers where it IS.
- ``BUCKET_HIT`` — no entry matched, but ``<root>/{host}/{path}`` exists and
  is PROJECT-SHAPED; ``path`` is that dir, ``project`` is ``None`` (D9: no
  name is ever derived).
- ``MISS`` — neither. ``path`` is ``None``. ``bucket_path`` still says where
  the work WOULD be parked, so a caller that must write (D9: never refuse,
  never lose) knows where without re-deriving it; the resolver creates
  nothing.

**Precedence is registry first, bucket second**, as SCHEMA.md "Host
buckets" states. When a remote has both an entry and a real bucket dir the
entry wins here; the split tree is the validator's ERROR, not a resolution
ambiguity. ``tests/test_lmer_schema_resolve.py`` asserts the precedence
directly rather than leaving it emergent.

The bucket path derives from the canonical SPELLING (``normalize``), not the
comparison key. Schema 1 keeps the historical ``{hostname}/{path}`` address.
Schema 2 preserves path case and encodes a non-default endpoint port in the
host directory as ``%3A<port>``. A real legacy portless bucket makes a
schema-2 fallback ambiguous and is refused; a registered short-name project
remains authoritative. The §1 markers decide whether a candidate is a bucket:

- the host must be a **dotted** name (``is_host_bucket_name``): a dotless
  host is unsupported as a bucket at v1 — register the project — and a
  short-name dir at the root is therefore never read as a bucket;
- the path must be one or more plain components with no ``.``/``..`` hops.
  There is no depth rule at any length: a bucket descends until the first
  project-shaped directory, and every level above it is a namespace level
  (the marker test decides, never depth). A cap would unresolve deep subgroup
  trees silently, and a MISS with no ``bucket_path`` cannot satisfy D9's
  "never refuse, never lose";
- the candidate must be **project-shaped** (``is_project_shaped``): it
  directly contains a schema area or a legacy area — exactly the
  ``PROJECT_MARKERS`` list, no depth rule. A namespace level or an empty dir
  is a MISS.

A legacy tree (no manifest) has no registry, so resolution is bucket-only;
a present-but-broken manifest raises (D20) rather than falling back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

from lmer_cli.container.sources import scrub_credentials, url_has_embedded_credential
from lmer_schema.manifest import Manifest, ManifestError, load_manifest
from lmer_schema.urls import entry_keys, lookup_key, normalize

__all__ = [
    "BUCKET_HIT",
    "LEGACY_AREAS",
    "MISS",
    "PROJECT_MARKERS",
    "REGISTRY_HIT",
    "SCHEMA_AREAS",
    "SHORT_NAME_RE",
    "Project",
    "Registry",
    "RegistryError",
    "Resolution",
    "bucket_path",
    "build_registry",
    "is_host_bucket_name",
    "is_project_shaped",
    "is_short_name",
    "load_registry",
    "resolve_project_dir",
]

#: §1 short-name charset: lowercase, no dots, no slashes, no leading ``_``/``-``.
SHORT_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")

#: The schema areas whose direct presence makes a dir project-shaped (§1).
SCHEMA_AREAS = (
    "_runs", "_plans", "_agents", "_info", "_specs", "_taskdef", "_claims",
)

#: The legacy areas that do the same (§1, §9).
LEGACY_AREAS = ("runs", "specs", "info", "memory")

#: The §1 marker test, verbatim: any of these directly inside a dir.
PROJECT_MARKERS = SCHEMA_AREAS + LEGACY_AREAS

#: :attr:`Resolution.how` values.
REGISTRY_HIT = "registry"
BUCKET_HIT = "bucket"
MISS = "miss"

#: Sentinel for "load the registry from *root*" (``None`` is meaningful:
#: legacy tree / bucket-only).
_LOAD = object()


class RegistryError(ManifestError):
    """The manifest's ``projects:`` block violates the registry rules.

    A :class:`ManifestError` (see the module docstring for why): the tree is
    schema form and unusable, never "no registry, use the bucket".
    """


@dataclass(frozen=True)
class Project:
    """One registry entry, typed.

    ``repo`` and ``remotes`` are the values AS WRITTEN (the canonical-form
    nudge is the validator's); ``keys`` are their comparison keys, primary
    first, then the additional remotes in declared order — ``()`` for a
    repoless project (D19).
    """

    name: str
    repo: Optional[str]
    remotes: Tuple[str, ...]
    description: Optional[str]
    keys: Tuple[str, ...]

    @property
    def repoless(self) -> bool:
        """D19: no ``repo:`` (and therefore no ``remotes:``)."""
        return self.repo is None

    def dir(self, root) -> Path:
        """This project's tree: ``<root>/<short-name>`` (§2)."""
        return Path(root) / self.name


@dataclass(frozen=True)
class Registry:
    """The typed registry: ``projects`` by short name in manifest order, and
    ``index`` — the union — from comparison key to short name.

    Mapping-like over short names (``in``, ``[]``, ``get``, iteration,
    ``len``); :meth:`lookup` is the remote-URL side.
    """

    path: Path
    projects: Dict[str, Project]
    index: Dict[str, str]
    schema: int = 1

    def __contains__(self, name) -> bool:
        return name in self.projects

    def __getitem__(self, name) -> Project:
        return self.projects[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self.projects)

    def __len__(self) -> int:
        return len(self.projects)

    def get(self, name) -> Optional[Project]:
        return self.projects.get(name)

    def lookup(self, url) -> Optional[Project]:
        """The project *url* belongs to, by normalized lookup across the
        union (D8), or ``None``. An empty URL never matches."""
        key = lookup_key(url, schema=self.schema)
        if not key:
            return None
        name = self.index.get(key)
        return self.projects[name] if name is not None else None


def load_registry(root) -> Optional[Registry]:
    """The registry of the plexus rooted at *root*.

    ``None`` when there is no manifest (legacy form — the same signal as
    :func:`lmer_schema.manifest.load_manifest`). A present manifest that is
    broken raises :class:`ManifestError`; a well-formed one whose registry
    breaks the rules raises :class:`RegistryError`.
    """
    manifest = load_manifest(root)
    if manifest is None:
        return None
    return build_registry(manifest)


def build_registry(manifest: Manifest) -> Registry:
    """Build the typed registry from a parsed :class:`Manifest` (pure)."""
    path = manifest.path
    projects: Dict[str, Project] = {}
    index: Dict[str, str] = {}
    for name, entry in manifest.projects.items():
        project = _parse_entry(path, name, entry, schema=manifest.schema)
        for key in project.keys:
            owner = index.get(key)
            if owner is not None:
                raise RegistryError(
                    path,
                    f"remote {scrub_credentials(key)} is claimed by both "
                    f"`projects.{owner}` and `projects.{name}`; a repo binds to "
                    "at most one project (resolution must stay deterministic) — "
                    "list it under one entry only.",
                )
            index[key] = name
        projects[name] = project
    return Registry(
        path=path, schema=manifest.schema, projects=projects, index=index,
    )


def _parse_entry(path, name, entry: Dict[str, Any], *, schema: int = 1) -> Project:
    """One ``projects:`` entry -> a :class:`Project`.

    The key charset and the ``repo:``/``remotes:`` TYPES are checked by
    :mod:`lmer_schema.manifest` for every document it loads; they are
    re-checked here because :class:`~lmer_schema.manifest.Manifest` is a
    public dataclass and ``build_registry`` accepts a hand-built one. The
    two layers agree on the rule and on the taxonomy (``RegistryError`` is a
    ``ManifestError``), so which one fires first is not a contract — that a
    broken entry raises rather than yielding a mangled key is.
    """
    if not is_short_name(name):
        raise RegistryError(
            path,
            f"project name {name!r} is not a short name: names match "
            f"`{SHORT_NAME_RE.pattern}` (lowercase, no dots — a dot marks a "
            "host bucket — no slashes, no leading `_` or `-`).",
        )
    where = f"projects.{name}"

    repo = entry.get("repo")
    if repo is not None:
        repo = _check_remote(path, f"{where}.repo:", repo, schema=schema)

    raw_remotes = entry.get("remotes")
    if raw_remotes is not None and repo is None:
        raise RegistryError(
            path,
            f"`{where}.remotes:` without `repo:` — nothing to clone. The "
            "primary is named, never positional: put the remote lmer should "
            "clone under `repo:` and only the additional ones under `remotes:`.",
        )
    remotes: Tuple[str, ...] = ()
    if raw_remotes is not None:
        if not isinstance(raw_remotes, list):
            raise RegistryError(
                path,
                f"`{where}.remotes:` must be a list of remote URLs, got "
                f"{type(raw_remotes).__name__}",
            )
        remotes = tuple(
            _check_remote(path, f"{where}.remotes:[{i}]", value, schema=schema)
            for i, value in enumerate(raw_remotes)
        )

    description = entry.get("description")
    if description is not None and not isinstance(description, str):
        raise RegistryError(
            path,
            f"`{where}.description:` must be a string, got "
            f"{type(description).__name__}",
        )

    _check_entry_collisions(path, where, repo, remotes, schema=schema)
    return Project(
        name=name,
        repo=repo,
        remotes=remotes,
        description=description,
        keys=entry_keys(repo, remotes, schema=schema),
    )


def _check_remote(path, where, value, *, schema: int = 1) -> str:
    """One ``repo:``/``remotes:`` value: a non-empty string, credential-free,
    normalizing to a real ``host/path`` key — HOST included, which is the
    check that catches ``repo: origin`` (a remote NAME normalizes to a
    hostless ``origin`` and would otherwise become a key, and a bucket, of
    its own). Returns it as written."""
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(
            path,
            f"`{where}` must be a non-empty remote URL string, got "
            f"{type(value).__name__}",
        )
    if url_has_embedded_credential(value):
        raise RegistryError(
            path,
            f"`{where}` carries an embedded credential "
            f"({scrub_credentials(value)}); the manifest never holds secrets — "
            "write the remote without userinfo and let lmer supply auth.",
        )
    key = lookup_key(value, schema=schema)
    if not key or "/" not in key:
        raise RegistryError(
            path,
            f"`{where}` is not a repository URL: {scrub_credentials(value)!r} "
            f"normalizes to {scrub_credentials(key)!r}, which has no host "
            "(expected `host/path`) — a bare remote NAME like `origin` is not "
            "a URL.",
        )
    return value


def _check_entry_collisions(path, where, repo, remotes, *, schema: int = 1) -> None:
    """§4 registry corners: two values in ONE entry folding to one key."""
    primary = lookup_key(repo, schema=schema) if repo is not None else None
    seen: Dict[str, str] = {}
    for value in remotes:
        key = lookup_key(value, schema=schema)
        if key == primary:
            raise RegistryError(
                path,
                f"`{where}.repo:` is repeated inside `remotes:` as "
                f"{scrub_credentials(value)} — `repo:` is the primary and is "
                "never listed again (D8); `remotes:` holds only ADDITIONAL "
                "remotes.",
            )
        if key in seen:
            raise RegistryError(
                path,
                f"`{where}.remotes:` lists the same remote twice: "
                f"{scrub_credentials(seen[key])} and {scrub_credentials(value)} "
                f"both normalize to {scrub_credentials(key)}; keep one.",
            )
        seen[key] = value


# ---------------------------------------------------------------------------
# §1 markers
# ---------------------------------------------------------------------------


def is_short_name(name) -> bool:
    """§1: *name* is a legal project short name."""
    return isinstance(name, str) and SHORT_NAME_RE.fullmatch(name) is not None


def is_host_bucket_name(name) -> bool:
    """§1: a top-level *name* that is a host bucket — it contains a dot and is
    neither hidden (``.git``) nor schema-owned (``_*``). Dotless hosts are
    unsupported as buckets at v1."""
    return (
        isinstance(name, str)
        and "." in name
        and not name.startswith((".", "_"))
    )


def is_project_shaped(path) -> bool:
    """§1 marker test: *path* directly contains a schema area or a legacy
    area (``PROJECT_MARKERS``). No depth rule; a symlinked area counts."""
    path = Path(path)
    if not path.is_dir():
        return False
    return any((path / marker).is_dir() for marker in PROJECT_MARKERS)


def _bucket_host_segment(host: str, schema: int = 1) -> str:
    """Filesystem spelling for a normalized endpoint host."""
    if schema < 2:
        return host.partition(":")[0]
    hostname, separator, port = host.rpartition(":")
    if separator and hostname and port.isdigit():
        return f"{hostname}%3A{port}"
    return host


def _bucket_identity(parts, schema: int = 1) -> tuple[str, str]:
    """Remote identity encoded by bucket path *parts*."""
    host = parts[0]
    if schema >= 2:
        hostname, separator, port = host.rpartition("%3A")
        if separator and hostname and port.isdigit():
            host = f"{hostname}:{port}"
    return host, "/".join(parts[1:])


def _reject_filesystem_alias(root: Path, parts: list[str]) -> None:
    """Refuse an existing path reached through a differently spelled alias."""
    parent = root
    for part in parts:
        candidate = parent / part
        if not candidate.exists():
            return
        try:
            exact = any(child.name == part for child in parent.iterdir())
        except OSError as exc:
            raise RegistryError(
                root,
                f"bucket placement is ambiguous: cannot inspect {parent} "
                "for exact filesystem spelling",
            ) from exc
        if not exact:
            raise RegistryError(
                root,
                f"bucket placement is ambiguous: {candidate} aliases an existing "
                "filesystem entry with different case",
            )
        parent = candidate


def _bucket_address(root, spelling, schema: int = 1) -> Optional[Path]:
    """Derive a bucket address without consulting existing filesystem state."""
    if not spelling or "/" not in spelling:
        return None
    host, _, path = spelling.partition("/")
    hostname = host.partition(":")[0]
    if not is_host_bucket_name(hostname):
        return None
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    root = Path(root)
    bucket_host = _bucket_host_segment(host, schema=schema)
    address_parts = [bucket_host, *parts]
    return root.joinpath(*address_parts)


def bucket_path(root, spelling, schema: int = 1) -> Optional[Path]:
    """Filesystem address for the canonical remote *spelling*.

    Schema 1 retains its hostname-only address. Schema 2 encodes a
    non-default port as ``%3A<port>`` and preserves path case. This derivation
    is read-only; :func:`resolve_project_dir` checks write ambiguity when the
    registry does not claim the remote.
    """
    return _bucket_address(root, spelling, schema=schema)


def _check_bucket_ambiguity(root: Path, spelling: str, schema: int) -> None:
    """Refuse a schema-2 fallback whose endpoint has no unique disk address."""
    if schema < 2:
        return
    host, _, path = spelling.partition("/")
    hostname = host.partition(":")[0]
    bucket_host = _bucket_host_segment(host, schema=schema)
    parts = path.split("/")
    address_parts = [bucket_host, *parts]
    _reject_filesystem_alias(root, address_parts)
    if bucket_host != hostname:
        legacy = root.joinpath(hostname, *parts)
        if not legacy.is_symlink() and is_project_shaped(legacy):
            raise RegistryError(
                root,
                f"bucket placement is ambiguous: legacy path {legacy} drops "
                f"the endpoint port from {host}",
            )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Resolution:
    """How a lookup URL resolved (see the module docstring).

    ``how`` is ``REGISTRY_HIT``, ``BUCKET_HIT`` or ``MISS``; ``path`` the
    project dir (``None`` on a miss); ``project`` the short name on a
    registry hit, else ``None`` (D9: never derived); ``key`` the comparison
    key that was looked up; ``bucket_path`` where this key's bucket is or
    would be (``None`` when no bucket can hold it).
    """

    how: str
    path: Optional[Path]
    project: Optional[str]
    key: str
    bucket_path: Optional[Path]

    @property
    def resolved(self) -> bool:
        return self.how != MISS


def resolve_project_dir(root, url, registry=_LOAD, schema: Optional[int] = None) -> Resolution:
    """Resolve the project dir for *url* in the plexus rooted at *root*.

    Registry first, bucket second (D9). *registry* defaults to the tree's
    own (:func:`load_registry`; ``None`` for a legacy tree, raising on a
    broken manifest). Pass ``None`` explicitly for bucket-only resolution or
    an already-loaded :class:`Registry` to skip the reload.
    """
    root = Path(root)
    if registry is _LOAD:
        registry = load_registry(root)
    if schema is None:
        schema = registry.schema if registry is not None else 1
    key = lookup_key(url, schema=schema)
    spelling = normalize(url)

    if registry is not None:
        project = registry.lookup(url)
        if project is not None:
            # The registry is authoritative. A stale real bucket is a layout
            # finding, not grounds to redirect or refuse the registered tree.
            bucket = _bucket_address(root, spelling, schema=schema)
            return Resolution(
                how=REGISTRY_HIT,
                path=project.dir(root),
                project=project.name,
                key=key,
                bucket_path=bucket,
            )
    bucket = bucket_path(root, spelling, schema=schema)
    if bucket is not None:
        _check_bucket_ambiguity(root, spelling, schema)
    if bucket is not None and is_project_shaped(bucket):
        return Resolution(
            how=BUCKET_HIT, path=bucket, project=None, key=key, bucket_path=bucket
        )
    return Resolution(how=MISS, path=None, project=None, key=key, bucket_path=bucket)
