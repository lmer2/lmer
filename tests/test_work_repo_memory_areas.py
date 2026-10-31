"""The split memory index and its load budget (issue #435).

The harness loads only the head of ``MEMORY.md`` at session start, so a scope
can split its index: ``MEMORY.md`` becomes a MAP (one line per area) and the
entry lines live in ``areas/<slug>.md``. These tests pin:

- **Shape is the switch.** A scope with no ``areas/`` index behaves exactly as
  before, plus the size report; a scope with one is split.
- **Entry lines leave the map.** An entry an agent adds to ``MEMORY.md`` in a
  split scope is filed into its area (``metadata.area``, else ``unsorted``),
  and a line already filed in some area is dropped rather than duplicated.
- **Area indexes merge like the map.** Three-way over the restore manifest:
  a session removes only what it restored, adds only what it did not.
- **The budget.** Every persist reports the loaded index against the limit; a
  flat scope over it warns; a split scope refuses a persist that grows an
  over-budget map, and leaves the work repo and the manifest as they were.
"""

import dataclasses
import json
import os
from unittest.mock import patch

import pytest

from lmer_cli import harness
from lmer_cli.harness import HARNESSES, MemoryIndexLimit
from lmer_cli.memory_areas import is_area_slug
from lmer_schema.checks.agents import is_forget_tombstone, parse_index_entries
from lmer_schema.checks.runner import validate
from tests.conftest import strip_lmer_env
from tests.plexus_fixtures import (
    PRIMARY_REPO,
    PROJECT_MEMORY_FILE,
    USER_MEMORY_FILE,
    build_legacy_plexus,
    build_schema_plexus,
)
from work_repo import memory, migrate, utils


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    monkeypatch.setattr(memory, "check_commit_ready", lambda *args: 0)
    utils._ANNOUNCED_MANIFEST_PROBLEMS.clear()


def _memory_file(name, kind="project", area=None, description="a fact"):
    lines = ["---", f"name: {name}", f'description: "{description}"', "metadata:"]
    lines.append(f"  type: {kind}")
    if area is not None:
        lines.append(f"  area: {area}")
    lines += ["---", "", "The fact.", ""]
    return "\n".join(lines)


def _set_context(monkeypatch, tree, agent_dir):
    host, project = PRIMARY_REPO.split("/", 1)
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tree.root))
    monkeypatch.setenv("LMER_REPO_HOST", host)
    monkeypatch.setenv("LMER_REPO_PROJECT", project)
    monkeypatch.setenv("LMER_REPO_URL", f"https://{PRIMARY_REPO}")
    monkeypatch.setenv("LMER_AGENT_MEMORY_DIR", str(agent_dir))
    monkeypatch.setenv("LMER_PERSIST_AGENT_MEMORY", "1")


@pytest.fixture
def legacy_tree(tmp_path, monkeypatch):
    tree = build_legacy_plexus(tmp_path / "plexus-legacy")
    agent_dir = tmp_path / "agent-memory"
    agent_dir.mkdir()
    _set_context(monkeypatch, tree, agent_dir)
    return tree, agent_dir


@pytest.fixture
def schema_tree(tmp_path, monkeypatch):
    tree = build_schema_plexus(tmp_path / "plexus-schema")
    agent_dir = tmp_path / "agent-memory"
    agent_dir.mkdir()
    _set_context(monkeypatch, tree, agent_dir)
    return tree, agent_dir


@pytest.fixture
def schema2_tree(schema_tree):
    tree, agent_dir = schema_tree
    text = tree.manifest.read_text()
    tree.manifest.write_text(text.replace("schema: 1", "schema: 2", 1))
    return tree, agent_dir


def _persist():
    with patch("work_repo.memory.commit_work_path", return_value=0) as commit:
        rc = memory.persist_memory()
    return rc, commit.call_args is not None


def _split(scope, areas):
    """Turn *scope* into a split scope: every entry line moves to an area.

    *areas* maps an area slug to the memory filenames it files; the map line
    for each area is ``- [<slug>](areas/<slug>.md) — read when <slug>``.
    """
    index = (scope / memory.MEMORY_INDEX).read_text()
    lines = {line.split("](", 1)[1].split(")", 1)[0]: line for line in index.splitlines()}
    (scope / "areas").mkdir(exist_ok=True)
    map_lines = []
    for slug, filenames in areas.items():
        body = "".join(
            lines[name].replace(f"]({name})", f"](../{name})") + "\n" for name in filenames
        )
        (scope / "areas" / f"{slug}.md").write_text(f"# {slug}\n\n{body}")
        map_lines.append(f"- [{slug}](areas/{slug}.md) — read when {slug}\n")
    (scope / memory.MEMORY_INDEX).write_text("# Memory map\n\n" + "".join(map_lines))


def _add_memory(agent_dir, filename, *, kind="project", area=None, title="New fact",
                into=memory.MEMORY_INDEX):
    """What an agent does: write the file and add its index line to *into*."""
    (agent_dir / filename).write_text(_memory_file(filename[:-3], kind, area))
    link = filename if into == memory.MEMORY_INDEX else f"../{filename}"
    index = agent_dir / into
    index.parent.mkdir(parents=True, exist_ok=True)
    text = index.read_text() if index.is_file() else ""
    index.write_text(text + f"- [{title}]({link}) — hook for {title}.\n")


#: What prep-release 12d5e7f3 wrote to the split fixture's MEMORY.md (see
#: test_a_re_flattened_map_heals_on_the_next_persist). Captured from a run of
#: that tree's restore_memory + persist_memory, not written by hand.
OLD_PERSIST_MAP = (
    "# Memory map\n"
    "\n"
    "- [dev](areas/dev.md) — read when dev\n"
    "- [New fact](project_new_fact.md) — hook for New fact.\n"
    "- [selfdev-pythonpath-gotcha](project_selfdev_pythonpath_gotcha.md) — bare pytest "
    "imports the operational install, prefix PYTHONPATH with the checkout's src/\n"
    "- [operator-prefers-terse-replies](user_operator_prefers_terse_replies.md) — short "
    "answers first, detail on request\n"
)


def _entry_lines(text):
    return [line for line in text.splitlines() if line.startswith("- [")]


# ---------------------------------------------------------------------------
# Shape is the switch
# ---------------------------------------------------------------------------


class TestFlatScope:
    def test_a_flat_persist_keeps_entries_in_memory_md_and_reports(
        self, legacy_tree, capsys
    ):
        tree, agent_dir = legacy_tree
        assert memory.restore_memory() == 0
        _add_memory(agent_dir, "project_new_fact.md")
        rc, committed = _persist()
        assert rc == 0 and committed
        index = (tree.project_memory_dir / memory.MEMORY_INDEX).read_text()
        assert "](project_new_fact.md)" in index
        assert not (tree.project_memory_dir / "areas").exists()
        out = capsys.readouterr().out
        assert "MEMORY.md as loaded" in out
        assert "/ 25,000 bytes" in out and "/ 200 lines" in out
        assert "flat" in out

    def test_a_flat_scope_over_budget_warns_and_still_persists(
        self, legacy_tree, capsys
    ):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        filler = "".join(f"<!-- note {i:03d} -->\n" for i in range(250))
        (scope / memory.MEMORY_INDEX).write_text(
            (scope / memory.MEMORY_INDEX).read_text() + filler
        )
        assert memory.restore_memory() == 0
        _add_memory(agent_dir, "project_new_fact.md")
        rc, committed = _persist()
        assert rc == 0 and committed
        assert "](project_new_fact.md)" in (scope / memory.MEMORY_INDEX).read_text()
        captured = capsys.readouterr()
        assert "OVER" in captured.out
        assert "areas/" in captured.err

    def test_the_report_runs_on_a_persist_with_nothing_to_write(
        self, legacy_tree, capsys
    ):
        assert memory.restore_memory() == 0
        capsys.readouterr()
        rc, _ = _persist()
        assert rc == 0
        assert "MEMORY.md as loaded" in capsys.readouterr().out


class TestSplitScope:
    @pytest.fixture
    def split_tree(self, legacy_tree):
        tree, agent_dir = legacy_tree
        _split(tree.project_memory_dir, {"dev": [PROJECT_MEMORY_FILE, USER_MEMORY_FILE]})
        return tree, agent_dir

    def test_a_persist_after_a_split_does_not_re_flatten_it(self, split_tree):
        # The prep-release persist read MEMORY.md as the whole index, so its
        # reconcile appended a generated line to the map for every memory
        # filed in an area (OLD_PERSIST_MAP); this code must not.
        tree, agent_dir = split_tree
        scope = tree.project_memory_dir
        before = _tree_bytes(scope)
        assert memory.restore_memory() == 0
        rc, _ = _persist()
        assert rc == 0
        assert _tree_bytes(scope) == before

    def test_a_re_flattened_map_heals_on_the_next_persist(self, split_tree):
        # What a stale-image persist really leaves behind in a split store.
        # OLD_PERSIST_MAP is byte-for-byte the MEMORY.md that prep-release
        # 12d5e7f3's restore + persist wrote for this fixture after the agent
        # added project_new_fact.md to the map: the agent's line, then one
        # generated line per memory already filed in areas/dev.md. Old code
        # adds no line for an area file the map already links.
        tree, agent_dir = split_tree
        scope = tree.project_memory_dir
        split_map = (scope / memory.MEMORY_INDEX).read_text()
        dev = (scope / "areas" / "dev.md").read_text()
        assert OLD_PERSIST_MAP.startswith(split_map)
        (scope / "project_new_fact.md").write_text(_memory_file("project_new_fact"))
        (scope / memory.MEMORY_INDEX).write_text(OLD_PERSIST_MAP)
        assert memory.restore_memory() == 0
        assert _persist()[0] == 0
        assert (scope / memory.MEMORY_INDEX).read_text() == split_map + memory._map_line(
            memory.UNSORTED_AREA
        )
        assert (scope / "areas" / "dev.md").read_text() == dev
        unsorted = (scope / "areas" / "unsorted.md").read_text()
        assert _entry_lines(unsorted) == [
            "- [New fact](../project_new_fact.md) — hook for New fact."
        ]

    def test_restore_hands_the_agent_the_map_and_its_areas(self, split_tree):
        tree, agent_dir = split_tree
        assert memory.restore_memory() == 0
        assert (agent_dir / memory.MEMORY_INDEX).read_text().startswith("# Memory map")
        area = (agent_dir / "areas" / "dev.md").read_text()
        assert f"](../{PROJECT_MEMORY_FILE})" in area

    def test_a_new_top_level_entry_is_filed_into_its_metadata_area(self, split_tree):
        tree, agent_dir = split_tree
        scope = tree.project_memory_dir
        assert memory.restore_memory() == 0
        before = (scope / memory.MEMORY_INDEX).read_text()
        _add_memory(agent_dir, "project_gate_fact.md", area="dev")
        rc, _ = _persist()
        assert rc == 0
        assert (scope / memory.MEMORY_INDEX).read_text() == before
        area = (scope / "areas" / "dev.md").read_text()
        assert "- [New fact](../project_gate_fact.md) — hook for New fact.\n" in area
        assert (scope / "project_gate_fact.md").is_file()

    def test_an_entry_with_no_area_goes_to_unsorted_with_a_map_line(
        self, split_tree, capsys
    ):
        tree, agent_dir = split_tree
        scope = tree.project_memory_dir
        assert memory.restore_memory() == 0
        _add_memory(agent_dir, "project_loose_fact.md")
        rc, _ = _persist()
        assert rc == 0
        unsorted = (scope / "areas" / "unsorted.md").read_text()
        assert "](../project_loose_fact.md)" in unsorted
        index = (scope / memory.MEMORY_INDEX).read_text()
        assert "- [unsorted](areas/unsorted.md)" in index
        assert "project_loose_fact" not in index
        assert "unsorted: 1 line" in capsys.readouterr().out

    def test_an_unknown_area_slug_files_to_unsorted(self, split_tree):
        tree, agent_dir = split_tree
        assert memory.restore_memory() == 0
        _add_memory(agent_dir, "project_odd_fact.md", area="Not A Slug")
        assert _persist()[0] == 0
        unsorted = (tree.project_memory_dir / "areas" / "unsorted.md").read_text()
        assert "](../project_odd_fact.md)" in unsorted

    def test_a_new_area_named_by_metadata_gets_its_own_index_and_map_line(
        self, split_tree
    ):
        tree, agent_dir = split_tree
        scope = tree.project_memory_dir
        assert memory.restore_memory() == 0
        _add_memory(agent_dir, "project_release_fact.md", area="release")
        assert _persist()[0] == 0
        assert "](../project_release_fact.md)" in (scope / "areas" / "release.md").read_text()
        assert "- [release](areas/release.md)" in (scope / memory.MEMORY_INDEX).read_text()

    def test_a_line_added_straight_to_an_area_index_merges_in(self, split_tree):
        tree, agent_dir = split_tree
        scope = tree.project_memory_dir
        assert memory.restore_memory() == 0
        _add_memory(agent_dir, "project_direct.md", into="areas/dev.md")
        assert _persist()[0] == 0
        area = (scope / "areas" / "dev.md").read_text()
        assert "](../project_direct.md)" in area
        assert f"](../{PROJECT_MEMORY_FILE})" in area

    def test_another_sessions_area_line_survives_this_persist(self, split_tree):
        tree, agent_dir = split_tree
        scope = tree.project_memory_dir
        assert memory.restore_memory() == 0
        # Another session files a memory into the same area after this restore.
        (scope / "project_theirs.md").write_text(_memory_file("project-theirs"))
        area = scope / "areas" / "dev.md"
        area.write_text(area.read_text() + "- [Theirs](../project_theirs.md) — theirs.\n")
        _add_memory(agent_dir, "project_mine.md", into="areas/dev.md")
        assert _persist()[0] == 0
        text = area.read_text()
        assert "](../project_theirs.md)" in text
        assert "](../project_mine.md)" in text

    def test_removing_a_memory_drops_its_area_line(self, split_tree):
        tree, agent_dir = split_tree
        scope = tree.project_memory_dir
        assert memory.restore_memory() == 0
        (agent_dir / PROJECT_MEMORY_FILE).unlink()
        area = agent_dir / "areas" / "dev.md"
        area.write_text(
            "".join(
                line + "\n"
                for line in area.read_text().splitlines()
                if PROJECT_MEMORY_FILE not in line
            )
        )
        assert _persist()[0] == 0
        assert not (scope / PROJECT_MEMORY_FILE).exists()
        assert PROJECT_MEMORY_FILE not in (scope / "areas" / "dev.md").read_text()
        assert f"](../{USER_MEMORY_FILE})" in (scope / "areas" / "dev.md").read_text()

    def test_a_flat_restore_persisting_into_a_split_store_does_not_duplicate(
        self, legacy_tree
    ):
        # The session restored the store while it was flat; the split migration
        # split it before this session persisted. The session still holds every
        # entry line in its top-level MEMORY.md.
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        assert memory.restore_memory() == 0
        _split(scope, {"dev": [PROJECT_MEMORY_FILE, USER_MEMORY_FILE]})
        migrated_map = (scope / memory.MEMORY_INDEX).read_text()
        _add_memory(agent_dir, "project_late.md", area="dev")
        assert _persist()[0] == 0
        assert (scope / memory.MEMORY_INDEX).read_text() == migrated_map
        area = (scope / "areas" / "dev.md").read_text()
        for name in (PROJECT_MEMORY_FILE, USER_MEMORY_FILE, "project_late.md"):
            assert area.count(f"](../{name})") == 1, name
        assert not (scope / "areas" / "unsorted.md").exists()

    def test_an_unindexed_file_gets_a_generated_entry_in_its_area(self, split_tree):
        tree, agent_dir = split_tree
        scope = tree.project_memory_dir
        (scope / "project_orphan.md").write_text(
            _memory_file("project-orphan", area="dev", description="an orphan")
        )
        assert memory.restore_memory() == 0
        _add_memory(agent_dir, "project_trigger.md", area="dev")
        assert _persist()[0] == 0
        area = (scope / "areas" / "dev.md").read_text()
        assert "- [project-orphan](../project_orphan.md) — an orphan\n" in area
        assert "project_orphan" not in (scope / memory.MEMORY_INDEX).read_text()

    def test_map_lines_are_not_entries_and_are_never_tombstoned(self, split_tree):
        tree, agent_dir = split_tree
        scope = tree.project_memory_dir
        assert memory.restore_memory() == 0
        _add_memory(agent_dir, "project_trigger.md", area="dev")
        assert _persist()[0] == 0
        index = (scope / memory.MEMORY_INDEX).read_text()
        assert "- [dev](areas/dev.md) — read when dev\n" in index
        assert "archived" not in index
        assert memory._entry_targets("- [dev](areas/dev.md) — read when dev", split=True) == []

    def test_area_entries_resolve_against_the_area_file(self):
        line = "- [Fact](../fact.md) — hook"
        assert memory._entry_targets(line, "areas/dev.md", split=True) == ["fact.md"]
        # A link out of the scope is a dead entry, as it is in MEMORY.md: the
        # reconcile tombstones it rather than letting it pass as commentary.
        assert memory._entry_targets("- [Out](../../x.md)", "areas/dev.md", split=True) == ["../x.md"]


# ---------------------------------------------------------------------------
# The budget
# ---------------------------------------------------------------------------


@pytest.fixture
def tiny_budget(monkeypatch):
    """A 400-byte, 12-line budget so a test index can cross it cheaply."""
    tight = MemoryIndexLimit(max_bytes=400, max_lines=12)
    monkeypatch.setitem(
        harness.HARNESSES,
        "claude",
        dataclasses.replace(HARNESSES["claude"], memory_index_limit=tight),
    )
    return tight


def _tree_bytes(directory):
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


class TestBudget:
    @pytest.fixture
    def over_split_tree(self, legacy_tree, tiny_budget):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE, USER_MEMORY_FILE]})
        index = scope / memory.MEMORY_INDEX
        index.write_text(index.read_text() + "".join(f"note {i}\n" for i in range(12)))
        return tree, agent_dir

    def test_a_split_persist_that_grows_an_over_budget_map_is_refused(
        self, over_split_tree, capsys
    ):
        tree, agent_dir = over_split_tree
        scope = tree.project_memory_dir
        assert memory.restore_memory() == 0
        manifest_before = memory.restore_manifest_path().read_text()
        before = _tree_bytes(scope)
        (agent_dir / "project_new.md").write_text(_memory_file("project-new", area="dev"))
        index = agent_dir / memory.MEMORY_INDEX
        index.write_text(index.read_text() + "A long commentary line nobody needs.\n")
        rc, committed = _persist()
        assert rc != 0 and not committed
        assert _tree_bytes(scope) == before
        assert memory.restore_manifest_path().read_text() == manifest_before
        err = capsys.readouterr().err
        assert "Refusing" in err
        assert "areas/<slug>.md" in err
        assert "A long commentary line nobody needs." in err

    def test_a_split_persist_that_does_not_grow_the_map_goes_through(
        self, over_split_tree, capsys
    ):
        tree, agent_dir = over_split_tree
        scope = tree.project_memory_dir
        assert memory.restore_memory() == 0
        _add_memory(agent_dir, "project_new.md", area="dev")
        rc, committed = _persist()
        assert rc == 0 and committed
        assert "](../project_new.md)" in (scope / "areas" / "dev.md").read_text()
        assert "OVER" in capsys.readouterr().out

    def test_a_flat_scope_over_the_budget_is_never_refused(
        self, legacy_tree, tiny_budget, capsys
    ):
        tree, agent_dir = legacy_tree
        assert memory.restore_memory() == 0
        for i in range(10):
            _add_memory(agent_dir, f"project_n{i}.md", title=f"Fact {i}")
        rc, committed = _persist()
        assert rc == 0 and committed
        assert "OVER" in capsys.readouterr().out

    def test_the_report_names_where_the_load_stops(self, legacy_tree, tiny_budget, capsys):
        tree, agent_dir = legacy_tree
        assert memory.restore_memory() == 0
        for i in range(10):
            _add_memory(agent_dir, f"project_n{i}.md", title=f"Fact {i}")
        assert _persist()[0] == 0
        loaded = (tree.project_memory_dir / memory.MEMORY_INDEX).read_text()
        shown = memory._lines_within(loaded, tiny_budget)
        assert 0 < shown < len(loaded.splitlines())
        assert f"only the first {shown} line" in capsys.readouterr().out

    def test_restore_warns_when_the_index_is_over_budget(
        self, legacy_tree, tiny_budget, capsys
    ):
        tree, agent_dir = legacy_tree
        index = tree.project_memory_dir / memory.MEMORY_INDEX
        index.write_text(index.read_text() + "".join(f"note {i}\n" for i in range(20)))
        assert memory.restore_memory() == 0
        assert "over the session-start load" in capsys.readouterr().err

    def test_lines_within_counts_bytes_not_characters(self):
        limit = MemoryIndexLimit(max_bytes=10, max_lines=100)
        # Four lines of "é\n": 3 bytes each but 2 characters.
        assert memory._lines_within("é\n" * 4, limit) == 3


# ---------------------------------------------------------------------------
# Schema form: both scopes split independently, restore merges area indexes
# ---------------------------------------------------------------------------


class TestSchemaForm:
    def test_area_lines_route_with_their_files_and_restore_merges_them(
        self, schema_tree
    ):
        tree, agent_dir = schema_tree
        _split(tree.instance_memory_dir, {"dev": [USER_MEMORY_FILE]})
        _split(tree.project_memory_dir, {"dev": [PROJECT_MEMORY_FILE]})
        assert memory.restore_memory() == 0
        area = (agent_dir / "areas" / "dev.md").read_text()
        assert f"](../{USER_MEMORY_FILE})" in area
        assert f"](../{PROJECT_MEMORY_FILE})" in area
        assert (agent_dir / memory.MEMORY_INDEX).read_text().count("areas/dev.md") == 1

        _add_memory(agent_dir, "feedback_new_rule.md", kind="feedback", area="dev")
        _add_memory(agent_dir, "project_new_fact.md", kind="project", area="dev")
        assert _persist()[0] == 0
        instance_area = (tree.instance_memory_dir / "areas" / "dev.md").read_text()
        project_area = (tree.project_memory_dir / "areas" / "dev.md").read_text()
        assert "](../feedback_new_rule.md)" in instance_area
        assert "project_new_fact" not in instance_area
        assert "](../project_new_fact.md)" in project_area
        assert "feedback_new_rule" not in project_area
        assert f"](../{USER_MEMORY_FILE})" in instance_area
        assert f"](../{PROJECT_MEMORY_FILE})" in project_area

    def test_forget_tombstones_the_line_in_its_area_and_revive_restores_it(
        self, schema2_tree
    ):
        tree, agent_dir = schema2_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE]})
        key = (scope / PROJECT_MEMORY_FILE).relative_to(tree.root).as_posix()
        area = scope / "areas" / "dev.md"
        original = area.read_text()

        paths = memory.forget_memory(key)
        assert (scope / "areas" / "dev.md").relative_to(tree.root).as_posix() in paths
        text = area.read_text()
        assert f"](../{PROJECT_MEMORY_FILE})" not in text
        assert "_archive/" in text
        state = json.loads((tree.root / memory.RETIREMENT_STATE_NAME).read_text())
        assert state["retired"][key]["index"] == "areas/dev.md"

        memory.revive_memory(key)
        assert f"](../{PROJECT_MEMORY_FILE})" in area.read_text()
        assert area.read_text().count(PROJECT_MEMORY_FILE) == original.count(
            PROJECT_MEMORY_FILE
        )


# ---------------------------------------------------------------------------
# Review round 1: a flat scope stays flat, deletions carry, refusals explain
# ---------------------------------------------------------------------------


class TestFlatStaysFlat:
    def test_a_split_project_never_splits_a_flat_instance(self, schema_tree):
        tree, agent_dir = schema_tree
        _split(tree.project_memory_dir, {"tooling": [PROJECT_MEMORY_FILE]})
        assert memory.restore_memory() == 0
        _add_memory(agent_dir, "feedback_new_rule.md", kind="feedback")
        assert _persist()[0] == 0
        instance = tree.instance_memory_dir
        assert not (instance / "areas").exists()
        index = (instance / memory.MEMORY_INDEX).read_text()
        assert f"]({USER_MEMORY_FILE})" in index
        assert "](feedback_new_rule.md)" in index
        assert "areas/" not in index

    def test_an_area_filed_into_a_flat_scope_is_ignored_out_loud(self, legacy_tree, capsys):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        assert memory.restore_memory() == 0
        _add_memory(agent_dir, "project_eager.md", into="areas/dev.md")
        assert _persist()[0] == 0
        assert not (scope / "areas").exists()
        assert "](project_eager.md)" in (scope / memory.MEMORY_INDEX).read_text()
        assert "areas/dev.md not persisted" in capsys.readouterr().err

    def test_a_flat_instance_over_budget_is_not_refused_beside_a_split_project(
        self, schema_tree, tiny_budget
    ):
        tree, agent_dir = schema_tree
        _split(tree.project_memory_dir, {"dev": [PROJECT_MEMORY_FILE]})
        instance = tree.instance_memory_dir / memory.MEMORY_INDEX
        instance.write_text(instance.read_text() + "".join(f"note {i}\n" for i in range(15)))
        assert memory.restore_memory() == 0
        _add_memory(agent_dir, "feedback_new_rule.md", kind="feedback")
        rc, committed = _persist()
        assert rc == 0 and committed


class TestAreaLifecycle:
    def test_deleting_a_restored_area_carries_through(self, legacy_tree):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"old": [PROJECT_MEMORY_FILE], "dev": [USER_MEMORY_FILE]})
        assert memory.restore_memory() == 0
        old = agent_dir / "areas" / "old.md"
        moved = [line for line in old.read_text().splitlines() if line.startswith("- [")]
        dev = agent_dir / "areas" / "dev.md"
        dev.write_text(dev.read_text() + "".join(line + "\n" for line in moved))
        old.unlink()
        index = agent_dir / memory.MEMORY_INDEX
        index.write_text(
            "".join(
                line + "\n" for line in index.read_text().splitlines()
                if "areas/old.md" not in line
            )
        )
        assert _persist()[0] == 0
        assert not (scope / "areas" / "old.md").exists()
        assert "areas/old.md" not in (scope / memory.MEMORY_INDEX).read_text()
        assert (scope / "areas" / "dev.md").read_text().count(PROJECT_MEMORY_FILE) == 1

    def test_a_deleted_area_another_session_filed_into_survives(self, legacy_tree):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"old": [PROJECT_MEMORY_FILE], "dev": [USER_MEMORY_FILE]})
        assert memory.restore_memory() == 0
        (scope / "project_theirs.md").write_text(_memory_file("project-theirs"))
        area = scope / "areas" / "old.md"
        area.write_text(area.read_text() + "- [Theirs](../project_theirs.md) — theirs.\n")
        (agent_dir / "areas" / "old.md").unlink()
        assert _persist()[0] == 0
        text = area.read_text()
        assert "](../project_theirs.md)" in text
        assert PROJECT_MEMORY_FILE not in text

    def test_a_map_line_to_a_missing_area_is_tombstoned(self, legacy_tree):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE, USER_MEMORY_FILE]})
        index = scope / memory.MEMORY_INDEX
        index.write_text(index.read_text() + "- [ops](areas/ops.md) — read for ops\n")
        assert memory.restore_memory() == 0
        assert _persist()[0] == 0
        text = index.read_text()
        # The convention of every index tombstone: the link becomes the date.
        assert "- archived " in text and "— read for ops" in text
        assert "areas/ops.md" not in text
        assert "- [dev](areas/dev.md) — read when dev\n" in text

    def test_a_map_entry_for_a_filed_memory_is_dropped_out_loud(self, legacy_tree, capsys):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE, USER_MEMORY_FILE]})
        assert memory.restore_memory() == 0
        index = agent_dir / memory.MEMORY_INDEX
        index.write_text(index.read_text() + f"- [Gotcha]({PROJECT_MEMORY_FILE}) — NEW HOOK\n")
        assert _persist()[0] == 0
        assert "NEW HOOK" not in (scope / memory.MEMORY_INDEX).read_text()
        err = capsys.readouterr().err
        assert "already filed in an area index" in err
        assert "NEW HOOK" in err


class TestRefusalDetail:
    @pytest.fixture
    def over_split_tree(self, legacy_tree, tiny_budget):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE, USER_MEMORY_FILE]})
        index = scope / memory.MEMORY_INDEX
        index.write_text(index.read_text() + "".join(f"note {i}\n" for i in range(12)))
        return tree, agent_dir

    def test_a_new_areas_map_line_is_named_as_such(self, over_split_tree, capsys):
        tree, agent_dir = over_split_tree
        assert memory.restore_memory() == 0
        _add_memory(agent_dir, "project_rel.md", area="release", into="areas/release.md")
        rc, committed = _persist()
        assert rc != 0 and not committed
        err = capsys.readouterr().err
        assert "creates a new area" in err
        assert "- [release](areas/release.md)" in err
        assert "move these lines out of MEMORY.md" not in err

    def test_rollback_keeps_directories_that_were_there(self, over_split_tree):
        tree, agent_dir = over_split_tree
        scope = tree.project_memory_dir
        (scope / "emptydir").mkdir()
        assert memory.restore_memory() == 0
        before = _tree_bytes(scope)
        _add_memory(agent_dir, "project_rel.md", area="release", into="areas/release.md")
        assert _persist()[0] != 0
        assert _tree_bytes(scope) == before
        assert (scope / "emptydir").is_dir()
        assert not (scope / "areas" / "release.md").exists()


# ---------------------------------------------------------------------------
# Writers that disagree with this one: older code, staler sessions (#435 r1)
# ---------------------------------------------------------------------------


class TestOlderAndStalerWriters:
    def test_an_old_format_manifest_never_deletes_an_area_index(self, legacy_tree):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE], "ops": [USER_MEMORY_FILE]})
        before = _tree_bytes(scope)
        assert memory.restore_memory() == 0
        # Rewrite the manifest the way prep-release recorded it: area indexes
        # listed under `files` as if they were memories, no `areas` key.
        path = memory.restore_manifest_path()
        payload = json.loads(path.read_text())
        for entry in payload["scopes"].values():
            for rel in entry.pop("areas", {}):
                entry["files"][rel] = memory._digest(scope / rel)
        path.write_text(json.dumps(payload))
        assert _persist()[0] == 0
        assert _tree_bytes(scope) == before

    def test_a_stale_session_does_not_recreate_an_area_another_deleted(
        self, legacy_tree
    ):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE], "ops": [USER_MEMORY_FILE]})
        assert memory.restore_memory() == 0
        # Another session moves dev's entry into ops and deletes dev.
        dev, ops = scope / "areas" / "dev.md", scope / "areas" / "ops.md"
        ops.write_text(ops.read_text() + "".join(
            line + "\n" for line in _entry_lines(dev.read_text())
        ))
        dev.unlink()
        index = scope / memory.MEMORY_INDEX
        index.write_text(index.read_text().replace("- [dev](areas/dev.md) — read when dev\n", ""))
        after_them = _tree_bytes(scope)
        # This session changed nothing, and persists.
        assert _persist()[0] == 0
        assert not dev.exists()
        assert _tree_bytes(scope) == after_them

    def test_a_stale_session_does_not_refill_an_area_another_emptied(self, legacy_tree):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE], "ops": [USER_MEMORY_FILE]})
        assert memory.restore_memory() == 0
        dev, ops = scope / "areas" / "dev.md", scope / "areas" / "ops.md"
        ops.write_text(ops.read_text() + "".join(
            line + "\n" for line in _entry_lines(dev.read_text())
        ))
        dev.write_text("")
        assert _persist()[0] == 0
        assert PROJECT_MEMORY_FILE not in dev.read_text()
        assert ops.read_text().count(PROJECT_MEMORY_FILE) == 1

    def test_a_stale_sessions_new_line_still_lands_in_a_deleted_area(self, legacy_tree):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE], "ops": [USER_MEMORY_FILE]})
        assert memory.restore_memory() == 0
        dev, ops = scope / "areas" / "dev.md", scope / "areas" / "ops.md"
        ops.write_text(ops.read_text() + "".join(
            line + "\n" for line in _entry_lines(dev.read_text())
        ))
        dev.unlink()
        _add_memory(agent_dir, "project_mine.md", into="areas/dev.md")
        assert _persist()[0] == 0
        assert _entry_lines(dev.read_text()) == [
            "- [New fact](../project_mine.md) — hook for New fact."
        ]
        assert ops.read_text().count(PROJECT_MEMORY_FILE) == 1


class TestMapLineHooks:
    def test_a_hook_that_links_a_memory_stays_part_of_the_map_line(self, legacy_tree):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE, USER_MEMORY_FILE]})
        index = scope / memory.MEMORY_INDEX
        hooked = (
            f"- [dev](areas/dev.md) — read when dev; start with "
            f"[Gotcha]({PROJECT_MEMORY_FILE})\n"
        )
        index.write_text(index.read_text().replace(
            "- [dev](areas/dev.md) — read when dev\n", hooked
        ))
        before = _tree_bytes(scope)
        assert memory.restore_memory() == 0
        assert _persist()[0] == 0
        assert _tree_bytes(scope) == before
        assert memory._entry_targets(hooked, split=True) == []

    def test_generated_map_lines_carry_a_read_when_hook(self):
        assert memory._map_line("unsorted") == (
            "- [unsorted](areas/unsorted.md) — read when no other area fits: "
            "memories not yet filed into one\n"
        )
        line = memory._map_line("release-train")
        assert line.startswith("- [release-train](areas/release-train.md) — read when ")
        assert "release train" in line
        assert memory._MAP_LINE_RE.match(line.rstrip("\n"))

    def test_an_area_cross_reference_keeps_the_lines_memory_entry(self):
        line = "- [Gotcha](../gotcha.md) — see also [ops](ops.md)"
        assert memory._entry_targets(line, "areas/dev.md", split=True) == ["gotcha.md"]


class TestMigrationContract:
    def test_an_area_slug_is_matched_whole(self):
        assert is_area_slug("dev")
        for bad in ("dev\n", "Dev", "-dev", "dev ops", "", None):
            assert not is_area_slug(bad)
        assert not memory._is_area_index("areas/dev\n.md")

    def test_a_newline_area_in_metadata_files_to_unsorted(self, legacy_tree):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE, USER_MEMORY_FILE]})
        assert memory.restore_memory() == 0
        _add_memory(agent_dir, "project_nl.md")
        path = agent_dir / "project_nl.md"
        path.write_text(path.read_text().replace(
            "  type: project\n", '  type: project\n  area: "dev\\n"\n'
        ))
        assert memory._memory_area(path) == memory.UNSORTED_AREA
        assert _persist()[0] == 0
        assert "](../project_nl.md)" in (scope / "areas" / "unsorted.md").read_text()
        assert sorted(p.name for p in (scope / "areas").iterdir()) == ["dev.md", "unsorted.md"]

    def test_an_area_link_missing_its_dot_dot_is_refused_until_fixed(
        self, legacy_tree, capsys
    ):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE, USER_MEMORY_FILE]})
        area = scope / "areas" / "dev.md"
        area.write_text(area.read_text().replace(
            f"](../{PROJECT_MEMORY_FILE})", f"]({PROJECT_MEMORY_FILE})"
        ))
        before = _tree_bytes(scope)
        assert memory.restore_memory() == 0
        _add_memory(agent_dir, "project_new.md", area="dev")
        rc, committed = _persist()
        assert rc == 1 and not committed
        assert _tree_bytes(scope) == before
        err = capsys.readouterr().err
        assert "not migrated cleanly" in err
        assert f"`{PROJECT_MEMORY_FILE}` resolves to areas/{PROJECT_MEMORY_FILE}" in err
        assert "(your memory dir)" in err
        # The session was handed the line, so it can fix it in its own copy.
        local = agent_dir / "areas" / "dev.md"
        local.write_text(local.read_text().replace(
            f"]({PROJECT_MEMORY_FILE})", f"](../{PROJECT_MEMORY_FILE})"
        ))
        assert _persist()[0] == 0
        fixed = area.read_text()
        assert f"](../{PROJECT_MEMORY_FILE})" in fixed
        assert f"]({PROJECT_MEMORY_FILE})" not in fixed
        assert (scope / "project_new.md").is_file()

    def test_a_bad_link_this_session_never_held_warns_and_persists(
        self, legacy_tree, capsys
    ):
        # Another writer breaks a line after this session restored: no local
        # edit can reach it, so refusing would block this session for good.
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE, USER_MEMORY_FILE]})
        assert memory.restore_memory() == 0
        area = scope / "areas" / "dev.md"
        area.write_text(area.read_text().replace(
            f"](../{PROJECT_MEMORY_FILE})", f"]({PROJECT_MEMORY_FILE})"
        ))
        _add_memory(agent_dir, "project_new.md", area="dev")
        rc, committed = _persist()
        assert rc == 0 and committed
        assert (scope / "project_new.md").is_file()
        err = capsys.readouterr().err
        assert "never held" in err and "areas/dev.md: `" in err
        assert "(your memory dir)" not in err
        # The dead link is tombstoned and the memory keeps exactly one entry.
        text = (scope / "areas" / "dev.md").read_text()
        assert f"]({PROJECT_MEMORY_FILE})" not in text
        indexed = [
            line for rel in (scope / "areas").iterdir()
            for line in rel.read_text().splitlines()
            if f"../{PROJECT_MEMORY_FILE})" in line
        ]
        assert len(indexed) == 1

    def test_a_session_writing_a_bad_area_link_is_refused_and_writes_nothing(
        self, legacy_tree, capsys
    ):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE, USER_MEMORY_FILE]})
        before = _tree_bytes(scope)
        assert memory.restore_memory() == 0
        (agent_dir / "project_mine.md").write_text(_memory_file("project-mine"))
        local = agent_dir / "areas" / "dev.md"
        local.write_text(local.read_text() + "- [Mine](project_mine.md) — mine.\n")
        assert _persist()[0] == 1
        assert _tree_bytes(scope) == before
        assert "(your memory dir)" in capsys.readouterr().err

    def test_a_non_slug_area_file_is_refused_until_removed(self, legacy_tree, capsys):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE, USER_MEMORY_FILE]})
        (scope / "areas" / "Ops Notes.md").write_text("# ops\n")
        before = _tree_bytes(scope)
        assert memory.restore_memory() == 0
        assert _persist()[0] == 1
        assert _tree_bytes(scope) == before
        err = capsys.readouterr().err
        assert "areas/Ops Notes.md (your memory dir): not an area index" in err
        assert err.count("Ops Notes.md") == 1
        # Restored and then removed: the persist deletes it upstream too.
        (agent_dir / "areas" / "Ops Notes.md").unlink()
        assert _persist()[0] == 0
        assert not (scope / "areas" / "Ops Notes.md").exists()


class TestCrossAreaLinks:
    """A link from one area index to another names no memory: never refused.

    It is a dead link once its area is gone, and tombstoned like one.
    """

    def test_a_cross_link_to_an_area_another_session_deleted_never_blocks(
        self, legacy_tree
    ):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE], "ops": [USER_MEMORY_FILE]})
        assert memory.restore_memory() == 0
        local = agent_dir / "areas" / "dev.md"
        local.write_text(local.read_text() + "See also [ops](ops.md).\n")
        assert _persist()[0] == 0
        assert "[ops](ops.md)" in (scope / "areas" / "dev.md").read_text()
        # Another session retires the ops area: its entry moves to dev.
        ops = scope / "areas" / "ops.md"
        dev = scope / "areas" / "dev.md"
        dev.write_text(dev.read_text() + "".join(
            line + "\n" for line in _entry_lines(ops.read_text())
        ))
        ops.unlink()
        index = scope / memory.MEMORY_INDEX
        index.write_text(index.read_text().replace("- [ops](areas/ops.md) — read when ops\n", ""))
        # Every later session still persists, with and without changes.
        assert memory.restore_memory() == 0
        assert _persist()[0] == 0
        _add_memory(agent_dir, "project_later.md", area="dev")
        assert _persist()[0] == 0
        assert "](../project_later.md)" in dev.read_text()
        assert "See also archived " in dev.read_text()

    def test_a_stale_session_linking_a_concurrently_deleted_area_blocks_no_one(
        self, legacy_tree
    ):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE], "ops": [USER_MEMORY_FILE]})
        assert memory.restore_memory() == 0
        ops, dev = scope / "areas" / "ops.md", scope / "areas" / "dev.md"
        dev.write_text(dev.read_text() + "".join(
            line + "\n" for line in _entry_lines(ops.read_text())
        ))
        ops.unlink()
        local = agent_dir / "areas" / "dev.md"
        local.write_text(local.read_text() + "See also [ops](ops.md).\n")
        assert _persist()[0] == 0
        assert memory.restore_memory() == 0
        assert _persist()[0] == 0

    def test_a_cross_link_to_an_area_in_the_other_scope_never_blocks(self, schema_tree):
        tree, agent_dir = schema_tree
        _split(tree.instance_memory_dir, {"user": [USER_MEMORY_FILE]})
        _split(tree.project_memory_dir, {"dev": [PROJECT_MEMORY_FILE]})
        assert memory.restore_memory() == 0
        local = agent_dir / "areas" / "dev.md"
        local.write_text(local.read_text() + "See also [user area](user.md).\n")
        assert _persist()[0] == 0
        assert "[user area](user.md)" in (
            tree.project_memory_dir / "areas" / "dev.md"
        ).read_text()
        assert memory.restore_memory() == 0
        assert _persist()[0] == 0
        _add_memory(agent_dir, "project_later.md", area="dev")
        assert _persist()[0] == 0
        assert (tree.project_memory_dir / "project_later.md").is_file()


class TestFlatIgnoresAreas:
    def test_a_bad_local_area_never_refuses_a_flat_persist(self, legacy_tree):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        assert memory.restore_memory() == 0
        (agent_dir / "project_alpha.md").write_text(_memory_file("project-alpha"))
        (agent_dir / "areas").mkdir()
        (agent_dir / "areas" / "dev.md").write_text("- [Alpha](project_alpha.md) — a\n")
        (agent_dir / "areas" / "Notes.md").write_text("# notes\n")
        assert _persist()[0] == 0
        assert "](project_alpha.md)" in (scope / memory.MEMORY_INDEX).read_text()
        assert not (scope / "areas" / "dev.md").exists()

    def test_a_non_slug_file_in_a_flat_work_repo_areas_dir_never_refuses(
        self, legacy_tree
    ):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        (scope / "areas").mkdir()
        (scope / "areas" / "Notes.md").write_text("# notes\n")
        assert memory.restore_memory() == 0
        _add_memory(agent_dir, "project_new.md")
        assert _persist()[0] == 0
        assert "](project_new.md)" in (scope / memory.MEMORY_INDEX).read_text()

    @pytest.mark.parametrize("damage", ["blank", "delete"])
    def test_a_flat_index_damaged_outside_persist_heals_from_the_session(
        self, legacy_tree, damage
    ):
        tree, agent_dir = legacy_tree
        index = tree.project_memory_dir / memory.MEMORY_INDEX
        original = index.read_text()
        assert memory.restore_memory() == 0
        if damage == "blank":
            index.write_text("")
        else:
            index.unlink()
        assert _persist()[0] == 0
        assert index.read_text() == original


class TestMapHookLinks:
    @pytest.fixture
    def hooked(self, legacy_tree):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE], "ops": [USER_MEMORY_FILE]})
        index = scope / memory.MEMORY_INDEX
        index.write_text(index.read_text().replace(
            "- [dev](areas/dev.md) — read when dev\n",
            f"- [dev](areas/dev.md) — read when dev; start with [Gotcha]({PROJECT_MEMORY_FILE})\n",
        ))
        return tree, agent_dir

    def test_a_dead_memory_link_in_a_hook_is_tombstoned(self, hooked):
        tree, agent_dir = hooked
        scope = tree.project_memory_dir
        assert memory.restore_memory() == 0
        (agent_dir / PROJECT_MEMORY_FILE).unlink()
        local = agent_dir / "areas" / "dev.md"
        local.write_text("# dev\n")
        assert _persist()[0] == 0
        text = (scope / memory.MEMORY_INDEX).read_text()
        assert PROJECT_MEMORY_FILE not in text
        assert "- [dev](areas/dev.md) — read when dev; start with archived " in text

    def test_a_tombstoned_hooked_map_line_survives_later_persists(self, hooked):
        tree, agent_dir = hooked
        scope = tree.project_memory_dir
        # Another session's migration retires the dev area: its entry moves to
        # ops, dev.md goes, the map line stays.
        dev, ops = scope / "areas" / "dev.md", scope / "areas" / "ops.md"
        ops.write_text(ops.read_text() + "".join(
            line + "\n" for line in _entry_lines(dev.read_text())
        ))
        dev.unlink()
        assert memory.restore_memory() == 0
        assert _persist()[0] == 0
        index = scope / memory.MEMORY_INDEX
        tombstoned = [line for line in index.read_text().splitlines() if "read when dev" in line]
        assert tombstoned == ["- archived " + tombstoned[0][len("- archived "):]]
        assert "start with Gotcha" in tombstoned[0] and "](" not in tombstoned[0]
        after = _tree_bytes(scope)
        assert memory.restore_memory() == 0
        assert _persist()[0] == 0
        assert _tree_bytes(scope) == after

    def test_the_validator_reads_a_hooked_map_line_as_persist_does(self):
        line = "- [dev](areas/dev.md) — read when dev; start with [Gone](gone.md)\n"
        assert parse_index_entries(line, memory_map=True) == ["areas/dev.md"]
        assert parse_index_entries(line) == ["areas/dev.md", "gone.md"]
        entry = "- [Fact](fact.md) — a fact\n"
        assert parse_index_entries(entry, memory_map=True) == ["fact.md"]


class TestPre435ManifestBase:
    def test_a_no_change_persist_does_not_put_back_a_moved_entry(self, legacy_tree):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE], "ops": [USER_MEMORY_FILE]})
        assert memory.restore_memory() == 0
        path = memory.restore_manifest_path()
        payload = json.loads(path.read_text())
        for entry in payload["scopes"].values():
            for rel in entry.pop("areas", {}):
                entry["files"][rel] = memory._digest(scope / rel)
        path.write_text(json.dumps(payload))
        # Another session moves dev's entry into ops.
        dev, ops = scope / "areas" / "dev.md", scope / "areas" / "ops.md"
        moved = _entry_lines(dev.read_text())
        ops.write_text(ops.read_text() + "".join(line + "\n" for line in moved))
        dev.write_text("# dev\n")
        after_them = _tree_bytes(scope)
        assert _persist()[0] == 0
        assert _tree_bytes(scope) == after_them

    def test_an_edited_area_under_a_pre_435_manifest_stays_additive(self, legacy_tree):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE], "ops": [USER_MEMORY_FILE]})
        assert memory.restore_memory() == 0
        path = memory.restore_manifest_path()
        payload = json.loads(path.read_text())
        for entry in payload["scopes"].values():
            for rel in entry.pop("areas", {}):
                entry["files"][rel] = memory._digest(scope / rel)
        path.write_text(json.dumps(payload))
        local = agent_dir / "areas" / "dev.md"
        local.write_text("# dev\n")  # the session dropped the line
        assert _persist()[0] == 0
        # No base for an edited copy: nothing it dropped is deleted upstream.
        assert PROJECT_MEMORY_FILE in (scope / "areas" / "dev.md").read_text()


class TestRefusalWording:
    def test_a_lengthened_hook_is_the_agents_line_not_a_new_area(
        self, legacy_tree, tiny_budget, capsys
    ):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE, USER_MEMORY_FILE]})
        index = scope / memory.MEMORY_INDEX
        index.write_text(index.read_text() + "".join(f"note {i}\n" for i in range(12)))
        assert memory.restore_memory() == 0
        local = agent_dir / memory.MEMORY_INDEX
        local.write_text(local.read_text().replace(
            "— read when dev\n", "— read when dev, gates, the suite, and CI\n"
        ))
        assert _persist()[0] == 1
        err = capsys.readouterr().err
        assert "move these lines out of MEMORY.md" in err
        assert "creates a new area" not in err


def _index_findings(tree):
    """The validator's entry/file findings for *tree*: ``(code, path, detail)``."""
    return [
        (finding.code, finding.path, finding.detail)
        for finding in validate(tree.root).findings
        if finding.code
        in ("agents.memory-entry-without-file", "agents.memory-file-without-entry")
    ]


def _with_area_line(scope, slug, line):
    """Another writer appends *line* to *scope*'s area index *slug*."""
    path = scope / "areas" / f"{slug}.md"
    path.write_text(path.read_text() + line)
    return path


class TestBothScopesMigrationCheck:
    """Schema form: the migration check reads the agent's merged view (#435 r3)."""

    @pytest.fixture
    def both_split(self, schema_tree):
        tree, agent_dir = schema_tree
        _split(tree.instance_memory_dir, {"user": [USER_MEMORY_FILE]})
        _split(tree.project_memory_dir, {"dev": [PROJECT_MEMORY_FILE]})
        return tree, agent_dir

    def test_a_missing_dot_dot_to_a_memory_in_the_other_scope_is_refused(
        self, both_split, capsys
    ):
        tree, agent_dir = both_split
        scopes = (tree.instance_memory_dir, tree.project_memory_dir)
        before = [_tree_bytes(scope) for scope in scopes]
        assert memory.restore_memory() == 0
        local = agent_dir / "areas" / "user.md"
        local.write_text(local.read_text() + f"- [Again]({USER_MEMORY_FILE}) — bad link\n")
        rc, committed = _persist()
        assert rc == 1 and not committed
        assert [_tree_bytes(scope) for scope in scopes] == before
        err = capsys.readouterr().err
        assert f"`{USER_MEMORY_FILE}` resolves to areas/{USER_MEMORY_FILE}" in err
        assert "(your memory dir)" in err

    def test_a_link_to_an_area_in_the_other_scope_survives_a_same_named_memory(
        self, both_split
    ):
        tree, agent_dir = both_split
        assert memory.restore_memory() == 0
        local = agent_dir / "areas" / "dev.md"
        local.write_text(local.read_text() + "See also [User](user.md).\n")
        _add_memory(agent_dir, "user.md", area="dev", into="areas/dev.md")
        assert _persist() == (0, True)
        text = (tree.project_memory_dir / "areas" / "dev.md").read_text()
        assert "See also [User](user.md)." in text and "](../user.md)" in text
        assert (tree.project_memory_dir / "user.md").is_file()

    def test_a_refusal_leaves_no_scope_dir_it_created(self, schema_tree, capsys):
        tree, agent_dir = schema_tree
        _split(tree.instance_memory_dir, {"user": [USER_MEMORY_FILE]})
        # Its first link routes the line to the instance scope, where the
        # second is the missing-`../` signature.
        _with_area_line(
            tree.instance_memory_dir,
            "user",
            f"- [Terse](../{USER_MEMORY_FILE}) — also [Again]({USER_MEMORY_FILE})\n",
        )
        project = tree.project_memory_dir
        for path in sorted(project.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        project.rmdir()
        before = _tree_bytes(tree.instance_memory_dir)
        assert memory.restore_memory() == 0
        _add_memory(agent_dir, "project_fresh.md")
        assert _persist() == (1, False)
        assert "(your memory dir)" in capsys.readouterr().err
        assert not project.exists()
        assert _tree_bytes(tree.instance_memory_dir) == before


class TestUnheldLines:
    """A session is never refused over a line it never held (#435 r3)."""

    def test_a_line_another_writer_added_after_restore_never_refuses(
        self, legacy_tree, capsys
    ):
        # The wedge: another writer's dangling `alpha.md` link meets this
        # session's new memory of that name, which the link now "means".
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE, USER_MEMORY_FILE]})
        assert memory.restore_memory() == 0
        dev = _with_area_line(scope, "dev", "- [Alpha notes](alpha.md) — write up later\n")
        _add_memory(agent_dir, "alpha.md", area="dev", into="areas/dev.md")
        _add_memory(agent_dir, "project_other.md", area="dev", into="areas/dev.md")
        assert _persist() == (0, True)
        assert (scope / "alpha.md").is_file() and (scope / "project_other.md").is_file()
        assert "never held" in capsys.readouterr().err
        text = dev.read_text()
        assert "](alpha.md)" not in text and "write up later" in text
        assert text.count("](../alpha.md)") == 1
        assert memory.restore_memory() == 0
        assert _persist()[0] == 0

    def test_a_link_to_an_area_that_exists_nowhere_is_tombstoned_by_its_writer(
        self, legacy_tree, tmp_path, monkeypatch
    ):
        tree, first = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE, USER_MEMORY_FILE]})
        second = tmp_path / "second-agent"
        second.mkdir()
        monkeypatch.setenv("LMER_AGENT_MEMORY_DIR", str(second))
        assert memory.restore_memory() == 0
        monkeypatch.setenv("LMER_AGENT_MEMORY_DIR", str(first))
        assert memory.restore_memory() == 0
        local = first / "areas" / "dev.md"
        local.write_text(local.read_text() + "- [Alpha notes](alpha.md) — write up later\n")
        assert _persist()[0] == 0
        dev = scope / "areas" / "dev.md"
        assert "](alpha.md)" not in dev.read_text()
        assert "write up later" in dev.read_text()
        # A second session's slug-named memory meets nothing it must answer for.
        monkeypatch.setenv("LMER_AGENT_MEMORY_DIR", str(second))
        _add_memory(second, "alpha.md", area="dev", into="areas/dev.md")
        assert _persist() == (0, True)
        assert dev.read_text().count("](../alpha.md)") == 1


class TestHookLinksAcrossScopes:
    """A map hook may link what this scope does not hold (#435 r3)."""

    @pytest.mark.parametrize("spelling", ["bare", "relative"])
    def test_a_hook_link_to_a_memory_in_the_other_scope_is_kept(
        self, schema_tree, spelling
    ):
        tree, agent_dir = schema_tree
        _split(tree.instance_memory_dir, {"user": [USER_MEMORY_FILE]})
        _split(tree.project_memory_dir, {"dev": [PROJECT_MEMORY_FILE]})
        index = tree.project_memory_dir / memory.MEMORY_INDEX
        target = USER_MEMORY_FILE
        if spelling == "relative":
            target = os.path.relpath(
                tree.instance_memory_dir / USER_MEMORY_FILE, tree.project_memory_dir
            )
        hook = f"- [dev](areas/dev.md) — read when dev; start with [Terse]({target})\n"
        index.write_text(index.read_text().replace("- [dev](areas/dev.md) — read when dev\n", hook))
        assert memory.restore_memory() == 0
        assert _persist()[0] == 0
        assert hook in index.read_text()
        assert memory.restore_memory() == 0
        assert _persist()[0] == 0
        assert hook in index.read_text()

    def test_a_hook_link_to_a_document_outside_the_memory_set_is_kept(self, legacy_tree):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE, USER_MEMORY_FILE]})
        (scope.parent / "notes.md").write_text("# notes\n")
        index = scope / memory.MEMORY_INDEX
        hook = "- [dev](areas/dev.md) — read when dev; [Notes](../notes.md)\n"
        index.write_text(index.read_text().replace("- [dev](areas/dev.md) — read when dev\n", hook))
        assert memory.restore_memory() == 0
        assert _persist()[0] == 0
        assert hook in index.read_text()


class TestValidatorAgreesOnAreaLinks:
    """What persist leaves behind validates clean (#435 r3, agents/work#8)."""

    def test_a_link_to_an_area_in_the_other_scope_validates(self, schema_tree):
        tree, agent_dir = schema_tree
        _split(tree.instance_memory_dir, {"user": [USER_MEMORY_FILE]})
        _split(tree.project_memory_dir, {"dev": [PROJECT_MEMORY_FILE]})
        assert memory.restore_memory() == 0
        local = agent_dir / "areas" / "dev.md"
        local.write_text(local.read_text() + "See also [user area](user.md).\n")
        assert _persist()[0] == 0
        assert "[user area](user.md)" in (
            tree.project_memory_dir / "areas" / "dev.md"
        ).read_text()
        assert _index_findings(tree) == []

    def test_a_link_to_a_deleted_area_is_tombstoned_and_validates(self, legacy_tree):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE], "ops": [USER_MEMORY_FILE]})
        assert memory.restore_memory() == 0
        local = agent_dir / "areas" / "dev.md"
        local.write_text(local.read_text() + "See also [ops](ops.md).\n")
        assert _persist()[0] == 0
        # Another session retires the ops area, filing its entry into dev.
        ops, dev = scope / "areas" / "ops.md", scope / "areas" / "dev.md"
        dev.write_text(dev.read_text() + "".join(
            line + "\n" for line in _entry_lines(ops.read_text())
        ))
        ops.unlink()
        index = scope / memory.MEMORY_INDEX
        index.write_text(index.read_text().replace("- [ops](areas/ops.md) — read when ops\n", ""))
        assert memory.restore_memory() == 0
        assert _persist()[0] == 0
        assert "See also archived " in dev.read_text()
        assert _index_findings(tree) == []


class TestFlatAreaLinkIsAnEntryLink:
    """A flat scope ignores ``areas/``, as 12d5e7f3 did (#435 r3)."""

    def test_an_entry_that_also_links_an_area_keeps_its_memory_link(self, legacy_tree):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        index = scope / memory.MEMORY_INDEX
        lines = index.read_text().splitlines(keepends=True)
        entry = next(line for line in lines if f"]({PROJECT_MEMORY_FILE})" in line)
        related = entry.rstrip("\n") + " Related: [dev notes](areas/dev.md)\n"
        index.write_text(index.read_text().replace(entry, related))
        assert memory.restore_memory() == 0
        assert _persist()[0] == 0
        text = index.read_text()
        tombstoned = related.replace("[dev notes](areas/dev.md)", "archived ")
        assert tombstoned.split("archived ")[0] in text  # the memory link survives
        assert "Related: archived " in text
        assert text.count(f"]({PROJECT_MEMORY_FILE})") == 1
        assert not (scope / "areas").exists()
        assert _index_findings(tree) == []

    def test_the_validator_reads_a_flat_area_link_as_an_entry_link(self, legacy_tree):
        tree, _agent_dir = legacy_tree
        index = tree.project_memory_dir / memory.MEMORY_INDEX
        entry = next(
            line for line in index.read_text().splitlines(keepends=True)
            if f"]({PROJECT_MEMORY_FILE})" in line
        )
        index.write_text(index.read_text().replace(
            entry, entry.rstrip("\n") + " Related: [dev notes](areas/dev.md)\n"
        ))
        # The dead area link is the finding; the memory is still indexed.
        assert [(code, detail) for code, _path, detail in _index_findings(tree)] == [
            ("agents.memory-entry-without-file", "areas/dev.md")
        ]


# ---------------------------------------------------------------------------
# Review round 4: forget tombstones, the shared instance, the end state, mixed
# form and hooks across scopes
# ---------------------------------------------------------------------------


class TestForgetTombstonesStayVerbatim:
    def test_a_forget_tombstone_survives_its_cross_referenced_areas_deletion(
        self, schema2_tree
    ):
        tree, agent_dir = schema2_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE]})
        dev = scope / "areas" / "dev.md"
        dev.write_text("".join(
            line.rstrip("\n") + " See also [ops](ops.md).\n"
            if f"](../{PROJECT_MEMORY_FILE})" in line else line
            for line in dev.read_text().splitlines(keepends=True)
        ))
        (scope / "areas" / "ops.md").write_text("# ops\n")
        index = scope / memory.MEMORY_INDEX
        ops_line = "- [ops](areas/ops.md) — read when ops\n"
        index.write_text(index.read_text() + ops_line)
        key = (scope / PROJECT_MEMORY_FILE).relative_to(tree.root).as_posix()
        memory.forget_memory(key)
        tombstone = next(line for line in dev.read_text().splitlines() if "_archive/" in line)
        assert tombstone.endswith("See also [ops](ops.md).")
        (scope / "areas" / "ops.md").unlink()
        index.write_text(index.read_text().replace(ops_line, ""))

        assert memory.restore_memory() == 0
        _add_memory(agent_dir, "project_unrelated.md", area="dev", into="areas/dev.md")
        assert _persist()[0] == 0
        assert tombstone in dev.read_text().splitlines()
        # Kept verbatim, its dead `See also` is no validator finding (#601).
        assert _index_findings(tree) == []

        memory.revive_memory(key)
        text = dev.read_text()
        assert "_archive/" not in text
        assert text.count(f"](../{PROJECT_MEMORY_FILE})") == 1


def _session_for(monkeypatch, tree, project_scope):
    """Point this session's project scope at *project_scope* (another project)."""
    monkeypatch.setattr(
        memory,
        "memory_scope_dirs",
        lambda: {
            memory.PROJECT_SCOPE: project_scope,
            memory.INSTANCE_SCOPE: tree.instance_memory_dir,
        },
    )


class TestTheSharedInstanceScope:
    """The instance scope is judged against every project's scope (#435 r4)."""

    def test_another_projects_no_change_persist_keeps_live_instance_links(
        self, schema_tree, monkeypatch
    ):
        tree, _agent_dir = schema_tree
        _split(tree.instance_memory_dir, {"user": [USER_MEMORY_FILE]})
        _split(tree.project_memory_dir, {"dev": [PROJECT_MEMORY_FILE]})
        user = _with_area_line(tree.instance_memory_dir, "user", "See also [dev notes](dev.md).\n")
        index = tree.instance_memory_dir / memory.MEMORY_INDEX
        hook = (
            "- [user](areas/user.md) — read when user; "
            f"start with [Gotcha]({PROJECT_MEMORY_FILE})\n"
        )
        index.write_text(index.read_text().replace("- [user](areas/user.md) — read when user\n", hook))
        _session_for(monkeypatch, tree, tree.repoless_dir / "_agents" / "memory")
        assert memory.restore_memory() == 0
        assert _persist()[0] == 0
        assert "See also [dev notes](dev.md)." in user.read_text()
        assert hook in index.read_text()
        assert _index_findings(tree) == []


class TestJudgedOnTheEndState:
    """Links are judged against what the persist leaves behind (#435 r4)."""

    def test_a_link_to_an_area_deleted_after_restore_is_tombstoned_by_its_writer(
        self, legacy_tree
    ):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE], "ops": [USER_MEMORY_FILE]})
        assert memory.restore_memory() == 0
        # Another session retires the ops area, filing its entry into dev.
        ops, dev = scope / "areas" / "ops.md", scope / "areas" / "dev.md"
        dev.write_text(dev.read_text() + "".join(
            line + "\n" for line in _entry_lines(ops.read_text())
        ))
        ops.unlink()
        index = scope / memory.MEMORY_INDEX
        index.write_text(index.read_text().replace("- [ops](areas/ops.md) — read when ops\n", ""))
        # This session still holds its copy of ops, and links it.
        local = agent_dir / "areas" / "dev.md"
        local.write_text(local.read_text() + "See also [ops](ops.md).\n")
        assert _persist()[0] == 0
        assert not ops.exists()
        assert "See also archived " in dev.read_text()
        assert _index_findings(tree) == []

    def test_a_hook_link_to_a_memory_deleted_from_a_later_scope_is_tombstoned(
        self, schema_tree
    ):
        tree, agent_dir = schema_tree
        _split(tree.instance_memory_dir, {"user": [USER_MEMORY_FILE]})
        _split(tree.project_memory_dir, {"dev": [PROJECT_MEMORY_FILE]})
        index = tree.project_memory_dir / memory.MEMORY_INDEX
        hook = f"- [dev](areas/dev.md) — read when dev; start with [User]({USER_MEMORY_FILE})\n"
        index.write_text(index.read_text().replace("- [dev](areas/dev.md) — read when dev\n", hook))
        assert memory.restore_memory() == 0
        (agent_dir / USER_MEMORY_FILE).unlink()
        local = agent_dir / "areas" / "user.md"
        local.write_text("".join(
            line for line in local.read_text().splitlines(keepends=True)
            if USER_MEMORY_FILE not in line
        ))
        assert _persist()[0] == 0
        assert not (tree.instance_memory_dir / USER_MEMORY_FILE).exists()
        text = index.read_text()
        assert "start with archived " in text and USER_MEMORY_FILE not in text


class TestMixedFormFlatEntries:
    """Project split, instance flat: a flat entry stays an entry (#435 r4)."""

    def test_an_edited_flat_instance_entry_linking_an_area_keeps_its_link(
        self, schema_tree
    ):
        tree, agent_dir = schema_tree
        _split(tree.project_memory_dir, {"dev": [PROJECT_MEMORY_FILE]})
        instance = tree.instance_memory_dir / memory.MEMORY_INDEX
        entry = next(
            line for line in instance.read_text().splitlines(keepends=True)
            if f"]({USER_MEMORY_FILE})" in line
        )
        related = entry.rstrip("\n") + " Related: [missing](areas/missing.md)\n"
        instance.write_text(instance.read_text().replace(entry, related))
        assert memory.restore_memory() == 0
        local = agent_dir / memory.MEMORY_INDEX
        edited = related.replace(" Related:", " CHANGED HOOK. Related:")
        local.write_text(local.read_text().replace(related, edited))
        assert _persist()[0] == 0
        text = instance.read_text()
        line = next(line for line in text.splitlines() if "CHANGED HOOK." in line)
        assert line.startswith(entry.split(" — ")[0])  # the memory link survives
        assert line.endswith("Related: archived " + memory._tombstone_date())
        assert text.count(f"]({USER_MEMORY_FILE})") == 1
        assert "CHANGED HOOK" not in (tree.project_memory_dir / memory.MEMORY_INDEX).read_text()
        assert not (tree.instance_memory_dir / "areas").exists()


class TestMapHookAreasAcrossScopes:
    """A hook may link an area the other scope holds (#435 r4)."""

    def test_a_hook_link_to_an_area_in_the_other_scope_is_kept(self, schema_tree):
        tree, agent_dir = schema_tree
        _split(tree.instance_memory_dir, {"user": [USER_MEMORY_FILE]})
        _split(tree.project_memory_dir, {"dev": [PROJECT_MEMORY_FILE]})
        index = tree.project_memory_dir / memory.MEMORY_INDEX
        hook = "- [dev](areas/dev.md) — read when dev; see [user area](areas/user.md)\n"
        index.write_text(index.read_text().replace("- [dev](areas/dev.md) — read when dev\n", hook))
        assert memory.restore_memory() == 0
        assert _persist()[0] == 0
        assert hook in index.read_text()
        # Edited, it follows its own area, not the one its hook links.
        local = agent_dir / memory.MEMORY_INDEX
        local.write_text(local.read_text().replace(hook, hook.replace("; see", "; EDITED, see")))
        assert _persist()[0] == 0
        assert "EDITED, see [user area](areas/user.md)" in index.read_text()
        assert "EDITED" not in (tree.instance_memory_dir / memory.MEMORY_INDEX).read_text()


class TestLegacyLiftReadsFlatAreaLinks:
    def test_the_lift_moves_a_flat_entry_that_also_links_an_area(self, tmp_path):
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        (memory_dir / USER_MEMORY_FILE).write_text(_memory_file("terse", "user"))
        line = f"- [Terse]({USER_MEMORY_FILE}) — short. Related: [dev](areas/dev.md)\n"
        (memory_dir / memory.MEMORY_INDEX).write_text(line)
        notes = []
        lift = migrate._plan_memory_lift(
            memory_dir,
            ((USER_MEMORY_FILE, memory.INSTANCE_SCOPE, ""),),
            tmp_path / "instance",
            "_agents/memory",
            notes,
        )
        # As 12d5e7f3 did: the area link reads as a second (dangling) entry,
        # so the line is cut and the memory's half lifts to the instance.
        assert lift.lines == (
            (memory.INSTANCE_SCOPE, f"- [Terse]({USER_MEMORY_FILE}) — short. Related:"),
            (memory.PROJECT_SCOPE, "[dev](areas/dev.md)"),
        )
        assert [write.note for write in lift.writes][-1] == "new instance index: 1 entry"
        assert not [note for note in notes if "no MEMORY.md line" in note]


# ---------------------------------------------------------------------------
# Review round 5 follow-ups (#601): ownership evidence, archive-linked hooks
# and areas this persist generates
# ---------------------------------------------------------------------------


class TestMapLineOwnershipEvidence:
    """Only a memory the session holds says where a map line belongs (#601)."""

    def test_an_edited_instance_map_line_leading_with_a_document_stays_put(
        self, schema_tree
    ):
        tree, agent_dir = schema_tree
        _split(tree.instance_memory_dir, {"user": [USER_MEMORY_FILE]})
        (tree.instance_memory_dir.parent / "notes.md").write_text("# notes\n")
        assert memory.restore_memory() == 0
        local = agent_dir / memory.MEMORY_INDEX
        edited = "- [Notes](../notes.md) first, then [user](areas/user.md) — read when user\n"
        local.write_text(local.read_text().replace(
            "- [user](areas/user.md) — read when user\n", edited
        ))
        assert _persist()[0] == 0
        assert edited in (tree.instance_memory_dir / memory.MEMORY_INDEX).read_text()
        assert "notes.md" not in (tree.project_memory_dir / memory.MEMORY_INDEX).read_text()
        assert _index_findings(tree) == []


class TestArchiveLinkedMapHooks:
    """A hook's archive link spares itself, not the line's dead area (#601)."""

    def _retire_dev(self, scope):
        dev, ops = scope / "areas" / "dev.md", scope / "areas" / "ops.md"
        ops.write_text(ops.read_text() + "".join(
            line + "\n" for line in _entry_lines(dev.read_text())
        ))
        dev.unlink()

    def test_a_dead_own_area_is_tombstoned_beside_a_hooks_archive_link(self, legacy_tree):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE], "ops": [USER_MEMORY_FILE]})
        index = scope / memory.MEMORY_INDEX
        hook = "— read when dev; history in [old dev](_archive/dev-old.md)\n"
        index.write_text(index.read_text().replace(
            "- [dev](areas/dev.md) — read when dev\n", f"- [dev](areas/dev.md) {hook}"
        ))
        self._retire_dev(scope)
        assert memory.restore_memory() == 0
        assert _persist()[0] == 0
        lines = index.read_text().splitlines(keepends=True)
        assert f"- archived {memory._tombstone_date()} {hook}" in lines
        assert _index_findings(tree) == []
        after = _tree_bytes(scope)
        assert memory.restore_memory() == 0
        assert _persist()[0] == 0
        assert _tree_bytes(scope) == after

    def test_a_forget_tombstone_on_the_map_is_still_kept_verbatim(self, legacy_tree):
        # Its first link is the archive mirror: `revive` finds it by its text.
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE], "ops": [USER_MEMORY_FILE]})
        index = scope / memory.MEMORY_INDEX
        tombstone = "- [Old fact](_archive/old_fact.md) — see [dev](areas/dev.md)\n"
        index.write_text(index.read_text() + tombstone)
        self._retire_dev(scope)
        assert memory.restore_memory() == 0
        assert _persist()[0] == 0
        assert tombstone in index.read_text().splitlines(keepends=True)


class TestAreasThisPersistGenerates:
    """A cross-reference to an area the reconcile creates is live (#601)."""

    def test_a_link_to_an_area_a_later_scope_generates_is_kept(self, schema_tree):
        tree, agent_dir = schema_tree
        _split(tree.instance_memory_dir, {"user": [USER_MEMORY_FILE]})
        _split(tree.project_memory_dir, {"dev": [PROJECT_MEMORY_FILE]})
        assert memory.restore_memory() == 0
        # No index line: the instance scope's reconcile files it, creating the
        # area — after the project scope has settled.
        (agent_dir / "user_new.md").write_text(_memory_file("user-new", "user", "prefs"))
        local = agent_dir / "areas" / "dev.md"
        local.write_text(local.read_text() + "See also [prefs](prefs.md).\n")
        assert _persist()[0] == 0
        assert "](../user_new.md)" in (
            tree.instance_memory_dir / "areas" / "prefs.md"
        ).read_text()
        assert "See also [prefs](prefs.md)." in (
            tree.project_memory_dir / "areas" / "dev.md"
        ).read_text()
        assert _index_findings(tree) == []

    def test_a_link_to_an_area_the_same_scope_generates_is_kept(self, legacy_tree):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE, USER_MEMORY_FILE]})
        assert memory.restore_memory() == 0
        (agent_dir / "project_new.md").write_text(_memory_file("project-new", area="prefs"))
        local = agent_dir / "areas" / "dev.md"
        local.write_text(local.read_text() + "See also [prefs](prefs.md).\n")
        assert _persist()[0] == 0
        assert "](../project_new.md)" in (scope / "areas" / "prefs.md").read_text()
        assert "See also [prefs](prefs.md)." in (scope / "areas" / "dev.md").read_text()
        assert _index_findings(tree) == []


class TestOneForgetTombstonePredicate:
    """Writers and the validator read a ``forget`` tombstone alike (#601)."""

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("- [Fact](../_archive/x/fact.md) — see also [ops](ops.md)", True),
            ("- [Fact](_archive/x/fact.md) — a fact", True),
            ("- [Fact](https://example.invalid/a.md) then [Old](_archive/old.md)", True),
            ("- [dev](areas/dev.md) — history in [old](_archive/dev-old.md)", False),
            ("See also [ops](ops.md); history in [old](../_archive/old.md).", False),
            ("- [Fact](fact.md) — a fact", False),
            ("- archived 2026-10-29 — a fact", False),
        ],
    )
    def test_the_writer_and_the_validator_agree(self, line, expected):
        assert memory._is_forget_tombstone is is_forget_tombstone
        assert is_forget_tombstone(line) is expected
        if expected:
            assert memory._entry_targets(line, split=False) == []
            assert parse_index_entries(line) == []

    def test_a_tombstones_later_live_link_is_no_entry_for_persist_either(self, legacy_tree):
        # Its "see also" names a live memory with no other entry and a dead
        # one. Persist must file the live one and leave the line verbatim.
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        tombstone = (
            "- [Old](_archive/old.md) — see also [Fresh](project_fresh.md) "
            "and [Gone](project_gone.md)\n"
        )
        index = scope / memory.MEMORY_INDEX
        index.write_text(index.read_text() + tombstone)
        (scope / "project_fresh.md").write_text(_memory_file("fresh"))
        assert memory.restore_memory() == 0
        assert _persist()[0] == 0
        lines = index.read_text().splitlines(keepends=True)
        assert tombstone in lines
        assert any("](project_fresh.md)" in line for line in lines if line != tombstone)
        assert _index_findings(tree) == []

    def test_a_tombstone_naming_an_instance_memory_stays_in_its_scope(self, schema_tree):
        tree, agent_dir = schema_tree
        tombstone = f"- [Old](_archive/old.md) — see also [Terse]({USER_MEMORY_FILE})\n"
        index = tree.project_memory_dir / memory.MEMORY_INDEX
        index.write_text(index.read_text() + tombstone)
        assert memory.restore_memory() == 0
        assert _persist()[0] == 0
        assert tombstone in index.read_text().splitlines(keepends=True)
        instance = (tree.instance_memory_dir / memory.MEMORY_INDEX).read_text()
        assert tombstone not in instance.splitlines(keepends=True)
        assert _index_findings(tree) == []

    def test_an_area_line_that_also_links_the_archive_still_loses_its_dead_area(
        self, legacy_tree
    ):
        tree, agent_dir = legacy_tree
        scope = tree.project_memory_dir
        _split(scope, {"dev": [PROJECT_MEMORY_FILE], "ops": [USER_MEMORY_FILE]})
        assert memory.restore_memory() == 0
        local = agent_dir / "areas" / "dev.md"
        local.write_text(
            local.read_text() + "See also [ops](ops.md); history in [old](../_archive/old.md).\n"
        )
        assert _persist()[0] == 0
        # Another session retires the ops area, filing its entry into dev.
        ops, dev = scope / "areas" / "ops.md", scope / "areas" / "dev.md"
        dev.write_text(dev.read_text() + "".join(
            line + "\n" for line in _entry_lines(ops.read_text())
        ))
        ops.unlink()
        index = scope / memory.MEMORY_INDEX
        index.write_text(index.read_text().replace("- [ops](areas/ops.md) — read when ops\n", ""))
        assert memory.restore_memory() == 0
        assert _persist()[0] == 0
        assert (
            f"See also archived {memory._tombstone_date()}; "
            "history in [old](../_archive/old.md).\n"
        ) in dev.read_text().splitlines(keepends=True)
        assert _index_findings(tree) == []


class TestLegacyLiftReadsSplitAreas:
    """A split legacy dir's hand-written area lines survive the lift (#601)."""

    HAND = f"- [Terse](../{USER_MEMORY_FILE}) — HAND HOOK\n"

    def _split_legacy(self, tmp_path):
        memory_dir = tmp_path / "memory"
        (memory_dir / "areas").mkdir(parents=True)
        (memory_dir / USER_MEMORY_FILE).write_text(_memory_file("terse", "user"))
        (memory_dir / PROJECT_MEMORY_FILE).write_text(_memory_file("gotcha"))
        (memory_dir / memory.MEMORY_INDEX).write_text(
            "# Memory map\n\n- [general](areas/general.md) — read when anything\n"
        )
        (memory_dir / "areas" / "general.md").write_text(
            f"# general\n\n{self.HAND}- [Gotcha](../{PROJECT_MEMORY_FILE}) — project hook\n"
        )
        return memory_dir

    def _lift(self, memory_dir, instance_dir, notes):
        return migrate._plan_memory_lift(
            memory_dir, migrate._memory_routes(memory_dir), instance_dir, "../_agents/memory",
            notes,
        )

    def test_an_instance_memorys_area_line_lifts_with_its_links_rebased(self, tmp_path):
        memory_dir = self._split_legacy(tmp_path)
        notes = []
        lift = self._lift(memory_dir, tmp_path / "instance", notes)
        writes = {write.path: write for write in lift.writes}
        instance = writes["../_agents/memory/MEMORY.md"].text
        assert f"- [Terse]({USER_MEMORY_FILE}) — HAND HOOK\n" in instance
        area = writes["_agents/memory/areas/general.md"]
        assert self.HAND not in area.text
        assert f"](../{PROJECT_MEMORY_FILE}) — project hook\n" in area.text
        assert area.source == "memory/areas/general.md"
        # An area index is no memory: it is neither routed nor remarked on.
        assert [rel for rel, _scope, _remark in lift.files] == sorted(
            [PROJECT_MEMORY_FILE, USER_MEMORY_FILE]
        )
        assert not [note for note in notes if "metadata.type" in note]
        assert not [note for note in notes if "no MEMORY.md line" in note]

    def test_an_already_split_instance_keeps_the_line_in_the_project_and_says_so(
        self, tmp_path
    ):
        memory_dir = self._split_legacy(tmp_path)
        instance_dir = tmp_path / "instance"
        (instance_dir / "areas").mkdir(parents=True)
        (instance_dir / "areas" / "user.md").write_text("# user\n")
        notes = []
        lift = self._lift(memory_dir, instance_dir, notes)
        assert all("HAND HOOK" not in write.text for write in lift.writes)
        assert not [write for write in lift.writes if "areas/" in write.path]
        assert [note for note in notes if "already split" in note and "1 line" in note]
