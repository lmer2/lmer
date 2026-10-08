"""The thin-consumer guard for the migration tooling (§13.B, #366/#367/#368).

`work adopt` and `work migrate` are consumers of the registry writer, never
YAML writers of their own:

- no ``yaml.dump`` / ``safe_dump`` / ``dump_all`` / ``safe_dump_all`` and no
  ``ruamel`` import anywhere in either module;
- the manifest is written only through ``lmer_schema.writer`` (reached via
  ``work_repo.utils.core_module("writer")``), never by opening
  ``_plexus.yaml`` for writing or calling ``write_text``/``write_bytes`` on
  it;
- no module imports the test package.

This is a source/AST guard in the style of the existing drift guards
(``tests/test_lmer_schema_writer.py``, ``tests/test_work_repo_adopt.py``):
the two modules must not grow a private YAML write path beside the writer.
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULES = {
    "adopt": REPO_ROOT / "src" / "work_repo" / "adopt.py",
    "migrate": REPO_ROOT / "src" / "work_repo" / "migrate.py",
}

#: Attribute names that would be a YAML dump.
_DUMP_ATTRS = {"dump", "safe_dump", "dump_all", "safe_dump_all"}

#: Attribute names that write a file directly.
_FILE_WRITE_ATTRS = {"write_text", "write_bytes", "write"}

#: The manifest's own names; a direct write touching one of these is a
#: bypass of the writer.
_MANIFEST_NAMES = {"_plexus.yaml", "MANIFEST_NAME", "manifest_path", "manifest"}


def _tree(name):
    return ast.parse(MODULES[name].read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", sorted(MODULES))
def test_no_yaml_dump_and_no_ruamel(name):
    tree = _tree(name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in _DUMP_ATTRS, ast.dump(node)
        if isinstance(node, ast.Import):
            assert not any(a.name.startswith("ruamel") for a in node.names), ast.dump(node)
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(("ruamel", "tests")), ast.dump(node)


@pytest.mark.parametrize("name", sorted(MODULES))
def test_no_direct_manifest_write(name):
    """Every manifest mutation is the writer's: no ``open``-for-write and no
    ``write_text``/``write_bytes`` whose arguments name the manifest."""
    tree = _tree(name)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        attr = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else None
        )
        if attr not in _FILE_WRITE_ATTRS and attr != "open":
            continue
        arguments = ast.dump(node)
        assert not any(spelling in arguments for spelling in _MANIFEST_NAMES), ast.dump(node)


@pytest.mark.parametrize("name", sorted(MODULES))
def test_the_writer_is_reached_through_the_core_accessor(name):
    """The modules reach ``lmer_schema.writer`` through
    ``core_module("writer")`` (the one core accessor), never a direct
    ``from lmer_schema.writer import ...``."""
    tree = _tree(name)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "lmer_schema.writer", ast.dump(node)
        if isinstance(node, ast.Import):
            assert not any(a.name == "lmer_schema.writer" for a in node.names), ast.dump(node)
    source = MODULES[name].read_text(encoding="utf-8")
    assert 'core_module("writer")' in source


@pytest.mark.parametrize("name", sorted(MODULES))
def test_no_test_package_import(name):
    tree = _tree(name)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("tests"), ast.dump(node)
        if isinstance(node, ast.Import):
            assert not any(a.name.startswith("tests") for a in node.names), ast.dump(node)
