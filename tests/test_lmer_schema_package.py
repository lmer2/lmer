"""Package-level contract for ``lmer_schema`` (issue #353).

The package is the importable core behind ``lmer-schema validate`` (spec D15)
and the dual-read helpers the work CLI uses (spec D20). These tests pin the
things sibling subsystems depend on: the package imports cleanly, its public
surface is exactly what ``__all__`` documents (and every documented name really
resolves), and ``pyproject.toml`` declares the console entry pointing at
``lmer_schema.cli:main``. The script's *behaviour* is the validator's business;
this file only asserts the declaration, so it stays green before ``cli`` lands.
"""

import importlib
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def pyproject():
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_package_imports_cleanly():
    module = importlib.import_module("lmer_schema")
    assert module.__doc__, "package docstring records the import-spelling decision"


#: The flat public surface: the four core modules' documented API, spelled
#: once. A name here is a consumed contract (the work CLI's dual-read, the
#: validator's checks, hooks-adjacent tooling), so adding or removing one is a
#: deliberate change of this list — never a side effect of an edit inside a
#: submodule.
PUBLIC_SURFACE = [
    # lmer_schema.manifest — the _plexus.yaml envelope
    "CURRENT_SCHEMA",
    "MANIFEST_NAME",
    "SUPPORTED_SCHEMAS",
    "Manifest",
    "ManifestError",
    "load_manifest",
    "manifest_path",
    "parse_manifest",
    # lmer_schema.urls — the D21 normalization adapter
    "entry_keys",
    "lookup_key",
    "normalize",
    # lmer_schema.registry — identity and project-dir resolution
    "BUCKET_HIT",
    "LEGACY_AREAS",
    "MISS",
    "PROJECT_MARKERS",
    "Project",
    "REGISTRY_HIT",
    "Registry",
    "RegistryError",
    "Resolution",
    "SCHEMA_AREAS",
    "SHORT_NAME_RE",
    "bucket_path",
    "build_registry",
    "is_host_bucket_name",
    "is_project_shaped",
    "is_short_name",
    "load_registry",
    "resolve_project_dir",
    # lmer_schema.form — D20 form detection
    "Detection",
    "Form",
    "detect",
    "detect_form",
    "find_plexus_root",
    "has_manifest",
]


def test_public_surface_matches_documented_all():
    """``__all__`` IS the documented surface: exactly these names, and every
    one of them resolves."""
    module = importlib.import_module("lmer_schema")
    assert isinstance(module.__all__, list)
    assert sorted(module.__all__) == sorted(PUBLIC_SURFACE)
    assert module.__all__ == sorted(module.__all__), "__all__ is kept sorted"
    for name in module.__all__:
        assert hasattr(module, name), f"__all__ names {name!r} but it does not resolve"


@pytest.mark.parametrize(
    "name, submodule",
    [
        ("load_manifest", "manifest"),
        ("ManifestError", "manifest"),
        ("normalize", "urls"),
        ("resolve_project_dir", "registry"),
        ("Resolution", "registry"),
        ("detect_form", "form"),
        ("Form", "form"),
    ],
)
def test_reexport_is_the_same_object_as_the_submodule_name(name, submodule):
    """A flat re-export, not a copy: importing either spelling gets the one
    object, so ``isinstance``/``is`` checks hold across the two."""
    module = importlib.import_module("lmer_schema")
    origin = importlib.import_module(f"lmer_schema.{submodule}")
    assert getattr(module, name) is getattr(origin, name)


def test_validator_internals_are_not_part_of_the_surface():
    """``lmer_schema.cli`` and ``lmer_schema.checks`` are never re-exported
    (the docstring's decision) — consumers of the core do not reach them."""
    module = importlib.import_module("lmer_schema")
    assert "cli" not in module.__all__
    assert "checks" not in module.__all__
    assert not {n for n in module.__all__ if n in {"Severity", "Finding", "Report", "CHECKS"}}


def test_docstring_example_names_are_really_public():
    """The docstring shows one import line; every name in it must be on the
    surface, or the first thing a consumer copies fails."""
    module = importlib.import_module("lmer_schema")
    example = "from lmer_schema import load_manifest, detect_form, resolve_project_dir"
    assert example in module.__doc__
    for name in example.split("import ", 1)[1].split(", "):
        assert name in module.__all__


def test_flat_reexport_is_the_recorded_spelling():
    """Sibling subsystems import one spelling; the docstring must say which."""
    module = importlib.import_module("lmer_schema")
    assert "from lmer_schema import" in module.__doc__


def test_pyproject_declares_console_entry(pyproject):
    scripts = pyproject["project"]["scripts"]
    assert "lmer-schema" in scripts
    assert scripts["lmer-schema"] == "lmer_schema.cli:main"


def test_package_is_discoverable_by_setuptools(pyproject):
    """The package lives under ``src/`` like every sibling, and pyproject finds
    packages there — no explicit package list to keep in sync."""
    assert pyproject["tool"]["setuptools"]["packages"]["find"]["where"] == ["src"]
    assert (REPO_ROOT / "src" / "lmer_schema" / "__init__.py").is_file()
