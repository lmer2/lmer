"""The memory store-format marker, ``.lmer-memory-format`` (issue #598).

A scope's marker names the newest layout its writer used (1 flat, 2 split; a
missing marker is 1). It is a guard and nothing else — the split shape stays
the switch, and persist never writes it. These tests pin:

- a marker newer than this lmer refuses every writer (persist, forget,
  revive, housekeep, the migrate lift) and leaves the tree as it was — for
  persist the restore manifest too — while restore warns and goes on;
- the marker is no file of the memory set: never restored into the agent
  dir, never carried into the other scope, never deleted, never validated;
- a marker this lmer understands changes nothing a persist writes.
"""

import pytest

from tests.plexus_fixtures import build_schema_plexus
from tests.test_work_repo_memory_areas import (  # noqa: F401  (fixtures)
    PROJECT_MEMORY_FILE,
    _add_memory,
    _clean_lmer_env,
    _persist,
    _set_context,
    _split,
    _tree_bytes,
    legacy_tree,
    schema2_tree,
    schema_tree,
)
from work_repo import memory

NEWER = str(memory.MEMORY_FORMAT_MAX + 1)
REFUSAL = (
    f"store format {NEWER} is newer than this lmer understands "
    f"(max {memory.MEMORY_FORMAT_MAX}); update the image"
)


def _mark(scope, value):
    (scope / memory.MEMORY_FORMAT_FILE).write_text(f"{value}\n")


class TestCheckStoreFormat:
    @pytest.mark.parametrize("value", [None, "1", "2", " 2\n"])
    def test_a_format_this_lmer_knows_is_no_problem(self, tmp_path, value):
        if value is not None:
            (tmp_path / memory.MEMORY_FORMAT_FILE).write_text(value)
        assert memory.check_store_format(tmp_path) is None

    def test_a_newer_format_is_refused_with_the_update_hint(self, tmp_path):
        _mark(tmp_path, NEWER)
        assert REFUSAL in memory.check_store_format(tmp_path)

    @pytest.mark.parametrize("value", ["", "two", "0", "-1"])
    def test_a_marker_that_names_no_format_is_refused(self, tmp_path, value):
        (tmp_path / memory.MEMORY_FORMAT_FILE).write_text(value)
        assert "not a positive integer" in memory.check_store_format(tmp_path)


class TestPersistRefuses:
    @pytest.mark.parametrize("scope", ["project", "instance"])
    def test_a_newer_scope_refuses_and_changes_nothing(self, schema_tree, scope, capsys):
        tree, agent_dir = schema_tree
        assert memory.restore_memory() == 0
        _add_memory(agent_dir, "project_new.md")
        marked = tree.project_memory_dir if scope == "project" else tree.instance_memory_dir
        _mark(marked, NEWER)
        before = _tree_bytes(tree.root)
        manifest = memory.restore_manifest_path().read_bytes()
        assert _persist() == (1, False)
        assert _tree_bytes(tree.root) == before
        assert memory.restore_manifest_path().read_bytes() == manifest
        assert REFUSAL in capsys.readouterr().err

    def test_a_legacy_scope_refuses_too(self, legacy_tree, capsys):
        tree, agent_dir = legacy_tree
        assert memory.restore_memory() == 0
        _add_memory(agent_dir, "project_new.md")
        _mark(tree.project_memory_dir, NEWER)
        before = _tree_bytes(tree.root)
        assert _persist() == (1, False)
        assert _tree_bytes(tree.root) == before
        assert REFUSAL in capsys.readouterr().err


class TestRestoreWarns:
    def test_restore_warns_and_hands_over_the_memory(self, schema_tree, capsys):
        tree, agent_dir = schema_tree
        _mark(tree.instance_memory_dir, NEWER)
        assert memory.restore_memory() == 0
        assert REFUSAL in capsys.readouterr().err
        assert (agent_dir / PROJECT_MEMORY_FILE).is_file()
        assert not (agent_dir / memory.MEMORY_FORMAT_FILE).exists()


class TestRetirementRefuses:
    def test_forget_refuses_a_newer_scope(self, schema2_tree):
        tree, _agent_dir = schema2_tree
        scope = tree.project_memory_dir
        _mark(scope, NEWER)
        key = (scope / PROJECT_MEMORY_FILE).relative_to(tree.root).as_posix()
        before = _tree_bytes(tree.root)
        with pytest.raises(memory.MemoryRetirementError, match="update the image"):
            memory.forget_memory(key)
        assert _tree_bytes(tree.root) == before

    def test_revive_refuses_a_newer_scope(self, schema2_tree):
        tree, _agent_dir = schema2_tree
        scope = tree.project_memory_dir
        key = (scope / PROJECT_MEMORY_FILE).relative_to(tree.root).as_posix()
        memory.forget_memory(key)
        _mark(scope, NEWER)
        before = _tree_bytes(tree.root)
        with pytest.raises(memory.MemoryRetirementError, match="update the image"):
            memory.revive_memory(key)
        assert _tree_bytes(tree.root) == before


class TestNoMemberOfTheMemorySet:
    def test_an_instance_marker_never_lands_in_the_project_scope(self, schema_tree):
        tree, agent_dir = schema_tree
        _split(tree.instance_memory_dir, {"user": []})
        _mark(tree.instance_memory_dir, memory.MEMORY_FORMAT_SPLIT)
        assert memory.restore_memory() == 0
        assert not (agent_dir / memory.MEMORY_FORMAT_FILE).exists()
        # Even one the agent puts in its own dir is never persisted anywhere.
        _mark(agent_dir, memory.MEMORY_FORMAT_SPLIT)
        _add_memory(agent_dir, "project_new.md")
        assert _persist()[0] == 0
        assert not (tree.project_memory_dir / memory.MEMORY_FORMAT_FILE).exists()
        assert (tree.instance_memory_dir / memory.MEMORY_FORMAT_FILE).read_text() == "2\n"

    def test_an_unbaselined_delete_never_deletes_the_marker(self, schema_tree):
        tree, agent_dir = schema_tree
        _mark(tree.project_memory_dir, memory.MEMORY_FORMAT_FLAT)
        _add_memory(agent_dir, "project_new.md")
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(memory, "commit_work_path", lambda *args, **kwargs: 0)
            assert memory.persist_memory(allow_unbaselined_delete=True) == 0
        assert not (tree.project_memory_dir / PROJECT_MEMORY_FILE).exists()  # it did delete
        assert (tree.project_memory_dir / memory.MEMORY_FORMAT_FILE).read_text() == "1\n"

    def test_a_known_marker_changes_nothing_a_persist_writes(
        self, tmp_path, monkeypatch
    ):
        trees = {}
        for marked in (False, True):
            tree, agent_dir = _fresh_schema_tree(tmp_path / str(marked), monkeypatch)
            if marked:
                _mark(tree.project_memory_dir, memory.MEMORY_FORMAT_FLAT)
                _mark(tree.instance_memory_dir, memory.MEMORY_FORMAT_FLAT)
            assert memory.restore_memory() == 0
            _add_memory(agent_dir, "project_new.md")
            _add_memory(agent_dir, "user_new.md", kind="user")
            assert _persist()[0] == 0
            trees[marked] = {
                rel: data
                for rel, data in _tree_bytes(tree.root).items()
                if not rel.endswith(memory.MEMORY_FORMAT_FILE)
            }
        assert trees[True] == trees[False]


def _fresh_schema_tree(base, monkeypatch):
    """A schema tree and agent dir under *base*, the session pointed at them."""
    tree = build_schema_plexus(base / "plexus-schema")
    agent_dir = base / "agent-memory"
    agent_dir.mkdir()
    _set_context(monkeypatch, tree, agent_dir)
    return tree, agent_dir
