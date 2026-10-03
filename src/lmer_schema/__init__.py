"""The lmer schema ("plexus") — importable core behind ``lmer-schema`` (issue #353).

This package formalizes the layout of everything lmer reads and writes outside
the code repos (work repo, napkin, memory, knowledge, plans) as ONE schema with
one form, versioned by the manifest's ``schema:`` int. It is the core the
``lmer-schema validate`` CLI wraps (spec D15) and the dual-read helpers the
work CLI calls for form detection, registry resolution and area routing
(spec D20). ``docs/SCHEMA.md`` is the normative document; this package is its
executable half.

Import spelling — decided here so every sibling subsystem uses ONE spelling:

- The stable public surface is a **flat re-export** from the package root::

      from lmer_schema import load_manifest, detect_form, resolve_project_dir

  ``__all__`` IS that surface: a name is public exactly when it is listed there,
  and consumers (validator checks, work CLI, hooks-adjacent tooling) must not
  reach into submodules for anything it lists. Flat over submodule-only because
  the surface is small and deliberately curated (manifest / urls / registry /
  form), the callers are spread across three packages, and a single spelling is
  what makes a later module split invisible to them.
- ``lmer_schema.cli`` (the console entry ``lmer-schema``) and the
  ``lmer_schema.checks`` package (validator internals) are NOT part of the
  surface and are never re-exported here.
- The surface is the four core modules' own ``__all__``, flattened: the
  manifest envelope (``lmer_schema.manifest``), the D21 URL adapter
  (``lmer_schema.urls``), identity and project-dir resolution
  (``lmer_schema.registry``) and D20 form detection (``lmer_schema.form``).
  Re-exports are the same objects, not copies, so ``is``/``isinstance`` hold
  across both spellings. ``is_short_name`` and ``SHORT_NAME_RE`` come from
  ``registry`` (``manifest`` defines the same rule for the documents it
  parses; the registry is where identity lives, and the two are pinned
  against each other in their own tests).
- Adding or removing a name here is a deliberate contract change:
  ``tests/test_lmer_schema_package.py`` pins the exact list, so a name never
  joins the surface as a side effect of an edit inside a submodule.

Import DIRECTION, and why "import the submodule instead" is not an escape:
these re-exports are eager, and ``urls`` pulls ``lmer_cli.container.sources``
(D21). Importing any submodule runs this file first, so
``from lmer_schema.registry import ...`` pulls that chain exactly as
``import lmer_schema`` does — there is no spelling that skips the package
body. The dependency therefore points ONE way (``lmer_schema`` consumes
``lmer_cli.container.sources``, never the reverse), and anything on the
``lmer_cli`` side that ever needs this package **imports it inside the
function**, not at module scope. ``work_repo.utils`` is the worked example:
it imports ``lmer_schema.form``/``registry`` inside ``_schema_layout`` so a
hook-facing command degrades on ImportError instead of failing at module
load. Keeping the surface eager (rather than a lazy ``__getattr__``) is
deliberate — a missing core should fail loudly at import, at the one call
site that chose to reach for it.
"""

from lmer_schema.form import (
    Detection,
    Form,
    detect,
    detect_form,
    find_plexus_root,
    has_manifest,
)
from lmer_schema.manifest import (
    CURRENT_SCHEMA,
    MANIFEST_NAME,
    SUPPORTED_SCHEMAS,
    Manifest,
    ManifestError,
    load_manifest,
    manifest_path,
    parse_manifest,
)
from lmer_schema.registry import (
    BUCKET_HIT,
    LEGACY_AREAS,
    MISS,
    PROJECT_MARKERS,
    REGISTRY_HIT,
    SCHEMA_AREAS,
    SHORT_NAME_RE,
    Project,
    Registry,
    RegistryError,
    Resolution,
    bucket_path,
    build_registry,
    is_host_bucket_name,
    is_project_shaped,
    is_short_name,
    load_registry,
    resolve_project_dir,
)
from lmer_schema.urls import entry_keys, lookup_key, normalize

#: THE public surface (see the import-spelling decision above), kept sorted.
__all__: list[str] = [
    "BUCKET_HIT",
    "CURRENT_SCHEMA",
    "Detection",
    "Form",
    "LEGACY_AREAS",
    "MANIFEST_NAME",
    "MISS",
    "Manifest",
    "ManifestError",
    "PROJECT_MARKERS",
    "Project",
    "REGISTRY_HIT",
    "Registry",
    "RegistryError",
    "Resolution",
    "SCHEMA_AREAS",
    "SHORT_NAME_RE",
    "SUPPORTED_SCHEMAS",
    "bucket_path",
    "build_registry",
    "detect",
    "detect_form",
    "entry_keys",
    "find_plexus_root",
    "has_manifest",
    "is_host_bucket_name",
    "is_project_shaped",
    "is_short_name",
    "load_manifest",
    "load_registry",
    "lookup_key",
    "manifest_path",
    "normalize",
    "parse_manifest",
    "resolve_project_dir",
]
