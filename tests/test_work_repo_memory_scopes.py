"""Two-scope agent memory in the work CLI (issue #353, spec D17).

A memory's own `metadata.type` says which scope owns it: `project` belongs to
`<project>/_agents/memory/`, `user`/`feedback`/`reference` to the instance
scope `_agents/memory/` at the plexus root, where they are true of the
operator rather than of one project. Persist routes by type, restore reads
the UNION with the project scope overlaying instance, and each scope keeps
its own `MEMORY.md`.

Three properties are load-bearing and each has its own class here:

- **Legacy is byte-identical.** A tree with no manifest has ONE memory scope,
  and every type keeps landing in `<project>/memory/` with today's mirror
  semantics — same files, same verbatim `MEMORY.md`, same single staged path.
- **The split is honest in both directions.** What persist writes, restore
  reads back: a routed round trip reproduces the agent's memory dir and the
  CONTENT of its index — the merge groups entry lines by scope rather than
  recording where each one sat, and that shape is pinned here too.
- **A write never crosses a scope, and never crosses a session.** Persist
  deletes exactly what THIS session restored from THAT scope and then removed
  (issue #369): the work repo is shared — the instance scope by EVERY
  project's sessions — so a session that never saw a memory has nothing to
  say about it, neither about the file nor about its index line. A scope this
  persist has nothing routed to is left alone entirely, out loud.
"""

from unittest.mock import patch

import itertools
import json
import posixpath
import threading

import pytest

from tests.conftest import strip_lmer_env
from tests.plexus_fixtures import (
    PRIMARY_REPO,
    PROJECT,
    PROJECT_MEMORY_FILE,
    USER_MEMORY_FILE,
    build_legacy_plexus,
    build_schema_plexus,
)
from work_repo import memory, utils


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    """No ambient run context, and no announcement carried between tests."""
    strip_lmer_env(monkeypatch)
    monkeypatch.setattr(memory, "check_commit_ready", lambda *args: 0)
    utils._ANNOUNCED_MANIFEST_PROBLEMS.clear()


def _memory_file(name, kind, description="a fact", body="The fact.\n"):
    """A memory file's text: the frontmatter SCHEMA.md requires, plus a body.

    *kind* is the `metadata.type`; ``None`` writes the metadata block without
    a type, which is one of the three unroutable shapes.
    """
    lines = ["---", f"name: {name}", f'description: "{description}"', "metadata:"]
    lines.append("  node_type: memory")
    if kind is not None:
        lines.append(f"  type: {kind}")
    lines += ["---", "", body]
    return "\n".join(lines)


def _index_line(title, filename, description="a fact"):
    return f"- [{title}]({filename}) — {description}.\n"


def _set_context(monkeypatch, tree, agent_dir, *, url=PRIMARY_REPO):
    """Point the work CLI at *tree* with the launch context a session has."""
    host, project = url.split("/", 1)
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tree.root))
    monkeypatch.setenv("LMER_REPO_HOST", host)
    monkeypatch.setenv("LMER_REPO_PROJECT", project)
    monkeypatch.setenv("LMER_REPO_URL", f"https://{url}")
    monkeypatch.setenv("LMER_AGENT_MEMORY_DIR", str(agent_dir))
    monkeypatch.setenv("LMER_PERSIST_AGENT_MEMORY", "1")


@pytest.fixture
def schema_tree(tmp_path, monkeypatch):
    """A schema-form plexus, an empty agent memory dir, and the context."""
    tree = build_schema_plexus(tmp_path / "plexus-schema")
    agent_dir = tmp_path / "agent-memory"
    agent_dir.mkdir()
    _set_context(monkeypatch, tree, agent_dir)
    return tree, agent_dir


@pytest.fixture
def schema2_tree(schema_tree):
    """The schema fixture at the schema-2 writer boundary."""
    tree, agent_dir = schema_tree
    text = tree.manifest.read_text()
    tree.manifest.write_text(text.replace("schema: 1", "schema: 2", 1))
    return tree, agent_dir


@pytest.fixture
def legacy_tree(tmp_path, monkeypatch):
    """The legacy twin: one memory scope, no manifest."""
    tree = build_legacy_plexus(tmp_path / "plexus-legacy")
    agent_dir = tmp_path / "agent-memory"
    agent_dir.mkdir()
    _set_context(monkeypatch, tree, agent_dir)
    return tree, agent_dir


def _persist(message=None):
    """persist_memory with the push stubbed; returns (rc, staged rel paths).

    ``commit_work_path`` takes one path or a list of them, and a single scope
    is staged as the bare path it always was — the list shape appears only
    when there really are two scopes to stage — so the staged paths are
    normalised to a list here and pinned in their raw form once, below.
    """
    with patch("work_repo.memory.commit_work_path", return_value=0) as commit:
        rc = memory.persist_memory(message)
    if commit.call_args is None:
        return rc, None
    staged = commit.call_args[0][0]
    return rc, staged if isinstance(staged, list) else [staged]


# ---------------------------------------------------------------------------
# Routing (D17)
# ---------------------------------------------------------------------------


class TestRouting:
    """`metadata.type` selects the scope; an unroutable file has a home too."""

    @pytest.mark.parametrize(
        "kind,scope",
        [
            ("project", memory.PROJECT_SCOPE),
            ("user", memory.INSTANCE_SCOPE),
            ("feedback", memory.INSTANCE_SCOPE),
            ("reference", memory.INSTANCE_SCOPE),
        ],
    )
    def test_type_selects_the_scope(self, kind, scope):
        assert memory.scope_for_memory(_memory_file("m", kind)) == scope

    @pytest.mark.parametrize(
        "text",
        [
            _memory_file("m", None),  # metadata, no type
            _memory_file("m", "wishlist"),  # a type outside the vocabulary
            "no frontmatter at all\n",
            "---\nname: m\n",  # unterminated fence
            "---\n[: not yaml\n---\n",
            "---\n- a list\n---\n",  # frontmatter is not a mapping
            # A non-scalar type is not a lookup key at all: `dict.get` raises
            # on an unhashable one, and a hand-written `type: [user, project]`
            # or a `type:` whose value got indented into a mapping is an
            # ordinary shape. It routes like every other file this writer
            # cannot read — it never takes the persist down.
            _memory_file("m", "[user, project]"),
            "---\nname: m\nmetadata:\n  type:\n    kind: user\n---\n\nbody\n",
        ],
    )
    def test_unroutable_files_fall_to_the_project_scope(self, text):
        # The narrower blast radius: the instance scope is shared by every
        # project, and this is where the pre-D17 tree already put everything.
        assert memory.scope_for_memory(text) is None
        assert memory.DEFAULT_SCOPE == memory.PROJECT_SCOPE


# ---------------------------------------------------------------------------
# Persist: the split
# ---------------------------------------------------------------------------


class TestPersistRoutesByType:
    @staticmethod
    def _scope_texts(tree):
        return {
            path: path.read_text()
            for scope in (tree.instance_memory_dir, tree.project_memory_dir)
            for path in sorted(scope.iterdir())
        }

    def _two_typed_memories(self, agent_dir):
        # Restore first: a persist deletes only what THIS session restored and
        # then removed (issue #369), so the index this session writes replaces
        # the one it was handed only because it was handed one.
        assert memory.restore_memory() == 0
        (agent_dir / "prefs.md").write_text(_memory_file("prefs", "user"))
        (agent_dir / "gotcha.md").write_text(_memory_file("gotcha", "project"))
        (agent_dir / "MEMORY.md").write_text(
            "# Memory\n\n"
            + _index_line("Prefs", "prefs.md")
            + _index_line("Gotcha", "gotcha.md")
        )

    def test_each_type_lands_in_its_scope(self, schema_tree):
        tree, agent_dir = schema_tree
        self._two_typed_memories(agent_dir)

        rc, staged = _persist()

        assert rc == 0
        assert (tree.instance_memory_dir / "prefs.md").exists()
        assert (tree.project_memory_dir / "gotcha.md").exists()
        assert not (tree.instance_memory_dir / "gotcha.md").exists()
        assert not (tree.project_memory_dir / "prefs.md").exists()
        assert set(staged) == {
            f"{PROJECT}/_agents/memory",
            "_agents/memory",
        }

    def test_each_scope_keeps_its_own_index(self, schema_tree):
        tree, agent_dir = schema_tree
        self._two_typed_memories(agent_dir)
        _persist()

        instance_index = (tree.instance_memory_dir / "MEMORY.md").read_text()
        project_index = (tree.project_memory_dir / "MEMORY.md").read_text()
        # An entry line follows its file; a heading is commentary and stays
        # in both indexes (SCHEMA.md "Index lines").
        assert "prefs.md" in instance_index and "gotcha.md" not in instance_index
        assert "gotcha.md" in project_index and "prefs.md" not in project_index
        assert instance_index.startswith("# Memory\n")
        assert project_index.startswith("# Memory\n")

    def test_untyped_file_is_persisted_to_the_project_scope_with_a_warning(
        self, schema_tree, capsys
    ):
        tree, agent_dir = schema_tree
        (agent_dir / "mystery.md").write_text("just a note, no frontmatter\n")

        rc, _staged = _persist()

        assert rc == 0
        assert (tree.project_memory_dir / "mystery.md").exists()
        err = capsys.readouterr().err
        assert "mystery.md" in err
        assert "project scope" in err

    def test_a_non_scalar_type_does_not_take_the_whole_persist_down(
        self, schema_tree, capsys
    ):
        # One hand-edited `type: [user, feedback]` used to raise TypeError out
        # of persist_memory (which catches only OSError), so the session saved
        # nothing at all. It routes to the project scope like every other
        # shape this writer cannot read.
        tree, agent_dir = schema_tree
        (agent_dir / "handwritten.md").write_text(
            _memory_file("handwritten", "[user, feedback]")
        )
        (agent_dir / "prefs.md").write_text(_memory_file("prefs", "user"))

        rc, _staged = _persist()

        assert rc == 0
        assert (tree.project_memory_dir / "handwritten.md").exists()
        assert (tree.instance_memory_dir / "prefs.md").exists()
        assert "handwritten.md" in capsys.readouterr().err

    def test_a_project_scope_copy_moves_to_the_scope_its_type_owns(
        self, schema_tree
    ):
        # The D17 move, on the tree that needs it: a legacy-migrated one, where
        # every type already sits in `<project>/memory/`. Restore hands the
        # `user` memory to the agent, persist routes it to the instance scope
        # — and the project copy must GO. Keeping it (rule 2 applied in the
        # direction that has no shadow) leaves the memory duplicated in both
        # scopes forever, and the project copy then overlays later
        # instance-side updates from other projects' sessions.
        tree, agent_dir = schema_tree
        (tree.project_memory_dir / "user_likes_tabs.md").write_text(
            _memory_file("likes-tabs", "user")
        )

        assert memory.restore_memory() == 0
        assert (agent_dir / "user_likes_tabs.md").exists()
        assert _persist()[0] == 0

        assert (tree.instance_memory_dir / "user_likes_tabs.md").exists()
        assert not (tree.project_memory_dir / "user_likes_tabs.md").exists()

        # ...and the move is finished, not repeated: a second persist of the
        # same memory dir writes the same bytes.
        after_first = self._scope_texts(tree)
        assert _persist()[0] == 0
        assert self._scope_texts(tree) == after_first

    def test_a_deletion_propagates_within_its_own_scope(self, schema_tree):
        tree, agent_dir = schema_tree
        (tree.project_memory_dir / "wrong.md").write_text(
            _memory_file("wrong", "project")
        )
        # Both scopes were restored, then the agent dropped the project one.
        # The restore is what makes the drop a deletion rather than a session
        # that never saw the file (issue #369).
        assert memory.restore_memory() == 0
        (agent_dir / "wrong.md").unlink()
        (agent_dir / "prefs.md").write_text(_memory_file("prefs", "user"))
        (agent_dir / "keep.md").write_text(_memory_file("keep", "project"))

        _persist()

        assert not (tree.project_memory_dir / "wrong.md").exists()
        assert (tree.project_memory_dir / "keep.md").exists()
        assert (tree.instance_memory_dir / "prefs.md").exists()


class TestAMirrorNeverCrossesAScope:
    """The instance scope is shared by every project's sessions.

    The two rules that predate the restore manifest and still run under it:
    a scope with nothing routed to it is not written at all, and a file whose
    `name` moved to the other scope was shadowed out of the agent's dir
    rather than deleted from it.
    """

    def test_a_scope_with_nothing_routed_to_it_is_left_alone(
        self, schema_tree, capsys
    ):
        tree, agent_dir = schema_tree
        before = (tree.instance_memory_dir / USER_MEMORY_FILE).read_text()
        # This session only has project memory — say nothing about the other
        # scope rather than mirroring an empty set over it.
        (agent_dir / "gotcha.md").write_text(_memory_file("gotcha", "project"))

        rc, staged = _persist()

        assert rc == 0
        assert (tree.instance_memory_dir / USER_MEMORY_FILE).read_text() == before
        assert staged == [f"{PROJECT}/_agents/memory"]
        assert "instance" in capsys.readouterr().err

    def test_a_name_claimed_by_the_other_scope_is_never_deleted(self, schema_tree):
        # Restore shadows an instance memory when the project scope carries
        # the same `name`, so its absence from this persist is a shadow, not
        # a deletion.
        tree, agent_dir = schema_tree
        (tree.instance_memory_dir / "shadowed.md").write_text(
            _memory_file("terse-replies", "user")
        )
        # Restored — so both files below are ones this session could delete
        # (issue #369) and the shadow rule is what actually saves shadowed.md.
        assert memory.restore_memory() == 0
        (agent_dir / "shadowed.md").unlink()
        (agent_dir / "prefs.md").write_text(_memory_file("other", "user"))
        (agent_dir / "local.md").write_text(_memory_file("terse-replies", "project"))
        (agent_dir / USER_MEMORY_FILE).unlink()

        _persist()

        assert (tree.instance_memory_dir / "shadowed.md").exists()
        # while an unshadowed instance file the agent dropped still goes
        assert not (tree.instance_memory_dir / USER_MEMORY_FILE).exists()


class TestARetirementThatEmptiesAScopeStillCarries:
    """Nothing ROUTED is not the same answer as nothing RESTORED.

    The skip above protects a scope this session never touched. Keyed on
    routing alone it also swallows the opposite case: a session that DID
    restore from a scope and has since deleted everything it held there is
    retiring those memories, and skipping the scope leaves them upstream for
    the next restore to hand straight back.

    Every test here calls ``restore_memory()`` first — that is the whole
    point. Its sibling above does not, so its manifest is empty and its
    ``continue`` is still taken; that is why this went unnoticed.
    """

    def test_retiring_the_last_memory_in_a_scope_removes_it(self, schema_tree):
        tree, agent_dir = schema_tree
        assert memory.restore_memory() == 0
        # The workflow the memory instructions prescribe for a fact that
        # turned out wrong: delete the file, drop its index line. It is the
        # scope's only memory, so nothing routes to the project scope.
        (agent_dir / PROJECT_MEMORY_FILE).unlink()
        index = agent_dir / memory.MEMORY_INDEX
        index.write_text(
            "".join(
                line
                for line in index.read_text().splitlines(keepends=True)
                if PROJECT_MEMORY_FILE not in line
            )
        )

        rc, staged = _persist()

        assert rc == 0
        assert not (tree.project_memory_dir / PROJECT_MEMORY_FILE).exists()
        # and the index line goes with it, rather than dangling at a file
        # that is no longer there
        project_index = tree.project_memory_dir / memory.MEMORY_INDEX
        assert PROJECT_MEMORY_FILE not in project_index.read_text()
        # the scope was written, so it is staged for commit
        assert f"{PROJECT}/_agents/memory" in staged
        # the other scope is untouched by any of this
        assert (tree.instance_memory_dir / USER_MEMORY_FILE).exists()

    def test_a_retype_does_not_leave_the_old_scope_shadowing_it(self, schema_tree):
        """The retype is the worse half: it is silently ineffective forever.

        Restore overlays project on instance at the same relative path, so a
        stale project copy left behind wins over the corrected instance one
        on every later restore — the exact duplication the `claimed` rule
        above exists to prevent, which cannot help here because it protects
        an INSTANCE file from deletion while the file that must go is the
        project one.
        """
        tree, agent_dir = schema_tree
        assert memory.restore_memory() == 0
        # Same file, same name, corrected type: it now belongs to the
        # instance scope (D17) and nothing routes to the project one.
        (agent_dir / PROJECT_MEMORY_FILE).write_text(
            _memory_file("selfdev-pythonpath-gotcha", "feedback", body="Corrected.\n")
        )

        assert _persist()[0] == 0

        assert (tree.instance_memory_dir / PROJECT_MEMORY_FILE).exists()
        assert not (tree.project_memory_dir / PROJECT_MEMORY_FILE).exists()
        # and the move is real: what the NEXT session reads is the correction
        for path in list(agent_dir.iterdir()):
            path.unlink()
        assert memory.restore_memory() == 0
        assert "type: feedback" in (agent_dir / PROJECT_MEMORY_FILE).read_text()

    def test_retiring_every_memory_in_both_scopes_carries(self, schema_tree):
        """The whole-directory guard in `persist_memory`, same shape.

        An empty memory dir is "nothing to persist" only when this session
        was handed nothing either.
        """
        tree, agent_dir = schema_tree
        assert memory.restore_memory() == 0
        for path in agent_dir.rglob("*"):
            if path.is_file():
                path.unlink()

        rc, staged = _persist()

        assert rc == 0
        assert not (tree.project_memory_dir / PROJECT_MEMORY_FILE).exists()
        assert not (tree.instance_memory_dir / USER_MEMORY_FILE).exists()
        assert sorted(staged) == sorted(memory.memory_write_rel_paths())
        # The INDEXES survive this one, deliberately, and that is the
        # difference between deleting every memory and retiring them one at a
        # time. Emptying the dir takes `MEMORY.md` with it, and an absent
        # index is not a statement (:func:`_index_statement`) — a wiped memory
        # dir is exactly what a crash or a killed container leaves, and it is
        # indistinguishable from this. Under-deleting an index line loses
        # nothing; the lines go as soon as one session holds an index again.
        # Gap (e) then reconciles each surviving entry with the now-empty
        # scope: the line stays, non-lossily, as `archived <date>` rather
        # than dangling (never a silent drop).
        project_index = (tree.project_memory_dir / memory.MEMORY_INDEX).read_text()
        instance_index = (tree.instance_memory_dir / memory.MEMORY_INDEX).read_text()
        assert "archived" in project_index, project_index
        assert f"({PROJECT_MEMORY_FILE})" not in project_index, project_index
        assert "archived" in instance_index, instance_index
        assert f"({USER_MEMORY_FILE})" not in instance_index, instance_index

    def test_retiring_every_memory_carries_in_a_legacy_tree_too(self, legacy_tree):
        tree, agent_dir = legacy_tree
        assert memory.restore_memory() == 0
        for path in agent_dir.rglob("*"):
            if path.is_file():
                path.unlink()

        assert _persist()[0] == 0

        assert not (tree.project_memory_dir / PROJECT_MEMORY_FILE).exists()
        assert not (tree.project_memory_dir / USER_MEMORY_FILE).exists()

    def test_a_scope_this_session_never_restored_from_is_still_left_alone(
        self, schema_tree, capsys
    ):
        """The counter-test: the protection the skip exists for is intact.

        Nothing routed AND nothing restored is still silence, even while a
        retirement is being applied to the other scope in the same persist.
        """
        tree, agent_dir = schema_tree
        # This session restores when the instance scope is empty, so it holds
        # nothing from it and has nothing to say about it.
        (tree.instance_memory_dir / USER_MEMORY_FILE).unlink()
        (tree.instance_memory_dir / memory.MEMORY_INDEX).unlink()
        assert memory.restore_memory() == 0
        # Another session then files an instance memory this one never saw.
        (tree.instance_memory_dir / "other.md").write_text(
            _memory_file("other-session", "user")
        )
        (tree.instance_memory_dir / memory.MEMORY_INDEX).write_text(
            _index_line("Other", "other.md")
        )
        untouched = (tree.instance_memory_dir / memory.MEMORY_INDEX).read_text()
        # ...while this session retires the only memory it holds.
        (agent_dir / PROJECT_MEMORY_FILE).unlink()
        (agent_dir / memory.MEMORY_INDEX).write_text("")

        assert _persist()[0] == 0

        # The retirement carried...
        assert not (tree.project_memory_dir / PROJECT_MEMORY_FILE).exists()
        # ...and the other session's scope is byte-identical, out loud.
        assert (tree.instance_memory_dir / "other.md").exists()
        assert (tree.instance_memory_dir / memory.MEMORY_INDEX).read_text() == untouched
        assert "instance" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Restore: the union
# ---------------------------------------------------------------------------


class TestRestoreUnionsBothScopes:
    def test_both_scopes_are_restored(self, schema_tree):
        tree, agent_dir = schema_tree
        assert memory.restore_memory() == 0
        assert (agent_dir / USER_MEMORY_FILE).exists()
        assert (agent_dir / PROJECT_MEMORY_FILE).exists()

    def test_indexes_merge_without_duplicating_commentary(self, schema_tree):
        tree, agent_dir = schema_tree
        (tree.instance_memory_dir / "MEMORY.md").write_text(
            "# Memory\n\n" + _index_line("Prefs", USER_MEMORY_FILE)
        )
        (tree.project_memory_dir / "MEMORY.md").write_text(
            "# Memory\n\n" + _index_line("Gotcha", PROJECT_MEMORY_FILE)
        )

        memory.restore_memory()

        index = (agent_dir / "MEMORY.md").read_text()
        assert index.count("# Memory") == 1
        assert USER_MEMORY_FILE in index and PROJECT_MEMORY_FILE in index
        # instance first, project after it (project overlays instance)
        assert index.index(USER_MEMORY_FILE) < index.index(PROJECT_MEMORY_FILE)

    def test_project_scope_overlays_instance_on_a_collision(self, schema_tree):
        tree, agent_dir = schema_tree
        (tree.instance_memory_dir / "same.md").write_text(
            _memory_file("same", "user", body="instance copy\n")
        )
        (tree.project_memory_dir / "same.md").write_text(
            _memory_file("same", "project", body="project copy\n")
        )

        memory.restore_memory()

        assert "project copy" in (agent_dir / "same.md").read_text()

    def test_a_name_collision_across_filenames_prefers_the_project_file(
        self, schema_tree
    ):
        tree, agent_dir = schema_tree
        (tree.instance_memory_dir / "instance-spelling.md").write_text(
            _memory_file("shared-name", "user", body="instance copy\n")
        )
        (tree.project_memory_dir / "project-spelling.md").write_text(
            _memory_file("shared-name", "project", body="project copy\n")
        )

        memory.restore_memory()

        assert (agent_dir / "project-spelling.md").exists()
        assert not (agent_dir / "instance-spelling.md").exists()

    def test_restore_still_mirrors_deletions_into_the_agent_dir(self, schema_tree):
        tree, agent_dir = schema_tree
        (agent_dir / "stale.md").write_text(_memory_file("stale", "project"))

        memory.restore_memory()

        assert not (agent_dir / "stale.md").exists()

    def test_commentary_after_the_entries_comes_back_before_the_project_entries(
        self, schema_tree
    ):
        # The split/merge groups entry lines by scope and gives commentary the
        # place the FIRST index has for it — it does not record where an entry
        # sat. A trailing note therefore comes back above the project entries.
        # Pinned because it is what the docstrings now claim, and because the
        # reorder must stay stable: a second cycle moves nothing further.
        tree, agent_dir = schema_tree
        for path in tree.instance_memory_dir.iterdir():
            path.unlink()
        for path in tree.project_memory_dir.iterdir():
            path.unlink()
        (agent_dir / "a-user.md").write_text(_memory_file("a-user", "user"))
        (agent_dir / "b-proj.md").write_text(_memory_file("b-proj", "project"))
        (agent_dir / "c-fb.md").write_text(_memory_file("c-fb", "feedback"))
        (agent_dir / "MEMORY.md").write_text(
            "# Agent memory index\n"
            "\n"
            + _index_line("A user", "a-user.md")
            + _index_line("B proj", "b-proj.md")
            + _index_line("C fb", "c-fb.md")
            + "\n"
            "Notes: keep one line per memory.\n"
        )

        assert _persist()[0] == 0
        assert memory.restore_memory() == 0

        assert (agent_dir / "MEMORY.md").read_text() == (
            "# Agent memory index\n"
            "\n"
            + _index_line("A user", "a-user.md")
            + _index_line("C fb", "c-fb.md")
            + "\n"
            "Notes: keep one line per memory.\n"
            + _index_line("B proj", "b-proj.md")
        )

        once = (agent_dir / "MEMORY.md").read_text()
        assert _persist()[0] == 0
        assert memory.restore_memory() == 0
        assert (agent_dir / "MEMORY.md").read_text() == once

    def test_round_trip_reproduces_both_scopes(self, schema_tree):
        tree, agent_dir = schema_tree
        before = {
            path: path.read_text()
            for scope in (tree.instance_memory_dir, tree.project_memory_dir)
            for path in scope.iterdir()
        }

        assert memory.restore_memory() == 0
        rc, _staged = _persist()

        assert rc == 0
        after = {
            path: path.read_text()
            for scope in (tree.instance_memory_dir, tree.project_memory_dir)
            for path in scope.iterdir()
        }
        assert after == before


# ---------------------------------------------------------------------------
# Legacy: one scope, byte-identical
# ---------------------------------------------------------------------------


class TestLegacyFormIsUnchanged:
    def test_every_type_lands_in_the_one_project_memory_dir(self, legacy_tree):
        tree, agent_dir = legacy_tree
        assert memory.restore_memory() == 0
        (agent_dir / "prefs.md").write_text(_memory_file("prefs", "user"))
        (agent_dir / "gotcha.md").write_text(_memory_file("gotcha", "project"))
        index = "# Memory\n\n" + _index_line("Prefs", "prefs.md")
        (agent_dir / "MEMORY.md").write_text(index)

        rc, staged = _persist()

        assert rc == 0
        assert (tree.project_memory_dir / "prefs.md").exists()
        assert (tree.project_memory_dir / "gotcha.md").exists()
        # The hand-written lines are kept verbatim, and the one scope's own
        # index is reconciled against its live files (gap (e)): the unindexed
        # `gotcha.md` — and every seeded live file whose entry this session
        # dropped — gains a generated entry rather than staying invisible.
        text = (tree.project_memory_dir / "MEMORY.md").read_text()
        assert text.startswith(index), text
        for path in tree.project_memory_dir.glob("*.md"):
            if path.name != memory.MEMORY_INDEX:
                assert f"({path.name})" in text, (path.name, text)
        assert staged == [f"{PRIMARY_REPO}/memory"]
        assert not (tree.root / "_agents").exists()

    def test_the_one_scope_is_staged_as_the_bare_path(self, legacy_tree):
        # Not the list a two-scope persist stages: the legacy call shape is
        # what tests/test_persist_agent_memory.py pins, and it does not move.
        _tree, agent_dir = legacy_tree
        (agent_dir / "prefs.md").write_text(_memory_file("prefs", "user"))
        with patch("work_repo.memory.commit_work_path", return_value=0) as commit:
            memory.persist_memory()
        assert commit.call_args[0][0] == f"{PRIMARY_REPO}/memory"

    def test_restore_reads_the_one_scope(self, legacy_tree):
        tree, agent_dir = legacy_tree
        assert memory.restore_memory() == 0
        assert (agent_dir / USER_MEMORY_FILE).exists()
        assert (agent_dir / PROJECT_MEMORY_FILE).exists()
        assert (agent_dir / "MEMORY.md").read_text() == (
            tree.project_memory_dir / "MEMORY.md"
        ).read_text()

    def test_no_instance_scope_is_created_on_a_legacy_tree(self, legacy_tree):
        tree, agent_dir = legacy_tree
        (agent_dir / "prefs.md").write_text(_memory_file("prefs", "user"))
        _persist()
        assert not (tree.root / "_agents").exists()


# ---------------------------------------------------------------------------
# The journalled write scope (issue #233)
# ---------------------------------------------------------------------------


class TestWriteScopeNamesWhatIsWritten:
    """`work memory persist` journals the paths it may write, in either form."""

    def _intent(self, **attrs):
        import argparse

        from work_repo.cli import _write_intent_rel_paths

        return _write_intent_rel_paths(argparse.Namespace(**attrs))

    def test_legacy_scope_is_the_project_memory_dir(self, legacy_tree):
        assert memory.memory_write_rel_paths() == [f"{PRIMARY_REPO}/memory"]
        assert self._intent(command="memory", memory_action="persist") == [
            f"{PRIMARY_REPO}/memory"
        ]

    def test_schema_scope_names_both_scopes(self, schema_tree):
        assert memory.memory_write_rel_paths() == [
            f"{PROJECT}/_agents/memory",
            "_agents/memory",
        ]
        # The push behind persist may also regenerate or sweep the project's
        # README index block, so the journalled scope names it too.
        assert self._intent(command="memory", memory_action="persist") == [
            f"{PROJECT}/_agents/memory",
            "_agents/memory",
            f"{PROJECT}/README.md",
        ]

    def test_no_run_context_names_nothing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
        assert memory.memory_write_rel_paths() == []
        assert self._intent(command="memory", memory_action="persist") == []

    def test_restore_is_not_a_write(self, legacy_tree):
        assert self._intent(command="memory", memory_action="restore") is None

    def test_the_journalled_scope_is_what_persist_stages(self, schema_tree):
        tree, agent_dir = schema_tree
        (agent_dir / "prefs.md").write_text(_memory_file("prefs", "user"))
        (agent_dir / "gotcha.md").write_text(_memory_file("gotcha", "project"))

        _rc, staged = _persist()

        # The journal declares the scope; persist may stage a subset of it,
        # never a path outside it.
        assert set(staged) <= set(memory.memory_write_rel_paths())


class TestPersistRefusals:
    def test_no_project_scope_is_a_refusal(self, schema_tree, monkeypatch, capsys):
        # An unusable manifest resolves NO scope at all (fail-closed, D20):
        # persisting into the legacy names inside a schema-form tree is the
        # silent demotion the resolver exists to prevent.
        tree, agent_dir = schema_tree
        (agent_dir / "prefs.md").write_text(_memory_file("prefs", "user"))
        (tree.root / "_plexus.yaml").write_text("schema: [not, an, int]\n")

        rc, staged = _persist()

        assert rc == 1
        assert staged is None
        assert "Cannot persist agent memory" in capsys.readouterr().err

    def test_persist_without_memory_writes_nothing(self, schema_tree):
        tree, agent_dir = schema_tree
        before = (tree.instance_memory_dir / USER_MEMORY_FILE).read_text()

        rc, staged = _persist()

        assert rc == 0
        assert staged == []  # check pending commits without staging any files
        assert (tree.instance_memory_dir / USER_MEMORY_FILE).read_text() == before


class TestTheDefaultCommitMessage:
    """What the commit calls this memory when the message is not given."""

    def test_the_launch_context_names_it(self, schema_tree):
        _tree, agent_dir = schema_tree
        (agent_dir / "gotcha.md").write_text(_memory_file("gotcha", "project"))

        with patch("work_repo.memory.commit_work_path", return_value=0) as commit:
            assert memory.persist_memory() == 0

        assert commit.call_args[0][1] == f"Update agent memory: {PRIMARY_REPO}"

    def test_without_it_the_label_is_the_project_dir_not_a_scope_segment(
        self, schema_tree, monkeypatch
    ):
        # Reached when the registry resolves the project from LMER_REPO_URL
        # while host/project are unset. The label comes off the RESOLVED BASE
        # (`<plexus>/lmer`), so the schema scope's own shape —
        # `<project>/_agents/memory` — cannot put `_agents` in the message.
        _tree, agent_dir = schema_tree
        monkeypatch.delenv("LMER_REPO_HOST")
        monkeypatch.delenv("LMER_REPO_PROJECT")
        (agent_dir / "gotcha.md").write_text(_memory_file("gotcha", "project"))

        with patch("work_repo.memory.commit_work_path", return_value=0) as commit:
            assert memory.persist_memory() == 0

        assert commit.call_args[0][1] == f"Update agent memory: {PROJECT}"


# ---------------------------------------------------------------------------
# Two sessions, one work repo (issue #369)
# ---------------------------------------------------------------------------


class TestTwoSessionsDoNotDeleteEachOther:
    """Persist deletes only what THIS session restored and then removed.

    The work repo is a shared store: the instance scope is written by every
    project's sessions and the project scope by every session of one project.
    A session that never saw a memory has nothing to say about it — not about
    the file, and not about its index line.
    """

    @staticmethod
    def _session(monkeypatch, tmp_path, name):
        """Point the CLI at a fresh session's memory dir and return it.

        Each session gets its OWN parent directory: the restore manifest is a
        sibling of the memory dir, so two sessions sharing a parent would
        share a manifest and the test would prove nothing.
        """
        agent_dir = tmp_path / name / "memory"
        agent_dir.mkdir(parents=True)
        monkeypatch.setenv("LMER_AGENT_MEMORY_DIR", str(agent_dir))
        return agent_dir

    @staticmethod
    def _add(agent_dir, stem, kind, title):
        (agent_dir / f"{stem}.md").write_text(_memory_file(stem, kind))
        index = agent_dir / "MEMORY.md"
        text = index.read_text() if index.is_file() else ""
        index.write_text(text + _index_line(title, f"{stem}.md"))

    @pytest.fixture(params=["schema_tree", "legacy_tree"])
    def either_form(self, request):
        """The same guarantees, on BOTH tree forms.

        Persist has two branches — one scope (legacy) and routed (schema) —
        and they merge the index through separate code. Every guarantee in
        this class was written against the schema branch, so the legacy one
        carried the blank-index bug for a round after the schema one was
        fixed: the reviewer found it, not the suite. Legacy is also the form
        the work repos in use are in TODAY, which makes it the branch where a
        memory-loss bug is live rather than pending.

        Parametrized by fixture NAME rather than by a flag, so a test's id
        names the form it failed in.
        """
        return request.getfixturevalue(request.param)

    @pytest.mark.parametrize(
        "actions",
        list(itertools.product(["edit", "noop", "remove", "retire"], repeat=3)),
    )
    def test_no_session_ever_loses_another_sessions_edit(
        self, either_form, tmp_path, monkeypatch, actions
    ):
        """Every combination of three sessions acting on one shared memory.

        The two bugs this class exists for were both found by a reviewer, not
        by the suite, because each hand-written test covered one shape. This
        walks all 27 and drives TWO persist cycles per session — the second
        cycle is where the iteration-2 bug lived, and a sweep with one cycle
        passes on the broken code, so it would have proved nothing.

        Invariant, checked on the FILE and on its INDEX LINE uniformly after
        every persist: the scope holds the last thing some session
        deliberately did — an edit, a removal, or a retirement. Gap (e)
        reconciles the entry with the live files, so a removal that carries
        tombstones the line (``archived <date>``, never a silent drop) while
        a retirement tombstones it and keeps the file; a removal the write
        gate refuses leaves the file and its entry intact. A session that
        merely held a stale copy never reverts it, never deletes it, and
        never resurrects a line another session retired.

        The index half is here because `entry["index"]` had the mirror of the
        file-side bug: it recorded the surviving lines rather than the lines
        this session holds, so a retired entry came back on cycle 2 — the
        `a108934ae -> 40c997340` incident itself.
        """
        tree, _fixture_dir = either_form
        shared = tree.project_memory_dir / "shared.md"
        shared.write_text(_memory_file("shared", "project"))
        (tree.project_memory_dir / "MEMORY.md").write_text(
            _index_line("Shared", "shared.md")
        )

        dirs = {}
        for name in ("s1", "s2", "s3"):
            dirs[name] = self._session(monkeypatch, tmp_path, f"sweep-{name}")
            assert memory.restore_memory() == 0

        def retire(agent_dir):
            """Tombstone the shared line the way ``forget`` does: re-point the
            entry at THIS file's archive mirror (the identity-bearing form).
            A bare ``forgotten <date>`` word tombstone carries no identity and
            gap (e) correctly re-indexes the live file."""
            scope_rel = tree.project_memory_dir.relative_to(tree.root)
            target = posixpath.relpath(
                memory._archive_rel_for_key((scope_rel / "shared.md").as_posix()),
                scope_rel.as_posix(),
            )
            index = agent_dir / "MEMORY.md"
            index.write_text(
                "".join(
                    f"- [Shared]({target}) — a fact\n"
                    if "shared.md" in line
                    else line
                    for line in index.read_text().splitlines(keepends=True)
                )
            )

        expected = "a fact"
        retired = False  # has a session deliberately tombstoned the shared line?
        removed = False
        for name, action in zip(("s1", "s2", "s3"), actions):
            monkeypatch.setenv("LMER_AGENT_MEMORY_DIR", str(dirs[name]))
            if action == "edit":
                expected = f"edit from {name}"
                (dirs[name] / "shared.md").write_text(
                    _memory_file("shared", "project").replace("a fact", expected)
                )
            elif action == "remove":
                (dirs[name] / "shared.md").unlink()
                removed = True
            elif action == "retire":
                retire(dirs[name])
                retired = True

            for cycle in ("one", "two"):
                self._add(dirs[name], f"{name}-{cycle}", "project", f"{name} {cycle}")
                upstream_missing = not shared.is_file()
                rc, _staged = _persist()
                if action == "edit" and upstream_missing:
                    # An edit based on restored bytes whose shared copy was
                    # deleted is ambiguous: preserve it locally, but require
                    # explicit revival rather than undoing the deletion.
                    assert rc == 1
                    assert not shared.exists()
                    assert expected in (dirs[name] / "shared.md").read_text()
                    break
                assert rc == 0
                index_text = (tree.project_memory_dir / "MEMORY.md").read_text()

                # The index line: gap (e) ties it to the live file. A
                # retirement is a deliberate tombstone and stays unlinked
                # while the file survives; a removal whose deletion carries
                # leaves no file, so the line is tombstoned in place; a
                # removal the write gate refuses (an upstream edit is newer)
                # leaves the file live and the entry linked.
                expected_linked = (not retired) and shared.is_file()
                assert ("(shared.md)" in index_text) == expected_linked, (
                    f"{name}:{action} cycle {cycle} "
                    f"{'lost' if expected_linked else 'resurrected'} the shared index "
                    f"line (actions={actions})"
                )

                if removed:
                    continue  # deleting the value it held is a real opinion
                assert shared.is_file(), (
                    f"{name}:{action} cycle {cycle} deleted a memory it had no "
                    f"opinion about (actions={actions})"
                )
                assert expected in shared.read_text(), (
                    f"{name}:{action} cycle {cycle} reverted an edit it never "
                    f"made (actions={actions}, expected {expected!r})"
                )

    def test_a_stale_session_still_holds_no_opinion_on_the_second_cycle(
        self, schema_tree, tmp_path, monkeypatch
    ):
        """!267 iteration 2: the write gate was right and the RECORD was not.

        `_write_scope` digested the DESTINATION for every held file, including
        ones the gate had just declined to overwrite — so the manifest claimed
        this session held the other session's newer copy. The gate then never
        fired again: cycle 2 reverted the edit, and a later removal deleted
        their newer file as `restored_then_removed`.

        The one-cycle test above passed throughout. The bug lived entirely in
        the second cycle, which is why this one drives three.
        """
        tree, _fixture_dir = schema_tree
        shared = tree.project_memory_dir / "shared.md"
        shared.write_text(_memory_file("shared", "project"))
        (tree.project_memory_dir / "MEMORY.md").write_text(
            _index_line("Shared", "shared.md")
        )

        session_a = self._session(monkeypatch, tmp_path, "cycles-a")
        assert memory.restore_memory() == 0

        session_b = self._session(monkeypatch, tmp_path, "cycles-b")
        assert memory.restore_memory() == 0
        (session_b / "shared.md").write_text(
            _memory_file("shared", "project").replace("a fact", "B's corrected fact")
        )
        assert _persist()[0] == 0
        assert "B's corrected fact" in shared.read_text()

        # Cycle 1 for A — already covered above, but it is what poisons the
        # manifest, so it has to run before the cycle this test is about.
        monkeypatch.setenv("LMER_AGENT_MEMORY_DIR", str(session_a))
        self._add(session_a, "a-one", "project", "A one")
        assert _persist()[0] == 0
        assert "B's corrected fact" in shared.read_text(), "cycle 1"

        # Cycle 2: A still holds the ORIGINAL bytes and still has no opinion.
        self._add(session_a, "a-two", "project", "A two")
        assert _persist()[0] == 0
        assert "B's corrected fact" in shared.read_text(), (
            "cycle 2 reverted B's edit — the recorded digest described the "
            "work repo's copy rather than what this session holds"
        )

        # ...and A dropping its stale copy must not delete B's newer file.
        (session_a / "shared.md").unlink()
        assert _persist()[0] == 0
        assert shared.is_file(), (
            "a session deleted a file it never had an opinion about"
        )
        assert "B's corrected fact" in shared.read_text()

    @pytest.mark.parametrize("content", ["", "   \n\n"], ids=["empty", "blank"])
    def test_a_truncated_index_does_not_wipe_the_scopes_index(
        self, either_form, tmp_path, monkeypatch, content
    ):
        """A half-written `MEMORY.md` is an absence, not an empty statement.

        `is_file()` is true for a zero-byte file a crash left behind, so
        reading it as "I have no lines here" drops every line the session
        restored — the files stay on disk and vanish from the index the next
        session reads.

        On BOTH branches. The fix landed on the routed one first and the
        one-scope branch kept the bug — which is the branch a legacy work
        repo takes, so the loss was live while the fix looked complete.
        """
        tree, _fixture_dir = either_form
        keeper = tree.project_memory_dir / "keeper.md"
        keeper.write_text(_memory_file("keeper", "project"))
        (tree.project_memory_dir / "MEMORY.md").write_text(
            _index_line("Keeper", "keeper.md")
        )

        session = self._session(monkeypatch, tmp_path, f"truncated-{len(content)}")
        assert memory.restore_memory() == 0
        (session / "MEMORY.md").write_text(content)  # crash mid-write
        assert _persist()[0] == 0

        index = (tree.project_memory_dir / "MEMORY.md").read_text()
        assert "keeper.md" in index, (
            "a truncated index wiped the scope's lines while the files stayed"
        )
        assert keeper.is_file()

    def test_a_session_with_no_index_does_not_wipe_the_scopes_index(
        self, schema_tree, tmp_path, monkeypatch
    ):
        """The regression the dangling-line fix introduced, caught before it
        shipped: `_scope_indexes` returns {} when the agent has NO MEMORY.md,
        and defaulting that to "" read it as "I have no lines here" — dropping
        every line this session restored. No index is not an empty index."""
        tree, _fixture_dir = schema_tree
        keeper = tree.project_memory_dir / "keeper.md"
        keeper.write_text(_memory_file("keeper", "project"))
        (tree.project_memory_dir / "MEMORY.md").write_text(
            _index_line("Keeper", "keeper.md")
        )

        session = self._session(monkeypatch, tmp_path, "no-index")
        assert memory.restore_memory() == 0
        (session / "MEMORY.md").unlink()  # the agent keeps no index of its own
        assert _persist()[0] == 0

        index = (tree.project_memory_dir / "MEMORY.md").read_text()
        assert "keeper.md" in index, (
            "a session that keeps no index wiped the scope's index lines"
        )
        assert keeper.is_file()

    def test_forgetting_the_last_entry_of_a_scope_leaves_no_dangling_line(
        self, schema_tree, tmp_path, monkeypatch
    ):
        """!267 thread 3: an EMPTY split index used to skip the scope's
        MEMORY.md while that scope's file deletions still ran, so the file went
        and the line stayed — an index pointing at nothing, in the very build
        that made "the same rule applies to the index" normative."""
        tree, _fixture_dir = schema_tree
        only = tree.instance_memory_dir / "only-one.md"
        only.write_text(_memory_file("only-one", "user"))
        (tree.instance_memory_dir / "MEMORY.md").write_text(
            _index_line("Only one", "only-one.md")
        )

        session = self._session(monkeypatch, tmp_path, "forgetter")
        assert memory.restore_memory() == 0
        assert (session / "only-one.md").is_file()

        # Forget it: drop the file AND its line, keep a project memory so the
        # persist still runs and this scope's split comes out empty.
        (session / "only-one.md").unlink()
        (session / "MEMORY.md").write_text(
            "\n".join(
                line
                for line in (session / "MEMORY.md").read_text().splitlines()
                if "only-one.md" not in line
            )
            + "\n"
        )
        self._add(session, "keeps-persist-running", "project", "Keeps running")
        assert _persist()[0] == 0

        assert not only.exists(), "the file this session forgot is gone"
        index = (tree.instance_memory_dir / "MEMORY.md").read_text()
        assert "only-one.md" not in index, (
            "the index still linked a file this persist deleted"
        )

    def test_a_manifest_key_that_escapes_the_scope_is_ignored(
        self, schema_tree, tmp_path, monkeypatch
    ):
        """!267 thread 4: `files` is the one manifest field that drives an
        unlink, and `dest / Path("/abs")` is `/abs`. A hand-corrupted key must
        be as unusable as a bad version — dropped, never followed."""
        tree, _fixture_dir = schema_tree
        victim = tmp_path / "victim.md"
        victim.write_text("not the scope's file\n")

        session = self._session(monkeypatch, tmp_path, "escaper")
        assert memory.restore_memory() == 0

        manifest = memory.read_restore_manifest()
        entry = manifest.setdefault(memory.PROJECT_SCOPE, {})
        files = dict(entry.get("files") or {})
        files[str(victim)] = memory._digest(victim)
        files["../../escape.md"] = "0" * 64
        entry["files"] = files
        memory.write_restore_manifest(manifest)

        self._add(session, "something", "project", "Something")
        assert _persist()[0] == 0

        assert victim.is_file(), "a manifest key escaped the scope and deleted a file"

    def test_a_stale_session_does_not_revert_the_other_sessions_edit(
        self, schema_tree, tmp_path, monkeypatch
    ):
        """The same loss as the test below, one level down: on the file BODY.

        !267's review found the delete side gated and the write side open, so
        a session that had merely READ a memory rewrote it from its own stale
        copy on its next persist — reverting an edit it never saw, exactly the
        `a108934ae -> 40c997340` shape at the index level.

        A restores; B restores and EDITS the shared memory and persists; A then
        persists something unrelated. A still holds the bytes it was handed, so
        it has no opinion about that file and must not write it back.
        """
        tree, _fixture_dir = schema_tree
        shared = tree.project_memory_dir / "shared.md"
        shared.write_text(_memory_file("shared", "project"))
        (tree.project_memory_dir / "MEMORY.md").write_text(
            _index_line("Shared", "shared.md")
        )

        session_a = self._session(monkeypatch, tmp_path, "revert-a")
        assert memory.restore_memory() == 0
        assert (session_a / "shared.md").is_file(), "A was handed the file"

        session_b = self._session(monkeypatch, tmp_path, "revert-b")
        assert memory.restore_memory() == 0
        edited = _memory_file("shared", "project").replace(
            "a fact", "B's corrected fact"
        )
        (session_b / "shared.md").write_text(edited)
        assert _persist()[0] == 0
        assert "B's corrected fact" in shared.read_text(), "B's edit landed"

        # A persists something unrelated, still holding the ORIGINAL bytes.
        monkeypatch.setenv("LMER_AGENT_MEMORY_DIR", str(session_a))
        self._add(session_a, "a-unrelated", "project", "A unrelated")
        assert _persist()[0] == 0

        assert "B's corrected fact" in shared.read_text(), (
            "A reverted an edit it never saw — a session that only READ a "
            "memory has no opinion about its contents"
        )
        assert (tree.project_memory_dir / "a-unrelated.md").is_file()

    def test_a_session_that_edited_a_file_still_writes_it(
        self, schema_tree, tmp_path, monkeypatch
    ):
        """The other half: skipping unopinionated writes must not stop a
        session persisting an edit it actually made."""
        tree, _fixture_dir = schema_tree
        shared = tree.project_memory_dir / "shared.md"
        shared.write_text(_memory_file("shared", "project"))
        (tree.project_memory_dir / "MEMORY.md").write_text(
            _index_line("Shared", "shared.md")
        )

        session = self._session(monkeypatch, tmp_path, "editor")
        assert memory.restore_memory() == 0
        (session / "shared.md").write_text(
            _memory_file("shared", "project").replace("a fact", "my correction")
        )
        assert _persist()[0] == 0

        assert "my correction" in shared.read_text()

    def test_a_stale_session_does_not_delete_the_other_sessions_memory(
        self, schema_tree, tmp_path, monkeypatch
    ):
        # B restores first and holds that view for the rest of the run.
        tree, _fixture_dir = schema_tree
        session_b = self._session(monkeypatch, tmp_path, "session-b")
        assert memory.restore_memory() == 0

        # A restores, writes one memory per scope, and persists.
        session_a = self._session(monkeypatch, tmp_path, "session-a")
        assert memory.restore_memory() == 0
        self._add(session_a, "a-gotcha", "project", "A gotcha")
        self._add(session_a, "a-prefs", "user", "A prefs")
        assert _persist()[0] == 0

        # B's memory dir never held A's files. Its own persist must not
        # read "absent from my directory" as "deleted everywhere".
        monkeypatch.setenv("LMER_AGENT_MEMORY_DIR", str(session_b))
        self._add(session_b, "b-gotcha", "project", "B gotcha")
        self._add(session_b, "b-prefs", "user", "B prefs")
        assert _persist()[0] == 0

        assert (tree.project_memory_dir / "a-gotcha.md").is_file()
        assert (tree.project_memory_dir / "b-gotcha.md").is_file()
        assert (tree.instance_memory_dir / "a-prefs.md").is_file()
        assert (tree.instance_memory_dir / "b-prefs.md").is_file()
        # ...and both are in their scope's index, so recall finds them.
        project_index = (tree.project_memory_dir / "MEMORY.md").read_text()
        instance_index = (tree.instance_memory_dir / "MEMORY.md").read_text()
        assert "a-gotcha.md" in project_index and "b-gotcha.md" in project_index
        assert "a-prefs.md" in instance_index and "b-prefs.md" in instance_index
        # Nor did either session drop what was in the repo before them.
        assert (tree.instance_memory_dir / USER_MEMORY_FILE).is_file()
        assert (tree.project_memory_dir / PROJECT_MEMORY_FILE).is_file()

    def test_a_tombstoned_index_entry_is_not_resurrected(
        self, schema_tree, tmp_path, monkeypatch
    ):
        # The recorded case: a hygiene commit tombstones a dead entry (an
        # index line whose file is already gone) and an unrelated session's
        # routine persist reverts it line for line 25 minutes later.
        tree, _fixture_dir = schema_tree
        index = tree.project_memory_dir / "MEMORY.md"
        dead = _index_line("Flaky import perf", "project_flaky_import_perf.md")
        index.write_text(index.read_text() + dead)

        session = self._session(monkeypatch, tmp_path, "session-b")
        assert memory.restore_memory() == 0
        # The session's dir never contained the file — only the dead line.
        assert not (session / "project_flaky_import_perf.md").exists()

        tombstone = "- Flaky import perf — forgotten 2026-08-25 — test was fixed.\n"
        index.write_text(index.read_text().replace(dead, tombstone))

        self._add(session, "note", "project", "Note")
        assert _persist()[0] == 0

        after = index.read_text()
        assert tombstone in after
        assert "(project_flaky_import_perf.md)" not in after
        assert "note.md" in after

    def test_without_a_manifest_a_persist_deletes_nothing(self, schema_tree):
        # An older container, a restore that never ran, a hand-made memory
        # dir: no record of what this session restored degrades to "delete
        # nothing", never to "delete everything".
        tree, agent_dir = schema_tree
        (agent_dir / "gotcha.md").write_text(_memory_file("gotcha", "project"))
        (agent_dir / "prefs.md").write_text(_memory_file("prefs", "user"))

        assert _persist()[0] == 0

        assert (tree.project_memory_dir / PROJECT_MEMORY_FILE).is_file()
        assert (tree.instance_memory_dir / USER_MEMORY_FILE).is_file()
        assert (tree.project_memory_dir / "gotcha.md").is_file()
        assert (tree.instance_memory_dir / "prefs.md").is_file()

    def test_a_file_rewritten_since_this_session_restored_it_is_kept(
        self, schema_tree
    ):
        # Restored, dropped locally — but another session has rewritten the
        # work repo's copy since. Their update is newer than this session's
        # decision, so the deletion does not carry.
        tree, agent_dir = schema_tree
        assert memory.restore_memory() == 0
        (agent_dir / USER_MEMORY_FILE).unlink()
        (tree.instance_memory_dir / USER_MEMORY_FILE).write_text(
            _memory_file("terse", "user", body="rewritten by another session\n")
        )
        (agent_dir / "prefs.md").write_text(_memory_file("prefs", "user"))

        assert _persist()[0] == 0

        assert "rewritten by another session" in (
            tree.instance_memory_dir / USER_MEMORY_FILE
        ).read_text()

    def test_an_unchanged_restored_file_does_not_undo_upstream_deletion(
        self, schema_tree
    ):
        """A stale session has no opinion when upstream removed its old bytes."""
        tree, agent_dir = schema_tree
        upstream = tree.project_memory_dir / PROJECT_MEMORY_FILE
        assert memory.restore_memory() == 0
        restored_text = (agent_dir / PROJECT_MEMORY_FILE).read_text()

        # Another writer retires the memory after this session restored it.
        upstream.unlink()
        (agent_dir / "local.md").write_text(_memory_file("local", "project"))

        assert _persist()[0] == 0

        assert not upstream.exists(), "a stale unchanged copy revived the deletion"
        assert (agent_dir / PROJECT_MEMORY_FILE).read_text() == restored_text
        assert (tree.project_memory_dir / "local.md").is_file()

    def test_a_local_edit_of_an_upstream_deletion_is_an_explicit_conflict(
        self, schema_tree, capsys
    ):
        """A conflict preserves local content without accidentally reviving it."""
        tree, agent_dir = schema_tree
        upstream = tree.project_memory_dir / PROJECT_MEMORY_FILE
        local = agent_dir / PROJECT_MEMORY_FILE
        assert memory.restore_memory() == 0

        local.write_text(
            _memory_file(
                "selfdev-pythonpath-gotcha",
                "project",
                body="locally corrected while upstream retired it\n",
            )
        )
        upstream.unlink()

        assert _persist()[0] == 1

        assert not upstream.exists(), "a conflicted local edit revived the deletion"
        assert "locally corrected" in local.read_text()
        err = capsys.readouterr().err
        assert "conflict" in err.lower()
        assert PROJECT_MEMORY_FILE in err

    def test_the_manifest_is_not_work_repo_state(self, schema_tree):
        # It must survive the container and nothing else: not committed into
        # the work repo (where `lmer-schema validate` would have to learn it)
        # and not inside the memory dir persist mirrors.
        tree, agent_dir = schema_tree
        assert memory.restore_memory() == 0
        assert _persist()[0] == 0

        assert memory.restore_manifest_path().is_file()
        assert memory.restore_manifest_path().parent != agent_dir
        for root in (tree.root, agent_dir):
            assert not [
                p for p in root.rglob("*") if p.name == memory.RESTORE_MANIFEST_NAME
            ]


# ---------------------------------------------------------------------------
# Schema 2: explicit memory retirement
# ---------------------------------------------------------------------------


class TestSchema2MemoryRetirement:
    @staticmethod
    def _project_key(tree):
        return (tree.project_memory_dir / PROJECT_MEMORY_FILE).relative_to(
            tree.root
        ).as_posix()

    def test_forget_archives_content_and_records_a_structured_retirement(
        self, schema2_tree
    ):
        tree, _agent_dir = schema2_tree
        key = self._project_key(tree)
        source = tree.root / key
        original = source.read_text()

        changed = memory.forget_memory(
            key, metadata={"reason": "superseded", "ticket": 7}
        )

        archive_rel = f"{PROJECT}/_archive/_agents/memory/{PROJECT_MEMORY_FILE}"
        state = memory.read_memory_retirements()
        entry = state["retired"][key]
        assert not source.exists()
        assert (tree.root / archive_rel).read_text() == original
        assert entry == {
            "archive": archive_rel,
            "index_lines": [
                (
                    "- [Self-dev PYTHONPATH gotcha]"
                    f"({PROJECT_MEMORY_FILE}) — bare pytest imports the "
                    "operational install, prefix PYTHONPATH with the "
                    "checkout's src/."
                )
            ],
            "metadata": {"reason": "superseded", "ticket": 7},
        }
        index = (tree.project_memory_dir / memory.MEMORY_INDEX).read_text()
        assert f"({PROJECT_MEMORY_FILE})" not in index
        assert f"(../../_archive/_agents/memory/{PROJECT_MEMORY_FILE})" in index
        assert set(changed) == {
            key,
            archive_rel,
            f"{PROJECT}/_agents/memory/{memory.MEMORY_INDEX}",
            memory.RETIREMENT_STATE_NAME,
        }

    def test_revive_restores_archived_content_and_index_in_one_change_set(
        self, schema2_tree
    ):
        tree, _agent_dir = schema2_tree
        key = self._project_key(tree)
        original = (tree.root / key).read_text()
        memory.forget_memory(key, metadata={"reason": "mistake"})

        changed = memory.revive_memory(key)

        state = memory.read_memory_retirements()
        assert key not in state["retired"]
        assert (tree.root / key).read_text() == original
        assert not (
            tree.project_dir / "_archive" / "_agents" / "memory" / PROJECT_MEMORY_FILE
        ).exists()
        index = (tree.project_memory_dir / memory.MEMORY_INDEX).read_text()
        assert f"({PROJECT_MEMORY_FILE})" in index
        assert f"(_archive/_agents/memory/{PROJECT_MEMORY_FILE})" not in index
        assert memory.RETIREMENT_STATE_NAME in changed

    def test_unknown_top_level_and_entry_metadata_survive_other_key_updates(
        self, schema2_tree
    ):
        tree, _agent_dir = schema2_tree
        project_key = self._project_key(tree)
        instance_key = (
            tree.instance_memory_dir / USER_MEMORY_FILE
        ).relative_to(tree.root).as_posix()
        memory.forget_memory(project_key)
        memory.forget_memory(instance_key)
        state_path = memory.retirement_state_path()
        state = json.loads(state_path.read_text())
        state["extension"] = {"owner": "future-writer"}
        state["retired"][instance_key]["future"] = ["opaque", 2]
        state_path.write_text(json.dumps(state))

        memory.revive_memory(project_key)

        after = json.loads(state_path.read_text())
        assert after["extension"] == {"owner": "future-writer"}
        assert after["retired"][instance_key]["future"] == ["opaque", 2]

    def test_restore_filters_retired_content_even_if_an_old_writer_readded_it(
        self, schema2_tree
    ):
        tree, agent_dir = schema2_tree
        key = self._project_key(tree)
        stale = (tree.root / key).read_text()
        memory.forget_memory(key)
        # Simulate an old client that cannot read the marker and copied stale
        # bytes back into the live scope.
        (tree.root / key).write_text(stale)

        assert memory.restore_memory() == 0

        assert not (agent_dir / PROJECT_MEMORY_FILE).exists()
        index = (agent_dir / memory.MEMORY_INDEX).read_text()
        assert f"({PROJECT_MEMORY_FILE})" not in index
        assert f"_archive/_agents/memory/{PROJECT_MEMORY_FILE}" in index
        assert (agent_dir / USER_MEMORY_FILE).is_file()

    def test_persist_without_a_restore_baseline_cannot_revive_retired_content(
        self, schema2_tree
    ):
        tree, agent_dir = schema2_tree
        key = self._project_key(tree)
        stale = (tree.root / key).read_text()
        memory.forget_memory(key)
        (agent_dir / PROJECT_MEMORY_FILE).write_text(stale)
        (agent_dir / memory.MEMORY_INDEX).write_text(
            _index_line("Stale", PROJECT_MEMORY_FILE)
        )

        assert _persist()[0] == 0

        assert not (tree.root / key).exists()
        index = (tree.project_memory_dir / memory.MEMORY_INDEX).read_text()
        assert f"({PROJECT_MEMORY_FILE})" not in index

    def test_empty_agent_persist_enforces_marker_against_old_writer_revival(
        self, schema2_tree
    ):
        tree, agent_dir = schema2_tree
        key = self._project_key(tree)
        stale = (tree.root / key).read_text()
        memory.forget_memory(key)
        (tree.root / key).write_text(stale)
        assert not list(agent_dir.iterdir())

        assert _persist()[0] == 0

        assert not (tree.root / key).exists()

    def test_malformed_state_is_rejected_before_forget_mutates_anything(
        self, schema2_tree
    ):
        tree, _agent_dir = schema2_tree
        key = self._project_key(tree)
        source = tree.root / key
        index = tree.project_memory_dir / memory.MEMORY_INDEX
        before_source = source.read_bytes()
        before_index = index.read_bytes()
        malformed = b'{"version": 1, "retired": [], "sentinel": "keep"}\n'
        memory.retirement_state_path().write_bytes(malformed)

        with pytest.raises(memory.MemoryRetirementError, match="retired"):
            memory.forget_memory(key)

        assert source.read_bytes() == before_source
        assert index.read_bytes() == before_index
        assert memory.retirement_state_path().read_bytes() == malformed
        assert not (tree.project_dir / "_archive").exists()

    def test_malformed_state_makes_restore_fail_without_mutating_agent_memory(
        self, schema2_tree, capsys
    ):
        _tree, agent_dir = schema2_tree
        sentinel = agent_dir / "sentinel.md"
        sentinel.write_text("local content\n")
        malformed = b'{"version": 1, "retired": {"/escape.md": {}}}\n'
        memory.retirement_state_path().write_bytes(malformed)

        assert memory.restore_memory() == 1

        assert sentinel.read_text() == "local content\n"
        assert memory.retirement_state_path().read_bytes() == malformed
        assert "retirement" in capsys.readouterr().err.lower()

    def test_schema1_restore_ignores_schema2_retirement_state(self, schema_tree):
        tree, agent_dir = schema_tree
        key = self._project_key(tree)
        memory.retirement_state_path().write_text(
            json.dumps(
                {
                    "version": 1,
                    "retired": {
                        key: {
                            "archive": (
                                f"{PROJECT}/_archive/_agents/memory/"
                                f"{PROJECT_MEMORY_FILE}"
                            ),
                            "index_lines": [],
                        }
                    },
                }
            )
        )

        assert memory.restore_memory() == 0

        assert (agent_dir / PROJECT_MEMORY_FILE).is_file()

    def test_non_json_metadata_is_rejected_before_forget_mutates_files(
        self, schema2_tree
    ):
        tree, _agent_dir = schema2_tree
        key = self._project_key(tree)
        source = tree.root / key
        index = tree.project_memory_dir / memory.MEMORY_INDEX
        before_source = source.read_bytes()
        before_index = index.read_bytes()

        with pytest.raises(memory.MemoryRetirementError, match="JSON"):
            memory.forget_memory(key, metadata={"not-json": {1, 2}})

        assert source.read_bytes() == before_source
        assert index.read_bytes() == before_index
        assert not memory.retirement_state_path().exists()
        assert not (tree.project_dir / "_archive").exists()

    def test_forget_rolls_back_when_retirement_state_write_fails(
        self, schema2_tree
    ):
        tree, _agent_dir = schema2_tree
        key = self._project_key(tree)
        source = tree.root / key
        index = tree.project_memory_dir / memory.MEMORY_INDEX
        before_source = source.read_bytes()
        before_index = index.read_bytes()

        with patch.object(
            memory,
            "_write_memory_retirements",
            side_effect=OSError("state disk full"),
        ):
            with pytest.raises(memory.MemoryRetirementError, match="state disk full"):
                memory.forget_memory(key)

        assert source.read_bytes() == before_source
        assert index.read_bytes() == before_index
        assert not memory.retirement_state_path().exists()
        assert not (
            tree.project_dir / "_archive" / "_agents" / "memory" / source.name
        ).exists()

    def test_revive_rolls_back_when_retirement_state_write_fails(
        self, schema2_tree
    ):
        tree, _agent_dir = schema2_tree
        key = self._project_key(tree)
        memory.forget_memory(key)
        state_path = memory.retirement_state_path()
        state_before = state_path.read_bytes()
        index = tree.project_memory_dir / memory.MEMORY_INDEX
        index_before = index.read_bytes()
        archive = (
            tree.project_dir
            / "_archive"
            / "_agents"
            / "memory"
            / PROJECT_MEMORY_FILE
        )
        archive_before = archive.read_bytes()

        with patch.object(
            memory,
            "_write_memory_retirements",
            side_effect=OSError("state read-only"),
        ):
            with pytest.raises(memory.MemoryRetirementError, match="state read-only"):
                memory.revive_memory(key)

        assert not (tree.root / key).exists()
        assert archive.read_bytes() == archive_before
        assert index.read_bytes() == index_before
        assert state_path.read_bytes() == state_before

    @staticmethod
    def _delay_first_state_write(monkeypatch):
        """Hold writer one until writer two publishes, exposing lost updates.

        Under the per-root lock writer two cannot enter; the short timeout
        releases writer one, after which writer two reads its completed state.
        Without the lock writer two publishes immediately and writer one's
        stale document overwrites it.
        """
        real_write = memory._write_memory_retirements
        guard = threading.Lock()
        first_entered = threading.Event()
        second_published = threading.Event()
        calls = 0

        def delayed(payload, root):
            nonlocal calls
            with guard:
                calls += 1
                position = calls
            if position == 1:
                first_entered.set()
                second_published.wait(timeout=0.2)
            result = real_write(payload, root)
            if position == 2:
                second_published.set()
            return result

        monkeypatch.setattr(memory, "_write_memory_retirements", delayed)
        return first_entered

    def test_two_concurrent_forgets_preserve_both_retirement_markers(
        self, schema2_tree, monkeypatch
    ):
        tree, _agent_dir = schema2_tree
        project_key = self._project_key(tree)
        instance_key = (
            tree.instance_memory_dir / USER_MEMORY_FILE
        ).relative_to(tree.root).as_posix()
        first_entered = self._delay_first_state_write(monkeypatch)
        errors = []

        def forget(key):
            try:
                memory.forget_memory(key)
            except Exception as exc:  # pragma: no cover - assertion reports it
                errors.append(exc)

        first = threading.Thread(target=forget, args=(project_key,))
        second = threading.Thread(target=forget, args=(instance_key,))
        first.start()
        assert first_entered.wait(timeout=2), "first writer never reached state write"
        second.start()
        first.join(timeout=5)
        second.join(timeout=5)

        assert not first.is_alive() and not second.is_alive()
        assert not errors, errors
        assert set(memory.read_memory_retirements()["retired"]) == {
            project_key,
            instance_key,
        }

    def test_forget_waits_for_conflicting_persist_mutation(
        self, schema2_tree, monkeypatch
    ):
        tree, agent_dir = schema2_tree
        key = self._project_key(tree)
        assert memory.restore_memory() == 0
        assert (agent_dir / PROJECT_MEMORY_FILE).is_file()
        real_persist_scopes = memory._persist_scopes
        persist_entered = threading.Event()
        forget_finished = threading.Event()

        def delayed_persist(source, scopes, retired):
            persist_entered.set()
            forget_finished.wait(timeout=0.2)
            return real_persist_scopes(source, scopes, retired)

        monkeypatch.setattr(memory, "_persist_scopes", delayed_persist)
        monkeypatch.setattr(memory, "commit_work_path", lambda *_args: 0)
        errors = []

        def persist():
            try:
                assert memory.persist_memory() == 0
            except Exception as exc:  # pragma: no cover - assertion reports it
                errors.append(exc)

        def forget():
            try:
                memory.forget_memory(key)
            except Exception as exc:  # pragma: no cover - assertion reports it
                errors.append(exc)
            finally:
                forget_finished.set()

        first = threading.Thread(target=persist)
        second = threading.Thread(target=forget)
        first.start()
        assert persist_entered.wait(timeout=2), "persist never reached scope mutation"
        second.start()
        first.join(timeout=5)
        second.join(timeout=5)

        assert not first.is_alive() and not second.is_alive()
        assert not errors, errors
        assert key in memory.read_memory_retirements()["retired"]
        assert not (tree.root / key).exists()

    def test_two_concurrent_revives_remove_both_retirement_markers(
        self, schema2_tree, monkeypatch
    ):
        tree, _agent_dir = schema2_tree
        project_key = self._project_key(tree)
        instance_key = (
            tree.instance_memory_dir / USER_MEMORY_FILE
        ).relative_to(tree.root).as_posix()
        memory.forget_memory(project_key)
        memory.forget_memory(instance_key)
        first_entered = self._delay_first_state_write(monkeypatch)
        errors = []

        def revive(key):
            try:
                memory.revive_memory(key)
            except Exception as exc:  # pragma: no cover - assertion reports it
                errors.append(exc)

        first = threading.Thread(target=revive, args=(project_key,))
        second = threading.Thread(target=revive, args=(instance_key,))
        first.start()
        assert first_entered.wait(timeout=2), "first writer never reached state write"
        second.start()
        first.join(timeout=5)
        second.join(timeout=5)

        assert not first.is_alive() and not second.is_alive()
        assert not errors, errors
        assert memory.read_memory_retirements()["retired"] == {}
